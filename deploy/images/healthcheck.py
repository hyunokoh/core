#!/usr/bin/env python3
"""Distroless-friendly HTTP health probe.

Distroless images have no shell, no curl, no wget. This script replaces the
old ``curl -fsS http://127.0.0.1:$PORT/$PATH`` HEALTHCHECK.

Usage (inside a Dockerfile HEALTHCHECK):

    HEALTHCHECK ... CMD ["python", "/app/healthcheck.py", "5503", "/pol/server-info"]

Exits 0 on HTTP 2xx, non-zero otherwise. Timeout is honoured via socket
default — set HEALTHCHECK_TIMEOUT for an explicit value (seconds, default 2).

Why not just inline a ``python -c "import urllib..."`` in each Dockerfile?
Three reasons:
  1. Keeps each service Dockerfile to a single readable line.
  2. Single point to fix if probe behaviour needs to change (TLS, headers,
     redirect handling).
  3. Easier to test from a shell during local dev.
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: healthcheck.py <port> <path> [host]", file=sys.stderr)
        return 2
    port = sys.argv[1]
    path = sys.argv[2]
    host = sys.argv[3] if len(sys.argv) > 3 else "127.0.0.1"
    if not path.startswith("/"):
        path = "/" + path
    timeout = float(os.environ.get("HEALTHCHECK_TIMEOUT", "2"))
    url = f"http://{host}:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            status = getattr(resp, "status", None) or resp.getcode()
            if 200 <= int(status) < 300:
                return 0
            print(f"healthcheck: {url} -> HTTP {status}", file=sys.stderr)
            return 1
    except urllib.error.HTTPError as e:
        print(f"healthcheck: {url} -> HTTP {e.code}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        print(f"healthcheck: {url} -> {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
