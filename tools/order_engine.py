#!/usr/bin/env python3
"""Conditional-order watcher for zkCEX.

The Kotlin matching-engine only knows ``LIMIT_ORDER`` and ``MARKET_ORDER`` (see
``matching-engine-core/.../OrderMetaData.kt``). To support Stop-Limit /
Stop-Market and OCO (One-Cancels-Other) without touching the engine, this
service sits *in front* of the matching-gateway:

* Browser submits a conditional via ``POST /orders/conditional``.
* We persist it as ``pending`` in SQLite (``tools/.local/order_engine.db``).
* A single background thread polls ``/v3/trades?symbol=<sym>&limit=1`` once a
  second per *active* symbol and evaluates each pending row's trigger.
* When a trigger fires we POST a regular ``LIMIT_ORDER`` / ``MARKET_ORDER`` to
  the matching-gateway (``:8093/order``) using the user's ``X-Opex-User`` and
  flip the row to ``triggered``. For OCO, the sibling leg is flipped to
  ``oco_cancelled_sibling`` in the same transaction.

Stdlib only. Persistence via WAL-mode SQLite. Bearer auth resolved against the
auth_server (``GET /auth/me``) with a tiny in-memory TTL cache.
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal, InvalidOperation

# --- Paths ---------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join(HERE, ".local")
os.makedirs(LOCAL_DIR, exist_ok=True)
DB_PATH = os.path.join(LOCAL_DIR, "order_engine.db")


# --- Config (env overridable) -------------------------------------------
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
# The proxy at :5500 also hosts /v3/* (forwarded to api on :8094). We prefer
# hitting the API directly to avoid a dependency on the static homepage server,
# but allow either via env.
API_BASE = _validated_http_base_url("API_BASE", os.environ.get("API_BASE", "http://127.0.0.1:8094"))
GATEWAY_BASE = _validated_http_base_url(
    "GATEWAY_BASE", os.environ.get("GATEWAY_BASE", "http://127.0.0.1:8093")
)
WATCHER_INTERVAL_S = float(os.environ.get("ORDER_ENGINE_TICK_S", "1.0"))
PRICE_CACHE_TTL_S = float(os.environ.get("ORDER_ENGINE_PRICE_TTL_S", "1.0"))
# Backpressure: cap how many pending rows we evaluate per tick.
MAX_PENDING_PER_TICK = int(os.environ.get("ORDER_ENGINE_MAX_PENDING", "200"))
AUTH_TTL = 30.0  # seconds; mirrors chain_server.py
PUSH_BASE = _validated_http_base_url(
    "PUSH_BASE", os.environ.get("PUSH_BASE", "http://127.0.0.1:5580")
)
NOTIF_BASE = _validated_http_base_url(
    "NOTIF_BASE", os.environ.get("NOTIF_BASE", "http://127.0.0.1:5691")
)


# --- Logging shim -------------------------------------------------------
def log(*args):
    sys.stderr.write("[order_engine] " + " ".join(str(a) for a in args) + "\n")
    sys.stderr.flush()


def _push_notify(opex_user: str, payload: dict) -> None:
    """Best-effort fan-out to push_server. Wrapped tight so a missing push
    service never blocks order triggering."""
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
        _http_urlopen(req, timeout=2).read()
    except Exception as e:  # noqa: BLE001
        log(f"push notify skipped: {e!r}")


def _notif_send(
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


# --- DB -----------------------------------------------------------------
_db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS conditional_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  client_order_id TEXT NOT NULL UNIQUE,
  opex_user TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  type TEXT NOT NULL,
  oco_group_id TEXT,
  trigger_price TEXT,
  limit_price TEXT,
  quantity TEXT NOT NULL,
  status TEXT NOT NULL,
  triggered_exchange_order_id TEXT,
  triggered_at INTEGER,
  created_at INTEGER NOT NULL,
  reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_conditional_user ON conditional_orders(opex_user);
CREATE INDEX IF NOT EXISTS idx_conditional_status ON conditional_orders(status, symbol);
CREATE INDEX IF NOT EXISTS idx_conditional_oco ON conditional_orders(oco_group_id);
"""


def init_db():
    with _db_lock, db() as conn:
        conn.executescript(SCHEMA)


