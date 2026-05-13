#!/usr/bin/env python3
"""Background supervisor for chaos-killed services.

Run this in a separate terminal *before* starting kill_loop.py and it will
poll the service registry every few seconds, noticing dead ports and
restarting them with the canonical command from ``service_registry``.

    python3 chaos/supervisor.py --poll 5

Kill switch:
    KILL_CHAOS=1 pkill -f supervisor.py
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from chaos import service_registry as reg  # noqa: E402


def is_port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="zkCEX chaos supervisor.")
    ap.add_argument(
        "--poll", type=float, default=5.0, help="Seconds between health checks (default 5)."
    )
    ap.add_argument(
        "--include-critical", action="store_true", help="Also restart CRITICAL services if dead."
    )
    args = ap.parse_args()

    if os.environ.get("KILL_CHAOS"):
        log("KILL_CHAOS=1 detected, refusing to run")
        return 1

    services = list(reg.REGISTRY.values())
    if not args.include_critical:
        services = [s for s in services if s.tier != "CRITICAL"]

    log(f"supervisor watching {len(services)} services, poll={args.poll}s")

    # Track last restart attempt so we don't loop on a broken service.
    last_restart_at: dict[int, float] = {}
    RESTART_BACKOFF = 15.0

    try:
        while True:
            if os.environ.get("KILL_CHAOS"):
                log("kill switch engaged, stopping")
                break
            for spec in services:
                if is_port_open(spec.port):
                    continue
                now = time.monotonic()
                if now - last_restart_at.get(spec.port, 0.0) < RESTART_BACKOFF:
                    continue
                last_restart_at[spec.port] = now
                log(f"restart {spec.name}:{spec.port} via {spec.restart_cmd}")
                try:
                    subprocess.Popen(  # noqa: S603 - restart_cmd comes from service_registry.
                        spec.restart_cmd,
                        cwd=str(reg.PROJECT_ROOT),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                except Exception as e:
                    log(f"restart failed for {spec.name}: {e}")
            time.sleep(args.poll)
    except KeyboardInterrupt:
        log("stopped (^C)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
