#!/usr/bin/env python3
"""Evil proxy: corrupt a percentage of upstream responses.

Stands up a tiny HTTP proxy on ``--listen-port`` that forwards every request
to ``--upstream`` but with probability ``--corrupt-rate`` flips a random
byte in the response body before returning it.  Use it to verify the
consumer (e.g. chain_server) detects corruption -- malformed JSON should
cause it to reject the response rather than crash or silently use bad data.

    python3 chaos/data_corruption.py \
        --listen-port 5699 --upstream http://localhost:5502 \
        --corrupt-rate 0.10

Then point the consumer at http://localhost:5699 instead of the real
upstream and exercise it normally.

Kill switch:  Ctrl-C, or  KILL_CHAOS=1 pkill -f data_corruption.py
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from chaos import service_registry as reg  # noqa: E402

CORRUPT_RATE: float = 0.0
UPSTREAM: str = ""


def _validated_http_url(raw_url: str, *, name: str = "url") -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return raw_url


def _validated_http_base_url(raw_url: str, *, name: str = "url") -> str:
    return _validated_http_url(raw_url, name=name).rstrip("/")


def _http_request(url: str, **kwargs) -> urllib.request.Request:
    return urllib.request.Request(_validated_http_url(url), **kwargs)  # noqa: S310


def _http_urlopen(target, **kwargs):
    if isinstance(target, str):
        target = _validated_http_url(target)
    return urllib.request.urlopen(target, **kwargs)  # noqa: S310


def corrupt(body: bytes) -> bytes:
    if not body:
        return body
    i = secrets.randbelow(len(body))
    b = bytearray(body)
    b[i] ^= 0xFF
    return bytes(b)


class EvilHandler(BaseHTTPRequestHandler):
    def _proxy(self, method: str) -> None:
        target = UPSTREAM.rstrip("/") + self.path
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else None

        req_headers = {
            k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length")
        }
        req = _http_request(target, data=body, method=method, headers=req_headers)
        try:
            with _http_urlopen(req, timeout=10) as r:
                status = r.status
                resp_body = r.read()
                resp_hdrs = list(r.headers.items())
        except urllib.error.HTTPError as e:
            status = e.code
            resp_body = e.read()
            resp_hdrs = list(e.headers.items()) if e.headers else []
        except Exception as e:
            self.send_error(502, f"upstream error: {e}")
            return

        threshold = int(CORRUPT_RATE * 1_000_000)
        if secrets.randbelow(1_000_000) < threshold:
            resp_body = corrupt(resp_body)
            self.log_message(
                "CORRUPTED %s %s (rate=%.2f, %d bytes)",
                method,
                self.path,
                CORRUPT_RATE,
                len(resp_body),
            )

        self.send_response(status)
        for k, v in resp_hdrs:
            if k.lower() in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        try:
            self.wfile.write(resp_body)
        except BrokenPipeError:
            pass

    def do_GET(self):
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")

    def do_PUT(self):
        self._proxy("PUT")

    def do_DELETE(self):
        self._proxy("DELETE")

    def do_PATCH(self):
        self._proxy("PATCH")


def main() -> int:
    global CORRUPT_RATE, UPSTREAM
    ap = argparse.ArgumentParser(description="Evil corrupting proxy.")
    ap.add_argument("--listen-port", type=int, default=5699)
    ap.add_argument(
        "--upstream", required=True, help="Real backend URL, e.g. http://localhost:5502"
    )
    ap.add_argument(
        "--corrupt-rate", type=float, default=0.05, help="Probability of flipping a byte (0.0-1.0)."
    )
    args = ap.parse_args()

    if os.environ.get("KILL_CHAOS"):
        print("KILL_CHAOS=1, refusing to run", file=sys.stderr)
        return 1

    if not reg.is_safe_port(args.listen_port):
        print(
            f"refuse: --listen-port {args.listen_port} outside safe range "
            f"{reg.SAFE_PORT_MIN}-{reg.SAFE_PORT_MAX}",
            file=sys.stderr,
        )
        return 2

    CORRUPT_RATE = max(0.0, min(1.0, args.corrupt_rate))
    UPSTREAM = _validated_http_base_url(args.upstream, name="--upstream")

    srv = ThreadingHTTPServer(("127.0.0.1", args.listen_port), EvilHandler)
    print(
        f"evil proxy listening on 127.0.0.1:{args.listen_port}, "
        f"upstream={UPSTREAM}, corrupt_rate={CORRUPT_RATE}"
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
