#!/usr/bin/env python3
"""Web Application Firewall (WAF) + L7 DDoS protection for zkCEX.

Listens on :5499 and reverse-proxies validated traffic to the existing
serve_homepage.py proxy at :5500. Every rule layer can short-circuit with
a friendly HTTP response — nothing that fails a rule reaches :5500.

Architecture (rule order):
  1. TCP-level DDoS heuristics (per-IP conn rate, slowloris, manual block)
  2. Geo-blocking (uses providers/geo_provider.py)
  3. Per-(IP, endpoint-tier) token-bucket rate limiting
  4. Bot heuristics (UA patterns, cadence, JS-challenge cookie)
  5. Request-body inspection (SQLi / XSS / body-size cap)
  6. Forward to upstream :5500
  7. Response hardening (CSP / HSTS / X-Frame-Options / etc.)

The WAF is stdlib-only. State lives in tools/.local/waf.db. Hot path uses
in-memory hashmaps; we flush counters to SQLite every 60s.

This is the L7 origin shield. Real DDoS defense at L3/L4 (SYN floods, UDP
amplification, BGP-level Anycast scrubbing) is delegated upstream — see
deploy/security/WAF.md for the full threat-model and Cloudflare-front
migration guide.
"""

from __future__ import annotations

import collections
import hashlib
import http.client
import http.server
import json
import os
import socket
import socketserver
import sqlite3
import sys
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join(HERE, ".local")
os.makedirs(LOCAL_DIR, exist_ok=True)
DB_PATH = os.environ.get("WAF_DB_PATH", os.path.join(LOCAL_DIR, "waf.db"))

# Import the existing geo provider (sibling module).
sys.path.insert(0, HERE)
try:
    from providers import geo_provider  # type: ignore
except Exception as e:  # noqa: BLE001
    sys.stderr.write(f"[waf] geo_provider import failed: {e}; geo layer disabled\n")
    geo_provider = None  # type: ignore[assignment]


def _log(msg: str) -> None:
    sys.stderr.write(f"[waf] {msg}\n")


