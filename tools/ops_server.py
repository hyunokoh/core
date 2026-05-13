#!/usr/bin/env python3
"""Operator console / compliance ops dashboard server (port 5620).

Standalone stdlib-only HTTP service for the internal compliance + ops team.
It is NOT linked from any customer-facing page; operators bookmark or are
given the URL directly. The console is *role-gated*: a separate ``operators``
table holds the staff list, and operators authenticate with their normal
customer email+password PLUS a per-operator ``staff_token`` (issued at
bootstrap time, hashed at rest, never round-tripped through customer code).

The three core workflows:

  1. **KYC review** — queue of users with ``kyc_status='none'`` or
     ``'pending'``. Operator opens a row, sees the PASS / Sumsub fields,
     attached deposit history, and approves / rejects / requests-more-info.
  2. **Withdraw approval** — chain_server holds large withdraws
     (>= WITHDRAW_REVIEW_THRESHOLD_USDT, default $1000) in
     ``withdraw_holds`` and refuses to broadcast. The operator approves
     here and ops_server pings chain_server's new
     ``/chain/withdraw/release/<id>`` endpoint to actually fire the tx.
  3. **AML escalation queue** — REVIEW decisions from chain_server's AML
     screening land here; an operator either clears (releases the funds)
     or blocks (permanent block on the address).

Every action is logged to ``op_actions`` — even denials.

State at ``tools/.local/ops.db``. Customer credentials are read via the
auth_server HTTP API (``POST /auth/login``); the operator's customer-tier
session token from that call is immediately discarded and replaced with an
ops-only token. An ops token can NEVER be used as a customer session, and
vice versa, because:

  * ops tokens live in ``operators_sessions`` (this DB),
  * customer tokens live in ``sessions`` (auth.db),
  * the two tables share no keyspace and no service looks at both.

The schema is initialised on first run via ``ops_bootstrap.py`` which also
mints the staff_token for a new operator. See module-level main() below.
"""

from __future__ import annotations

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
import traceback
import urllib.error
import urllib.parse
import urllib.request

# --- Paths ----------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join(HERE, ".local")
os.makedirs(LOCAL_DIR, exist_ok=True)
DB_PATH = os.path.join(LOCAL_DIR, "ops.db")
AUTH_DB_PATH = os.path.join(LOCAL_DIR, "auth.db")
CHAIN_DB_PATH = os.path.join(LOCAL_DIR, "chain.db")
HOMEPAGE_OPS_DIR = os.path.normpath(os.path.join(HERE, os.pardir, "homepage", "ops"))


# --- Config ---------------------------------------------------------------
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
    "OPS_AUTH_BASE", os.environ.get("OPS_AUTH_BASE", "http://127.0.0.1:5501")
)
CHAIN_BASE = _validated_http_base_url(
    "OPS_CHAIN_BASE", os.environ.get("OPS_CHAIN_BASE", "http://127.0.0.1:5502")
)
PUSH_BASE = _validated_http_base_url(
    "OPS_PUSH_BASE", os.environ.get("OPS_PUSH_BASE", "http://127.0.0.1:5580")
)
NOTIF_BASE = _validated_http_base_url(
    "NOTIF_BASE", os.environ.get("NOTIF_BASE", "http://127.0.0.1:5691")
)
OPS_SESSION_TTL_S = int(os.environ.get("OPS_SESSION_TTL_S", str(8 * 3600)))
SCRYPT_N = int(os.environ.get("OPS_SCRYPT_N", "16384"))
SCRYPT_R = int(os.environ.get("OPS_SCRYPT_R", "8"))
SCRYPT_P = int(os.environ.get("OPS_SCRYPT_P", "1"))
SCRYPT_DKLEN = 64
# scrypt requires OpenSSL >= 1.1.0; auto-fallback to PBKDF2-SHA512 otherwise.
_HAS_SCRYPT = hasattr(hashlib, "scrypt")
PBKDF2_ITERS = int(os.environ.get("OPS_PBKDF2_ITERS", "210000"))

START_TS = int(time.time())
_db_lock = threading.Lock()
_runtime_state: dict[str, object] = {}


def log(msg: str) -> None:
    sys.stderr.write(f"[ops] {msg}\n")
    sys.stderr.flush()


# ==========================================================================
# DB
# ==========================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS operators (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE NOT NULL,
  display_name TEXT NOT NULL,
  role TEXT NOT NULL,
  staff_token_hash BLOB NOT NULL,
  staff_token_salt BLOB NOT NULL,
  created_at INTEGER NOT NULL,
  last_login_at INTEGER,
  is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS operators_sessions (
  token TEXT PRIMARY KEY,
  operator_id INTEGER NOT NULL REFERENCES operators(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  ip_redacted TEXT
);
CREATE INDEX IF NOT EXISTS idx_ops_sessions_op ON operators_sessions(operator_id);

CREATE TABLE IF NOT EXISTS op_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  operator_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  target_kind TEXT NOT NULL,
  target_id TEXT NOT NULL,
  reason TEXT,
  metadata_json TEXT,
  ip_redacted TEXT
);
CREATE INDEX IF NOT EXISTS idx_op_actions_ts ON op_actions(ts DESC);
CREATE INDEX IF NOT EXISTS idx_op_actions_op ON op_actions(operator_id);
CREATE INDEX IF NOT EXISTS idx_op_actions_action ON op_actions(action);

CREATE TABLE IF NOT EXISTS withdraw_holds (
  withdraw_id TEXT PRIMARY KEY,
  opex_user TEXT NOT NULL,
  asset TEXT NOT NULL,
  amount TEXT NOT NULL,
  destination TEXT NOT NULL,
  chain TEXT NOT NULL,
  amount_usdt TEXT,
  status TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  reviewed_at INTEGER,
  reviewed_by INTEGER,
  decision_reason TEXT,
  aml_status TEXT,
  aml_decision_json TEXT,
  callback_url TEXT
);
CREATE INDEX IF NOT EXISTS idx_holds_status ON withdraw_holds(status);
CREATE INDEX IF NOT EXISTS idx_holds_user ON withdraw_holds(opex_user);

CREATE TABLE IF NOT EXISTS kyc_reviews (
  kyc_request_id TEXT PRIMARY KEY,
  opex_user TEXT NOT NULL,
  applied_at INTEGER NOT NULL,
  status TEXT NOT NULL,
  reviewed_at INTEGER,
  reviewed_by INTEGER,
  decision_reason TEXT,
  source TEXT,
  metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_kyc_reviews_status ON kyc_reviews(status);

CREATE TABLE IF NOT EXISTS aml_escalations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  opex_user TEXT NOT NULL,
  trigger_kind TEXT NOT NULL,
  trigger_ref TEXT,
  decision_source TEXT,
  risk_score INTEGER,
  reasons_json TEXT,
  status TEXT NOT NULL,
  opened_at INTEGER NOT NULL,
  resolved_at INTEGER,
  resolved_by INTEGER,
  resolution_reason TEXT,
  address TEXT
);
CREATE INDEX IF NOT EXISTS idx_aml_status ON aml_escalations(status);
CREATE INDEX IF NOT EXISTS idx_aml_user ON aml_escalations(opex_user);

CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  category TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT,
  severity TEXT NOT NULL,
  status TEXT NOT NULL,
  opened_at INTEGER NOT NULL,
  resolved_at INTEGER,
  assigned_to INTEGER,
  notes_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);

CREATE TABLE IF NOT EXISTS aml_blocklist (
  address TEXT PRIMARY KEY,
  added_at INTEGER NOT NULL,
  added_by INTEGER,
  reason TEXT
);
"""


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def auth_db_ro() -> sqlite3.Connection | None:
    """Read-only handle to auth.db for the KYC queue fallback. None if
    the file is absent (the auth backend may be Postgres in that case, see
    the HTTP fallback in ``list_kyc_pending_users``)."""
    if not os.path.exists(AUTH_DB_PATH):
        return None
    uri = f"file:{urllib.parse.quote(AUTH_DB_PATH)}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError as e:
        log(f"auth.db open failed: {e!r}")
        return None


def chain_db_ro() -> sqlite3.Connection | None:
    if not os.path.exists(CHAIN_DB_PATH):
        return None
    uri = f"file:{urllib.parse.quote(CHAIN_DB_PATH)}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError as e:
        log(f"chain.db open failed: {e!r}")
        return None


def init_db() -> None:
    with _db_lock, db() as conn:
        conn.executescript(SCHEMA)


# ==========================================================================
# Crypto / token helpers
# ==========================================================================
def hash_staff_token(token: str, salt: bytes) -> bytes:
    if _HAS_SCRYPT:
        return hashlib.scrypt(
            token.encode("utf-8"),
            salt=salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=SCRYPT_DKLEN,
        )
    return hashlib.pbkdf2_hmac("sha512", token.encode("utf-8"), salt, PBKDF2_ITERS, SCRYPT_DKLEN)


def verify_staff_token(token: str, salt: bytes, expected: bytes) -> bool:
    if not token or not expected:
        return False
    got = hash_staff_token(token, salt)
    return hmac.compare_digest(got, expected)


def make_session_token() -> str:
    return "ops_" + secrets.token_urlsafe(32)


def make_staff_token() -> str:
    # 32 bytes -> 43 char URL-safe string. Prefix makes the format
    # self-describing in operator-facing logs / docs.
    return "stf_" + secrets.token_urlsafe(32)


def redact_ip(ip: str | None) -> str:
    """Drop the last octet of an IPv4 / last 80 bits of an IPv6 for the
    audit log. Matches the privacy treatment in auth_server.geo_decisions."""
    if not ip:
        return ""
    if ":" in ip:
        parts = ip.split(":")
        return ":".join(parts[:3]) + "::"
    parts = ip.split(".")
    if len(parts) == 4:
        return ".".join(parts[:3]) + ".0"
    return ip


# ==========================================================================
# Operator bootstrap (called from ops_bootstrap.py too)
# ==========================================================================
def create_operator(*, email: str, display_name: str, role: str) -> tuple[int, str]:
    """Create a new operator and return ``(operator_id, plaintext_staff_token)``.
    The plaintext token is shown ONCE (by ops_bootstrap.py); only the scrypt
    hash is persisted, so it cannot be recovered later.
    """
    email = (email or "").strip().lower()
    display_name = (display_name or "").strip()
    role = (role or "").strip().lower()
    if "@" not in email:
        raise ValueError("email is required")
    if not display_name:
        raise ValueError("display_name is required")
    if role not in ("compliance", "support", "admin"):
        raise ValueError("role must be one of: compliance, support, admin")
    token = make_staff_token()
    salt = secrets.token_bytes(16)
    h = hash_staff_token(token, salt)
    now = int(time.time())
    with _db_lock, db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO operators (email, display_name, role, "
                "staff_token_hash, staff_token_salt, created_at, is_active) "
                "VALUES (?,?,?,?,?,?,1)",
                (email, display_name, role, sqlite3.Binary(h), sqlite3.Binary(salt), now),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"operator already exists: {email}") from None
        return int(cur.lastrowid), token


def rotate_staff_token(email: str) -> str:
    """Issue a new staff_token for an existing operator and return it."""
    token = make_staff_token()
    salt = secrets.token_bytes(16)
    h = hash_staff_token(token, salt)
    with _db_lock, db() as conn:
        cur = conn.execute(
            "UPDATE operators SET staff_token_hash=?, staff_token_salt=? " "WHERE email=?",
            (sqlite3.Binary(h), sqlite3.Binary(salt), email.lower()),
        )
        if cur.rowcount == 0:
            raise ValueError(f"no such operator: {email}")
    return token


# ==========================================================================
# Auth helpers
# ==========================================================================
def verify_customer_password(email: str, password: str) -> dict | None:
    """Validate the operator's customer credentials by calling auth_server.
    Returns the auth_server user dict on success, or None on failure. The
    short-lived customer session token returned by /auth/login is THROWN AWAY
    here — operators never carry a customer-tier session.
    """
    try:
        req = _http_request(
            f"{AUTH_BASE}/auth/login",
            data=json.dumps({"email": email, "password": password}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with _http_urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError:
        return None
    except Exception as e:  # noqa: BLE001
        log(f"customer credential check failed: {e!r}")
        return None
    customer_token = data.get("token")
    if customer_token:
        # Discard the customer session so the operator's password check
        # doesn't accidentally grant a customer session as a side effect.
        try:
            req = _http_request(
                f"{AUTH_BASE}/auth/logout",
                method="POST",
                headers={"Authorization": f"Bearer {customer_token}"},
            )
            _http_urlopen(req, timeout=3).read()
        except Exception as e:  # noqa: BLE001
            log(f"customer logout cleanup failed: {e!r}")
    return data.get("user")


def issue_ops_session(operator_id: int, *, ip: str | None) -> tuple[str, int]:
    token = make_session_token()
    now = int(time.time())
    expires = now + OPS_SESSION_TTL_S
    with _db_lock, db() as conn:
        conn.execute(
            "INSERT INTO operators_sessions (token, operator_id, created_at, "
            "expires_at, ip_redacted) VALUES (?,?,?,?,?)",
            (token, operator_id, now, expires, redact_ip(ip)),
        )
        conn.execute(
            "UPDATE operators SET last_login_at=? WHERE id=?",
            (now, operator_id),
        )
    return token, expires


def lookup_ops_session(token: str | None) -> dict | None:
    if not token:
        return None
    now = int(time.time())
    with db() as conn:
        row = conn.execute(
            "SELECT o.id, o.email, o.display_name, o.role, o.is_active, "
            "       s.expires_at "
            "FROM operators_sessions s JOIN operators o ON o.id=s.operator_id "
            "WHERE s.token=? AND s.expires_at>?",
            (token, now),
        ).fetchone()
    if not row or not row["is_active"]:
        return None
    return dict(row)


def revoke_ops_session(token: str) -> None:
    with _db_lock, db() as conn:
        conn.execute("DELETE FROM operators_sessions WHERE token=?", (token,))


# Role -> permission set. Anything not in the operator's set is 403.
ROLE_PERMS = {
    "admin": {"kyc", "withdraw", "aml", "incident", "audit"},
    "compliance": {"kyc", "withdraw", "aml", "incident", "audit"},
    "support": {"kyc", "incident", "audit"},
}


def role_allows(role: str, permission: str) -> bool:
    return permission in ROLE_PERMS.get(role or "", set())


def log_action(
    operator_id: int,
    *,
    action: str,
    target_kind: str,
    target_id: str,
    reason: str | None,
    metadata: dict | None,
    ip: str | None,
) -> None:
    with _db_lock, db() as conn:
        conn.execute(
            "INSERT INTO op_actions (ts, operator_id, action, target_kind, "
            "target_id, reason, metadata_json, ip_redacted) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                int(time.time()),
                operator_id,
                action,
                target_kind,
                target_id,
                reason,
                json.dumps(metadata or {}, separators=(",", ":")),
                redact_ip(ip),
            ),
        )


# ==========================================================================
# Data adapters
# ==========================================================================
def list_kyc_pending_users(limit: int = 50, cursor: int = 0) -> dict:
    """Walk auth.db (or the HTTP fallback) for users whose ``kyc_status`` is
    ``none`` or ``pending``. Returns a paged response keyed off the user id."""
    rows: list[dict] = []
    conn = auth_db_ro()
    if conn is not None:
        try:
            db_rows = conn.execute(
                "SELECT id, opex_user, email, name, kyc_status, kyc_name, "
                "       kyc_phone, kyc_birth, kyc_gender, kyc_carrier, "
                "       created_at, kyc_provider_request_id "
                "FROM users WHERE kyc_status IN ('none','pending') "
                "AND id > ? ORDER BY id ASC LIMIT ?",
                (cursor, limit),
            ).fetchall()
            for r in db_rows:
                rows.append(dict(r))
        except sqlite3.OperationalError as e:
            log(f"auth.db query failed: {e!r}")
        finally:
            try:
                conn.close()
            except Exception as e:  # noqa: BLE001
                log(f"auth db close failed: {e!r}")
    next_cursor = rows[-1]["id"] if rows else cursor
    return {"users": rows, "next_cursor": next_cursor, "count": len(rows)}


def get_user_detail(opex_user: str) -> dict | None:
    """Full record for KYC review: user row + sumsub state + recent deposits."""
    out: dict = {"opex_user": opex_user}
    aconn = auth_db_ro()
    if aconn is not None:
        try:
            r = aconn.execute(
                "SELECT id, email, name, opex_user, kyc_status, kyc_verified_at, "
                "       kyc_name, kyc_phone, kyc_birth, kyc_gender, kyc_carrier, "
                "       created_at, kyc_provider_request_id "
                "FROM users WHERE opex_user=?",
                (opex_user,),
            ).fetchone()
            if not r:
                return None
            out["user"] = dict(r)
            sr = aconn.execute(
                "SELECT applicant_id, level_name, last_status, last_review_answer, "
                "       last_synced_at, created_at FROM sumsub_applicants "
                "WHERE external_user_id=?",
                (opex_user,),
            ).fetchone()
            out["sumsub"] = dict(sr) if sr else None
        finally:
            try:
                aconn.close()
            except Exception as e:  # noqa: BLE001
                log(f"auth detail db close failed: {e!r}")
    else:
        return None
    cconn = chain_db_ro()
    if cconn is not None:
        try:
            deps = cconn.execute(
                "SELECT id, tx, asset, amount, status, observed_at, credited_at "
                "FROM deposits WHERE opex_user=? ORDER BY id DESC LIMIT 20",
                (opex_user,),
            ).fetchall()
            out["deposits"] = [dict(d) for d in deps]
        finally:
            try:
                cconn.close()
            except Exception as e:  # noqa: BLE001
                log(f"chain detail db close failed: {e!r}")
    else:
        out["deposits"] = []
    return out


def set_user_kyc_status_via_auth(opex_user: str, status: str) -> bool:
    """Flip kyc_status on auth.db directly. We hold the lock briefly while
    writing — auth_server.py uses WAL so concurrent reads still work. The
    auth_db abstraction sits on the same file; writing here is equivalent
    to the auth_server's own ``set_user_kyc_status`` call."""
    conn = sqlite3.connect(AUTH_DB_PATH, timeout=10.0, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.execute(
            "UPDATE users SET kyc_status=? WHERE opex_user=?",
            (status, opex_user),
        )
        return cur.rowcount > 0
    finally:
        conn.close()


# ==========================================================================
# Withdraw release callback (loopback to chain_server)
# ==========================================================================
def release_withdraw_to_chain(withdraw_id: str) -> tuple[bool, dict]:
    try:
        req = _http_request(
            f"{CHAIN_BASE}/chain/withdraw/release/{urllib.parse.quote(withdraw_id)}",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json", "X-Ops-Loopback": "1"},
        )
        with _http_urlopen(req, timeout=10) as resp:
            return True, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read()
        return False, {
            "error": f"chain_release_failed_{e.code}",
            "message": body[:300].decode(errors="replace"),
        }
    except Exception as e:  # noqa: BLE001
        return False, {"error": "chain_release_unavailable", "message": str(e)}


def cancel_withdraw_to_chain(withdraw_id: str) -> tuple[bool, dict]:
    try:
        req = _http_request(
            f"{CHAIN_BASE}/chain/withdraw/cancel/{urllib.parse.quote(withdraw_id)}",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json", "X-Ops-Loopback": "1"},
        )
        with _http_urlopen(req, timeout=10) as resp:
            return True, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read()
        return False, {
            "error": f"chain_cancel_failed_{e.code}",
            "message": body[:300].decode(errors="replace"),
        }
    except Exception as e:  # noqa: BLE001
        return False, {"error": "chain_cancel_unavailable", "message": str(e)}


def push_notify(opex_user: str, payload: dict) -> None:
    if not opex_user:
        return
    try:
        body = json.dumps({"opex_user": opex_user, "payload": payload}).encode("utf-8")
        req = _http_request(
            f"{PUSH_BASE}/push/send",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        _http_urlopen(req, timeout=3).read()
    except Exception as e:  # noqa: BLE001
        log(f"push notify skipped: {e!r}")


def notif_send(
    opex_user: str, ntype: str, category: str, title: str, body: str, metadata: dict | None = None
) -> None:
    """Best-effort inbox+email+push fan-out via notification_center."""
    if not opex_user:
        return
    try:
        payload = {
            "opex_user": opex_user,
            "type": ntype,
            "category": category,
            "title": title,
            "body": body,
            "metadata": metadata or {},
        }
        req = _http_request(
            f"{NOTIF_BASE}/notifications/send",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        _http_urlopen(req, timeout=1).read()
    except Exception as e:  # noqa: BLE001
        log(f"notification send skipped: {e!r}")


# ==========================================================================
# HTTP server
# ==========================================================================
HTML_FILES = {
    "/ops/": "index.html",
    "/ops/index.html": "index.html",
    "/ops/login.html": "login.html",
    "/ops/kyc.html": "kyc.html",
    "/ops/withdraws.html": "withdraws.html",
    "/ops/aml.html": "aml.html",
    "/ops/incidents.html": "incidents.html",
    "/ops/audit.html": "audit.html",
    "/ops/me.html": "me.html",
    "/ops/ops.js": "ops.js",
    "/ops/ops.css": "ops.css",
}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "zkcex-ops/1.0"

    # -- standard plumbing -----------------------------------------------
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[ops] {self.address_string()} - {fmt % args}\n")

    def _send_json(self, status: int, payload):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_file(self, fname: str):
        path = os.path.join(HOMEPAGE_OPS_DIR, fname)
        if not os.path.isfile(path):
            return self._send_json(404, {"error": "file_not_found", "file": fname})
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError as e:
            return self._send_json(500, {"error": "read_failed", "message": str(e)})
        ctype = "text/html; charset=utf-8"
        if fname.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        elif fname.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        elif fname.endswith(".json"):
            ctype = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        if not auth.lower().startswith("bearer "):
            return None
        return auth.split(None, 1)[1].strip()

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n > 0 else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _client_ip(self) -> str:
        xff = self.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",", 1)[0].strip()
        try:
            return self.client_address[0] if self.client_address else ""
        except Exception:
            return ""

    def _client_is_loopback(self) -> bool:
        try:
            ip = self.client_address[0] if self.client_address else ""
        except Exception:
            return False
        return ip in ("127.0.0.1", "::1", "localhost")

    def _require_operator(self, *, permission: str | None = None) -> dict | None:
        op = lookup_ops_session(self._bearer())
        if not op:
            self._send_json(401, {"error": "unauthorized", "message": "ops bearer token required"})
            return None
        if permission and not role_allows(op["role"], permission):
            self._send_json(
                403, {"error": "forbidden", "message": f"role '{op['role']}' lacks '{permission}'"}
            )
            return None
        return op

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers", "Content-Type, Authorization, X-Ops-Loopback"
        )
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    # -- routing ---------------------------------------------------------
    def do_GET(self):  # noqa: N802
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            q = urllib.parse.parse_qs(parsed.query or "")
            # Static HTML / JS / CSS served from homepage/ops/
            if path in HTML_FILES:
                return self._send_file(HTML_FILES[path])
            # JSON API. Both /ops/<path> and /ops-api/<path> reach the same
            # handlers — the dual prefix lets serve_homepage proxy /ops-api/
            # without colliding with the static HTML at /ops/*.html.
            api = self._api_path(path)
            if api is None:
                return self._send_json(404, {"error": "not_found", "path": path})
            return self._route_get(api, q)
        except Exception:
            log(f"GET {self.path} crashed:\n{traceback.format_exc(limit=4)}")
            return self._send_json(500, {"error": "internal"})

    def do_POST(self):  # noqa: N802
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            api = self._api_path(path)
            if api is None:
                return self._send_json(404, {"error": "not_found", "path": path})
            return self._route_post(api)
        except Exception:
            log(f"POST {self.path} crashed:\n{traceback.format_exc(limit=4)}")
            return self._send_json(500, {"error": "internal"})

    def _api_path(self, path: str) -> str | None:
        """Normalize ``/ops-api/foo`` and ``/ops/foo`` (JSON) -> ``/foo``."""
        if path.startswith("/ops-api/"):
            return path[len("/ops-api") :]  # leading "/"
        if path.startswith("/ops/"):
            # Only treat as API if it doesn't look like a known HTML asset.
            if path in HTML_FILES:
                return None
            return path[len("/ops") :]
        return None

    # -- GET handlers ----------------------------------------------------
    def _route_get(self, p: str, q: dict):
        if p == "/health":
            return self._send_json(
                200,
                {
                    "ok": True,
                    "uptime_s": int(time.time()) - START_TS,
                },
            )
        if p == "/me":
            op = self._require_operator()
            if not op:
                return
            return self._send_json(
                200,
                {
                    "operator": {
                        "id": op["id"],
                        "email": op["email"],
                        "display_name": op["display_name"],
                        "role": op["role"],
                        "permissions": sorted(ROLE_PERMS.get(op["role"], set())),
                        "session_expires_at": op["expires_at"],
                    },
                },
            )
        if p == "/kyc/pending":
            op = self._require_operator(permission="kyc")
            if not op:
                return
            try:
                limit = max(1, min(int((q.get("limit") or ["50"])[0]), 200))
                cursor = int((q.get("cursor") or ["0"])[0])
            except ValueError:
                return self._send_json(400, {"error": "bad_query"})
            return self._send_json(200, list_kyc_pending_users(limit, cursor))
        if p.startswith("/kyc/") and p.count("/") == 2 and not p.endswith("/"):
            op = self._require_operator(permission="kyc")
            if not op:
                return
            opex = urllib.parse.unquote(p.rsplit("/", 1)[1])
            detail = get_user_detail(opex)
            if detail is None:
                return self._send_json(404, {"error": "user_not_found"})
            return self._send_json(200, detail)
        if p == "/withdraws/pending":
            op = self._require_operator(permission="withdraw")
            if not op:
                return
            try:
                limit = max(1, min(int((q.get("limit") or ["50"])[0]), 200))
            except ValueError:
                limit = 50
            with db() as conn:
                rows = conn.execute(
                    "SELECT * FROM withdraw_holds WHERE status='pending_review' "
                    "ORDER BY created_at ASC LIMIT ?",
                    (limit,),
                ).fetchall()
            return self._send_json(
                200,
                {
                    "withdraws": [dict(r) for r in rows],
                    "count": len(rows),
                },
            )
        if p == "/withdraws":
            op = self._require_operator(permission="withdraw")
            if not op:
                return
            status = (q.get("status") or [""])[0]
            with db() as conn:
                if status:
                    rows = conn.execute(
                        "SELECT * FROM withdraw_holds WHERE status=? "
                        "ORDER BY created_at DESC LIMIT 200",
                        (status,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM withdraw_holds ORDER BY created_at DESC LIMIT 200",
                    ).fetchall()
            return self._send_json(
                200,
                {
                    "withdraws": [dict(r) for r in rows],
                    "count": len(rows),
                },
            )
        if p.startswith("/withdraws/") and p.count("/") == 2:
            op = self._require_operator(permission="withdraw")
            if not op:
                return
            wid = urllib.parse.unquote(p.rsplit("/", 1)[1])
            with db() as conn:
                row = conn.execute(
                    "SELECT * FROM withdraw_holds WHERE withdraw_id=?", (wid,)
                ).fetchone()
            if not row:
                return self._send_json(404, {"error": "withdraw_not_found"})
            return self._send_json(200, {"withdraw": dict(row)})
        if p == "/aml/escalations":
            op = self._require_operator(permission="aml")
            if not op:
                return
            status = (q.get("status") or ["open"])[0]
            with db() as conn:
                rows = conn.execute(
                    "SELECT * FROM aml_escalations WHERE status=? "
                    "ORDER BY opened_at DESC LIMIT 200",
                    (status,),
                ).fetchall()
            return self._send_json(
                200,
                {
                    "escalations": [dict(r) for r in rows],
                    "count": len(rows),
                },
            )
        if p == "/aml/blocklist":
            op = self._require_operator(permission="aml")
            if not op:
                return
            with db() as conn:
                rows = conn.execute(
                    "SELECT * FROM aml_blocklist ORDER BY added_at DESC LIMIT 500",
                ).fetchall()
            return self._send_json(
                200,
                {
                    "blocklist": [dict(r) for r in rows],
                    "count": len(rows),
                },
            )
        if p == "/incidents":
            op = self._require_operator(permission="incident")
            if not op:
                return
            status = (q.get("status") or [""])[0]
            with db() as conn:
                if status:
                    rows = conn.execute(
                        "SELECT * FROM incidents WHERE status=? "
                        "ORDER BY opened_at DESC LIMIT 200",
                        (status,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM incidents ORDER BY opened_at DESC LIMIT 200",
                    ).fetchall()
            return self._send_json(
                200,
                {
                    "incidents": [dict(r) for r in rows],
                    "count": len(rows),
                },
            )
        if p == "/audit":
            op = self._require_operator(permission="audit")
            if not op:
                return
            actor = (q.get("actor") or [""])[0]
            action = (q.get("action") or [""])[0]
            try:
                limit = max(1, min(int((q.get("limit") or ["100"])[0]), 500))
            except ValueError:
                limit = 100
            sql = (
                "SELECT a.*, o.email, o.display_name FROM op_actions a "
                "LEFT JOIN operators o ON o.id=a.operator_id WHERE 1=1 "
            )
            args: list = []
            if actor:
                sql += "AND a.operator_id=? "
                args.append(int(actor))
            if action:
                sql += "AND a.action=? "
                args.append(action)
            sql += "ORDER BY a.id DESC LIMIT ?"
            args.append(limit)
            with db() as conn:
                rows = conn.execute(sql, args).fetchall()
            return self._send_json(
                200,
                {
                    "actions": [dict(r) for r in rows],
                    "count": len(rows),
                },
            )
        if p == "/overview":
            op = self._require_operator()
            if not op:
                return
            return self._send_json(200, self._overview())
        return self._send_json(404, {"error": "not_found", "path": p})

    # -- POST handlers ---------------------------------------------------
    def _route_post(self, p: str):
        if p == "/auth/login":
            return self._h_login()
        if p == "/auth/logout":
            return self._h_logout()
        # Internal hold endpoint — loopback only, no operator session
        # required. chain_server calls this when a large withdraw arrives.
        if p == "/withdraws/_internal/hold":
            return self._h_internal_hold()
        if p.startswith("/withdraws/") and p.endswith("/approve"):
            return self._h_withdraw_approve(p[len("/withdraws/") : -len("/approve")])
        if p.startswith("/withdraws/") and p.endswith("/reject"):
            return self._h_withdraw_reject(p[len("/withdraws/") : -len("/reject")])
        if p.startswith("/kyc/") and p.endswith("/approve"):
            return self._h_kyc_approve(p[len("/kyc/") : -len("/approve")])
        if p.startswith("/kyc/") and p.endswith("/reject"):
            return self._h_kyc_reject(p[len("/kyc/") : -len("/reject")])
        if p.startswith("/kyc/") and p.endswith("/request-more-info"):
            return self._h_kyc_request_more_info(p[len("/kyc/") : -len("/request-more-info")])
        if p.startswith("/aml/escalations/") and p.endswith("/clear"):
            return self._h_aml_clear(p[len("/aml/escalations/") : -len("/clear")])
        if p.startswith("/aml/escalations/") and p.endswith("/block"):
            return self._h_aml_block(p[len("/aml/escalations/") : -len("/block")])
        # Internal hook — AML providers (or chain_server) escalate REVIEW
        # decisions here. Loopback only.
        if p == "/aml/escalations/_internal/open":
            return self._h_internal_aml_open()
        if p == "/incidents":
            return self._h_incident_create()
        if p.startswith("/incidents/") and p.endswith("/update"):
            return self._h_incident_update(p[len("/incidents/") : -len("/update")])
        if p.startswith("/incidents/") and p.endswith("/close"):
            return self._h_incident_close(p[len("/incidents/") : -len("/close")])
        return self._send_json(404, {"error": "not_found", "path": p})

    # -- AUTH handlers ---------------------------------------------------
    def _h_login(self):
        body = self._read_json()
        email = (body.get("email") or "").strip().lower()
        password = body.get("password") or ""
        staff_token = body.get("staff_token") or ""
        if not email or not password or not staff_token:
            return self._send_json(
                400,
                {
                    "error": "bad_request",
                    "message": "email, password, staff_token are all required",
                },
            )
        # 1. Must be a known operator.
        with db() as conn:
            row = conn.execute(
                "SELECT * FROM operators WHERE email=? AND is_active=1",
                (email,),
            ).fetchone()
        if not row:
            return self._send_json(401, {"error": "invalid_credentials"})
        # 2. staff_token verified against the per-operator scrypt hash.
        if not verify_staff_token(
            staff_token, bytes(row["staff_token_salt"]), bytes(row["staff_token_hash"])
        ):
            return self._send_json(401, {"error": "invalid_credentials"})
        # 3. Customer-side email+password must also match (via auth_server HTTP).
        cust = verify_customer_password(email, password)
        if not cust:
            return self._send_json(401, {"error": "invalid_credentials"})
        token, expires = issue_ops_session(int(row["id"]), ip=self._client_ip())
        log_action(
            int(row["id"]),
            action="auth.login",
            target_kind="operator",
            target_id=str(row["id"]),
            reason=None,
            metadata={"email": email, "customer_opex": cust.get("opex_user")},
            ip=self._client_ip(),
        )
        return self._send_json(
            200,
            {
                "token": token,
                "expires_at": expires,
                "operator": {
                    "id": int(row["id"]),
                    "email": row["email"],
                    "display_name": row["display_name"],
                    "role": row["role"],
                    "permissions": sorted(ROLE_PERMS.get(row["role"], set())),
                },
            },
        )

    def _h_logout(self):
        tok = self._bearer()
        op = lookup_ops_session(tok)
        if tok:
            revoke_ops_session(tok)
        if op:
            log_action(
                op["id"],
                action="auth.logout",
                target_kind="operator",
                target_id=str(op["id"]),
                reason=None,
                metadata={},
                ip=self._client_ip(),
            )
        return self._send_json(204, None)

    # -- KYC handlers ----------------------------------------------------
    def _h_kyc_approve(self, opex: str):
        op = self._require_operator(permission="kyc")
        if not op:
            return
        body = self._read_json()
        reason = (body.get("reason") or "operator-approved").strip()
        ok = set_user_kyc_status_via_auth(opex, "verified")
        if not ok:
            return self._send_json(404, {"error": "user_not_found"})
        with _db_lock, db() as conn:
            conn.execute(
                "INSERT INTO kyc_reviews (kyc_request_id, opex_user, applied_at, "
                "status, reviewed_at, reviewed_by, decision_reason, source) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(kyc_request_id) DO UPDATE SET "
                "  status=excluded.status, reviewed_at=excluded.reviewed_at, "
                "  reviewed_by=excluded.reviewed_by, decision_reason=excluded.decision_reason",
                (
                    f"kyc-{opex}",
                    opex,
                    int(time.time()),
                    "approved",
                    int(time.time()),
                    op["id"],
                    reason,
                    "manual",
                ),
            )
        log_action(
            op["id"],
            action="kyc.approve",
            target_kind="user",
            target_id=opex,
            reason=reason,
            metadata={},
            ip=self._client_ip(),
        )
        push_notify(opex, {"kind": "kyc.approved", "reason": reason})
        notif_send(
            opex,
            "kyc_verified",
            "critical",
            "KYC approved",
            reason or "Your identity verification has been approved by an operator.",
            {"reason": reason},
        )
        return self._send_json(200, {"ok": True, "opex_user": opex, "kyc_status": "verified"})

    def _h_kyc_reject(self, opex: str):
        op = self._require_operator(permission="kyc")
        if not op:
            return
        body = self._read_json()
        reason = (body.get("reason") or "").strip()
        if not reason:
            return self._send_json(400, {"error": "reason_required"})
        ok = set_user_kyc_status_via_auth(opex, "rejected")
        if not ok:
            return self._send_json(404, {"error": "user_not_found"})
        with _db_lock, db() as conn:
            conn.execute(
                "INSERT INTO kyc_reviews (kyc_request_id, opex_user, applied_at, "
                "status, reviewed_at, reviewed_by, decision_reason, source) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(kyc_request_id) DO UPDATE SET "
                "  status=excluded.status, reviewed_at=excluded.reviewed_at, "
                "  reviewed_by=excluded.reviewed_by, decision_reason=excluded.decision_reason",
                (
                    f"kyc-{opex}",
                    opex,
                    int(time.time()),
                    "rejected",
                    int(time.time()),
                    op["id"],
                    reason,
                    "manual",
                ),
            )
        log_action(
            op["id"],
            action="kyc.reject",
            target_kind="user",
            target_id=opex,
            reason=reason,
            metadata={},
            ip=self._client_ip(),
        )
        push_notify(opex, {"kind": "kyc.rejected", "reason": reason})
        notif_send(
            opex,
            "security",
            "critical",
            "KYC rejected",
            reason or "Your identity verification was rejected. Please contact support.",
            {"reason": reason},
        )
        return self._send_json(200, {"ok": True, "opex_user": opex, "kyc_status": "rejected"})

    def _h_kyc_request_more_info(self, opex: str):
        op = self._require_operator(permission="kyc")
        if not op:
            return
        body = self._read_json()
        message = (body.get("message") or "").strip()
        if not message:
            return self._send_json(400, {"error": "message_required"})
        with _db_lock, db() as conn:
            conn.execute(
                "INSERT INTO kyc_reviews (kyc_request_id, opex_user, applied_at, "
                "status, reviewed_at, reviewed_by, decision_reason, source) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(kyc_request_id) DO UPDATE SET "
                "  status=excluded.status, reviewed_at=excluded.reviewed_at, "
                "  reviewed_by=excluded.reviewed_by, decision_reason=excluded.decision_reason",
                (
                    f"kyc-{opex}",
                    opex,
                    int(time.time()),
                    "needs_more_info",
                    int(time.time()),
                    op["id"],
                    message,
                    "manual",
                ),
            )
        log_action(
            op["id"],
            action="kyc.request_more_info",
            target_kind="user",
            target_id=opex,
            reason=message,
            metadata={},
            ip=self._client_ip(),
        )
        push_notify(opex, {"kind": "kyc.more_info_requested", "message": message})
        return self._send_json(200, {"ok": True, "opex_user": opex})

    # -- Withdraw handlers ----------------------------------------------
    def _h_internal_hold(self):
        if not self._client_is_loopback():
            return self._send_json(403, {"error": "loopback_only"})
        body = self._read_json()
        # Required fields populated by chain_server.
        try:
            withdraw_id = str(body["withdraw_id"])
            opex_user = str(body["opex_user"])
            asset = str(body["asset"])
            amount = str(body["amount"])
            destination = str(body["destination"])
        except KeyError as e:
            return self._send_json(400, {"error": "missing_field", "field": str(e)})
        chain_slug = str(body.get("chain") or "hardhat-localhost")
        amount_usdt = body.get("amount_usdt")
        aml_status = body.get("aml_status")
        aml_decision = body.get("aml_decision")
        with _db_lock, db() as conn:
            try:
                conn.execute(
                    "INSERT INTO withdraw_holds (withdraw_id, opex_user, asset, "
                    "amount, destination, chain, amount_usdt, status, created_at, "
                    "aml_status, aml_decision_json) "
                    "VALUES (?,?,?,?,?,?,?, 'pending_review', ?,?,?)",
                    (
                        withdraw_id,
                        opex_user,
                        asset,
                        amount,
                        destination,
                        chain_slug,
                        str(amount_usdt) if amount_usdt is not None else None,
                        int(time.time()),
                        aml_status,
                        json.dumps(aml_decision) if aml_decision else None,
                    ),
                )
            except sqlite3.IntegrityError:
                return self._send_json(
                    409, {"error": "withdraw_already_held", "withdraw_id": withdraw_id}
                )
        return self._send_json(
            200, {"ok": True, "withdraw_id": withdraw_id, "status": "pending_review"}
        )

    def _h_withdraw_approve(self, wid: str):
        op = self._require_operator(permission="withdraw")
        if not op:
            return
        body = self._read_json()
        reason = (body.get("reason") or "operator-approved").strip()
        with db() as conn:
            row = conn.execute(
                "SELECT * FROM withdraw_holds WHERE withdraw_id=?", (wid,)
            ).fetchone()
        if not row:
            return self._send_json(404, {"error": "withdraw_not_found"})
        if row["status"] != "pending_review":
            return self._send_json(409, {"error": "not_pending", "status": row["status"]})
        ok, release_resp = release_withdraw_to_chain(wid)
        now = int(time.time())
        new_status = "approved" if ok else "approval_failed"
        with _db_lock, db() as conn:
            conn.execute(
                "UPDATE withdraw_holds SET status=?, reviewed_at=?, reviewed_by=?, "
                "decision_reason=? WHERE withdraw_id=?",
                (new_status, now, op["id"], reason, wid),
            )
        log_action(
            op["id"],
            action="withdraw.approve",
            target_kind="withdraw",
            target_id=wid,
            reason=reason,
            metadata={"chain_response": release_resp, "ok": ok},
            ip=self._client_ip(),
        )
        if ok:
            push_notify(
                row["opex_user"],
                {
                    "kind": "withdraw.approved",
                    "withdraw_id": wid,
                    "asset": row["asset"],
                    "amount": row["amount"],
                },
            )
        return self._send_json(
            200 if ok else 502,
            {
                "ok": ok,
                "withdraw_id": wid,
                "status": new_status,
                "chain_response": release_resp,
            },
        )

    def _h_withdraw_reject(self, wid: str):
        op = self._require_operator(permission="withdraw")
        if not op:
            return
        body = self._read_json()
        reason = (body.get("reason") or "").strip()
        if not reason:
            return self._send_json(400, {"error": "reason_required"})
        with db() as conn:
            row = conn.execute(
                "SELECT * FROM withdraw_holds WHERE withdraw_id=?", (wid,)
            ).fetchone()
        if not row:
            return self._send_json(404, {"error": "withdraw_not_found"})
        if row["status"] != "pending_review":
            return self._send_json(409, {"error": "not_pending", "status": row["status"]})
        ok, cancel_resp = cancel_withdraw_to_chain(wid)
        now = int(time.time())
        new_status = "rejected" if ok else "rejection_failed"
        with _db_lock, db() as conn:
            conn.execute(
                "UPDATE withdraw_holds SET status=?, reviewed_at=?, reviewed_by=?, "
                "decision_reason=? WHERE withdraw_id=?",
                (new_status, now, op["id"], reason, wid),
            )
        log_action(
            op["id"],
            action="withdraw.reject",
            target_kind="withdraw",
            target_id=wid,
            reason=reason,
            metadata={"chain_response": cancel_resp, "ok": ok},
            ip=self._client_ip(),
        )
        if ok:
            push_notify(
                row["opex_user"],
                {
                    "kind": "withdraw.rejected",
                    "withdraw_id": wid,
                    "reason": reason,
                },
            )
        return self._send_json(
            200 if ok else 502,
            {
                "ok": ok,
                "withdraw_id": wid,
                "status": new_status,
                "chain_response": cancel_resp,
            },
        )

    # -- AML handlers ----------------------------------------------------
    def _h_internal_aml_open(self):
        if not self._client_is_loopback():
            return self._send_json(403, {"error": "loopback_only"})
        body = self._read_json()
        try:
            opex_user = str(body["opex_user"])
            trigger_kind = str(body["trigger_kind"])
        except KeyError as e:
            return self._send_json(400, {"error": "missing_field", "field": str(e)})
        with _db_lock, db() as conn:
            cur = conn.execute(
                "INSERT INTO aml_escalations (opex_user, trigger_kind, trigger_ref, "
                "decision_source, risk_score, reasons_json, status, opened_at, address) "
                "VALUES (?,?,?,?,?,?, 'open', ?, ?)",
                (
                    opex_user,
                    trigger_kind,
                    body.get("trigger_ref"),
                    body.get("decision_source"),
                    int(body.get("risk_score") or 0),
                    json.dumps(body.get("reasons") or []),
                    int(time.time()),
                    body.get("address"),
                ),
            )
        return self._send_json(200, {"ok": True, "id": int(cur.lastrowid)})

    def _h_aml_clear(self, eid: str):
        op = self._require_operator(permission="aml")
        if not op:
            return
        body = self._read_json()
        reason = (body.get("reason") or "").strip()
        if not reason:
            return self._send_json(400, {"error": "reason_required"})
        try:
            eid_i = int(eid)
        except ValueError:
            return self._send_json(400, {"error": "bad_id"})
        with _db_lock, db() as conn:
            row = conn.execute("SELECT * FROM aml_escalations WHERE id=?", (eid_i,)).fetchone()
            if not row:
                return self._send_json(404, {"error": "escalation_not_found"})
            if row["status"] != "open":
                return self._send_json(409, {"error": "not_open", "status": row["status"]})
            conn.execute(
                "UPDATE aml_escalations SET status='cleared', resolved_at=?, "
                "resolved_by=?, resolution_reason=? WHERE id=?",
                (int(time.time()), op["id"], reason, eid_i),
            )
        log_action(
            op["id"],
            action="aml.clear",
            target_kind="incident",
            target_id=str(eid_i),
            reason=reason,
            metadata={"trigger_kind": row["trigger_kind"], "address": row["address"]},
            ip=self._client_ip(),
        )
        return self._send_json(200, {"ok": True, "id": eid_i, "status": "cleared"})

    def _h_aml_block(self, eid: str):
        op = self._require_operator(permission="aml")
        if not op:
            return
        body = self._read_json()
        reason = (body.get("reason") or "").strip()
        if not reason:
            return self._send_json(400, {"error": "reason_required"})
        try:
            eid_i = int(eid)
        except ValueError:
            return self._send_json(400, {"error": "bad_id"})
        with _db_lock, db() as conn:
            row = conn.execute("SELECT * FROM aml_escalations WHERE id=?", (eid_i,)).fetchone()
            if not row:
                return self._send_json(404, {"error": "escalation_not_found"})
            if row["status"] != "open":
                return self._send_json(409, {"error": "not_open", "status": row["status"]})
            conn.execute(
                "UPDATE aml_escalations SET status='blocked_permanent', resolved_at=?, "
                "resolved_by=?, resolution_reason=? WHERE id=?",
                (int(time.time()), op["id"], reason, eid_i),
            )
            addr = row["address"]
            if addr:
                conn.execute(
                    "INSERT INTO aml_blocklist (address, added_at, added_by, reason) "
                    "VALUES (?,?,?,?) ON CONFLICT(address) DO NOTHING",
                    (addr.lower(), int(time.time()), op["id"], reason),
                )
        log_action(
            op["id"],
            action="aml.block",
            target_kind="incident",
            target_id=str(eid_i),
            reason=reason,
            metadata={"trigger_kind": row["trigger_kind"], "address": row["address"]},
            ip=self._client_ip(),
        )
        return self._send_json(200, {"ok": True, "id": eid_i, "status": "blocked_permanent"})

    # -- Incident handlers ----------------------------------------------
    def _h_incident_create(self):
        op = self._require_operator(permission="incident")
        if not op:
            return
        body = self._read_json()
        category = (body.get("category") or "").strip().lower()
        title = (body.get("title") or "").strip()
        severity = (body.get("severity") or "medium").strip().lower()
        description = body.get("description") or ""
        if category not in ("security", "liquidity", "compliance", "support"):
            return self._send_json(400, {"error": "bad_category"})
        if severity not in ("low", "medium", "high", "critical"):
            return self._send_json(400, {"error": "bad_severity"})
        if not title:
            return self._send_json(400, {"error": "title_required"})
        now = int(time.time())
        with _db_lock, db() as conn:
            cur = conn.execute(
                "INSERT INTO incidents (category, title, description, severity, "
                "status, opened_at, assigned_to) VALUES (?,?,?,?, 'open', ?, ?)",
                (category, title, description, severity, now, op["id"]),
            )
            iid = int(cur.lastrowid)
        log_action(
            op["id"],
            action="incident.create",
            target_kind="incident",
            target_id=str(iid),
            reason=title,
            metadata={"category": category, "severity": severity},
            ip=self._client_ip(),
        )
        return self._send_json(
            200,
            {
                "ok": True,
                "id": iid,
                "category": category,
                "title": title,
                "severity": severity,
                "status": "open",
                "opened_at": now,
            },
        )

    def _h_incident_update(self, iid: str):
        op = self._require_operator(permission="incident")
        if not op:
            return
        body = self._read_json()
        try:
            iid_i = int(iid)
        except ValueError:
            return self._send_json(400, {"error": "bad_id"})
        updates: list[tuple[str, object]] = []
        for f in ("status", "severity", "title", "description"):
            if f in body:
                updates.append((f, body[f]))
        if not updates:
            return self._send_json(400, {"error": "nothing_to_update"})
        with _db_lock, db() as conn:
            row = conn.execute("SELECT id FROM incidents WHERE id=?", (iid_i,)).fetchone()
            if not row:
                return self._send_json(404, {"error": "incident_not_found"})
            cols = ", ".join(f"{k}=?" for k, _ in updates)
            conn.execute(
                f"UPDATE incidents SET {cols} WHERE id=?",  # noqa: S608 - fields are whitelisted.
                (*[v for _, v in updates], iid_i),
            )
        log_action(
            op["id"],
            action="incident.update",
            target_kind="incident",
            target_id=str(iid_i),
            reason=body.get("reason"),
            metadata=dict(updates),
            ip=self._client_ip(),
        )
        return self._send_json(200, {"ok": True, "id": iid_i})

    def _h_incident_close(self, iid: str):
        op = self._require_operator(permission="incident")
        if not op:
            return
        try:
            iid_i = int(iid)
        except ValueError:
            return self._send_json(400, {"error": "bad_id"})
        body = self._read_json()
        reason = (body.get("reason") or "operator-closed").strip()
        now = int(time.time())
        with _db_lock, db() as conn:
            cur = conn.execute(
                "UPDATE incidents SET status='resolved', resolved_at=? "
                "WHERE id=? AND status!='resolved'",
                (now, iid_i),
            )
            if cur.rowcount == 0:
                return self._send_json(404, {"error": "incident_not_found_or_already_closed"})
        log_action(
            op["id"],
            action="incident.close",
            target_kind="incident",
            target_id=str(iid_i),
            reason=reason,
            metadata={},
            ip=self._client_ip(),
        )
        return self._send_json(200, {"ok": True, "id": iid_i, "status": "resolved"})

    # -- Overview --------------------------------------------------------
    def _overview(self) -> dict:
        cutoff = int(time.time()) - 86400
        out: dict = {}
        # KYC pending count from auth.db
        kyc_n = 0
        aconn = auth_db_ro()
        if aconn is not None:
            try:
                kyc_n = int(
                    aconn.execute(
                        "SELECT COUNT(*) FROM users WHERE kyc_status IN ('none','pending')"
                    ).fetchone()[0]
                )
            finally:
                aconn.close()
        with db() as conn:
            wd = int(
                conn.execute(
                    "SELECT COUNT(*) FROM withdraw_holds WHERE status='pending_review'"
                ).fetchone()[0]
            )
            aml = int(
                conn.execute("SELECT COUNT(*) FROM aml_escalations WHERE status='open'").fetchone()[
                    0
                ]
            )
            incs = int(
                conn.execute(
                    "SELECT COUNT(*) FROM incidents WHERE status NOT IN ('resolved')"
                ).fetchone()[0]
            )
            timeline = conn.execute(
                "SELECT a.ts, a.action, a.target_kind, a.target_id, "
                "       o.display_name FROM op_actions a "
                "LEFT JOIN operators o ON o.id=a.operator_id "
                "WHERE a.ts>=? ORDER BY a.ts DESC LIMIT 50",
                (cutoff,),
            ).fetchall()
        out["counts"] = {
            "kyc_pending": kyc_n,
            "withdraws_pending": wd,
            "aml_open": aml,
            "incidents_open": incs,
        }
        out["timeline_24h"] = [dict(r) for r in timeline]
        out["snapshot_at"] = int(time.time())
        return out


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5620
    init_db()
    log(f"db={DB_PATH}")
    log(f"homepage_ops_dir={HOMEPAGE_OPS_DIR}")
    log(f"auth_base={AUTH_BASE}")
    log(f"chain_base={CHAIN_BASE}")
    log(f"listening on :{port}")
    with ThreadingServer(("127.0.0.1", port), Handler) as srv:
        srv.serve_forever()


if __name__ == "__main__":
    main()
