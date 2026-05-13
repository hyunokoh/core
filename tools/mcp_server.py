#!/usr/bin/env python3
"""zkCEX Model Context Protocol (MCP) server.

Exposes the zkCEX REST surface as MCP tools so AI agents (Claude Desktop,
Cursor, OpenAI's Agent SDK, any MCP-compatible client) can place orders,
query balances, and inspect markets through the standard MCP tool-calling
interface.

Wire format: JSON-RPC 2.0. Two transports are supported:

  * ``stdio``  — line-delimited JSON-RPC on stdin/stdout. This is what
    Claude Desktop / Cursor will use when the user pastes the canonical
    config snippet from /app/mcp.html into their config file.

  * ``http``   — POST / accepts a JSON-RPC envelope and returns one
    synchronously. GET /stream is an SSE channel for any future
    server-initiated notifications (currently emits only a heartbeat).
    GET /health is unauthenticated and returns ``{"ok":true,...}``.
    GET /calls returns the recent JSON-RPC call log (Bearer-required).

The server itself only talks to the public REST surface of zkCEX:

  * ``/v3/...``       — Binance-compatible market and trading endpoints
                        (``http://127.0.0.1:8094`` by default).
  * ``/auth/me``      — bearer-token -> opex_user resolution
                        (``http://127.0.0.1:5501``).
  * ``/chain/...``    — non-custodial chain info / balance / history
                        (``http://127.0.0.1:5502``).
  * ``/pol/...``      — Proof-of-Liabilities certificate + per-account
                        Merkle proofs (``http://127.0.0.1:5503``).
  * ``/export/...``   — CSV exports (``http://127.0.0.1:5540``).

It does *not* import or touch ``auth_server`` / ``chain_server`` directly;
sibling agents are rewriting those modules concurrently.

Auth modes
----------

  * ``none``     — default for stdio. The server trusts the local user and
                   uses ``MCP_OPEX_USER`` (preferred) or
                   ``MCP_API_KEY`` + ``MCP_API_SECRET`` from the env to
                   identify which zkCEX user we're acting as.

  * ``bearer``   — HTTP only. Each request must carry
                   ``Authorization: Bearer <token>``; the token is
                   resolved against ``/auth/me``. Scopes default to
                   ``["read","trade"]`` for a logged-in human session.

  * ``api-key``  — HTTP only. Requires both ``X-MCP-API-KEY`` and
                   ``X-MCP-API-SECRET`` headers. The API key resolves to
                   an opex_user via ``/auth/api-key/lookup``; if that
                   route is not deployed yet (sibling agent) we fall back
                   to using the api-key string itself as the opex_user
                   for upstream calls (the matching-gateway accepts
                   ``X-Opex-User`` directly).

All trading tools that require the ``trade`` scope, and on-chain withdraw
tools that require the ``withdraw`` scope, are still listed in
``tools/list`` even when the caller's identity doesn't grant them — but
calling them returns a helpful JSON-RPC error pointing to the keys page
instead of letting the upstream 403 propagate.

Pure stdlib — no ``mcp`` SDK, no pip dependencies.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.server
import io
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
import uuid
from collections.abc import Callable
from typing import Any

SERVER_NAME = "zkcex-mcp"
SERVER_VERSION = "0.1.0"
# The MCP spec version we implement. Older clients will ignore unknown
# capability fields and continue working.
MCP_PROTOCOL_VERSION = "2024-11-05"

# Upstream zkCEX services. Override via env if these ever move.


def _validated_http_url(raw_url: str, *, name: str = "url") -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return raw_url


def _validated_http_base_url(name: str, raw_url: str) -> str:
    return _validated_http_url(raw_url, name=name).rstrip("/")


def _url_request(url: str, **kwargs) -> urllib.request.Request:
    return urllib.request.Request(_validated_http_url(url), **kwargs)  # noqa: S310


def _urlopen(target, **kwargs):
    if isinstance(target, str):
        target = _validated_http_url(target)
    return urllib.request.urlopen(target, **kwargs)  # noqa: S310


def _log(msg: str) -> None:
    sys.stderr.write(f"[mcp] {msg}\n")


API_BASE = _validated_http_base_url(
    "MCP_API_BASE", os.environ.get("MCP_API_BASE", "http://127.0.0.1:8094")
)
AUTH_BASE = _validated_http_base_url(
    "MCP_AUTH_BASE", os.environ.get("MCP_AUTH_BASE", "http://127.0.0.1:5501")
)
CHAIN_BASE = _validated_http_base_url(
    "MCP_CHAIN_BASE", os.environ.get("MCP_CHAIN_BASE", "http://127.0.0.1:5502")
)
POL_BASE = _validated_http_base_url(
    "MCP_POL_BASE", os.environ.get("MCP_POL_BASE", "http://127.0.0.1:5503")
)
ANCHOR_BASE = _validated_http_base_url(
    "MCP_ANCHOR_BASE", os.environ.get("MCP_ANCHOR_BASE", "http://127.0.0.1:5707")
)
SAFU_BASE = _validated_http_base_url(
    "MCP_SAFU_BASE", os.environ.get("MCP_SAFU_BASE", "http://127.0.0.1:5601")
)
EXPORT_BASE = _validated_http_base_url(
    "MCP_EXPORT_BASE", os.environ.get("MCP_EXPORT_BASE", "http://127.0.0.1:5540")
)
# perp_engine.py — USDT-margined perpetual futures. Same auth flow as /v3/*
# (X-Opex-User direct in trusted local mode, or HMAC-signed via /fapi/*).
PERP_BASE = _validated_http_base_url(
    "MCP_PERP_BASE", os.environ.get("MCP_PERP_BASE", "http://127.0.0.1:5590")
)
# zk_orderbook.py — commit-reveal FBA privacy trading.
ZK_BASE = _validated_http_base_url(
    "MCP_ZK_BASE", os.environ.get("MCP_ZK_BASE", "http://127.0.0.1:5660")
)
# notification_center.py — in-app inbox + email queue + price alerts.
NOTIF_BASE = _validated_http_base_url(
    "MCP_NOTIF_BASE", os.environ.get("MCP_NOTIF_BASE", "http://127.0.0.1:5691")
)
# referral.py — per-user codes + lifetime fee-share rewards.
REFERRAL_BASE = _validated_http_base_url(
    "MCP_REFERRAL_BASE", os.environ.get("MCP_REFERRAL_BASE", "http://127.0.0.1:5695")
)
# lending.py — earn / loan pool product (supply, borrow, liquidate).
LENDING_BASE = _validated_http_base_url(
    "MCP_LENDING_BASE", os.environ.get("MCP_LENDING_BASE", "http://127.0.0.1:5693")
)

# Where the MCP call log lives. The directory is created lazily.
LOG_DB_PATH = os.environ.get(
    "MCP_LOG_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".local", "mcp_calls.db"),
)
_LOG_LOCK = threading.Lock()

# ============================================================================
# Tiny helpers
# ============================================================================


def _now_ms() -> int:
    return int(time.time() * 1000)


def _http_request(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    body: bytes | None = None,
    timeout: float = 12.0,
) -> tuple[int, dict, bytes]:
    """Low-level HTTP. Returns (status, response_headers, body_bytes).
    Both 2xx and HTTPError responses come back here so the caller can
    inspect upstream error JSON instead of raising. Network failures still
    raise OSError / URLError so the caller can wrap them in a JSON-RPC
    -32603 (internal error) response.
    """
    req = _url_request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        if v is None:
            continue
        req.add_header(k, v)
    try:
        with _urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as e:
        try:
            buf = e.read()
        except Exception as read_error:  # noqa: BLE001
            _log(f"http error body read failed: {read_error!r}")
            buf = b""
        return e.code, dict((e.headers or {}).items()), buf


def _http_json(
    method: str, url: str, *, headers: dict | None = None, body: Any = None, timeout: float = 12.0
) -> tuple[int, Any]:
    h = dict(headers or {})
    raw: bytes | None = None
    if body is not None:
        raw = json.dumps(body).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    h.setdefault("Accept", "application/json")
    status, _, data = _http_request(method, url, headers=h, body=raw, timeout=timeout)
    parsed: Any = None
    if data:
        try:
            parsed = json.loads(data.decode("utf-8"))
        except Exception:
            parsed = data.decode("utf-8", "replace")
    return status, parsed


def _http_form(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    form: dict | None = None,
    timeout: float = 12.0,
) -> tuple[int, Any]:
    """POST / DELETE with x-www-form-urlencoded body — what the
    Binance-compatible /v3 endpoints expect for signed mutating calls."""
    h = dict(headers or {})
    raw: bytes | None = None
    if form is not None:
        raw = urllib.parse.urlencode({k: v for k, v in form.items() if v is not None}).encode(
            "utf-8"
        )
        h.setdefault("Content-Type", "application/x-www-form-urlencoded")
    h.setdefault("Accept", "application/json")
    status, _, data = _http_request(method, url, headers=h, body=raw, timeout=timeout)
    parsed: Any = None
    if data:
        try:
            parsed = json.loads(data.decode("utf-8"))
        except Exception:
            parsed = data.decode("utf-8", "replace")
    return status, parsed


def _redact(obj: Any) -> Any:
    """Strip likely-secret keys from a structure before logging it. Operates
    recursively on dicts/lists. Anything that smells like a key, secret,
    token, password, or signature gets replaced with the literal string
    ``"<redacted>"``."""
    SECRET_HINTS = (
        "secret",
        "password",
        "token",
        "apikey",
        "api_key",
        "x-mbx-apikey",
        "authorization",
        "signature",
        "private",
    )
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(h in str(k).lower() for h in SECRET_HINTS):
                out[k] = "<redacted>"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


# ============================================================================
# Symbol normalization. zkCEX uses underscored slugs like ``ETH_USDT`` for
# its non-Binance services and the unsuffixed ``ETHUSDT`` form for /v3/*.
# Agents typically know the Binance-compatible form, so that's what our
# tool schemas accept.
# ============================================================================

_QUOTE_HINTS = ("USDT", "BUSD", "USDC", "IRT", "BTC", "ETH")


def split_symbol(symbol: str) -> tuple[str, str]:
    s = (symbol or "").upper().replace("-", "")
    if "_" in s:
        a, b = s.split("_", 1)
        return a, b
    for q in _QUOTE_HINTS:
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)], q
    return s, "USDT"


def to_compact(symbol: str) -> str:
    """ETH_USDT / ETH-USDT / ethusdt -> ETHUSDT (Binance-compat form)."""
    base, quote = split_symbol(symbol)
    return f"{base}{quote}"


def to_underscored(symbol: str) -> str:
    base, quote = split_symbol(symbol)
    return f"{base}_{quote}"


# ============================================================================
# Call log — append-only sqlite, written under tools/.local/mcp_calls.db.
# Logging never blocks tool execution: schema setup and inserts are
# guarded by a coarse lock and any IO error is swallowed.
# ============================================================================

_LOG_INITED = False


def _log_init() -> None:
    global _LOG_INITED
    if _LOG_INITED:
        return
    try:
        os.makedirs(os.path.dirname(LOG_DB_PATH), exist_ok=True)
    except Exception:
        return
    try:
        with sqlite3.connect(LOG_DB_PATH) as cx:
            cx.execute("""
                CREATE TABLE IF NOT EXISTS mcp_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    transport TEXT NOT NULL,
                    client_label TEXT,
                    method TEXT NOT NULL,
                    tool TEXT,
                    opex_user TEXT,
                    request_json TEXT,
                    response_status TEXT,
                    error_message TEXT,
                    duration_ms INTEGER
                )
            """)
            cx.execute("CREATE INDEX IF NOT EXISTS mcp_calls_ts ON mcp_calls(ts DESC)")
            cx.commit()
        _LOG_INITED = True
    except Exception as e:  # noqa: BLE001
        _log(f"call log init skipped: {e!r}")


def log_call(
    *,
    transport: str,
    client_label: str | None,
    method: str,
    tool: str | None,
    opex_user: str | None,
    request: Any,
    status: str,
    error: str | None,
    duration_ms: int,
) -> None:
    _log_init()
    try:
        redacted = json.dumps(_redact(request), ensure_ascii=False)[:8000]
    except Exception:
        redacted = "<unserializable>"
    with _LOG_LOCK:
        try:
            with sqlite3.connect(LOG_DB_PATH) as cx:
                cx.execute(
                    "INSERT INTO mcp_calls (ts, transport, client_label, method, tool, "
                    "opex_user, request_json, response_status, error_message, duration_ms) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        _now_ms(),
                        transport,
                        client_label,
                        method,
                        tool,
                        opex_user,
                        redacted,
                        status,
                        error,
                        duration_ms,
                    ),
                )
                cx.commit()
        except Exception as e:  # noqa: BLE001
            _log(f"call log write skipped: {e!r}")


def recent_calls(limit: int = 50) -> list[dict]:
    _log_init()
    try:
        with sqlite3.connect(LOG_DB_PATH) as cx:
            cx.row_factory = sqlite3.Row
            rows = cx.execute(
                "SELECT * FROM mcp_calls ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


# ============================================================================
# Identity resolution — turn the per-transport credentials into an
# Identity object that downstream tool handlers can sign zkCEX requests
# with.
# ============================================================================


class Identity:
    __slots__ = ("opex_user", "scopes", "auth_kind", "bearer", "api_key", "api_secret")

    def __init__(
        self,
        *,
        opex_user: str | None = None,
        scopes: list[str] | None = None,
        auth_kind: str = "none",
        bearer: str | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
    ) -> None:
        self.opex_user = opex_user
        # scopes default depends on auth_kind: an authenticated bearer
        # session gets ['read','trade']; an api-key gets only what the
        # key was issued with (we can't know without the lookup endpoint,
        # so default to read+trade and let upstream 403 if revoked).
        if scopes is None:
            scopes = ["read", "trade"] if opex_user else []
        self.scopes = scopes
        self.auth_kind = auth_kind
        self.bearer = bearer
        self.api_key = api_key
        self.api_secret = api_secret

    def has_scope(self, want: str) -> bool:
        return want in self.scopes

    def upstream_headers(self) -> dict:
        """Headers to forward to zkCEX upstream services on this identity's
        behalf. The matching-gateway / market service accept either a
        ``X-Opex-User`` direct identity header (trusted internal) or a
        ``X-MBX-APIKEY`` + HMAC signature. We pass both when available."""
        h: dict = {}
        if self.opex_user:
            h["X-Opex-User"] = self.opex_user
        if self.bearer:
            h["Authorization"] = f"Bearer {self.bearer}"
        if self.api_key:
            h["X-MBX-APIKEY"] = self.api_key
        return h

    def __repr__(self) -> str:
        return f"Identity(opex={self.opex_user!r}, scopes={self.scopes}, " f"auth={self.auth_kind})"


# Process-wide cache of bearer -> Identity. Bounded TTL avoids slamming
# /auth/me on every tool call.
_BEARER_CACHE: dict[str, tuple[float, Identity]] = {}
_BEARER_CACHE_LOCK = threading.Lock()
_BEARER_TTL = 60.0


def resolve_bearer(token: str) -> Identity | None:
    if not token:
        return None
    now = time.time()
    with _BEARER_CACHE_LOCK:
        cached = _BEARER_CACHE.get(token)
        if cached and now - cached[0] < _BEARER_TTL:
            return cached[1]
    try:
        status, body = _http_json(
            "GET", f"{AUTH_BASE}/auth/me", headers={"Authorization": f"Bearer {token}"}, timeout=5.0
        )
    except Exception:
        return None
    if status != 200 or not isinstance(body, dict):
        return None
    user = body.get("user") or {}
    opex = user.get("opex_user")
    if not opex:
        return None
    scopes = user.get("scopes") or ["read", "trade"]
    ident = Identity(opex_user=opex, scopes=list(scopes), auth_kind="bearer", bearer=token)
    with _BEARER_CACHE_LOCK:
        _BEARER_CACHE[token] = (now, ident)
    return ident


def resolve_api_key(api_key: str, api_secret: str) -> Identity | None:
    """Resolve a zkCEX API key/secret pair to an Identity.

    The auth_server may eventually expose ``GET /auth/api-key/lookup``
    that returns the bound opex_user + scopes; if not, we fall back to
    using the api_key string itself as the opex_user (the matching-gateway
    will reject if it's not registered)."""
    if not api_key:
        return None
    try:
        status, body = _http_json(
            "GET",
            f"{AUTH_BASE}/auth/api-key/lookup?api_key={urllib.parse.quote(api_key)}",
            timeout=5.0,
        )
    except Exception:
        body = None
        status = 0
    if status == 200 and isinstance(body, dict) and body.get("opex_user"):
        return Identity(
            opex_user=body["opex_user"],
            scopes=list(body.get("scopes") or ["read", "trade"]),
            auth_kind="api-key",
            api_key=api_key,
            api_secret=api_secret,
        )
    # Fallback: use api_key as opex_user. This is what the demo currently
    # accepts because the bc-gateway / matching-gateway trust X-Opex-User
    # directly on localhost.
    return Identity(
        opex_user=api_key,
        scopes=["read", "trade"],
        auth_kind="api-key",
        api_key=api_key,
        api_secret=api_secret,
    )


def env_identity() -> Identity:
    """Resolve the identity for stdio mode (or HTTP --auth none) from the
    process env. Order: MCP_OPEX_USER -> MCP_API_KEY+MCP_API_SECRET -> none."""
    if os.environ.get("MCP_OPEX_USER"):
        return Identity(
            opex_user=os.environ["MCP_OPEX_USER"], scopes=["read", "trade"], auth_kind="env"
        )
    ak = os.environ.get("MCP_API_KEY")
    asec = os.environ.get("MCP_API_SECRET")
    if ak:
        return resolve_api_key(ak, asec or "") or Identity(auth_kind="none")
    return Identity(auth_kind="none")


# ============================================================================
# /v3 signed call helper. The bc-gateway accepts any of:
#    * ``X-Opex-User`` direct (internal-trusted)
#    * ``X-MBX-APIKEY`` + HMAC-SHA256(secret, querystring) on the qs
# We use the simpler X-Opex-User form when available; if only an api-key
# is set we sign the request the way Binance does (HMAC-SHA256 over the
# canonical query string, appended as ``&signature=...``).
# ============================================================================


def _sign_qs(qs: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), qs.encode("utf-8"), hashlib.sha256).hexdigest()


