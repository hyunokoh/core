#!/usr/bin/env python3
"""Sentry-style error collector for zkCEX (port 5690).

Standalone stdlib HTTP service that receives JS errors from the browser
and Python exceptions from internal services, deduplicates them by
fingerprint, and exposes an admin API + operator dashboard.

State at ``tools/.local/errors.db``. Source maps live encrypted at rest
using the same envelope as ``tools/backup/`` (AES-256-CTR + HMAC-SHA256
over the 32-byte key in ``tools/.local/backup_encryption.key``).

Endpoints
---------
  Public (rate limited):
    POST /errors/report                  -- browser JS + manual error report
  Admin (Bearer ERROR_ADMIN_TOKEN):
    GET  /errors/events?...              -- list events with filters
    GET  /errors/aggregates?...          -- Sentry-style grouped view
    GET  /errors/event/<id>              -- full single event detail
    POST /errors/aggregates/<fp>/resolve -- mark resolved
    POST /errors/aggregates/<fp>/mute    -- mute
    POST /errors/aggregates/<fp>/note    -- attach a free-text note
    POST /errors/source-maps             -- multipart source-map upload
    GET  /errors/stats                   -- rolled-up counters
  Loopback only:
    POST /errors/internal/python-error   -- Python services post traceback here

Constraints
-----------
- Stdlib only, single-process, sqlite for state.
- Browser endpoint accepts cross-origin POSTs (CORS *).
- Token-bucket rate limit on /errors/report: 30 req/min/IP, 1000 req/hour/user.
- Session tokens in the report payload are redacted to first 8 chars
  before storage. No raw PII is persisted.
- Loopback endpoint refuses anything that isn't 127.0.0.1/::1.
"""

from __future__ import annotations

import gzip
import hashlib
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
import traceback
import urllib.parse

# Encryption for source maps: reuse the backup envelope so they share the
# same operational key custody story.
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    from backup import crypto as backup_crypto  # type: ignore
    from backup.common import MASTER_KEY_PATH  # type: ignore

    _CRYPTO_AVAILABLE = True
except Exception:
    _CRYPTO_AVAILABLE = False
    backup_crypto = None  # type: ignore
    MASTER_KEY_PATH = os.path.join(HERE, ".local", "backup_encryption.key")


# ==========================================================================
# Paths / config
# ==========================================================================
LOCAL_DIR = os.path.join(HERE, ".local")
os.makedirs(LOCAL_DIR, exist_ok=True)
DB_PATH = os.path.join(LOCAL_DIR, "errors.db")

ERROR_RETENTION_DAYS = int(os.environ.get("ERROR_RETENTION_DAYS", "30"))
RATE_LIMIT_PER_MIN = int(os.environ.get("ERROR_RATE_LIMIT_PER_MIN", "30"))
RATE_LIMIT_PER_HOUR_USER = int(os.environ.get("ERROR_RATE_LIMIT_PER_HOUR_USER", "1000"))
MAX_BODY_BYTES = int(os.environ.get("ERROR_MAX_BODY_BYTES", "256000"))  # 256 KiB
MAX_STACK_CHARS = 16000
MAX_MESSAGE_CHARS = 4000
MAX_URL_CHARS = 1024
MAX_UA_CHARS = 512

START_TS = int(time.time())
_runtime_state: dict[str, object] = {}
_db_lock = threading.Lock()


def log(msg: str) -> None:
    sys.stderr.write(f"[errors] {msg}\n")
    sys.stderr.flush()


# ==========================================================================
# DB
# ==========================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS error_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  source TEXT NOT NULL,
  level TEXT NOT NULL,
  message TEXT NOT NULL,
  exception_type TEXT,
  stack_trace TEXT,
  url TEXT,
  user_id TEXT,
  session_token_redacted TEXT,
  user_agent TEXT,
  fingerprint TEXT NOT NULL,
  metadata_json TEXT,
  release_version TEXT,
  environment TEXT
);
CREATE INDEX IF NOT EXISTS idx_error_events_ts ON error_events(ts);
CREATE INDEX IF NOT EXISTS idx_error_events_fingerprint ON error_events(fingerprint);
CREATE INDEX IF NOT EXISTS idx_error_events_source ON error_events(source);

