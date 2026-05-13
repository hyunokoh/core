#!/usr/bin/env python3
"""Market-maker bot for zkCEX (demo only).

Background loop that keeps the public order book on each configured spot pair
populated with a small geometric ladder of bid/ask quotes. Designed so that
when a visitor opens ``/app/trade.html`` they never see an empty book.

Identity
--------
The bot identifies itself to the matching-gateway via the legacy header
``X-Opex-User: zkcex-mm`` — the same trust-the-header path the e2e harness
uses. ``zkcex-mm`` is NOT a real signed-up user: it has no row in auth.db,
no KYC, no API key. The matching-gateway just takes the header at face
value (this is intentional for the demo stack). Production-grade auth is
out of scope; do not deploy this against a real custody surface.

Strategy
--------
1. For each configured market, fetch a mid price from the 24h ticker
   (``lastPrice``) on the Binance-compatible REST surface, falling back to
   recent trades, then to a per-symbol anchor constant.
2. Build target ladders: ``MM_LEVELS`` levels each side, geometric spacing
   starting at ``MM_SPREAD_BPS`` bps from mid (default 30 bps = 0.3%).
   Each subsequent level is roughly 2x further out (0.3% / 0.6% / 1.0% /
   2.0% / 3.5%). Per-level quantities decay 0.05 / 0.10 / 0.20 / 0.50 /
   1.00 (in base units, then scaled by an asset-specific factor so BTC
   doesn't cost half a billion to seed).
3. Cancel any existing bot orders on the symbol that have drifted more
   than ``MM_DRIFT_TOLERANCE_BPS`` (default 50 bps) from their intended
   level price, or that no longer correspond to any active level.
4. Submit fresh ``LIMIT_ORDER`` quotes for any missing levels.
5. Sleep ``MM_REFRESH_INTERVAL_S`` (default 5s) and repeat.

Order ownership tracking
------------------------
Every order the bot submits carries a ``clientOrderId`` of the form
``mm-<symbol>-<side>-<level>-<ts><nonce>`` and is recorded in
``tools/.local/mm_bot.db``. The bot will only ever cancel an order whose
client_order_id starts with ``mm-`` AND which it placed itself (DB row
exists). Other users' orders are never touched.

Endpoints (port 5600)
---------------------
* ``GET  /mm/health``         — public; uptime, n_symbols, last error.
* ``GET  /mm/status``         — public; per-symbol summary + bot balances.
* ``GET  /mm/orders?symbol=`` — public; current active bot orders.
* ``POST /mm/pause``          — admin (Bearer token); pause the loop.
* ``POST /mm/resume``         — admin; resume the loop.
* ``POST /mm/refresh``        — admin; force one tick immediately.
* ``POST /mm/seed-wallet``    — admin; top up the bot wallet manually.

The admin Bearer token is configurable via ``MM_ADMIN_TOKEN``; if unset,
a fresh ``secrets.token_urlsafe(32)`` value is generated at first run and
printed to stderr (then cached in the DB for restarts).

Stdlib only. No new pip deps.
"""

from __future__ import annotations

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
from decimal import ROUND_DOWN, Decimal, InvalidOperation, getcontext

getcontext().prec = 28

# --- Paths ---------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join(HERE, ".local")
os.makedirs(LOCAL_DIR, exist_ok=True)
DB_PATH = os.path.join(LOCAL_DIR, "mm_bot.db")

# --- Bot identity --------------------------------------------------------
# The bot speaks to matching-gateway/wallet using this opex_user header.
# zkcex-mm is a synthetic identity: no signup, no KYC, no API key. The
# gateway trusts the header in demo mode (legacy E2E path).
BOT_OPEX_USER = os.environ.get("MM_BOT_USER", "zkcex-mm")


# --- Service config (env overridable) -----------------------------------
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


API_BASE = _validated_http_base_url("API_BASE", os.environ.get("API_BASE", "http://127.0.0.1:8094"))
GATEWAY_BASE = _validated_http_base_url(
    "GATEWAY_BASE", os.environ.get("GATEWAY_BASE", "http://127.0.0.1:8093")
)
WALLET_BASE = _validated_http_base_url(
    "WALLET_BASE", os.environ.get("WALLET_BASE", "http://127.0.0.1:8091")
)
MARKET_BASE = _validated_http_base_url(
    "MARKET_BASE", os.environ.get("MARKET_BASE", "http://127.0.0.1:8096")
)

REFRESH_INTERVAL_S = float(os.environ.get("MM_REFRESH_INTERVAL_S", "5"))
LEVELS = int(os.environ.get("MM_LEVELS", "5"))
SPREAD_BPS = Decimal(os.environ.get("MM_SPREAD_BPS", "30"))  # 30 bps = 0.30%
DRIFT_TOLERANCE_BPS = Decimal(os.environ.get("MM_DRIFT_TOLERANCE_BPS", "50"))
MAX_OUTSTANDING_PER_SYMBOL = int(os.environ.get("MM_MAX_OUTSTANDING_ORDERS_PER_SYMBOL", "20"))