def _v3_signed_get(
    ident: Identity, path: str, params: dict | None = None, *, timeout: float = 8.0
) -> tuple[int, Any]:
    params = dict(params or {})
    params.setdefault("timestamp", _now_ms())
    params.setdefault("recvWindow", 60_000)
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    headers = ident.upstream_headers()
    if ident.api_key and ident.api_secret and "X-Opex-User" not in headers:
        sig = _sign_qs(qs, ident.api_secret)
        qs = f"{qs}&signature={sig}"
    url = f"{API_BASE}{path}?{qs}"
    return _http_json("GET", url, headers=headers, timeout=timeout)


def _v3_signed_post(
    ident: Identity,
    path: str,
    form: dict | None = None,
    *,
    method: str = "POST",
    timeout: float = 8.0,
) -> tuple[int, Any]:
    form = dict(form or {})
    form.setdefault("timestamp", _now_ms())
    form.setdefault("recvWindow", 60_000)
    headers = ident.upstream_headers()
    if ident.api_key and ident.api_secret and "X-Opex-User" not in headers:
        qs = urllib.parse.urlencode({k: v for k, v in form.items() if v is not None})
        form["signature"] = _sign_qs(qs, ident.api_secret)
    url = f"{API_BASE}{path}"
    return _http_form(method, url, headers=headers, form=form, timeout=timeout)


def _v3_public_get(
    path: str, params: dict | None = None, *, timeout: float = 6.0
) -> tuple[int, Any]:
    qs = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = f"{API_BASE}{path}" + (("?" + qs) if qs else "")
    return _http_json("GET", url, timeout=timeout)


# ============================================================================
# /fapi (perpetual futures) call helpers. The perp_engine is a sibling
# service on :5590; it accepts the same X-Opex-User trust shortcut as the
# matching-gateway and the same X-MBX-APIKEY + HMAC scheme as /v3. We use
# the X-Opex-User form when available because it's simpler.
# ============================================================================


def _perp_public_get(
    path: str, params: dict | None = None, *, timeout: float = 6.0
) -> tuple[int, Any]:
    qs = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = f"{PERP_BASE}{path}" + (("?" + qs) if qs else "")
    return _http_json("GET", url, timeout=timeout)


def _perp_signed_get(
    ident: Identity, path: str, params: dict | None = None, *, timeout: float = 8.0
) -> tuple[int, Any]:
    params = dict(params or {})
    headers = ident.upstream_headers()
    if "X-Opex-User" not in headers:
        # Fall back to HMAC the same way /v3 does. perp_engine doesn't yet
        # enforce HMAC itself, but the proxy layer at :5500 does — so we
        # only need to sign if we're going *through* the proxy. The MCP
        # server talks straight to :5590, which trusts X-Opex-User. If
        # neither is set, the call will be rejected with 401.
        if ident.api_key and ident.api_secret:
            params.setdefault("timestamp", _now_ms())
            params.setdefault("recvWindow", 60_000)
            qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            params["signature"] = _sign_qs(qs, ident.api_secret)
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{PERP_BASE}{path}" + (("?" + qs) if qs else "")
    return _http_json("GET", url, headers=headers, timeout=timeout)


def _perp_signed_post(
    ident: Identity,
    path: str,
    form: dict | None = None,
    *,
    method: str = "POST",
    timeout: float = 8.0,
) -> tuple[int, Any]:
    form = dict(form or {})
    headers = ident.upstream_headers()
    if ident.api_key and ident.api_secret and "X-Opex-User" not in headers:
        form.setdefault("timestamp", _now_ms())
        form.setdefault("recvWindow", 60_000)
        qs = urllib.parse.urlencode({k: v for k, v in form.items() if v is not None})
        form["signature"] = _sign_qs(qs, ident.api_secret)
    url = f"{PERP_BASE}{path}"
    return _http_form(method, url, headers=headers, form=form, timeout=timeout)


def _normalize_perp_symbol(s: str) -> str:
    """Accepts ETHUSDT, ETHUSDT_PERP, ETH_USDT, etc; returns the canonical
    ``BASEQUOTE_PERP`` form the perp_engine uses."""
    if not s:
        return s
    up = s.upper().strip()
    if up.endswith("_PERP"):
        return up
    # Strip an optional underscore between base and quote.
    if "_" in up:
        base, _, quote = up.partition("_")
        return f"{base}{quote}_PERP"
    return f"{up}_PERP"


# ============================================================================
# Tool definitions. Each entry is (schema_dict, handler). The handler takes
# ``(ident: Identity, args: dict)`` and returns either a JSON-serialisable
# payload or raises ToolError.
# ============================================================================


