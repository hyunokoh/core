#!/usr/bin/env python3
"""USDT-margined perpetual swap futures engine for zkCEX.

This service sits beside the existing spot stack and exposes a Binance Futures
compatible REST surface under ``/fapi/v1/*``. The spot matching-engine, wallet,
and accountant are untouched; this engine maintains its own SQLite database in
``tools/.local/perp.db`` and only talks to the spot wallet over HTTP when the
user transfers USDT between their spot and futures wallets.

Production-shape design, demo-scoped math:

* **CLOB (Part J)** — every perp symbol has a real in-memory order book.
  LIMIT orders are matched price-time priority against the resting opposite
  side; the residual rests as a maker order. MARKET orders walk the book and
  refund any margin attached to an unfilled residual. Taker / maker fees are
  charged separately (0.05% / 0.02%).
* **Mark price** = mid of (best_bid, best_ask) if both sides have liquidity,
  otherwise last trade price, otherwise the spot index. **Bounded ±5% of
  the spot index** so a thin order book cannot move mark far from oracle.
* **Funding rate** = clamp(premium_ema, ±funding_clamp). ``premium_ema`` is
  the EMA of (perp_mark - spot_index)/spot over real perp trades inside each
  funding interval. Reset at the end of each interval.
* **Margin (Part I)** — per (user, symbol) margin type, ``ISOLATED`` or
  ``CROSSED``. Isolated positions each hold their own ``isolated_margin``.
  Cross positions share the user's whole wallet_balance as collateral; the
  liquidation engine partially deleverages the worst position iteratively
  until the cross account is healthy again.
* **Liquidation watcher** (500 ms tick, single-locked): isolated positions
  liquidate independently; cross positions liquidate by closing the largest
  loss-leader first, then re-check.

Stdlib only. ``decimal.Decimal`` everywhere — never floats — for money math.
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
from decimal import Decimal, InvalidOperation, getcontext

# 38 digits is comfortably more than 2x the precision of fiat amounts on any
# real exchange. Keep it process-wide so every Decimal op uses the same prec.
getcontext().prec = 38

# --- Paths ---------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
LOCAL_DIR = os.path.join(HERE, ".local")
os.makedirs(LOCAL_DIR, exist_ok=True)
DB_PATH = os.path.join(LOCAL_DIR, "perp.db")

# OpenTelemetry tracing (stdlib-only). Set service name BEFORE importing otel.
os.environ.setdefault("OTEL_SERVICE_NAME", "zkcex-perp")
try:
    from otel.shim import install as _otel_install  # noqa: E402
    from otel.shim import server_span as _otel_server_span
except Exception:  # noqa: BLE001

    def _otel_install():
        pass

    def _otel_server_span(_h):
        class _N:
            def __enter__(self):
                class _S:
                    def set_attribute(self, *a, **kw):
                        pass

                return _S()

            def __exit__(self, *a):
                return False

        return _N()


# --- Config (env-overridable) -------------------------------------------
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


WALLET_BASE = _validated_http_base_url(
    "PERP_WALLET_BASE", os.environ.get("PERP_WALLET_BASE", "http://127.0.0.1:8091")
)
SPOT_API_BASE = _validated_http_base_url(
    "PERP_API_BASE", os.environ.get("PERP_API_BASE", "http://127.0.0.1:8094")
)
INSURANCE_USER = "zkcex-insurance"  # synthetic counterparty
PRICE_TICK_S = float(os.environ.get("PERP_PRICE_TICK_S", "1.0"))
LIQ_TICK_S = float(os.environ.get("PERP_LIQ_TICK_S", "0.5"))
BOOK_CLEAN_TICK_S = float(os.environ.get("PERP_BOOK_CLEAN_TICK_S", "5.0"))
TAKER_FEE = Decimal(os.environ.get("PERP_TAKER_FEE", "0.0005"))  # 5 bps
MAKER_FEE = Decimal(os.environ.get("PERP_MAKER_FEE", "0.0002"))  # 2 bps

# Mark-price guardrails. The book mid is bounded inside ±MARK_BAND of the
# spot index to prevent an attacker who placed a single $10k limit order from
# moving the mark and triggering everyone's liquidations.
MARK_BAND = Decimal("0.05")  # ±5%

# Cross-margin healthy-after-liquidation buffer; we iterate partial closes
# until margin_ratio >= 1.0 + CROSS_HEALTHY_BUFFER (so we leave headroom).
CROSS_HEALTHY_BUFFER = Decimal("0.2")

# Demo seed markets. Index points at the spot symbol on /v3/trades.
DEFAULT_MARKETS = [
    {
        "symbol": "ETHUSDT_PERP",
        "base_asset": "ETH",
        "quote_asset": "USDT",
        "spot_index_symbol": "ETHUSDT",
        "contract_size": "1",
        "tick_size": "0.01",
        "step_size": "0.001",
        "max_leverage": 50,
        "maintenance_margin_rate": "0.005",
        "funding_interval_seconds": 300,
        "funding_clamp": "0.0075",
    },
    {
        "symbol": "BTCUSDT_PERP",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "spot_index_symbol": "BTCUSDT",
        "contract_size": "1",
        "tick_size": "0.1",
        "step_size": "0.0001",
        "max_leverage": 50,
        "maintenance_margin_rate": "0.005",
        "funding_interval_seconds": 300,
        "funding_clamp": "0.0075",
    },
]


def log(*args):
    sys.stderr.write("[perp_engine] " + " ".join(str(a) for a in args) + "\n")
    sys.stderr.flush()


# --- DB -----------------------------------------------------------------
_db_lock = threading.Lock()
# Single lock that serializes liquidation re-checks so cross-margin loops
# don't race with one another (e.g. two threads both partial-closing while
# the other is mid-flight). Held only inside ``_liq_tick_once``.
_liq_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS perp_markets (
  symbol TEXT PRIMARY KEY,
  base_asset TEXT NOT NULL,
  quote_asset TEXT NOT NULL,
  spot_index_symbol TEXT NOT NULL,
  contract_size TEXT NOT NULL DEFAULT '1',
  tick_size TEXT NOT NULL DEFAULT '0.01',
  step_size TEXT NOT NULL DEFAULT '0.001',
  max_leverage INTEGER NOT NULL DEFAULT 50,
  maintenance_margin_rate TEXT NOT NULL DEFAULT '0.005',
  funding_interval_seconds INTEGER NOT NULL DEFAULT 300,
  funding_clamp TEXT NOT NULL DEFAULT '0.0075',
  last_funding_at INTEGER,
  next_funding_at INTEGER,
  mark_price TEXT,
  index_price TEXT,
  last_trade_price TEXT,
  premium_ema TEXT NOT NULL DEFAULT '0',
  last_funding_rate TEXT NOT NULL DEFAULT '0',
  status TEXT NOT NULL DEFAULT 'TRADING'
);

CREATE TABLE IF NOT EXISTS perp_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  opex_user TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  quantity TEXT NOT NULL,
  entry_price TEXT NOT NULL,
  isolated_margin TEXT NOT NULL,
  leverage INTEGER NOT NULL,
  unrealized_pnl TEXT NOT NULL DEFAULT '0',
  liquidation_price TEXT,
  realized_pnl TEXT NOT NULL DEFAULT '0',
  total_commission TEXT NOT NULL DEFAULT '0',
  total_funding TEXT NOT NULL DEFAULT '0',
  margin_type TEXT NOT NULL DEFAULT 'ISOLATED',
  opened_at INTEGER NOT NULL,
  closed_at INTEGER,
  close_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_pos_user_open ON perp_positions(opex_user, closed_at);
CREATE INDEX IF NOT EXISTS idx_pos_sym_open  ON perp_positions(symbol, closed_at);

CREATE TABLE IF NOT EXISTS perp_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  client_order_id TEXT,
  opex_user TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  position_side TEXT,
  type TEXT NOT NULL,
  reduce_only INTEGER NOT NULL DEFAULT 0,
  post_only INTEGER NOT NULL DEFAULT 0,
  quantity TEXT NOT NULL,
  remaining_quantity TEXT NOT NULL DEFAULT '0',
  price TEXT,
  stop_price TEXT,
  time_in_force TEXT NOT NULL DEFAULT 'GTC',
  status TEXT NOT NULL DEFAULT 'NEW',
  cancel_reason TEXT,
  executed_qty TEXT NOT NULL DEFAULT '0',
  cumulative_quote_qty TEXT NOT NULL DEFAULT '0',
  avg_fill_price TEXT,
  reserved_margin TEXT NOT NULL DEFAULT '0',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON perp_orders(opex_user, status);
CREATE INDEX IF NOT EXISTS idx_orders_book ON perp_orders(symbol, status, side, price);

CREATE TABLE IF NOT EXISTS perp_balances (
  opex_user TEXT NOT NULL,
  asset TEXT NOT NULL,
  wallet_balance TEXT NOT NULL DEFAULT '0',
  margin_balance TEXT NOT NULL DEFAULT '0',
  available_balance TEXT NOT NULL DEFAULT '0',
  total_unrealized_pnl TEXT NOT NULL DEFAULT '0',
  PRIMARY KEY (opex_user, asset)
);

CREATE TABLE IF NOT EXISTS perp_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  opex_user TEXT NOT NULL,
  symbol TEXT NOT NULL,
  order_id INTEGER NOT NULL,
  side TEXT NOT NULL,
  price TEXT NOT NULL,
  quantity TEXT NOT NULL,
  realized_pnl TEXT NOT NULL DEFAULT '0',
  commission TEXT NOT NULL DEFAULT '0',
  commission_asset TEXT NOT NULL DEFAULT 'USDT',
  is_maker INTEGER NOT NULL DEFAULT 0,
  time INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_user ON perp_trades(opex_user, time DESC);
CREATE INDEX IF NOT EXISTS idx_trades_sym ON perp_trades(symbol, time DESC);

CREATE TABLE IF NOT EXISTS perp_funding_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  funding_rate TEXT NOT NULL,
  funding_time INTEGER NOT NULL,
  applied_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_funding_sym ON perp_funding_history(symbol, funding_time DESC);

CREATE TABLE IF NOT EXISTS perp_income_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  opex_user TEXT NOT NULL,
  symbol TEXT,
  income_type TEXT NOT NULL,
  income TEXT NOT NULL,
  asset TEXT NOT NULL DEFAULT 'USDT',
  time INTEGER NOT NULL,
  related_order_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_income_user ON perp_income_history(opex_user, time DESC);

-- Per-user leverage setting (default to the market max if no row exists).
CREATE TABLE IF NOT EXISTS perp_user_leverage (
  opex_user TEXT NOT NULL,
  symbol TEXT NOT NULL,
  leverage INTEGER NOT NULL,
  PRIMARY KEY (opex_user, symbol)
);

-- Per (user, symbol) margin mode: ISOLATED (default) or CROSSED.
CREATE TABLE IF NOT EXISTS perp_margin_settings (
  opex_user TEXT NOT NULL,
  symbol TEXT NOT NULL,
  margin_type TEXT NOT NULL DEFAULT 'ISOLATED',
  PRIMARY KEY (opex_user, symbol)
);

-- Periodic order book snapshot (debugging + replay).
CREATE TABLE IF NOT EXISTS perp_book_snapshots (
  symbol TEXT NOT NULL,
  taken_at INTEGER NOT NULL,
  best_bid TEXT, best_ask TEXT, mid TEXT,
  n_bids INTEGER, n_asks INTEGER
);
"""


# Columns that we may need to backfill on an older perp.db. The schema above
# is the source of truth; this list keeps ALTER statements in one place so
# we never silently lose a migration.
ALTERS = [
    ("perp_orders", "remaining_quantity TEXT NOT NULL DEFAULT '0'"),
    ("perp_orders", "cancel_reason TEXT"),
    ("perp_orders", "post_only INTEGER NOT NULL DEFAULT 0"),
    ("perp_orders", "reserved_margin TEXT NOT NULL DEFAULT '0'"),
    ("perp_positions", "margin_type TEXT NOT NULL DEFAULT 'ISOLATED'"),
    ("perp_markets", "last_trade_price TEXT"),
]


def _drop_legacy_unique_constraint(conn: sqlite3.Connection):
    """Older perp.db has UNIQUE(opex_user, symbol, opened_at) on
    perp_positions, which collides when a user closes and immediately reopens
    inside the same second. SQLite cannot DROP CONSTRAINT, so we recreate the
    table without it (data preserved)."""
    info = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='perp_positions'"
    ).fetchone()
    if not info or "UNIQUE" not in info[0]:
        return
    log("rebuilding perp_positions to drop legacy UNIQUE constraint")
    conn.executescript("""
        CREATE TABLE perp_positions_new (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          opex_user TEXT NOT NULL,
          symbol TEXT NOT NULL,
          side TEXT NOT NULL,
          quantity TEXT NOT NULL,
          entry_price TEXT NOT NULL,
          isolated_margin TEXT NOT NULL,
          leverage INTEGER NOT NULL,
          unrealized_pnl TEXT NOT NULL DEFAULT '0',
          liquidation_price TEXT,
          realized_pnl TEXT NOT NULL DEFAULT '0',
          total_commission TEXT NOT NULL DEFAULT '0',
          total_funding TEXT NOT NULL DEFAULT '0',
          margin_type TEXT NOT NULL DEFAULT 'ISOLATED',
          opened_at INTEGER NOT NULL,
          closed_at INTEGER,
          close_reason TEXT
        );
        INSERT INTO perp_positions_new SELECT
          id, opex_user, symbol, side, quantity, entry_price, isolated_margin,
          leverage, unrealized_pnl, liquidation_price, realized_pnl,
          total_commission, total_funding, margin_type, opened_at, closed_at,
          close_reason FROM perp_positions;
        DROP TABLE perp_positions;
        ALTER TABLE perp_positions_new RENAME TO perp_positions;
        CREATE INDEX IF NOT EXISTS idx_pos_user_open ON perp_positions(opex_user, closed_at);
        CREATE INDEX IF NOT EXISTS idx_pos_sym_open  ON perp_positions(symbol, closed_at);
    """)


