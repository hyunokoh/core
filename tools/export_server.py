#!/usr/bin/env python3
"""zkCEX export service — CSV downloads for trades, deposits, withdrawals,
orders, balance history, and a draft tax / cost-basis report.

The service is intentionally a thin aggregator: it pulls live data from the
existing services (market service on :8096, chain_server on :5502, wallet API
on :8091, auth_server on :5501), reshapes the rows into Excel-friendly CSV,
and streams them straight back to the caller. Nothing is persisted — this is
on-demand, generated-per-request, no on-disk caching.

Routes:
    GET /export/index.json
    GET /export/trades.csv
    GET /export/deposits.csv
    GET /export/withdraws.csv
    GET /export/orders.csv
    GET /export/balance-history.csv
    GET /export/tax.csv

Auth: every CSV route requires a Bearer token. The service validates the
token against /auth/me once (with a 60s in-process cache) and uses the
resolved opex_user as the scope for downstream queries.

The service is stdlib-only (no pip deps): http.server, csv, urllib, threading.
Default listen port: 5540.

Mount under /export/ in tools/serve_homepage.py:
    "/export/": "http://127.0.0.1:5540"

Deliberate non-goals:
- No persistence. Two consecutive exports of the same window may differ if a
  new trade/deposit lands in between.
- No fee-aware tax math beyond commission accounting on the disposal leg.
  The cost-basis report is labelled as a working draft, not tax advice.
- No streaming pagination from the upstream market service. Trades are
  fetched in one POST /v1/user/{opex}/trades call with limit=1000 per
  symbol. A user with >1k trades on a single symbol will see the most
  recent 1000 in this build (clearly noted in the README of the brief).
"""

from __future__ import annotations

import csv
import http.server
import io
import json
import os
import socketserver
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, getcontext

# Set a comfortable precision for cost-basis arithmetic. Far higher than any
# real exchange ever needs, but cheap and avoids surprise rounding.
getcontext().prec = 40

# --- Upstream service base URLs. All resolve via 127.0.0.1 so the export
# service is self-contained on the demo box. Override via env if you ever
# move the components apart.


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
MARKET_BASE = _validated_http_base_url(
    "MARKET_BASE", os.environ.get("MARKET_BASE", "http://127.0.0.1:8096")
)
WALLET_BASE = _validated_http_base_url(
    "WALLET_BASE", os.environ.get("WALLET_BASE", "http://127.0.0.1:8091")
)
CHAIN_BASE = _validated_http_base_url(
    "CHAIN_BASE", os.environ.get("CHAIN_BASE", "http://127.0.0.1:5502")
)
GW_BASE = _validated_http_base_url(
    "GW_BASE", os.environ.get("GW_BASE", "http://127.0.0.1:8093")
)  # matching-gateway
COND_BASE = os.environ.get(
    "COND_BASE", "http://127.0.0.1:5520"
)  # order_engine.py conditional watcher
COND_BASE = _validated_http_base_url("COND_BASE", COND_BASE)

# --- Symbols we ship trade exports for by default. Real exchanges would pull
# this from /v3/exchangeInfo, but for the demo a static list is enough — the
# CSV stream just iterates them. Anything missing simply yields zero rows.
DEFAULT_SYMBOLS = ["ETH_USDT", "BTC_USDT", "SOL_USDT", "DOGE_USDT", "TON_USDT"]

# --- Per-token /auth/me cache. Keyed by raw bearer token string -> (when, user).
_AUTH_CACHE: dict[str, tuple[float, dict]] = {}
_AUTH_CACHE_LOCK = threading.Lock()
AUTH_TTL = 60.0  # seconds

# UTF-8 BOM so Excel auto-detects encoding for files containing Korean.
BOM = "﻿"

# Disclaimer text for the tax CSV header.
TAX_DISCLAIMER = (
    "This is a working draft, not tax advice. Consult a CPA. / "
    "이 자료는 자동 계산된 참고용 산출물입니다. 정확한 신고는 회계사 또는 세무사와 상의하세요."
)


# ============================================================================
# Helpers
# ============================================================================