class ToolError(Exception):
    """Raised by tool handlers to surface a structured JSON-RPC error.

    code: integer JSON-RPC error code. Custom range -32001..-32099 used
    here per the MCP convention for application-specific errors.
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _require_scope(ident: Identity, scope: str, tool: str) -> None:
    if not ident.opex_user:
        raise ToolError(
            -32002,
            "Not authenticated. Set MCP_API_KEY + MCP_API_SECRET (or use "
            "--auth bearer with a session token from /auth/login).",
            data={"tool": tool, "required_scope": scope},
        )
    if not ident.has_scope(scope):
        raise ToolError(
            -32001,
            (
                f"Tool '{tool}' requires scope '{scope}'. Your API key has "
                f"scopes: {ident.scopes}. Open the keys page in zkCEX to "
                f"request a new key with {scope} scope."
            ),
            data={"tool": tool, "required_scope": scope, "have_scopes": ident.scopes},
        )


# ----- Public market-data tools (no scope required) --------------------------


def t_list_markets(ident: Identity, args: dict) -> Any:
    status, body = _v3_public_get("/v3/exchangeInfo")
    if status != 200 or not isinstance(body, dict):
        raise ToolError(-32603, f"upstream exchangeInfo failed: HTTP {status}", data=body)
    out = []
    for s in body.get("symbols") or []:
        out.append(
            {
                "symbol": s.get("symbol"),
                "base": s.get("baseAsset"),
                "quote": s.get("quoteAsset"),
                "status": s.get("status"),
                "permissions": s.get("permissions") or [],
            }
        )
    return out


def t_get_ticker(ident: Identity, args: dict) -> Any:
    sym = to_compact(args.get("symbol", ""))
    if not sym:
        raise ToolError(-32602, "symbol is required")
    status, body = _v3_public_get("/v3/ticker/24h", {"symbol": sym})
    # Some Binance-compat backends return a single-element list when called
    # with ``?symbol=...``; unwrap it to the expected dict shape.
    if status == 200 and isinstance(body, list) and len(body) == 1 and isinstance(body[0], dict):
        body = body[0]
    if status != 200 or not isinstance(body, dict):
        raise ToolError(-32603, f"upstream ticker failed: HTTP {status}", data=body)
    return {
        "symbol": body.get("symbol"),
        "last": body.get("lastPrice"),
        "change_24h": body.get("priceChange"),
        "change_24h_percent": body.get("priceChangePercent"),
        "high": body.get("highPrice"),
        "low": body.get("lowPrice"),
        "volume": body.get("volume"),
        "quote_volume": body.get("quoteVolume"),
        "open_time": body.get("openTime"),
        "close_time": body.get("closeTime"),
    }


def t_get_orderbook(ident: Identity, args: dict) -> Any:
    sym = to_compact(args.get("symbol", ""))
    limit = int(args.get("limit") or 50)
    if not sym:
        raise ToolError(-32602, "symbol is required")
    status, body = _v3_public_get("/v3/depth", {"symbol": sym, "limit": limit})
    if status != 200 or not isinstance(body, dict):
        raise ToolError(-32603, f"upstream depth failed: HTTP {status}", data=body)
    return {
        "symbol": sym,
        "bids": body.get("bids") or [],
        "asks": body.get("asks") or [],
        "last_update_id": body.get("lastUpdateId"),
    }


def t_get_recent_trades(ident: Identity, args: dict) -> Any:
    sym = to_compact(args.get("symbol", ""))
    limit = int(args.get("limit") or 50)
    if not sym:
        raise ToolError(-32602, "symbol is required")
    status, body = _v3_public_get("/v3/trades", {"symbol": sym, "limit": limit})
    if status != 200 or not isinstance(body, list):
        raise ToolError(-32603, f"upstream trades failed: HTTP {status}", data=body)
    out = []
    for r in body:
        out.append(
            {
                "id": r.get("id"),
                "price": r.get("price"),
                "qty": r.get("qty"),
                "quote_qty": r.get("quoteQty"),
                "time": r.get("time"),
                "is_buyer_maker": r.get("isBuyerMaker"),
            }
        )
    return out


def t_get_klines(ident: Identity, args: dict) -> Any:
    sym = to_compact(args.get("symbol", ""))
    interval = args.get("interval") or "1m"
    limit = int(args.get("limit") or 100)
    if not sym:
        raise ToolError(-32602, "symbol is required")
    status, body = _v3_public_get(
        "/v3/klines", {"symbol": sym, "interval": interval, "limit": limit}
    )
    if status != 200 or not isinstance(body, list):
        raise ToolError(-32603, f"upstream klines failed: HTTP {status}", data=body)
    out = []
    for row in body:
        # Binance kline tuple shape:
        # [openTime, open, high, low, close, volume, closeTime, qVol, ...]
        if not isinstance(row, list) or len(row) < 6:
            continue
        out.append(
            {
                "open_time": row[0],
                "open": row[1],
                "high": row[2],
                "low": row[3],
                "close": row[4],
                "volume": row[5],
                "close_time": row[6] if len(row) > 6 else None,
            }
        )
    return out


# ----- Account read tools (require 'read') ----------------------------------


def t_get_balance(ident: Identity, args: dict) -> Any:
    _require_scope(ident, "read", "get_balance")
    status, body = _v3_signed_get(ident, "/v3/account")
    if status != 200 or not isinstance(body, dict):
        raise ToolError(-32603, f"upstream account failed: HTTP {status}", data=body)
    out = []
    for b in body.get("balances") or []:
        free = b.get("free") or "0"
        locked = b.get("locked") or "0"
        if free in ("0", "0.0", "0.00") and locked in ("0", "0.0", "0.00"):
            continue
        out.append({"asset": b.get("asset"), "free": free, "locked": locked})
    return out


def t_get_open_orders(ident: Identity, args: dict) -> Any:
    _require_scope(ident, "read", "get_open_orders")
    params: dict = {}
    if args.get("symbol"):
        params["symbol"] = to_compact(args["symbol"])
    status, body = _v3_signed_get(ident, "/v3/openOrders", params)
    if status != 200 or not isinstance(body, list):
        raise ToolError(-32603, f"upstream openOrders failed: HTTP {status}", data=body)
    return body


def t_get_order(ident: Identity, args: dict) -> Any:
    _require_scope(ident, "read", "get_order")
    sym = args.get("symbol")
    if not sym:
        raise ToolError(-32602, "symbol is required")
    params = {"symbol": to_compact(sym)}
    if args.get("order_id"):
        params["orderId"] = args["order_id"]
    elif args.get("client_order_id"):
        params["origClientOrderId"] = args["client_order_id"]
    else:
        raise ToolError(-32602, "either order_id or client_order_id is required")
    status, body = _v3_signed_get(ident, "/v3/order", params)
    if status != 200:
        raise ToolError(-32603, f"upstream order lookup failed: HTTP {status}", data=body)
    return body


def t_get_my_trades(ident: Identity, args: dict) -> Any:
    _require_scope(ident, "read", "get_my_trades")
    sym = args.get("symbol")
    if not sym:
        raise ToolError(-32602, "symbol is required")
    params = {"symbol": to_compact(sym), "limit": int(args.get("limit") or 100)}
    status, body = _v3_signed_get(ident, "/v3/myTrades", params)
    if status != 200 or not isinstance(body, list):
        raise ToolError(-32603, f"upstream myTrades failed: HTTP {status}", data=body)
    return body


# ----- Trading tools (require 'trade') --------------------------------------


def t_place_order(ident: Identity, args: dict) -> Any:
    _require_scope(ident, "trade", "place_order")
    required = ["symbol", "side", "type", "quantity"]
    for k in required:
        if not args.get(k):
            raise ToolError(-32602, f"{k} is required")
    side = str(args["side"]).upper()
    typ = str(args["type"]).upper()
    if side not in ("BUY", "SELL"):
        raise ToolError(-32602, "side must be BUY or SELL")
    if typ not in ("LIMIT", "MARKET"):
        raise ToolError(
            -32602,
            "type must be LIMIT or MARKET (use the order_engine " "tools for STOP-LIMIT/OCO)",
        )
    form: dict = {
        "symbol": to_compact(args["symbol"]),
        "side": side,
        "type": typ,
        "quantity": str(args["quantity"]),
    }
    if typ == "LIMIT":
        if not args.get("price"):
            raise ToolError(-32602, "price is required for LIMIT orders")
        form["price"] = str(args["price"])
        form["timeInForce"] = str(args.get("time_in_force") or "GTC").upper()
    if args.get("client_order_id"):
        form["newClientOrderId"] = str(args["client_order_id"])
    else:
        form["newClientOrderId"] = f"mcp-{uuid.uuid4().hex[:16]}"
    status, body = _v3_signed_post(ident, "/v3/order", form)
    if status >= 400:
        raise ToolError(-32603, f"order rejected: HTTP {status}", data=body)
    if not isinstance(body, dict):
        raise ToolError(-32603, "unexpected order response shape", data=body)
    return {
        "order_id": body.get("orderId"),
        "client_order_id": body.get("clientOrderId") or form.get("newClientOrderId"),
        "symbol": body.get("symbol"),
        "status": body.get("status"),
        "executed_qty": body.get("executedQty"),
        "cummulative_quote_qty": body.get("cummulativeQuoteQty"),
        "transact_time": body.get("transactTime"),
        "price": body.get("price"),
        "side": body.get("side"),
        "type": body.get("type"),
        "tx_id_if_chain_settled": None,
    }


def t_cancel_order(ident: Identity, args: dict) -> Any:
    _require_scope(ident, "trade", "cancel_order")
    sym = args.get("symbol")
    if not sym:
        raise ToolError(-32602, "symbol is required")
    form = {"symbol": to_compact(sym)}
    if args.get("order_id"):
        form["orderId"] = args["order_id"]
    elif args.get("client_order_id"):
        form["origClientOrderId"] = args["client_order_id"]
    else:
        raise ToolError(-32602, "either order_id or client_order_id is required")
    status, body = _v3_signed_post(ident, "/v3/order", form, method="DELETE")
    if status >= 400:
        raise ToolError(-32603, f"cancel rejected: HTTP {status}", data=body)
    return body


def t_cancel_all_orders(ident: Identity, args: dict) -> Any:
    _require_scope(ident, "trade", "cancel_all_orders")
    sym = args.get("symbol")
    if not sym:
        raise ToolError(-32602, "symbol is required")
    form = {"symbol": to_compact(sym)}
    status, body = _v3_signed_post(ident, "/v3/openOrders", form, method="DELETE")
    if status >= 400:
        raise ToolError(-32603, f"cancel-all rejected: HTTP {status}", data=body)
    return body


# ----- On-chain read tools --------------------------------------------------


def t_get_chain_info(ident: Identity, args: dict) -> Any:
    headers = ident.upstream_headers()
    status, body = _http_json("GET", f"{CHAIN_BASE}/chain/info", headers=headers)
    if status != 200:
        raise ToolError(-32603, f"chain/info failed: HTTP {status}", data=body)
    if isinstance(body, dict) and "chains" in body:
        return body["chains"]
    return body


def t_get_chain_balance(ident: Identity, args: dict) -> Any:
    _require_scope(ident, "read", "get_chain_balance")
    chain = args.get("chain") or ""
    headers = ident.upstream_headers()
    qs = urllib.parse.urlencode({"chain": chain} if chain else {})
    url = f"{CHAIN_BASE}/chain/wallet" + (("?" + qs) if qs else "")
    status, body = _http_json("GET", url, headers=headers)
    if status != 200:
        raise ToolError(-32603, f"chain/wallet failed: HTTP {status}", data=body)
    return body


def t_list_deposits(ident: Identity, args: dict) -> Any:
    _require_scope(ident, "read", "list_deposits")
    headers = ident.upstream_headers()
    status, body = _http_json("GET", f"{CHAIN_BASE}/chain/deposits", headers=headers)
    if status != 200:
        raise ToolError(-32603, f"chain/deposits failed: HTTP {status}", data=body)
    rows = (
        body
        if isinstance(body, list)
        else (body.get("deposits") or [] if isinstance(body, dict) else [])
    )
    limit = int(args.get("limit") or 100)
    return rows[:limit]


def t_list_withdraws(ident: Identity, args: dict) -> Any:
    _require_scope(ident, "read", "list_withdraws")
    headers = ident.upstream_headers()
    status, body = _http_json("GET", f"{CHAIN_BASE}/chain/withdraws", headers=headers)
    if status != 200:
        raise ToolError(-32603, f"chain/withdraws failed: HTTP {status}", data=body)
    rows = (
        body
        if isinstance(body, list)
        else (body.get("withdraws") or [] if isinstance(body, dict) else [])
    )
    limit = int(args.get("limit") or 100)
    return rows[:limit]


# ----- Verification tools ---------------------------------------------------


def t_get_pol_my_proof(ident: Identity, args: dict) -> Any:
    _require_scope(ident, "read", "get_pol_my_proof")
    headers = ident.upstream_headers()
    status, body = _http_json("GET", f"{POL_BASE}/pol/my-proof", headers=headers)
    if status != 200:
        raise ToolError(-32603, f"pol/my-proof failed: HTTP {status}", data=body)
    return body


def t_get_pol_reserves(ident: Identity, args: dict) -> Any:
    status, body = _http_json("GET", f"{POL_BASE}/pol/reserves-vs-liabilities")
    if status != 200:
        raise ToolError(-32603, f"pol/reserves failed: HTTP {status}", data=body)
    return body


# --- BulletinBoard on-chain anchor (anchor_indexer.py) --------------------


def t_get_anchor_status(ident: Identity, args: dict) -> Any:
    """Surface anchor_indexer's /anchor/health: whether the BulletinBoard is
    configured, latest scanned block, head lag, and event counts. No auth
    required; the data is fully public."""
    status, body = _http_json("GET", f"{ANCHOR_BASE}/anchor/health")
    if status != 200:
        raise ToolError(-32603, f"anchor/health failed: HTTP {status}", data=body)
    # Also fold in /anchor/latest so the caller gets the chain tip + latest
    # batch hash in a single round-trip. /anchor/latest 503's in degraded mode
    # though, so we tolerate that.
    latest_status, latest_body = _http_json("GET", f"{ANCHOR_BASE}/anchor/latest")
    if latest_status == 200:
        body["latest"] = latest_body
    return body


def t_get_my_commitment_history(ident: Identity, args: dict) -> Any:
    """Return the caller's per-account commitment history from
    anchor_indexer (Pedersen point updates published to the BulletinBoard
    contract). Requires 'read' scope. The caller's addr_key is derived
    deterministically from their opex_user; the upstream auth check
    happens inside anchor_indexer."""
    _require_scope(ident, "read", "get_my_commitment_history")
    if not ident.opex_user:
        raise ToolError(-32602, "opex_user not resolvable from identity")
    if not ident.bearer:
        raise ToolError(-32603, "this tool requires a bearer session, not an api-key")
    # Mirror anchor_indexer._opex_to_addr_key (sha256, matching zkPoL).
    import hashlib

    addr_key = "0x" + hashlib.sha256(ident.opex_user.encode("utf-8")).hexdigest()
    limit = int(args.get("limit") or 25)
    limit = max(1, min(limit, 500))
    url = f"{ANCHOR_BASE}/anchor/account/{addr_key}?limit={limit}"
    headers = {"Authorization": f"Bearer {ident.bearer}"}
    status, body = _http_json("GET", url, headers=headers)
    if status != 200:
        raise ToolError(-32603, f"anchor/account failed: HTTP {status}", data=body)
    return body


def t_get_anchor_batch(ident: Identity, args: dict) -> Any:
    """Look up a single appendBatch batch by its on-chain ``batch_hash``
    (0x-prefixed 32-byte hex). Returns the batch's tokenKey, liability
    transition, tx hash, block #, and every commitment update bundled
    into it. Public — anyone can audit a batch given its hash."""
    bh = (args.get("batch_hash") or "").strip()
    if not bh:
        raise ToolError(-32602, "batch_hash required")
    url = f"{ANCHOR_BASE}/anchor/batch/{bh}"
    status, body = _http_json("GET", url)
    if status == 404:
        raise ToolError(-32602, "batch not found", data=body)
    if status != 200:
        raise ToolError(-32603, f"anchor/batch failed: HTTP {status}", data=body)
    return body


def t_get_safu_summary(ident: Identity, args: dict) -> Any:
    """Reflects /safu/summary — public SAFU (Secure Asset Fund for Users)
    insurance-fund state: per-asset balances, monthly inflow/payout, open
    incident count."""
    status, body = _http_json("GET", f"{SAFU_BASE}/safu/summary")
    if status != 200:
        raise ToolError(-32603, f"safu/summary failed: HTTP {status}", data=body)
    return body


def t_get_safu_attestation(ident: Identity, args: dict) -> Any:
    """Reflects /safu/attestation — Ed25519-signed snapshot of the SAFU
    totals. The pubkey is the same custodian key that signs PoL roots,
    so an external verifier only has to trust one key."""
    status, body = _http_json("GET", f"{SAFU_BASE}/safu/attestation")
    if status != 200:
        raise ToolError(-32603, f"safu/attestation failed: HTTP {status}", data=body)
    return body


# ----- Referral tools -------------------------------------------------------


def t_get_my_referral_code(ident: Identity, args: dict) -> Any:
    """Reflects /referral/my-code — returns the authenticated user's referral
    code (auto-generates one on first call) plus a shareable signup link.
    Requires 'read' scope."""
    _require_scope(ident, "read", "get_my_referral_code")
    if not ident.bearer:
        raise ToolError(
            -32001,
            "get_my_referral_code requires a bearer-mode session.",
            data={"tool": "get_my_referral_code"},
        )
    status, body = _http_json(
        "GET",
        f"{REFERRAL_BASE}/referral/my-code",
        headers={"Authorization": f"Bearer {ident.bearer}"},
    )
    if status != 200:
        raise ToolError(-32603, f"referral/my-code failed: HTTP {status}", data=body)
    return body


def t_get_referral_stats(ident: Identity, args: dict) -> Any:
    """Reflects /referral/stats — the user's referral dashboard with total
    signups, qualified referrals, earnings breakdown, and a redacted list of
    recent referees. Requires 'read' scope."""
    _require_scope(ident, "read", "get_referral_stats")
    if not ident.bearer:
        raise ToolError(
            -32001,
            "get_referral_stats requires a bearer-mode session.",
            data={"tool": "get_referral_stats"},
        )
    status, body = _http_json(
        "GET",
        f"{REFERRAL_BASE}/referral/stats",
        headers={"Authorization": f"Bearer {ident.bearer}"},
    )
    if status != 200:
        raise ToolError(-32603, f"referral/stats failed: HTTP {status}", data=body)
    return body


def t_get_referral_leaderboard(ident: Identity, args: dict) -> Any:
    """Reflects /referral/leaderboard — top referrers by 30-day earnings.
    Public endpoint; opex_user is redacted to first 3 chars + '***'."""
    try:
        limit = int(args.get("limit") or 20)
    except Exception:
        limit = 20
    limit = max(1, min(100, limit))
    status, body = _http_json(
        "GET",
        f"{REFERRAL_BASE}/referral/leaderboard?limit={limit}",
    )
    if status != 200:
        raise ToolError(-32603, f"referral/leaderboard failed: HTTP {status}", data=body)
    return body


def t_customize_referral_code(ident: Identity, args: dict) -> Any:
    """Reflects POST /referral/customize — set a custom referral code. Only
    allowed once. Code must be 4-12 uppercase alphanumeric chars. Requires
    'trade' scope (this is an account-level mutation)."""
    _require_scope(ident, "trade", "customize_referral_code")
    if not ident.bearer:
        raise ToolError(
            -32001,
            "customize_referral_code requires a bearer-mode session.",
            data={"tool": "customize_referral_code"},
        )
    custom_code = (args.get("custom_code") or "").strip().upper()
    if not custom_code:
        raise ToolError(-32602, "custom_code is required")
    status, body = _http_json(
        "POST",
        f"{REFERRAL_BASE}/referral/customize",
        headers={"Authorization": f"Bearer {ident.bearer}"},
        body={"custom_code": custom_code},
    )
    if status != 200:
        raise ToolError(
            -32603,
            f"referral/customize failed: HTTP {status}",
            data=body,
        )
    return body


# ----- VIP fee tools --------------------------------------------------------

FEE_BASE = os.environ.get("MCP_FEE_BASE", "http://127.0.0.1:5710")


def t_get_fee_tiers(ident: Identity, args: dict) -> Any:
    """Public ladder of all 10 VIP fee tiers (level 0..9)."""
    status, body = _http_json("GET", f"{FEE_BASE}/fees/tiers")
    if status != 200:
        raise ToolError(-32603, f"fees/tiers failed: HTTP {status}", data=body)
    return body.get("tiers") if isinstance(body, dict) else body


def t_get_my_fee_tier(ident: Identity, args: dict) -> Any:
    """The authenticated user's current VIP tier with 30d volume, holdings,
    and the gap to the next tier. Requires 'read' scope."""
    _require_scope(ident, "read", "get_my_fee_tier")
    status, body = _http_json("GET", f"{FEE_BASE}/fees/my-tier", headers=ident.upstream_headers())
    if status != 200:
        raise ToolError(-32603, f"fees/my-tier failed: HTTP {status}", data=body)
    return body


def t_get_fee_quote(ident: Identity, args: dict) -> Any:
    """Compute the maker/taker fee for a hypothetical fill at the caller's
    current VIP tier. Use this before place_order to know what fee you'll
    pay. Requires 'read' scope."""
    _require_scope(ident, "read", "get_fee_quote")
    market = (args.get("market") or "spot").lower()
    if market not in ("spot", "futures"):
        raise ToolError(-32602, "market must be 'spot' or 'futures'")
    symbol = args.get("symbol") or ""
    side = (args.get("side") or "BUY").upper()
    qty = args.get("qty")
    price = args.get("price")
    if qty in (None, "", 0) or price in (None, "", 0):
        raise ToolError(-32602, "qty and price are required")
    qs = urllib.parse.urlencode(
        {"market": market, "symbol": symbol, "side": side, "qty": str(qty), "price": str(price)}
    )
    status, body = _http_json(
        "GET", f"{FEE_BASE}/fees/quote?{qs}", headers=ident.upstream_headers()
    )
    if status != 200:
        raise ToolError(-32603, f"fees/quote failed: HTTP {status}", data=body)
    return body


# ----- Staking tools --------------------------------------------------------

STAKING_BASE = os.environ.get("MCP_STAKING_BASE", "http://127.0.0.1:5692")


def t_list_staking_products(ident: Identity, args: dict) -> Any:
    """Public list of active staking products with APY, term, and remaining
    capacity. No authentication required."""
    status, body = _http_json("GET", f"{STAKING_BASE}/staking/products")
    if status != 200:
        raise ToolError(-32603, f"staking/products failed: HTTP {status}", data=body)
    return body


def t_get_my_staking_positions(ident: Identity, args: dict) -> Any:
    """List the authenticated user's staking positions (active + matured
    pending + recently redeemed). Requires 'read' scope."""
    _require_scope(ident, "read", "get_my_staking_positions")
    headers = ident.upstream_headers()
    status, body = _http_json("GET", f"{STAKING_BASE}/staking/positions", headers=headers)
    if status != 200:
        raise ToolError(-32603, f"staking/positions failed: HTTP {status}", data=body)
    return body.get("positions") if isinstance(body, dict) else body


def t_stake(ident: Identity, args: dict) -> Any:
    """Open a new staking position: lock ``amount`` of the product's asset
    into ``product_id`` and start earning the product's APY. The principal
    is debited from the user's MAIN wallet. Requires 'trade' scope."""
    _require_scope(ident, "trade", "stake")
    product_id = args.get("product_id")
    amount = args.get("amount")
    if not product_id:
        raise ToolError(-32602, "product_id is required")
    if amount in (None, "", 0):
        raise ToolError(-32602, "amount is required")
    headers = ident.upstream_headers()
    status, body = _http_json(
        "POST",
        f"{STAKING_BASE}/staking/stake",
        headers=headers,
        body={"product_id": str(product_id), "amount": str(amount)},
    )
    if status != 200:
        raise ToolError(-32603, f"staking/stake failed: HTTP {status}", data=body)
    return body