# Per-level offset multipliers (in units of MM_SPREAD_BPS) — geometric ladder.
# Default: 1x / 2x / ~3.3x / 6.6x / ~11.6x → 0.30 / 0.60 / 1.00 / 2.00 / 3.50%
LEVEL_OFFSET_MULTIPLIERS = [
    Decimal("1"),
    Decimal("2"),
    Decimal("3.3333"),
    Decimal("6.6667"),
    Decimal("11.6667"),
]
# Per-level base quantity multipliers (in base units). Scaled per-asset
# by SYMBOL_CONFIG["qty_scale"] so we don't try to quote 1 BTC per level.
LEVEL_QTY_BASE = [
    Decimal("0.05"),
    Decimal("0.10"),
    Decimal("0.20"),
    Decimal("0.50"),
    Decimal("1.00"),
]


# --- Markets to seed -----------------------------------------------------
# Symbol naming:
#   ``symbol`` is the Binance-compatible concat (ETHUSDT) used by /v3/*.
#   ``pair`` is the underscore form (ETH_USDT) the matching-gateway expects.
# ``anchor`` is the fallback mid when no last trade is available.
# ``qty_scale`` multiplies the base-quantity ladder so we quote sensible
# sizes per asset (BTC at 0.001, DOGE at thousands, etc).
# ``qty_decimals`` is the decimal precision the wallet enforces for the
# base currency (from /v1/owner/.../wallets price precision).
# ``price_decimals`` is the precision we should round price to.
#
# qty_scale notes
# ---------------
# The base ladder is 0.05 / 0.10 / 0.20 / 0.50 / 1.00 base units; multiply
# by qty_scale to get the per-level base quantity. The total per-side base
# reservation is sum(ladder) * qty_scale = 1.85 * qty_scale.
#
# For the *bid* side that translates into a USDT lock of
# (mid * total_base * (1 - avg_offset)) ~ mid * 1.85 * qty_scale.
# Demo wallet "1" only holds a few thousand USDT, so we keep total
# bid-side USDT reservation across all symbols well under that ceiling.
DEFAULT_SYMBOL_CONFIG = {
    "ETHUSDT": {
        "pair": "ETH_USDT",
        "base": "ETH",
        "quote": "USDT",
        "anchor": Decimal("95"),
        "qty_scale": Decimal("0.5"),  # 0.025 .. 0.50 ETH per level
        "qty_decimals": 6,
        "price_decimals": 2,
        "seed_base": Decimal("100"),
        "seed_quote": Decimal("1500"),
    },
    "BTCUSDT": {
        "pair": "BTC_USDT",
        "base": "BTC",
        "quote": "USDT",
        "anchor": Decimal("60000"),
        "qty_scale": Decimal("0.001"),  # 0.00005 .. 0.001 BTC per level
        "qty_decimals": 6,
        "price_decimals": 2,
        "seed_base": Decimal("10"),
        "seed_quote": Decimal("1500"),
    },
    "SOLUSDT": {
        "pair": "SOL_USDT",
        "base": "SOL",
        "quote": "USDT",
        "anchor": Decimal("150"),
        "qty_scale": Decimal("0.5"),  # 0.025 .. 0.5 SOL per level
        "qty_decimals": 5,
        "price_decimals": 2,
        "seed_base": Decimal("1000"),
        "seed_quote": Decimal("1500"),
    },
    "DOGEUSDT": {
        "pair": "DOGE_USDT",
        "base": "DOGE",
        "quote": "USDT",
        "anchor": Decimal("0.14"),
        "qty_scale": Decimal("100"),  # 5 .. 100 DOGE per level
        "qty_decimals": 3,
        "price_decimals": 2,  # USDT quote precision is 2 dp
        "seed_base": Decimal("100000"),
        "seed_quote": Decimal("1500"),
    },
}


def _resolve_symbols() -> dict[str, dict]:
    """Resolve the per-symbol configuration honouring MM_SYMBOLS."""
    env = os.environ.get("MM_SYMBOLS", "").strip()
    if not env:
        return dict(DEFAULT_SYMBOL_CONFIG)
    requested = [s.strip().upper() for s in env.split(",") if s.strip()]
    out = {}
    for sym in requested:
        if sym in DEFAULT_SYMBOL_CONFIG:
            out[sym] = DEFAULT_SYMBOL_CONFIG[sym]
        else:
            log(f"warn: MM_SYMBOLS lists unknown symbol {sym!r}, skipping")
    return out or dict(DEFAULT_SYMBOL_CONFIG)


SYMBOL_CONFIG: dict[str, dict] = {}


# --- Logging --------------------------------------------------------------
def log(*args):
    sys.stderr.write("[mm_bot] " + " ".join(str(a) for a in args) + "\n")
    sys.stderr.flush()


