#!/usr/bin/env python3
"""Web Push subscription service for the zkCEX demo.

Listens on :5580 by default and is reverse-proxied by ``serve_homepage.py``
under ``/push/``. Stores per-user PushSubscription JSON in
``tools/.local/push.db`` (SQLite). Generates a VAPID key pair on first run.

Routes
------
GET  /push/vapid-public-key                        public
POST /push/subscribe         Bearer required       store one PushSubscription
POST /push/unsubscribe       Bearer required       drop by id / endpoint / this_device
GET  /push/subscriptions     Bearer required       list this user's subs
POST /push/test              Bearer required       enqueue a notification to *this user*
POST /push/send              loopback only         enqueue a notification to {opex_user}
GET  /push/pending           Bearer required       drain pending notifications

Web Push delivery
-----------------
Real Web Push (RFC 8030/8291/8292) requires ECDH P-256 + AES-128-GCM
encryption and ECDSA P-256 JWT signing. Implementing those from the standard
library alone is on the order of several hundred lines of careful crypto.
For this iteration the server records each notification it would send in a
``pending_notifications`` table; the browser drains them via
``GET /push/pending`` (polled by the service worker / a foreground page) and
mints the actual ``self.registration.showNotification(...)`` call locally.

That keeps the demo end-to-end (you really get a notification), preserves the
subscribe/unsubscribe schema for the day a real VAPID + encrypt path lands,
and avoids shipping crypto we can't fully audit. See the TODO at the bottom
of this file for the production-track plan.
"""

from __future__ import annotations

import base64
import http.client
import http.server
import json
import os
import re
import secrets
import socketserver
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def _validated_http_url(raw_url: str, *, name: str = "url") -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return raw_url


def _validated_http_base_url(name: str, raw_url: str) -> str:
    return _validated_http_url(raw_url, name=name).rstrip("/")


def _http_request(url: str, **kwargs) -> urllib.request.Request:
    return urllib.request.Request(_validated_http_url(url), **kwargs)  # noqa: S310


def _http_urlopen(target, **kwargs):
    if isinstance(target, str):
        target = _validated_http_url(target)
    return urllib.request.urlopen(target, **kwargs)  # noqa: S310


# --------------------------------------------------------------------------
# Config / paths
# --------------------------------------------------------------------------

DEFAULT_PORT = 5580
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PUSH_DB", os.path.join(HERE, ".local", "push.db"))
VAPID_PATH = os.environ.get("PUSH_VAPID", os.path.join(HERE, ".local", "vapid.json"))
AUTH_BASE = _validated_http_base_url(
    "AUTH_BASE", os.environ.get("AUTH_BASE", "http://127.0.0.1:5501")
)
AUTH_TTL_SECONDS = 30

LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------


def log(*args: object) -> None:
    sys.stderr.write("[push] " + " ".join(str(a) for a in args) + "\n")


# --------------------------------------------------------------------------
# DB helpers
# --------------------------------------------------------------------------

_db_lock = threading.RLock()


