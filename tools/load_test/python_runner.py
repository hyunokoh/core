#!/usr/bin/env python3
"""Pure-stdlib load runner for environments without k6.

Hits the same endpoints the k6 scripts hit (market-data, balance lookup,
chain polling, auth flow).  Less sophisticated than k6 -- no ramp stages,
no thresholds, no live dashboard -- but it has zero install footprint.

    python3 tools/load_test/python_runner.py \
        --scenario market-data \
        --users 100 --duration 60s

Available scenarios:
  market-data       depth + ticker + trades
  account           signed GET /v3/account
  chain             /chain/info + /chain/wallet
  auth              signup -> login -> /auth/me

Output goes to stdout AND to a JSON summary at
``tools/load_test/results/python-<scenario>.json`` with the same shape as
k6 --summary-export so build_report.py can render either.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

SYMBOLS = ["ETHUSDT", "BTCUSDT", "SOLUSDT", "DOGEUSDT"]
MID = {"ETHUSDT": 100, "BTCUSDT": 50000, "SOLUSDT": 20, "DOGEUSDT": 0.1}


def parse_duration(s: str) -> float:
    """Accept '60', '60s', '2m', '1h' -> seconds (float)."""
    s = s.strip().lower()
    if s.endswith("ms"):
        return float(s[:-2]) / 1000
    if s.endswith("s"):
        return float(s[:-1])
    if s.endswith("m"):
        return float(s[:-1]) * 60
    if s.endswith("h"):
        return float(s[:-1]) * 3600
    return float(s)


def hmac_sha256_hex(secret: str, msg: str) -> str:
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def qs(params: dict) -> str:
    return urllib.parse.urlencode(params, safe="-._~")


def _validated_http_url(raw_url: str, *, name: str = "url") -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return raw_url


def _http_request(url: str, **kwargs) -> urllib.request.Request:
    return urllib.request.Request(_validated_http_url(url), **kwargs)  # noqa: S310


def _http_urlopen(target, **kwargs):
    if isinstance(target, str):
        target = _validated_http_url(target)
    return urllib.request.urlopen(target, **kwargs)  # noqa: S310


# ---------------------------------------------------------------------------
# single request worker (thread-safe stats accumulator)
# ---------------------------------------------------------------------------


class Stats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.durations_ms: list[float] = []
        self.errors = 0
        self.ok = 0
        self.status_codes: dict[int, int] = {}
        self.total_bytes = 0

    def record(self, t_ms: float, status: int, n_bytes: int, ok: bool) -> None:
        with self.lock:
            self.durations_ms.append(t_ms)
            self.status_codes[status] = self.status_codes.get(status, 0) + 1
            self.total_bytes += n_bytes
            if ok:
                self.ok += 1
            else:
                self.errors += 1

    def snapshot(self) -> dict:
        with self.lock:
            d = sorted(self.durations_ms)
            n = len(d)

            def pct(p: float) -> float:
                if not d:
                    return 0.0
                i = max(0, min(n - 1, int(round((p / 100) * (n - 1)))))
                return d[i]

            total = self.ok + self.errors
            return {
                "total_requests": total,
                "ok": self.ok,
                "errors": self.errors,
                "error_rate": (self.errors / total) if total else 0.0,
                "total_bytes": self.total_bytes,
                "latency_ms": {
                    "avg": statistics.fmean(d) if d else 0.0,
                    "min": d[0] if d else 0.0,
                    "max": d[-1] if d else 0.0,
                    "p50": pct(50),
                    "p90": pct(90),
                    "p95": pct(95),
                    "p99": pct(99),
                },
                "status_codes": dict(self.status_codes),
            }


def do_request(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    body: bytes | None = None,
    timeout: float = 5.0,
) -> tuple[float, int, int, bool]:
    """Return (duration_ms, status, n_bytes, ok)."""
    req = _http_request(url, data=body, method=method, headers=headers or {})
    t0 = time.perf_counter()
    try:
        with _http_urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        dur = (time.perf_counter() - t0) * 1000
        return dur, resp.status, len(data), True
    except urllib.error.HTTPError as e:
        dur = (time.perf_counter() - t0) * 1000
        try:
            data = e.read()
        except Exception:
            data = b""
        # treat 4xx as a delivered response, only 5xx counts as error
        return dur, e.code, len(data), e.code < 500
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        dur = (time.perf_counter() - t0) * 1000
        return dur, 0, 0, False


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------


def scenario_market_data(base: str, stats: Stats, *, api_key="", api_sec="") -> None:
    s = secrets.choice(SYMBOLS)
    for url in (
        f"{base}/v3/depth?symbol={s}&limit=20",
        f"{base}/v3/ticker/24h?symbol={s}",
        f"{base}/v3/trades?symbol={s}&limit=50",
    ):
        stats.record(*do_request("GET", url))


def scenario_account(base: str, stats: Stats, *, api_key="", api_sec="") -> None:
    params = {"timestamp": int(time.time() * 1000), "recvWindow": 5000}
    q = qs(params)
    sig = hmac_sha256_hex(api_sec or "demo-secret", q)
    url = f"{base}/v3/account?{q}&signature={sig}"
    headers = {"X-MBX-APIKEY": api_key or "demo-key"}
    stats.record(*do_request("GET", url, headers=headers))


def scenario_chain(base: str, stats: Stats, *, api_key="", api_sec="") -> None:
    for url in (
        f"{base}/chain/info",
        f"{base}/chain/wallet?address=0x0000000000000000000000000000000000000001",
    ):
        stats.record(*do_request("GET", url))


def scenario_auth(base: str, stats: Stats, *, api_key="", api_sec="") -> None:
    email = f"lt+{threading.get_ident()}-{secrets.randbelow(10**9) + 1}@example.test"
    pw = "CorrectHorse-Battery-7!"
    body = json.dumps({"email": email, "password": pw}).encode()
    hdr = {"Content-Type": "application/json"}

    stats.record(*do_request("POST", f"{base}/auth/signup", headers=hdr, body=body))
    dur, status, n, ok = do_request("POST", f"{base}/auth/login", headers=hdr, body=body)
    stats.record(dur, status, n, ok)


SCENARIOS = {
    "market-data": scenario_market_data,
    "account": scenario_account,
    "chain": scenario_chain,
    "auth": scenario_auth,
}


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------


def worker(
    stop_at: float, fn, base: str, stats: Stats, api_key: str, api_sec: str, think_ms: float
) -> None:
    while time.monotonic() < stop_at:
        try:
            fn(base, stats, api_key=api_key, api_sec=api_sec)
        except Exception:  # don't let one VU die
            stats.record(0.0, 0, 0, False)
        # think-time between iterations -- keeps us from melting localhost
        # via TIME_WAIT exhaustion on HTTP/1.0 endpoints.
        if think_ms > 0:
            time.sleep(think_ms / 1000.0)


def main() -> int:
    ap = argparse.ArgumentParser(description="Pure-stdlib load runner.")
    ap.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    ap.add_argument("--users", type=int, default=20)
    ap.add_argument("--duration", default="30s", help="e.g. '60s', '2m'")
    ap.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://localhost:5500"))
    ap.add_argument("--api-key", default=os.environ.get("API_KEY", "demo-key"))
    ap.add_argument("--api-secret", default=os.environ.get("API_SECRET", "demo-secret"))
    ap.add_argument("--out-dir", default=str(Path(__file__).parent / "results"))
    ap.add_argument(
        "--think-ms",
        type=float,
        default=100,
        help="Sleep between iterations per VU (default 100 ms).  "
        "Lower = more load, but Python stdlib HTTP/1.0 "
        "backends on localhost will TIME_WAIT out fast.",
    )
    args = ap.parse_args()

    secs = parse_duration(args.duration)
    print(
        f"scenario={args.scenario} users={args.users} duration={secs:.0f}s "
        f"base={args.base_url}",
        flush=True,
    )

    stats = Stats()
    fn = SCENARIOS[args.scenario]
    t0 = time.monotonic()
    stop = t0 + secs

    threads = [
        threading.Thread(
            target=worker,
            args=(stop, fn, args.base_url, stats, args.api_key, args.api_secret, args.think_ms),
            daemon=True,
        )
        for _ in range(args.users)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=secs + 30)

    elapsed = time.monotonic() - t0
    snap = stats.snapshot()
    snap["scenario"] = args.scenario
    snap["users"] = args.users
    snap["duration_sec"] = elapsed
    snap["rps"] = (snap["total_requests"] / elapsed) if elapsed else 0.0

    print("\n=== summary ===")
    print(f"total requests : {snap['total_requests']}")
    print(f"rps            : {snap['rps']:.1f}")
    print(f"error rate     : {snap['error_rate']*100:.2f}%")
    print(f"latency  avg   : {snap['latency_ms']['avg']:.1f} ms")
    print(f"latency  p50   : {snap['latency_ms']['p50']:.1f} ms")
    print(f"latency  p95   : {snap['latency_ms']['p95']:.1f} ms")
    print(f"latency  p99   : {snap['latency_ms']['p99']:.1f} ms")
    print(f"status codes   : {snap['status_codes']}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"python-{args.scenario}.json"
    out_file.write_text(json.dumps(snap, indent=2))
    print(f"\nwrote {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