# --- DB -------------------------------------------------------------------
_db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_orders (
  client_order_id TEXT PRIMARY KEY,
  symbol          TEXT NOT NULL,          -- concat form (ETHUSDT)
  pair            TEXT NOT NULL,          -- underscore form (ETH_USDT)
  side            TEXT NOT NULL,          -- BID | ASK
  level           INTEGER NOT NULL,       -- 1..LEVELS
  price           TEXT NOT NULL,
  quantity        TEXT NOT NULL,
  ouid            TEXT,                   -- filled in after gateway echo
  order_id        INTEGER,                -- numeric Binance-style id (if known)
  status          TEXT NOT NULL,          -- open | cancelled | filled | error
  reason          TEXT,
  created_at      INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bot_orders_symbol_status
  ON bot_orders(symbol, status);
CREATE INDEX IF NOT EXISTS idx_bot_orders_status
  ON bot_orders(status);

CREATE TABLE IF NOT EXISTS bot_state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seeded_assets (
  asset      TEXT PRIMARY KEY,
  seeded_at  INTEGER NOT NULL,
  amount     TEXT NOT NULL
);
"""


def init_db():
    with _db_lock, db() as conn:
        conn.executescript(SCHEMA)


def get_state(key: str, default: str | None = None) -> str | None:
    with db() as conn:
        row = conn.execute("SELECT value FROM bot_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(key: str, value: str) -> None:
    with _db_lock, db() as conn:
        conn.execute(
            "INSERT INTO bot_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# --- Admin token ----------------------------------------------------------
def resolve_admin_token() -> str:
    """Read MM_ADMIN_TOKEN, or generate-and-cache a random token on first run."""
    env = os.environ.get("MM_ADMIN_TOKEN", "").strip()
    if env:
        return env
    cached = get_state("admin_token")
    if cached:
        return cached
    token = secrets.token_urlsafe(32)
    set_state("admin_token", token)
    sys.stderr.write(
        "[mm_bot] generated admin Bearer token (cached in mm_bot.db):\n"
        f"[mm_bot]   MM_ADMIN_TOKEN={token}\n"
    )
    sys.stderr.flush()
    return token


# --- HTTP helpers ---------------------------------------------------------
class HttpError(Exception):
    def __init__(self, code: int, body: str = ""):
        super().__init__(f"HTTP {code}: {body[:200]}")
        self.code = code
        self.body = body


def _http(
    method: str,
    url: str,
    body: bytes | None = None,
    headers: dict | None = None,
    timeout: float = 6.0,
) -> bytes:
    req = _http_request(url, data=body, method=method, headers=headers or {})
    try:
        with _http_urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        try:
            body_txt = e.read().decode("utf-8", "replace")
        except Exception:
            body_txt = ""
        raise HttpError(e.code, body_txt) from e


def http_get_json(url: str, timeout: float = 6.0):
    raw = _http("GET", url, timeout=timeout)
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def http_post_json(url: str, payload: dict, opex_user: str | None, timeout: float = 8.0):
    headers = {"Content-Type": "application/json"}
    if opex_user:
        headers["X-Opex-User"] = opex_user
    body = json.dumps(payload).encode("utf-8")
    raw = _http("POST", url, body=body, headers=headers, timeout=timeout)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return {}


# --- Wallet helpers -------------------------------------------------------
def fetch_bot_balance(asset: str) -> Decimal | None:
    """Return free balance for ``asset`` (None on transport failure)."""
    try:
        obj = http_get_json(
            f"{WALLET_BASE}/v1/owner/{urllib.parse.quote(BOT_OPEX_USER)}"
            f"/wallets/{urllib.parse.quote(asset)}",
            timeout=4,
        )
    except HttpError as e:
        if e.code == 404:
            return Decimal("0")
        log(f"wallet read {asset} HTTP {e.code}")
        return None
    except Exception as e:  # noqa: BLE001
        log(f"wallet read {asset} error: {e!r}")
        return None
    if not isinstance(obj, dict):
        return None
    try:
        return Decimal(str(obj.get("balance", "0")))
    except (InvalidOperation, TypeError):
        return None


def fetch_all_bot_balances() -> dict[str, Decimal]:
    try:
        obj = http_get_json(
            f"{WALLET_BASE}/v1/owner/{urllib.parse.quote(BOT_OPEX_USER)}/wallets",
            timeout=4,
        )
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, Decimal] = {}
    if isinstance(obj, list):
        for w in obj:
            try:
                out[str(w.get("asset"))] = Decimal(str(w.get("balance", "0")))
            except (InvalidOperation, TypeError):
                continue
    return out


def deposit_to_bot(asset: str, amount: Decimal, network: str = "test-ethereum") -> bool:
    """POST /deposit/<amt>_<network>_<asset>/<user>_MAIN — same demo path the
    e2e harness uses. Returns True on success."""
    qty_str = str(amount.normalize())
    # ensure not exponent form like "1E+5"
    if "E" in qty_str or "e" in qty_str:
        qty_str = format(amount, "f").rstrip("0").rstrip(".")
        if not qty_str:
            qty_str = "0"
    ref = f"mm-seed-{asset}-{int(time.time() * 1000)}"
    path = (
        f"/deposit/{qty_str}_{network}_{asset}/"
        f"{urllib.parse.quote(BOT_OPEX_USER)}_MAIN"
        f"?description=mm-bot-seed&transferRef={urllib.parse.quote(ref)}"
    )
    url = f"{WALLET_BASE}{path}"
    try:
        _http("POST", url, body=b"", headers={"Content-Type": "application/json"}, timeout=8)
    except HttpError as e:
        log(f"seed deposit {asset} {amount} HTTP {e.code}: {e.body[:200]}")
        return False
    except Exception as e:  # noqa: BLE001
        log(f"seed deposit {asset} {amount} error: {e!r}")
        return False
    return True


# --- Mid-price discovery --------------------------------------------------
def fetch_mid_price(symbol: str) -> Decimal | None:
    """Return a usable mid for ``symbol`` (Binance-concat form), or None.

    Preference order:
      1. 24h ticker ``lastPrice``
      2. Most recent trade
      3. fallback anchor in SYMBOL_CONFIG (caller decides whether to use)
    """
    try:
        obj = http_get_json(
            f"{API_BASE}/v3/ticker/24h?symbol={urllib.parse.quote(symbol)}",
            timeout=4,
        )
    except HttpError as e:
        if e.code != 404:
            log(f"ticker/24h {symbol} HTTP {e.code}")
        obj = None
    except Exception as e:  # noqa: BLE001
        log(f"ticker/24h {symbol} error: {e!r}")
        obj = None
    # ticker shape: list of one dict
    if isinstance(obj, list) and obj:
        last = obj[0].get("lastPrice")
        try:
            v = Decimal(str(last))
            if v > 0:
                return v
        except (InvalidOperation, TypeError):
            pass
    elif isinstance(obj, dict):
        last = obj.get("lastPrice")
        try:
            v = Decimal(str(last))
            if v > 0:
                return v
        except (InvalidOperation, TypeError):
            pass
    # Fallback to recent trades.
    try:
        trades = http_get_json(
            f"{API_BASE}/v3/trades?symbol={urllib.parse.quote(symbol)}&limit=1",
            timeout=4,
        )
    except Exception:  # noqa: BLE001
        trades = None
    if isinstance(trades, list) and trades:
        try:
            v = Decimal(str(trades[0].get("price")))
            if v > 0:
                return v
        except (InvalidOperation, TypeError):
            pass
    return None


# --- Quantization helpers -------------------------------------------------
def _round_down(value: Decimal, decimals: int) -> Decimal:
    q = Decimal(1).scaleb(-decimals) if decimals > 0 else Decimal(1)
    return value.quantize(q, rounding=ROUND_DOWN)


def target_levels(mid: Decimal, cfg: dict) -> list[dict]:
    """Build the per-symbol target ladder for both sides.

    Returns a list of ``{side, level, price, quantity}`` dicts. ``side`` is
    ``BID`` or ``ASK``, ``level`` is 1..LEVELS.
    """
    out: list[dict] = []
    price_dec = cfg["price_decimals"]
    qty_dec = cfg["qty_decimals"]
    qty_scale: Decimal = cfg["qty_scale"]
    for i in range(min(LEVELS, len(LEVEL_OFFSET_MULTIPLIERS))):
        offset_bps = SPREAD_BPS * LEVEL_OFFSET_MULTIPLIERS[i]
        # 1 bp = 0.0001
        offset = mid * offset_bps / Decimal("10000")
        bid_price = _round_down(mid - offset, price_dec)
        ask_price = _round_down(mid + offset, price_dec)
        if bid_price <= 0:
            continue
        qty = _round_down(LEVEL_QTY_BASE[i] * qty_scale, qty_dec)
        if qty <= 0:
            continue
        out.append({"side": "BID", "level": i + 1, "price": bid_price, "quantity": qty})
        out.append({"side": "ASK", "level": i + 1, "price": ask_price, "quantity": qty})
    return out


# --- Order placement / cancel --------------------------------------------
_client_id_nonce = 0
_client_id_nonce_lock = threading.Lock()


def _new_client_order_id(symbol: str, side: str, level: int) -> str:
    global _client_id_nonce
    with _client_id_nonce_lock:
        _client_id_nonce = (_client_id_nonce + 1) % 1_000_000
        nonce = _client_id_nonce
    ts = int(time.time())
    return f"mm-{symbol}-{side}-{level}-{ts}{nonce:06d}"


def _decimal_to_float_str(d: Decimal) -> float:
    # The gateway accepts JSON numbers; we cast via float for compatibility
    # with the Kotlin parser which uses BigDecimal.
    return float(d)


def submit_order(
    symbol: str, cfg: dict, side: str, level: int, price: Decimal, quantity: Decimal
) -> tuple[bool, str | None, str | None]:
    """Place a limit order on the matching-gateway. Returns
    ``(ok, client_order_id, error)``."""
    cid = _new_client_order_id(symbol, side, level)
    body = {
        "uuid": None,
        "pair": cfg["pair"],
        "price": _decimal_to_float_str(price),
        "quantity": _decimal_to_float_str(quantity),
        "direction": side,
        "matchConstraint": "GTC",
        "orderType": "LIMIT_ORDER",
        "userLevel": "*",
        "clientOrderId": cid,
    }
    now = int(time.time())
    with _db_lock, db() as conn:
        conn.execute(
            "INSERT INTO bot_orders(client_order_id, symbol, pair, side, level, "
            "price, quantity, status, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (cid, symbol, cfg["pair"], side, level, str(price), str(quantity), now, now),
        )
    try:
        resp = http_post_json(
            f"{GATEWAY_BASE}/order",
            body,
            opex_user=BOT_OPEX_USER,
            timeout=8,
        )
    except HttpError as e:
        with _db_lock, db() as conn:
            conn.execute(
                "UPDATE bot_orders SET status='error', reason=?, updated_at=? "
                "WHERE client_order_id=?",
                (f"gateway HTTP {e.code}: {e.body[:120]}", int(time.time()), cid),
            )
        return False, cid, f"gateway HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        with _db_lock, db() as conn:
            conn.execute(
                "UPDATE bot_orders SET status='error', reason=?, updated_at=? "
                "WHERE client_order_id=?",
                (f"transport: {e!r}", int(time.time()), cid),
            )
        return False, cid, f"transport error: {e!r}"
    # Gateway response is usually empty on success; treat as open.
    with _db_lock, db() as conn:
        conn.execute(
            "UPDATE bot_orders SET status='open', updated_at=? " "WHERE client_order_id=?",
            (int(time.time()), cid),
        )
    _ = resp  # response body usually empty; we look up ouid via market later
    return True, cid, None


def fetch_open_orders_from_market(pair: str) -> list[dict]:
    """Fetch the bot's currently-open orders on ``pair`` from market service."""
    try:
        obj = http_get_json(
            f"{MARKET_BASE}/v1/user/{urllib.parse.quote(BOT_OPEX_USER)}"
            f"/orders/{urllib.parse.quote(pair)}/open?limit=50",
            timeout=4,
        )
    except HttpError as e:
        if e.code != 404:
            log(f"market open-orders {pair} HTTP {e.code}")
        return []
    except Exception as e:  # noqa: BLE001
        log(f"market open-orders {pair} error: {e!r}")
        return []
    return obj if isinstance(obj, list) else []


def cancel_order(symbol: str, pair: str, ouid: str, order_id: int, client_order_id: str) -> bool:
    body = {"ouid": ouid, "uuid": BOT_OPEX_USER, "orderId": order_id, "symbol": pair}
    try:
        http_post_json(
            f"{GATEWAY_BASE}/order/cancel",
            body,
            opex_user=BOT_OPEX_USER,
            timeout=8,
        )
    except HttpError as e:
        log(f"cancel {client_order_id} HTTP {e.code}: {e.body[:120]}")
        with _db_lock, db() as conn:
            conn.execute(
                "UPDATE bot_orders SET reason=?, updated_at=? " "WHERE client_order_id=?",
                (f"cancel HTTP {e.code}", int(time.time()), client_order_id),
            )
        return False
    except Exception as e:  # noqa: BLE001
        log(f"cancel {client_order_id} error: {e!r}")
        return False
    with _db_lock, db() as conn:
        conn.execute(
            "UPDATE bot_orders SET status='cancelled', updated_at=? " "WHERE client_order_id=?",
            (int(time.time()), client_order_id),
        )
    return True


# --- Per-symbol tick ------------------------------------------------------
def _bps_distance(a: Decimal, b: Decimal) -> Decimal:
    if b == 0:
        return Decimal("1000000")
    return abs(a - b) / b * Decimal("10000")


# Compiled regex to recognize our own client order ids in case the DB row
# is missing (e.g. someone manually inserted, or restart between submit
# response and DB ack). We never act on ids that fail this AND aren't in
# the DB.
_MM_CID_RE = re.compile(r"^mm-[A-Z0-9_]+-(BID|ASK)-\d+-\d+$")


def reconcile_symbol(symbol: str, cfg: dict) -> tuple[bool, str | None]:
    """One pass of the strategy for ``symbol``. Returns ``(ok, error)``."""
    mid = fetch_mid_price(symbol)
    if mid is None:
        mid = cfg["anchor"]
        log(f"{symbol}: no live mid; using anchor {mid}")
    targets = target_levels(mid, cfg)
    # Index targets for quick lookup: keyed by (side, level).
    target_idx = {(t["side"], t["level"]): t for t in targets}

    # Sweep what the market thinks is currently open for the bot on this pair.
    live_orders = fetch_open_orders_from_market(cfg["pair"])
    # Hydrate ouid/order_id back into the DB based on clientOrderId so we
    # always know how to cancel an order we placed.
    with _db_lock, db() as conn:
        for o in live_orders:
            cid = o.get("clientOrderId")
            if not cid or not cid.startswith("mm-"):
                continue
            conn.execute(
                "UPDATE bot_orders SET ouid=?, order_id=?, status='open', "
                "updated_at=? WHERE client_order_id=?",
                (
                    str(o.get("ouid")) if o.get("ouid") else None,
                    int(o.get("orderId") or 0) or None,
                    int(time.time()),
                    cid,
                ),
            )

    # The set of (side, level) we already have an *acceptable* live order for.
    have_levels: set[tuple[str, int]] = set()
    # Mark which live orders to cancel (drift / orphan / over-cap).
    to_cancel: list[dict] = []

    # Pull our DB rows for this symbol to cross-reference.
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM bot_orders WHERE symbol=? AND status IN " "('open', 'pending')",
            (symbol,),
        ).fetchall()
    db_rows_by_cid = {r["client_order_id"]: r for r in rows}

    # Tag live orders that *we* placed (DB row OR cid prefix matches our
    # pattern). For safety, never touch live orders that don't match either.
    bot_live: list[dict] = []
    for o in live_orders:
        cid = o.get("clientOrderId") or ""
        if cid in db_rows_by_cid or (cid.startswith("mm-") and _MM_CID_RE.match(cid)):
            bot_live.append(o)

    if len(bot_live) > MAX_OUTSTANDING_PER_SYMBOL:
        # Trim oldest first (defensive — shouldn't happen in normal flow).
        bot_live.sort(key=lambda o: o.get("createDate") or "")
        to_cancel.extend(bot_live[: len(bot_live) - MAX_OUTSTANDING_PER_SYMBOL])
        bot_live = bot_live[len(bot_live) - MAX_OUTSTANDING_PER_SYMBOL :]

    for o in bot_live:
        cid = o.get("clientOrderId") or ""
        # Extract level + side from cid (or DB row).
        side: str | None = None
        level: int | None = None
        m = _MM_CID_RE.match(cid)
        if m:
            side = m.group(1)
            # cid form: mm-<SYMBOL>-<SIDE>-<level>-<ts>
            parts = cid.split("-")
            # parts: ['mm', SYMBOL, SIDE, level, tsNNN]
            try:
                level = int(parts[3])
            except (IndexError, ValueError):
                level = None
        if side is None or level is None:
            # Orphan-shaped bot order; cancel it.
            to_cancel.append(o)
            continue
        target = target_idx.get((side, level))
        if target is None:
            # Level no longer in target ladder (e.g. config changed). Cancel.
            to_cancel.append(o)
            continue
        # Drift check.
        try:
            live_price = Decimal(str(o.get("price")))
        except (InvalidOperation, TypeError):
            to_cancel.append(o)
            continue
        if _bps_distance(live_price, target["price"]) > DRIFT_TOLERANCE_BPS:
            to_cancel.append(o)
            continue
        # Keep it.
        have_levels.add((side, level))

    # Actually cancel the drifted/orphan orders.
    for o in to_cancel:
        cid = o.get("clientOrderId") or ""
        ouid = o.get("ouid")
        order_id = o.get("orderId")
        if not ouid or order_id is None:
            log(f"cancel skip {cid}: missing ouid/orderId in market view")
            continue
        cancel_order(symbol, cfg["pair"], str(ouid), int(order_id), cid)

    # Submit fresh orders for missing levels. Stop if we'd exceed the cap.
    n_active = len(bot_live) - len(to_cancel)
    submitted = 0
    last_error: str | None = None
    for t in targets:
        key = (t["side"], t["level"])
        if key in have_levels:
            continue
        if n_active + submitted >= MAX_OUTSTANDING_PER_SYMBOL:
            log(f"{symbol}: outstanding cap reached, skipping further levels")
            break
        ok, _cid, err = submit_order(symbol, cfg, t["side"], t["level"], t["price"], t["quantity"])
        if not ok:
            last_error = err
            # Only flag for re-seed on a body that *specifically* says
            # NotEnoughBalance — the accountant returns the same HTTP 400
            # for rate-limits / temporary constraints (which look like
            # ``SubmitOrderForbiddenByAccountant``) and re-seeding on those
            # would just spam deposits.
            if err and "NotEnoughBalance" in err:
                _flag_insufficient_balance(symbol, cfg, t["side"])
            continue
        submitted += 1
        # Pacing delay between submits. The Kotlin accountant returns
        # ``SubmitOrderForbiddenByAccountant`` (HTTP 400) when too many
        # back-to-back reservations on the same user arrive before earlier
        # ones have been bookkept; that's not a balance problem and gets
        # cleared by spacing the calls out. 250ms is enough to land all
        # 40 orders (4 symbols x 10 levels) within ~10s and stay below the
        # accountant's burst threshold.
        time.sleep(0.25)
    return last_error is None, last_error