# ============================================================================
# Config
# ============================================================================
UPSTREAM_HOST = os.environ.get("WAF_UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("WAF_UPSTREAM_PORT", "5500"))
WAF_GEO_BLOCKLIST = [
    c.strip().upper()
    for c in (os.environ.get("WAF_GEO_BLOCKLIST") or "US,IR,KP,CU,SY,RU").split(",")
    if c.strip()
]
# Public read-only paths that bypass the geo block (so banned-country IPs can
# still browse markets and the marketing site).
GEO_PUBLIC_PATH_PREFIXES = (
    "/",  # marketing landing (exact match handled separately)
    "/en/",
    "/openapi.json",
    "/v3/exchangeInfo",
    "/v3/depth",
    "/v3/ticker/24h",
    "/app/",  # PWA static shell — the API calls inside still hit geo
    "/ops/",  # operator console assets (operators may be roaming)
    "/static/",
    "/assets/",
    "/favicon",
    "/robots.txt",
)

# Bot-y user-agent fragments — case-insensitive substring match.
BOT_UA_PATTERNS = (
    "curl/",
    "python-requests/",
    "go-http-client/",
    "apache-httpclient/",
    "axios/",
    "wget/",
    "libwww-perl",
)

# SQLi / XSS body inspection patterns (case-insensitive).
SQLI_PATTERNS = (
    "' or 1=1",
    "' or '1'='1",
    "; drop table",
    " drop table ",
    "union select",
    "union all select",
    "' or 1#",
    "' or 1--",
    "or 1=1--",
    "or 1=1#",
    "/*!",
    "xp_cmdshell",
    "into outfile",
)
XSS_PATTERNS = (
    "<script",
    "javascript:",
    "onerror=",
    "onload=",
    "onclick=",
    "<iframe",
    "<svg/onload",
    "document.cookie",
)

# Maximum request body sizes.
DEFAULT_BODY_MAX = 1 * 1024 * 1024  # 1 MiB
UPLOAD_BODY_MAX = 16 * 1024 * 1024  # 16 MiB for /v1/upload/*

# Per-IP TCP connection rate (rolling 1-second window).
CONN_RATE_PER_IP_PER_SEC = int(os.environ.get("WAF_CONN_RATE_PER_IP_PER_SEC", "50"))
SLOWLORIS_IDLE_TIMEOUT_S = 10.0

# JS challenge cookie.
CHALLENGE_COOKIE_NAME = "_waf_challenge"
CHALLENGE_COOKIE_TTL_S = 3600

# Bot cadence threshold: same path+body hash repeats.
BOT_REPEAT_WINDOW_S = 30.0
BOT_REPEAT_THRESHOLD = 100

# Token-bucket flush interval.
BUCKET_FLUSH_INTERVAL_S = 60.0


# ============================================================================
# Endpoint tiers / rate limit table
# ============================================================================
# Tier table: list of (matcher, tier_name, per_minute_limit).
# Matcher is a list of either ("prefix", path) or ("exact", path).
# Order matters — first match wins.
TIER_RULES: list[tuple[str, str, str, int]] = [
    # (kind, pattern, tier, per_minute_limit)
    ("exact", "/", "public", 600),
    ("exact", "/en/", "public", 600),
    ("exact", "/openapi.json", "public", 600),
    ("exact", "/v3/exchangeInfo", "public", 600),
    ("prefix", "/v3/depth", "public", 600),
    ("prefix", "/v3/ticker/24h", "public", 600),
    ("prefix", "/v3/trades", "public", 600),
    ("prefix", "/v3/klines", "public", 600),
    ("exact", "/auth/signup", "auth", 10),
    ("exact", "/auth/login", "auth", 10),
    ("exact", "/chain/withdraw", "withdraw", 5),
    ("exact", "/v3/withdraw", "withdraw", 5),
    ("exact", "/v3/order", "trade", 60),
    ("exact", "/fapi/v1/order", "trade", 60),
    ("prefix", "/orders/conditional", "trade", 60),
    ("exact", "/v3/account", "account", 120),
    ("exact", "/v3/myTrades", "account", 120),
    ("exact", "/v3/openOrders", "account", 120),
    ("prefix", "/v1/owner/", "account", 120),
    ("exact", "/chain/wallet", "account", 120),
    ("prefix", "/app/", "static", 200),
    ("prefix", "/ops/", "static", 200),
    ("prefix", "/static/", "static", 200),
    ("prefix", "/assets/", "static", 200),
]

DEFAULT_TIER = ("static", 200)


def classify_tier(path: str) -> tuple[str, int]:
    """Return (tier_name, per_minute_limit) for the given path."""
    bare = path.split("?", 1)[0]
    for kind, pattern, tier, limit in TIER_RULES:
        if kind == "exact" and bare == pattern:
            return tier, limit
        if kind == "prefix" and bare.startswith(pattern):
            return tier, limit
    return DEFAULT_TIER


# ============================================================================
# DB
# ============================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS waf_blocks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  ip TEXT NOT NULL,
  path TEXT NOT NULL,
  rule TEXT NOT NULL,
  severity TEXT NOT NULL,
  decision TEXT NOT NULL,
  user_agent TEXT,
  request_summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_waf_blocks_ts ON waf_blocks(ts);
CREATE INDEX IF NOT EXISTS idx_waf_blocks_ip ON waf_blocks(ip);

CREATE TABLE IF NOT EXISTS waf_manual_blocks (
  ip TEXT PRIMARY KEY,
  blocked_at INTEGER NOT NULL,
  blocked_until INTEGER,
  reason TEXT,
  blocked_by_operator TEXT
);

CREATE TABLE IF NOT EXISTS waf_token_buckets (
  bucket_key TEXT PRIMARY KEY,
  tokens REAL NOT NULL,
  last_refill_at INTEGER NOT NULL,
  refill_per_second REAL NOT NULL,
  capacity REAL NOT NULL
);
"""


_db_lock = threading.Lock()


def db_conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=5.0, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.row_factory = sqlite3.Row
    return c


def db_init() -> None:
    with _db_lock, db_conn() as c:
        c.executescript(SCHEMA)


# ============================================================================
# In-memory state
# ============================================================================
class _Counters:
    """Aggregate counters for stats endpoints. Reset on process restart."""

    def __init__(self) -> None:
        self.started_at = time.time()
        self.req_total = 0
        self.req_blocked = 0
        # Per-rule block counts.
        self.by_rule: dict[str, int] = collections.defaultdict(int)
        # Per-tier hit counts.
        self.by_tier: dict[str, int] = collections.defaultdict(int)
        # Rolling 1h list of (ts, ip, rule).
        self.recent_blocks: collections.deque = collections.deque(maxlen=4000)
        self.rate_limit_hits_last_hour: collections.deque = collections.deque(maxlen=4000)
        # Per-IP "is bot" flag — once flagged, stricter rate limit until reset.
        self.bot_flagged: set[str] = set()
        # Country-of-block tally.
        self.by_country: dict[str, int] = collections.defaultdict(int)

    def lock(self) -> threading.Lock:
        if not hasattr(self, "_l"):
            self._l = threading.Lock()  # noqa: SLF001
        return self._l


COUNTERS = _Counters()


# Token buckets: key="ip|tier" -> {tokens, last_refill, refill_rate, capacity}.
_buckets: dict[str, dict] = {}
_buckets_lock = threading.Lock()


def _bucket_take(
    ip: str, tier: str, per_minute_limit: int, bot_multiplier: float = 1.0
) -> tuple[bool, float]:
    """Take one token. Returns (allowed, retry_after_seconds)."""
    capacity = per_minute_limit / bot_multiplier
    refill = capacity / 60.0
    now = time.time()
    key = f"{ip}|{tier}"
    with _buckets_lock:
        b = _buckets.get(key)
        if b is None:
            b = {"tokens": capacity - 1.0, "last": now, "refill": refill, "capacity": capacity}
            _buckets[key] = b
            return True, 0.0
        elapsed = now - b["last"]
        b["tokens"] = min(b["capacity"], b["tokens"] + elapsed * b["refill"])
        b["last"] = now
        if b["tokens"] >= 1.0:
            b["tokens"] -= 1.0
            return True, 0.0
        # Not enough — how long until 1 token?
        needed = 1.0 - b["tokens"]
        retry = max(0.1, needed / max(b["refill"], 1e-9))
        return False, retry


# Per-IP connection-rate rolling window.
_conn_window: dict[str, collections.deque] = collections.defaultdict(
    lambda: collections.deque(maxlen=CONN_RATE_PER_IP_PER_SEC * 2 + 5)
)
_conn_window_lock = threading.Lock()


def _conn_rate_ok(ip: str) -> bool:
    """True if this IP is under the per-second connection cap."""
    now = time.time()
    with _conn_window_lock:
        dq = _conn_window[ip]
        # Drop entries older than 1s.
        while dq and (now - dq[0]) > 1.0:
            dq.popleft()
        if len(dq) >= CONN_RATE_PER_IP_PER_SEC:
            return False
        dq.append(now)
        return True


# Per-IP request fingerprint (path + body hash) for bot-cadence detection.
_req_window: dict[str, collections.deque] = collections.defaultdict(
    lambda: collections.deque(maxlen=BOT_REPEAT_THRESHOLD * 2 + 5)
)
_req_window_lock = threading.Lock()


def _is_bot_cadence(ip: str, path: str, body: bytes) -> bool:
    """True if this IP has issued BOT_REPEAT_THRESHOLD+ identical requests in 30s."""
    now = time.time()
    h = hashlib.blake2b(path.encode("utf-8", "replace") + b"|" + body, digest_size=8).hexdigest()
    with _req_window_lock:
        dq = _req_window[ip]
        # Drop old.
        while dq and (now - dq[0][0]) > BOT_REPEAT_WINDOW_S:
            dq.popleft()
        dq.append((now, h))
        # Count identical hashes in the current window.
        same = sum(1 for ts, hh in dq if hh == h)
        return same >= BOT_REPEAT_THRESHOLD


# Manual blocklist — loaded from DB on boot + on each /admin write.
_manual_blocks: dict[str, dict] = {}
_manual_blocks_lock = threading.Lock()


def _reload_manual_blocks() -> None:
    now = int(time.time())
    with _db_lock, db_conn() as c:
        rows = list(
            c.execute(
                "SELECT ip, blocked_at, blocked_until, reason, blocked_by_operator "
                "FROM waf_manual_blocks"
            )
        )
    fresh: dict[str, dict] = {}
    for r in rows:
        until = r["blocked_until"]
        if until is not None and until < now:
            continue
        fresh[r["ip"]] = dict(r)
    with _manual_blocks_lock:
        _manual_blocks.clear()
        _manual_blocks.update(fresh)


def _is_manually_blocked(ip: str) -> dict | None:
    now = int(time.time())
    with _manual_blocks_lock:
        rec = _manual_blocks.get(ip)
        if not rec:
            return None
        until = rec.get("blocked_until")
        if until is not None and until < now:
            _manual_blocks.pop(ip, None)
            return None
        return rec


# ============================================================================
# Block logging
# ============================================================================
def _redact_ip(ip: str, severity: str) -> str:
    """Redact IP unless severity is critical."""
    if severity == "critical":
        return ip
    if geo_provider is not None:
        try:
            return geo_provider.redact_ip(ip)
        except Exception as e:  # noqa: BLE001
            _log(f"geo redact failed: {e!r}")
    # Fallback redaction.
    if "." in ip:
        parts = ip.split(".")
        if len(parts) == 4:
            return ".".join(parts[:3]) + ".x"
    return "x.x.x.x"


def log_block(
    ip: str,
    path: str,
    rule: str,
    severity: str,
    decision: str,
    user_agent: str = "",
    request_summary: str = "",
) -> None:
    """Log a block to DB + bump in-memory counters."""
    redacted = _redact_ip(ip, severity)
    ts = int(time.time())
    try:
        with _db_lock, db_conn() as c:
            c.execute(
                "INSERT INTO waf_blocks "
                "(ts, ip, path, rule, severity, decision, user_agent, request_summary) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    ts,
                    redacted,
                    path[:240],
                    rule,
                    severity,
                    decision,
                    user_agent[:240] if user_agent else None,
                    request_summary[:240] if request_summary else None,
                ),
            )
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[waf] DB write failed: {e}\n")
    with COUNTERS.lock():
        COUNTERS.req_blocked += 1
        COUNTERS.by_rule[rule] += 1
        if decision == "block":
            COUNTERS.recent_blocks.append((ts, redacted, rule))
        if rule == "rate_limit":
            COUNTERS.rate_limit_hits_last_hour.append((ts, redacted))


# ============================================================================
# Response hardening
# ============================================================================
SECURITY_HEADERS = (
    (
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net "
        "https://static.sumsub.com; "
        "connect-src 'self' wss: ws:; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com",
    ),
    ("Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload"),
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ("Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=()"),
    ("Server", "zkCEX"),
)


# ============================================================================
# Admin token
# ============================================================================
def get_admin_token() -> str:
    tok = os.environ.get("WAF_ADMIN_TOKEN")
    if tok:
        return tok
    cached = _runtime_state.get("admin_token")
    if cached:
        return str(cached)
    import secrets

    tok = "waf_" + secrets.token_urlsafe(24)
    _runtime_state["admin_token"] = tok
    sys.stderr.write(f"[waf] WAF_ADMIN_TOKEN={tok}  (set in env for persistence)\n")
    return tok


_runtime_state: dict = {}


# ============================================================================
# Path classification helpers
# ============================================================================
def is_public_geo_path(path: str) -> bool:
    bare = path.split("?", 1)[0]
    if bare == "/":
        return True
    for pfx in GEO_PUBLIC_PATH_PREFIXES:
        if pfx == "/":
            continue
        if pfx.endswith("/"):
            if bare.startswith(pfx):
                return True
        else:
            if bare == pfx or bare.startswith(pfx):
                return True
    return False


def body_size_cap_for(path: str) -> int:
    bare = path.split("?", 1)[0]
    if bare.startswith("/v1/upload"):
        return UPLOAD_BODY_MAX
    return DEFAULT_BODY_MAX


# ============================================================================
# JS challenge
# ============================================================================
_CHALLENGE_HTML_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Just a moment</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font-family:system-ui;background:#0b1020;color:#e6ecff;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{text-align:center}.spin{width:36px;height:36px;border:3px solid #2a3258;
border-top-color:#7c9cff;border-radius:50%;animation:s 1s linear infinite;margin:0 auto 14px}
@keyframes s{to{transform:rotate(360deg)}}small{color:#9aa6c7}</style></head>
<body><div class="box"><div class="spin"></div>
<div>Verifying your browser...</div>
<small>This takes about a second. zkCEX security.</small></div>
<script>
(function(){
  // Computational challenge: find n such that sha1(n + nonce) starts with '00'.
  // Trivial proof-of-work; defeats the laziest bots.
  var nonce = "__NONCE__";
  var start = Date.now();
  function hex(buf){
    var arr = Array.prototype.slice.call(new Uint8Array(buf));
    return arr.map(function(b){return ('00'+b.toString(16)).slice(-2);}).join('');
  }
  async function solve(){
    var enc = new TextEncoder();
    for(var n=0; n<5000000; n++){
      var h = await crypto.subtle.digest('SHA-1', enc.encode(n + ':' + nonce));
      var hh = hex(h);
      if(hh.slice(0,2) === '00'){
        document.cookie = '__COOKIE__=' + nonce + '.' + n + '; Max-Age=__TTL__; Path=/; SameSite=Lax';
        var elapsed = Date.now() - start;
        setTimeout(function(){ window.location.reload(); }, Math.max(0, 600 - elapsed));
        return;
      }
    }
  }
  if (!window.crypto || !window.crypto.subtle){
    document.cookie = '__COOKIE__=plain.' + Date.now() + '; Max-Age=__TTL__; Path=/; SameSite=Lax';
    setTimeout(function(){ window.location.reload(); }, 600);
  } else {
    solve();
  }
})();
</script></body></html>
"""


def _challenge_page(nonce: str) -> bytes:
    out = (
        _CHALLENGE_HTML_TEMPLATE.replace("__NONCE__", nonce)
        .replace("__COOKIE__", CHALLENGE_COOKIE_NAME)
        .replace("__TTL__", str(CHALLENGE_COOKIE_TTL_S))
    )
    return out.encode("utf-8")


# ============================================================================
# Body inspection
# ============================================================================
def scan_body(body: bytes) -> tuple[str, str] | None:
    """Returns (rule, snippet) on detection, or None."""
    if not body:
        return None
    # Decode lossily for pattern matching. Cap to first 64KB; the rest is
    # almost certainly a file upload and we won't introspect it.
    sample = body[:65536].decode("utf-8", errors="replace").lower()
    sample = urllib.parse.unquote(sample)
    for p in SQLI_PATTERNS:
        if p in sample:
            return "sql_injection", p
    for p in XSS_PATTERNS:
        if p in sample:
            return "xss", p
    return None


# ============================================================================
# HTTP handler
# ============================================================================
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "zkCEX"
    sys_version = ""  # don't leak Python version
    timeout = SLOWLORIS_IDLE_TIMEOUT_S
    protocol_version = "HTTP/1.1"

    # We're a proxy — read the body manually after rule checks.
    def setup(self):
        super().setup()
        # Slowloris guard: cap how long we wait for the request line.
        try:
            self.request.settimeout(SLOWLORIS_IDLE_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            _log(f"socket timeout setup failed: {e!r}")

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"[waf] {self.address_string()} - {fmt % args}\n")

    # ------------------------------------------------------------------
    # Top-level dispatch — every method goes through _handle.
    # ------------------------------------------------------------------
    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_OPTIONS(self):
        self._handle("OPTIONS")

    def do_HEAD(self):
        self._handle("HEAD")

    # ------------------------------------------------------------------
    def _client_ip(self) -> str:
        peer = self.client_address[0] if self.client_address else ""
        xff = self.headers.get("X-Forwarded-For")
        xri = self.headers.get("X-Real-IP")
        if geo_provider is not None:
            try:
                return geo_provider.resolve_client_ip(
                    remote_addr=peer, x_forwarded_for=xff, x_real_ip=xri
                )
            except Exception as e:  # noqa: BLE001
                _log(f"geo client ip resolution failed: {e!r}")
        if xff:
            return xff.split(",")[0].strip()
        return peer

    def _send_simple(
        self,
        status: int,
        body: bytes,
        content_type: str = "application/json",
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in SECURITY_HEADERS:
            self.send_header(k, v)
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception as e:  # noqa: BLE001
            _log(f"response write failed: {e!r}")

    def _send_json(
        self, status: int, payload: dict, extra_headers: list[tuple[str, str]] | None = None
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send_simple(status, body, "application/json", extra_headers)

    def _send_block(
        self, status: int, rule: str, message: str, retry_after: int | None = None
    ) -> None:
        headers = []
        if retry_after is not None:
            headers.append(("Retry-After", str(retry_after)))
        self._send_json(
            status,
            {
                "error": rule,
                "message": message,
                "code": status,
                "ref": "zkcex-waf",
            },
            extra_headers=headers,
        )

    # ------------------------------------------------------------------
    # WAF-local admin / health endpoints — never forwarded.
    # ------------------------------------------------------------------
    def _maybe_handle_waf_endpoint(self, method: str, path: str, body: bytes) -> bool:
        """Return True if we handled the request locally."""
        bare = path.split("?", 1)[0]

        if bare == "/waf/health" and method == "GET":
            now = int(time.time())
            with COUNTERS.lock():
                blocked_last_hour = sum(
                    1 for ts, _ip, _r in COUNTERS.recent_blocks if (now - ts) <= 3600
                )
                rl_hits = sum(
                    1 for ts, _ip in COUNTERS.rate_limit_hits_last_hour if (now - ts) <= 3600
                )
            self._send_json(
                200,
                {
                    "ok": True,
                    "uptime_s": int(now - COUNTERS.started_at),
                    "n_rules_active": 6,
                    "blocked_last_hour": blocked_last_hour,
                    "rate_limit_hits_last_hour": rl_hits,
                    "upstream": f"{UPSTREAM_HOST}:{UPSTREAM_PORT}",
                    "geo_blocklist": WAF_GEO_BLOCKLIST,
                },
            )
            return True

        if bare == "/waf/stats" and method == "GET":
            if not self._require_admin():
                return True
            now = int(time.time())
            with COUNTERS.lock():
                by_rule = dict(COUNTERS.by_rule)
                by_tier = dict(COUNTERS.by_tier)
                by_country = dict(COUNTERS.by_country)
                recent = [(ts, ip, r) for ts, ip, r in COUNTERS.recent_blocks if (now - ts) <= 3600]
            # Top-10 attacker IPs by recent block count.
            ip_counts: dict[str, int] = collections.defaultdict(int)
            for _ts, ip, _r in recent:
                ip_counts[ip] += 1
            top_ips = sorted(ip_counts.items(), key=lambda kv: -kv[1])[:10]
            self._send_json(
                200,
                {
                    "uptime_s": int(now - COUNTERS.started_at),
                    "req_total": COUNTERS.req_total,
                    "req_blocked": COUNTERS.req_blocked,
                    "by_rule": by_rule,
                    "by_tier": by_tier,
                    "by_country": by_country,
                    "top_attacker_ips_last_hour": [{"ip": ip, "blocks": n} for ip, n in top_ips],
                },
            )
            return True

        if bare == "/waf/_admin/block-ip" and method == "POST":
            if not self._require_admin():
                return True
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except Exception:  # noqa: BLE001
                self._send_json(400, {"error": "bad_json"})
                return True
            ip = (payload.get("ip") or "").strip()
            dur = int(payload.get("duration_seconds") or 0)
            reason = (payload.get("reason") or "")[:240]
            if not ip:
                self._send_json(400, {"error": "missing_ip"})
                return True
            now = int(time.time())
            until = (now + dur) if dur > 0 else None
            with _db_lock, db_conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO waf_manual_blocks "
                    "(ip, blocked_at, blocked_until, reason, blocked_by_operator) "
                    "VALUES (?,?,?,?,?)",
                    (ip, now, until, reason, "admin"),
                )
            _reload_manual_blocks()
            self._send_json(200, {"ok": True, "ip": ip, "blocked_until": until, "reason": reason})
            return True

        if bare.startswith("/waf/_admin/block-ip/") and method == "DELETE":
            if not self._require_admin():
                return True
            ip = bare[len("/waf/_admin/block-ip/") :]
            if not ip:
                self._send_json(400, {"error": "missing_ip"})
                return True
            with _db_lock, db_conn() as c:
                c.execute("DELETE FROM waf_manual_blocks WHERE ip=?", (ip,))
            _reload_manual_blocks()
            self._send_json(200, {"ok": True, "ip": ip, "removed": True})
            return True

        if bare == "/waf/_admin/blocked" and method == "GET":
            if not self._require_admin():
                return True
            with _manual_blocks_lock:
                rows = [dict(v) for v in _manual_blocks.values()]
            self._send_json(200, {"blocks": rows})
            return True

        return False

    def _require_admin(self) -> bool:
        auth = self.headers.get("Authorization") or ""
        tok = ""
        if auth.lower().startswith("bearer "):
            tok = auth.split(None, 1)[1].strip()
        if not tok or tok != get_admin_token():
            self._send_json(
                401, {"error": "unauthorized", "message": "Bearer WAF_ADMIN_TOKEN required"}
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Main rule pipeline.
    # ------------------------------------------------------------------
    def _handle(self, method: str) -> None:
        with COUNTERS.lock():
            COUNTERS.req_total += 1
        path = self.path
        ip = self._client_ip()
        ua = self.headers.get("User-Agent") or ""

        # ---- Layer 1: TCP-level / manual blocklist ----
        manual = _is_manually_blocked(ip)
        if manual:
            log_block(
                ip, path, "manual_block", "high", "block", ua, f"reason={manual.get('reason','')}"
            )
            self._send_block(403, "manual_block", "Your IP has been blocked by an operator.")
            return
        if not _conn_rate_ok(ip):
            log_block(ip, path, "conn_rate", "medium", "block", ua, "conn_rate_per_ip exceeded")
            # Close the connection abruptly — no body.
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except Exception as e:  # noqa: BLE001
                _log(f"connection shutdown failed: {e!r}")
            return

        # Read body up front — we need it for tier classification, bot
        # cadence, and body inspection. Cap to the per-path limit.
        body_max = body_size_cap_for(path)
        clen_hdr = self.headers.get("Content-Length")
        body = b""
        try:
            clen = int(clen_hdr) if clen_hdr else 0
        except ValueError:
            clen = 0
        if clen > body_max:
            log_block(ip, path, "body_too_large", "low", "block", ua, f"content_length={clen}")
            self._send_block(413, "body_too_large", f"Request body exceeds {body_max} bytes.")
            return
        if clen > 0:
            try:
                body = self.rfile.read(clen)
            except Exception:  # noqa: BLE001
                self._send_block(400, "bad_request", "Body read failed.")
                return

        # ---- Local WAF endpoints (always before upstream) ----
        if self._maybe_handle_waf_endpoint(method, path, body):
            return

        # ---- Layer 2: Geo-block ----
        if geo_provider is not None and WAF_GEO_BLOCKLIST:
            try:
                info = geo_provider.lookup_country(ip)
            except Exception:  # noqa: BLE001
                info = None
            if info and info.country_iso2 in WAF_GEO_BLOCKLIST:
                if not is_public_geo_path(path):
                    with COUNTERS.lock():
                        COUNTERS.by_country[info.country_iso2] += 1
                    log_block(
                        ip, path, "geo", "medium", "block", ua, f"country={info.country_iso2}"
                    )
                    self._send_block(
                        451,
                        "geo_block",
                        f"Service unavailable in your region " f"({info.country_iso2}).",
                    )
                    return

        # ---- Layer 3: Rate limit (per IP+tier) ----
        tier, limit = classify_tier(path)
        with COUNTERS.lock():
            COUNTERS.by_tier[tier] += 1
        bot_multi = 2.0 if ip in COUNTERS.bot_flagged else 1.0
        allowed, retry = _bucket_take(ip, tier, limit, bot_multi)
        if not allowed:
            log_block(
                ip,
                path,
                "rate_limit",
                "low",
                "block",
                ua,
                f"tier={tier} limit={limit} retry={retry:.1f}",
            )
            self._send_block(
                429,
                "rate_limit",
                f"Too many requests on tier '{tier}'. " f"Try again in {int(retry)+1}s.",
                retry_after=int(retry) + 1,
            )
            return

        # ---- Layer 4: Bot heuristics ----
        ua_low = ua.lower()
        suspicious_ua = (not ua) or any(p in ua_low for p in BOT_UA_PATTERNS)
        # Cadence — only check on POST/PUT/DELETE since GETs with the same
        # path are extremely common (polling) and not by themselves bot-y.
        if method in ("POST", "PUT", "DELETE") and _is_bot_cadence(ip, path, body):
            COUNTERS.bot_flagged.add(ip)
            log_block(
                ip, path, "bot_cadence", "medium", "log_only", ua, "identical_request_repetition"
            )

        # JS challenge — only for HTML-y GETs where the client looks bot-y.
        # Don't challenge API requests (Accept: application/json) or static
        # asset paths or the WAF's own endpoints.
        bare = path.split("?", 1)[0]
        is_api_path = (
            bare.startswith("/v3/")
            or bare.startswith("/sapi/")
            or bare.startswith("/api/")
            or bare.startswith("/fapi/")
            or bare.startswith("/v1/")
            or bare.startswith("/auth/")
            or bare.startswith("/chain/")
            or bare.startswith("/orders/")
            or bare.startswith("/order/")
            or bare.startswith("/order")
            or bare.startswith("/ws/")
            or bare.startswith("/zk-trade/")
            or bare.startswith("/api-keys/")
            or bare.startswith("/mcp/")
            or bare.startswith("/push/")
            or bare.startswith("/mm/")
            or bare.startswith("/safu/")
            or bare.startswith("/travel-rule/")
            or bare.startswith("/ops-api/")
            or bare.startswith("/zkpol/")
            or bare.startswith("/pol/")
            or bare.startswith("/pol-")
            or bare.startswith("/bridge/")
            or bare.startswith("/export/")
            or bare.startswith("/withdraw/")
            or bare.startswith("/deposit/")
            or bare.startswith("/admin/")
            or bare.startswith("/internal/")
            or bare.startswith("/kyc/")
        )
        accept = (self.headers.get("Accept") or "").lower()
        wants_html = "text/html" in accept or accept == "" or "*/*" in accept
        has_lang = bool(self.headers.get("Accept-Language"))
        has_enc = bool(self.headers.get("Accept-Encoding"))
        has_ref = bool(self.headers.get("Referer"))
        cookie = self.headers.get("Cookie") or ""
        has_challenge = (CHALLENGE_COOKIE_NAME + "=") in cookie
        # Don't challenge the ops console or known asset paths; operators
        # auth via Bearer header and have no cookie-based session here.
        is_exempt_from_challenge = (
            bare.startswith("/ops/")
            or bare.startswith("/static/")
            or bare.startswith("/assets/")
            or bare == "/favicon.ico"
            or bare == "/robots.txt"
        )
        botty_for_challenge = (
            method == "GET"
            and wants_html
            and not is_api_path
            and not is_exempt_from_challenge
            and (not has_lang)
            and (not has_enc)
            and (not has_ref)
            and not has_challenge
        )
        if botty_for_challenge:
            import secrets

            nonce = secrets.token_hex(8)
            log_block(ip, path, "bot_challenge", "low", "challenge", ua, "served JS challenge")
            self._send_simple(200, _challenge_page(nonce), "text/html")
            return

        if suspicious_ua:
            # Don't block; just record. Many legit clients use these UAs.
            log_block(ip, path, "bot_ua", "low", "log_only", ua, "ua_pattern_matched")

        # ---- Layer 5: Body inspection ----
        if body:
            hit = scan_body(body)
            if hit is not None:
                rule, snippet = hit
                log_block(ip, path, rule, "high", "block", ua, f"matched={snippet}")
                self._send_block(403, rule, "Request blocked by content inspection.")
                return

        # ---- Forward to upstream ----
        self._forward(method, path, body)

    # ------------------------------------------------------------------
    # Forward to :5500 and wrap the response with security headers.
    # ------------------------------------------------------------------
    def _forward(self, method: str, path: str, body: bytes) -> None:
        try:
            conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=30)
        except Exception as e:  # noqa: BLE001
            self._send_block(502, "upstream_unavailable", f"Upstream unavailable: {e}")
            return

        # Build forwarded headers. Strip hop-by-hop headers; inject X-F-F.
        HOP_BY_HOP = {
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailers",
            "transfer-encoding",
            "upgrade",
        }
        headers_out: list[tuple[str, str]] = []
        for k in self.headers.keys():
            if k.lower() in HOP_BY_HOP:
                continue
            v = self.headers.get(k)
            if v is None:
                continue
            headers_out.append((k, v))
        # X-Forwarded-For: append our peer.
        peer = self.client_address[0] if self.client_address else ""
        existing_xff = self.headers.get("X-Forwarded-For")
        new_xff = f"{existing_xff}, {peer}" if existing_xff else peer
        # Drop the old X-F-F header before adding new.
        headers_out = [(k, v) for k, v in headers_out if k.lower() != "x-forwarded-for"]
        headers_out.append(("X-Forwarded-For", new_xff))
        headers_out.append(("X-Forwarded-Proto", "https"))
        headers_out.append(("Via", "1.1 zkcex-waf"))

        try:
            conn.request(method, path, body=body if body else None, headers=dict(headers_out))
            resp = conn.getresponse()
            resp_body = resp.read()
        except Exception as e:  # noqa: BLE001
            self._send_block(502, "upstream_error", f"Upstream error: {e}")
            try:
                conn.close()
            except Exception as close_error:  # noqa: BLE001
                _log(f"upstream close failed after error: {close_error!r}")
            return

        # Mirror upstream response with security headers layered on.
        self.send_response(resp.status)
        # Skip headers we'll set ourselves.
        SKIP = {
            "server",
            "content-length",
            "strict-transport-security",
            "content-security-policy",
            "x-frame-options",
            "x-content-type-options",
            "referrer-policy",
            "permissions-policy",
        }
        for k, v in resp.getheaders():
            if k.lower() in SKIP:
                continue
            if k.lower() in HOP_BY_HOP:
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(resp_body)))
        for k, v in SECURITY_HEADERS:
            self.send_header(k, v)
        self.end_headers()
        if method != "HEAD":
            try:
                self.wfile.write(resp_body)
            except Exception as e:  # noqa: BLE001
                _log(f"upstream response write failed: {e!r}")
        try:
            conn.close()
        except Exception as e:  # noqa: BLE001
            _log(f"upstream close failed: {e!r}")