def t_redeem_staking(ident: Identity, args: dict) -> Any:
    """Close a staking position. Use this for matured fixed-term positions
    or flexible-term positions (instant). For un-matured fixed-term, set
    ``force_early=true`` to exit with the early-unstake penalty. Returns
    the redeemed amount (principal + net yield - any penalty) which is
    credited back to the user's MAIN wallet. Requires 'trade' scope."""
    _require_scope(ident, "trade", "redeem_staking")
    pid = args.get("position_id")
    if pid in (None, ""):
        raise ToolError(-32602, "position_id is required")
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        raise ToolError(-32602, "position_id must be an integer") from None
    endpoint = "unstake" if args.get("force_early") else "redeem"
    headers = ident.upstream_headers()
    status, body = _http_json(
        "POST",
        f"{STAKING_BASE}/staking/positions/{pid_int}/{endpoint}",
        headers=headers,
    )
    if status != 200:
        raise ToolError(-32603, f"staking/{endpoint} failed: HTTP {status}", data=body)
    return body


# ----- NFT marketplace tools -----------------------------------------------

NFT_BASE = os.environ.get("MCP_NFT_BASE", "http://127.0.0.1:5694")


def t_list_nfts(ident: Identity, args: dict) -> Any:
    """List active NFT marketplace listings. Optionally filter by
    ``collection`` (contract address), ``min_price`` / ``max_price``,
    ``asset`` (USDT|ETH), or ``standard`` (erc721|erc1155). Public — no
    scope required."""
    params: dict = {}
    for k in ("collection", "min_price", "max_price", "asset", "standard", "sort"):
        v = args.get(k)
        if v is not None and v != "":
            params[k] = str(v)
    if args.get("limit"):
        params["limit"] = str(int(args["limit"]))
    qs = urllib.parse.urlencode(params)
    url = f"{NFT_BASE}/nft/listings" + (("?" + qs) if qs else "")
    status, body = _http_json("GET", url)
    if status != 200:
        raise ToolError(-32603, f"nft/listings failed: HTTP {status}", data=body)
    return body


def t_list_nft_collections(ident: Identity, args: dict) -> Any:
    """Public list of NFT collections on the zkCEX marketplace, with floor
    price, total volume, active listing count, and a sample image."""
    status, body = _http_json("GET", f"{NFT_BASE}/nft/collections")
    if status != 200:
        raise ToolError(-32603, f"nft/collections failed: HTTP {status}", data=body)
    return body.get("collections") if isinstance(body, dict) else body


def t_get_my_nfts(ident: Identity, args: dict) -> Any:
    """List NFTs currently owned by the authenticated user. Reads on-chain
    ownership directly (ownerOf for ERC721, balanceOf for ERC1155).
    Requires 'read' scope."""
    _require_scope(ident, "read", "get_my_nfts")
    headers = ident.upstream_headers()
    status, body = _http_json("GET", f"{NFT_BASE}/nft/my-nfts", headers=headers)
    if status != 200:
        raise ToolError(-32603, f"nft/my-nfts failed: HTTP {status}", data=body)
    return body


def t_list_nft_for_sale(ident: Identity, args: dict) -> Any:
    """Put an NFT up for sale on the marketplace. Verifies on-chain
    ownership, moves the token into custodial escrow, and creates the
    listing. Requires 'trade' scope."""
    _require_scope(ident, "trade", "list_nft_for_sale")
    contract = args.get("contract_address") or ""
    token_id = args.get("token_id")
    if not contract or token_id in (None, ""):
        raise ToolError(-32602, "contract_address and token_id are required")
    body_req = {
        "contract_address": str(contract),
        "token_id": str(token_id),
        "quantity": str(args.get("quantity") or 1),
        "list_price": str(args.get("list_price") or "0"),
        "list_asset": (args.get("list_asset") or "USDT").upper(),
        "expires_in_days": int(args.get("expires_in_days") or 7),
    }
    headers = ident.upstream_headers()
    status, body = _http_json("POST", f"{NFT_BASE}/nft/list", headers=headers, body=body_req)
    if status != 200:
        raise ToolError(-32603, f"nft/list failed: HTTP {status}", data=body)
    return body


def t_buy_nft(ident: Identity, args: dict) -> Any:
    """Buy a listed NFT atomically: debit the buyer's USDT/ETH, credit the
    seller minus the 2.5% platform fee, and transfer the NFT on-chain
    from the custodial escrow to the buyer's wallet. Requires 'trade'
    scope and that the buyer's chain wallet has been derived (visit
    /app/wallet.html once)."""
    _require_scope(ident, "trade", "buy_nft")
    lid = args.get("listing_id")
    if lid in (None, ""):
        raise ToolError(-32602, "listing_id is required")
    try:
        lid_i = int(lid)
    except (TypeError, ValueError):
        raise ToolError(-32602, "listing_id must be an integer") from None
    headers = ident.upstream_headers()
    status, body = _http_json("POST", f"{NFT_BASE}/nft/buy/{lid_i}", headers=headers)
    if status != 200:
        raise ToolError(-32603, f"nft/buy failed: HTTP {status}", data=body)
    return body


# ----- Exports --------------------------------------------------------------


def t_export_trades_csv(ident: Identity, args: dict) -> Any:
    _require_scope(ident, "read", "export_trades_csv")
    if not ident.bearer:
        raise ToolError(
            -32001,
            "export_trades_csv requires a bearer-mode session "
            "(use --auth bearer or set up an app token).",
            data={"tool": "export_trades_csv"},
        )
    params: dict = {}
    if args.get("symbol"):
        params["symbol"] = to_underscored(args["symbol"])
    if args.get("from_ms"):
        params["from"] = int(args["from_ms"])
    if args.get("to_ms"):
        params["to"] = int(args["to_ms"])
    qs = urllib.parse.urlencode(params)
    url = f"{EXPORT_BASE}/export/trades.csv" + (("?" + qs) if qs else "")
    status, _, data = _http_request("GET", url, headers={"Authorization": f"Bearer {ident.bearer}"})
    if status != 200:
        raise ToolError(
            -32603, f"export failed: HTTP {status}", data=data.decode("utf-8", "replace")[:500]
        )
    return {
        "filename": "trades.csv",
        "content_type": "text/csv",
        "size_bytes": len(data),
        "base64": base64.b64encode(data).decode("ascii"),
    }


# ----- Perpetual-futures tools ---------------------------------------------
#
# These wrap /fapi/v1/* on the perp_engine sibling service. The same scope
# rules used for /v3 apply: reads need 'read', mutating calls need 'trade'.
# Withdraw is not relevant here — wallet transfers within the cluster don't
# leave the exchange.


def t_get_perp_markets(ident: Identity, args: dict) -> Any:
    status, body = _perp_public_get("/fapi/v1/exchangeInfo")
    if status != 200 or not isinstance(body, dict):
        raise ToolError(-32603, f"upstream perp exchangeInfo failed: HTTP {status}", data=body)
    return [
        {
            "symbol": s.get("symbol"),
            "base": s.get("baseAsset"),
            "quote": s.get("quoteAsset"),
            "contract_size": s.get("contractSize"),
            "tick_size": s.get("tickSize"),
            "step_size": s.get("stepSize"),
            "max_leverage": s.get("maxLeverage"),
            "maintenance_margin_rate": s.get("maintenanceMarginRate"),
            "funding_interval_s": s.get("fundingIntervalSeconds"),
            "index_symbol": s.get("indexSymbol"),
            "status": s.get("status"),
        }
        for s in (body.get("symbols") or [])
    ]


def t_get_premium_index(ident: Identity, args: dict) -> Any:
    sym = args.get("symbol")
    params = {"symbol": _normalize_perp_symbol(sym)} if sym else None
    status, body = _perp_public_get("/fapi/v1/premiumIndex", params)
    if status != 200:
        raise ToolError(-32603, f"upstream premiumIndex failed: HTTP {status}", data=body)
    return body


def t_get_funding_history(ident: Identity, args: dict) -> Any:
    sym = args.get("symbol")
    limit = int(args.get("limit") or 100)
    params: dict = {"limit": limit}
    if sym:
        params["symbol"] = _normalize_perp_symbol(sym)
    status, body = _perp_public_get("/fapi/v1/fundingRate", params)
    if status != 200 or not isinstance(body, list):
        raise ToolError(-32603, f"upstream fundingRate failed: HTTP {status}", data=body)
    return body


def t_get_positions(ident: Identity, args: dict) -> Any:
    _require_scope(ident, "read", "get_positions")
    params: dict = {}
    if args.get("symbol"):
        params["symbol"] = _normalize_perp_symbol(args["symbol"])
    status, body = _perp_signed_get(ident, "/fapi/v1/positionRisk", params)
    if status != 200:
        raise ToolError(-32603, f"perp positionRisk failed: HTTP {status}", data=body)
    return body


def t_get_perp_account(ident: Identity, args: dict) -> Any:
    _require_scope(ident, "read", "get_perp_account")
    status, body = _perp_signed_get(ident, "/fapi/v1/account")
    if status != 200:
        raise ToolError(-32603, f"perp account failed: HTTP {status}", data=body)
    return body


def t_place_perp_order(ident: Identity, args: dict) -> Any:
    _require_scope(ident, "trade", "place_perp_order")
    for k in ("symbol", "side", "quantity"):
        if not args.get(k):
            raise ToolError(-32602, f"{k} is required")
    side = str(args["side"]).upper()
    typ = str(args.get("type") or "MARKET").upper()
    if side not in ("BUY", "SELL"):
        raise ToolError(-32602, "side must be BUY or SELL")
    if typ not in ("MARKET", "LIMIT"):
        raise ToolError(-32602, "type must be MARKET or LIMIT")
    sym = _normalize_perp_symbol(args["symbol"])
    leverage = int(args.get("leverage") or 0)
    # Best-effort: set the per-symbol leverage before placing the order so
    # the agent gets the position size it expects. Failures are not fatal —
    # the perp engine will fall back to the user's current saved setting.
    if leverage and leverage > 0:
        try:
            _perp_signed_post(ident, "/fapi/v1/leverage", {"symbol": sym, "leverage": leverage})
        except Exception as e:  # noqa: BLE001
            _log(f"perp leverage set skipped: {e!r}")
    form: dict = {"symbol": sym, "side": side, "type": typ, "quantity": str(args["quantity"])}
    if typ == "LIMIT":
        if not args.get("price"):
            raise ToolError(-32602, "price is required for LIMIT orders (settled at mark in v0)")
        form["price"] = str(args["price"])
    if args.get("reduce_only"):
        form["reduceOnly"] = "true"
    if args.get("client_order_id"):
        form["newClientOrderId"] = args["client_order_id"]
    status, body = _perp_signed_post(ident, "/fapi/v1/order", form)
    if status >= 400:
        raise ToolError(-32603, f"perp order rejected: HTTP {status}", data=body)
    return body


