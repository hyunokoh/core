#!/usr/bin/env python3
"""Binance-compatible API key server for zkCEX.

Stdlib-only HTTP service (default port 5550) that owns the API-key lifecycle
for external clients (ccxt, python-binance, hummingbot, AI trading agents).

Persistence: SQLite at ``tools/.local/api_keys.db`` — kept distinct from the
auth and chain SQLite files so concurrent agents don't collide.

Authentication model:
- Bearer-required management endpoints (``/api-keys/create``, ``/list``,
  ``PATCH``, ``DELETE``, ``/usage``) verify the session by hitting the auth
  server's ``/auth/me`` over HTTP — we do NOT crack open the auth DB.
- ``/api-keys/verify`` is a server-internal endpoint used by the front-door
  proxy (``serve_homepage.py``) to validate Binance-style HMAC-SHA256 signed
  requests. It rejects any caller whose IP isn't in 127.0.0.0/8.

HMAC scheme (Binance-compatible):
- Client builds ``query_string`` containing ``timestamp=<ms>&recvWindow=<ms>``
  (plus any business params).
- ``signature = HMAC-SHA256(secret, query_string).hex_digest()``
- Sent as ``X-MBX-APIKEY: <key_id>`` header + ``signature=<sig>`` appended to
  the query string (or in form-encoded body for POST).
- Server checks ``|now_ms - timestamp| <= recvWindow`` (max 60_000 ms).

Error codes returned to the client follow Binance's table:
  -1003 too many requests / quota
  -1021 outside recvWindow
  -1022 bad signature
  -2010 unauthorized (scope, IP, withdraw confirm-phrase, etc.)
  -2014 invalid key (unknown / malformed / revoked / expired)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.server
import json
import os
import secrets
import socketserver
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any

# --- Paths -----------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join(HERE, ".local")
os.makedirs(LOCAL_DIR, exist_ok=True)
DB_PATH = os.path.join(LOCAL_DIR, "api_keys.db")


# --- Config ----------------------------------------------------------------
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


AUTH_BASE = _validated_http_base_url(
    "AUTH_BASE", os.environ.get("AUTH_BASE", "http://127.0.0.1:5501")
)
PBKDF2_ITERS = 600_000
PBKDF2_DKLEN = 64
DEFAULT_EXPIRES_DAYS = 90
RECV_WINDOW_MAX_MS = 60_000
RECV_WINDOW_DEFAULT_MS = 5_000
WITHDRAW_CONFIRM_PHRASE = "I want to enable withdraw"

ALLOWED_SCOPES = {"read", "trade", "withdraw"}
DEFAULT_SCOPES = ("read",)

# Binance-compatible error codes the verify path returns up to the proxy.
ERR_TOO_MANY = -1003
ERR_TIMESTAMP = -1021
ERR_SIGNATURE = -1022
ERR_UNAUTHORIZED = -2010
ERR_INVALID_KEY = -2014


# --- DB helpers ------------------------------------------------------------
_db_lock = threading.RLock()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=4000")
    return conn


def init_db() -> None:
    with _db_lock, db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              key_id TEXT UNIQUE NOT NULL,
              secret_hash BLOB NOT NULL,
              secret_salt BLOB NOT NULL,
              user_id INTEGER NOT NULL,
              opex_user TEXT NOT NULL,
              label TEXT NOT NULL,
              scopes TEXT NOT NULL,
              ip_allowlist TEXT,
              daily_quote_cap_usdt TEXT,
              hourly_request_cap INTEGER,
              created_at INTEGER NOT NULL,
              expires_at INTEGER,
              revoked_at INTEGER,
              last_used_at INTEGER,
              metadata_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);

            CREATE TABLE IF NOT EXISTS api_key_usage (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              key_id TEXT NOT NULL,
              endpoint TEXT NOT NULL,
              method TEXT NOT NULL,
              decision TEXT NOT NULL,
              notional_usdt TEXT,
              client_ip TEXT,
              ts INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_usage_key_ts
              ON api_key_usage(key_id, ts);

            CREATE TABLE IF NOT EXISTS api_key_quota (
              key_id TEXT NOT NULL,
              utc_day INTEGER NOT NULL,
              traded_usdt TEXT NOT NULL,
              PRIMARY KEY (key_id, utc_day)
            );
            """
        )