def init_db():
    with _db_lock, db() as conn:
        conn.executescript(SCHEMA)
        # Idempotent ALTER pass for older databases. SQLite raises if the
        # column exists, so we swallow that one error per column.
        for table, col_def in ALTERS:
            col_name = col_def.split()[0]
            cur = conn.execute(f"PRAGMA table_info({table})").fetchall()
            names = {r[1] for r in cur}
            if col_name not in names:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
                    log(f"migrated {table}.{col_name}")
                except sqlite3.OperationalError as e:
                    log(f"alter {table}.{col_name} skipped: {e}")
        _drop_legacy_unique_constraint(conn)
        # Seed markets if missing.
        for m in DEFAULT_MARKETS:
            row = conn.execute(
                "SELECT 1 FROM perp_markets WHERE symbol=?", (m["symbol"],)
            ).fetchone()
            if row:
                continue
            conn.execute(
                "INSERT INTO perp_markets "
                "(symbol, base_asset, quote_asset, spot_index_symbol, contract_size, "
                " tick_size, step_size, max_leverage, maintenance_margin_rate, "
                " funding_interval_seconds, funding_clamp, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?, 'TRADING')",
                (
                    m["symbol"],
                    m["base_asset"],
                    m["quote_asset"],
                    m["spot_index_symbol"],
                    m["contract_size"],
                    m["tick_size"],
                    m["step_size"],
                    m["max_leverage"],
                    m["maintenance_margin_rate"],
                    m["funding_interval_seconds"],
                    m["funding_clamp"],
                ),
            )


# --- Decimal helpers ----------------------------------------------------
ZERO = Decimal(0)
INF = Decimal("9999999999999999999999")  # +∞ stand-in for market BUY price


def D(v) -> Decimal:
    """Coerce any number-like to Decimal, raising on garbage."""
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError) as e:
        raise BadRequest("invalid_number", f"cannot parse {v!r}: {e}") from e


def Dq(v, *, allow_zero: bool = False, name: str = "value") -> Decimal:
    """Parse + validate (>0 or >=0)."""
    d = D(v)
    if not allow_zero and d <= 0:
        raise BadRequest("invalid_number", f"{name} must be > 0; got {d}")
    if allow_zero and d < 0:
        raise BadRequest("invalid_number", f"{name} must be >= 0; got {d}")
    return d