CREATE TABLE IF NOT EXISTS error_aggregates (
  fingerprint TEXT PRIMARY KEY,
  first_seen INTEGER NOT NULL,
  last_seen INTEGER NOT NULL,
  count INTEGER NOT NULL DEFAULT 1,
  sample_event_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  resolved_by TEXT,
  resolved_at INTEGER,
  notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_error_aggregates_status ON error_aggregates(status);
CREATE INDEX IF NOT EXISTS idx_error_aggregates_last_seen ON error_aggregates(last_seen);

CREATE TABLE IF NOT EXISTS source_map_uploads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  release_version TEXT NOT NULL,
  source_path TEXT NOT NULL,
  map_content_gz BLOB NOT NULL,
  uploaded_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_maps_release ON source_map_uploads(release_version);
"""


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)


# ==========================================================================
# Admin token
# ==========================================================================
def get_admin_token() -> str:
    tok = os.environ.get("ERROR_ADMIN_TOKEN")
    if tok:
        return tok
    cached = _runtime_state.get("admin_token")
    if cached:
        return str(cached)
    tok = "errcol_" + secrets.token_urlsafe(24)
    _runtime_state["admin_token"] = tok
    log(f"ERROR_ADMIN_TOKEN={tok}  (set in env to make it persistent)")
    return tok


# ==========================================================================
# Encryption helpers (source maps only)
# ==========================================================================
def _master_key() -> bytes | None:
    if not _CRYPTO_AVAILABLE:
        return None
    cached = _runtime_state.get("master_key")
    if cached:
        return cached  # type: ignore[return-value]
    try:
        k = backup_crypto.load_or_create_master_key(MASTER_KEY_PATH)
        _runtime_state["master_key"] = k
        return k
    except Exception as e:
        log(f"warn: could not load master key for source map encryption: {e}")
        return None


def encrypt_blob(plaintext: bytes) -> bytes:
    """Encrypt a payload using the backup envelope, or return a sentinel
    plaintext-marker prefix if crypto is unavailable (demo fallback)."""
    k = _master_key()
    if k is None:
        # Mark as plaintext so decrypt can round-trip in the absence of a key.
        return b"PLAIN1::" + plaintext
    return backup_crypto.encrypt(k, plaintext)  # type: ignore[union-attr]


def decrypt_blob(blob: bytes) -> bytes:
    if blob.startswith(b"PLAIN1::"):
        return blob[len(b"PLAIN1::") :]
    k = _master_key()
    if k is None:
        raise ValueError("master key unavailable")
    return backup_crypto.decrypt(k, blob)  # type: ignore[union-attr]


# ==========================================================================
# Fingerprinting + redaction
# ==========================================================================
_STACK_FRAME_RE = re.compile(r"^[ \t]*(?:at\s+)?(.+?)(?:\s*\n|$)", re.MULTILINE)


def first_stack_frame(stack: str | None) -> str:
    """Pick the first meaningful frame from a JS or Python stack trace."""
    if not stack:
        return ""
    # Python traceback puts the bottom-most frame line as
    # 'File "x.py", line N, in func'. JS has 'at fn (file:line:col)'.
    for line in stack.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(("Traceback", "The above exception")):
            continue
        return line[:300]
    return ""


def compute_fingerprint(
    source: str, exception_type: str | None, stack_trace: str | None, message: str
) -> str:
    """SHA256(source + exception_type + first stack frame).

    Falls back to the message when there's no usable stack frame so events
    without a stack still cluster sanely.
    """
    frame = first_stack_frame(stack_trace)
    if not frame:
        frame = (message or "")[:300]
    seed = f"{source}|{exception_type or ''}|{frame}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def redact_token(tok: str | None) -> str | None:
    if not tok:
        return None
    t = str(tok)
    return t[:8] + "..." if len(t) > 8 else t


def _clip(s: str | None, n: int) -> str | None:
    if s is None:
        return None
    s = str(s)
    if len(s) <= n:
        return s
    return s[:n] + "...[truncated]"


# ==========================================================================
# Token-bucket rate limiter
# ==========================================================================
class RateLimiter:
    """Two-tier limiter: per-IP/minute and per-user/hour.

    Token bucket math is computed lazily on each check; we keep one bucket
    per key with (tokens, last_refill_ts). Capacity equals the budget.
    """

    def __init__(self, ip_per_min: int, user_per_hour: int):
        self.ip_capacity = ip_per_min
        self.ip_refill_per_s = ip_per_min / 60.0
        self.user_capacity = user_per_hour
        self.user_refill_per_s = user_per_hour / 3600.0
        self.lock = threading.Lock()
        self.ip_buckets: dict[str, list[float]] = {}
        self.user_buckets: dict[str, list[float]] = {}

    def _consume(
        self, buckets: dict[str, list[float]], key: str, capacity: float, refill: float
    ) -> bool:
        now = time.time()
        b = buckets.get(key)
        if b is None:
            buckets[key] = [capacity - 1.0, now]
            return True
        tokens, last = b
        tokens = min(capacity, tokens + (now - last) * refill)
        if tokens < 1.0:
            b[0] = tokens
            b[1] = now
            return False
        b[0] = tokens - 1.0
        b[1] = now
        return True

    def allow(self, ip: str, user_id: str | None) -> tuple[bool, str]:
        with self.lock:
            if not self._consume(self.ip_buckets, ip, self.ip_capacity, self.ip_refill_per_s):
                return False, "ip_rate_limit"
            if user_id:
                if not self._consume(
                    self.user_buckets, user_id, self.user_capacity, self.user_refill_per_s
                ):
                    return False, "user_rate_limit"
            return True, ""

    def gc(self, older_than_s: float = 3600.0) -> int:
        """Drop buckets that haven't been touched recently."""
        now = time.time()
        removed = 0
        with self.lock:
            for d in (self.ip_buckets, self.user_buckets):
                stale = [k for k, b in d.items() if now - b[1] > older_than_s]
                for k in stale:
                    d.pop(k, None)
                    removed += 1
        return removed


_limiter = RateLimiter(RATE_LIMIT_PER_MIN, RATE_LIMIT_PER_HOUR_USER)


# ==========================================================================
# Core ingest
# ==========================================================================
def ingest_event(payload: dict) -> dict:
    """Store one error event + UPSERT its aggregate.

    Returns ``{event_id, fingerprint}`` on success. Caller has already
    validated the payload shape and applied rate-limiting.
    """
    source = str(payload.get("source") or "manual")[:128]
    level = str(payload.get("level") or "error")[:32]
    if level not in ("error", "warning", "info", "debug"):
        level = "error"
    message = _clip(payload.get("message"), MAX_MESSAGE_CHARS) or "(no message)"
    exception_type = _clip(payload.get("exception_type"), 256)
    stack_trace = _clip(payload.get("stack_trace"), MAX_STACK_CHARS)
    url = _clip(payload.get("url"), MAX_URL_CHARS)
    user_id = _clip(payload.get("user_id"), 128)
    ua = _clip(payload.get("user_agent"), MAX_UA_CHARS)
    rv = _clip(payload.get("release_version"), 128)
    env = _clip(payload.get("environment"), 32)
    md = payload.get("metadata")
    if isinstance(md, (dict, list)):
        try:
            metadata_json = json.dumps(md)[:8000]
        except Exception:
            metadata_json = "{}"
    else:
        metadata_json = None

    raw_session = payload.get("session_token")
    redacted = redact_token(raw_session) if raw_session else None

    fp = compute_fingerprint(source, exception_type, stack_trace, message)
    now = int(time.time())

    with _db_lock, db() as conn:
        cur = conn.execute(
            "INSERT INTO error_events("
            "  ts, source, level, message, exception_type, stack_trace, url,"
            "  user_id, session_token_redacted, user_agent, fingerprint,"
            "  metadata_json, release_version, environment) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                now,
                source,
                level,
                message,
                exception_type,
                stack_trace,
                url,
                user_id,
                redacted,
                ua,
                fp,
                metadata_json,
                rv,
                env,
            ),
        )
        event_id = cur.lastrowid
        # UPSERT aggregate
        row = conn.execute(
            "SELECT fingerprint FROM error_aggregates WHERE fingerprint=?",
            (fp,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE error_aggregates SET last_seen=?, count=count+1 " "WHERE fingerprint=?",
                (now, fp),
            )
        else:
            conn.execute(
                "INSERT INTO error_aggregates("
                "  fingerprint, first_seen, last_seen, count, sample_event_id) "
                "VALUES(?,?,?,?,?)",
                (fp, now, now, 1, event_id),
            )
    return {"event_id": event_id, "fingerprint": fp}


# ==========================================================================
# Background retention
# ==========================================================================
def _retention_loop():
    interval = int(os.environ.get("ERROR_RETENTION_INTERVAL_S", "300"))
    while True:
        try:
            prune_old(ERROR_RETENTION_DAYS)
            _limiter.gc()
        except Exception:
            log("retention error:\n" + traceback.format_exc())
        time.sleep(interval)


def prune_old(retention_days: int) -> dict:
    """Delete events older than ``retention_days`` and archive resolved
    aggregates with no recent activity."""
    cutoff = int(time.time()) - retention_days * 86400
    aggregate_archive_cutoff = int(time.time()) - 7 * 86400
    with _db_lock, db() as conn:
        cur = conn.execute("DELETE FROM error_events WHERE ts<?", (cutoff,))
        deleted_events = cur.rowcount or 0
        cur = conn.execute(
            "DELETE FROM error_aggregates " "WHERE status='resolved' AND last_seen<?",
            (aggregate_archive_cutoff,),
        )
        archived_aggregates = cur.rowcount or 0
    return {"deleted_events": deleted_events, "archived_aggregates": archived_aggregates}


# ==========================================================================
# HTTP helpers
# ==========================================================================
def _is_loopback(addr: str) -> bool:
    return addr in ("127.0.0.1", "::1", "localhost") or addr.startswith("127.")


def _client_ip(handler: http.server.BaseHTTPRequestHandler) -> str:
    """Extract the client IP. Trust X-Forwarded-For only when the connection
    itself comes from loopback (i.e. the local serve_homepage proxy)."""
    peer = handler.client_address[0] if handler.client_address else "127.0.0.1"
    xff = handler.headers.get("X-Forwarded-For") or handler.headers.get("X-Real-IP")
    if xff and _is_loopback(peer):
        return xff.split(",", 1)[0].strip() or peer
    return peer


# ==========================================================================
# HTTP handler
# ==========================================================================
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "zkcex-errors/1.0"

    # ---- shared ----
    def _send_json(self, status: int, payload):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _bearer(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        if not auth.lower().startswith("bearer "):
            return None
        return auth.split(None, 1)[1].strip()

    def _require_admin(self) -> bool:
        tok = self._bearer()
        if not tok or tok != get_admin_token():
            self._send_json(
                401, {"error": "unauthorized", "message": "Bearer ERROR_ADMIN_TOKEN required"}
            )
            return False
        return True

    def _read_json(self) -> dict | None:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > MAX_BODY_BYTES:
            self._send_json(413, {"error": "body_too_large"})
            return None
        raw = self.rfile.read(n)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                self._send_json(400, {"error": "expected_object"})
                return None
            return data
        except Exception:
            self._send_json(400, {"error": "invalid_json"})
            return None

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[errors] {self.address_string()} - {fmt % args}\n")

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    # ---- routing ----
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query or "")
        if path in ("/errors/health", "/health"):
            return self._send_json(200, {"ok": True, "uptime_s": int(time.time()) - START_TS})
        if path == "/errors/events":
            return self.h_events(q)
        if path == "/errors/aggregates":
            return self.h_aggregates(q)
        if path.startswith("/errors/event/"):
            tail = path[len("/errors/event/") :]
            try:
                eid = int(tail)
            except ValueError:
                return self._send_json(400, {"error": "bad_event_id"})
            return self.h_event_detail(eid)
        if path == "/errors/stats":
            return self.h_stats()
        if path.startswith("/errors/source-maps/"):
            return self.h_source_map_get(path)
        return self._send_json(404, {"error": "not_found"})

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/errors/report":
            return self.h_report()
        if path == "/errors/internal/python-error":
            return self.h_python_error()
        if path == "/errors/source-maps":
            return self.h_source_map_upload()
        m = re.match(r"^/errors/aggregates/([0-9a-fA-F]{8,64})/(resolve|mute|note)$", path)
        if m:
            return self.h_aggregate_action(m.group(1), m.group(2))
        return self._send_json(404, {"error": "not_found"})

    # ---- ingest endpoints ----
    def h_report(self):
        ip = _client_ip(self)
        data = self._read_json()
        if data is None:
            return
        user_id = data.get("user_id") or None
        ok, reason = _limiter.allow(ip, user_id)
        if not ok:
            return self._send_json(429, {"error": "rate_limited", "reason": reason})
        # Basic shape checks
        if not data.get("message"):
            return self._send_json(400, {"error": "missing_message"})
        # Force source to a safe value if obviously bogus
        src = str(data.get("source") or "browser-js")
        if src not in ("browser-js", "manual") and not src.startswith("python-service:"):
            src = "manual"
        data["source"] = src
        try:
            result = ingest_event(data)
        except Exception:
            log("ingest error:\n" + traceback.format_exc())
            return self._send_json(500, {"error": "ingest_failed"})
        return self._send_json(200, result)

    def h_python_error(self):
        # Loopback-only entrypoint for Python services.
        peer = self.client_address[0] if self.client_address else ""
        if not _is_loopback(peer):
            return self._send_json(404, {"error": "not_found"})
        data = self._read_json()
        if data is None:
            return
        src = str(data.get("source") or "python-service:unknown")
        if not src.startswith("python-service:"):
            src = "python-service:" + src
        data["source"] = src
        if not data.get("message"):
            data["message"] = (data.get("exception_type") or "PythonError") + ": (no message)"
        try:
            result = ingest_event(data)
        except Exception:
            log("python ingest error:\n" + traceback.format_exc())
            return self._send_json(500, {"error": "ingest_failed"})
        return self._send_json(200, result)

    # ---- admin reads ----
    def h_events(self, q: dict):
        if not self._require_admin():
            return
        try:
            limit = min(int((q.get("limit") or ["100"])[0]), 1000)
        except ValueError:
            limit = 100
        clauses = []
        params: list = []
        if q.get("source"):
            clauses.append("source=?")
            params.append(q["source"][0])
        if q.get("fingerprint"):
            clauses.append("fingerprint=?")
            params.append(q["fingerprint"][0])
        if q.get("level"):
            clauses.append("level=?")
            params.append(q["level"][0])
        if q.get("since_ts"):
            try:
                clauses.append("ts>=?")
                params.append(int(q["since_ts"][0]))
            except ValueError:
                pass
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with db() as conn:
            rows = conn.execute(
                "SELECT id, ts, source, level, message, exception_type,"
                "       url, user_id, fingerprint, release_version, environment "
                "FROM error_events" + where + " ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        return self._send_json(200, {"events": [dict(r) for r in rows]})

    def h_aggregates(self, q: dict):
        if not self._require_admin():
            return
        try:
            limit = min(int((q.get("limit") or ["50"])[0]), 500)
        except ValueError:
            limit = 50
        clauses = []
        params: list = []
        if q.get("status"):
            clauses.append("a.status=?")
            params.append(q["status"][0])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with db() as conn:
            rows = conn.execute(
                "SELECT a.fingerprint, a.first_seen, a.last_seen, a.count,"
                "       a.sample_event_id, a.status, a.resolved_by, a.resolved_at,"
                "       a.notes, e.source, e.level, e.message, e.exception_type,"
                "       e.url, e.stack_trace, e.environment, e.release_version "
                "FROM error_aggregates a "
                "LEFT JOIN error_events e ON e.id=a.sample_event_id"
                + where
                + " ORDER BY a.last_seen DESC LIMIT ?",
                params,
            ).fetchall()
        return self._send_json(200, {"aggregates": [dict(r) for r in rows]})

    def h_event_detail(self, event_id: int):
        if not self._require_admin():
            return
        with db() as conn:
            row = conn.execute("SELECT * FROM error_events WHERE id=?", (event_id,)).fetchone()
        if not row:
            return self._send_json(404, {"error": "not_found"})
        return self._send_json(200, {"event": dict(row)})

    def h_stats(self):
        if not self._require_admin():
            return
        now = int(time.time())
        cutoff_24h = now - 86400
        cutoff_1h = now - 3600
        with db() as conn:
            total_events = conn.execute("SELECT COUNT(*) FROM error_events").fetchone()[0]
            total_aggregates = conn.execute("SELECT COUNT(*) FROM error_aggregates").fetchone()[0]
            open_aggregates = conn.execute(
                "SELECT COUNT(*) FROM error_aggregates WHERE status='open'"
            ).fetchone()[0]
            events_24h = conn.execute(
                "SELECT COUNT(*) FROM error_events WHERE ts>=?",
                (cutoff_24h,),
            ).fetchone()[0]
            events_1h = conn.execute(
                "SELECT COUNT(*) FROM error_events WHERE ts>=?",
                (cutoff_1h,),
            ).fetchone()[0]
            top = conn.execute(
                "SELECT a.fingerprint, a.count, a.last_seen, e.message, e.source "
                "FROM error_aggregates a "
                "LEFT JOIN error_events e ON e.id=a.sample_event_id "
                "ORDER BY a.count DESC LIMIT 5"
            ).fetchall()
        return self._send_json(
            200,
            {
                "total_events": total_events,
                "total_aggregates": total_aggregates,
                "open_aggregates": open_aggregates,
                "events_last_24h": events_24h,
                "events_last_1h": events_1h,
                "top_5_fingerprints": [dict(r) for r in top],
                "uptime_s": now - START_TS,
                "retention_days": ERROR_RETENTION_DAYS,
            },
        )

    # ---- admin mutations ----
    def h_aggregate_action(self, fingerprint: str, action: str):
        if not self._require_admin():
            return
        data = self._read_json() or {}
        now = int(time.time())
        with _db_lock, db() as conn:
            row = conn.execute(
                "SELECT fingerprint FROM error_aggregates WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
            if not row:
                return self._send_json(404, {"error": "aggregate_not_found"})
            if action == "resolve":
                conn.execute(
                    "UPDATE error_aggregates SET status='resolved',"
                    " resolved_by=?, resolved_at=? WHERE fingerprint=?",
                    ("admin", now, fingerprint),
                )
            elif action == "mute":
                conn.execute(
                    "UPDATE error_aggregates SET status='muted' WHERE fingerprint=?",
                    (fingerprint,),
                )
            elif action == "note":
                note = _clip(str(data.get("note") or ""), 4000)
                conn.execute(
                    "UPDATE error_aggregates SET notes=? WHERE fingerprint=?",
                    (note, fingerprint),
                )
            else:
                return self._send_json(400, {"error": "unknown_action"})
        return self._send_json(200, {"ok": True, "fingerprint": fingerprint, "action": action})

    # ---- source maps ----
    def h_source_map_upload(self):
        if not self._require_admin():
            return
        ctype = self.headers.get("Content-Type", "")
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 8 * 1024 * 1024:
            return self._send_json(413, {"error": "bad_size"})
        body = self.rfile.read(n)
        release = None
        source_path = None
        map_bytes: bytes | None = None
        if ctype.startswith("application/json"):
            try:
                obj = json.loads(body.decode("utf-8"))
            except Exception:
                return self._send_json(400, {"error": "invalid_json"})
            release = obj.get("release_version")
            source_path = obj.get("source_path")
            content = obj.get("map_content")
            if content is None:
                return self._send_json(400, {"error": "missing_map_content"})
            if isinstance(content, str):
                map_bytes = content.encode("utf-8")
            else:
                map_bytes = json.dumps(content).encode("utf-8")
        elif ctype.startswith("multipart/form-data"):
            boundary = self._extract_boundary(ctype)
            if not boundary:
                return self._send_json(400, {"error": "no_boundary"})
            parts = self._split_multipart(body, boundary)
            for name, _headers, value in parts:
                if name == "release_version":
                    release = value.decode("utf-8", "replace").strip()
                elif name == "source_path":
                    source_path = value.decode("utf-8", "replace").strip()
                elif name == "map":
                    map_bytes = value
        else:
            return self._send_json(415, {"error": "unsupported_content_type"})
        if not release or not source_path or not map_bytes:
            return self._send_json(400, {"error": "missing_fields"})
        gz = gzip.compress(map_bytes)
        enc = encrypt_blob(gz)
        now = int(time.time())
        with _db_lock, db() as conn:
            cur = conn.execute(
                "INSERT INTO source_map_uploads("
                "  release_version, source_path, map_content_gz, uploaded_at) "
                "VALUES(?,?,?,?)",
                (release, source_path, enc, now),
            )
            sm_id = cur.lastrowid
        return self._send_json(
            200,
            {
                "id": sm_id,
                "release_version": release,
                "source_path": source_path,
                "stored_bytes": len(enc),
            },
        )

    def h_source_map_get(self, path: str):
        if not self._require_admin():
            return
        tail = path[len("/errors/source-maps/") :]
        try:
            sm_id = int(tail)
        except ValueError:
            return self._send_json(400, {"error": "bad_id"})
        with db() as conn:
            row = conn.execute(
                "SELECT release_version, source_path, map_content_gz, uploaded_at "
                "FROM source_map_uploads WHERE id=?",
                (sm_id,),
            ).fetchone()
        if not row:
            return self._send_json(404, {"error": "not_found"})
        try:
            gz = decrypt_blob(row["map_content_gz"])
            raw = gzip.decompress(gz)
        except Exception:
            return self._send_json(500, {"error": "decrypt_failed"})
        return self._send_json(
            200,
            {
                "release_version": row["release_version"],
                "source_path": row["source_path"],
                "uploaded_at": row["uploaded_at"],
                "map": raw.decode("utf-8", "replace"),
            },
        )

    @staticmethod
    def _extract_boundary(ctype: str) -> str | None:
        for piece in ctype.split(";"):
            piece = piece.strip()
            if piece.lower().startswith("boundary="):
                b = piece.split("=", 1)[1].strip().strip('"')
                return b
        return None

    @staticmethod
    def _split_multipart(body: bytes, boundary: str):
        """Yield (name, headers, value_bytes) for each part of an
        ``multipart/form-data`` body. Minimal: handles Content-Disposition's
        name= attribute only."""
        delim = ("--" + boundary).encode()
        ("--" + boundary + "--").encode()
        # Split into parts; ignore the preamble before the first delim and
        # everything after the closing delimiter.
        chunks = body.split(delim)
        for chunk in chunks:
            if not chunk or chunk.strip(b"\r\n-") == b"":
                continue
            if chunk.startswith(b"--"):
                # closing delim
                continue
            # Strip leading CRLF after the delim
            if chunk.startswith(b"\r\n"):
                chunk = chunk[2:]
            # Find header/body separator
            sep = chunk.find(b"\r\n\r\n")
            if sep == -1:
                continue
            hdr_block = chunk[:sep].decode("utf-8", "replace")
            value = chunk[sep + 4 :]
            # Trim trailing CRLF that precedes the next boundary
            if value.endswith(b"\r\n"):
                value = value[:-2]
            headers = {}
            name = ""
            for line in hdr_block.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            disp = headers.get("content-disposition", "")
            m = re.search(r'name="([^"]+)"', disp)
            if m:
                name = m.group(1)
            yield (name, headers, value)


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5690
    init_db()
    get_admin_token()
    t = threading.Thread(target=_retention_loop, daemon=True)
    t.start()
    log(f"db={DB_PATH}")
    log(f"retention_days={ERROR_RETENTION_DAYS}")
    log(f"rate_limit ip/min={RATE_LIMIT_PER_MIN} user/hour={RATE_LIMIT_PER_HOUR_USER}")
    log(f"listening on :{port}")
    with ThreadingServer(("", port), Handler) as srv:
        srv.serve_forever()


if __name__ == "__main__":
    main()