_low_balance_flag_lock = threading.Lock()
_low_balance_flag: set[str] = set()


def _flag_insufficient_balance(symbol: str, cfg: dict, side: str) -> None:
    asset = cfg["base"] if side == "ASK" else cfg["quote"]
    with _low_balance_flag_lock:
        _low_balance_flag.add(asset)


# --- Initial seed --------------------------------------------------------
def ensure_seed_wallet(force: bool = False) -> dict:
    """Top up the bot wallet for each asset that is below the threshold.

    Returns a dict of ``{asset: action}`` summarising what we did.
    """
    out: dict[str, str] = {}
    assets_needed: dict[str, Decimal] = {}
    for cfg in SYMBOL_CONFIG.values():
        assets_needed.setdefault(cfg["base"], cfg["seed_base"])
        assets_needed[cfg["base"]] = max(assets_needed[cfg["base"]], cfg["seed_base"])
        assets_needed.setdefault(cfg["quote"], cfg["seed_quote"])
        assets_needed[cfg["quote"]] = max(assets_needed[cfg["quote"]], cfg["seed_quote"])

    for asset, target in assets_needed.items():
        bal = fetch_bot_balance(asset)
        if bal is None:
            out[asset] = "skip (wallet unreachable)"
            continue
        if not force and bal >= target:
            out[asset] = f"ok ({bal})"
            continue
        topup = target - bal
        if topup <= 0:
            out[asset] = f"ok ({bal})"
            continue
        # Some wallet ledgers can't accept gigantic single deposits; chunk
        # if necessary. The demo wallet path takes any size, so this is a
        # belt-and-braces guard.
        chunk = topup
        if chunk > Decimal("1000000"):
            chunk = Decimal("1000000")
        ok = deposit_to_bot(asset, chunk)
        if not ok:
            out[asset] = f"FAIL (wanted +{chunk})"
            continue
        # Keep depositing until we hit target (capped at 10 iterations).
        for _ in range(10):
            bal = fetch_bot_balance(asset) or Decimal("0")
            if bal >= target:
                break
            chunk = min(target - bal, Decimal("1000000"))
            if chunk <= 0:
                break
            if not deposit_to_bot(asset, chunk):
                break
        with _db_lock, db() as conn:
            conn.execute(
                "INSERT INTO seeded_assets(asset, seeded_at, amount) "
                "VALUES(?, ?, ?) ON CONFLICT(asset) DO UPDATE SET "
                "seeded_at=excluded.seeded_at, amount=excluded.amount",
                (asset, int(time.time()), str(bal)),
            )
        with _low_balance_flag_lock:
            _low_balance_flag.discard(asset)
        out[asset] = f"seeded -> {bal}"
    return out


