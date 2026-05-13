#!/usr/bin/env python3
"""Snapshot feed for the upstream Rust pol-snapshot-server.

The Rust server (binary in ~/Documents/Projects/zkPoL-snapshot/server/target/release/)
runs in pull mode: every `interval-seconds` it GETs the configured `--exchange-url`
and expects an ``ExchangeSnapshot`` JSON body of:

    {
      "epoch": <u64>,
      "timestamp_ms": <u64>,
      "epoch_nonce": "<32-byte hex>",
      "leaves": [{"user_id": "<str>", "balance": <u64>}, ...]
    }

This service exposes that endpoint at ``GET /snapshot`` on port 5505. It reads the
list of customer ``opex_user`` values from auth.db (read-only) and the per-asset
balances from the wallet API, collapses them into a single USDT-equivalent number
per user, and emits one leaf per user. The epoch nonce is derived deterministically
from the epoch number + a server secret stored in tools/.local/pol_snapshot_feed_secret
so successive runs reproduce the same nonces and the snapshot history is replayable.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import secrets
import socketserver
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL = os.path.join(HERE, ".local")
os.makedirs(LOCAL, exist_ok=True)
AUTH_DB = os.path.join(LOCAL, "auth.db")
SECRET_PATH = os.path.join(LOCAL, "pol_snapshot_feed_secret")


def _validated_http_url(raw_url: str, *, name: str = "url") -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return raw_url


def _validated_http_base_url(name: str, raw_url: str) -> str:
    return _validated_http_url(raw_url, name=name).rstrip("/")


def _http_urlopen(target, **kwargs):
    if isinstance(target, str):
        target = _validated_http_url(target)
    return urllib.request.urlopen(target, **kwargs)  # noqa: S310


WALLET_BASE = _validated_http_base_url(
    "ZKCEX_WALLET_BASE", os.environ.get("ZKCEX_WALLET_BASE", "http://127.0.0.1:8091")
)
TICKER_BASE = _validated_http_base_url(
    "ZKCEX_TICKER_BASE", os.environ.get("ZKCEX_TICKER_BASE", "http://127.0.0.1:8094")
)
EPOCH_SECONDS = int(os.environ.get("POL_SNAPSHOT_EPOCH_SECONDS", "60"))


def server_secret() -> bytes:
    if os.path.exists(SECRET_PATH):
        return open(SECRET_PATH, "rb").read()
    s = secrets.token_bytes(32)
    with open(SECRET_PATH, "wb") as fh:
        fh.write(s)
    return s


def auth_users() -> list[str]:
    if not os.path.exists(AUTH_DB):
        return []
    uri = f"file:{AUTH_DB}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as db:
            db.row_factory = sqlite3.Row
            return [r["opex_user"] for r in db.execute("SELECT opex_user FROM users ORDER BY id")]
    except sqlite3.OperationalError:
        return []


_PRICE_CACHE: dict[str, tuple[float, float]] = {}  # asset -> (price, fetched_at)
PRICE_TTL = 30.0


def asset_price_usdt(asset: str) -> float:
    if asset.upper() == "USDT":
        return 1.0
    now = time.time()
    cached = _PRICE_CACHE.get(asset)
    if cached and now - cached[1] < PRICE_TTL:
        return cached[0]
    sym = f"{asset.upper()}USDT"
    try:
        with _http_urlopen(f"{TICKER_BASE}/v3/ticker/24h?symbol={sym}", timeout=4) as r:
            body = json.loads(r.read())
        if isinstance(body, list):
            body = body[0] if body else {}
        price = float(body.get("lastPrice") or 0.0) or float(body.get("openPrice") or 0.0)
        if price <= 0:
            price = _PRICE_CACHE.get(asset, (1.0, 0.0))[0]
        _PRICE_CACHE[asset] = (price, now)
        return price
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError):
        return _PRICE_CACHE.get(asset, (0.0, 0.0))[0]


def user_total_usdt_scaled(opex_user: str) -> int | None:
    """Sum every asset balance in USDT-equivalent, scaled to 8 decimals (u64)."""
    try:
        owner = urllib.parse.quote(opex_user, safe="")
        with _http_urlopen(f"{WALLET_BASE}/v1/owner/{owner}/wallets", timeout=4) as r:
            wallets = json.loads(r.read())
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError):
        return None
    total = 0.0
    for w in wallets or []:
        bal = float(w.get("balance") or 0)
        if bal <= 0:
            continue
        total += bal * asset_price_usdt(w.get("asset", "USDT"))
    return int(total * 1e8)


def epoch_for_now() -> int:
    return int(time.time() // EPOCH_SECONDS)


def epoch_nonce_hex(epoch: int) -> str:
    """Deterministic per-epoch nonce: HMAC-SHA256(secret, "zkcex|epoch|<id>")."""
    import hmac

    msg = f"zkcex|epoch|{epoch}".encode()
    return hmac.new(server_secret(), msg, hashlib.sha256).hexdigest()


def build_snapshot() -> dict:
    epoch = epoch_for_now()
    leaves = []
    for u in auth_users():
        scaled = user_total_usdt_scaled(u)
        if scaled is None:
            continue
        leaves.append({"user_id": u, "balance": int(scaled)})
    return {
        "epoch": epoch,
        "timestamp_ms": int(time.time() * 1000),
        "epoch_nonce": epoch_nonce_hex(epoch),
        "leaves": leaves,
    }


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self._json(200, {"ok": True, "epoch_seconds": EPOCH_SECONDS})
        if path == "/snapshot":
            try:
                snap = build_snapshot()
                return self._json(200, snap)
            except Exception as exc:
                return self._json(503, {"error": "feed_failed", "message": str(exc)})
        self._json(404, {"error": "not_found"})

    def _json(self, code: int, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")


class ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5505
    with ThreadingServer(("127.0.0.1", port), Handler) as srv:
        sys.stderr.write(
            f"pol_snapshot_feed serving on :{port}, "
            f"wallet={WALLET_BASE}, ticker={TICKER_BASE}, epoch={EPOCH_SECONDS}s\n"
        )
        srv.serve_forever()


if __name__ == "__main__":
    main()
