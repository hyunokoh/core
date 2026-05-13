#!/usr/bin/env python3
"""Simulate a network partition by dropping traffic to a single service.

macOS uses pfctl (packet filter), Linux uses iptables.  Both require sudo,
so this script PRINTS the command rather than executing it -- copy/paste to
run.  The teardown line is also printed.

    python3 chaos/network_partition.py --target 5503 --duration 30s

You'll see something like:

    Would run:
      sudo pfctl -a chaos_zkcex -f - <<'EOF'
      block drop in proto tcp from any to 127.0.0.1 port 5503
      EOF
    After 30s, undo with:
      sudo pfctl -a chaos_zkcex -F all

Use this to test what happens when, say, the AML provider is unreachable
or the chain RPC times out.
"""

from __future__ import annotations

import argparse
import platform
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from chaos import service_registry as reg  # noqa: E402


def parse_duration(s: str) -> float:
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


def emit_macos(port: int, duration: float) -> None:
    print("Would run:")
    print(
        f"  (echo 'block drop in proto tcp from any to 127.0.0.1 port {port}'; "
        f"echo 'block drop out proto tcp from any to 127.0.0.1 port {port}') | "
        f"sudo pfctl -a chaos_zkcex -f -"
    )
    print("  sudo pfctl -E")
    print()
    print(f"After {duration:.0f}s, undo with:")
    print("  sudo pfctl -a chaos_zkcex -F all")


def emit_linux(port: int, duration: float) -> None:
    print("Would run:")
    print(f"  sudo iptables -I INPUT  -p tcp --dport {port} -j DROP")
    print(f"  sudo iptables -I OUTPUT -p tcp --sport {port} -j DROP")
    print()
    print(f"After {duration:.0f}s, undo with:")
    print(f"  sudo iptables -D INPUT  -p tcp --dport {port} -j DROP")
    print(f"  sudo iptables -D OUTPUT -p tcp --sport {port} -j DROP")


def main() -> int:
    ap = argparse.ArgumentParser(description="Simulate network partition.")
    ap.add_argument("--target", type=int, required=True, help="Port to isolate.")
    ap.add_argument("--duration", default="30s")
    args = ap.parse_args()

    if not reg.is_safe_port(args.target):
        print(
            f"refuse: port {args.target} is outside the safe range "
            f"{reg.SAFE_PORT_MIN}-{reg.SAFE_PORT_MAX}",
            file=sys.stderr,
        )
        return 2

    spec = reg.get(args.target)
    if spec is None:
        print(f"warn: port {args.target} not in service registry; proceeding anyway")
    else:
        if spec.tier == "CRITICAL":
            print(
                f"refuse: {spec.name}:{spec.port} is CRITICAL; partitioning "
                f"it would take down the demo.",
                file=sys.stderr,
            )
            return 2
        print(f"target: {spec.name}:{spec.port} (tier={spec.tier})")

    secs = parse_duration(args.duration)
    sysname = platform.system()
    if sysname == "Darwin":
        emit_macos(args.target, secs)
    elif sysname == "Linux":
        emit_linux(args.target, secs)
    else:
        print(f"unsupported platform: {sysname}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