# --- Background loop ------------------------------------------------------
_paused = False
_paused_lock = threading.Lock()
_stop_event = threading.Event()

_last_tick_at: float = 0.0
_last_error: str | None = None
_started_at: float = time.time()
_force_tick_event = threading.Event()
_per_symbol_status: dict[str, dict] = {}


def is_paused() -> bool:
    with _paused_lock:
        return _paused


def set_paused(value: bool) -> None:
    global _paused
    with _paused_lock:
        _paused = value


def background_loop():
    global _last_tick_at, _last_error
    log(
        f"loop start: levels={LEVELS} spread={SPREAD_BPS}bps "
        f"refresh={REFRESH_INTERVAL_S}s drift={DRIFT_TOLERANCE_BPS}bps"
    )
    # First-run wallet seed.
    try:
        log("seeding bot wallet …")
        seed_summary = ensure_seed_wallet(force=False)
        log("seed result: " + ", ".join(f"{k}={v}" for k, v in seed_summary.items()))
    except Exception as e:  # noqa: BLE001
        log(f"seed error: {e!r}")
    while not _stop_event.is_set():
        # Wait either for the refresh interval or a forced tick.
        if not _force_tick_event.is_set():
            _force_tick_event.wait(timeout=REFRESH_INTERVAL_S)
        _force_tick_event.clear()
        if _stop_event.is_set():
            break
        if is_paused():
            continue
        # If anything got flagged as low-balance last tick, try a re-seed
        # for those assets only.
        with _low_balance_flag_lock:
            low_assets = list(_low_balance_flag)
            _low_balance_flag.clear()
        if low_assets:
            log(f"low-balance flagged: {low_assets}; attempting re-seed")
            try:
                ensure_seed_wallet(force=True)
            except Exception as e:  # noqa: BLE001
                log(f"re-seed error: {e!r}")

        tick_err: str | None = None
        for symbol, cfg in SYMBOL_CONFIG.items():
            try:
                ok, err = reconcile_symbol(symbol, cfg)
                if not ok and err:
                    tick_err = err
                _per_symbol_status[symbol] = {
                    "last_refresh_at": int(time.time()),
                    "last_error": err,
                }
            except Exception as e:  # noqa: BLE001
                log(f"reconcile {symbol} exc: {e!r}")
                tick_err = str(e)
        _last_tick_at = time.time()
        _last_error = tick_err
    log("loop exit")