def db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db() -> None:
    with _db_lock, db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS push_subscriptions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          opex_user TEXT NOT NULL,
          endpoint TEXT NOT NULL,
          p256dh TEXT NOT NULL,
          auth TEXT NOT NULL,
          ua TEXT,
          prefs_json TEXT,
          created_at INTEGER NOT NULL,
          last_seen_at INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_push_endpoint
          ON push_subscriptions(opex_user, endpoint);
        CREATE INDEX IF NOT EXISTS ix_push_user
          ON push_subscriptions(opex_user);

        CREATE TABLE IF NOT EXISTS pending_notifications (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          opex_user TEXT NOT NULL,
          subscription_id INTEGER,        -- nullable: scope-to-user delivery
          payload_json TEXT NOT NULL,     -- {title, body, tag, data, ...}
          created_at INTEGER NOT NULL,
          delivered_at INTEGER,
          delivery_attempts INTEGER NOT NULL DEFAULT 0,
          delivery_error TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_pending_user
          ON pending_notifications(opex_user, delivered_at);
        """)


# --------------------------------------------------------------------------
# VAPID key (P-256). We don't actually use it to *encrypt* yet — see the
# module docstring — but the manifest, /push/vapid-public-key endpoint, and
# subscription handshake all require something stable. Browsers only verify
# the public key against what they were subscribed with, so a fresh key on
# first boot is fine.
#
# We generate a P-256 keypair by deriving a random scalar and synthesising
# the (x,y) coordinates using stdlib int math against the secp256r1 curve.
# --------------------------------------------------------------------------

# secp256r1 (a.k.a. P-256) parameters from FIPS 186-4 / SEC 2.
_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFC
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def _inv_mod(a: int, m: int) -> int:
    return pow(a % m, -1, m)


def _ec_double(p: tuple[int, int] | None) -> tuple[int, int] | None:
    if p is None:
        return None
    x, y = p
    if y == 0:
        return None
    s = ((3 * x * x + _A) * _inv_mod(2 * y, _P)) % _P
    xr = (s * s - 2 * x) % _P
    yr = (s * (x - xr) - y) % _P
    return (xr, yr)


def _ec_add(p: tuple[int, int] | None, q: tuple[int, int] | None) -> tuple[int, int] | None:
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0]:
        if (p[1] + q[1]) % _P == 0:
            return None
        return _ec_double(p)
    s = ((q[1] - p[1]) * _inv_mod((q[0] - p[0]) % _P, _P)) % _P
    xr = (s * s - p[0] - q[0]) % _P
    yr = (s * (p[0] - xr) - p[1]) % _P
    return (xr, yr)


def _ec_mul(k: int, p: tuple[int, int]) -> tuple[int, int]:
    r: tuple[int, int] | None = None
    addend: tuple[int, int] | None = p
    while k > 0:
        if k & 1:
            r = _ec_add(r, addend)
        addend = _ec_double(addend)
        k >>= 1
    if r is None:
        raise ValueError("invalid scalar multiplication result")
    return r


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    s = s + "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s.encode("ascii"))


def _load_or_gen_vapid() -> dict[str, str]:
    os.makedirs(os.path.dirname(VAPID_PATH), exist_ok=True)
    if os.path.exists(VAPID_PATH):
        with open(VAPID_PATH) as f:
            v = json.load(f)
        if "publicKey" in v and "privateKey" in v:
            return v
    # Generate a fresh scalar d in [1, n-1] and compute Q = d*G.
    d = (secrets.randbits(256) % (_N - 1)) + 1
    Q = _ec_mul(d, (_GX, _GY))
    # Uncompressed SEC1: 0x04 || X(32) || Y(32) — that's the format browsers
    # want for applicationServerKey.
    pub = b"\x04" + Q[0].to_bytes(32, "big") + Q[1].to_bytes(32, "big")
    priv = d.to_bytes(32, "big")
    v = {
        "publicKey": _b64url(pub),
        "privateKey": _b64url(priv),
        "subject": os.environ.get("PUSH_VAPID_SUBJECT", "mailto:ops@zkcex.local"),
        "createdAt": int(time.time()),
    }
    with open(VAPID_PATH, "w") as f:
        json.dump(v, f, indent=2)
    log("generated VAPID key, pub=", v["publicKey"][:20] + "...")
    return v


VAPID = _load_or_gen_vapid()


# --------------------------------------------------------------------------
# Auth: validate Bearer against the auth_server's /auth/me. Cache the result
# briefly so we don't hammer it on every poll.
# --------------------------------------------------------------------------

_auth_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_auth_cache_lock = threading.Lock()


def resolve_user(token: str | None) -> dict | None:
    if not token:
        return None
    now = time.time()
    with _auth_cache_lock:
        hit = _auth_cache.get(token)
        if hit and now - hit[0] < AUTH_TTL_SECONDS:
            return hit[1]
    try:
        req = _http_request(
            f"{AUTH_BASE}/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        with _http_urlopen(req, timeout=4) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log("auth/me", e.code, "token=", token[:8])
        return None
    except Exception as e:
        log("auth/me transport error:", e)
        return None
    user = obj.get("user") or {}
    if not user.get("opex_user"):
        return None
    with _auth_cache_lock:
        _auth_cache[token] = (now, user)
    return user


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------


def _json(handler: http.server.BaseHTTPRequestHandler, status: int, body: Any) -> None:
    payload = json.dumps(body, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _read_body(handler: http.server.BaseHTTPRequestHandler) -> dict:
    n = int(handler.headers.get("Content-Length") or 0)
    if n <= 0:
        return {}
    raw = handler.rfile.read(n)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def _bearer(handler: http.server.BaseHTTPRequestHandler) -> str | None:
    h = handler.headers.get("Authorization") or ""
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return None


def _client_is_loopback(handler: http.server.BaseHTTPRequestHandler) -> bool:
    ip = handler.client_address[0] if handler.client_address else ""
    return ip in LOOPBACK_HOSTS or ip.startswith("127.")


def _ua_brief(ua: str) -> str:
    if not ua:
        return "Unknown device"
    # Short, human label. We avoid pulling in a UA parser dep.
    if "iPhone" in ua:
        return "iPhone"
    if "iPad" in ua:
        return "iPad"
    if "Android" in ua:
        m = re.search(r"Android [\d.]+", ua)
        return m.group(0) if m else "Android"
    if "Mac OS X" in ua:
        return "Mac"
    if "Windows" in ua:
        return "Windows"
    if "Linux" in ua:
        return "Linux"
    return ua[:32]


def _endpoint_brief(endpoint: str) -> str:
    try:
        from urllib.parse import urlsplit

        u = urlsplit(endpoint)
        host = u.hostname or "?"
        tail = (u.path or "/").rstrip("/").split("/")[-1]
        return f"{host} · {tail[:10]}"
    except Exception:
        return endpoint[:48]


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "zkcex-push/1.0"

    # noisy default; route through our logger.
    def log_message(self, fmt, *args):
        log(self.client_address[0], fmt % args)

    # CORS preflight (used when the page is opened on a port different from
    # the homepage proxy).
    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/push/vapid-public-key":
            return _json(self, 200, {"publicKey": VAPID["publicKey"]})
        if path == "/push/subscriptions":
            return self._list_subs()
        if path == "/push/pending":
            return self._pending()
        if path == "/push/health":
            return _json(self, 200, {"ok": True, "now": int(time.time())})
        return _json(self, 404, {"error": "not_found"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/push/subscribe":
            return self._subscribe()
        if path == "/push/unsubscribe":
            return self._unsubscribe()
        if path == "/push/test":
            return self._test_self()
        if path == "/push/send":
            return self._send_loopback()
        if path == "/push/mark-delivered":
            return self._mark_delivered()
        return _json(self, 404, {"error": "not_found"})

    # ---- /push/subscribe -------------------------------------------------
    def _subscribe(self):
        token = _bearer(self)
        user = resolve_user(token)
        if not user:
            return _json(self, 401, {"error": "unauthorized"})
        body = _read_body(self)
        endpoint = (body.get("endpoint") or "").strip()
        keys = body.get("keys") or {}
        p256dh = (keys.get("p256dh") or "").strip()
        auth = (keys.get("auth") or "").strip()
        if not endpoint or not p256dh or not auth:
            return _json(
                self,
                400,
                {"error": "missing_fields", "hint": "endpoint + keys.p256dh + keys.auth required"},
            )
        ua = (body.get("ua") or self.headers.get("User-Agent") or "").strip()[:512]
        prefs_json = json.dumps(body.get("prefs") or {}, ensure_ascii=False)[:4096]
        now = int(time.time())
        with _db_lock, db() as c:
            row = c.execute(
                "SELECT id FROM push_subscriptions WHERE opex_user=? AND endpoint=?",
                (user["opex_user"], endpoint),
            ).fetchone()
            if row:
                c.execute(
                    "UPDATE push_subscriptions SET p256dh=?, auth=?, ua=?, prefs_json=?,"
                    " last_seen_at=? WHERE id=?",
                    (p256dh, auth, ua, prefs_json, now, row["id"]),
                )
                sub_id = row["id"]
            else:
                cur = c.execute(
                    "INSERT INTO push_subscriptions(opex_user, endpoint, p256dh, auth, ua, prefs_json,"
                    " created_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
                    (user["opex_user"], endpoint, p256dh, auth, ua, prefs_json, now, now),
                )
                sub_id = cur.lastrowid
        return _json(self, 200, {"id": sub_id, "opex_user": user["opex_user"]})

    # ---- /push/unsubscribe ----------------------------------------------
    def _unsubscribe(self):
        token = _bearer(self)
        user = resolve_user(token)
        if not user:
            return _json(self, 401, {"error": "unauthorized"})
        body = _read_body(self)
        sid = body.get("id")
        endpoint = body.get("endpoint")
        this_device = bool(body.get("this_device"))
        with _db_lock, db() as c:
            if sid:
                c.execute(
                    "DELETE FROM push_subscriptions WHERE id=? AND opex_user=?",
                    (sid, user["opex_user"]),
                )
            elif endpoint:
                c.execute(
                    "DELETE FROM push_subscriptions WHERE endpoint=? AND opex_user=?",
                    (endpoint, user["opex_user"]),
                )
            elif this_device:
                # We don't know the endpoint here (the browser side will drop
                # its own subscription). Delete the most recent subscription
                # for the user — close enough for the demo.
                c.execute(
                    "DELETE FROM push_subscriptions"
                    " WHERE id IN (SELECT id FROM push_subscriptions"
                    "              WHERE opex_user=? ORDER BY last_seen_at DESC LIMIT 1)",
                    (user["opex_user"],),
                )
            else:
                return _json(self, 400, {"error": "specify_id_or_endpoint"})
        return _json(self, 200, {"ok": True})

    # ---- /push/subscriptions --------------------------------------------
    def _list_subs(self):
        token = _bearer(self)
        user = resolve_user(token)
        if not user:
            return _json(self, 401, {"error": "unauthorized"})
        with _db_lock, db() as c:
            rows = c.execute(
                "SELECT id, endpoint, ua, created_at, last_seen_at FROM push_subscriptions"
                " WHERE opex_user=? ORDER BY last_seen_at DESC",
                (user["opex_user"],),
            ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "id": r["id"],
                    "endpoint_brief": _endpoint_brief(r["endpoint"]),
                    "ua_brief": _ua_brief(r["ua"] or ""),
                    "created_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created_at"])),
                    "last_seen_at": time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(r["last_seen_at"])
                    ),
                }
            )
        return _json(self, 200, {"subscriptions": out})

    # ---- /push/test (self) ----------------------------------------------
    def _test_self(self):
        token = _bearer(self)
        user = resolve_user(token)
        if not user:
            return _json(self, 401, {"error": "unauthorized"})
        body = _read_body(self)
        payload = {
            "title": body.get("title") or "테스트 알림 / Test notification",
            "body": body.get("body") or "푸시가 정상적으로 동작합니다. / Push is working.",
            "tag": body.get("tag") or "test",
            "data": body.get("data") or {"url": "/app/notifications.html"},
        }
        n = enqueue(user["opex_user"], payload)
        return _json(self, 200, {"queued": n})

    # ---- /push/send (loopback) ------------------------------------------
    def _send_loopback(self):
        if not _client_is_loopback(self):
            return _json(self, 403, {"error": "loopback_only"})
        body = _read_body(self)
        opex_user = body.get("opex_user") or ""
        payload = body.get("payload") or {}
        if not opex_user or not isinstance(payload, dict):
            return _json(self, 400, {"error": "missing_opex_user_or_payload"})
        n = enqueue(opex_user, payload)
        return _json(self, 200, {"queued": n})

    # ---- /push/pending --------------------------------------------------
    def _pending(self):
        """Drain undelivered notifications for the calling user.

        Browsers/service workers can poll this while open as a fallback for
        Web Push encryption — see module docstring. Returns at most N rows
        and marks them delivered atomically.
        """
        token = _bearer(self)
        user = resolve_user(token)
        if not user:
            return _json(self, 401, {"error": "unauthorized"})
        # ?limit=
        from urllib.parse import parse_qs, urlsplit

        qs = parse_qs(urlsplit(self.path).query)
        try:
            limit = max(1, min(50, int(qs.get("limit", ["20"])[0])))
        except ValueError:
            limit = 20
        out = []
        with _db_lock, db() as c:
            rows = c.execute(
                "SELECT id, payload_json, created_at FROM pending_notifications"
                " WHERE opex_user=? AND delivered_at IS NULL"
                " ORDER BY id ASC LIMIT ?",
                (user["opex_user"], limit),
            ).fetchall()
            now = int(time.time())
            for r in rows:
                try:
                    payload = json.loads(r["payload_json"])
                except Exception:
                    payload = {}
                out.append(
                    {
                        "id": r["id"],
                        "payload": payload,
                        "created_at": r["created_at"],
                    }
                )
                c.execute(
                    "UPDATE pending_notifications SET delivered_at=?,"
                    " delivery_attempts=delivery_attempts+1 WHERE id=?",
                    (now, r["id"]),
                )
        return _json(self, 200, {"notifications": out})

    # ---- /push/mark-delivered (idempotent) ------------------------------
    def _mark_delivered(self):
        token = _bearer(self)
        user = resolve_user(token)
        if not user:
            return _json(self, 401, {"error": "unauthorized"})
        body = _read_body(self)
        ids = body.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return _json(self, 400, {"error": "missing_ids"})
        with _db_lock, db() as c:
            for i in ids:
                try:
                    c.execute(
                        "UPDATE pending_notifications SET delivered_at=COALESCE(delivered_at,?)"
                        " WHERE id=? AND opex_user=?",
                        (int(time.time()), int(i), user["opex_user"]),
                    )
                except (TypeError, ValueError):
                    pass
        return _json(self, 200, {"ok": True})


# --------------------------------------------------------------------------
# Notification enqueue
# --------------------------------------------------------------------------


def enqueue(opex_user: str, payload: dict) -> int:
    """Insert one row per subscription the user has, plus a user-scope row.

    The user-scope row (subscription_id=NULL) is what the foreground page or
    long-poll drains; per-subscription rows exist so that, once real Web
    Push encryption lands, we can attempt delivery per endpoint and surface
    per-endpoint errors.
    """
    now = int(time.time())
    payload_json = json.dumps(payload, ensure_ascii=False)
    with _db_lock, db() as c:
        c.execute(
            "INSERT INTO pending_notifications(opex_user, subscription_id, payload_json, created_at)"
            " VALUES (?, NULL, ?, ?)",
            (opex_user, payload_json, now),
        )
        rows = c.execute(
            "SELECT id FROM push_subscriptions WHERE opex_user=?",
            (opex_user,),
        ).fetchall()
        for r in rows:
            c.execute(
                "INSERT INTO pending_notifications(opex_user, subscription_id, payload_json, created_at)"
                " VALUES (?, ?, ?, ?)",
                (opex_user, r["id"], payload_json, now),
            )
    log(f"enqueued opex_user={opex_user} subs={len(rows)} title={payload.get('title')!r}")
    return 1 + len(rows)


# --------------------------------------------------------------------------
# Server bootstrap
# --------------------------------------------------------------------------


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    init_db()
    with ThreadingServer(("", port), Handler) as srv:
        log(f"push server listening on :{port}")
        log(f"  db    -> {DB_PATH}")
        log(f"  vapid -> {VAPID_PATH} (pub {VAPID['publicKey'][:24]}...)")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


# ============================================================================
# TODO: real Web Push (RFC 8030/8291/8292) delivery path.
#
# Steps remaining:
#   1. Build a VAPID JWT (ES256) per push: header={alg:ES256,typ:JWT},
#      claims={aud:<origin of endpoint>, exp:now+12h, sub:VAPID["subject"]}.
#      Sign with the private scalar above — needs ECDSA P-256 (RFC 6979 for
#      deterministic k is the friendliest path from stdlib).
#   2. Encrypt the payload per RFC 8291 (aes128gcm content encoding):
#      - generate an ephemeral P-256 keypair (we already have _ec_mul)
#      - ECDH-shared secret = scalarmult(priv_ephemeral, sub.p256dh)
#      - HKDF-Extract / -Expand with sub.auth as the secret, fixed info
#        strings as in the RFC, yielding CEK (16 B) and nonce (12 B)
#      - AES-128-GCM encrypt {salt(16) || rs(4) || idlen(1) || keyid(...) || data}
#        Python stdlib has no AES-GCM; we'd need to port one ourselves, ~150
#        lines (Rijndael + GHASH). At that point shipping `cryptography`
#        would be cheaper, but the constraint here is "no pip deps."
#   3. POST encrypted body to sub.endpoint with headers:
#         Authorization: vapid t=<jwt>,k=<base64url(pubkey)>
#         Content-Encoding: aes128gcm
#         TTL: 600
#      A 201/202 means delivered; a 410/404 means the endpoint is gone and
#      we should DELETE FROM push_subscriptions WHERE id=...
#
# When this lands, /push/send should attempt the live POST for each
# subscription_id and only fall back to the pending_notifications path on
# transport error.
# ============================================================================


if __name__ == "__main__":
    main()