def iso_utc(ms_or_dt) -> str:
    """Format an epoch-ms timestamp (or anything Date-like with .time()) as
    ISO-8601 UTC, e.g. ``2026-05-10T11:32:00Z``. Returns "" on falsy input.
    Spreadsheet apps reliably parse this format."""
    if ms_or_dt in (None, "", 0):
        return ""
    try:
        ms = int(ms_or_dt)
    except (TypeError, ValueError):
        # Maybe an ISO string already; if so, normalize to UTC Z form.
        try:
            dt = datetime.fromisoformat(str(ms_or_dt).replace("Z", "+00:00"))
        except Exception:
            return str(ms_or_dt)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Detect seconds vs milliseconds.
    if ms < 10_000_000_000:  # < year 2286 in seconds; assume seconds
        secs = ms
    else:
        secs = ms / 1000.0
    return datetime.fromtimestamp(secs, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_epoch_ms(v) -> int:
    """Coerce a timestamp-like value to epoch milliseconds.

    Accepts: epoch-seconds int, epoch-ms int, ISO-8601 string with or without
    a timezone offset. Returns 0 on parse failure, so sort keys stay valid
    even on garbage input from upstream.
    """
    if v in (None, "", 0):
        return 0
    if isinstance(v, (int, float)):
        n = int(v)
        return n if n >= 10_000_000_000 else n * 1000
    s = str(v).strip()
    if not s:
        return 0
    if s.isdigit():
        n = int(s)
        return n if n >= 10_000_000_000 else n * 1000
    try:
        # datetime.fromisoformat (3.9) needs a 'Z' rewrite to '+00:00'.
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def date_utc(ms_or_dt) -> str:
    if ms_or_dt in (None, "", 0):
        return ""
    try:
        ms = int(ms_or_dt)
    except (TypeError, ValueError):
        return str(ms_or_dt)[:10]
    secs = ms if ms < 10_000_000_000 else ms / 1000.0
    return datetime.fromtimestamp(secs, tz=timezone.utc).strftime("%Y-%m-%d")


def dec(v) -> Decimal:
    """Coerce a number-or-string to Decimal. Empty / None / bad -> 0."""
    if v in (None, "", "null"):
        return Decimal(0)
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def fmt_dec(d: Decimal) -> str:
    """Plain-decimal string (no scientific notation, no locale separators)."""
    if d == 0:
        return "0"
    # Decimal stringifies as e+nn for small magnitudes; format() with 'f' avoids it.
    s = format(d, "f")
    # Trim trailing zeros after a decimal point but keep at least one digit
    # before the point.
    if "." in s:
        s = s.rstrip("0").rstrip(".")
        if s == "" or s == "-":
            s = "0"
    return s


def parse_symbol(symbol: str) -> tuple[str, str]:
    """Split 'ETH_USDT' into ('ETH','USDT'). Falls back to (symbol,'USDT')."""
    if not symbol:
        return ("", "USDT")
    if "_" in symbol:
        a, b = symbol.split("_", 1)
        return (a.upper(), b.upper())
    # Heuristic for un-delimited symbols like 'ETHUSDT'.
    for q in ("USDT", "BUSD", "USDC", "BTC", "ETH"):
        if symbol.upper().endswith(q) and len(symbol) > len(q):
            return (symbol[: -len(q)].upper(), q)
    return (symbol.upper(), "USDT")


def http_get_json(url: str, *, headers: dict | None = None, timeout: float = 8.0):
    req = _http_request(url, headers=headers or {})
    with _http_urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "null")


def http_post_json(
    url: str, body: dict | list, *, headers: dict | None = None, timeout: float = 12.0
):
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        h.update(headers)
    data = json.dumps(body).encode("utf-8")
    req = _http_request(url, data=data, method="POST", headers=h)
    with _http_urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "null")


