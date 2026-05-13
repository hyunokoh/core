"""Custody signer node.

Holds ONE Shamir share of the custodial secp256k1 private key. In production
each node would run on a separate physical machine in a separate trust zone
(separate cloud region or, ideally, separate company). For the demo we run
five of these on this same machine, ports 5520..5524, gated by Bearer tokens
that the coordinator possesses.

The node exposes:

  GET  /health
        -> {"ok": true, "share_index": int, "node_id": "n1"}
        Public health check, no auth required (just liveness).

  POST /sign-partial   (Bearer-token-protected)
        Body: {"tx_hash_hex": "0x...", "request_id": "uuid"}
        Returns: {"share_index": int, "share_payload_hex": "<hex>"}

        IMPORTANT: this is NOT real partial ECDSA. The "partial" exposed here
        is just the Shamir share itself. Real partial signing in FROST/GG18
        would emit a partial signature scalar instead, and the full key would
        never need to be reassembled. For this demo the coordinator combines
        shares to reconstruct the key, signs once, then wipes the key. This
        is what we explicitly call out as the threshold-CUSTODY shape (real
        threshold ECDSA is a separate harder primitive).

        Replay protection: if the same `request_id` arrives twice within
        REPLAY_WINDOW_S seconds, the second request is rejected with 409.

CLI:
    python3 -m tools.custody.signer_node \
        --port 5520 \
        --node-id n0 \
        --share-path /path/to/shard_0.bin \
        --coordinator-token <secret>

The share file format is:
    1 byte:    share_index (1..255)
    32 bytes:  share payload (big-endian)

We deliberately accept only the in-process bearer token for sign-partial; in
production this MUST be mTLS with mutually-signed client certs.
"""

from __future__ import annotations

import argparse
import http.server
import json
import logging
import os
import socketserver
import sys
import threading
import time

log = logging.getLogger("zkcex.custody.signer")

REPLAY_WINDOW_S = 60.0


def _read_share(path: str) -> tuple[int, bytes]:
    with open(path, "rb") as f:
        b = f.read()
    if len(b) != 33:
        raise ValueError(f"share file {path}: expected 33 bytes, got {len(b)}")
    idx = b[0]
    if idx == 0:
        raise ValueError(f"share file {path}: index byte must be 1..255")
    payload = bytes(b[1:])
    return idx, payload


class _NodeState:
    def __init__(
        self, node_id: str, share_index: int, share_payload: bytes, coordinator_token: str
    ) -> None:
        self.node_id = node_id
        self.share_index = share_index
        # Hold the share bytes in memory only; never log it.
        self._share_payload = share_payload
        self.coordinator_token = coordinator_token
        self._lock = threading.Lock()
        # request_id -> timestamp (for replay window)
        self._seen: dict[str, float] = {}

    def share_payload(self) -> bytes:
        # Defensive copy to avoid handing out the underlying bytes.
        return bytes(self._share_payload)

    def check_and_register_request(self, request_id: str) -> bool:
        """Returns True if this request_id is fresh (and registers it).
        Returns False if it's a replay within REPLAY_WINDOW_S."""
        now = time.time()
        with self._lock:
            # GC old entries
            stale = [k for k, ts in self._seen.items() if (now - ts) > REPLAY_WINDOW_S]
            for k in stale:
                del self._seen[k]
            if request_id in self._seen:
                return False
            self._seen[request_id] = now
            return True


def _json(handler, code: int, obj) -> None:
    body = json.dumps(obj).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _read_body(handler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode())
    except Exception:
        return {}


def _bearer_ok(handler, expected: str) -> bool:
    auth = handler.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return False
    return auth[len("Bearer ") :].strip() == expected


def make_handler(state: _NodeState):
    class Handler(http.server.BaseHTTPRequestHandler):
        # Override default logging — too chatty.
        def log_message(self, fmt, *args):
            log.info("%s - %s", self.client_address[0], fmt % args)

        def do_GET(self):
            if self.path == "/health" or self.path.startswith("/health?"):
                return _json(
                    self,
                    200,
                    {
                        "ok": True,
                        "share_index": state.share_index,
                        "node_id": state.node_id,
                    },
                )
            return _json(self, 404, {"error": "not_found"})

        def do_POST(self):
            if self.path != "/sign-partial":
                return _json(self, 404, {"error": "not_found"})
            if not _bearer_ok(self, state.coordinator_token):
                return _json(self, 401, {"error": "unauthorized"})
            body = _read_body(self)
            req_id = (body.get("request_id") or "").strip()
            tx_hash = (body.get("tx_hash_hex") or "").strip()
            if not req_id or not tx_hash:
                return _json(
                    self,
                    400,
                    {
                        "error": "bad_request",
                        "expected": "{request_id, tx_hash_hex}",
                    },
                )
            if not state.check_and_register_request(req_id):
                return _json(
                    self,
                    409,
                    {
                        "error": "duplicate_request_id",
                        "message": f"replay within {REPLAY_WINDOW_S}s",
                    },
                )
            # NOTE: the tx_hash is logged for audit but the share itself is
            # never logged. The coordinator is responsible for keeping the
            # reconstructed key out of logs.
            log.info("sign-partial req_id=%s tx_hash=%s", req_id, tx_hash[:18] + "...")
            payload = state.share_payload()
            return _json(
                self,
                200,
                {
                    "share_index": state.share_index,
                    "share_payload_hex": payload.hex(),
                    "node_id": state.node_id,
                },
            )

    return Handler


class _ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    ap = argparse.ArgumentParser(description="zkCEX custody signer node")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--node-id", required=True)
    ap.add_argument("--share-path", required=True)
    ap.add_argument(
        "--coordinator-token",
        default=None,
        help="bearer token; if omitted, read from CUSTODY_NODE_TOKEN env",
    )
    ap.add_argument("--bind", default="127.0.0.1")
    args = ap.parse_args()

    token = args.coordinator_token or os.environ.get("CUSTODY_NODE_TOKEN", "")
    if not token:
        print(
            "ERROR: coordinator token must be provided via --coordinator-token "
            "or CUSTODY_NODE_TOKEN env",
            file=sys.stderr,
        )
        sys.exit(2)

    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [{args.node_id}] %(levelname)s %(message)s",
    )
    idx, payload = _read_share(args.share_path)
    state = _NodeState(args.node_id, idx, payload, token)
    handler = make_handler(state)
    with _ThreadingServer((args.bind, args.port), handler) as srv:
        log.info(
            "signer node %s listening on %s:%d (share_index=%d)",
            args.node_id,
            args.bind,
            args.port,
            idx,
        )
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