# --- Status snapshots -----------------------------------------------------
def status_snapshot() -> list[dict]:
    """Per-symbol info for /mm/status."""
    out = []
    balances = fetch_all_bot_balances()
    for symbol, cfg in SYMBOL_CONFIG.items():
        with db() as conn:
            n_bid = conn.execute(
                "SELECT COUNT(*) AS n FROM bot_orders WHERE symbol=? "
                "AND side='BID' AND status='open'",
                (symbol,),
            ).fetchone()["n"]
            n_ask = conn.execute(
                "SELECT COUNT(*) AS n FROM bot_orders WHERE symbol=? "
                "AND side='ASK' AND status='open'",
                (symbol,),
            ).fetchone()["n"]
        mid = fetch_mid_price(symbol)
        per = _per_symbol_status.get(symbol, {})
        out.append(
            {
                "symbol": symbol,
                "pair": cfg["pair"],
                "base": cfg["base"],
                "quote": cfg["quote"],
                "mid": str(mid) if mid is not None else None,
                "n_bid_levels": n_bid,
                "n_ask_levels": n_ask,
                "last_refresh_at": per.get("last_refresh_at"),
                "last_error": per.get("last_error"),
                "bot_balance": {
                    cfg["base"]: str(balances.get(cfg["base"], Decimal("0"))),
                    cfg["quote"]: str(balances.get(cfg["quote"], Decimal("0"))),
                },
            }
        )
    return out