# ============================================================================
# Threading server
# ============================================================================
class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _bucket_flush_loop() -> None:
    """Persist token-bucket snapshots to DB every 60s."""
    while True:
        time.sleep(BUCKET_FLUSH_INTERVAL_S)
        try:
            with _buckets_lock:
                snap = list(_buckets.items())
            now = int(time.time())
            with _db_lock, db_conn() as c:
                for key, b in snap:
                    c.execute(
                        "INSERT OR REPLACE INTO waf_token_buckets "
                        "(bucket_key, tokens, last_refill_at, "
                        " refill_per_second, capacity) VALUES (?,?,?,?,?)",
                        (key, float(b["tokens"]), now, float(b["refill"]), float(b["capacity"])),
                    )
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[waf] bucket flush failed: {e}\n")


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5499
    db_init()
    _reload_manual_blocks()
    # Warm geo cache.
    if geo_provider is not None:
        try:
            geo_provider.lookup_country("8.8.8.8")
        except Exception as e:  # noqa: BLE001
            _log(f"geo warmup failed: {e!r}")
    # Make sure the admin token is available at startup.
    get_admin_token()
    # Background flush.
    t = threading.Thread(target=_bucket_flush_loop, daemon=True)
    t.start()
    with ThreadingServer(("", port), Handler) as srv:
        sys.stderr.write(
            f"[waf] listening on :{port} -> {UPSTREAM_HOST}:{UPSTREAM_PORT} "
            f"(blocklist={','.join(WAF_GEO_BLOCKLIST) or '<empty>'})\n"
        )
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