def clamp(value: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def dstr(v) -> str:
    """Stringify a Decimal cleanly: "0" instead of "0E-127", and no scientific
    notation. We round to 12 decimal places — more than any fiat exchange
    needs, but enough for accurate fractional-bps pricing."""
    if not isinstance(v, Decimal):
        v = D(v)
    if v == 0:
        return "0"
    q = v.quantize(Decimal("0.000000000001")) if abs(v) >= Decimal("0.000000000001") else v
    s = format(q, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def now_ms() -> int:
    return int(time.time() * 1000)


def now_s() -> int:
    return int(time.time())


# --- Validation ---------------------------------------------------------
class BadRequest(Exception):
    def __init__(self, code: str | int, message: str = "", http_status: int = 400):
        super().__init__(message or str(code))
        self.code = code
        self.message = message or str(code)
        self.http_status = http_status


def binance_error(code: int, msg: str) -> dict:
    """Binance Futures error shape: {"code":-2010,"msg":"..."}"""
    return {"code": code, "msg": msg}


# --- Market state helpers ----------------------------------------------
def get_market(symbol: str) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("SELECT * FROM perp_markets WHERE symbol=?", (symbol,)).fetchone()


def list_markets() -> list[sqlite3.Row]:
    with db() as conn:
        return list(conn.execute("SELECT * FROM perp_markets ORDER BY symbol").fetchall())


# --- Spot index fetch ---------------------------------------------------
_price_cache: dict[str, tuple[float, Decimal]] = {}
_price_cache_lock = threading.Lock()
_PRICE_TTL = 0.8  # seconds; cap how often we hit /v3/trades per symbol


def fetch_spot_last_price(spot_symbol: str) -> Decimal | None:
    """Read the most recent trade price for ``ETHUSDT`` etc from /v3/trades."""
    now = time.time()
    with _price_cache_lock:
        cached = _price_cache.get(spot_symbol)
        if cached and now - cached[0] < _PRICE_TTL:
            return cached[1]
    url = f"{SPOT_API_BASE}/v3/trades?symbol={urllib.parse.quote(spot_symbol)}&limit=1"
    try:
        with _http_urlopen(url, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8") or "[]")
    except Exception as e:  # noqa: BLE001
        log(f"spot price {spot_symbol} fetch error: {e!r}")
        return None
    if not isinstance(data, list) or not data:
        return None
    try:
        px = Decimal(str(data[0].get("price")))
    except Exception:
        return None
    if px <= 0:
        return None
    with _price_cache_lock:
        _price_cache[spot_symbol] = (now, px)
    return px


# --- Margin math --------------------------------------------------------
def liquidation_price(
    side: str, entry: Decimal, qty: Decimal, isolated_margin: Decimal, mmr: Decimal
) -> Decimal:
    """Liquidation price for an isolated-margin one-way position.

    LONG:  entry - (isolated_margin - maintenance_margin) / qty
    SHORT: entry + (isolated_margin - maintenance_margin) / qty
    where maintenance_margin = entry * qty * mmr.

    Caps the LONG result at zero — price can't go negative.
    """
    if qty <= 0:
        return ZERO
    maint = entry * qty * mmr
    cushion = (isolated_margin - maint) / qty
    if side == "LONG":
        return max(ZERO, entry - cushion)
    return entry + cushion


def unrealized_pnl(side: str, entry: Decimal, qty: Decimal, mark: Decimal) -> Decimal:
    """LONG profits when mark > entry, SHORT profits when mark < entry."""
    if side == "LONG":
        return (mark - entry) * qty
    return (entry - mark) * qty


# --- Balance helpers (single-row, atomic) ------------------------------
def _ensure_balance_row(conn: sqlite3.Connection, opex_user: str):
    conn.execute(
        "INSERT OR IGNORE INTO perp_balances "
        "(opex_user, asset, wallet_balance, margin_balance, available_balance, "
        " total_unrealized_pnl) VALUES (?, 'USDT', '0','0','0','0')",
        (opex_user,),
    )


def _get_margin_type(conn: sqlite3.Connection, opex_user: str, symbol: str) -> str:
    row = conn.execute(
        "SELECT margin_type FROM perp_margin_settings WHERE opex_user=? AND symbol=?",
        (opex_user, symbol),
    ).fetchone()
    return row["margin_type"] if row else "ISOLATED"


def get_balance(opex_user: str) -> dict:
    """Return wallet/margin/available + unrealized PnL for a user. Now treats
    CROSSED positions specially: their isolated_margin contribution to
    used_margin is the user's whole wallet (we account it differently in the
    UI)."""
    with db() as conn:
        _ensure_balance_row(conn, opex_user)
        row = conn.execute(
            "SELECT * FROM perp_balances WHERE opex_user=? AND asset='USDT'",
            (opex_user,),
        ).fetchone()
        wallet = D(row["wallet_balance"])
        used_iso_margin = ZERO  # sum of isolated margin
        cross_initial_margin = ZERO  # sum of qty*entry/leverage for cross positions
        cross_upnl = ZERO
        iso_upnl = ZERO
        positions = conn.execute(
            "SELECT * FROM perp_positions WHERE opex_user=? AND closed_at IS NULL",
            (opex_user,),
        ).fetchall()
        # Also include open-order reserved margin (resting LIMIT orders).
        resv_rows = conn.execute(
            "SELECT COALESCE(SUM(CAST(reserved_margin AS REAL)),0) AS s "
            "FROM perp_orders WHERE opex_user=? AND status IN ('NEW','PARTIALLY_FILLED')",
            (opex_user,),
        ).fetchone()
        reserved = D(str(resv_rows["s"] or 0))
    market_marks: dict[str, Decimal] = {}
    for p in positions:
        mark = market_marks.get(p["symbol"])
        if mark is None:
            m = get_market(p["symbol"])
            mark = D(m["mark_price"]) if m and m["mark_price"] else D(p["entry_price"])
            market_marks[p["symbol"]] = mark
        upnl = unrealized_pnl(p["side"], D(p["entry_price"]), D(p["quantity"]), mark)
        if p["margin_type"] == "CROSSED":
            cross_initial_margin += D(p["quantity"]) * D(p["entry_price"]) / Decimal(p["leverage"])
            cross_upnl += upnl
        else:
            used_iso_margin += D(p["isolated_margin"])
            iso_upnl += upnl
    total_upnl = cross_upnl + iso_upnl
    margin_balance = wallet + total_upnl
    # Available = wallet - used_iso_margin - cross_initial_margin - reserved
    # (cross uPnL doesn't release available margin; it just changes
    # margin_balance.)
    available = wallet - used_iso_margin - cross_initial_margin - reserved
    if available < 0:
        available = ZERO
    return {
        "wallet_balance": str(wallet),
        "margin_balance": str(margin_balance),
        "available_balance": str(available),
        "total_unrealized_pnl": str(total_upnl),
        "cross_unrealized_pnl": str(cross_upnl),
        "used_margin": str(used_iso_margin + cross_initial_margin),
        "reserved_margin": str(reserved),
    }


def get_leverage(opex_user: str, symbol: str, default: int) -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT leverage FROM perp_user_leverage WHERE opex_user=? AND symbol=?",
            (opex_user, symbol),
        ).fetchone()
        if row:
            return int(row["leverage"])
    return default


def set_leverage(opex_user: str, symbol: str, leverage: int):
    with _db_lock, db() as conn:
        conn.execute(
            "INSERT INTO perp_user_leverage (opex_user, symbol, leverage) "
            "VALUES (?,?,?) "
            "ON CONFLICT(opex_user, symbol) DO UPDATE SET leverage=excluded.leverage",
            (opex_user, symbol, leverage),
        )


# =====================================================================
# CLOB — in-memory order book
# =====================================================================
#
# Per symbol:
#   bids: max-heap by price (we store descending), time-priority within price
#   asks: min-heap by price (ascending), time-priority within price
# We keep them as sorted lists rather than heaps so we can introspect /
# cancel cheaply — perp throughput is low compared to spot.
#
# Each level is a list of dicts:
#   {"order_id": int, "opex_user": str, "price": Decimal,
#    "quantity": Decimal, "ts": int}
# bids sorted by (-price, ts)  -> best at index 0
# asks sorted by  (price, ts)  -> best at index 0
#
# All mutations go through ``_book_lock``. Matching happens entirely under
# the lock + the DB lock so no two place_order calls can interleave on the
# same symbol.

_book_lock = threading.Lock()
_books: dict[str, dict[str, list[dict]]] = {}
_last_update_id: dict[str, int] = {}


def _book_for(symbol: str) -> dict[str, list[dict]]:
    b = _books.get(symbol)
    if b is None:
        b = {"bids": [], "asks": []}
        _books[symbol] = b
    return b


def _bump_update_id(symbol: str) -> int:
    nxt = _last_update_id.get(symbol, 0) + 1
    _last_update_id[symbol] = nxt
    return nxt


def _book_add(symbol: str, side: str, level: dict):
    """Insert a maker order. side is BUY (bids) or SELL (asks)."""
    book = _book_for(symbol)
    bucket = book["bids"] if side == "BUY" else book["asks"]
    if side == "BUY":
        # bids: descending by price, time-first within price
        i = 0
        while i < len(bucket) and bucket[i]["price"] >= level["price"]:
            # within same price, keep older orders first
            if bucket[i]["price"] == level["price"] and bucket[i]["ts"] <= level["ts"]:
                i += 1
                continue
            if bucket[i]["price"] > level["price"]:
                i += 1
                continue
            break
        bucket.insert(i, level)
    else:
        # asks: ascending by price, time-first within price
        i = 0
        while i < len(bucket) and bucket[i]["price"] <= level["price"]:
            if bucket[i]["price"] == level["price"] and bucket[i]["ts"] <= level["ts"]:
                i += 1
                continue
            if bucket[i]["price"] < level["price"]:
                i += 1
                continue
            break
        bucket.insert(i, level)
    _bump_update_id(symbol)


def _book_remove_order(symbol: str, order_id: int) -> dict | None:
    book = _book_for(symbol)
    for side_key in ("bids", "asks"):
        for i, lvl in enumerate(book[side_key]):
            if lvl["order_id"] == order_id:
                _bump_update_id(symbol)
                return book[side_key].pop(i)
    return None


def _restore_book_from_db():
    """Rehydrate the in-memory book from any orders that survived a restart."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM perp_orders WHERE status IN ('NEW','PARTIALLY_FILLED') "
            "AND type IN ('LIMIT','POST_ONLY') AND price IS NOT NULL "
            "ORDER BY id ASC"
        ).fetchall()
    for r in rows:
        rem = D(r["remaining_quantity"] or "0")
        if rem <= 0:
            continue
        level = {
            "order_id": r["id"],
            "opex_user": r["opex_user"],
            "price": D(r["price"]),
            "quantity": rem,
            "ts": int(r["created_at"]),
        }
        _book_add(r["symbol"], r["side"], level)
    log(f"restored {sum(len(b['bids'])+len(b['asks']) for b in _books.values())} resting orders")


def _book_depth_snapshot(symbol: str, levels: int = 100) -> dict:
    """Aggregate resting orders to (price, total_qty) for up to ``levels``."""
    book = _book_for(symbol)

    def agg(side_list, reverse=False):
        # Already sorted; group by price.
        out: list[tuple[Decimal, Decimal]] = []
        cur_px = None
        cur_qty = ZERO
        for lvl in side_list:
            if cur_px is None or lvl["price"] != cur_px:
                if cur_px is not None:
                    out.append((cur_px, cur_qty))
                cur_px = lvl["price"]
                cur_qty = lvl["quantity"]
            else:
                cur_qty += lvl["quantity"]
            if len(out) >= levels:
                break
        if cur_px is not None and len(out) < levels:
            out.append((cur_px, cur_qty))
        return out

    bids = agg(book["bids"])
    asks = agg(book["asks"])
    return {
        "lastUpdateId": _last_update_id.get(symbol, 0),
        "bids": [[dstr(p), dstr(q)] for p, q in bids],
        "asks": [[dstr(p), dstr(q)] for p, q in asks],
    }


def _book_best(symbol: str) -> tuple[Decimal | None, Decimal | None]:
    book = _book_for(symbol)
    best_bid = book["bids"][0]["price"] if book["bids"] else None
    best_ask = book["asks"][0]["price"] if book["asks"] else None
    return best_bid, best_ask


# --- Pricing / funding background thread --------------------------------
_stop_event = threading.Event()
_last_pricing_tick = 0.0
# Per-interval perp trade aggregates feeding the premium EMA.
_premium_samples_lock = threading.Lock()
_premium_samples: dict[str, list[tuple[Decimal, Decimal]]] = {}  # sym -> [(perp_px, spot_px)]


def _record_perp_trade_for_premium(symbol: str, price: Decimal, spot_index: Decimal | None):
    if spot_index is None or spot_index <= 0:
        return
    with _premium_samples_lock:
        bucket = _premium_samples.setdefault(symbol, [])
        bucket.append((price, spot_index))
        # Cap the bucket so a runaway loop can't blow memory.
        if len(bucket) > 10000:
            del bucket[:5000]


def _consume_premium_samples(symbol: str) -> list[tuple[Decimal, Decimal]]:
    with _premium_samples_lock:
        out = _premium_samples.pop(symbol, [])
    return out


def pricing_loop():
    """Once per second: refresh index from spot, recompute mark from book mid
    (bounded ±5% of index), and roll funding payments when due."""
    global _last_pricing_tick
    log("pricing loop start")
    with _db_lock, db() as conn:
        for m in conn.execute("SELECT * FROM perp_markets").fetchall():
            if not m["next_funding_at"]:
                interval = int(m["funding_interval_seconds"])
                conn.execute(
                    "UPDATE perp_markets SET next_funding_at=? WHERE symbol=?",
                    (now_s() + interval, m["symbol"]),
                )
    while not _stop_event.is_set():
        try:
            _pricing_tick_once()
        except Exception as e:  # noqa: BLE001
            log(f"pricing tick crashed: {e!r}")
        _last_pricing_tick = time.time()
        _stop_event.wait(PRICE_TICK_S)


def _pricing_tick_once():
    markets = list_markets()
    now = now_s()
    for m in markets:
        idx = fetch_spot_last_price(m["spot_index_symbol"])
        if idx is None:
            continue
        # Mark = mid of book, fallback to last trade, fallback to index.
        with _book_lock:
            best_bid, best_ask = _book_best(m["symbol"])
            book_mid = None
            if best_bid is not None and best_ask is not None and best_bid > 0 and best_ask > 0:
                book_mid = (best_bid + best_ask) / Decimal(2)
            last_trade = D(m["last_trade_price"]) if m["last_trade_price"] else None
        if book_mid is not None:
            mark = book_mid
        elif last_trade is not None and last_trade > 0:
            mark = last_trade
        else:
            mark = idx
        # Bound mark inside ±MARK_BAND of the index — protects against
        # toxic single-sided liquidity moving the mark unrealistically.
        lo = idx * (Decimal(1) - MARK_BAND)
        hi = idx * (Decimal(1) + MARK_BAND)
        bounded = clamp(mark, lo, hi)
        # Refresh premium EMA from any real perp trade samples that landed
        # since the last tick (we used to feed it from synthetic mark
        # settlements; now it comes from trades-vs-index).
        samples = _consume_premium_samples(m["symbol"])
        prem = D(m["premium_ema"])
        alpha = Decimal("0.1")
        for px, spot_px in samples:
            inst = (px - spot_px) / spot_px
            prem = prem + alpha * (inst - prem)
        # In a flat tape, decay slowly toward zero so funding doesn't stick.
        if not samples:
            prem = prem * Decimal("0.98")
        prem = clamp(prem, Decimal("-0.05"), Decimal("0.05"))
        with _db_lock, db() as conn:
            conn.execute(
                "UPDATE perp_markets SET index_price=?, mark_price=?, premium_ema=? "
                "WHERE symbol=?",
                (str(idx), str(bounded), str(prem), m["symbol"]),
            )
        next_fund = int(m["next_funding_at"] or 0)
        if next_fund and now >= next_fund:
            apply_funding(m["symbol"])


def apply_funding(symbol: str):
    """Compute the funding rate for this interval and apply it to every open
    position in the symbol. Longs pay positive rate, shorts pay negative rate.
    """
    with _db_lock, db() as conn:
        m = conn.execute("SELECT * FROM perp_markets WHERE symbol=?", (symbol,)).fetchone()
        if not m:
            return
        prem = D(m["premium_ema"])
        clamp_v = D(m["funding_clamp"])
        rate = clamp(prem, -clamp_v, clamp_v)
        mark = D(m["mark_price"] or "0")
        now = now_s()
        cur = conn.execute(
            "INSERT INTO perp_funding_history (symbol, funding_rate, funding_time, applied_count) "
            "VALUES (?,?,?,0)",
            (symbol, str(rate), now),
        )
        funding_id = cur.lastrowid
        positions = conn.execute(
            "SELECT * FROM perp_positions WHERE symbol=? AND closed_at IS NULL",
            (symbol,),
        ).fetchall()
        applied = 0
        for p in positions:
            qty = D(p["quantity"])
            notional = qty * mark
            if p["side"] == "LONG":
                fee = notional * rate
            else:
                fee = -notional * rate
            row = conn.execute(
                "SELECT wallet_balance FROM perp_balances WHERE opex_user=? AND asset='USDT'",
                (p["opex_user"],),
            ).fetchone()
            cur_bal = D(row["wallet_balance"] if row else "0")
            new_bal = cur_bal - fee
            _ensure_balance_row(conn, p["opex_user"])
            conn.execute(
                "UPDATE perp_balances SET wallet_balance=? " "WHERE opex_user=? AND asset='USDT'",
                (str(new_bal), p["opex_user"]),
            )
            new_total_funding = D(p["total_funding"]) + fee
            conn.execute(
                "UPDATE perp_positions SET total_funding=? WHERE id=?",
                (str(new_total_funding), p["id"]),
            )
            conn.execute(
                "INSERT INTO perp_income_history "
                "(opex_user, symbol, income_type, income, asset, time, related_order_id) "
                "VALUES (?,?,?,?,?,?,NULL)",
                (p["opex_user"], symbol, "FUNDING_FEE", str(-fee), "USDT", now),
            )
            applied += 1
        conn.execute(
            "UPDATE perp_funding_history SET applied_count=? WHERE id=?",
            (applied, funding_id),
        )
        next_fund = now + int(m["funding_interval_seconds"])
        conn.execute(
            "UPDATE perp_markets SET last_funding_at=?, next_funding_at=?, "
            "last_funding_rate=? WHERE symbol=?",
            (now, next_fund, str(rate), symbol),
        )
        log(f"funding {symbol} rate={rate} applied to {applied} positions")


# --- Book maintenance thread -------------------------------------------
def book_cleanup_loop():
    """Tick every BOOK_CLEAN_TICK_S to take a book snapshot and prune stale
    IOC residuals (in case any escaped the synchronous path)."""
    log("book cleanup loop start")
    while not _stop_event.is_set():
        try:
            _book_cleanup_tick()
        except Exception as e:  # noqa: BLE001
            log(f"book cleanup crashed: {e!r}")
        _stop_event.wait(BOOK_CLEAN_TICK_S)


def _book_cleanup_tick():
    with _book_lock:
        for symbol, book in _books.items():
            best_bid, best_ask = _book_best(symbol)
            mid = None
            if best_bid is not None and best_ask is not None:
                mid = (best_bid + best_ask) / Decimal(2)
            with _db_lock, db() as conn:
                conn.execute(
                    "INSERT INTO perp_book_snapshots "
                    "(symbol, taken_at, best_bid, best_ask, mid, n_bids, n_asks) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        symbol,
                        now_s(),
                        dstr(best_bid) if best_bid is not None else None,
                        dstr(best_ask) if best_ask is not None else None,
                        dstr(mid) if mid is not None else None,
                        len(book["bids"]),
                        len(book["asks"]),
                    ),
                )
    # Cancel orphan IOC residuals (defensive — should not happen).
    with _db_lock, db() as conn:
        rows = conn.execute(
            "SELECT id, opex_user, symbol, side FROM perp_orders "
            "WHERE status='NEW' AND time_in_force='IOC'"
        ).fetchall()
    for r in rows:
        cancel_order(r["opex_user"], symbol=r["symbol"], order_id=r["id"], reason="IOC_RESIDUAL")


# --- Liquidation watcher -----------------------------------------------
def liquidation_loop():
    log("liquidation watcher start")
    while not _stop_event.is_set():
        try:
            with _liq_lock:
                _liq_tick_once()
        except Exception as e:  # noqa: BLE001
            log(f"liq tick crashed: {e!r}")
        _stop_event.wait(LIQ_TICK_S)


def _isolated_liq_check(pos_row: sqlite3.Row, mark: Decimal, mmr: Decimal) -> bool:
    """Returns True if the isolated position is underwater at ``mark``."""
    qty = D(pos_row["quantity"])
    entry = D(pos_row["entry_price"])
    upnl = unrealized_pnl(pos_row["side"], entry, qty, mark)
    notional = qty * mark
    maint = notional * mmr
    if maint <= 0:
        return False
    equity = D(pos_row["isolated_margin"]) + upnl
    return equity / maint <= Decimal(1)


def _cross_account_health(
    opex_user: str,
) -> tuple[Decimal, list[sqlite3.Row], dict[str, sqlite3.Row]]:
    """Return (margin_ratio, cross_positions, markets_by_symbol).

    margin_ratio = (wallet_balance + sum(cross_upnl)) / sum(cross_maint_margin)
    Returns ratio=+∞ when there's no cross position / no maint margin.
    """
    with db() as conn:
        bal = conn.execute(
            "SELECT wallet_balance FROM perp_balances WHERE opex_user=? AND asset='USDT'",
            (opex_user,),
        ).fetchone()
        wallet = D(bal["wallet_balance"]) if bal else ZERO
        cross_positions = conn.execute(
            "SELECT * FROM perp_positions WHERE opex_user=? AND closed_at IS NULL "
            "AND margin_type='CROSSED'",
            (opex_user,),
        ).fetchall()
        markets = {m["symbol"]: m for m in conn.execute("SELECT * FROM perp_markets").fetchall()}
    if not cross_positions:
        return (Decimal("99999"), [], markets)
    total_upnl = ZERO
    total_maint = ZERO
    for p in cross_positions:
        m = markets.get(p["symbol"])
        if not m or not m["mark_price"]:
            continue
        mark = D(m["mark_price"])
        qty = D(p["quantity"])
        entry = D(p["entry_price"])
        upnl = unrealized_pnl(p["side"], entry, qty, mark)
        mmr = D(m["maintenance_margin_rate"])
        total_upnl += upnl
        total_maint += qty * mark * mmr
    if total_maint <= 0:
        return (Decimal("99999"), cross_positions, markets)
    equity = wallet + total_upnl
    return (equity / total_maint, cross_positions, markets)


def _liq_tick_once():
    with db() as conn:
        positions = conn.execute("SELECT * FROM perp_positions WHERE closed_at IS NULL").fetchall()
        markets = {m["symbol"]: m for m in conn.execute("SELECT * FROM perp_markets").fetchall()}
    # First: isolated positions liquidate independently.
    for p in positions:
        if p["margin_type"] != "ISOLATED":
            continue
        m = markets.get(p["symbol"])
        if not m or not m["mark_price"]:
            continue
        mark = D(m["mark_price"])
        mmr = D(m["maintenance_margin_rate"])
        if _isolated_liq_check(p, mark, mmr):
            _force_liquidate(p["id"], mark, partial_close_qty=None, reason="liquidation")

    # Then: cross-margin accounts. Group by user.
    cross_users: set[str] = set()
    for p in positions:
        if p["margin_type"] == "CROSSED":
            cross_users.add(p["opex_user"])
    for u in cross_users:
        _cross_liq_iterate(u)


def _cross_liq_iterate(opex_user: str, max_iters: int = 20):
    """Iterative partial-close cross-margin liquidation: while margin_ratio
    < 1.0, close the worst (most negative uPnL) position and re-check.
    Loop until ratio >= 1.0 + CROSS_HEALTHY_BUFFER or no positions left."""
    for _ in range(max_iters):
        ratio, positions, markets = _cross_account_health(opex_user)
        if not positions:
            return
        if ratio >= Decimal(1):
            return
        # Liquidate the loss-leader: position with the most negative uPnL.
        worst: sqlite3.Row | None = None
        worst_upnl = Decimal("99999999")
        for p in positions:
            m = markets.get(p["symbol"])
            if not m or not m["mark_price"]:
                continue
            mark = D(m["mark_price"])
            upnl = unrealized_pnl(p["side"], D(p["entry_price"]), D(p["quantity"]), mark)
            if upnl < worst_upnl:
                worst_upnl = upnl
                worst = p
        if worst is None:
            return
        m = markets[worst["symbol"]]
        mark = D(m["mark_price"])
        # Partial close: enough to lift ratio back to 1.2x. If we can't
        # compute a sensible partial, just close the whole position.
        full_qty = D(worst["quantity"])
        # Approximate: deleveraging by X reduces total_maint by X*mark*mmr
        # and changes upnl proportionally (the position's upnl share). Solve
        # for the smallest fraction f such that ratio >= 1.0+buffer after.
        # The math gets fuzzy when other positions are also flipping, so we
        # just close the full worst position and let the loop re-check.
        _force_liquidate(worst["id"], mark, partial_close_qty=full_qty, reason="cross_liquidation")
    log(f"cross-liq for {opex_user} hit max iters; manual review")


def _force_liquidate(
    pos_id: int, mark: Decimal, *, partial_close_qty: Decimal | None, reason: str = "liquidation"
):
    """Close (or partially close) the position at ``mark`` synthetically.

    For ISOLATED: leftover isolated_margin (after the 0.3% liq penalty) is
    refunded to the user and the rest goes to the insurance fund.
    For CROSSED:  realized pnl + 0.3% penalty are applied directly to wallet
    balance; the position's "margin" was never carved out so there's nothing
    to refund.
    """
    with _db_lock, db() as conn:
        p = conn.execute(
            "SELECT * FROM perp_positions WHERE id=? AND closed_at IS NULL", (pos_id,)
        ).fetchone()
        if not p:
            return
        entry = D(p["entry_price"])
        side = p["side"]
        margin_type = p["margin_type"]
        full_qty = D(p["quantity"])
        close_qty = full_qty if partial_close_qty is None else min(partial_close_qty, full_qty)
        if close_qty <= 0:
            return
        realized = unrealized_pnl(side, entry, close_qty, mark)
        notional = close_qty * mark
        liq_fee = notional * Decimal("0.003")
        now = now_s()
        opex = p["opex_user"]
        _ensure_balance_row(conn, opex)
        wal_row = conn.execute(
            "SELECT wallet_balance FROM perp_balances WHERE opex_user=? AND asset='USDT'",
            (opex,),
        ).fetchone()
        cur_bal = D(wal_row["wallet_balance"])

        if margin_type == "ISOLATED":
            isol_margin = D(p["isolated_margin"])
            margin_for_close = isol_margin * (close_qty / full_qty)
            leftover = margin_for_close + realized
            net_to_user = leftover - liq_fee
            if net_to_user < 0:
                net_to_user = ZERO
            insurance_take = leftover - net_to_user
            new_bal = cur_bal + net_to_user
        else:
            # CROSSED: directly hit wallet by (realized - liq_fee).
            net_change = realized - liq_fee
            new_bal = cur_bal + net_change
            net_to_user = net_change if net_change > 0 else ZERO
            insurance_take = liq_fee if liq_fee > 0 else ZERO

        conn.execute(
            "UPDATE perp_balances SET wallet_balance=? " "WHERE opex_user=? AND asset='USDT'",
            (str(new_bal), opex),
        )
        cur = conn.execute(
            "INSERT INTO perp_orders "
            "(client_order_id, opex_user, symbol, side, position_side, type, "
            " reduce_only, quantity, remaining_quantity, price, time_in_force, "
            " status, executed_qty, cumulative_quote_qty, avg_fill_price, "
            " created_at, updated_at) "
            "VALUES (?,?,?,?,'BOTH','MARKET',1,?,?,?,?, 'FILLED', ?,?,?,?,?)",
            (
                f"liq-{uuid.uuid4().hex[:16]}",
                opex,
                p["symbol"],
                "SELL" if side == "LONG" else "BUY",
                str(close_qty),
                "0",
                str(mark),
                "GTC",
                str(close_qty),
                str(close_qty * mark),
                str(mark),
                now * 1000,
                now * 1000,
            ),
        )
        order_id = cur.lastrowid
        conn.execute(
            "INSERT INTO perp_trades "
            "(opex_user, symbol, order_id, side, price, quantity, realized_pnl, "
            " commission, commission_asset, is_maker, time) "
            "VALUES (?,?,?,?,?,?,?,?, 'USDT', 0, ?)",
            (
                opex,
                p["symbol"],
                order_id,
                "SELL" if side == "LONG" else "BUY",
                str(mark),
                str(close_qty),
                str(realized),
                str(liq_fee),
                now * 1000,
            ),
        )
        conn.execute(
            "INSERT INTO perp_income_history "
            "(opex_user, symbol, income_type, income, asset, time, related_order_id) "
            "VALUES (?,?, 'REALIZED_PNL', ?, 'USDT', ?, ?)",
            (opex, p["symbol"], str(realized), now, order_id),
        )
        conn.execute(
            "INSERT INTO perp_income_history "
            "(opex_user, symbol, income_type, income, asset, time, related_order_id) "
            "VALUES (?,?, 'COMMISSION', ?, 'USDT', ?, ?)",
            (opex, p["symbol"], str(-liq_fee), now, order_id),
        )
        if insurance_take > 0:
            conn.execute(
                "INSERT INTO perp_income_history "
                "(opex_user, symbol, income_type, income, asset, time, related_order_id) "
                "VALUES (?,?, 'LIQUIDATION_CLEARANCE', ?, 'USDT', ?, ?)",
                (opex, p["symbol"], str(-insurance_take), now, order_id),
            )
            _ensure_balance_row(conn, INSURANCE_USER)
            ins_row = conn.execute(
                "SELECT wallet_balance FROM perp_balances WHERE opex_user=? AND asset='USDT'",
                (INSURANCE_USER,),
            ).fetchone()
            ins_bal = D(ins_row["wallet_balance"])
            conn.execute(
                "UPDATE perp_balances SET wallet_balance=? " "WHERE opex_user=? AND asset='USDT'",
                (str(ins_bal + insurance_take), INSURANCE_USER),
            )
        # Reduce or close the position.
        remaining = full_qty - close_qty
        if remaining <= 0:
            conn.execute(
                "UPDATE perp_positions SET closed_at=?, close_reason=?, "
                "realized_pnl=CAST(CAST(realized_pnl AS REAL)+? AS TEXT), "
                "total_commission=CAST(CAST(total_commission AS REAL)+? AS TEXT), "
                "quantity='0' WHERE id=?",
                (now, reason, float(realized), float(liq_fee), pos_id),
            )
        else:
            # Pro-rate isolated_margin if isolated.
            if margin_type == "ISOLATED":
                new_isol = D(p["isolated_margin"]) * (remaining / full_qty)
            else:
                new_isol = D(p["isolated_margin"])  # 0 for crossed
            conn.execute(
                "UPDATE perp_positions SET quantity=?, isolated_margin=?, "
                "realized_pnl=CAST(CAST(realized_pnl AS REAL)+? AS TEXT), "
                "total_commission=CAST(CAST(total_commission AS REAL)+? AS TEXT) "
                "WHERE id=?",
                (str(remaining), str(new_isol), float(realized), float(liq_fee), pos_id),
            )
        log(
            f"{reason} pos_id={pos_id} user={opex} sym={p['symbol']} "
            f"close_qty={close_qty}/{full_qty} mark={mark} realized={realized}"
        )


# =====================================================================
# Order placement — CLOB matching
# =====================================================================
def cancel_order(
    opex_user: str,
    *,
    symbol: str,
    order_id: int | None = None,
    client_order_id: str | None = None,
    reason: str | None = None,
) -> dict | None:
    """Remove a resting order from the book, refund the proportional reserved
    margin, and mark the row CANCELED (or PARTIALLY_FILLED_CANCELED)."""
    with _book_lock, _db_lock, db() as conn:
        if order_id:
            row = conn.execute(
                "SELECT * FROM perp_orders WHERE id=? AND opex_user=? AND symbol=?",
                (order_id, opex_user, symbol),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM perp_orders WHERE client_order_id=? AND opex_user=? AND symbol=?",
                (client_order_id, opex_user, symbol),
            ).fetchone()
        if not row:
            return None
        if row["status"] in (
            "FILLED",
            "CANCELED",
            "PARTIALLY_FILLED_CANCELED",
            "REJECTED",
            "EXPIRED",
        ):
            return _order_to_dict(row)
        # Pull from book.
        _book_remove_order(symbol, row["id"])
        orig_qty = D(row["quantity"])
        executed = D(row["executed_qty"])
        remaining = orig_qty - executed
        # Refund the proportional reserved margin.
        reserved = D(row["reserved_margin"] or "0")
        refund = reserved * (remaining / orig_qty) if orig_qty > 0 else ZERO
        if refund > 0:
            _ensure_balance_row(conn, opex_user)
            bal = conn.execute(
                "SELECT wallet_balance FROM perp_balances WHERE opex_user=? AND asset='USDT'",
                (opex_user,),
            ).fetchone()
            new_bal = D(bal["wallet_balance"]) + refund
            conn.execute(
                "UPDATE perp_balances SET wallet_balance=? " "WHERE opex_user=? AND asset='USDT'",
                (str(new_bal), opex_user),
            )
            # Reduce reserved_margin on the order so subsequent
            # accounting doesn't double-refund.
            conn.execute(
                "UPDATE perp_orders SET reserved_margin=? WHERE id=?",
                (str(reserved - refund), row["id"]),
            )
        new_status = "CANCELED" if executed == 0 else "PARTIALLY_FILLED_CANCELED"
        conn.execute(
            "UPDATE perp_orders SET status=?, remaining_quantity='0', "
            "cancel_reason=?, updated_at=? WHERE id=?",
            (new_status, reason, now_ms(), row["id"]),
        )
        row = conn.execute("SELECT * FROM perp_orders WHERE id=?", (row["id"],)).fetchone()
    return _order_to_dict(row)


def _apply_fill_to_user(
    conn: sqlite3.Connection,
    *,
    opex_user: str,
    symbol: str,
    fill_side: str,
    fill_price: Decimal,
    fill_qty: Decimal,
    is_maker: bool,
    order_id: int,
    market: sqlite3.Row,
    leverage: int,
    reduce_only: bool,
    margin_type: str,
) -> tuple[Decimal, Decimal]:
    """Apply a single fill to a user's positions. Returns (realized_pnl, fee).

    All wallet/position updates happen inside the passed-in connection (caller
    holds _db_lock). The fill_side is the order's side (BUY/SELL); the resulting
    position side is LONG for BUY, SHORT for SELL.
    """
    fee_rate = MAKER_FEE if is_maker else TAKER_FEE
    notional = fill_qty * fill_price
    fee = notional * fee_rate
    desired_side = "LONG" if fill_side == "BUY" else "SHORT"

    # Get the (single) open position on this symbol.
    existing = conn.execute(
        "SELECT * FROM perp_positions WHERE opex_user=? AND symbol=? "
        "AND closed_at IS NULL ORDER BY opened_at DESC LIMIT 1",
        (opex_user, symbol),
    ).fetchone()

    _ensure_balance_row(conn, opex_user)
    bal_row = conn.execute(
        "SELECT wallet_balance FROM perp_balances WHERE opex_user=? AND asset='USDT'",
        (opex_user,),
    ).fetchone()
    wallet = D(bal_row["wallet_balance"])
    realized = ZERO
    now = now_s()

    if existing and existing["side"] != desired_side:
        # Reducing / flipping the opposite side.
        existing_qty = D(existing["quantity"])
        existing_entry = D(existing["entry_price"])
        existing_margin = D(existing["isolated_margin"])
        close_qty = min(fill_qty, existing_qty)
        pnl_close = unrealized_pnl(existing["side"], existing_entry, close_qty, fill_price)
        realized = pnl_close
        if existing["margin_type"] == "ISOLATED":
            refund_margin = (
                existing_margin * (close_qty / existing_qty) if existing_qty > 0 else ZERO
            )
            new_wallet = wallet + refund_margin + pnl_close - fee
        else:
            refund_margin = ZERO
            new_wallet = wallet + pnl_close - fee
        conn.execute(
            "UPDATE perp_balances SET wallet_balance=? " "WHERE opex_user=? AND asset='USDT'",
            (str(new_wallet), opex_user),
        )
        wallet = new_wallet
        remaining_existing = existing_qty - close_qty
        if remaining_existing <= 0:
            conn.execute(
                "UPDATE perp_positions SET closed_at=?, close_reason='manual', "
                "realized_pnl=?, total_commission=CAST(CAST(total_commission AS REAL)+? AS TEXT), "
                "quantity='0', isolated_margin='0' WHERE id=?",
                (now, str(D(existing["realized_pnl"]) + pnl_close), float(fee), existing["id"]),
            )
        else:
            conn.execute(
                "UPDATE perp_positions SET quantity=?, isolated_margin=?, "
                "realized_pnl=?, total_commission=CAST(CAST(total_commission AS REAL)+? AS TEXT) "
                "WHERE id=?",
                (
                    str(remaining_existing),
                    str(existing_margin - refund_margin),
                    str(D(existing["realized_pnl"]) + pnl_close),
                    float(fee),
                    existing["id"],
                ),
            )
        leftover_qty = fill_qty - close_qty
        if leftover_qty > 0 and not reduce_only:
            new_notional = leftover_qty * fill_price
            new_initial_margin = new_notional / Decimal(leverage)
            # In CROSSED mode there's no per-position margin; the whole
            # wallet backs every position.
            if margin_type == "ISOLATED":
                # Check available margin (wallet minus other isolated margins).
                used = conn.execute(
                    "SELECT COALESCE(SUM(CAST(isolated_margin AS REAL)),0) AS s "
                    "FROM perp_positions WHERE opex_user=? AND closed_at IS NULL "
                    "AND id!=?",
                    (opex_user, existing["id"]),
                ).fetchone()
                used_margin = D(str(used["s"] or 0))
                if wallet - used_margin < new_initial_margin:
                    # Not enough free margin to flip; leave the close as-is.
                    return realized, fee
                new_wallet2 = wallet - new_initial_margin
                conn.execute(
                    "UPDATE perp_balances SET wallet_balance=? "
                    "WHERE opex_user=? AND asset='USDT'",
                    (str(new_wallet2), opex_user),
                )
                isol_margin_val = new_initial_margin
            else:
                isol_margin_val = ZERO
            liq_px = liquidation_price(
                desired_side,
                fill_price,
                leftover_qty,
                isol_margin_val if margin_type == "ISOLATED" else (wallet * Decimal("0.5")),
                D(market["maintenance_margin_rate"]),
            )
            conn.execute(
                "INSERT INTO perp_positions "
                "(opex_user, symbol, side, quantity, entry_price, isolated_margin, "
                " leverage, liquidation_price, margin_type, opened_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    opex_user,
                    symbol,
                    desired_side,
                    str(leftover_qty),
                    str(fill_price),
                    str(isol_margin_val),
                    leverage,
                    str(liq_px),
                    margin_type,
                    now,
                ),
            )
    else:
        # Opening (same side or no existing).
        if reduce_only:
            # Reduce-only against nothing: just take the fee but otherwise no-op.
            new_wallet = wallet - fee
            conn.execute(
                "UPDATE perp_balances SET wallet_balance=? " "WHERE opex_user=? AND asset='USDT'",
                (str(new_wallet), opex_user),
            )
            return realized, fee
        new_initial_margin = (fill_qty * fill_price) / Decimal(leverage)
        if margin_type == "ISOLATED":
            new_wallet = wallet - new_initial_margin - fee
            isol_val = new_initial_margin
        else:
            new_wallet = wallet - fee
            isol_val = ZERO
        conn.execute(
            "UPDATE perp_balances SET wallet_balance=? " "WHERE opex_user=? AND asset='USDT'",
            (str(new_wallet), opex_user),
        )
        if existing:
            old_qty = D(existing["quantity"])
            old_entry = D(existing["entry_price"])
            old_margin = D(existing["isolated_margin"])
            new_qty = old_qty + fill_qty
            new_entry = (old_qty * old_entry + fill_qty * fill_price) / new_qty
            new_margin = old_margin + isol_val
            liq_px = liquidation_price(
                desired_side,
                new_entry,
                new_qty,
                new_margin
                if existing["margin_type"] == "ISOLATED"
                else (D(wallet) * Decimal("0.5") + Decimal("1")),
                D(market["maintenance_margin_rate"]),
            )
            conn.execute(
                "UPDATE perp_positions SET quantity=?, entry_price=?, "
                "isolated_margin=?, leverage=?, liquidation_price=?, "
                "total_commission=CAST(CAST(total_commission AS REAL)+? AS TEXT) "
                "WHERE id=?",
                (
                    str(new_qty),
                    str(new_entry),
                    str(new_margin),
                    leverage,
                    str(liq_px),
                    float(fee),
                    existing["id"],
                ),
            )
        else:
            liq_px = liquidation_price(
                desired_side,
                fill_price,
                fill_qty,
                isol_val if margin_type == "ISOLATED" else (wallet * Decimal("0.5") + Decimal("1")),
                D(market["maintenance_margin_rate"]),
            )
            conn.execute(
                "INSERT INTO perp_positions "
                "(opex_user, symbol, side, quantity, entry_price, isolated_margin, "
                " leverage, liquidation_price, margin_type, opened_at, total_commission) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    opex_user,
                    symbol,
                    desired_side,
                    str(fill_qty),
                    str(fill_price),
                    str(isol_val),
                    leverage,
                    str(liq_px),
                    margin_type,
                    now,
                    str(fee),
                ),
            )
    return realized, fee


def place_order(
    opex_user: str,
    *,
    symbol: str,
    side: str,
    order_type: str,
    quantity: Decimal,
    price: Decimal | None,
    reduce_only: bool,
    client_order_id: str | None,
    time_in_force: str,
    post_only: bool = False,
) -> dict:
    """CLOB place + match. Returns a Binance-shape order dict.

    Order types:
      * MARKET   — walks the book at any price; refund residual margin.
      * LIMIT    — match opposite side at <= price (BUY) / >= price (SELL),
                   then rest the residual.
      * POST_ONLY — alias for LIMIT with post_only=True.
    timeInForce:
      * GTC (default), IOC (cancel residual), FOK (fill-or-kill),
        GTX (post-only — synonym for post_only=True).
    """
    m = get_market(symbol)
    if not m:
        raise BadRequest(-1121, f"unknown symbol {symbol}", http_status=400)
    if m["status"] != "TRADING":
        raise BadRequest(-1131, f"{symbol} is not trading", http_status=400)
    if quantity <= 0:
        raise BadRequest(-1102, "quantity must be > 0", http_status=400)
    step = D(m["step_size"])
    if quantity < step:
        raise BadRequest(-1111, f"quantity below step size {step}", http_status=400)
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise BadRequest(-1102, "side must be BUY or SELL", http_status=400)
    order_type = order_type.upper()
    time_in_force = (time_in_force or "GTC").upper()
    # Normalize post-only flags.
    if order_type == "POST_ONLY":
        post_only = True
        order_type = "LIMIT"
    if time_in_force == "GTX":
        post_only = True
    if order_type not in ("MARKET", "LIMIT"):
        raise BadRequest(-1102, "type must be MARKET, LIMIT, or POST_ONLY", http_status=400)
    if order_type == "LIMIT":
        if price is None or price <= 0:
            raise BadRequest(-1102, "LIMIT requires price > 0", http_status=400)
    leverage = get_leverage(opex_user, symbol, int(m["max_leverage"]))
    if leverage <= 0 or leverage > int(m["max_leverage"]):
        leverage = int(m["max_leverage"])

    margin_type = _get_user_symbol_margin_type(opex_user, symbol)
    mark = D(m["mark_price"] or "0")

    # Match price: market BUY uses +inf, market SELL uses 0.
    if order_type == "MARKET":
        match_limit = INF if side == "BUY" else ZERO
    else:
        match_limit = price

    now_ms_v = now_ms()
    # Margin reservation for a LIMIT order: the post-fill state requires up to
    # (qty*price)/leverage as initial margin (for the opening case). We
    # reserve that up-front (only for ISOLATED — cross uses the whole wallet).
    if order_type == "LIMIT" and margin_type == "ISOLATED":
        reserve = (quantity * price) / Decimal(leverage)
    else:
        reserve = ZERO

    # The matching engine work is interleaved with DB writes; we hold both
    # locks for the entire duration so the in-memory book and the persisted
    # state stay consistent.
    with _book_lock, _db_lock, db() as conn:
        # Insert order row.
        cur = conn.execute(
            "INSERT INTO perp_orders "
            "(client_order_id, opex_user, symbol, side, position_side, type, "
            " reduce_only, post_only, quantity, remaining_quantity, price, "
            " time_in_force, status, executed_qty, cumulative_quote_qty, "
            " avg_fill_price, reserved_margin, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'NEW', '0','0', NULL, '0', ?, ?)",
            (
                client_order_id or f"x-{uuid.uuid4().hex[:16]}",
                opex_user,
                symbol,
                side,
                "BOTH",
                order_type,
                1 if reduce_only else 0,
                1 if post_only else 0,
                str(quantity),
                str(quantity),
                str(price) if price is not None else None,
                time_in_force,
                now_ms_v,
                now_ms_v,
            ),
        )
        order_row_id = cur.lastrowid

        # ---- pre-checks -------------------------------------------------
        if reduce_only:
            existing = conn.execute(
                "SELECT * FROM perp_positions WHERE opex_user=? AND symbol=? "
                "AND closed_at IS NULL ORDER BY opened_at DESC LIMIT 1",
                (opex_user, symbol),
            ).fetchone()
            if not existing or existing["side"] == ("LONG" if side == "BUY" else "SHORT"):
                conn.execute(
                    "UPDATE perp_orders SET status='REJECTED', cancel_reason='reduce_only_no_position', updated_at=? "
                    "WHERE id=?",
                    (now_ms_v, order_row_id),
                )
                raise BadRequest(-2022, "reduceOnly with no opposing position", http_status=400)

        # Available-margin pre-check for opening orders (ISOLATED only).
        if margin_type == "ISOLATED" and not reduce_only:
            bal = conn.execute(
                "SELECT wallet_balance FROM perp_balances WHERE opex_user=? AND asset='USDT'",
                (opex_user,),
            ).fetchone()
            wallet = D(bal["wallet_balance"]) if bal else ZERO
            used = conn.execute(
                "SELECT COALESCE(SUM(CAST(isolated_margin AS REAL)),0) AS s "
                "FROM perp_positions WHERE opex_user=? AND closed_at IS NULL",
                (opex_user,),
            ).fetchone()
            used_margin = D(str(used["s"] or 0))
            resv = conn.execute(
                "SELECT COALESCE(SUM(CAST(reserved_margin AS REAL)),0) AS s "
                "FROM perp_orders WHERE opex_user=? AND status IN ('NEW','PARTIALLY_FILLED') "
                "AND id != ?",
                (opex_user, order_row_id),
            ).fetchone()
            reserved_other = D(str(resv["s"] or 0))
            available = wallet - used_margin - reserved_other
            # Required margin for the full quantity, priced at the order's
            # match limit (taker fill price for MARKET, limit price for LIMIT).
            ref_price = mark if order_type == "MARKET" else price
            need = (quantity * ref_price) / Decimal(leverage) if ref_price else ZERO
            if available < need:
                conn.execute(
                    "UPDATE perp_orders SET status='REJECTED', "
                    "cancel_reason='insufficient_margin', updated_at=? WHERE id=?",
                    (now_ms_v, order_row_id),
                )
                raise BadRequest(-2019, "Margin is insufficient", http_status=400)

        # ---- POST_ONLY pre-cross check ----------------------------------
        book = _book_for(symbol)
        if post_only:
            # If we'd cross the spread, reject.
            crosses = False
            if side == "BUY":
                if book["asks"] and book["asks"][0]["price"] <= price:
                    crosses = True
            else:
                if book["bids"] and book["bids"][0]["price"] >= price:
                    crosses = True
            if crosses:
                conn.execute(
                    "UPDATE perp_orders SET status='REJECTED', "
                    "cancel_reason='post_only_would_take', updated_at=? WHERE id=?",
                    (now_ms_v, order_row_id),
                )
                raise BadRequest(-5022, "Post-only order would be a taker", http_status=400)

        # ---- match loop -------------------------------------------------
        remaining = quantity
        executed_qty = ZERO
        cum_quote = ZERO
        total_realized = ZERO
        total_fee = ZERO
        fills: list[dict] = []
        # Reserve margin upfront for LIMIT orders (will refund residual at end
        # if matched / refund all if canceled later).
        if reserve > 0:
            conn.execute(
                "UPDATE perp_orders SET reserved_margin=? WHERE id=?",
                (str(reserve), order_row_id),
            )
            # We don't actually carve it out of the balance here; we just track
            # it in the order row so available_balance accounts for it.

        if side == "BUY":
            opposite = book["asks"]
        else:
            opposite = book["bids"]

        while remaining > 0 and opposite:
            top = opposite[0]
            # Price check: side BUY consumes asks at top["price"] <= match_limit
            #              side SELL consumes bids at top["price"] >= match_limit
            if side == "BUY" and top["price"] > match_limit:
                break
            if side == "SELL" and top["price"] < match_limit:
                break
            # Self-trade prevention: skip own maker order — pop it back to the
            # owner's resting state but never cross.
            if top["opex_user"] == opex_user:
                # Just bail to avoid self-match; a real exchange would cancel
                # the older order. Keep it simple for the demo.
                break
            trade_qty = min(remaining, top["quantity"])
            trade_price = top["price"]  # maker price wins
            # Apply fill to maker (is_maker=True) and taker (is_maker=False).
            r_maker, f_maker = _apply_fill_to_user(
                conn,
                opex_user=top["opex_user"],
                symbol=symbol,
                fill_side=("SELL" if side == "BUY" else "BUY"),
                fill_price=trade_price,
                fill_qty=trade_qty,
                is_maker=True,
                order_id=top["order_id"],
                market=m,
                leverage=get_leverage(top["opex_user"], symbol, int(m["max_leverage"])),
                reduce_only=False,
                margin_type=_get_user_symbol_margin_type(top["opex_user"], symbol),
            )
            r_taker, f_taker = _apply_fill_to_user(
                conn,
                opex_user=opex_user,
                symbol=symbol,
                fill_side=side,
                fill_price=trade_price,
                fill_qty=trade_qty,
                is_maker=False,
                order_id=order_row_id,
                market=m,
                leverage=leverage,
                reduce_only=reduce_only,
                margin_type=margin_type,
            )
            # Write trade rows: one for the maker, one for the taker.
            conn.execute(
                "INSERT INTO perp_trades "
                "(opex_user, symbol, order_id, side, price, quantity, realized_pnl, "
                " commission, commission_asset, is_maker, time) "
                "VALUES (?,?,?,?,?,?,?,?, 'USDT', 1, ?)",
                (
                    top["opex_user"],
                    symbol,
                    top["order_id"],
                    "SELL" if side == "BUY" else "BUY",
                    str(trade_price),
                    str(trade_qty),
                    str(r_maker),
                    str(f_maker),
                    now_ms_v,
                ),
            )
            conn.execute(
                "INSERT INTO perp_trades "
                "(opex_user, symbol, order_id, side, price, quantity, realized_pnl, "
                " commission, commission_asset, is_maker, time) "
                "VALUES (?,?,?,?,?,?,?,?, 'USDT', 0, ?)",
                (
                    opex_user,
                    symbol,
                    order_row_id,
                    side,
                    str(trade_price),
                    str(trade_qty),
                    str(r_taker),
                    str(f_taker),
                    now_ms_v,
                ),
            )
            # Income rows for both.
            for u, fee_val, pnl_val in [
                (top["opex_user"], f_maker, r_maker),
                (opex_user, f_taker, r_taker),
            ]:
                conn.execute(
                    "INSERT INTO perp_income_history "
                    "(opex_user, symbol, income_type, income, asset, time, related_order_id) "
                    "VALUES (?,?, 'COMMISSION', ?, 'USDT', ?, ?)",
                    (u, symbol, str(-fee_val), now_s(), order_row_id),
                )
                if pnl_val != 0:
                    conn.execute(
                        "INSERT INTO perp_income_history "
                        "(opex_user, symbol, income_type, income, asset, time, related_order_id) "
                        "VALUES (?,?, 'REALIZED_PNL', ?, 'USDT', ?, ?)",
                        (u, symbol, str(pnl_val), now_s(), order_row_id),
                    )

            executed_qty += trade_qty
            cum_quote += trade_qty * trade_price
            total_realized += r_taker
            total_fee += f_taker
            remaining -= trade_qty
            # Reduce maker order
            top["quantity"] -= trade_qty
            # Update the persisted maker order row.
            maker_row = conn.execute(
                "SELECT * FROM perp_orders WHERE id=?", (top["order_id"],)
            ).fetchone()
            maker_executed = D(maker_row["executed_qty"]) + trade_qty
            maker_remaining = D(maker_row["quantity"]) - maker_executed
            maker_cum = D(maker_row["cumulative_quote_qty"]) + trade_qty * trade_price
            maker_avg = (maker_cum / maker_executed) if maker_executed > 0 else ZERO
            maker_status = "FILLED" if maker_remaining <= 0 else "PARTIALLY_FILLED"
            # Refund maker's per-fill reserved margin proportional to the qty.
            maker_reserved = D(maker_row["reserved_margin"] or "0")
            if maker_reserved > 0 and D(maker_row["quantity"]) > 0:
                refund_chunk = maker_reserved * (trade_qty / D(maker_row["quantity"]))
                new_maker_reserved = maker_reserved - refund_chunk
            else:
                new_maker_reserved = maker_reserved
            conn.execute(
                "UPDATE perp_orders SET executed_qty=?, remaining_quantity=?, "
                "cumulative_quote_qty=?, avg_fill_price=?, status=?, "
                "reserved_margin=?, updated_at=? WHERE id=?",
                (
                    str(maker_executed),
                    str(maker_remaining),
                    str(maker_cum),
                    str(maker_avg),
                    maker_status,
                    str(new_maker_reserved),
                    now_ms_v,
                    top["order_id"],
                ),
            )
            if top["quantity"] <= 0:
                opposite.pop(0)
                _bump_update_id(symbol)
            fills.append({"price": trade_price, "qty": trade_qty, "maker_uid": top["opex_user"]})
            # Feed real perp trade -> premium EMA sample.
            spot_px = D(m["index_price"]) if m["index_price"] else None
            _record_perp_trade_for_premium(symbol, trade_price, spot_px)

        # ---- residual handling ------------------------------------------
        if remaining > 0:
            if order_type == "MARKET":
                # Refund the residual: just reject the unfilled chunk.
                if executed_qty == 0:
                    conn.execute(
                        "UPDATE perp_orders SET status='REJECTED', "
                        "cancel_reason='no_liquidity', updated_at=? WHERE id=?",
                        (now_ms_v, order_row_id),
                    )
                    raise BadRequest(
                        -2010,
                        "Cannot execute market order; insufficient liquidity",
                        http_status=400,
                    )
                # Partial fill — close the order out without resting.
                conn.execute(
                    "UPDATE perp_orders SET status='PARTIALLY_FILLED_CANCELED', "
                    "cancel_reason='no_liquidity_residual', "
                    "executed_qty=?, remaining_quantity='0', "
                    "cumulative_quote_qty=?, avg_fill_price=?, updated_at=? "
                    "WHERE id=?",
                    (
                        str(executed_qty),
                        str(cum_quote),
                        str(cum_quote / executed_qty) if executed_qty > 0 else None,
                        now_ms_v,
                        order_row_id,
                    ),
                )
            elif time_in_force == "FOK":
                # FOK: rolled-back. Reject + refund any margin used by the
                # taker fills isn't trivial (positions already changed), so
                # in practice FOK should only fully fill or wholly reject.
                # We approximate: if not fully filled, mark canceled and
                # leave residual unrested. (Strict FOK would require rollback
                # of executed fills — out of scope for the demo.)
                conn.execute(
                    "UPDATE perp_orders SET status='EXPIRED', "
                    "cancel_reason='fok_partial', executed_qty=?, "
                    "cumulative_quote_qty=?, avg_fill_price=?, "
                    "remaining_quantity='0', updated_at=? WHERE id=?",
                    (
                        str(executed_qty),
                        str(cum_quote),
                        str(cum_quote / executed_qty) if executed_qty > 0 else None,
                        now_ms_v,
                        order_row_id,
                    ),
                )
            elif time_in_force == "IOC":
                conn.execute(
                    "UPDATE perp_orders SET status='PARTIALLY_FILLED_CANCELED' " "WHERE id=?",
                    (order_row_id,),
                )
                conn.execute(
                    "UPDATE perp_orders SET executed_qty=?, remaining_quantity='0', "
                    "cumulative_quote_qty=?, avg_fill_price=?, "
                    "cancel_reason='ioc_residual', updated_at=? WHERE id=?",
                    (
                        str(executed_qty),
                        str(cum_quote),
                        str(cum_quote / executed_qty) if executed_qty > 0 else None,
                        now_ms_v,
                        order_row_id,
                    ),
                )
            else:
                # LIMIT GTC / GTX (post-only that didn't cross): rest the residual.
                # Adjust the reserved margin proportional to remaining qty.
                if order_type == "LIMIT" and margin_type == "ISOLATED":
                    new_reserve = (remaining * price) / Decimal(leverage)
                else:
                    new_reserve = ZERO
                conn.execute(
                    "UPDATE perp_orders SET status=?, executed_qty=?, "
                    "remaining_quantity=?, cumulative_quote_qty=?, "
                    "avg_fill_price=?, reserved_margin=?, updated_at=? WHERE id=?",
                    (
                        "PARTIALLY_FILLED" if executed_qty > 0 else "NEW",
                        str(executed_qty),
                        str(remaining),
                        str(cum_quote),
                        str(cum_quote / executed_qty) if executed_qty > 0 else None,
                        str(new_reserve),
                        now_ms_v,
                        order_row_id,
                    ),
                )
                level = {
                    "order_id": order_row_id,
                    "opex_user": opex_user,
                    "price": price,
                    "quantity": remaining,
                    "ts": now_ms_v,
                }
                _book_add(symbol, side, level)
        else:
            # Fully filled.
            conn.execute(
                "UPDATE perp_orders SET status='FILLED', executed_qty=?, "
                "remaining_quantity='0', cumulative_quote_qty=?, "
                "avg_fill_price=?, reserved_margin='0', updated_at=? WHERE id=?",
                (
                    str(executed_qty),
                    str(cum_quote),
                    str(cum_quote / executed_qty) if executed_qty > 0 else None,
                    now_ms_v,
                    order_row_id,
                ),
            )

        # Refresh last trade price on market row so the pricing loop has
        # something to fall back to when the book empties.
        if executed_qty > 0:
            last_fill_px = fills[-1]["price"]
            conn.execute(
                "UPDATE perp_markets SET last_trade_price=? WHERE symbol=?",
                (str(last_fill_px), symbol),
            )

        # Read back the final order row for the response.
        order_row = conn.execute("SELECT * FROM perp_orders WHERE id=?", (order_row_id,)).fetchone()
        order_dict = _order_to_dict(order_row)
    return order_dict


def _order_to_dict(row: sqlite3.Row) -> dict:
    return {
        "orderId": row["id"],
        "clientOrderId": row["client_order_id"],
        "symbol": row["symbol"],
        "status": row["status"],
        "side": row["side"],
        "positionSide": row["position_side"] or "BOTH",
        "type": row["type"],
        "reduceOnly": bool(row["reduce_only"]),
        "postOnly": bool(_safe_col(row, "post_only", 0)),
        "origQty": dstr(row["quantity"]),
        "remainingQty": dstr(_safe_col(row, "remaining_quantity", "0") or "0"),
        "price": dstr(row["price"] or "0"),
        "executedQty": dstr(row["executed_qty"]),
        "cumQuote": dstr(row["cumulative_quote_qty"]),
        "avgPrice": dstr(row["avg_fill_price"] or "0"),
        "timeInForce": row["time_in_force"],
        "stopPrice": dstr(row["stop_price"] or "0"),
        "cancelReason": _safe_col(row, "cancel_reason", None),
        "updateTime": row["updated_at"],
        "time": row["created_at"],
    }


def _safe_col(row: sqlite3.Row, name: str, default):
    """sqlite3.Row.__getitem__ raises IndexError for missing columns. We tolerate
    older rows that pre-date a schema migration."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return default


def _position_to_dict(p: sqlite3.Row, m: sqlite3.Row | None) -> dict:
    mark = D(m["mark_price"]) if m and m["mark_price"] else D(p["entry_price"])
    qty = D(p["quantity"])
    entry = D(p["entry_price"])
    upnl = unrealized_pnl(p["side"], entry, qty, mark)
    signed_qty = qty if p["side"] == "LONG" else -qty
    margin_type = p["margin_type"] or "ISOLATED"
    return {
        "symbol": p["symbol"],
        "positionAmt": dstr(signed_qty),
        "entryPrice": dstr(entry),
        "markPrice": dstr(mark),
        "unRealizedProfit": dstr(upnl),
        "liquidationPrice": dstr(p["liquidation_price"] or "0"),
        "leverage": str(p["leverage"]),
        "maxNotionalValue": "0",
        "marginType": margin_type.lower(),
        "isolatedMargin": dstr(p["isolated_margin"]),
        "isAutoAddMargin": "false",
        "positionSide": "BOTH",
        "notional": dstr(qty * mark),
        "isolatedWallet": dstr(p["isolated_margin"]),
        "updateTime": p["opened_at"] * 1000,
        "side": p["side"],
        "openedAt": p["opened_at"],
        "totalFunding": dstr(p["total_funding"]),
        "realizedPnl": dstr(p["realized_pnl"]),
    }


def _get_user_symbol_margin_type(opex_user: str, symbol: str) -> str:
    """Lookup margin mode for (user, symbol). Defaults to ISOLATED."""
    with db() as conn:
        return _get_margin_type(conn, opex_user, symbol)


# --- Spot wallet bridge (HTTP only) ------------------------------------
def spot_debit_usdt(opex_user: str, amount: Decimal, *, ref: str) -> tuple[bool, str]:
    """Debit USDT from the spot wallet via the wallet API."""
    amt_int = int(amount)
    if amt_int <= 0 or Decimal(amt_int) != amount:
        return False, "perp transfer requires a whole-USDT amount"
    url = (
        f"{WALLET_BASE}/v2/transfer/{amt_int}_USDT"
        f"/from/{urllib.parse.quote(opex_user)}_MAIN"
        f"/to/zkcex-futures_MAIN"
    )
    body = json.dumps(
        {
            "description": "zkcex-spot-to-futures",
            "transferRef": ref,
            "transferCategory": "WITHDRAW_REQUEST",
        }
    ).encode()
    req = _http_request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with _http_urlopen(req, timeout=10) as resp:
            if resp.status >= 300:
                return False, f"wallet HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        txt = ""
        try:
            txt = e.read().decode("utf-8", errors="replace")[:240]
        except Exception as read_error:  # noqa: BLE001
            log(f"wallet error body read failed: {read_error!r}")
        return False, f"wallet HTTP {e.code}: {txt}"
    except Exception as e:
        return False, f"wallet transport: {e!r}"
    return True, "ok"


def spot_credit_usdt(opex_user: str, amount: Decimal, *, ref: str) -> tuple[bool, str]:
    """Credit USDT back to the spot wallet via the test-deposit endpoint."""
    amt_int = int(amount)
    if amt_int <= 0 or Decimal(amt_int) != amount:
        return False, "perp transfer requires a whole-USDT amount"
    cap = 10
    remaining = amt_int
    chunk = 0
    while remaining > 0:
        amt = min(cap, remaining)
        path = (
            f"/deposit/{amt}_test-ethereum_USDT/"
            f"{urllib.parse.quote(opex_user)}_MAIN"
            f"?description=zkcex-futures-to-spot&transferRef={urllib.parse.quote(ref + '-' + str(chunk))}"
        )
        try:
            req = _http_request(f"{WALLET_BASE}{path}", method="POST")
            with _http_urlopen(req, timeout=10) as resp:
                if resp.status >= 300:
                    return False, f"wallet HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            return False, f"wallet HTTP {e.code}"
        except Exception as e:
            return False, f"wallet transport: {e!r}"
        remaining -= amt
        chunk += 1
    return True, "ok"


# --- HTTP server --------------------------------------------------------
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "zkcex-perp-engine/2.0"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[perp_engine] {self.address_string()} - {fmt % args}\n")

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

    def _send_error(self, status: int, code: int, msg: str):
        return self._send_json(status, binance_error(code, msg))

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n > 0 else b""

    def _form_or_query(self) -> dict:
        params: dict = {}
        parsed = urllib.parse.urlsplit(self.path)
        for k, vs in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items():
            params[k] = vs[0] if vs else ""
        body = self._read_body()
        if body:
            ct = (self.headers.get("Content-Type") or "").lower()
            if "json" in ct:
                try:
                    obj = json.loads(body.decode("utf-8"))
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            params[k] = v
                except Exception as e:  # noqa: BLE001
                    log(f"request JSON body ignored: {e!r}")
            else:
                for k, vs in urllib.parse.parse_qs(
                    body.decode("utf-8"), keep_blank_values=True
                ).items():
                    params[k] = vs[0] if vs else ""
        return params

    def _opex_user(self) -> str | None:
        return self.headers.get("X-Opex-User") or None

    def _require_user(self) -> str | None:
        u = self._opex_user()
        if not u:
            self._send_error(401, -2014, "API-key format invalid.")
            return None
        return u

    def _is_loopback(self) -> bool:
        ip = self.client_address[0] if self.client_address else ""
        return ip in ("127.0.0.1", "::1", "localhost")

    # ---- CORS -----------------------------------------------------------
    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers", "Content-Type, Authorization, X-Opex-User, X-MBX-APIKEY"
        )
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
        with _otel_server_span(self):
            path = urllib.parse.urlsplit(self.path).path
            method = self.command
            try:
                # Public
                if path == "/fapi/v1/ping" and method == "GET":
                    return self._send_json(200, {})
                if path == "/fapi/v1/time" and method == "GET":
                    return self._send_json(200, {"serverTime": now_ms()})
                if path == "/fapi/v1/exchangeInfo" and method == "GET":
                    return self.h_exchange_info()
                if path == "/fapi/v1/premiumIndex" and method == "GET":
                    return self.h_premium_index()
                if path == "/fapi/v1/fundingRate" and method == "GET":
                    return self.h_funding_rate()
                if path == "/fapi/v1/depth" and method == "GET":
                    return self.h_depth()
                if path == "/fapi/v1/ticker/24hr" and method == "GET":
                    return self.h_ticker_24h()
                if path == "/fapi/v1/health" and method == "GET":
                    return self._send_json(
                        200,
                        {
                            "ok": True,
                            "last_pricing_tick": int(_last_pricing_tick)
                            if _last_pricing_tick
                            else None,
                        },
                    )
                # Admin (loopback-only) — for testing
                if path == "/fapi/v1/_admin/set-mark" and method == "POST":
                    return self.h_admin_set_mark()
                if path == "/fapi/v1/_admin/force-funding" and method == "POST":
                    return self.h_admin_force_funding()
                if path == "/fapi/v1/_admin/force-liq" and method == "POST":
                    return self.h_admin_force_liq()
                # Signed
                if path == "/fapi/v1/order" and method == "POST":
                    return self.h_place_order()
                if path == "/fapi/v1/order" and method == "DELETE":
                    return self.h_cancel_order()
                if path == "/fapi/v1/order" and method == "GET":
                    return self.h_get_order()
                if path == "/fapi/v1/openOrders" and method == "GET":
                    return self.h_open_orders()
                if path == "/fapi/v1/userTrades" and method == "GET":
                    return self.h_user_trades()
                if path == "/fapi/v1/positionRisk" and method == "GET":
                    return self.h_position_risk()
                if path == "/fapi/v1/leverage" and method == "POST":
                    return self.h_set_leverage()
                if path == "/fapi/v1/marginType" and method == "POST":
                    return self.h_set_margin_type()
                if path == "/fapi/v1/account" and method == "GET":
                    return self.h_account()
                if path == "/fapi/v1/transfer" and method == "POST":
                    return self.h_transfer()
                if path == "/fapi/v1/income" and method == "GET":
                    return self.h_income()
                if path == "/fapi/v1/closePosition" and method == "POST":
                    return self.h_close_position()
                return self._send_json(404, {"code": -1100, "msg": "not_found", "path": path})
            except BadRequest as e:
                return self._send_error(
                    getattr(e, "http_status", 400),
                    int(e.code)
                    if isinstance(e.code, (int, str)) and str(e.code).lstrip("-").isdigit()
                    else -1100,
                    e.message,
                )
            except Exception as e:  # noqa: BLE001
                log(f"handler error on {self.path}: {e!r}")
                import traceback

                traceback.print_exc(file=sys.stderr)
                return self._send_error(500, -1000, "internal error")

    # ---- public handlers -----------------------------------------------
    def h_exchange_info(self):
        markets = list_markets()
        out_syms = []
        for m in markets:
            out_syms.append(
                {
                    "symbol": m["symbol"],
                    "baseAsset": m["base_asset"],
                    "quoteAsset": m["quote_asset"],
                    "marginAsset": "USDT",
                    "contractType": "PERPETUAL",
                    "status": m["status"],
                    "contractSize": m["contract_size"],
                    "tickSize": m["tick_size"],
                    "stepSize": m["step_size"],
                    "maxLeverage": m["max_leverage"],
                    "maintenanceMarginRate": m["maintenance_margin_rate"],
                    "fundingIntervalSeconds": m["funding_interval_seconds"],
                    "fundingClamp": m["funding_clamp"],
                    "indexSymbol": m["spot_index_symbol"],
                    "markBand": dstr(MARK_BAND),
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": m["tick_size"]},
                        {"filterType": "LOT_SIZE", "stepSize": m["step_size"]},
                    ],
                }
            )
        return self._send_json(
            200,
            {
                "timezone": "UTC",
                "serverTime": now_ms(),
                "symbols": out_syms,
            },
        )

    def h_premium_index(self):
        q = self._form_or_query()
        sym = (q.get("symbol") or "").upper().strip()
        rows = list_markets() if not sym else [r for r in [get_market(sym)] if r]
        out = []
        for m in rows:
            next_funding = (int(m["next_funding_at"]) * 1000) if m["next_funding_at"] else 0
            out.append(
                {
                    "symbol": m["symbol"],
                    "markPrice": dstr(m["mark_price"] or "0"),
                    "indexPrice": dstr(m["index_price"] or "0"),
                    "estimatedSettlePrice": dstr(m["mark_price"] or "0"),
                    "lastFundingRate": dstr(m["last_funding_rate"] or "0"),
                    "interestRate": "0",
                    "nextFundingTime": next_funding,
                    "time": now_ms(),
                }
            )
        if sym:
            if not out:
                return self._send_error(404, -1121, f"unknown symbol {sym}")
            return self._send_json(200, out[0])
        return self._send_json(200, out)

    def h_funding_rate(self):
        q = self._form_or_query()
        sym = (q.get("symbol") or "").upper().strip()
        limit = int(q.get("limit") or 100)
        with db() as conn:
            if sym:
                rows = conn.execute(
                    "SELECT * FROM perp_funding_history WHERE symbol=? "
                    "ORDER BY funding_time DESC LIMIT ?",
                    (sym, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM perp_funding_history " "ORDER BY funding_time DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        out = [
            {
                "symbol": r["symbol"],
                "fundingRate": dstr(r["funding_rate"]),
                "fundingTime": int(r["funding_time"]) * 1000,
                "appliedCount": r["applied_count"],
            }
            for r in rows
        ]
        return self._send_json(200, out)

    def h_depth(self):
        q = self._form_or_query()
        sym = (q.get("symbol") or "").upper().strip()
        try:
            limit = min(int(q.get("limit") or 100), 100)
        except (TypeError, ValueError):
            limit = 100
        if not sym:
            return self._send_error(400, -1102, "symbol required")
        if not get_market(sym):
            return self._send_error(404, -1121, f"unknown symbol {sym}")
        with _book_lock:
            snap = _book_depth_snapshot(sym, levels=limit)
        snap.update(
            {
                "T": now_ms(),
                "E": now_ms(),
                "symbol": sym,
            }
        )
        return self._send_json(200, snap)

    def h_ticker_24h(self):
        q = self._form_or_query()
        sym = (q.get("symbol") or "").upper().strip()
        out = []
        for m in list_markets():
            if sym and m["symbol"] != sym:
                continue
            mark = m["mark_price"] or "0"
            idx = m["index_price"] or "0"
            out.append(
                {
                    "symbol": m["symbol"],
                    "lastPrice": mark,
                    "markPrice": mark,
                    "indexPrice": idx,
                    "priceChange": "0",
                    "priceChangePercent": "0",
                    "highPrice": mark,
                    "lowPrice": mark,
                    "volume": "0",
                    "quoteVolume": "0",
                    "openTime": now_ms() - 86400_000,
                    "closeTime": now_ms(),
                }
            )
        if sym:
            if not out:
                return self._send_error(404, -1121, f"unknown symbol {sym}")
            return self._send_json(200, out[0])
        return self._send_json(200, out)

    # ---- admin (loopback only) -----------------------------------------
    def h_admin_set_mark(self):
        if not self._is_loopback():
            return self._send_error(403, -1100, "admin endpoints loopback-only")
        q = self._form_or_query()
        sym = (q.get("symbol") or "").upper().strip()
        price = Dq(q.get("price"), name="price")
        premium = q.get("premium")
        with _db_lock, db() as conn:
            if premium is not None:
                conn.execute(
                    "UPDATE perp_markets SET mark_price=?, index_price=?, premium_ema=? "
                    "WHERE symbol=?",
                    (str(price), str(price), str(D(premium)), sym),
                )
            else:
                conn.execute(
                    "UPDATE perp_markets SET mark_price=?, index_price=? WHERE symbol=?",
                    (str(price), str(price), sym),
                )
        return self._send_json(200, {"ok": True, "symbol": sym, "mark_price": str(price)})

    def h_admin_force_funding(self):
        if not self._is_loopback():
            return self._send_error(403, -1100, "admin endpoints loopback-only")
        q = self._form_or_query()
        sym = (q.get("symbol") or "").upper().strip()
        if not sym:
            return self._send_error(400, -1102, "symbol required")
        apply_funding(sym)
        m = get_market(sym)
        return self._send_json(
            200,
            {"ok": True, "symbol": sym, "last_funding_rate": (m and m["last_funding_rate"]) or "0"},
        )

    def h_admin_force_liq(self):
        """Force one liquidation tick — useful for tests."""
        if not self._is_loopback():
            return self._send_error(403, -1100, "admin endpoints loopback-only")
        with _liq_lock:
            _liq_tick_once()
        return self._send_json(200, {"ok": True})

    # ---- signed handlers -----------------------------------------------
    def h_place_order(self):
        opex = self._require_user()
        if not opex:
            return
        q = self._form_or_query()
        sym = (q.get("symbol") or "").upper().strip()
        side = (q.get("side") or "").upper()
        typ = (q.get("type") or "MARKET").upper()
        if side not in ("BUY", "SELL"):
            return self._send_error(400, -1102, "side must be BUY or SELL")
        qty = Dq(q.get("quantity") or q.get("origQty"), name="quantity")
        price = D(q.get("price") or "0") if q.get("price") else None
        reduce_only = str(q.get("reduceOnly") or q.get("reduce_only") or "false").lower() in (
            "true",
            "1",
            "yes",
        )
        post_only = str(q.get("postOnly") or q.get("post_only") or "false").lower() in (
            "true",
            "1",
            "yes",
        )
        client_oid = q.get("newClientOrderId") or q.get("clientOrderId")
        tif = (q.get("timeInForce") or "GTC").upper()
        try:
            order = place_order(
                opex,
                symbol=sym,
                side=side,
                order_type=typ,
                quantity=qty,
                price=price,
                reduce_only=reduce_only,
                client_order_id=client_oid,
                time_in_force=tif,
                post_only=post_only,
            )
        except BadRequest as e:
            return self._send_error(
                e.http_status, e.code if isinstance(e.code, int) else -2010, e.message
            )
        return self._send_json(200, order)

    def h_cancel_order(self):
        opex = self._require_user()
        if not opex:
            return
        q = self._form_or_query()
        sym = (q.get("symbol") or "").upper().strip()
        order_id = q.get("orderId")
        client_oid = q.get("origClientOrderId") or q.get("clientOrderId")
        if not order_id and not client_oid:
            return self._send_error(400, -1102, "orderId or origClientOrderId required")
        try:
            out = cancel_order(
                opex,
                symbol=sym,
                order_id=int(order_id) if order_id else None,
                client_order_id=client_oid,
                reason="user_cancel",
            )
        except (TypeError, ValueError):
            return self._send_error(400, -1102, "orderId must be integer")
        if out is None:
            return self._send_error(404, -2013, "Order does not exist.")
        return self._send_json(200, out)

    def h_get_order(self):
        opex = self._require_user()
        if not opex:
            return
        q = self._form_or_query()
        sym = (q.get("symbol") or "").upper().strip()
        order_id = q.get("orderId")
        client_oid = q.get("origClientOrderId") or q.get("clientOrderId")
        with db() as conn:
            if order_id:
                row = conn.execute(
                    "SELECT * FROM perp_orders WHERE id=? AND opex_user=? AND symbol=?",
                    (order_id, opex, sym),
                ).fetchone()
            elif client_oid:
                row = conn.execute(
                    "SELECT * FROM perp_orders WHERE client_order_id=? AND opex_user=? AND symbol=?",
                    (client_oid, opex, sym),
                ).fetchone()
            else:
                return self._send_error(400, -1102, "orderId or origClientOrderId required")
            if not row:
                return self._send_error(404, -2013, "Order does not exist.")
        return self._send_json(200, _order_to_dict(row))

    def h_open_orders(self):
        opex = self._require_user()
        if not opex:
            return
        q = self._form_or_query()
        sym = (q.get("symbol") or "").upper().strip()
        with db() as conn:
            if sym:
                rows = conn.execute(
                    "SELECT * FROM perp_orders WHERE opex_user=? AND symbol=? "
                    "AND status IN ('NEW','PARTIALLY_FILLED') ORDER BY id DESC",
                    (opex, sym),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM perp_orders WHERE opex_user=? "
                    "AND status IN ('NEW','PARTIALLY_FILLED') ORDER BY id DESC",
                    (opex,),
                ).fetchall()
        return self._send_json(200, [_order_to_dict(r) for r in rows])

    def h_user_trades(self):
        opex = self._require_user()
        if not opex:
            return
        q = self._form_or_query()
        sym = (q.get("symbol") or "").upper().strip()
        limit = int(q.get("limit") or 100)
        with db() as conn:
            if sym:
                rows = conn.execute(
                    "SELECT * FROM perp_trades WHERE opex_user=? AND symbol=? "
                    "ORDER BY id DESC LIMIT ?",
                    (opex, sym, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM perp_trades WHERE opex_user=? " "ORDER BY id DESC LIMIT ?",
                    (opex, limit),
                ).fetchall()
        return self._send_json(
            200,
            [
                {
                    "id": r["id"],
                    "orderId": r["order_id"],
                    "symbol": r["symbol"],
                    "side": r["side"],
                    "price": dstr(r["price"]),
                    "qty": dstr(r["quantity"]),
                    "realizedPnl": dstr(r["realized_pnl"]),
                    "commission": dstr(r["commission"]),
                    "commissionAsset": r["commission_asset"],
                    "maker": bool(r["is_maker"]),
                    "time": r["time"],
                }
                for r in rows
            ],
        )

    def h_position_risk(self):
        opex = self._require_user()
        if not opex:
            return
        q = self._form_or_query()
        sym = (q.get("symbol") or "").upper().strip()
        markets = {m["symbol"]: m for m in list_markets()}
        with db() as conn:
            if sym:
                rows = conn.execute(
                    "SELECT * FROM perp_positions WHERE opex_user=? AND symbol=? "
                    "AND closed_at IS NULL ORDER BY id DESC",
                    (opex, sym),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM perp_positions WHERE opex_user=? "
                    "AND closed_at IS NULL ORDER BY id DESC",
                    (opex,),
                ).fetchall()
        return self._send_json(200, [_position_to_dict(p, markets.get(p["symbol"])) for p in rows])

    def h_set_leverage(self):
        opex = self._require_user()
        if not opex:
            return
        q = self._form_or_query()
        sym = (q.get("symbol") or "").upper().strip()
        try:
            lev = int(q.get("leverage") or 0)
        except (TypeError, ValueError):
            return self._send_error(400, -1102, "leverage must be an integer")
        m = get_market(sym)
        if not m:
            return self._send_error(404, -1121, f"unknown symbol {sym}")
        if lev <= 0 or lev > int(m["max_leverage"]):
            return self._send_error(400, -1102, f"leverage must be in [1, {m['max_leverage']}]")
        set_leverage(opex, sym, lev)
        return self._send_json(
            200,
            {
                "leverage": lev,
                "maxNotionalValue": "0",
                "symbol": sym,
            },
        )

    def h_set_margin_type(self):
        """POST /fapi/v1/marginType — set ISOLATED / CROSSED for (user, symbol).

        Rejects with -3045 if the user has an open position on the symbol, and
        -4046 if the mode is already what they asked for (Binance behaviour).
        """
        opex = self._require_user()
        if not opex:
            return
        q = self._form_or_query()
        sym = (q.get("symbol") or "").upper().strip()
        mt = (q.get("marginType") or "ISOLATED").upper()
        if mt not in ("ISOLATED", "CROSSED"):
            return self._send_error(400, -1102, "marginType must be ISOLATED or CROSSED")
        m = get_market(sym)
        if not m:
            return self._send_error(404, -1121, f"unknown symbol {sym}")
        with _db_lock, db() as conn:
            current = _get_margin_type(conn, opex, sym)
            if current == mt:
                return self._send_error(400, -4046, "No change of margin type")
            # Block if open positions on this symbol.
            open_pos = conn.execute(
                "SELECT 1 FROM perp_positions WHERE opex_user=? AND symbol=? "
                "AND closed_at IS NULL LIMIT 1",
                (opex, sym),
            ).fetchone()
            if open_pos:
                return self._send_error(
                    400, -3045, "Margin mode cannot be changed with open positions"
                )
            # Also block if there are resting open orders on this symbol —
            # otherwise we'd have to recompute their reserved margin.
            open_ord = conn.execute(
                "SELECT 1 FROM perp_orders WHERE opex_user=? AND symbol=? "
                "AND status IN ('NEW','PARTIALLY_FILLED') LIMIT 1",
                (opex, sym),
            ).fetchone()
            if open_ord:
                return self._send_error(
                    400, -3045, "Margin mode cannot be changed with open orders"
                )
            conn.execute(
                "INSERT INTO perp_margin_settings (opex_user, symbol, margin_type) "
                "VALUES (?,?,?) "
                "ON CONFLICT(opex_user, symbol) DO UPDATE SET margin_type=excluded.margin_type",
                (opex, sym, mt),
            )
        return self._send_json(200, {"code": 200, "msg": "ok", "symbol": sym, "marginType": mt})

    def h_account(self):
        opex = self._require_user()
        if not opex:
            return
        bal = get_balance(opex)
        markets = {m["symbol"]: m for m in list_markets()}
        with db() as conn:
            positions = conn.execute(
                "SELECT * FROM perp_positions WHERE opex_user=? AND closed_at IS NULL",
                (opex,),
            ).fetchall()
            # Expose all per-symbol margin settings on the account too.
            margin_settings = conn.execute(
                "SELECT symbol, margin_type FROM perp_margin_settings " "WHERE opex_user=?",
                (opex,),
            ).fetchall()
        # Compute total maint margin too — small extra info for the UI.
        total_maint = ZERO
        for p in positions:
            m = markets.get(p["symbol"])
            if not m or not m["mark_price"]:
                continue
            total_maint += D(p["quantity"]) * D(m["mark_price"]) * D(m["maintenance_margin_rate"])
        pos_out = [_position_to_dict(p, markets.get(p["symbol"])) for p in positions]
        return self._send_json(
            200,
            {
                "feeTier": 0,
                "canTrade": True,
                "canDeposit": True,
                "canWithdraw": True,
                "updateTime": now_ms(),
                "totalWalletBalance": dstr(bal["wallet_balance"]),
                "totalUnrealizedProfit": dstr(bal["total_unrealized_pnl"]),
                "crossUnPnl": dstr(bal["cross_unrealized_pnl"]),
                "totalMarginBalance": dstr(bal["margin_balance"]),
                "totalInitialMargin": dstr(bal["used_margin"]),
                "totalReservedMargin": dstr(bal["reserved_margin"]),
                "totalMaintMargin": dstr(total_maint),
                "availableBalance": dstr(bal["available_balance"]),
                "maxWithdrawAmount": dstr(bal["available_balance"]),
                "marginSettings": [
                    {"symbol": r["symbol"], "marginType": r["margin_type"]} for r in margin_settings
                ],
                "assets": [
                    {
                        "asset": "USDT",
                        "walletBalance": dstr(bal["wallet_balance"]),
                        "unrealizedProfit": dstr(bal["total_unrealized_pnl"]),
                        "marginBalance": dstr(bal["margin_balance"]),
                        "availableBalance": dstr(bal["available_balance"]),
                        "maxWithdrawAmount": dstr(bal["available_balance"]),
                        "marginAvailable": True,
                    }
                ],
                "positions": pos_out,
            },
        )

    def h_transfer(self):
        opex = self._require_user()
        if not opex:
            return
        q = self._form_or_query()
        asset = (q.get("asset") or "USDT").upper()
        ttype = (q.get("type") or "").upper()
        if asset != "USDT":
            return self._send_error(400, -1102, "only USDT transfers are supported")
        if ttype not in ("SPOT_TO_FUTURES", "FUTURES_TO_SPOT", "MAIN_UMFUTURE", "UMFUTURE_MAIN"):
            return self._send_error(400, -1102, "type must be SPOT_TO_FUTURES or FUTURES_TO_SPOT")
        direction = (
            "SPOT_TO_FUTURES"
            if ttype in ("SPOT_TO_FUTURES", "MAIN_UMFUTURE")
            else "FUTURES_TO_SPOT"
        )
        amount = Dq(q.get("amount"), name="amount")
        ref = f"perp-{opex}-{int(time.time()*1000)}"
        if direction == "SPOT_TO_FUTURES":
            ok, msg = spot_debit_usdt(opex, amount, ref=ref)
            if not ok:
                return self._send_error(400, -2018, f"spot debit failed: {msg}")
            with _db_lock, db() as conn:
                _ensure_balance_row(conn, opex)
                row = conn.execute(
                    "SELECT wallet_balance FROM perp_balances WHERE opex_user=? AND asset='USDT'",
                    (opex,),
                ).fetchone()
                new_bal = D(row["wallet_balance"]) + amount
                conn.execute(
                    "UPDATE perp_balances SET wallet_balance=? "
                    "WHERE opex_user=? AND asset='USDT'",
                    (str(new_bal), opex),
                )
                conn.execute(
                    "INSERT INTO perp_income_history "
                    "(opex_user, symbol, income_type, income, asset, time) "
                    "VALUES (?, NULL, 'TRANSFER', ?, 'USDT', ?)",
                    (opex, str(amount), now_s()),
                )
            return self._send_json(
                200,
                {
                    "tranId": ref,
                    "status": "CONFIRMED",
                    "amount": str(amount),
                    "asset": "USDT",
                    "type": direction,
                },
            )
        else:
            with _db_lock, db() as conn:
                _ensure_balance_row(conn, opex)
                row = conn.execute(
                    "SELECT wallet_balance FROM perp_balances WHERE opex_user=? AND asset='USDT'",
                    (opex,),
                ).fetchone()
                wallet_bal = D(row["wallet_balance"])
                used = conn.execute(
                    "SELECT COALESCE(SUM(CAST(isolated_margin AS REAL)),0) AS s "
                    "FROM perp_positions WHERE opex_user=? AND closed_at IS NULL",
                    (opex,),
                ).fetchone()
                used_margin = D(str(used["s"] or 0))
                available = wallet_bal - used_margin
                if amount > available:
                    return self._send_error(
                        400, -2018, f"insufficient available balance ({available})"
                    )
                conn.execute(
                    "UPDATE perp_balances SET wallet_balance=? "
                    "WHERE opex_user=? AND asset='USDT'",
                    (str(wallet_bal - amount), opex),
                )
            ok, msg = spot_credit_usdt(opex, amount, ref=ref)
            if not ok:
                with _db_lock, db() as conn:
                    row = conn.execute(
                        "SELECT wallet_balance FROM perp_balances WHERE opex_user=? AND asset='USDT'",
                        (opex,),
                    ).fetchone()
                    bal = D(row["wallet_balance"]) + amount
                    conn.execute(
                        "UPDATE perp_balances SET wallet_balance=? "
                        "WHERE opex_user=? AND asset='USDT'",
                        (str(bal), opex),
                    )
                return self._send_error(502, -2018, f"spot credit failed: {msg}")
            with _db_lock, db() as conn:
                conn.execute(
                    "INSERT INTO perp_income_history "
                    "(opex_user, symbol, income_type, income, asset, time) "
                    "VALUES (?, NULL, 'TRANSFER', ?, 'USDT', ?)",
                    (opex, str(-amount), now_s()),
                )
            return self._send_json(
                200,
                {
                    "tranId": ref,
                    "status": "CONFIRMED",
                    "amount": str(amount),
                    "asset": "USDT",
                    "type": direction,
                },
            )

    def h_income(self):
        opex = self._require_user()
        if not opex:
            return
        q = self._form_or_query()
        sym = (q.get("symbol") or "").upper().strip()
        income_type = (q.get("incomeType") or "").upper().strip()
        limit = int(q.get("limit") or 100)
        sqlb = "SELECT * FROM perp_income_history WHERE opex_user=?"
        args: list = [opex]
        if sym:
            sqlb += " AND symbol=?"
            args.append(sym)
        if income_type:
            sqlb += " AND income_type=?"
            args.append(income_type)
        sqlb += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with db() as conn:
            rows = conn.execute(sqlb, tuple(args)).fetchall()
        return self._send_json(
            200,
            [
                {
                    "symbol": r["symbol"] or "",
                    "incomeType": r["income_type"],
                    "income": dstr(r["income"]),
                    "asset": r["asset"],
                    "time": int(r["time"]) * 1000,
                    "tranId": r["related_order_id"] or "",
                }
                for r in rows
            ],
        )

    def h_close_position(self):
        """Convenience: close the user's entire open position on a symbol at the
        current mark, by emitting a MARKET reduce-only order through the
        normal CLOB path. If the book is empty, this will fail with -2010;
        fall back to a synthetic mark-price close in that case."""
        opex = self._require_user()
        if not opex:
            return
        q = self._form_or_query()
        sym = (q.get("symbol") or "").upper().strip()
        with db() as conn:
            p = conn.execute(
                "SELECT * FROM perp_positions WHERE opex_user=? AND symbol=? "
                "AND closed_at IS NULL ORDER BY id DESC LIMIT 1",
                (opex, sym),
            ).fetchone()
        if not p:
            return self._send_error(404, -2013, "no open position")
        qty = D(p["quantity"])
        opposite_side = "SELL" if p["side"] == "LONG" else "BUY"
        try:
            order = place_order(
                opex,
                symbol=sym,
                side=opposite_side,
                order_type="MARKET",
                quantity=qty,
                price=None,
                reduce_only=True,
                client_order_id=None,
                time_in_force="GTC",
            )
            return self._send_json(200, order)
        except BadRequest as e:
            if e.code == -2010:
                # Book empty — synthetically force-close at the mark.
                m = get_market(sym)
                mark = D(m["mark_price"] or "0")
                if mark <= 0:
                    return self._send_error(503, -1131, "no liquidity and no mark price")
                _force_liquidate(p["id"], mark, partial_close_qty=qty, reason="user_close")
                return self._send_json(
                    200,
                    {
                        "orderId": -1,
                        "symbol": sym,
                        "status": "FILLED",
                        "side": opposite_side,
                        "type": "MARKET",
                        "origQty": dstr(qty),
                        "executedQty": dstr(qty),
                        "avgPrice": dstr(mark),
                        "note": "no book liquidity; closed at mark",
                    },
                )
            return self._send_error(
                e.http_status, e.code if isinstance(e.code, int) else -2010, e.message
            )


class ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5590
    # Forward uncaught exceptions to the central error_collector
    # (loopback-only POST to :5690). Best-effort, never raises.
    try:
        from _error_reporter import install_global_handler  # type: ignore

        install_global_handler()
    except Exception as e:  # noqa: BLE001
        log(f"error reporter install skipped: {e!r}")
    try:
        _otel_install()
    except Exception as e:  # noqa: BLE001
        log(f"otel install skipped: {e!r}")
    init_db()
    _restore_book_from_db()
    t1 = threading.Thread(target=pricing_loop, name="perp-pricing", daemon=True)
    t1.start()
    t2 = threading.Thread(target=liquidation_loop, name="perp-liq", daemon=True)
    t2.start()
    t3 = threading.Thread(target=book_cleanup_loop, name="perp-bookclean", daemon=True)
    t3.start()
    with ThreadingServer(("", port), Handler) as srv:
        log(f"listening on :{port} (db={DB_PATH}) — CLOB + cross-margin enabled")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            log("shutting down")
        finally:
            _stop_event.set()


if __name__ == "__main__":
    main()