def row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "client_order_id": row["client_order_id"],
        "opex_user": row["opex_user"],
        "symbol": row["symbol"],
        "side": row["side"],
        "type": row["type"],
        "oco_group_id": row["oco_group_id"],
        "trigger_price": row["trigger_price"],
        "limit_price": row["limit_price"],
        "quantity": row["quantity"],
        "status": row["status"],
        "triggered_exchange_order_id": row["triggered_exchange_order_id"],
        "triggered_at": row["triggered_at"],
        "created_at": row["created_at"],
        "reason": row["reason"],
    }


# --- Bearer-token resolver (cached) -------------------------------------
_auth_cache: dict[str, tuple[float, dict]] = {}
_auth_cache_lock = threading.Lock()


def resolve_user_from_token(token: str | None) -> dict | None:
    if not token:
        return None
    now = time.time()
    with _auth_cache_lock:
        cached = _auth_cache.get(token)
        if cached and now - cached[0] < AUTH_TTL:
            return cached[1]
    req = _http_request(
        f"{AUTH_BASE}/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with _http_urlopen(req, timeout=5) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log(f"auth/me {token[:8]}... -> HTTP {e.code}")
        return None
    except Exception as e:  # noqa: BLE001
        log(f"auth/me transport error: {e!r}")
        return None
    user = (obj or {}).get("user")
    if not user or not user.get("opex_user"):
        return None
    with _auth_cache_lock:
        _auth_cache[token] = (now, user)
    return user


# --- Symbol helpers -----------------------------------------------------
# Internal (matching-gateway) format uses an underscore, e.g. "ETH_USDT".
# The Binance-compatible REST surface uses concat, e.g. "ETHUSDT". The /v3
# endpoints take the concat form, so we translate as needed.
def to_concat(symbol: str) -> str:
    return symbol.replace("_", "")


def to_underscore(symbol: str, *, base_hint: str | None = None) -> str:
    """Best-effort split of a concat symbol back to BASE_QUOTE.

    The watcher only ever sees the underscore form (it's stored in the DB),
    so this is just a convenience for callers that pass concat by mistake.
    """
    if "_" in symbol:
        return symbol.upper()
    sym = symbol.upper()
    for q in ("USDT", "BUSD", "USDC", "IRT", "BTC", "ETH"):
        if sym.endswith(q) and len(sym) > len(q):
            return f"{sym[:-len(q)]}_{q}"
    return sym  # leave as-is; caller will fail upstream


# --- Last-price cache ---------------------------------------------------
_price_cache: dict[str, tuple[float, Decimal]] = {}
_price_cache_lock = threading.Lock()


def fetch_last_price(symbol_underscore: str) -> Decimal | None:
    """Return the most recent trade price for ``BASE_QUOTE``, or None.

    Uses a 1s in-process cache so multiple pending rows on the same symbol
    cost only one HTTP call per tick.
    """
    now = time.time()
    with _price_cache_lock:
        cached = _price_cache.get(symbol_underscore)
        if cached and now - cached[0] < PRICE_CACHE_TTL_S:
            return cached[1]
    concat = to_concat(symbol_underscore)
    url = f"{API_BASE}/v3/trades?symbol={urllib.parse.quote(concat)}&limit=1"
    try:
        with _http_urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log(f"price fetch {concat} HTTP {e.code}")
        return None
    except Exception as e:  # noqa: BLE001
        log(f"price fetch {concat} error: {e!r}")
        return None
    if not isinstance(data, list) or not data:
        return None
    try:
        px = Decimal(str(data[0].get("price")))
    except (InvalidOperation, TypeError, ValueError):
        return None
    with _price_cache_lock:
        _price_cache[symbol_underscore] = (now, px)
    return px


# --- Trigger evaluation -------------------------------------------------
def should_trigger(row: sqlite3.Row, last_price: Decimal) -> bool:
    """Return True if the row's trigger condition is satisfied at last_price."""
    side = row["side"]
    typ = row["type"]
    if typ in ("STOP_LIMIT", "STOP_MARKET", "OCO_SL"):
        # Stop-loss semantics: ASK triggers when price falls below trigger
        # (you sell to cap losses on a long); BID triggers when price rises
        # above the trigger (cap losses on a short, or stop-buy breakout).
        try:
            trig = Decimal(row["trigger_price"])
        except (InvalidOperation, TypeError):
            return False
        if side == "ASK":
            return last_price <= trig
        if side == "BID":
            return last_price >= trig
        return False
    if typ == "OCO_TP":
        # Take-profit leg: ASK triggers when price rises above the limit
        # (sell as soon as the price has risen enough); BID triggers when
        # price falls below the limit (buy at or below your TP target).
        try:
            lim = Decimal(row["limit_price"])
        except (InvalidOperation, TypeError):
            return False
        if side == "ASK":
            return last_price >= lim
        if side == "BID":
            return last_price <= lim
        return False
    return False


# --- Submit-to-gateway --------------------------------------------------
def submit_to_gateway(row: sqlite3.Row) -> tuple[bool, str | int | None, str | None]:
    """POST the underlying limit/market order to the matching-gateway.

    Returns (ok, exchange_ref, err). ``exchange_ref`` is whatever the gateway
    returned in its ``OrderSubmitResult`` (``offset``) — there isn't a real
    orderId at submit time on this stack, so we record what we have. The
    client_order_id is the durable cross-reference.
    """
    typ = row["type"]
    is_market = typ == "STOP_MARKET"
    pair = row["symbol"]  # already underscore form
    direction = row["side"]
    qty = row["quantity"]
    price = "0" if is_market else (row["limit_price"] or "0")
    body = {
        "uuid": None,
        "pair": pair,
        "price": float(Decimal(price)),
        "quantity": float(Decimal(qty)),
        "direction": direction,
        "matchConstraint": "GTC",
        "orderType": "MARKET_ORDER" if is_market else "LIMIT_ORDER",
        "userLevel": "*",
        "clientOrderId": row["client_order_id"],
    }
    data = json.dumps(body).encode("utf-8")
    req = _http_request(
        f"{GATEWAY_BASE}/order",
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Opex-User": row["opex_user"],
        },
    )
    try:
        with _http_urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
        try:
            obj = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            obj = {}
        ref = obj.get("offset") if isinstance(obj, dict) else None
        return True, (str(ref) if ref is not None else row["client_order_id"]), None
    except urllib.error.HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8")[:240]
        except Exception as read_error:  # noqa: BLE001
            log(f"gateway error body read failed: {read_error!r}")
        return False, None, f"gateway HTTP {e.code}: {body_txt}"
    except Exception as e:  # noqa: BLE001
        return False, None, f"gateway error: {e!r}"