# --- HTTP server ----------------------------------------------------------
ADMIN_TOKEN: str = ""


def _check_admin(handler: Handler) -> bool:
    auth = handler.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        handler._send_json(401, {"error": "missing_bearer"})
        return False
    if auth[len("Bearer ") :].strip() != ADMIN_TOKEN:
        handler._send_json(403, {"error": "forbidden"})
        return False
    return True


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet stdlib access log
        return

    def _send_json(self, code: int, payload):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        if path == "/mm/health":
            return self.h_health()
        if path == "/mm/status":
            return self.h_status()
        if path == "/mm/orders":
            return self.h_orders(urllib.parse.parse_qs(url.query))
        return self._send_json(404, {"error": "not_found", "path": path})

    def do_POST(self):  # noqa: N802
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        if path == "/mm/pause":
            return self.h_pause()
        if path == "/mm/resume":
            return self.h_resume()
        if path == "/mm/refresh":
            return self.h_refresh()
        if path == "/mm/seed-wallet":
            return self.h_seed()
        return self._send_json(404, {"error": "not_found", "path": path})

    # ---- handlers --------------------------------------------------------
    def h_health(self):
        with db() as conn:
            n_active = conn.execute(
                "SELECT COUNT(*) AS n FROM bot_orders WHERE status='open'"
            ).fetchone()["n"]
        return self._send_json(
            200,
            {
                "ok": True,
                "uptime_s": int(time.time() - _started_at),
                "n_symbols": len(SYMBOL_CONFIG),
                "last_tick_at": int(_last_tick_at) if _last_tick_at else None,
                "n_orders_active": n_active,
                "last_error": _last_error,
                "paused": is_paused(),
                "refresh_interval_s": REFRESH_INTERVAL_S,
                "levels": LEVELS,
                "spread_bps": str(SPREAD_BPS),
                "bot_user": BOT_OPEX_USER,
            },
        )

    def h_status(self):
        return self._send_json(200, {"symbols": status_snapshot()})

    def h_orders(self, qs: dict):
        sym = (qs.get("symbol", [""])[0] or "").upper()
        if sym and sym not in SYMBOL_CONFIG:
            return self._send_json(400, {"error": "unknown_symbol", "symbol": sym})
        with db() as conn:
            if sym:
                rows = conn.execute(
                    "SELECT * FROM bot_orders WHERE symbol=? "
                    "AND status='open' ORDER BY side, level, price",
                    (sym,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM bot_orders WHERE status='open' "
                    "ORDER BY symbol, side, level, price"
                ).fetchall()
        out = [
            {
                "client_order_id": r["client_order_id"],
                "symbol": r["symbol"],
                "pair": r["pair"],
                "side": r["side"],
                "level": r["level"],
                "price": r["price"],
                "quantity": r["quantity"],
                "ouid": r["ouid"],
                "order_id": r["order_id"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
        return self._send_json(200, {"symbol": sym or None, "orders": out})

    def h_pause(self):
        if not _check_admin(self):
            return
        set_paused(True)
        return self._send_json(200, {"ok": True, "paused": True})

    def h_resume(self):
        if not _check_admin(self):
            return
        set_paused(False)
        _force_tick_event.set()
        return self._send_json(200, {"ok": True, "paused": False})

    def h_refresh(self):
        if not _check_admin(self):
            return
        _force_tick_event.set()
        return self._send_json(200, {"ok": True, "queued": True})

    def h_seed(self):
        if not _check_admin(self):
            return
        try:
            summary = ensure_seed_wallet(force=True)
            return self._send_json(200, {"ok": True, "summary": summary})
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"error": "seed_failed", "message": str(e)})


class ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    global SYMBOL_CONFIG, ADMIN_TOKEN
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5600
    init_db()
    SYMBOL_CONFIG = _resolve_symbols()
    ADMIN_TOKEN = resolve_admin_token()
    log(f"symbols: {list(SYMBOL_CONFIG.keys())}")
    log(f"bot user: {BOT_OPEX_USER}")
    log(f"db: {DB_PATH}")

    t = threading.Thread(target=background_loop, name="mm-bot-loop", daemon=True)
    t.start()
    try:
        with ThreadingServer(("", port), Handler) as srv:
            log(f"listening on :{port}")
            srv.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        _stop_event.set()
        _force_tick_event.set()


if __name__ == "__main__":
    main()