# --- Crypto / id helpers ---------------------------------------------------
def _b64url(n_bytes: int) -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(n_bytes)).rstrip(b"=").decode("ascii")


def make_key_id() -> str:
    # 18 raw bytes -> 24 chars urlsafe-base64 (no padding).
    return _b64url(18)


def make_secret() -> str:
    # 48 raw bytes -> 64 chars urlsafe-base64 (no padding).
    return _b64url(48)


def hash_secret(secret: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", secret.encode("utf-8"), salt, PBKDF2_ITERS, PBKDF2_DKLEN)


# --- /auth/me bridge -------------------------------------------------------
def authenticate_bearer(token: str | None) -> dict | None:
    """Resolve a Bearer token to a user dict by calling auth_server."""
    if not token:
        return None
    req = _http_request(
        f"{AUTH_BASE}/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with _http_urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
            user = data.get("user")
            if not user or not isinstance(user, dict):
                return None
            if not user.get("opex_user") or not user.get("id"):
                return None
            return user
    except urllib.error.HTTPError:
        return None
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[api-keys] auth lookup failed: {e!r}\n")
        return None


# --- Validation helpers ----------------------------------------------------
def parse_scopes(raw: Any) -> tuple[str, ...] | None:
    if raw is None:
        return tuple(DEFAULT_SCOPES)
    if isinstance(raw, str):
        items = [s.strip() for s in raw.split(",") if s.strip()]
    elif isinstance(raw, list):
        items = [str(s).strip() for s in raw if str(s).strip()]
    else:
        return None
    if not items:
        return tuple(DEFAULT_SCOPES)
    out: list[str] = []
    for s in items:
        s_low = s.lower()
        if s_low not in ALLOWED_SCOPES:
            return None
        if s_low not in out:
            out.append(s_low)
    if "read" not in out:
        out.insert(0, "read")
    return tuple(out)


def parse_ip_allowlist(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        items = [s.strip() for s in raw.split(",") if s.strip()]
    elif isinstance(raw, list):
        items = [str(s).strip() for s in raw if str(s).strip()]
    else:
        return None
    return ",".join(items) if items else None


def parse_decimal_cap(raw: Any) -> str | None:
    if raw is None or raw == "":
        return None
    try:
        d = Decimal(str(raw))
        if d <= 0:
            return None
        return format(d, "f")
    except (InvalidOperation, ValueError):
        return None


def parse_int_cap(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def withdraw_confirm_ok(raw: Any) -> bool:
    if not isinstance(raw, str):
        return False
    return hmac.compare_digest(raw, WITHDRAW_CONFIRM_PHRASE)


# --- IP / scope helpers ----------------------------------------------------
def _ip_to_int(addr: str) -> int | None:
    try:
        parts = addr.split(".")
        if len(parts) != 4:
            return None
        return sum(int(p) << ((3 - i) * 8) for i, p in enumerate(parts))
    except (ValueError, TypeError):
        return None


def ip_in_cidr(ip: str, cidr: str) -> bool:
    """Tiny IPv4 CIDR match. Returns False on malformed input."""
    if not ip or not cidr:
        return False
    if "/" in cidr:
        net, _, bits_s = cidr.partition("/")
        try:
            bits = int(bits_s)
        except ValueError:
            return False
    else:
        net, bits = cidr, 32
    if not (0 <= bits <= 32):
        return False
    n_ip = _ip_to_int(ip)
    n_net = _ip_to_int(net)
    if n_ip is None or n_net is None:
        return False
    if bits == 0:
        return True
    mask = ((1 << 32) - 1) ^ ((1 << (32 - bits)) - 1)
    return (n_ip & mask) == (n_net & mask)


def ip_in_allowlist(ip: str, allowlist: str | None) -> bool:
    """Empty / None allowlist = allow any."""
    if not allowlist or not allowlist.strip():
        return True
    for cidr in allowlist.split(","):
        cidr = cidr.strip()
        if not cidr:
            continue
        # Plain IP without /n is treated as /32.
        if "/" not in cidr:
            cidr = cidr + "/32"
        if ip_in_cidr(ip, cidr):
            return True
    return False


def is_loopback(ip: str | None) -> bool:
    return bool(ip) and ip_in_cidr(ip, "127.0.0.0/8")


# --- Audit logging ---------------------------------------------------------
def record_usage(
    *,
    key_id: str,
    endpoint: str,
    method: str,
    decision: str,
    client_ip: str | None,
    notional_usdt: str | None = None,
) -> None:
    try:
        with _db_lock, db() as conn:
            conn.execute(
                "INSERT INTO api_key_usage "
                "(key_id, endpoint, method, decision, notional_usdt, client_ip, ts) "
                "VALUES (?,?,?,?,?,?,?)",
                (key_id, endpoint, method, decision, notional_usdt, client_ip, int(time.time())),
            )
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[api-keys] usage log failed: {e!r}\n")


def update_last_used(key_id: str) -> None:
    try:
        with _db_lock, db() as conn:
            conn.execute(
                "UPDATE api_keys SET last_used_at=? WHERE key_id=?",
                (int(time.time()), key_id),
            )
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[api-keys] last-used update failed: {e!r}\n")


# --- Quota / rate-limit checks --------------------------------------------
def _utc_day(now_ts: int | None = None) -> int:
    return int((now_ts or int(time.time())) // 86400)


def hourly_request_count(key_id: str, since_ts: int) -> int:
    with db() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) AS c FROM api_key_usage "
            "WHERE key_id=? AND ts>=? AND decision='allow'",
            (key_id, since_ts),
        )
        return int(cur.fetchone()["c"])


def consume_quote_cap(key_id: str, notional_usdt: str | None, cap: str | None) -> bool:
    """Atomically increment the day's traded-notional counter for ``key_id``.

    Returns True if the increment was applied (under cap), False if the cap
    would be exceeded. ``notional_usdt`` <= 0 (or None) is a no-op success.
    """
    if not notional_usdt:
        return True
    try:
        delta = Decimal(notional_usdt)
    except (InvalidOperation, ValueError):
        return True
    if delta <= 0:
        return True
    day = _utc_day()
    cap_dec = Decimal(cap) if cap else None
    with _db_lock, db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "SELECT traded_usdt FROM api_key_quota " "WHERE key_id=? AND utc_day=?",
                (key_id, day),
            )
            row = cur.fetchone()
            current = Decimal(row["traded_usdt"]) if row else Decimal(0)
            new_total = current + delta
            if cap_dec is not None and new_total > cap_dec:
                conn.execute("ROLLBACK")
                return False
            if row is None:
                conn.execute(
                    "INSERT INTO api_key_quota (key_id, utc_day, traded_usdt) " "VALUES (?,?,?)",
                    (key_id, day, format(new_total, "f")),
                )
            else:
                conn.execute(
                    "UPDATE api_key_quota SET traded_usdt=? " "WHERE key_id=? AND utc_day=?",
                    (format(new_total, "f"), key_id, day),
                )
            conn.execute("COMMIT")
            return True
        except Exception:
            conn.execute("ROLLBACK")
            raise


# --- Verify (HMAC) ---------------------------------------------------------
class APIError(Exception):
    def __init__(self, code: int, msg: str, http_status: int = 401):
        super().__init__(msg)
        self.code = code
        self.msg = msg
        self.http_status = http_status


def verify_signed_request(
    *,
    key_id: str,
    signature: str,
    query_string: str,
    client_ip: str | None,
    endpoint_path: str,
    method: str,
    required_scope: str = "read",
) -> dict:
    """Look up the key, validate the signature, expiry, IP, scope, and
    rolling rate cap. Returns ``{opex_user, key_id, scopes, ...}`` on allow,
    raises ``APIError`` (with a Binance-shaped error code) on deny.

    Every decision is audited via ``record_usage``.
    """
    if not key_id:
        raise APIError(ERR_INVALID_KEY, "API-key format invalid.", 401)

    with db() as conn:
        row = conn.execute("SELECT * FROM api_keys WHERE key_id=?", (key_id,)).fetchone()
    if not row:
        record_usage(
            key_id=key_id,
            endpoint=endpoint_path,
            method=method,
            decision="deny_invalid_key",
            client_ip=client_ip,
        )
        raise APIError(ERR_INVALID_KEY, "Invalid API-key, IP, or permissions for action.", 401)

    now = int(time.time())
    if row["revoked_at"]:
        record_usage(
            key_id=key_id,
            endpoint=endpoint_path,
            method=method,
            decision="deny_revoked",
            client_ip=client_ip,
        )
        raise APIError(ERR_INVALID_KEY, "API-key has been revoked.", 401)
    if row["expires_at"] and row["expires_at"] <= now:
        record_usage(
            key_id=key_id,
            endpoint=endpoint_path,
            method=method,
            decision="deny_expired",
            client_ip=client_ip,
        )
        raise APIError(ERR_INVALID_KEY, "API-key has expired.", 401)

    if not ip_in_allowlist(client_ip or "", row["ip_allowlist"]):
        record_usage(
            key_id=key_id,
            endpoint=endpoint_path,
            method=method,
            decision="deny_ip",
            client_ip=client_ip,
        )
        raise APIError(ERR_UNAUTHORIZED, "Source IP not in API-key allowlist.", 401)

    scopes = tuple(s.strip() for s in (row["scopes"] or "").split(",") if s.strip())
    if required_scope not in scopes:
        record_usage(
            key_id=key_id,
            endpoint=endpoint_path,
            method=method,
            decision="deny_scope",
            client_ip=client_ip,
        )
        raise APIError(
            ERR_UNAUTHORIZED, f"This API-key lacks the '{required_scope}' permission.", 401
        )

    # ---- Parse the timestamp / recvWindow from the signed query --------
    pairs = urllib.parse.parse_qsl(query_string, keep_blank_values=True)
    qmap = dict(pairs)
    try:
        ts_ms = int(qmap.get("timestamp") or 0)
    except ValueError:
        ts_ms = 0
    try:
        recv_window = int(qmap.get("recvWindow") or RECV_WINDOW_DEFAULT_MS)
    except ValueError:
        recv_window = RECV_WINDOW_DEFAULT_MS
    if recv_window <= 0 or recv_window > RECV_WINDOW_MAX_MS:
        recv_window = RECV_WINDOW_DEFAULT_MS
    now_ms = int(time.time() * 1000)
    if ts_ms <= 0 or abs(now_ms - ts_ms) > recv_window:
        record_usage(
            key_id=key_id,
            endpoint=endpoint_path,
            method=method,
            decision="deny_timestamp",
            client_ip=client_ip,
        )
        raise APIError(ERR_TIMESTAMP, "Timestamp for this request is outside the recvWindow.", 401)

    # ---- Verify HMAC-SHA256 signature (constant-time) ------------------
    # Binance wire-spec: signature = HMAC-SHA256(secret_bytes, query_string).
    # The client never sends the secret over the wire — only the signature.
    # To verify, the server must hold key material it can recompute the same
    # HMAC with. We store the raw secret bytes in ``secret_hash`` as opaque
    # HMAC-key material (the column name is kept for schema-compat with the
    # spec). Treat that column like a private key: never log it, never echo
    # it, only ever feed it to ``hmac.new`` here.
    expected = hmac.new(
        bytes(row["secret_hash"]),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, (signature or "").lower()):
        record_usage(
            key_id=key_id,
            endpoint=endpoint_path,
            method=method,
            decision="deny_signature",
            client_ip=client_ip,
        )
        raise APIError(ERR_SIGNATURE, "Signature for this request is not valid.", 401)

    # ---- Hourly rate cap ----------------------------------------------
    if row["hourly_request_cap"]:
        since = int(time.time()) - 3600
        n_recent = hourly_request_count(key_id, since)
        if n_recent >= int(row["hourly_request_cap"]):
            record_usage(
                key_id=key_id,
                endpoint=endpoint_path,
                method=method,
                decision="deny_quota",
                client_ip=client_ip,
            )
            raise APIError(ERR_TOO_MANY, "Too many requests for this API-key.", 429)

    # ---- Allow ---------------------------------------------------------
    record_usage(
        key_id=key_id, endpoint=endpoint_path, method=method, decision="allow", client_ip=client_ip
    )
    update_last_used(key_id)
    return {
        "opex_user": row["opex_user"],
        "user_id": row["user_id"],
        "key_id": key_id,
        "scopes": list(scopes),
        "daily_quote_cap_usdt": row["daily_quote_cap_usdt"],
    }


# --- HTTP handler ----------------------------------------------------------
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "zkcex-apikeys/1.0"

    # ---- helpers ----------------------------------------------------------
    def _send_json(self, status: int, payload: dict | list | None):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_binance_error(self, code: int, msg: str, http_status: int = 401):
        self._send_json(http_status, {"code": code, "msg": msg})

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _bearer(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        if not auth.lower().startswith("bearer "):
            return None
        return auth.split(None, 1)[1].strip()

    def _require_user(self) -> dict | None:
        user = authenticate_bearer(self._bearer())
        if not user:
            self._send_json(401, {"error": "unauthorized"})
            return None
        return user

    def _client_ip(self) -> str:
        # client_address is (host, port). For loopback we just want the IP.
        return self.client_address[0] if self.client_address else ""

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[api-keys] {self.address_string()} - {fmt % args}\n")

    # ---- routing ----------------------------------------------------------
    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        self._dispatch()

    def do_POST(self):  # noqa: N802
        self._dispatch()

    def do_PATCH(self):  # noqa: N802
        self._dispatch()

    def do_DELETE(self):  # noqa: N802
        self._dispatch()

    def _dispatch(self):
        path = urllib.parse.urlsplit(self.path).path
        m = self.command
        try:
            if m == "GET" and path == "/api-keys/health":
                return self._send_json(200, {"ok": True, "service": "api-keys"})
            if m == "POST" and path == "/api-keys/create":
                return self.h_create()
            if m == "GET" and path == "/api-keys/list":
                return self.h_list()
            if m == "POST" and path == "/api-keys/verify":
                return self.h_verify()
            # /api-keys/<key_id>[/usage]
            if path.startswith("/api-keys/"):
                rest = path[len("/api-keys/") :]
                parts = [p for p in rest.split("/") if p != ""]
                if len(parts) == 1:
                    key_id = parts[0]
                    if m == "PATCH":
                        return self.h_patch(key_id)
                    if m == "DELETE":
                        return self.h_delete(key_id)
                if len(parts) == 2 and parts[1] == "usage" and m == "GET":
                    return self.h_usage(parts[0])
            self._send_json(404, {"error": "not_found", "path": self.path})
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[api-keys] handler error on {self.path}: {e!r}\n")
            self._send_json(500, {"error": "server_error"})

    # ---- POST /api-keys/create -------------------------------------------
    def h_create(self):
        user = self._require_user()
        if not user:
            return
        body = self._read_json()
        label = (body.get("label") or "").strip()
        if not label or len(label) > 64:
            return self._send_json(400, {"error": "invalid_label"})
        scopes = parse_scopes(body.get("scopes"))
        if scopes is None:
            return self._send_json(
                400, {"error": "invalid_scopes", "allowed": sorted(ALLOWED_SCOPES)}
            )
        if "withdraw" in scopes and not withdraw_confirm_ok(body.get("confirm_phrase")):
            return self._send_json(
                400,
                {
                    "error": "withdraw_confirm_required",
                    "message": (
                        "To enable the 'withdraw' scope, also pass "
                        f'confirm_phrase: "{WITHDRAW_CONFIRM_PHRASE}"'
                    ),
                    "confirm_phrase": WITHDRAW_CONFIRM_PHRASE,
                },
            )

        ip_allowlist = parse_ip_allowlist(body.get("ip_allowlist"))
        daily_cap = parse_decimal_cap(body.get("daily_quote_cap_usdt"))
        hourly_cap = parse_int_cap(body.get("hourly_request_cap"))
        try:
            expires_in_days = int(
                body.get("expires_in_days")
                if body.get("expires_in_days") is not None
                else DEFAULT_EXPIRES_DAYS
            )
        except (TypeError, ValueError):
            return self._send_json(400, {"error": "invalid_expires_in_days"})
        if expires_in_days < 0 or expires_in_days > 3650:
            return self._send_json(400, {"error": "invalid_expires_in_days"})

        now = int(time.time())
        expires_at = now + expires_in_days * 86400 if expires_in_days > 0 else None

        # Generate id + secret. The Binance wire-spec dictates that the
        # client signs with ``HMAC-SHA256(secret, query_string)``, so the
        # server must hold material it can recompute that HMAC with. We
        # persist the raw secret bytes as opaque HMAC-key material in the
        # ``secret_hash`` column; the salt is reserved for a future
        # envelope-encryption upgrade.
        key_id = make_key_id()
        secret = make_secret()
        salt = secrets.token_bytes(16)
        secret_hash = secret.encode("utf-8")  # HMAC key (Binance wire-compat)

        with _db_lock, db() as conn:
            conn.execute(
                "INSERT INTO api_keys "
                "(key_id, secret_hash, secret_salt, user_id, opex_user, "
                " label, scopes, ip_allowlist, daily_quote_cap_usdt, "
                " hourly_request_cap, created_at, expires_at, metadata_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key_id,
                    secret_hash,
                    salt,
                    int(user["id"]),
                    user["opex_user"],
                    label,
                    ",".join(scopes),
                    ip_allowlist,
                    daily_cap,
                    hourly_cap,
                    now,
                    expires_at,
                    None,
                ),
            )
        sys.stderr.write(
            f"[api-keys] created key={key_id} user={user['opex_user']} "
            f"scopes={scopes} expires_at={expires_at}\n"
        )
        return self._send_json(
            201,
            {
                "key_id": key_id,
                "secret": secret,
                "scopes": list(scopes),
                "expires_at": expires_at,
                "label": label,
                "ip_allowlist": ip_allowlist,
                "daily_quote_cap_usdt": daily_cap,
                "hourly_request_cap": hourly_cap,
                "warning": (
                    "이 시크릿은 다시 표시되지 않습니다 / " "This secret will not be shown again."
                ),
            },
        )

    # ---- GET /api-keys/list ----------------------------------------------
    def h_list(self):
        user = self._require_user()
        if not user:
            return
        with db() as conn:
            rows = conn.execute(
                "SELECT key_id, label, scopes, ip_allowlist, expires_at, "
                "       revoked_at, last_used_at, daily_quote_cap_usdt, "
                "       hourly_request_cap, created_at "
                "FROM api_keys WHERE user_id=? ORDER BY created_at DESC",
                (int(user["id"]),),
            ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "key_id": r["key_id"],
                    "label": r["label"],
                    "scopes": [s for s in (r["scopes"] or "").split(",") if s],
                    "ip_allowlist": r["ip_allowlist"],
                    "expires_at": r["expires_at"],
                    "revoked_at": r["revoked_at"],
                    "last_used_at": r["last_used_at"],
                    "daily_quote_cap_usdt": r["daily_quote_cap_usdt"],
                    "hourly_request_cap": r["hourly_request_cap"],
                    "created_at": r["created_at"],
                }
            )
        return self._send_json(200, {"keys": out})

    # ---- PATCH /api-keys/<key_id> ----------------------------------------
    def h_patch(self, key_id: str):
        user = self._require_user()
        if not user:
            return
        body = self._read_json()
        with _db_lock, db() as conn:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE key_id=? AND user_id=?",
                (key_id, int(user["id"])),
            ).fetchone()
            if not row:
                return self._send_json(404, {"error": "not_found"})

            updates: list[str] = []
            args: list[Any] = []
            if "label" in body:
                lbl = (body.get("label") or "").strip()
                if not lbl or len(lbl) > 64:
                    return self._send_json(400, {"error": "invalid_label"})
                updates.append("label=?")
                args.append(lbl)
            if "scopes" in body:
                scopes = parse_scopes(body.get("scopes"))
                if scopes is None:
                    return self._send_json(400, {"error": "invalid_scopes"})
                old_scopes = {s for s in (row["scopes"] or "").split(",") if s}
                if "withdraw" in scopes and "withdraw" not in old_scopes:
                    if not withdraw_confirm_ok(body.get("confirm_phrase")):
                        return self._send_json(
                            400,
                            {
                                "error": "withdraw_confirm_required",
                                "confirm_phrase": WITHDRAW_CONFIRM_PHRASE,
                            },
                        )
                updates.append("scopes=?")
                args.append(",".join(scopes))
            if "ip_allowlist" in body:
                ip_allow = parse_ip_allowlist(body.get("ip_allowlist"))
                updates.append("ip_allowlist=?")
                args.append(ip_allow)
            if "daily_quote_cap_usdt" in body:
                cap = parse_decimal_cap(body.get("daily_quote_cap_usdt"))
                updates.append("daily_quote_cap_usdt=?")
                args.append(cap)
            if "hourly_request_cap" in body:
                cap = parse_int_cap(body.get("hourly_request_cap"))
                updates.append("hourly_request_cap=?")
                args.append(cap)
            if not updates:
                return self._send_json(400, {"error": "no_fields"})
            args.append(key_id)
            args.append(int(user["id"]))
            conn.execute(
                f"UPDATE api_keys SET {', '.join(updates)} "  # noqa: S608
                "WHERE key_id=? AND user_id=?",
                args,
            )
            row = conn.execute(
                "SELECT key_id, label, scopes, ip_allowlist, expires_at, "
                "       revoked_at, last_used_at, daily_quote_cap_usdt, "
                "       hourly_request_cap, created_at "
                "FROM api_keys WHERE key_id=?",
                (key_id,),
            ).fetchone()
        return self._send_json(
            200,
            {
                "key_id": row["key_id"],
                "label": row["label"],
                "scopes": [s for s in (row["scopes"] or "").split(",") if s],
                "ip_allowlist": row["ip_allowlist"],
                "expires_at": row["expires_at"],
                "revoked_at": row["revoked_at"],
                "last_used_at": row["last_used_at"],
                "daily_quote_cap_usdt": row["daily_quote_cap_usdt"],
                "hourly_request_cap": row["hourly_request_cap"],
                "created_at": row["created_at"],
            },
        )

    # ---- DELETE /api-keys/<key_id> ---------------------------------------
    def h_delete(self, key_id: str):
        user = self._require_user()
        if not user:
            return
        now = int(time.time())
        with _db_lock, db() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET revoked_at=? "
                "WHERE key_id=? AND user_id=? AND revoked_at IS NULL",
                (now, key_id, int(user["id"])),
            )
            if cur.rowcount == 0:
                # Maybe key doesn't exist, or already revoked.
                row = conn.execute(
                    "SELECT 1 FROM api_keys WHERE key_id=? AND user_id=?",
                    (key_id, int(user["id"])),
                ).fetchone()
                if not row:
                    return self._send_json(404, {"error": "not_found"})
        return self._send_json(200, {"key_id": key_id, "revoked_at": now})

    # ---- GET /api-keys/<key_id>/usage ------------------------------------
    def h_usage(self, key_id: str):
        user = self._require_user()
        if not user:
            return
        try:
            limit = int(
                urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("limit", ["50"])[
                    0
                ]
            )
        except ValueError:
            limit = 50
        limit = max(1, min(limit, 500))
        with db() as conn:
            owner = conn.execute(
                "SELECT user_id FROM api_keys WHERE key_id=?", (key_id,)
            ).fetchone()
            if not owner or int(owner["user_id"]) != int(user["id"]):
                return self._send_json(404, {"error": "not_found"})
            rows = conn.execute(
                "SELECT endpoint, method, decision, notional_usdt, client_ip, ts "
                "FROM api_key_usage WHERE key_id=? ORDER BY ts DESC LIMIT ?",
                (key_id, limit),
            ).fetchall()
        return self._send_json(
            200,
            {
                "key_id": key_id,
                "rows": [
                    {
                        "endpoint": r["endpoint"],
                        "method": r["method"],
                        "decision": r["decision"],
                        "notional_usdt": r["notional_usdt"],
                        "client_ip": r["client_ip"],
                        "ts": r["ts"],
                    }
                    for r in rows
                ],
            },
        )

    # ---- POST /api-keys/verify (server-internal, loopback only) ----------
    def h_verify(self):
        client_ip = self._client_ip()
        if not is_loopback(client_ip):
            # Pretend the route doesn't exist to outside callers.
            return self._send_json(404, {"error": "not_found"})
        body = self._read_json()
        key_id = (body.get("key_id") or "").strip()
        signature = (body.get("signature") or "").strip()
        query_string = body.get("query_string") or ""
        endpoint_path = body.get("endpoint_path") or "/"
        method = (body.get("method") or "GET").upper()
        required_scope = (body.get("required_scope") or "read").lower()
        peer_ip = (body.get("client_ip") or "").strip() or client_ip
        try:
            result = verify_signed_request(
                key_id=key_id,
                signature=signature,
                query_string=query_string,
                client_ip=peer_ip,
                endpoint_path=endpoint_path,
                method=method,
                required_scope=required_scope,
            )
        except APIError as e:
            return self._send_json(e.http_status, {"code": e.code, "msg": e.msg})
        return self._send_json(200, result)


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5550
    init_db()
    sys.stderr.write(f"[api-keys] db={DB_PATH}\n")
    sys.stderr.write(f"[api-keys] auth upstream={AUTH_BASE}\n")
    sys.stderr.write(f"[api-keys] listening on :{port}\n")
    with ThreadingServer(("", port), Handler) as srv:
        srv.serve_forever()


if __name__ == "__main__":
    main()