# --- Watcher loop -------------------------------------------------------
_last_tick_at: float = 0.0
_n_symbols_watched: int = 0
_stop_event = threading.Event()


def watcher_loop():
    log(f"watcher start: tick={WATCHER_INTERVAL_S}s gateway={GATEWAY_BASE} api={API_BASE}")
    while not _stop_event.is_set():
        try:
            tick_once()
        except Exception as e:  # noqa: BLE001
            log(f"tick crashed: {e!r}")
        _stop_event.wait(WATCHER_INTERVAL_S)


def tick_once():
    global _last_tick_at, _n_symbols_watched
    _last_tick_at = time.time()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM conditional_orders WHERE status='pending' " "ORDER BY id DESC LIMIT ?",
            (MAX_PENDING_PER_TICK,),
        ).fetchall()
        # Backpressure warn
        total_pending = conn.execute(
            "SELECT COUNT(*) AS n FROM conditional_orders WHERE status='pending'"
        ).fetchone()["n"]
    if total_pending > MAX_PENDING_PER_TICK:
        log(
            f"backpressure: {total_pending} pending > cap {MAX_PENDING_PER_TICK}; "
            f"processing latest {MAX_PENDING_PER_TICK} this tick"
        )
    if not rows:
        _n_symbols_watched = 0
        return
    symbols = sorted({r["symbol"] for r in rows})
    _n_symbols_watched = len(symbols)
    prices: dict[str, Decimal] = {}
    for sym in symbols:
        px = fetch_last_price(sym)
        if px is not None:
            prices[sym] = px
    for r in rows:
        sym = r["symbol"]
        if sym not in prices:
            continue
        try:
            if should_trigger(r, prices[sym]):
                _try_trigger_row(r, prices[sym])
        except Exception as e:  # noqa: BLE001
            log(f"trigger eval row id={r['id']} crashed: {e!r}")