def t_close_position(ident: Identity, args: dict) -> Any:
    _require_scope(ident, "trade", "close_position")
    if not args.get("symbol"):
        raise ToolError(-32602, "symbol is required")
    sym = _normalize_perp_symbol(args["symbol"])
    status, body = _perp_signed_post(ident, "/fapi/v1/closePosition", {"symbol": sym})
    if status >= 400:
        raise ToolError(-32603, f"closePosition rejected: HTTP {status}", data=body)
    return body


def t_transfer_to_futures(ident: Identity, args: dict) -> Any:
    _require_scope(ident, "trade", "transfer_to_futures")
    if not args.get("amount"):
        raise ToolError(-32602, "amount is required")
    form = {"asset": "USDT", "amount": str(args["amount"]), "type": "SPOT_TO_FUTURES"}
    status, body = _perp_signed_post(ident, "/fapi/v1/transfer", form)
    if status >= 400:
        raise ToolError(-32603, f"spot->futures transfer failed: HTTP {status}", data=body)
    return body


def t_transfer_to_spot(ident: Identity, args: dict) -> Any:
    _require_scope(ident, "trade", "transfer_to_spot")
    if not args.get("amount"):
        raise ToolError(-32602, "amount is required")
    form = {"asset": "USDT", "amount": str(args["amount"]), "type": "FUTURES_TO_SPOT"}
    status, body = _perp_signed_post(ident, "/fapi/v1/transfer", form)
    if status >= 400:
        raise ToolError(-32603, f"futures->spot transfer failed: HTTP {status}", data=body)
    return body


# ----- Privacy-trading (zk_orderbook) tools --------------------------------
#
# Wraps the commit-reveal FBA service at :5660. The agent generates the
# salt locally (so the MCP host can't see the preimage if it ever logs the
# RPC body) and persists (commitment_hex -> preimage) in a process-local
# cache so a subsequent zk_reveal_order call can look up the right values.
# All mutating endpoints accept either Authorization: Bearer or trusted
# X-Opex-User (we forward both via ident.upstream_headers()).

_ZK_PREIMAGE_LOCK = threading.Lock()
_ZK_PREIMAGE_CACHE: dict[str, dict] = {}  # commitment_hex -> {salt,...}


def _zk_u8(n):
    return int(n).to_bytes(1, "big", signed=False)


def _zk_u16(n):
    return int(n).to_bytes(2, "big", signed=False)


def _zk_u64(n):
    if n < 0:
        n = 0
    if n >= 1 << 64:
        n = (1 << 64) - 1
    return int(n).to_bytes(8, "big", signed=False)


def _zk_lp(s: str) -> bytes:
    b = s.encode("utf-8")
    if len(b) > 0xFFFF:
        b = b[:0xFFFF]
    return _zk_u16(len(b)) + b


def _zk_scale8(s: str) -> int:
    from decimal import Decimal as _D

    try:
        d = _D(str(s))
    except Exception:
        return 0
    if d < 0:
        return 0
    return int((d * _D(10**8)).to_integral_value())


def _zk_commitment(
    salt_hex: str, price: str, qty: str, side: str, symbol: str, user_id: str, expires_at: int
) -> str:
    salt = bytes.fromhex(salt_hex)
    if len(salt) != 32:
        raise ToolError(-32602, "salt must be 32 bytes")
    msg = (
        salt
        + _zk_u64(_zk_scale8(price))
        + _zk_u64(_zk_scale8(qty))
        + _zk_u8(0 if side.upper() == "BUY" else 1)
        + _zk_lp(symbol)
        + _zk_lp(user_id)
        + _zk_u64(expires_at)
    )
    return hashlib.sha256(msg).hexdigest()


def t_zk_info(ident: Identity, args: dict) -> Any:
    """Public phase + batch state for the privacy trading mode."""
    sym = args.get("symbol")
    qs = ("?symbol=" + urllib.parse.quote(sym.upper())) if sym else ""
    status, body = _http_json("GET", f"{ZK_BASE}/zk-trade/info{qs}")
    if status != 200:
        raise ToolError(-32603, f"zk-trade/info failed: HTTP {status}", data=body)
    return body


def t_zk_commit_order(ident: Identity, args: dict) -> Any:
    """Commit a privacy order. Generates the salt server-side and submits
    SHA-256(salt || price_8dp || qty_8dp || side || symbol || user_id ||
    expires_at). The preimage is cached in-process so zk_reveal_order
    can look it up later — agents shouldn't need to manage the salt
    themselves."""
    _require_scope(ident, "trade", "zk_commit_order")
    if not ident.opex_user:
        raise ToolError(-32002, "Authenticate first.")
    symbol = str(args.get("symbol") or "").upper()
    side = str(args.get("side") or "").upper()
    price = str(args.get("price") or "")
    quantity = str(args.get("quantity") or "")
    if not symbol or side not in ("BUY", "SELL") or not price or not quantity:
        raise ToolError(-32602, "required: symbol, side (BUY|SELL), price, quantity")
    # Fetch current phase to pick expires_at = window_end - 1.
    status, info = _http_json("GET", f"{ZK_BASE}/zk-trade/info?symbol={symbol}")
    if status != 200 or not isinstance(info, dict):
        raise ToolError(-32603, "zk-trade/info failed", data=info)
    if info.get("phase") != "commit":
        raise ToolError(
            -32603,
            f"Not in commit phase (current={info.get('phase')}, "
            f"wait {info.get('time_left_in_phase')}s).",
            data=info,
        )
    expires_at = int(info["window_end"]) - 1
    salt_hex = secrets.token_bytes(32).hex()
    commitment_hex = _zk_commitment(
        salt_hex, price, quantity, side, symbol, ident.opex_user, expires_at
    )
    headers = ident.upstream_headers()
    headers["Content-Type"] = "application/json"
    status, body = _http_json(
        "POST",
        f"{ZK_BASE}/zk-trade/commit",
        headers=headers,
        body={"symbol": symbol, "commitment_hex": commitment_hex, "expires_at": expires_at},
    )
    if status != 200:
        raise ToolError(-32603, f"zk-trade/commit failed: HTTP {status}", data=body)
    with _ZK_PREIMAGE_LOCK:
        _ZK_PREIMAGE_CACHE[commitment_hex] = {
            "salt_hex": salt_hex,
            "price": price,
            "quantity": quantity,
            "side": side,
            "symbol": symbol,
            "expires_at": expires_at,
            "opex_user": ident.opex_user,
        }
    return {
        "ok": True,
        "commitment_hex": commitment_hex,
        "salt_hex": salt_hex,
        "batch_id": body.get("batch_id"),
        "phase": body.get("phase"),
        "time_to_reveal": body.get("time_to_reveal"),
        "expires_at": expires_at,
        "note": (
            "Server cannot recover your order from the commitment alone. "
            "The reveal preimage is cached in-process — call "
            "zk_reveal_order with this commitment_hex during the reveal "
            "phase."
        ),
    }


def t_zk_reveal_order(ident: Identity, args: dict) -> Any:
    """Reveal a previously-committed order. Looks up the preimage from the
    in-process cache and POSTs it to /zk-trade/reveal."""
    _require_scope(ident, "trade", "zk_reveal_order")
    commitment_hex = str(args.get("commitment_hex") or "").lower()
    if len(commitment_hex) != 64:
        raise ToolError(-32602, "bad commitment_hex")
    with _ZK_PREIMAGE_LOCK:
        pre = _ZK_PREIMAGE_CACHE.get(commitment_hex)
    if not pre:
        raise ToolError(
            -32602,
            "preimage not cached. The salt only lives in the "
            "process that called zk_commit_order. Pass it "
            "explicitly in args if you committed elsewhere.",
            data={"commitment_hex": commitment_hex},
        )
    headers = ident.upstream_headers()
    headers["Content-Type"] = "application/json"
    status, body = _http_json(
        "POST",
        f"{ZK_BASE}/zk-trade/reveal",
        headers=headers,
        body={
            "commitment_hex": commitment_hex,
            "salt_hex": pre["salt_hex"],
            "price": pre["price"],
            "quantity": pre["quantity"],
            "side": pre["side"],
            "symbol": pre["symbol"],
            "expires_at": pre["expires_at"],
        },
    )
    if status != 200:
        raise ToolError(-32603, f"zk-trade/reveal failed: HTTP {status}", data=body)
    return body


def t_zk_get_batches(ident: Identity, args: dict) -> Any:
    """Public batch history with clearing prices + signed Merkle roots."""
    symbol = args.get("symbol")
    limit = int(args.get("limit") or 10)
    qs = []
    if symbol:
        qs.append(f"symbol={urllib.parse.quote(str(symbol).upper())}")
    qs.append(f"limit={limit}")
    url = f"{ZK_BASE}/zk-trade/batches?" + "&".join(qs)
    status, body = _http_json("GET", url)
    if status != 200:
        raise ToolError(-32603, f"zk-trade/batches failed: HTTP {status}", data=body)
    return body


def t_zk_my_commits(ident: Identity, args: dict) -> Any:
    """The authenticated user's commits + reveals + match outcomes."""
    _require_scope(ident, "read", "zk_my_commits")
    if not ident.opex_user:
        raise ToolError(-32002, "Authenticate first.")
    status, body = _http_json(
        "GET", f"{ZK_BASE}/zk-trade/my-commits", headers=ident.upstream_headers()
    )
    if status != 200:
        raise ToolError(-32603, f"zk-trade/my-commits failed: HTTP {status}", data=body)
    return body


# ----- Notification center tools (require Bearer/'read' or 'trade') -------


def _notif_require_bearer(ident: Identity, tool: str) -> dict:
    """Notification_center endpoints all auth via Bearer to /auth/me.
    Reject API-key identities (no bearer to forward)."""
    if not ident.bearer:
        raise ToolError(
            -32002,
            f"{tool} requires a user Bearer token (sign in to your zkCEX account).",
        )
    return {"Authorization": f"Bearer {ident.bearer}"}


def t_list_my_notifications(ident: Identity, args: dict) -> Any:
    """List the calling user's recent in-app notifications (newest first)."""
    _require_scope(ident, "read", "list_my_notifications")
    headers = _notif_require_bearer(ident, "list_my_notifications")
    try:
        limit = max(1, min(200, int(args.get("limit", 50))))
    except (TypeError, ValueError):
        limit = 50
    status, body = _http_json(
        "GET",
        f"{NOTIF_BASE}/notifications/inbox?limit={limit}",
        headers=headers,
    )
    if status != 200:
        raise ToolError(-32603, f"notifications/inbox failed: HTTP {status}", data=body)
    return body


def t_mark_notification_read(ident: Identity, args: dict) -> Any:
    """Mark a single notification as read by id."""
    _require_scope(ident, "read", "mark_notification_read")
    headers = _notif_require_bearer(ident, "mark_notification_read")
    nid = args.get("notification_id")
    try:
        nid = int(nid)
    except (TypeError, ValueError):
        raise ToolError(-32602, "notification_id must be an integer") from None
    status, body = _http_json(
        "POST",
        f"{NOTIF_BASE}/notifications/{nid}/read",
        headers=headers,
        body={},
    )
    if status != 200:
        raise ToolError(-32603, f"mark-read failed: HTTP {status}", data=body)
    return body


def t_create_price_alert(ident: Identity, args: dict) -> Any:
    """Create an active price alert on a market. Fires once when the last-trade
    price crosses the threshold; the user receives an in-app + push + email
    notification (subject to their preferences). Hard cap: 20 active alerts
    per user."""
    _require_scope(ident, "trade", "create_price_alert")
    headers = _notif_require_bearer(ident, "create_price_alert")
    symbol = (args.get("symbol") or "").strip().upper()
    condition = (args.get("condition") or "").strip().lower()
    threshold = args.get("threshold")
    if isinstance(threshold, (int, float)):
        threshold = str(threshold)
    threshold = (threshold or "").strip()
    if not symbol or condition not in ("above", "below") or not threshold:
        raise ToolError(-32602, "symbol, condition ('above'|'below'), threshold required")
    status, body = _http_json(
        "POST",
        f"{NOTIF_BASE}/notifications/price-alerts",
        headers=headers,
        body={
            "symbol": symbol,
            "condition": condition,
            "threshold_price": threshold,
        },
    )
    if status != 200:
        raise ToolError(-32603, f"create-price-alert failed: HTTP {status}", data=body)
    return body


# ----- Lending / Earn tools ------------------------------------------------
#
# Wraps the standalone lending.py service. Pools are public; per-user reads
# and the supply/borrow mutating endpoints require Bearer auth (resolved by
# lending.py itself against /auth/me) — so trade scope here gates only the
# mutating operations.


def t_get_lending_pools(ident: Identity, args: dict) -> Any:
    """Public per-asset pool state: utilisation, supply/borrow APY,
    collateral factor + liquidation threshold."""
    status, body = _http_json("GET", f"{LENDING_BASE}/lending/pools")
    if status != 200:
        raise ToolError(-32603, f"lending/pools failed: HTTP {status}", data=body)
    return body


def t_get_my_lending_positions(ident: Identity, args: dict) -> Any:
    """Authenticated user's open supply + borrow positions, with current LTV
    and distance to liquidation. Requires 'read' scope."""
    _require_scope(ident, "read", "get_my_lending_positions")
    headers = ident.upstream_headers()
    status, body = _http_json("GET", f"{LENDING_BASE}/lending/my-positions", headers=headers)
    if status != 200:
        raise ToolError(-32603, f"lending/my-positions failed: HTTP {status}", data=body)
    return body


def t_lending_supply(ident: Identity, args: dict) -> Any:
    """Supply ``amount`` of ``asset`` into the pool. Debits the user's spot
    wallet, creates a supply position that accrues interest at the live
    supply APY. Requires 'trade' scope."""
    _require_scope(ident, "trade", "lending_supply")
    asset = (args.get("asset") or "").upper()
    amount = args.get("amount")
    if not asset or not amount:
        raise ToolError(-32602, "asset and amount are required")
    headers = ident.upstream_headers()
    status, body = _http_json(
        "POST",
        f"{LENDING_BASE}/lending/supply",
        headers=headers,
        body={"asset": asset, "amount": str(amount)},
    )
    if status != 200:
        raise ToolError(-32603, f"lending/supply failed: HTTP {status}", data=body)
    return body


