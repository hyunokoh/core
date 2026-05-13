#!/usr/bin/env python3
"""Clock skew chaos for HMAC timestamp validation.

Real exchanges use signed requests of the form

    GET /v3/account?timestamp=<ms-since-epoch>&recvWindow=5000&signature=...

If the server's clock drifts from the client's clock by more than the
recvWindow, perfectly-signed requests get rejected with 401 / "timestamp
outside recvWindow".  This script verifies that behaviour from the *client*
side -- it sends a request whose `timestamp` is intentionally skewed by N
seconds and asserts the server rejects it.

    python3 chaos/clock_skew.py \
        --base-url http://localhost:5500 \
        --skew-sec -120  --recv-window 5000

It does NOT change the OS clock (that would require sudo and is risky).
For per-service clock injection there's a hook in each demo service that
honors $MOCK_TIME_OFFSET (seconds); restart the target service with that env
set and this script will detect whether the server still validates honest
client requests properly.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


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


def hmac_hex(secret: str, msg: str) -> str:
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def signed_get(
    base: str, path: str, params: dict, *, api_key: str, api_sec: str, timeout: float = 5.0
):
    q = urllib.parse.urlencode(params, safe="-._~")
    sig = hmac_hex(api_sec, q)
    url = f"{base}{path}?{q}&signature={sig}"
    req = _http_request(url, headers={"X-MBX-APIKEY": api_key})
    try:
        with _http_urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        return 0, str(e).encode()


def main() -> int:
    ap = argparse.ArgumentParser(description="Clock skew chaos.")
    ap.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://localhost:5500"))
    ap.add_argument("--api-key", default=os.environ.get("API_KEY", "demo-key"))
    ap.add_argument("--api-secret", default=os.environ.get("API_SECRET", "demo-secret"))
    ap.add_argument(
        "--skew-sec",
        type=float,
        default=-120,
        help="Seconds to add to client timestamp (negative = past).",
    )
    ap.add_argument(
        "--recv-window", type=int, default=5000, help="recvWindow in ms (default 5000)."
    )
    args = ap.parse_args()

    now_ms = int(time.time() * 1000)
    honest_ts = now_ms
    skewed_ts = now_ms + int(args.skew_sec * 1000)

    print("clock-skew chaos:")
    print(f"  base       = {args.base_url}")
    print(f"  skew       = {args.skew_sec:+.1f} s")
    print(f"  recvWindow = {args.recv_window} ms")
    print()

    # 1) sanity: honest request should pass
    status, body = signed_get(
        args.base_url,
        "/v3/account",
        {"timestamp": honest_ts, "recvWindow": args.recv_window},
        api_key=args.api_key,
        api_sec=args.api_secret,
    )
    print(f"honest request : status={status}   body={body[:120]!r}")

    # 2) skewed request -- expect rejection (4xx, ideally 401 or -1021)
    status, body = signed_get(
        args.base_url,
        "/v3/account",
        {"timestamp": skewed_ts, "recvWindow": args.recv_window},
        api_key=args.api_key,
        api_sec=args.api_secret,
    )
    print(f"skewed request : status={status}   body={body[:120]!r}")

    rejected = 400 <= status < 500
    abs_skew_ms = abs(args.skew_sec * 1000)
    if abs_skew_ms > args.recv_window and rejected:
        print("\nPASS  skewed request was rejected as expected.")
        return 0
    if abs_skew_ms <= args.recv_window and status == 200:
        print("\nPASS  honest request inside recvWindow was accepted.")
        return 0
    print("\nFAIL  server did NOT enforce recvWindow correctly.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