def _try_trigger_row(row: sqlite3.Row, last_price: Decimal):
    """Submit the underlying order, mark this row triggered, and (if OCO) cancel the sibling.

    Done in a small SQLite transaction to avoid double-firing in the unlikely
    event two ticks overlap. If the sibling has already triggered we no-op.
    """
    rid = row["id"]
    with _db_lock, db() as conn:
        cur = conn.execute(
            "SELECT * FROM conditional_orders WHERE id=? AND status='pending'", (rid,)
        ).fetchone()
        if not cur:
            return
        ok, ref, err = submit_to_gateway(cur)
        now = int(time.time())
        if ok:
            conn.execute(
                "UPDATE conditional_orders SET status='triggered', "
                "triggered_exchange_order_id=?, triggered_at=?, reason=? "
                "WHERE id=?",
                (ref, now, f"last_price={last_price}", rid),
            )
            log(
                f"triggered id={rid} type={cur['type']} side={cur['side']} "
                f"sym={cur['symbol']} last={last_price} ref={ref}"
            )
            # Push-notify the owner. Best-effort.
            _push_notify(
                cur["opex_user"],
                {
                    "title": "조건부 주문 체결 / Conditional order triggered",
                    "body": f"{cur['symbol']} {cur['side']} 조건이 충족되어 주문이 제출되었습니다.",
                    "tag": f"order-trigger-{rid}",
                    "data": {
                        "url": "/app/wallet.html#orders",
                        "symbol": cur["symbol"],
                        "side": cur["side"],
                        "trigger_price": str(cur["trigger_price"] or ""),
                        "last_price": str(last_price),
                    },
                },
            )
            _notif_send(
                cur["opex_user"],
                "order_filled",
                "financial",
                "Conditional order triggered",
                f"Your {cur['symbol']} {cur['side']} stop/OCO triggered at last={last_price}.",
                {
                    "symbol": cur["symbol"],
                    "side": cur["side"],
                    "price": str(cur["trigger_price"] or ""),
                    "quantity": str(cur["quantity"] or ""),
                },
            )
            # OCO: cancel sibling
            if cur["oco_group_id"]:
                conn.execute(
                    "UPDATE conditional_orders SET status='oco_cancelled_sibling', "
                    "reason=? WHERE oco_group_id=? AND id<>? AND status='pending'",
                    (f"sibling_triggered:{rid}", cur["oco_group_id"], rid),
                )
        else:
            conn.execute(
                "UPDATE conditional_orders SET status='failed', reason=? WHERE id=?",
                (err or "unknown", rid),
            )
            log(f"trigger FAILED id={rid}: {err}")


# --- Validation helpers -------------------------------------------------
class BadRequest(Exception):
    def __init__(self, code: str, message: str | None = None):
        super().__init__(code)
        self.code = code
        self.message = message or code


def _D(v, *, name: str, allow_zero: bool = False) -> Decimal:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        raise BadRequest("invalid_number", f"{name} must be numeric") from None
    if not allow_zero and d <= 0:
        raise BadRequest("invalid_number", f"{name} must be positive")
    if allow_zero and d < 0:
        raise BadRequest("invalid_number", f"{name} must be non-negative")
    return d


def validate_symbol(s) -> str:
    if not isinstance(s, str):
        raise BadRequest("invalid_symbol")
    s = s.upper().strip()
    if "_" not in s:
        s = to_underscore(s)
    if "_" not in s or len(s.split("_")) != 2 or not all(s.split("_")):
        raise BadRequest("invalid_symbol", f"symbol must be BASE_QUOTE; got {s!r}")
    return s


def validate_side(v) -> str:
    if v not in ("BID", "ASK"):
        raise BadRequest("invalid_side", "side must be 'BID' or 'ASK'")
    return v


def _trigger_sanity(side: str, trig: Decimal, last: Decimal | None, *, kind: str):
    """Reject obviously-misordered triggers. ``kind`` is for the error string."""
    if last is None:
        return  # no live price yet — let the watcher catch it later
    if side == "ASK" and trig >= last:
        raise BadRequest(
            "invalid_trigger",
            f"{kind} ASK trigger {trig} must be below current price {last}",
        )
    if side == "BID" and trig <= last:
        raise BadRequest(
            "invalid_trigger",
            f"{kind} BID trigger {trig} must be above current price {last}",
        )


def _tp_sanity(side: str, tp_limit: Decimal, last: Decimal | None):
    """OCO take-profit limit sanity. For an ASK take-profit (selling on the
    way up) the TP limit must be *above* the current price; for a BID
    take-profit (covering a short below your entry) it must be *below*."""
    if last is None:
        return
    if side == "ASK" and tp_limit <= last:
        raise BadRequest(
            "invalid_tp",
            f"OCO ASK take_profit limit {tp_limit} must be above current price {last}",
        )
    if side == "BID" and tp_limit >= last:
        raise BadRequest(
            "invalid_tp",
            f"OCO BID take_profit limit {tp_limit} must be below current price {last}",
        )