def resolve_user(token: str) -> dict | None:
    """Bearer -> user object, with a 60s in-process cache. None on 401 or
    transport failure."""
    if not token:
        return None
    now = time.time()
    with _AUTH_CACHE_LOCK:
        cached = _AUTH_CACHE.get(token)
        if cached and now - cached[0] < AUTH_TTL:
            return cached[1]
    try:
        body = http_get_json(
            f"{AUTH_BASE}/auth/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
    except urllib.error.HTTPError:
        return None
    except Exception:
        return None
    user = (body or {}).get("user")
    if not user or not user.get("opex_user"):
        return None
    with _AUTH_CACHE_LOCK:
        _AUTH_CACHE[token] = (now, user)
    return user


# ============================================================================
# Upstream fetch wrappers — each returns a normalized list of dicts with the
# fields the CSV writers consume. Failures upstream are swallowed and a
# synthetic ``__error__`` row is emitted instead, so a 502 in one slice
# doesn't abort the whole stream.
# ============================================================================


def fetch_user_trades(
    opex: str, symbol: str | None, from_ms: int | None, to_ms: int | None, limit: int = 1000
) -> list[dict]:
    """POST /v1/user/{opex}/trades on the market service."""
    syms = [symbol] if symbol else list(DEFAULT_SYMBOLS)
    out: list[dict] = []
    for sym in syms:
        body = {
            "symbol": sym,
            "fromTrade": None,
            # The market service expects ISO-ish dates in JSON; java Date
            # deserializers will accept epoch-ms numbers.
            "startTime": from_ms,
            "endTime": to_ms,
            "limit": limit,
            "orderId": None,
        }
        url = f"{MARKET_BASE}/v1/user/{urllib.parse.quote(opex, safe='')}/trades"
        try:
            rows = http_post_json(url, body)
        except urllib.error.HTTPError as e:
            out.append({"__error__": f"trades:{sym}:HTTP {e.code}"})
            continue
        except Exception as e:  # noqa: BLE001
            out.append({"__error__": f"trades:{sym}:{e!r}"})
            continue
        if not isinstance(rows, list):
            continue
        for r in rows:
            r["__symbol"] = sym
            out.append(r)
    return out


def fetch_user_orders(
    opex: str, symbol: str | None, from_ms: int | None, to_ms: int | None, limit: int = 1000
) -> list[dict]:
    """POST /v1/user/{opex}/orders on the market service. Returns filled,
    canceled, and (best-effort) open orders.
    """
    syms = [symbol] if symbol else list(DEFAULT_SYMBOLS)
    out: list[dict] = []
    for sym in syms:
        body = {
            "symbol": sym,
            "startTime": from_ms,
            "endTime": to_ms,
            "limit": limit,
        }
        url = f"{MARKET_BASE}/v1/user/{urllib.parse.quote(opex, safe='')}/orders"
        try:
            rows = http_post_json(url, body)
        except urllib.error.HTTPError as e:
            out.append({"__error__": f"orders:{sym}:HTTP {e.code}"})
            continue
        except Exception as e:  # noqa: BLE001
            out.append({"__error__": f"orders:{sym}:{e!r}"})
            continue
        if isinstance(rows, list):
            out.extend(rows)
    return out


def fetch_deposits(token: str) -> list[dict]:
    try:
        body = http_get_json(
            f"{CHAIN_BASE}/chain/deposits",
            headers={"Authorization": f"Bearer {token}"},
        )
    except urllib.error.HTTPError as e:
        return [{"__error__": f"deposits:HTTP {e.code}"}]
    except Exception as e:  # noqa: BLE001
        return [{"__error__": f"deposits:{e!r}"}]
    return body if isinstance(body, list) else []


def fetch_withdraws(token: str) -> list[dict]:
    try:
        body = http_get_json(
            f"{CHAIN_BASE}/chain/withdraws",
            headers={"Authorization": f"Bearer {token}"},
        )
    except urllib.error.HTTPError as e:
        return [{"__error__": f"withdraws:HTTP {e.code}"}]
    except Exception as e:  # noqa: BLE001
        return [{"__error__": f"withdraws:{e!r}"}]
    return body if isinstance(body, list) else []


def fetch_conditional_orders(token: str, opex: str) -> list[dict]:
    """Best-effort. The conditional-orders service is being built by a
    sibling agent and may not exist yet. A 404/connection-refused yields
    an empty list, not an error row."""
    try:
        body = http_get_json(
            f"{COND_BASE}/orders/conditional",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Opex-User": opex,
            },
            timeout=4.0,
        )
    except urllib.error.HTTPError as e:
        if e.code in (404, 501, 503):
            return []
        return [{"__error__": f"conditional:HTTP {e.code}"}]
    except Exception:
        return []
    return (
        body
        if isinstance(body, list)
        else (body.get("items") or [])
        if isinstance(body, dict)
        else []
    )


# ============================================================================
# CSV streaming
# ============================================================================


class CSVStreamWriter:
    """Tiny adapter around csv.writer that flushes to an http.server response
    after every row. Handles the BOM and keeps things locale-independent.
    """

    def __init__(self, wfile, *, with_bom: bool = True):
        self._wfile = wfile
        self._buf = io.StringIO()
        self._writer = csv.writer(self._buf, lineterminator="\r\n")
        if with_bom:
            self._wfile.write(BOM.encode("utf-8"))

    def row(self, cols):
        self._buf.seek(0)
        self._buf.truncate(0)
        self._writer.writerow([("" if v is None else str(v)) for v in cols])
        chunk = self._buf.getvalue().encode("utf-8")
        try:
            self._wfile.write(chunk)
            self._wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            raise

    def comment(self, text: str):
        # CSV doesn't formally have comments; we use the convention of a row
        # whose first cell starts with '#'.
        self.row([f"# {text}"])