def t_lending_borrow(ident: Identity, args: dict) -> Any:
    """Borrow ``borrow_amount`` of ``borrowed_asset`` against
    ``collateral_amount`` of ``collateral_asset``. Validates LTV against the
    collateral pool's collateral_factor, locks collateral, credits the
    borrowed amount. Requires 'trade' scope."""
    _require_scope(ident, "trade", "lending_borrow")
    b_asset = (args.get("borrowed_asset") or "").upper()
    c_asset = (args.get("collateral_asset") or "").upper()
    b_amt = args.get("borrow_amount")
    c_amt = args.get("collateral_amount")
    if not (b_asset and c_asset and b_amt and c_amt):
        raise ToolError(
            -32602,
            "borrowed_asset, borrow_amount, collateral_asset, " "collateral_amount are required",
        )
    headers = ident.upstream_headers()
    status, body = _http_json(
        "POST",
        f"{LENDING_BASE}/lending/borrow",
        headers=headers,
        body={
            "borrowed_asset": b_asset,
            "borrow_amount": str(b_amt),
            "collateral_asset": c_asset,
            "collateral_amount": str(c_amt),
        },
    )
    if status != 200:
        raise ToolError(-32603, f"lending/borrow failed: HTTP {status}", data=body)
    return body


# ----- Tool registry -------------------------------------------------------