# --- HTTP server --------------------------------------------------------
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "zkcex-order-engine/1.0"

    # ---- helpers --------------------------------------------------------
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

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _bearer(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        if not auth.lower().startswith("bearer "):
            return None
        return auth.split(None, 1)[1].strip()

    def _require_user(self) -> dict | None:
        user = resolve_user_from_token(self._bearer())
        if not user:
            self._send_json(401, {"error": "unauthorized"})
            return None
        return user

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[order_engine] {self.address_string()} - {fmt % args}\n")

    # ---- CORS -----------------------------------------------------------
    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Opex-User")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    # ---- routing --------------------------------------------------------
    def do_GET(self):  # noqa: N802
        self._dispatch()

    def do_POST(self):  # noqa: N802
        self._dispatch()

    def do_DELETE(self):  # noqa: N802
        self._dispatch()

    def _dispatch(self):
        path = urllib.parse.urlsplit(self.path).path
        method = self.command
        try:
            if method == "GET" and path == "/orders/health":
                return self.h_health()
            if method == "POST" and path == "/orders/conditional":
                return self.h_create()
            if method == "GET" and path == "/orders/conditional":
                return self.h_list()
            if method == "DELETE" and path.startswith("/orders/conditional/"):
                cid = path[len("/orders/conditional/") :].strip("/")
                if not cid:
                    return self._send_json(404, {"error": "not_found"})
                return self.h_cancel(cid)
            return self._send_json(404, {"error": "not_found", "path": self.path})
        except BadRequest as e:
            return self._send_json(400, {"error": e.code, "message": e.message})
        except Exception as e:  # noqa: BLE001
            log(f"handler error on {self.path}: {e!r}")
            return self._send_json(500, {"error": "server_error"})

    # ---- /orders/health -------------------------------------------------
    def h_health(self):
        with db() as conn:
            n_pending = conn.execute(
                "SELECT COUNT(*) AS n FROM conditional_orders WHERE status='pending'"
            ).fetchone()["n"]
        return self._send_json(
            200,
            {
                "ok": True,
                "pending_count": n_pending,
                "last_tick_at": int(_last_tick_at) if _last_tick_at else None,
                "n_symbols_watched": _n_symbols_watched,
            },
        )

    # ---- POST /orders/conditional --------------------------------------
    def h_create(self):
        user = self._require_user()
        if not user:
            return
        opex = user["opex_user"]
        body = self._read_json()
        typ = (body.get("type") or "").upper()
        symbol = validate_symbol(body.get("symbol"))
        side = validate_side(body.get("side"))
        qty = _D(body.get("quantity"), name="quantity")
        # Best-effort current price for sanity checks (None if unavailable).
        last_price = fetch_last_price(symbol)
        now = int(time.time())

        if typ in ("STOP_LIMIT", "STOP_MARKET"):
            trig = _D(body.get("trigger_price"), name="trigger_price")
            limit_price = None
            if typ == "STOP_LIMIT":
                lim = _D(body.get("limit_price"), name="limit_price")
                if lim == trig:
                    raise BadRequest(
                        "invalid_limit",
                        "limit_price must differ from trigger_price",
                    )
                limit_price = str(lim)
            _trigger_sanity(side, trig, last_price, kind=typ)
            cid = self._insert(
                opex=opex,
                symbol=symbol,
                side=side,
                type_=typ,
                group=None,
                trigger_price=str(trig),
                limit_price=limit_price,
                quantity=str(qty),
                created_at=now,
            )
            return self._send_json(
                200,
                {
                    "client_order_id": cid["client_order_id"],
                    "conditional_id": cid["id"],
                    "status": "pending",
                    "created_at": now,
                },
            )

        if typ == "OCO":
            tp = body.get("take_profit") or {}
            sl = body.get("stop_loss") or {}
            tp_limit = _D(tp.get("limit_price"), name="take_profit.limit_price")
            sl_trig = _D(sl.get("trigger_price"), name="stop_loss.trigger_price")
            sl_limit = _D(sl.get("limit_price"), name="stop_loss.limit_price")
            _tp_sanity(side, tp_limit, last_price)
            _trigger_sanity(side, sl_trig, last_price, kind="OCO_SL")
            group_id = uuid.uuid4().hex
            tp_row = self._insert(
                opex=opex,
                symbol=symbol,
                side=side,
                type_="OCO_TP",
                group=group_id,
                trigger_price=None,
                limit_price=str(tp_limit),
                quantity=str(qty),
                created_at=now,
            )
            sl_row = self._insert(
                opex=opex,
                symbol=symbol,
                side=side,
                type_="OCO_SL",
                group=group_id,
                trigger_price=str(sl_trig),
                limit_price=str(sl_limit),
                quantity=str(qty),
                created_at=now,
            )
            return self._send_json(
                200,
                {
                    "oco_group_id": group_id,
                    "status": "pending",
                    "created_at": now,
                    "legs": [
                        {
                            "client_order_id": tp_row["client_order_id"],
                            "conditional_id": tp_row["id"],
                            "type": "OCO_TP",
                        },
                        {
                            "client_order_id": sl_row["client_order_id"],
                            "conditional_id": sl_row["id"],
                            "type": "OCO_SL",
                        },
                    ],
                },
            )

        raise BadRequest("invalid_type", "type must be STOP_LIMIT, STOP_MARKET, or OCO")

    def _insert(
        self, *, opex, symbol, side, type_, group, trigger_price, limit_price, quantity, created_at
    ) -> dict:
        client_order_id = f"co-{uuid.uuid4().hex[:24]}"
        with _db_lock, db() as conn:
            cur = conn.execute(
                "INSERT INTO conditional_orders "
                "(client_order_id, opex_user, symbol, side, type, oco_group_id, "
                " trigger_price, limit_price, quantity, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?, 'pending', ?)",
                (
                    client_order_id,
                    opex,
                    symbol,
                    side,
                    type_,
                    group,
                    trigger_price,
                    limit_price,
                    quantity,
                    created_at,
                ),
            )
            return {"id": cur.lastrowid, "client_order_id": client_order_id}

    # ---- GET /orders/conditional ---------------------------------------
    def h_list(self):
        user = self._require_user()
        if not user:
            return
        opex = user["opex_user"]
        # pending + recently triggered/cancelled, last 50 across the user.
        with db() as conn:
            rows = conn.execute(
                "SELECT * FROM conditional_orders WHERE opex_user=? " "ORDER BY id DESC LIMIT 50",
                (opex,),
            ).fetchall()
        return self._send_json(200, {"orders": [row_to_dict(r) for r in rows]})

    # ---- DELETE /orders/conditional/<id> -------------------------------
    def h_cancel(self, cid: str):
        user = self._require_user()
        if not user:
            return
        opex = user["opex_user"]
        with _db_lock, db() as conn:
            row = conn.execute(
                "SELECT * FROM conditional_orders WHERE id=? AND opex_user=?",
                (cid, opex),
            ).fetchone()
            if not row:
                return self._send_json(404, {"error": "not_found"})
            status = row["status"]
            if status == "pending":
                conn.execute(
                    "UPDATE conditional_orders SET status='cancelled', reason='user_cancel' "
                    "WHERE id=?",
                    (row["id"],),
                )
                # OCO: also cancel the still-pending sibling — user wanted
                # the whole bracket gone.
                if row["oco_group_id"]:
                    conn.execute(
                        "UPDATE conditional_orders SET status='cancelled', "
                        "reason='user_cancel_sibling' WHERE oco_group_id=? AND id<>? "
                        "AND status='pending'",
                        (row["oco_group_id"], row["id"]),
                    )
                return self._send_json(
                    200,
                    {
                        "ok": True,
                        "id": row["id"],
                        "status": "cancelled",
                    },
                )
            if status == "triggered":
                # Already on the exchange — return the linked ref so the UI
                # can drive the regular cancel flow against the gateway.
                return self._send_json(
                    409,
                    {
                        "error": "already_triggered",
                        "message": "order has already fired; cancel via the regular order surface",
                        "triggered_exchange_order_id": row["triggered_exchange_order_id"],
                        "client_order_id": row["client_order_id"],
                        "symbol": row["symbol"],
                    },
                )
            return self._send_json(
                409,
                {
                    "error": "not_cancellable",
                    "status": status,
                },
            )


class ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5520
    init_db()
    t = threading.Thread(target=watcher_loop, name="order-engine-watcher", daemon=True)
    t.start()
    with ThreadingServer(("", port), Handler) as srv:
        log(f"listening on :{port} (db={DB_PATH})")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            log("shutting down")
        finally:
            _stop_event.set()


if __name__ == "__main__":
    main()