# ============================================================================
# HTTP handler
# ============================================================================


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "zkcex-export/1.0"

    # ---- low-level helpers ------------------------------------------------
    def log_message(self, fmt, *args):  # quiet default access logging
        sys.stderr.write(f"[export] {self.address_string()} - {fmt % args}\n")

    def _bearer(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        if not auth.lower().startswith("bearer "):
            return None
        return auth[7:].strip() or None

    def _query(self) -> dict[str, str]:
        if "?" not in self.path:
            return {}
        qs = self.path.split("?", 1)[1]
        out = {}
        for k, v in urllib.parse.parse_qsl(qs, keep_blank_values=True):
            out[k] = v
        return out

    def _send_json(self, status: int, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_csv_headers(self, filename: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        # Chunked / unbuffered so big exports stream as they're produced.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        # Disable nginx-style buffering even if we're behind a proxy.
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _require_user(self) -> dict | None:
        tok = self._bearer()
        if not tok:
            self._send_json(401, {"error": "missing_bearer"})
            return None
        user = resolve_user(tok)
        if not user:
            self._send_json(401, {"error": "invalid_token"})
            return None
        return user

    def _filename(self, kind: str, opex: str, q: dict) -> str:
        f = q.get("from_ms") or q.get("startTime") or ""
        t = q.get("to_ms") or q.get("endTime") or ""
        if f or t:
            tag = f"{date_utc(f) or 'all'}--{date_utc(t) or 'now'}"
        else:
            tag = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
        # Sanitize opex for filename use (no path separators).
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in opex)
        return f"zkcex-{kind}-{safe}-{tag}.csv"

    # ---- routing ----------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Opex-User")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self):
        try:
            base = self.path.split("?", 1)[0]
            if base in ("/export/index.json", "/export"):
                return self._index_json()
            if base in ("/export/health", "/health"):
                return self._send_json(200, {"ok": True, "service": "export"})
            if base == "/export/trades.csv":
                return self._trades_csv()
            if base == "/export/deposits.csv":
                return self._deposits_csv()
            if base == "/export/withdraws.csv":
                return self._withdraws_csv()
            if base == "/export/orders.csv":
                return self._orders_csv()
            if base == "/export/balance-history.csv":
                return self._balance_history_csv()
            if base == "/export/tax.csv":
                return self._tax_csv()
            return self._send_json(404, {"error": "not_found", "path": self.path})
        except (BrokenPipeError, ConnectionResetError):
            return  # client hung up mid-stream — nothing to do
        except Exception:
            sys.stderr.write(traceback.format_exc())
            try:
                self._send_json(500, {"error": "internal"})
            except Exception as send_error:  # noqa: BLE001
                sys.stderr.write(f"[export] error response failed: {send_error!r}\n")

    # ---- /export/index.json -----------------------------------------------
    def _index_json(self):
        out = {
            "service": "zkcex-export",
            "version": "1.0",
            "generated_at": iso_utc(int(time.time() * 1000)),
            "exports": [
                {
                    "path": "/export/trades.csv",
                    "label": "체결 내역 / Trade history",
                    "filters": ["symbol", "from_ms", "to_ms"],
                },
                {
                    "path": "/export/deposits.csv",
                    "label": "입금 내역 / Deposits",
                    "filters": ["from_ms", "to_ms"],
                },
                {
                    "path": "/export/withdraws.csv",
                    "label": "출금 내역 / Withdrawals",
                    "filters": ["from_ms", "to_ms"],
                },
                {
                    "path": "/export/orders.csv",
                    "label": "주문 내역 / Order history",
                    "filters": ["symbol"],
                },
                {
                    "path": "/export/balance-history.csv",
                    "label": "일별 잔고 변동 / Daily balance history",
                    "filters": ["from_ms", "to_ms"],
                },
                {
                    "path": "/export/tax.csv",
                    "label": "세금 리포트 / Tax cost-basis report",
                    "filters": ["method", "country_code", "from_ms", "to_ms"],
                    "params": {
                        "method": ["fifo", "lifo", "hifo"],
                        "country_code": ["KR", "US", "JP", "SG", "OTHER"],
                    },
                    "disclaimer": TAX_DISCLAIMER,
                },
            ],
        }
        return self._send_json(200, out)

    # ---- /export/trades.csv ------------------------------------------------
    def _trades_csv(self):
        user = self._require_user()
        if not user:
            return
        q = self._query()
        symbol = (q.get("symbol") or "").strip() or None
        from_ms = int(q["from_ms"]) if q.get("from_ms") else None
        to_ms = int(q["to_ms"]) if q.get("to_ms") else None
        opex = user["opex_user"]
        self._send_csv_headers(self._filename("trades", opex, q))
        w = CSVStreamWriter(self.wfile)
        w.row(
            [
                "trade_id",
                "time_iso",
                "symbol",
                "side",
                "price",
                "quantity",
                "quote_quantity",
                "commission",
                "commission_asset",
                "is_maker",
                "is_buyer",
                "exchange_order_id",
                "client_order_id",
            ]
        )
        rows = fetch_user_trades(opex, symbol, from_ms, to_ms)
        for r in rows:
            if "__error__" in r:
                w.comment(f"ERROR {r['__error__']}")
                continue
            sym = r.get("symbol") or r.get("__symbol") or ""
            side = "BUY" if r.get("isBuyer") else "SELL"
            w.row(
                [
                    r.get("id", ""),
                    iso_utc(r.get("time")),
                    sym,
                    side,
                    fmt_dec(dec(r.get("price"))),
                    fmt_dec(dec(r.get("quantity"))),
                    fmt_dec(dec(r.get("quoteQuantity"))),
                    fmt_dec(dec(r.get("commission"))),
                    r.get("commissionAsset", ""),
                    "true" if r.get("isMaker") else "false",
                    "true" if r.get("isBuyer") else "false",
                    r.get("orderId", ""),
                    r.get("clientOrderId", ""),
                ]
            )

    # ---- /export/deposits.csv ----------------------------------------------
    def _deposits_csv(self):
        user = self._require_user()
        if not user:
            return
        q = self._query()
        from_ms = int(q["from_ms"]) if q.get("from_ms") else None
        to_ms = int(q["to_ms"]) if q.get("to_ms") else None
        opex = user["opex_user"]
        token = self._bearer()
        self._send_csv_headers(self._filename("deposits", opex, q))
        w = CSVStreamWriter(self.wfile)
        w.row(
            [
                "time_iso",
                "chain",
                "asset",
                "amount",
                "tx_hash",
                "block_number",
                "status",
                "aml_status",
                "aml_risk_score",
                "aml_source",
                "observed_at_ms",
            ]
        )
        rows = fetch_deposits(token)
        for r in rows:
            if "__error__" in r:
                w.comment(f"ERROR {r['__error__']}")
                continue
            obs = r.get("observed_at")
            obs_ms = int(obs) * 1000 if (obs and int(obs) < 10_000_000_000) else int(obs or 0)
            if from_ms and obs_ms and obs_ms < from_ms:
                continue
            if to_ms and obs_ms and obs_ms > to_ms:
                continue
            w.row(
                [
                    iso_utc(obs_ms),
                    "hardhat",  # the chain server only tracks deposits on the writable demo chain
                    r.get("asset", ""),
                    r.get("amount", ""),
                    r.get("tx", ""),
                    r.get("block", ""),
                    r.get("status", ""),
                    r.get("aml_status", ""),
                    r.get("aml_risk_score", ""),
                    r.get("aml_source", ""),
                    obs_ms,
                ]
            )

    # ---- /export/withdraws.csv ---------------------------------------------
    def _withdraws_csv(self):
        user = self._require_user()
        if not user:
            return
        q = self._query()
        from_ms = int(q["from_ms"]) if q.get("from_ms") else None
        to_ms = int(q["to_ms"]) if q.get("to_ms") else None
        opex = user["opex_user"]
        token = self._bearer()
        self._send_csv_headers(self._filename("withdraws", opex, q))
        w = CSVStreamWriter(self.wfile)
        w.row(
            [
                "time_iso",
                "chain",
                "asset",
                "amount",
                "tx_hash",
                "destination",
                "status",
                "aml_status",
                "aml_risk_score",
                "aml_source",
                "submitted_at_ms",
            ]
        )
        rows = fetch_withdraws(token)
        for r in rows:
            if "__error__" in r:
                w.comment(f"ERROR {r['__error__']}")
                continue
            sub = r.get("submitted_at")
            sub_ms = int(sub) * 1000 if (sub and int(sub) < 10_000_000_000) else int(sub or 0)
            if from_ms and sub_ms and sub_ms < from_ms:
                continue
            if to_ms and sub_ms and sub_ms > to_ms:
                continue
            w.row(
                [
                    iso_utc(sub_ms),
                    "hardhat",
                    r.get("asset", ""),
                    r.get("amount", ""),
                    r.get("tx", ""),
                    r.get("destination", ""),
                    r.get("status", ""),
                    r.get("aml_status", ""),
                    r.get("aml_risk_score", ""),
                    r.get("aml_source", ""),
                    sub_ms,
                ]
            )

    # ---- /export/orders.csv ------------------------------------------------
    def _orders_csv(self):
        user = self._require_user()
        if not user:
            return
        q = self._query()
        symbol = (q.get("symbol") or "").strip() or None
        opex = user["opex_user"]
        self._send_csv_headers(self._filename("orders", opex, q))
        w = CSVStreamWriter(self.wfile)
        w.row(
            [
                "time_iso",
                "symbol",
                "side",
                "type",
                "price",
                "stop_price",
                "quantity",
                "executed_quantity",
                "status",
                "time_in_force",
                "exchange_order_id",
                "client_order_id",
                "commission_asset",
                "commission_total",
            ]
        )
        # Filled / canceled / open all flow through the same /orders endpoint.
        orders = fetch_user_orders(opex, symbol, None, None)
        # Best-effort enrich with conditional orders. The sibling agent's
        # service may not be online; on 404 we skip silently.
        cond = fetch_conditional_orders(self._bearer(), opex)
        for r in orders:
            if "__error__" in r:
                w.comment(f"ERROR {r['__error__']}")
                continue
            d = r.get("direction") or r.get("side") or ""
            side = "BUY" if str(d).upper() in ("BID", "BUY", "B") else "SELL"
            w.row(
                [
                    iso_utc_from_local(r.get("createDate")),
                    r.get("symbol", ""),
                    side,
                    r.get("type", "") or r.get("matchingType", ""),
                    fmt_dec(dec(r.get("price"))),
                    "",  # stop_price — present only on conditional orders below
                    fmt_dec(dec(r.get("quantity"))),
                    fmt_dec(dec(r.get("executedQuantity"))),
                    r.get("status", ""),
                    r.get("constraint", "") or r.get("timeInForce", ""),
                    r.get("orderId", "") or r.get("id", ""),
                    r.get("clientOrderId", "") or r.get("ouid", ""),
                    "",
                    "",  # market service doesn't aggregate fee per order
                ]
            )
        for r in cond:
            if "__error__" in r:
                w.comment(f"ERROR {r['__error__']}")
                continue
            w.row(
                [
                    iso_utc(r.get("createdAtMs") or r.get("created_at")),
                    r.get("symbol", ""),
                    (r.get("side") or "").upper(),
                    r.get("type", "STOP_LIMIT"),
                    fmt_dec(dec(r.get("price"))),
                    fmt_dec(dec(r.get("stopPrice") or r.get("stop_price"))),
                    fmt_dec(dec(r.get("quantity"))),
                    fmt_dec(dec(r.get("executedQuantity") or 0)),
                    r.get("status", ""),
                    r.get("timeInForce", ""),
                    r.get("orderId", "") or r.get("id", ""),
                    r.get("clientOrderId", ""),
                    "",
                    "",
                ]
            )

    # ---- /export/balance-history.csv ---------------------------------------
    def _balance_history_csv(self):
        """Daily inflow / outflow / net / running-total per asset.

        Inflow  = deposits + buy-fills (base) + sell-proceeds (quote)
        Outflow = withdrawals + sell-fills (base) + buy-cost (quote) + commission

        We don't have a snapshot table, so this is reconstructed from the same
        upstream sources used by the other CSVs. Aggregation is per UTC day.
        """
        user = self._require_user()
        if not user:
            return
        q = self._query()
        from_ms = int(q["from_ms"]) if q.get("from_ms") else None
        to_ms = int(q["to_ms"]) if q.get("to_ms") else None
        opex = user["opex_user"]
        token = self._bearer()

        # --- Pull all sources first; we need them in memory to bucket by day.
        deposits = fetch_deposits(token)
        withdraws = fetch_withdraws(token)
        trades = fetch_user_trades(opex, None, from_ms, to_ms)

        # bucket: { (date, asset) : {"in": Decimal, "out": Decimal} }
        bucket: dict[tuple[str, str], dict[str, Decimal]] = {}

        def add(date: str, asset: str, *, in_: Decimal = Decimal(0), out: Decimal = Decimal(0)):
            if not date or not asset:
                return
            k = (date, asset.upper())
            b = bucket.setdefault(k, {"in": Decimal(0), "out": Decimal(0)})
            b["in"] += in_
            b["out"] += out

        for r in deposits:
            if "__error__" in r:
                continue
            obs_ms = to_epoch_ms(r.get("observed_at"))
            if from_ms and obs_ms and obs_ms < from_ms:
                continue
            if to_ms and obs_ms and obs_ms > to_ms:
                continue
            add(date_utc(obs_ms), r.get("asset", ""), in_=dec(r.get("amount")))

        for r in withdraws:
            if "__error__" in r:
                continue
            sub_ms = to_epoch_ms(r.get("submitted_at"))
            if from_ms and sub_ms and sub_ms < from_ms:
                continue
            if to_ms and sub_ms and sub_ms > to_ms:
                continue
            add(date_utc(sub_ms), r.get("asset", ""), out=dec(r.get("amount")))

        for r in trades:
            if "__error__" in r:
                continue
            sym = r.get("symbol") or r.get("__symbol") or ""
            base, quote = parse_symbol(sym)
            d = date_utc(to_epoch_ms(r.get("time")))
            qty = dec(r.get("quantity"))
            qq = dec(r.get("quoteQuantity"))
            fee = dec(r.get("commission"))
            fee_a = (r.get("commissionAsset") or "").upper()
            if r.get("isBuyer"):
                add(d, base, in_=qty)
                add(d, quote, out=qq)
            else:
                add(d, base, out=qty)
                add(d, quote, in_=qq)
            if fee and fee_a:
                add(d, fee_a, out=fee)

        # --- Compute running totals per asset across ordered dates.
        rows = sorted(bucket.items(), key=lambda kv: (kv[0][1], kv[0][0]))
        running: dict[str, Decimal] = {}

        self._send_csv_headers(self._filename("balance-history", opex, q))
        w = CSVStreamWriter(self.wfile)
        w.row(["date", "asset", "daily_inflow", "daily_outflow", "daily_net", "running_total"])
        for (date, asset), b in rows:
            net = b["in"] - b["out"]
            running[asset] = running.get(asset, Decimal(0)) + net
            w.row(
                [
                    date,
                    asset,
                    fmt_dec(b["in"]),
                    fmt_dec(b["out"]),
                    fmt_dec(net),
                    fmt_dec(running[asset]),
                ]
            )

    # ---- /export/tax.csv ---------------------------------------------------
    def _tax_csv(self):
        """Cost-basis-aware realized-P&L report.

        Lots are built from buy fills (oldest -> newest). Each sell fill is
        matched against unmatched lots according to the chosen accounting
        method (FIFO / LIFO / HIFO) and emits one row per matched lot
        fragment. Cost basis is tracked in USDT for the demo; the
        country_code parameter is recorded but not yet used to convert.
        """
        user = self._require_user()
        if not user:
            return
        q = self._query()
        method = (q.get("method") or "fifo").lower()
        if method not in ("fifo", "lifo", "hifo"):
            return self._send_json(
                400,
                {
                    "error": "bad_method",
                    "allowed": ["fifo", "lifo", "hifo"],
                },
            )
        country = (q.get("country_code") or "").upper() or "OTHER"
        currency = "USDT"  # demo-only; real impl would map country -> KRW/USD/etc.
        opex = user["opex_user"]

        # Pull every trade we can see for this user. We sort globally by time
        # (so two-asset users get a coherent lot history).
        all_trades_raw = fetch_user_trades(opex, None, None, None)
        trades = [r for r in all_trades_raw if "__error__" not in r]
        trades.sort(key=lambda r: to_epoch_ms(r.get("time")))

        # Per-asset lot lists. A lot is {qty_remaining, price_usdt,
        # acquired_at_ms, lot_id}.
        lots: dict[str, list[dict]] = {}
        next_lot_id = 1

        # Pre-process: split each trade into a (asset, qty, price_usdt, side,
        # time_ms, trade_id) tuple. We assume the quote leg is USDT (true for
        # all current zkCEX symbols). Trades on a non-USDT quote are skipped
        # with an inline error row.
        normalized = []
        for r in trades:
            sym = r.get("symbol") or r.get("__symbol") or ""
            base, quote = parse_symbol(sym)
            if quote != "USDT":
                normalized.append({"_skip": f"non-USDT pair {sym}", "_t": r.get("time")})
                continue
            normalized.append(
                {
                    "asset": base,
                    "qty": dec(r.get("quantity")),
                    "price": dec(r.get("price")),
                    "proceeds": dec(r.get("quoteQuantity")),
                    "is_buyer": bool(r.get("isBuyer")),
                    "time_ms": to_epoch_ms(r.get("time")),
                    "trade_id": r.get("id", ""),
                    "fee": dec(r.get("commission")),
                    "fee_asset": (r.get("commissionAsset") or "").upper(),
                }
            )

        # --- Stream the report.
        opex_safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in opex)
        filename = (
            f"zkcex-tax-{opex_safe}-{method}-{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        w = CSVStreamWriter(self.wfile)
        # Top-of-file metadata as comment rows.
        w.comment(f"method={method.upper()}  country_code={country}  currency={currency}")
        w.comment(f"generated_at={iso_utc(int(time.time()*1000))}  user={opex}")
        w.comment(TAX_DISCLAIMER)
        w.row(
            [
                "realized_at_iso",
                "asset",
                "disposed_quantity",
                "disposed_price",
                "disposed_proceeds_usdt",
                "acquired_at_iso",
                "acquired_price",
                "acquired_cost_usdt",
                "gain_loss_usdt",
                "holding_period_days",
                "matched_lot_id",
                "disposal_trade_id",
                "currency",
                "method",
                "country_code",
            ]
        )

        for ev in normalized:
            if "_skip" in ev:
                w.comment(f"skipped {ev['_skip']}")
                continue
            asset = ev["asset"]
            book = lots.setdefault(asset, [])
            if ev["is_buyer"]:
                # Acquisition: append a new lot. Cost basis includes the
                # quote-side commission (deducted from proceeds when this
                # lot is later disposed of), but for simplicity we record
                # the lot at the trade price and let commissions flow
                # through the disposal leg below.
                book.append(
                    {
                        "lot_id": next_lot_id,
                        "qty": ev["qty"],
                        "price": ev["price"],
                        "acquired_ms": ev["time_ms"],
                    }
                )
                next_lot_id += 1
                continue

            # Disposal: match against book per `method`.
            qty_to_match = ev["qty"]
            if qty_to_match <= 0:
                continue
            # Net the seller's commission (paid in fee_asset) into proceeds
            # if and only if the fee was charged in USDT. Otherwise leave
            # it out — matching real exchanges' UI which shows fees as a
            # separate line.
            net_proceeds = ev["proceeds"]
            if ev["fee_asset"] == "USDT":
                net_proceeds -= ev["fee"]

            while qty_to_match > 0 and book:
                idx = _pick_lot_index(book, method)
                lot = book[idx]
                if lot["qty"] <= 0:
                    book.pop(idx)
                    continue
                take = min(lot["qty"], qty_to_match)
                proceeds_share = net_proceeds * (take / ev["qty"]) if ev["qty"] > 0 else Decimal(0)
                cost_share = lot["price"] * take
                gain = proceeds_share - cost_share
                hold_days = ""
                if lot["acquired_ms"] and ev["time_ms"]:
                    hold_days = str((ev["time_ms"] - lot["acquired_ms"]) // 86_400_000)
                w.row(
                    [
                        iso_utc(ev["time_ms"]),
                        asset,
                        fmt_dec(take),
                        fmt_dec(ev["price"]),
                        fmt_dec(proceeds_share),
                        iso_utc(lot["acquired_ms"]),
                        fmt_dec(lot["price"]),
                        fmt_dec(cost_share),
                        fmt_dec(gain),
                        hold_days,
                        lot["lot_id"],
                        ev["trade_id"],
                        currency,
                        method.upper(),
                        country,
                    ]
                )
                lot["qty"] -= take
                qty_to_match -= take
                if lot["qty"] == 0:
                    book.pop(idx)

            if qty_to_match > 0:
                # Selling more than we acquired (short / data gap). Emit a
                # zero-cost row so the disposal still shows up; flagged so
                # the user can spot it.
                w.comment(
                    f"WARNING: disposal of {fmt_dec(qty_to_match)} {asset} "
                    f"unmatched (no acquisition lot); recorded at zero cost."
                )
                w.row(
                    [
                        iso_utc(ev["time_ms"]),
                        asset,
                        fmt_dec(qty_to_match),
                        fmt_dec(ev["price"]),
                        fmt_dec(ev["price"] * qty_to_match),
                        "",
                        "0",
                        "0",
                        fmt_dec(ev["price"] * qty_to_match),
                        "",
                        "",
                        ev["trade_id"],
                        currency,
                        method.upper(),
                        country,
                    ]
                )


def _pick_lot_index(book: list[dict], method: str) -> int:
    """Index into ``book`` for the next lot to consume under ``method``.
    Caller has already ensured book is non-empty."""
    if method == "fifo":
        return 0
    if method == "lifo":
        return len(book) - 1
    # HIFO: highest cost first (largest 'price'). Tie-break by oldest
    # acquired_ms so the result is deterministic.
    best = 0
    best_lot = book[0]
    for i in range(1, len(book)):
        lot = book[i]
        if lot["price"] > best_lot["price"] or (
            lot["price"] == best_lot["price"] and lot["acquired_ms"] < best_lot["acquired_ms"]
        ):
            best, best_lot = i, lot
    return best


def iso_utc_from_local(local_dt) -> str:
    """The market service's Order.createDate is a Java LocalDateTime serialized
    as an ISO-like string without a zone (e.g. ``2026-05-10T11:32:00.123``).
    We treat it as UTC for the export, which matches how the market service
    stores it in postgres.
    """
    if not local_dt:
        return ""
    if isinstance(local_dt, (int, float)):
        return iso_utc(local_dt)
    s = str(local_dt)
    # The serialized form may be a list [Y,M,D,h,m,s,ns] in some Spring
    # configurations. Normalize that too.
    if s.startswith("["):
        try:
            arr = json.loads(s)
            if len(arr) >= 6:
                dt = datetime(arr[0], arr[1], arr[2], arr[3], arr[4], arr[5], tzinfo=timezone.utc)
                return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[export] serialized date parse skipped: {e!r}\n")
    try:
        if "T" in s:
            dt = datetime.fromisoformat(s.split(".")[0])
        else:
            dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return s


# ============================================================================
# Server bootstrap
# ============================================================================


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5540
    sys.stderr.write(
        f"[export] auth={AUTH_BASE} market={MARKET_BASE} " f"chain={CHAIN_BASE} listen=:{port}\n"
    )
    with ThreadingServer(("", port), Handler) as srv:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