TOOLS: list[tuple[dict, Callable[[Identity, dict], Any]]] = [
    (
        {
            "name": "list_markets",
            "description": "List all tradable markets on zkCEX, including base/quote assets and trading status. No authentication required.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        t_list_markets,
    ),
    (
        {
            "name": "get_ticker",
            "description": "Get the 24h ticker (last, change, high, low, volume) for a single symbol like 'ETHUSDT'. No authentication required.",
            "inputSchema": {
                "type": "object",
                "required": ["symbol"],
                "properties": {
                    "symbol": {"type": "string", "description": "e.g. ETHUSDT, BTCUSDT"},
                },
            },
        },
        t_get_ticker,
    ),
    (
        {
            "name": "get_orderbook",
            "description": "Get current order-book bids and asks for a symbol. Returns up to 'limit' levels per side.",
            "inputSchema": {
                "type": "object",
                "required": ["symbol"],
                "properties": {
                    "symbol": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 50},
                },
            },
        },
        t_get_orderbook,
    ),
    (
        {
            "name": "get_recent_trades",
            "description": "Get recent public trades for a symbol (price, qty, time, is_buyer_maker).",
            "inputSchema": {
                "type": "object",
                "required": ["symbol"],
                "properties": {
                    "symbol": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 50},
                },
            },
        },
        t_get_recent_trades,
    ),
    (
        {
            "name": "get_klines",
            "description": "Get OHLCV candlesticks for a symbol at the given interval ('1m','5m','1h','1d', etc).",
            "inputSchema": {
                "type": "object",
                "required": ["symbol", "interval"],
                "properties": {
                    "symbol": {"type": "string"},
                    "interval": {"type": "string", "description": "e.g. 1m, 5m, 15m, 1h, 4h, 1d"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
                },
            },
        },
        t_get_klines,
    ),
    (
        {
            "name": "get_balance",
            "description": "Get the authenticated user's spot balances. Requires 'read' scope.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        t_get_balance,
    ),
    (
        {
            "name": "get_open_orders",
            "description": "List the authenticated user's open orders, optionally filtered by symbol. Requires 'read' scope.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                },
            },
        },
        t_get_open_orders,
    ),
    (
        {
            "name": "get_order",
            "description": "Look up a single order by order_id or client_order_id. Requires 'read' scope.",
            "inputSchema": {
                "type": "object",
                "required": ["symbol"],
                "properties": {
                    "symbol": {"type": "string"},
                    "order_id": {"type": ["string", "integer"]},
                    "client_order_id": {"type": "string"},
                },
            },
        },
        t_get_order,
    ),
    (
        {
            "name": "get_my_trades",
            "description": "Get the authenticated user's trade history for a symbol. Requires 'read' scope.",
            "inputSchema": {
                "type": "object",
                "required": ["symbol"],
                "properties": {
                    "symbol": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
                },
            },
        },
        t_get_my_trades,
    ),
    (
        {
            "name": "place_order",
            "description": "Place a new order on the exchange. Requires the 'trade' scope. For Stop-Limit and OCO use the 'order_engine' tools instead.",
            "inputSchema": {
                "type": "object",
                "required": ["symbol", "side", "type", "quantity"],
                "properties": {
                    "symbol": {"type": "string", "description": "e.g. ETHUSDT"},
                    "side": {"type": "string", "enum": ["BUY", "SELL"]},
                    "type": {"type": "string", "enum": ["LIMIT", "MARKET"]},
                    "quantity": {"type": "string"},
                    "price": {"type": "string", "description": "required for LIMIT"},
                    "time_in_force": {
                        "type": "string",
                        "enum": ["GTC", "IOC", "FOK"],
                        "default": "GTC",
                    },
                    "client_order_id": {"type": "string"},
                },
            },
        },
        t_place_order,
    ),
    (
        {
            "name": "cancel_order",
            "description": "Cancel an open order by order_id or client_order_id. Requires 'trade' scope.",
            "inputSchema": {
                "type": "object",
                "required": ["symbol"],
                "properties": {
                    "symbol": {"type": "string"},
                    "order_id": {"type": ["string", "integer"]},
                    "client_order_id": {"type": "string"},
                },
            },
        },
        t_cancel_order,
    ),
    (
        {
            "name": "cancel_all_orders",
            "description": "Cancel all open orders for a symbol. Requires 'trade' scope.",
            "inputSchema": {
                "type": "object",
                "required": ["symbol"],
                "properties": {"symbol": {"type": "string"}},
            },
        },
        t_cancel_all_orders,
    ),
    (
        {
            "name": "get_chain_info",
            "description": "List supported on-chain networks (chain id, native asset, RPC, deposit address style).",
            "inputSchema": {"type": "object", "properties": {}},
        },
        t_get_chain_info,
    ),
    (
        {
            "name": "get_chain_balance",
            "description": "Get the user's on-chain balances on a given network. Requires 'read' scope.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "chain": {"type": "string", "description": "chain slug, e.g. 'demo', 'eth'"},
                },
            },
        },
        t_get_chain_balance,
    ),
    (
        {
            "name": "list_deposits",
            "description": "List the user's deposit history. Requires 'read' scope.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100}
                },
            },
        },
        t_list_deposits,
    ),
    (
        {
            "name": "list_withdraws",
            "description": "List the user's withdrawal history. Requires 'read' scope. Initiating withdrawals is intentionally NOT exposed via MCP — use the web UI.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100}
                },
            },
        },
        t_list_withdraws,
    ),
    (
        {
            "name": "get_pol_my_proof",
            "description": "Get a Proof-of-Liabilities Merkle inclusion proof for the authenticated user's account at the latest published epoch. Requires 'read' scope.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        t_get_pol_my_proof,
    ),
    (
        {
            "name": "get_pol_reserves",
            "description": "Get the latest published Proof-of-Reserves vs Liabilities certificate (totals by asset, signature). No authentication required.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        t_get_pol_reserves,
    ),
    (
        {
            "name": "get_anchor_status",
            "description": "Status of the on-chain anchor indexer that mirrors the zkPoL BulletinBoard.sol contract: configured/degraded mode, head block, scanned-to block, lag, latest batch hash, batch & commitment counts. No authentication required.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        t_get_anchor_status,
    ),
    (
        {
            "name": "get_my_commitment_history",
            "description": "Return the authenticated user's per-account Pedersen-commitment history as posted to the on-chain BulletinBoard contract: latest (x,y) curve point, batch hash, tx hash, block number, plus 'pending' wallet changes whose CommitmentPosted event hasn't landed yet. Requires 'read' scope and a bearer session (not an API key).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 25},
                },
            },
        },
        t_get_my_commitment_history,
    ),
    (
        {
            "name": "get_anchor_batch",
            "description": "Look up a single appendBatch batch from the on-chain BulletinBoard contract by its batch_hash (0x-prefixed 32-byte hex). Returns the batch's tokenKey, liability transition (old/new/delta), tx hash, block number, and every commitment update bundled into it. Public — anyone can audit a batch given its hash.",
            "inputSchema": {
                "type": "object",
                "required": ["batch_hash"],
                "properties": {
                    "batch_hash": {
                        "type": "string",
                        "description": "0x-prefixed 32-byte hex hash from BatchAppended event",
                    },
                },
            },
        },
        t_get_anchor_batch,
    ),
    (
        {
            "name": "get_safu_summary",
            "description": "Get the current state of the zkCEX SAFU self-insurance fund: per-asset balances, monthly inflow/payout, payout/inflow ratio, and the count of open vs resolved incidents. SAFU absorbs 10% of every trading fee to backstop users against hacks, system errors, and liquidation cascades. No authentication required.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        t_get_safu_summary,
    ),
    (
        {
            "name": "get_safu_attestation",
            "description": "Get an Ed25519-signed snapshot of the SAFU fund's totals + per-asset balances. The same custodian pubkey signs PoL/PoRL roots — an external verifier only has to trust one key to audit both. No authentication required.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        t_get_safu_attestation,
    ),
    (
        {
            "name": "get_my_referral_code",
            "description": "Get the authenticated user's referral code and a shareable signup link. The code auto-generates on first call. Requires 'read' scope.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        t_get_my_referral_code,
    ),
    (
        {
            "name": "get_referral_stats",
            "description": "Get the authenticated user's referral dashboard: total signups, qualified signups, lifetime earnings broken down by source (signup bonus, KYC bonus, first-trade bonus, fee-share level-1, fee-share level-2), and a redacted list of recent referees. Requires 'read' scope.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        t_get_referral_stats,
    ),
    (
        {
            "name": "get_referral_leaderboard",
            "description": "Get the top referrers globally over the last 30 days, ranked by USDT earnings. opex_user values are redacted to 'u-***'. No authentication required.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
            },
        },
        t_get_referral_leaderboard,
    ),
    (
        {
            "name": "customize_referral_code",
            "description": "Set a custom referral code (e.g. 'ALICE2026'). Must be 4-12 uppercase alphanumeric chars and unique across the platform. Can only be changed once — subsequent calls return error 'already_customized'. Requires 'trade' scope.",
            "inputSchema": {
                "type": "object",
                "required": ["custom_code"],
                "properties": {
                    "custom_code": {
                        "type": "string",
                        "description": "4-12 uppercase letters or digits, e.g. 'ALICE2026'",
                    },
                },
            },
        },
        t_customize_referral_code,
    ),
    (
        {
            "name": "get_fee_tiers",
            "description": "List all 10 zkCEX VIP fee tiers (level 0..9) with the 30-day volume + holdings requirements and the maker/taker basis-point fees for spot and futures. No authentication required.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        t_get_fee_tiers,
    ),
    (
        {
            "name": "get_my_fee_tier",
            "description": "Get the authenticated user's current VIP tier, 30-day trading volume, holdings, the gap to the next tier, and a list of recent trades with the fees paid. Requires 'read' scope.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        t_get_my_fee_tier,
    ),
    (
        {
            "name": "get_fee_quote",
            "description": "Estimate the maker and taker fees for a hypothetical order at the authenticated user's current VIP tier. Use before place_order to know exactly what fee you'll pay. Requires 'read' scope.",
            "inputSchema": {
                "type": "object",
                "required": ["market", "symbol", "side", "qty", "price"],
                "properties": {
                    "market": {"type": "string", "enum": ["spot", "futures"]},
                    "symbol": {"type": "string", "description": "e.g. ETHUSDT"},
                    "side": {"type": "string", "enum": ["BUY", "SELL"]},
                    "qty": {"type": "string"},
                    "price": {"type": "string"},
                },
            },
        },
        t_get_fee_quote,
    ),
    (
        {
            "name": "export_trades_csv",
            "description": "Export the authenticated user's trade history as a CSV (returned as base64). Requires bearer-mode auth and 'read' scope.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "from_ms": {"type": "integer", "description": "epoch ms inclusive"},
                    "to_ms": {"type": "integer", "description": "epoch ms exclusive"},
                },
            },
        },
        t_export_trades_csv,
    ),
    # ---- Perpetual-futures tools -----------------------------------------
    (
        {
            "name": "get_perp_markets",
            "description": "List all USDT-margined perpetual futures markets on zkCEX (e.g. ETHUSDT_PERP, BTCUSDT_PERP). Returns contract size, tick/step size, max leverage, maintenance margin rate, funding interval, and current trading status. No authentication required.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        t_get_perp_markets,
    ),
    (
        {
            "name": "get_premium_index",
            "description": "Get the mark price, index price, current funding rate, and next funding time for a perpetual contract. Accepts 'ETHUSDT' or 'ETHUSDT_PERP'. No authentication required.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "e.g. ETHUSDT or ETHUSDT_PERP"},
                },
            },
        },
        t_get_premium_index,
    ),
    (
        {
            "name": "get_funding_history",
            "description": "Get recent funding-rate history for a perpetual contract. No authentication required.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
                },
            },
        },
        t_get_funding_history,
    ),
    (
        {
            "name": "get_positions",
            "description": "Get the authenticated user's open perpetual positions: entry, mark, size, leverage, isolated margin, liquidation price, and unrealized PnL. Requires 'read' scope.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                },
            },
        },
        t_get_positions,
    ),
    (
        {
            "name": "get_perp_account",
            "description": "Get the authenticated user's perpetual-futures account: USDT wallet balance, available balance, total margin balance, total unrealized PnL, plus the positions list. Requires 'read' scope.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        t_get_perp_account,
    ),
    (
        {
            "name": "place_perp_order",
            "description": "Open or close a USDT-margined perpetual position. side=BUY opens a long (or reduces a short); side=SELL opens a short (or reduces a long). Demo: both MARKET and LIMIT settle immediately at the current mark price. Requires 'trade' scope.",
            "inputSchema": {
                "type": "object",
                "required": ["symbol", "side", "quantity"],
                "properties": {
                    "symbol": {"type": "string", "description": "e.g. ETHUSDT_PERP or ETHUSDT"},
                    "side": {"type": "string", "enum": ["BUY", "SELL"]},
                    "type": {"type": "string", "enum": ["MARKET", "LIMIT"], "default": "MARKET"},
                    "quantity": {
                        "type": "string",
                        "description": "Position size in base asset (e.g. 0.1 for 0.1 ETH)",
                    },
                    "price": {
                        "type": "string",
                        "description": "Required for LIMIT; ignored for MARKET",
                    },
                    "leverage": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "If set, also calls /fapi/v1/leverage before placing",
                    },
                    "reduce_only": {"type": "boolean", "default": False},
                    "client_order_id": {"type": "string"},
                },
            },
        },
        t_place_perp_order,
    ),
    (
        {
            "name": "close_position",
            "description": "Close the user's entire open perpetual position on a symbol at the current mark price. Requires 'trade' scope.",
            "inputSchema": {
                "type": "object",
                "required": ["symbol"],
                "properties": {"symbol": {"type": "string"}},
            },
        },
        t_close_position,
    ),
    (
        {
            "name": "transfer_to_futures",
            "description": "Move USDT from the user's spot wallet to their futures wallet. Requires 'trade' scope.",
            "inputSchema": {
                "type": "object",
                "required": ["amount"],
                "properties": {
                    "amount": {"type": "string", "description": "USDT amount (whole units)"},
                },
            },
        },
        t_transfer_to_futures,
    ),
    (
        {
            "name": "transfer_to_spot",
            "description": "Move USDT from the user's futures wallet back to their spot wallet. Limited to the user's available_balance (wallet - used isolated margin). Requires 'trade' scope.",
            "inputSchema": {
                "type": "object",
                "required": ["amount"],
                "properties": {
                    "amount": {"type": "string", "description": "USDT amount (whole units)"},
                },
            },
        },
        t_transfer_to_spot,
    ),
    (
        {
            "name": "zk_info",
            "description": "Public phase + batch state for zkCEX privacy trading (commit-reveal FBA). Returns current batch_id, phase ('commit' or 'reveal'), time_left_in_phase, and n_committed/n_revealed counts.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Optional filter (ETHUSDT, BTCUSDT).",
                    },
                },
            },
        },
        t_zk_info,
    ),
    (
        {
            "name": "zk_commit_order",
            "description": "Submit a privacy-preserving order via commit-reveal. Generates a random 32-byte salt server-side, computes SHA-256 commitment, and posts the hash. Only callable during the commit phase. Requires 'trade' scope.",
            "inputSchema": {
                "type": "object",
                "required": ["symbol", "side", "price", "quantity"],
                "properties": {
                    "symbol": {"type": "string", "description": "ETHUSDT or BTCUSDT"},
                    "side": {"type": "string", "enum": ["BUY", "SELL"]},
                    "price": {"type": "string"},
                    "quantity": {"type": "string"},
                    "expires_in_seconds": {
                        "type": "integer",
                        "default": 30,
                        "description": "Hint only; the server uses window_end.",
                    },
                },
            },
        },
        t_zk_commit_order,
    ),
    (
        {
            "name": "zk_reveal_order",
            "description": "Reveal a previously-committed privacy order. Looks up the preimage cached during zk_commit_order and submits it to /zk-trade/reveal. Requires 'trade' scope.",
            "inputSchema": {
                "type": "object",
                "required": ["commitment_hex"],
                "properties": {
                    "commitment_hex": {
                        "type": "string",
                        "description": "64-char hex digest returned by zk_commit_order",
                    },
                },
            },
        },
        t_zk_reveal_order,
    ),
    (
        {
            "name": "zk_get_batches",
            "description": "Public batch history for the privacy trading sub-system. Each batch carries an Ed25519-signed Merkle root over its reveals plus the uniform clearing price.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 10},
                },
            },
        },
        t_zk_get_batches,
    ),
    (
        {
            "name": "zk_my_commits",
            "description": "List the authenticated user's privacy commits, reveals, and matched trades. Requires 'read' scope.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        t_zk_my_commits,
    ),
    (
        {
            "name": "list_my_notifications",
            "description": (
                "List the authenticated user's recent in-app notifications, newest first. "
                "Each row carries id, type, category, title, body, metadata, channels, "
                "read flag, and created_at. Requires a user Bearer token."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                },
            },
        },
        t_list_my_notifications,
    ),
    (
        {
            "name": "mark_notification_read",
            "description": (
                "Mark a single in-app notification as read by id. Idempotent; "
                "no-op if already read. Requires a user Bearer token."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["notification_id"],
                "properties": {
                    "notification_id": {"type": "integer"},
                },
            },
        },
        t_mark_notification_read,
    ),
    (
        {
            "name": "create_price_alert",
            "description": (
                "Create a one-shot price alert. When the last-trade price for "
                "`symbol` crosses the `threshold` in the chosen direction, "
                "the user receives a notification (in-app + push + email, "
                "subject to their preferences). Max 20 active alerts per user."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["symbol", "condition", "threshold"],
                "properties": {
                    "symbol": {"type": "string", "description": "e.g. ETHUSDT"},
                    "condition": {"type": "string", "enum": ["above", "below"]},
                    "threshold": {
                        "type": ["string", "number"],
                        "description": "price level to compare against",
                    },
                },
            },
        },
        t_create_price_alert,
    ),
    (
        {
            "name": "list_staking_products",
            "description": "Public catalogue of staking (Earn) products: flexible and fixed-term lock-ups for ETH/USDT/BTC with APY, minimum stake, capacity, and early-unstake penalty. No authentication required.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        t_list_staking_products,
    ),
    (
        {
            "name": "get_my_staking_positions",
            "description": "List the authenticated user's staking positions (active + matured pending + recently redeemed) with accrued yield. Requires 'read' scope.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        t_get_my_staking_positions,
    ),
    (
        {
            "name": "stake",
            "description": "Lock 'amount' of the product's asset into 'product_id' to start earning yield. Debits the user's MAIN wallet immediately. APY at stake-time is locked in for the life of the position. Requires 'trade' scope.",
            "inputSchema": {
                "type": "object",
                "required": ["product_id", "amount"],
                "properties": {
                    "product_id": {
                        "type": "string",
                        "description": "e.g. ETH-FLEX, ETH-30D, ETH-90D, USDT-FLEX, BTC-90D",
                    },
                    "amount": {
                        "type": ["string", "number"],
                        "description": "Amount of the product's asset to stake (Decimal as string preferred).",
                    },
                },
            },
        },
        t_stake,
    ),
    (
        {
            "name": "redeem_staking",
            "description": "Close a staking position. Use for matured fixed-term positions or flexible-term withdrawals (instant). For pre-maturity fixed-term, set force_early=true to exit with the early-unstake penalty. The redeemed amount (principal + net yield - any penalty) is credited to the user's MAIN wallet. Requires 'trade' scope.",
            "inputSchema": {
                "type": "object",
                "required": ["position_id"],
                "properties": {
                    "position_id": {
                        "type": ["integer", "string"],
                        "description": "Position id from get_my_staking_positions.",
                    },
                    "force_early": {
                        "type": "boolean",
                        "default": False,
                        "description": "Set true to exit a fixed-term position before maturity (penalty applies).",
                    },
                },
            },
        },
        t_redeem_staking,
    ),
    # ---- Lending / Earn pool tools ---------------------------------------
    (
        {
            "name": "get_lending_pools",
            "description": "Public per-asset lending-pool state: total supplied, total borrowed, utilisation, supply APY, borrow APY, collateral factor (max LTV), liquidation threshold, and reserves. Compound v2-style two-slope interest model. No authentication required.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        t_get_lending_pools,
    ),
    (
        {
            "name": "get_my_lending_positions",
            "description": "List the authenticated user's open supply + borrow positions on zkCEX Earn. Returns per-position principal, accrued interest, collateral, current LTV, liquidation price, and distance-to-liquidation health band (green/amber/red). Requires 'read' scope.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        t_get_my_lending_positions,
    ),
    (
        {
            "name": "supply",
            "description": "Supply 'amount' of 'asset' (USDT/ETH/BTC) into the corresponding lending pool to start earning the live supply APY. Debits the user's MAIN spot wallet immediately. The position keeps accruing interest until withdrawn. Requires 'trade' scope.",
            "inputSchema": {
                "type": "object",
                "required": ["asset", "amount"],
                "properties": {
                    "asset": {
                        "type": "string",
                        "description": "Pool asset: one of USDT, ETH, BTC",
                    },
                    "amount": {
                        "type": ["string", "number"],
                        "description": "Amount of the pool asset to supply (Decimal as string preferred).",
                    },
                },
            },
        },
        t_lending_supply,
    ),
    (
        {
            "name": "borrow",
            "description": "Open a collateralised borrow position. Locks 'collateral_amount' of 'collateral_asset' into the lending vault and credits 'borrow_amount' of 'borrowed_asset' to the user's MAIN wallet. The LTV (debt_usdt / collateral_usdt) must be at or under the collateral pool's collateral_factor at open time. If LTV later exceeds the liquidation_threshold the position is force-closed by the liquidation engine. Requires 'trade' scope, KYC verified for any meaningful borrow size.",
            "inputSchema": {
                "type": "object",
                "required": [
                    "borrowed_asset",
                    "borrow_amount",
                    "collateral_asset",
                    "collateral_amount",
                ],
                "properties": {
                    "borrowed_asset": {
                        "type": "string",
                        "description": "Asset to borrow: USDT, ETH, or BTC",
                    },
                    "borrow_amount": {
                        "type": ["string", "number"],
                        "description": "Amount to borrow (Decimal string preferred)",
                    },
                    "collateral_asset": {
                        "type": "string",
                        "description": "Asset to post as collateral; must differ from borrowed_asset",
                    },
                    "collateral_amount": {
                        "type": ["string", "number"],
                        "description": "Amount of collateral to lock",
                    },
                },
            },
        },
        t_lending_borrow,
    ),
    # ---- NFT marketplace tools -------------------------------------------
    (
        {
            "name": "list_nft_collections",
            "description": "List the NFT collections live on the zkCEX hardhat-deployed marketplace. Each row returns the contract address, ERC standard (erc721/erc1155), name, symbol, floor price, total volume, active-listing count, and a sample thumbnail. No authentication required.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        t_list_nft_collections,
    ),
    (
        {
            "name": "list_nfts",
            "description": "Browse active NFT marketplace listings, optionally filtered by collection (contract address), price range, asset (USDT|ETH), or ERC standard (erc721|erc1155). Sort by 'recent', 'price_asc', 'price_desc', or 'oldest'. No authentication required.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "collection": {
                        "type": "string",
                        "description": "Contract address to filter to",
                    },
                    "min_price": {"type": ["string", "number"]},
                    "max_price": {"type": ["string", "number"]},
                    "asset": {"type": "string", "enum": ["USDT", "ETH"]},
                    "standard": {"type": "string", "enum": ["erc721", "erc1155"]},
                    "sort": {
                        "type": "string",
                        "enum": ["recent", "price_asc", "price_desc", "oldest"],
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                },
            },
        },
        t_list_nfts,
    ),
    (
        {
            "name": "get_my_nfts",
            "description": "List the NFTs the authenticated user currently owns on-chain. Reads ownerOf (ERC721) / balanceOf (ERC1155) directly from the hardhat node. Requires 'read' scope.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        t_get_my_nfts,
    ),
    (
        {
            "name": "list_nft_for_sale",
            "description": "Put one of the user's NFTs up for sale. Verifies on-chain ownership, transfers the token into custodial escrow, and creates an active listing. The seller is paid (price − 2.5% platform fee) when a buy or accepted bid completes. Requires 'trade' scope and a derived chain wallet (visit /app/wallet.html once).",
            "inputSchema": {
                "type": "object",
                "required": ["contract_address", "token_id", "list_price"],
                "properties": {
                    "contract_address": {
                        "type": "string",
                        "description": "NFT contract (e.g. ZkNFT or ZkNFT1155)",
                    },
                    "token_id": {"type": ["string", "integer"]},
                    "quantity": {
                        "type": ["string", "integer"],
                        "default": 1,
                        "description": "Always 1 for ERC721",
                    },
                    "list_price": {
                        "type": ["string", "number"],
                        "description": "Whole-unit price (e.g. 100 for 100 USDT)",
                    },
                    "list_asset": {"type": "string", "enum": ["USDT", "ETH"], "default": "USDT"},
                    "expires_in_days": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 60,
                        "default": 7,
                    },
                },
            },
        },
        t_list_nft_for_sale,
    ),
    (
        {
            "name": "buy_nft",
            "description": "Buy a listed NFT atomically: debit the buyer's MAIN wallet for the full listing price, credit the seller proceeds (price − 2.5% platform fee), and transfer the NFT on-chain from custodial escrow to the buyer's derived chain wallet. Rolls back the wallet debit if the chain transfer fails. Requires 'trade' scope.",
            "inputSchema": {
                "type": "object",
                "required": ["listing_id"],
                "properties": {
                    "listing_id": {
                        "type": ["integer", "string"],
                        "description": "Marketplace listing id (from list_nfts)",
                    },
                },
            },
        },
        t_buy_nft,
    ),
]

TOOLS_BY_NAME = {t[0]["name"]: t for t in TOOLS}


# ============================================================================
# Resources. Every URI here is read-only.
# ============================================================================


def res_markets(ident: Identity) -> dict:
    rows = t_list_markets(ident, {})
    return {
        "contents": [
            {
                "uri": "zkcex://markets",
                "mimeType": "application/json",
                "text": json.dumps(rows, indent=2),
            }
        ]
    }


def res_account(ident: Identity, opex: str) -> dict:
    if not ident.opex_user:
        raise ToolError(-32002, "Not authenticated.")
    if opex and opex != ident.opex_user:
        raise ToolError(
            -32001,
            "You can only read your own account snapshot.",
            data={"requested": opex, "have": ident.opex_user},
        )
    bal = t_get_balance(ident, {})
    open_orders = []
    try:
        open_orders = t_get_open_orders(ident, {})
    except ToolError:
        pass
    body = {
        "opex_user": ident.opex_user,
        "balances": bal,
        "open_orders": open_orders,
    }
    return {
        "contents": [
            {
                "uri": f"zkcex://account/{ident.opex_user}",
                "mimeType": "application/json",
                "text": json.dumps(body, indent=2),
            }
        ]
    }


def res_pol_latest(ident: Identity) -> dict:
    body = t_get_pol_reserves(ident, {})
    return {
        "contents": [
            {
                "uri": "zkcex://pol/latest",
                "mimeType": "application/json",
                "text": json.dumps(body, indent=2),
            }
        ]
    }


AUTH_DOC = """\
# zkCEX MCP — Authentication

The zkCEX MCP server speaks JSON-RPC 2.0 over either:

  * **stdio** — line-delimited JSON, one envelope per line. Used by
    Claude Desktop and Cursor when configured locally.
  * **HTTP** on port 5560. Browser/agent-friendly. POST `/` for
    request/response, `GET /stream` for SSE notifications, `GET /health`
    for liveness, `GET /calls` for the audit log (Bearer-required).

## Identifying the agent

* **stdio (local Claude Desktop / Cursor):** export `MCP_OPEX_USER` in
  the `env` block of your client's MCP config, OR `MCP_API_KEY` +
  `MCP_API_SECRET` from your zkCEX keys page. Trading and withdraw scopes
  are inherited from the API key.
* **HTTP `--auth bearer`:** every request must carry
  `Authorization: Bearer <session-token>`. Get a token from
  `POST /auth/login` on the zkCEX site.
* **HTTP `--auth api-key`:** every request must carry
  `X-MCP-API-KEY` and `X-MCP-API-SECRET`. The same key works as the
  Binance-compatible signed-request key on `/v3/*`.

## Scopes

Tools that mutate the exchange or trigger on-chain transfers check
scopes before forwarding. If your identity lacks the scope, you get a
JSON-RPC error with code `-32001` listing the scopes you DO have, so the
agent can ask the user to upgrade their key.

  * `read`     — markets, balances, history (covers `get_balance`,
                 `get_open_orders`, `get_my_trades`, `list_deposits`,
                 `list_withdraws`, `get_chain_balance`, all `get_pol_*`
                 tools, and `export_trades_csv`).
  * `trade`    — `place_order`, `cancel_order`, `cancel_all_orders`.
  * `withdraw` — initiating an on-chain withdraw is **not** exposed via
                 MCP at all in this build. Use the web UI.
"""


def res_docs_auth(ident: Identity) -> dict:
    return {
        "contents": [
            {
                "uri": "zkcex://docs/auth",
                "mimeType": "text/markdown",
                "text": AUTH_DOC,
            }
        ]
    }


def list_resources(ident: Identity) -> list[dict]:
    items = [
        {
            "uri": "zkcex://markets",
            "name": "All markets",
            "description": "Static list of tradable symbols.",
            "mimeType": "application/json",
        },
        {
            "uri": "zkcex://pol/latest",
            "name": "Proof-of-Liabilities (latest)",
            "description": "The latest published reserves-vs-liabilities certificate.",
            "mimeType": "application/json",
        },
        {
            "uri": "zkcex://docs/auth",
            "name": "Authentication guide",
            "description": "How agents identify themselves and which scopes each tool needs.",
            "mimeType": "text/markdown",
        },
    ]
    if ident.opex_user:
        items.append(
            {
                "uri": f"zkcex://account/{ident.opex_user}",
                "name": "Account snapshot",
                "description": "Live balances + open orders for the authenticated user.",
                "mimeType": "application/json",
            }
        )
    return items


def read_resource(ident: Identity, uri: str) -> dict:
    if uri == "zkcex://markets":
        return res_markets(ident)
    if uri == "zkcex://pol/latest":
        return res_pol_latest(ident)
    if uri == "zkcex://docs/auth":
        return res_docs_auth(ident)
    if uri.startswith("zkcex://account/"):
        opex = uri[len("zkcex://account/") :]
        return res_account(ident, opex)
    raise ToolError(-32602, f"unknown resource uri: {uri}")


# ============================================================================
# JSON-RPC dispatcher. The same function services stdio and HTTP, modulo a
# transport label and the per-request Identity.
# ============================================================================


def _err(eid: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": eid, "error": err}


def _ok(eid: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": eid, "result": result}


def _server_capabilities() -> dict:
    return {
        "tools": {"listChanged": False},
        "resources": {"listChanged": False, "subscribe": False},
        "logging": {},
    }


# Per-session client info — captured from the ``initialize`` call so we
# can attribute tool-calls to e.g. "claude-desktop/0.7.4" in the audit
# log. Indexed by a synthetic session id we mint on initialize.
_SESSIONS: dict[str, dict] = {}


def _format_tool_result(payload: Any) -> dict:
    """MCP tools/call shape: {"content":[{type:"text",text:"..."}], "isError":false}"""
    if isinstance(payload, (dict, list)):
        text = json.dumps(payload, indent=2, default=str)
    else:
        text = str(payload)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def dispatch(
    envelope: dict, ident: Identity, transport: str, session: dict | None = None
) -> dict | None:
    """Handle one JSON-RPC envelope. Returns the response envelope, or
    None for notifications (no id present). Never raises — every error
    becomes a JSON-RPC error envelope."""
    method = envelope.get("method")
    eid = envelope.get("id")
    params = envelope.get("params") or {}
    is_notification = "id" not in envelope or eid is None

    started = time.time()
    tool_name = None
    status_label = "ok"
    error_msg: str | None = None
    response: dict | None = None

    try:
        if method == "initialize":
            client = params.get("clientInfo") or {}
            client_label = f"{client.get('name','?')}/{client.get('version','?')}"
            if session is not None:
                session["client_label"] = client_label
                session["initialized"] = True
            response = _ok(
                eid,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": _server_capabilities(),
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "version": SERVER_VERSION,
                    },
                    "instructions": (
                        "zkCEX MCP server. Public market data is available without "
                        "auth. Trading and account tools require the user's API key "
                        "in MCP_API_KEY/MCP_API_SECRET env, or a bearer token on the "
                        "HTTP transport."
                    ),
                },
            )
        elif method == "ping":
            response = _ok(eid, {})
        elif method == "tools/list":
            response = _ok(eid, {"tools": [t[0] for t in TOOLS]})
        elif method == "tools/call":
            tool_name = params.get("name")
            args = params.get("arguments") or {}
            if not tool_name or tool_name not in TOOLS_BY_NAME:
                response = _err(eid, -32601, f"unknown tool: {tool_name!r}")
            else:
                _, handler = TOOLS_BY_NAME[tool_name]
                try:
                    payload = handler(ident, args)
                    response = _ok(eid, _format_tool_result(payload))
                except ToolError as te:
                    response = _err(eid, te.code, te.message, te.data)
                    status_label = "error"
                    error_msg = te.message
                except Exception as e:  # noqa: BLE001
                    response = _err(
                        eid,
                        -32603,
                        f"tool {tool_name} crashed: {e!r}",
                        data=traceback.format_exc(limit=3),
                    )
                    status_label = "error"
                    error_msg = repr(e)
        elif method == "resources/list":
            response = _ok(eid, {"resources": list_resources(ident)})
        elif method == "resources/read":
            try:
                response = _ok(eid, read_resource(ident, params.get("uri") or ""))
            except ToolError as te:
                response = _err(eid, te.code, te.message, te.data)
                status_label = "error"
                error_msg = te.message
        elif method == "notifications/initialized":
            # A notification from the client — no response.
            return None
        elif method == "shutdown":
            response = _ok(eid, {})
        else:
            response = _err(eid, -32601, f"method not found: {method!r}")
            status_label = "error"
            error_msg = f"method not found: {method!r}"
    except Exception as e:  # noqa: BLE001 — last-resort safety net
        response = _err(eid, -32603, f"internal error: {e!r}", data=traceback.format_exc(limit=3))
        status_label = "error"
        error_msg = repr(e)

    duration_ms = int((time.time() - started) * 1000)

    # Don't log the noisy initialize handshakes? Keep them — useful for
    # debugging which client connected.
    if not is_notification and method:
        log_call(
            transport=transport,
            client_label=(session or {}).get("client_label"),
            method=method,
            tool=tool_name,
            opex_user=ident.opex_user,
            request=envelope,
            status=status_label,
            error=error_msg,
            duration_ms=duration_ms,
        )

    if is_notification:
        return None
    return response


# ============================================================================
# stdio transport. One JSON envelope per line, both directions.
# ============================================================================


def run_stdio() -> None:
    ident = env_identity()
    session = {"client_label": None, "initialized": False}
    sys.stderr.write(f"[mcp] stdio transport ready. identity={ident}\n")
    sys.stderr.flush()

    # Use a binary-mode reader to avoid any platform-specific newline
    # rewriting; we then decode/strip explicitly.
    in_buf = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", newline="\n")
    out = sys.stdout

    for raw in in_buf:
        line = raw.strip()
        if not line:
            continue
        try:
            env = json.loads(line)
        except json.JSONDecodeError as e:
            err = _err(None, -32700, f"parse error: {e!s}")
            out.write(json.dumps(err) + "\n")
            out.flush()
            continue
        if isinstance(env, list):
            # Batch — process each, emit a JSON array of responses.
            responses = [
                r for r in (dispatch(e, ident, "stdio", session) for e in env) if r is not None
            ]
            if responses:
                out.write(json.dumps(responses) + "\n")
                out.flush()
            continue
        if not isinstance(env, dict):
            out.write(json.dumps(_err(None, -32600, "invalid request")) + "\n")
            out.flush()
            continue
        resp = dispatch(env, ident, "stdio", session)
        if resp is not None:
            out.write(json.dumps(resp) + "\n")
            out.flush()


# ============================================================================
# HTTP / SSE transport. POST / for request-response, GET /stream for SSE,
# GET /health for liveness, GET /calls for the audit log.
# ============================================================================


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_http_handler(auth_mode: str):
    auth_mode = auth_mode or "none"

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = f"{SERVER_NAME}/{SERVER_VERSION}"

        def log_message(self, fmt, *args):
            sys.stderr.write(f"[mcp.http] {self.address_string()} - {fmt % args}\n")

        # ----- helpers -----
        def _read_body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n > 0 else b""

        def _send_json(self, code: int, body: Any) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, Authorization, X-MCP-API-KEY, X-MCP-API-SECRET",
            )
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            self.wfile.write(data)

        def _resolve_identity(self) -> tuple[Identity | None, str | None]:
            """Returns (Identity, error_message). If error_message is set,
            send back a -32002 (auth) error to the agent."""
            if auth_mode == "none":
                return env_identity(), None
            if auth_mode == "bearer":
                hdr = self.headers.get("Authorization") or ""
                if not hdr.lower().startswith("bearer "):
                    return None, (
                        "HTTP transport is in --auth bearer mode. "
                        "Send Authorization: Bearer <session-token>."
                    )
                tok = hdr[len("Bearer ") :].strip()
                ident = resolve_bearer(tok)
                if not ident:
                    return None, "bearer token rejected by /auth/me"
                return ident, None
            if auth_mode == "api-key":
                ak = self.headers.get("X-MCP-API-KEY") or ""
                asec = self.headers.get("X-MCP-API-SECRET") or ""
                if not ak:
                    return None, (
                        "HTTP transport is in --auth api-key mode. "
                        "Send X-MCP-API-KEY (and X-MCP-API-SECRET)."
                    )
                ident = resolve_api_key(ak, asec)
                return ident, None
            return None, f"unknown auth mode: {auth_mode}"

        # ----- routes -----
        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, Authorization, X-MCP-API-KEY, X-MCP-API-SECRET",
            )
            self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/health", "/mcp/health"):
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "name": SERVER_NAME,
                        "version": SERVER_VERSION,
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "auth_mode": auth_mode,
                    },
                )
                return
            if path in ("/stream", "/mcp/stream"):
                return self._serve_sse()
            if path in ("/calls", "/mcp/calls"):
                return self._serve_calls()
            if path in ("/", "/mcp", "/mcp/"):
                # GET on the JSON-RPC endpoint isn't part of the protocol;
                # return a tiny hint instead of 404.
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "hint": "POST a JSON-RPC 2.0 envelope here.",
                        "transports": ["http", "sse:/stream"],
                    },
                )
                return
            self._send_json(404, {"ok": False, "error": "not_found", "path": path})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path not in ("/", "/mcp", "/mcp/"):
                self._send_json(404, {"ok": False, "error": "not_found", "path": path})
                return
            ident, err = self._resolve_identity()
            if err and ident is None:
                self._send_json(200, _err(None, -32002, err))
                return
            if ident is None:
                self._send_json(200, _err(None, -32002, "identity resolution failed"))
                return
            try:
                env = json.loads(self._read_body() or b"null")
            except json.JSONDecodeError as e:
                self._send_json(200, _err(None, -32700, f"parse error: {e!s}"))
                return
            if isinstance(env, list):
                resps = [r for r in (dispatch(e, ident, "http") for e in env) if r is not None]
                self._send_json(200, resps)
                return
            if not isinstance(env, dict):
                self._send_json(200, _err(None, -32600, "invalid request"))
                return
            resp = dispatch(env, ident, "http")
            if resp is None:
                # Notification — JSON-RPC says respond with 204.
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                return
            self._send_json(200, resp)

        # ----- SSE channel -----
        def _serve_sse(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                # Initial hello, then a heartbeat every 25s.
                hello = {
                    "jsonrpc": "2.0",
                    "method": "notifications/server_hello",
                    "params": {"name": SERVER_NAME, "version": SERVER_VERSION},
                }
                self.wfile.write(f"event: message\ndata: {json.dumps(hello)}\n\n".encode())
                self.wfile.flush()
                while True:
                    time.sleep(25.0)
                    hb = {
                        "jsonrpc": "2.0",
                        "method": "notifications/heartbeat",
                        "params": {"ts": _now_ms()},
                    }
                    self.wfile.write(f"event: message\ndata: {json.dumps(hb)}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

        # ----- audit log read -----
        def _serve_calls(self) -> None:
            # Bearer-required regardless of the server's auth_mode (we
            # don't want anonymous readers in stdio's --auth none case).
            hdr = self.headers.get("Authorization") or ""
            if not hdr.lower().startswith("bearer "):
                self._send_json(401, {"ok": False, "error": "bearer_required"})
                return
            tok = hdr[len("Bearer ") :].strip()
            if not resolve_bearer(tok):
                self._send_json(403, {"ok": False, "error": "bearer_rejected"})
                return
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            try:
                limit = int((qs.get("limit") or ["50"])[0])
            except ValueError:
                limit = 50
            self._send_json(200, {"ok": True, "calls": recent_calls(limit)})

    return Handler


def run_http(port: int, auth_mode: str) -> None:
    handler = make_http_handler(auth_mode)
    srv = _ThreadedHTTPServer(("127.0.0.1", port), handler)
    sys.stderr.write(
        f"[mcp] HTTP transport listening on http://127.0.0.1:{port} " f"(auth={auth_mode})\n"
    )
    sys.stderr.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


# ============================================================================
# CLI
# ============================================================================


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="zkCEX MCP server")
    p.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    p.add_argument("--port", type=int, default=5560)
    p.add_argument(
        "--auth",
        choices=("none", "bearer", "api-key"),
        default=None,
        help="HTTP auth mode. Default: none (trust local env).",
    )
    args = p.parse_args(argv)
    _log_init()
    if args.transport == "stdio":
        run_stdio()
        return 0
    auth_mode = args.auth or "none"
    run_http(args.port, auth_mode)
    return 0


if __name__ == "__main__":
    sys.exit(main())
