#!/usr/bin/env python3
"""Chaos monkey for zkCEX demo services.

Every N seconds (default 60), pick a random non-critical Python service from
``service_registry.REGISTRY``, send it SIGTERM, then probe its /health 30 s
later to see whether something brought it back.  Logs every action to
``chaos.log`` (next to this script).

Service classification (see service_registry.py):
  CRITICAL       proxy, auth, chain                  -- never killed
  NORMAL         pol, mm-bot, ws-feed, ...           -- eligible
  EXPERIMENTAL   custody-signer, coord, ...          -- killed more often

Usage:
    python3 chaos/kill_loop.py --interval 60 --duration 600
    python3 chaos/kill_loop.py --kill 5600           # one-shot, kill these
    python3 chaos/kill_loop.py --kill 5600 --no-supervisor  # don't auto-restart

Kill switch:
    KILL_CHAOS=1 pkill -f kill_loop.py

Safety:
  * Refuses to kill any port outside SAFE_PORT_MIN..SAFE_PORT_MAX (5500-5640).
  * Refuses to kill CRITICAL services even if explicitly listed.
  * Never touches the zkCEX docker compose stack (ports 8091, 8094, etc.).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # so 'chaos.x' imports work
from chaos import service_registry as reg  # noqa: E402

LOG_PATH = HERE / "chaos.log"


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------


def log(event: str, **kw) -> None:
    line = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **kw}
    msg = json.dumps(line, default=str)
    print(msg, flush=True)
    try:
        with LOG_PATH.open("a") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def _require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise FileNotFoundError(name)
    return path


def _validated_http_url(raw_url: str, *, name: str = "url") -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return raw_url


def _http_urlopen(target, **kwargs):
    if isinstance(target, str):
        target = _validated_http_url(target)
    return urllib.request.urlopen(target, **kwargs)  # noqa: S310


# ---------------------------------------------------------------------------
# port -> pid lookup (lsof, portable enough for macOS + Linux)
# ---------------------------------------------------------------------------


def pid_listening_on(port: int) -> list[int]:
    """Return PIDs that are listening on ``port`` (TCP)."""
    try:
        out = subprocess.run(  # noqa: S603 - lsof is resolved before invocation.
            [_require_tool("lsof"), "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    return [int(p) for p in out.stdout.split() if p.strip().isdigit()]


def kill_pid(pid: int, sig: int = signal.SIGTERM) -> bool:
    try:
        os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError) as e:
        log("kill_failed", pid=pid, error=str(e))
        return False


def is_port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def health_probe(spec: reg.ServiceSpec, *, via_proxy: bool = True, timeout: float = 2.0) -> int:
    """Return HTTP status from the service's health endpoint, or 0 on conn err."""
    if via_proxy and spec.health_path.startswith("/"):
        url = f"http://127.0.0.1:5500{spec.health_path}"
    else:
        url = f"http://127.0.0.1:{spec.port}{spec.health_path}"
    try:
        with _http_urlopen(url, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return 0


# ---------------------------------------------------------------------------
# core kill action
# ---------------------------------------------------------------------------


def kill_service(
    spec: reg.ServiceSpec, *, observe_secs: float = 30.0, supervisor: bool = True
) -> dict:
    """SIGTERM ``spec``, wait ``observe_secs``, probe /health, return summary."""
    if spec.tier == "CRITICAL":
        log("refuse_kill_critical", port=spec.port, name=spec.name)
        return {"port": spec.port, "refused": True, "reason": "critical"}

    if not reg.is_safe_port(spec.port):
        log("refuse_unsafe_port", port=spec.port)
        return {"port": spec.port, "refused": True, "reason": "unsafe_port"}

    pids = pid_listening_on(spec.port)
    if not pids:
        log("not_running", port=spec.port, name=spec.name)
        return {"port": spec.port, "name": spec.name, "killed": False, "reason": "not_running"}

    log("kill", port=spec.port, name=spec.name, pids=pids, tier=spec.tier)
    for pid in pids:
        kill_pid(pid, signal.SIGTERM)

    # wait for the socket to actually close
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not is_port_open(spec.port):
            time.monotonic()
            break
        time.sleep(0.1)

    # observation window -- watch for auto-restart
    t0_obs = time.monotonic()
    restart_at: float | None = None
    while time.monotonic() - t0_obs < observe_secs:
        if is_port_open(spec.port):
            restart_at = time.monotonic() - t0_obs
            break
        time.sleep(0.5)

    status = health_probe(spec) if restart_at is not None else 0
    log(
        "post_kill",
        port=spec.port,
        name=spec.name,
        restarted=(restart_at is not None),
        restart_after_sec=round(restart_at, 2) if restart_at else None,
        health_status=status,
    )

    # if no auto-restart and supervisor is on, restart it ourselves
    if restart_at is None and supervisor:
        log("supervisor_restart", port=spec.port, cmd=spec.restart_cmd)
        try:
            subprocess.Popen(  # noqa: S603 - restart_cmd comes from service_registry.
                spec.restart_cmd,
                cwd=str(reg.PROJECT_ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:
            log("supervisor_restart_failed", port=spec.port, error=str(e))

    return {
        "port": spec.port,
        "name": spec.name,
        "tier": spec.tier,
        "pids": pids,
        "killed": True,
        "restarted_by_self": restart_at is not None,
        "restart_after_sec": restart_at,
        "health_status_after": status,
    }


# ---------------------------------------------------------------------------
# loop driver
# ---------------------------------------------------------------------------


def weighted_pick(killables: list[reg.ServiceSpec]) -> reg.ServiceSpec:
    """EXPERIMENTAL services get 3x weight relative to NORMAL."""
    weights = [3 if s.tier == "EXPERIMENTAL" else 1 for s in killables]
    pick = secrets.randbelow(sum(weights))
    acc = 0
    for spec, weight in zip(killables, weights, strict=True):
        acc += weight
        if pick < acc:
            return spec
    return killables[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description="zkCEX chaos monkey.")
    ap.add_argument(
        "--interval", type=float, default=60.0, help="Seconds between kills (default 60)."
    )
    ap.add_argument("--duration", type=float, default=600.0, help="Total run time (default 600s).")
    ap.add_argument(
        "--observe", type=float, default=30.0, help="Seconds to wait for auto-restart (default 30)."
    )
    ap.add_argument(
        "--kill",
        type=int,
        nargs="+",
        metavar="PORT",
        help="One-shot mode: kill these ports and exit.",
    )
    ap.add_argument(
        "--no-supervisor", action="store_true", help="Don't auto-restart killed services."
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="Pick a victim but do not actually kill."
    )
    args = ap.parse_args()

    if os.environ.get("KILL_CHAOS"):
        print("KILL_CHAOS=1 detected, refusing to run", file=sys.stderr)
        return 1

    supervisor = not args.no_supervisor

    # one-shot mode
    if args.kill:
        results = []
        for port in args.kill:
            spec = reg.get(port)
            if spec is None:
                log("unknown_port", port=port)
                continue
            if args.dry_run:
                log("dry_run_kill", port=port, name=spec.name)
                continue
            results.append(kill_service(spec, observe_secs=args.observe, supervisor=supervisor))
        print(json.dumps(results, indent=2, default=str))
        return 0

    # loop mode
    killables = reg.killable()
    log(
        "start_loop",
        interval=args.interval,
        duration=args.duration,
        eligible_services=[s.name for s in killables],
    )

    t0 = time.monotonic()
    while time.monotonic() - t0 < args.duration:
        if os.environ.get("KILL_CHAOS"):
            log("kill_switch_engaged")
            break
        victim = weighted_pick(killables)
        if args.dry_run:
            log("dry_run_pick", port=victim.port, name=victim.name)
        else:
            kill_service(victim, observe_secs=args.observe, supervisor=supervisor)
        time.sleep(args.interval)

    log("stop_loop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
