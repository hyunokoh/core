#!/usr/bin/env python3
"""zkCEX Earn / Lending pool product (port 5693).

Standalone stdlib-only Python service that runs the demo's lending pools:

  * Lenders supply assets (USDT / ETH / BTC) into a per-asset pool and earn
    a utilisation-driven supply APY.
  * Borrowers post collateral in a different asset, borrow against it up to
    the pool's collateral factor, and pay a higher borrow APY (Compound v2
    style two-slope rate curve).
  * A background accrual loop ticks every 60s — recomputing per-pool
    utilisation + APYs, accruing interest on every open position, and
    triggering liquidations on borrow positions whose LTV crosses the
    pool's liquidation threshold.

The state lives in ``tools/.local/lending.db`` (sqlite, WAL). User funds
are moved via the wallet API's ``/v2/transfer/...`` endpoint to a synthetic
``zkcex-lending`` vault user — same pattern as ``perp_engine.py`` and
``zkcex-futures``. Withdrawal/repayment is the inverse transfer.

CAVEATS (kept honest)
---------------------
* No real price oracle: the mark price comes from ``/v3/ticker/24hr``
  (last on-exchange spot). That makes the pool vulnerable to oracle
  manipulation on the matched-internal book — fine for the demo, NOT
  fine for production.
* No flash-loan mitigation: a same-block supply + borrow + withdraw could
  in principle escape exposure to a price move. The 60s accrual cadence
  + per-position lock + LTV check at open time mean the demo isn't
  vulnerable to a trivial sandwich, but a determined adversary could
  still race the accrual tick.
* Single-collateral-type per borrow position (no cross-margin pooling
  across collateral assets — every borrow row references one collateral
  asset). For multi-collateral you'd open multiple rows.
* Interest compounds discretely on every accrual tick (60s), not
  continuously. Over a year this is ~1bp off the continuous-compounding
  formula at 50% APY; negligible for the demo.

Bearer auth resolved against ``auth_server`` (``GET /auth/me``) with a
tiny in-memory cache, same pattern used by ``order_engine.py`` /
``fee_engine.py``.
"""

from __future__ import annotations

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
from decimal import Decimal, InvalidOperation, getcontext
from typing import Any

getcontext().prec = 36

# --- Paths / config -------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join(HERE, ".local")
os.makedirs(LOCAL_DIR, exist_ok=True)
DB_PATH = os.path.join(LOCAL_DIR, "lending.db")

DEFAULT_PORT = 5693


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
WALLET_BASE = _validated_http_base_url(
    "LENDING_WALLET_BASE", os.environ.get("LENDING_WALLET_BASE", "http://127.0.0.1:8091")
)
MARKET_BASE = _validated_http_base_url(
    "LENDING_MARKET_BASE", os.environ.get("LENDING_MARKET_BASE", "http://127.0.0.1:8094")
)
AUTH_TTL_SECONDS = int(os.environ.get("LENDING_AUTH_TTL_S", "30"))
ACCRUAL_INTERVAL_S = int(os.environ.get("LENDING_ACCRUAL_INTERVAL_S", "60"))
PRICE_TTL_S = int(os.environ.get("LENDING_PRICE_TTL_S", "20"))
VAULT_OPEX_USER = os.environ.get("LENDING_VAULT_USER", "zkcex-lending")
ADMIN_TOKEN_ENV = "LENDING_ADMIN_TOKEN"  # noqa: S105 - environment variable name, not a secret value.

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

# Seed pools. Tuple shape:
#   (asset, collateral_factor_bps, liquidation_threshold_bps,
#    liquidation_bonus_bps, reserve_factor_bps, optimal_utilization_bps,
#    rate_slope1_bps, rate_slope2_bps, base_rate_bps)
DEFAULT_POOLS = [
    ("USDT", 7500, 8000, 500, 1000, 8000, 400, 7500, 0),
    ("ETH", 7000, 7800, 800, 1000, 8000, 400, 7500, 0),
    ("BTC", 7000, 7800, 800, 1000, 8000, 400, 7500, 0),
]

# Per-asset capacity caps (USDT-equivalent, soft cap on total_supplied).
SUPPLY_CAPS = {
    "USDT": Decimal("10000000"),
    "ETH": Decimal("5000000"),
    "BTC": Decimal("5000000"),
}

# KYC-tier driven borrow ceilings (USDT-equivalent notional debt).
# 'none' gets a small starter limit so the demo flow can exercise borrows
# without first wiring up the Sumsub mock — production would enforce
# 'verified' as the floor. 'pending' is the same as 'none'. 'verified'
# gets the real one. 'rejected' is locked out. This is intentionally
# lighter than fee_engine's VIP ladder — borrowing risk is independent
# of trading volume.
BORROW_LIMITS_USDT = {
    "none": Decimal(os.environ.get("LENDING_BORROW_LIMIT_NONE_USDT", "1000")),
    "pending": Decimal("1000"),
    "verified": Decimal("100000"),
    "rejected": Decimal("0"),
}

# Price hints used when /v3/ticker is unavailable for a non-traded asset
# (this is the demo's BTC, which has no internal market).
PRICE_HINTS_USDT = {
    "USDT": Decimal("1"),
    "BTC": Decimal(os.environ.get("LENDING_BTC_PRICE_HINT", "65000")),
    "ETH": Decimal(os.environ.get("LENDING_ETH_PRICE_HINT", "3000")),
}

# Pool-asset -> network used for /deposit/.../{user}_MAIN test-deposit URL.
# The demo's wallet only knows about the hardhat 'test-ethereum' network.
DEPOSIT_NETWORK = "test-ethereum"

START_TS = int(time.time())


def log(*args: Any) -> None:
    sys.stderr.write("[lending] " + " ".join(str(a) for a in args) + "\n")
    sys.stderr.flush()


def now_s() -> int:
    return int(time.time())


# --- Decimal helpers -------------------------------------------------------
ZERO = Decimal(0)
ONE = Decimal(1)
BPS = Decimal(10000)
SECONDS_PER_YEAR = Decimal(365 * 86400)


def D(v: Any) -> Decimal:
    if v is None or v == "":
        return ZERO
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return ZERO


def dstr(v: Any) -> str:
    """Pretty-print a Decimal for JSON. 8 dp, trim trailing zeros."""
    x = D(v)
    if x == 0:
        return "0"
    q = x.quantize(Decimal("0.00000001")) if abs(x) >= Decimal("0.00000001") else x
    s = format(q, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


# --- DB --------------------------------------------------------------------
_db_lock = threading.Lock()
_position_locks: dict[int, threading.Lock] = {}
_position_locks_guard = threading.Lock()


def _pos_lock(position_id: int) -> threading.Lock:
    with _position_locks_guard:
        lk = _position_locks.get(position_id)
        if lk is None:
            lk = threading.Lock()
            _position_locks[position_id] = lk
        return lk


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS lending_pools (
  asset TEXT PRIMARY KEY,
  total_supplied TEXT NOT NULL DEFAULT '0',
  total_borrowed TEXT NOT NULL DEFAULT '0',
  reserves TEXT NOT NULL DEFAULT '0',
  utilization_bps INTEGER NOT NULL DEFAULT 0,
  supply_apy_bps INTEGER NOT NULL DEFAULT 0,
  borrow_apy_bps INTEGER NOT NULL DEFAULT 0,
  collateral_factor_bps INTEGER NOT NULL,
  liquidation_threshold_bps INTEGER NOT NULL,
  liquidation_bonus_bps INTEGER NOT NULL,
  reserve_factor_bps INTEGER NOT NULL DEFAULT 1000,
  optimal_utilization_bps INTEGER NOT NULL DEFAULT 8000,
  rate_slope1_bps INTEGER NOT NULL DEFAULT 400,
  rate_slope2_bps INTEGER NOT NULL DEFAULT 7500,
  base_rate_bps INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS supply_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  opex_user TEXT NOT NULL,
  asset TEXT NOT NULL,
  principal TEXT NOT NULL,
  accrued_interest TEXT NOT NULL DEFAULT '0',
  supplied_at INTEGER NOT NULL,
  last_accrual_at INTEGER NOT NULL,
  withdrawn_at INTEGER,
  status TEXT NOT NULL,
  UNIQUE(opex_user, asset, supplied_at)
);
CREATE INDEX IF NOT EXISTS idx_sup_user ON supply_positions(opex_user, status);
CREATE INDEX IF NOT EXISTS idx_sup_asset ON supply_positions(asset, status);

CREATE TABLE IF NOT EXISTS borrow_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  opex_user TEXT NOT NULL,
  borrowed_asset TEXT NOT NULL,
  borrowed_principal TEXT NOT NULL,
  borrowed_interest TEXT NOT NULL DEFAULT '0',
  collateral_asset TEXT NOT NULL,
  collateral_amount TEXT NOT NULL,
  borrowed_at INTEGER NOT NULL,
  last_accrual_at INTEGER NOT NULL,
  repaid_at INTEGER,
  liquidated_at INTEGER,
  status TEXT NOT NULL,
  ltv_at_open_bps INTEGER NOT NULL,
  current_ltv_bps INTEGER
);
CREATE INDEX IF NOT EXISTS idx_bor_user ON borrow_positions(opex_user, status);
CREATE INDEX IF NOT EXISTS idx_bor_open ON borrow_positions(status, borrowed_asset);

CREATE TABLE IF NOT EXISTS lending_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  opex_user TEXT NOT NULL,
  event TEXT NOT NULL,
  asset TEXT NOT NULL,
  amount TEXT NOT NULL,
  metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_hist_user_ts ON lending_history(opex_user, ts DESC);
CREATE INDEX IF NOT EXISTS idx_hist_event_ts ON lending_history(event, ts DESC);
"""


def init_db() -> None:
    with _db_lock, db() as c:
        c.executescript(SCHEMA)
        # Seed pools (idempotent — only inserts rows that don't exist).
        for (
            asset,
            cf,
            lt,
            lb,
            rf,
            ou,
            rs1,
            rs2,
            br,
        ) in DEFAULT_POOLS:
            existing = c.execute(
                "SELECT asset FROM lending_pools WHERE asset=?", (asset,)
            ).fetchone()
            if existing:
                continue
            c.execute(
                "INSERT INTO lending_pools "
                "(asset, collateral_factor_bps, liquidation_threshold_bps, "
                " liquidation_bonus_bps, reserve_factor_bps, "
                " optimal_utilization_bps, rate_slope1_bps, rate_slope2_bps, "
                " base_rate_bps) VALUES (?,?,?,?,?,?,?,?,?)",
                (asset, cf, lt, lb, rf, ou, rs1, rs2, br),
            )
        log(f"db initialised at {DB_PATH}, pools = {[p[0] for p in DEFAULT_POOLS]}")


# --- Admin token ----------------------------------------------------------
_admin_token_lock = threading.Lock()
_admin_token_cache: list[str] = []


def get_admin_token() -> str:
    env = os.environ.get(ADMIN_TOKEN_ENV)
    if env:
        return env
    with _admin_token_lock:
        if _admin_token_cache:
            return _admin_token_cache[0]
        tok = "lend_admin_" + secrets.token_urlsafe(20)
        _admin_token_cache.append(tok)
        log(f"{ADMIN_TOKEN_ENV}={tok}  (set in env to make it stable)")
        return tok


# --- Bearer -> opex_user (cached) -----------------------------------------
_auth_cache: dict[str, tuple[float, dict]] = {}
_auth_cache_lock = threading.Lock()


def resolve_user_from_token(token: str | None) -> dict | None:
    if not token:
        return None
    now = time.time()
    with _auth_cache_lock:
        hit = _auth_cache.get(token)
        if hit and now - hit[0] < AUTH_TTL_SECONDS:
            return hit[1]
    req = _http_request(
        f"{AUTH_BASE}/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with _http_urlopen(req, timeout=5) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log(f"auth/me HTTP {e.code} for token={token[:8]}…")
        return None
    except Exception as e:  # noqa: BLE001
        log(f"auth/me transport error: {e!r}")
        return None
    user = (obj or {}).get("user") or {}
    if not user.get("opex_user"):
        return None
    with _auth_cache_lock:
        _auth_cache[token] = (now, user)
    return user


# --- Price oracle (last-trade based with 20s cache) -----------------------
_price_cache: dict[str, tuple[float, Decimal]] = {}
_price_cache_lock = threading.Lock()


def price_usdt(asset: str) -> Decimal:
    """Return ``asset``'s last spot price in USDT. USDT itself is 1.

    Falls back to ``PRICE_HINTS_USDT`` if /v3/ticker is unavailable or the
    symbol isn't listed (e.g. BTCUSDT on a hardhat demo with no BTC market).
    """
    asset = asset.upper()
    if asset == "USDT":
        return ONE
    now = time.time()
    with _price_cache_lock:
        hit = _price_cache.get(asset)
        if hit and now - hit[0] < PRICE_TTL_S:
            return hit[1]
    sym = f"{asset}USDT"
    try:
        with _http_urlopen(
            f"{MARKET_BASE}/v3/ticker/24hr?symbol={urllib.parse.quote(sym)}",
            timeout=4,
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, list):
            data = data[0] if data else {}
        last = data.get("lastPrice") or data.get("price")
        if last:
            px = Decimal(str(last))
            if px > 0:
                with _price_cache_lock:
                    _price_cache[asset] = (now, px)
                return px
    except Exception as e:  # noqa: BLE001
        log(f"price fetch {asset} failed: {e!r} — falling back to hint")
    hint = PRICE_HINTS_USDT.get(asset, ZERO)
    with _price_cache_lock:
        # Cache hint for a short period too so we don't hammer a dead market.
        _price_cache[asset] = (now, hint)
    return hint


def usdt_value(asset: str, amount: Decimal) -> Decimal:
    return amount * price_usdt(asset)


# --- Interest rate model (Compound v2 style) ------------------------------
def compute_apy_bps(
    utilization_bps: int,
    base_rate_bps: int,
    optimal_util_bps: int,
    slope1_bps: int,
    slope2_bps: int,
    reserve_factor_bps: int,
) -> tuple[int, int]:
    """Return ``(borrow_apy_bps, supply_apy_bps)``.

    Two-slope curve:
      U <= U*  : borrow = base + slope1 * (U / U*)
      U >  U*  : borrow = base + slope1 + slope2 * (U - U*) / (1 - U*)
    Supply pays out the spread that doesn't go to reserves:
      supply = borrow * U * (1 - reserve_factor)
    """
    u = Decimal(utilization_bps) / BPS
    u_star = Decimal(optimal_util_bps) / BPS
    base = Decimal(base_rate_bps) / BPS
    s1 = Decimal(slope1_bps) / BPS
    s2 = Decimal(slope2_bps) / BPS
    if u <= u_star and u_star > 0:
        borrow = base + s1 * (u / u_star)
    elif u_star <= 0:
        borrow = base + s1
    else:
        denom = ONE - u_star
        if denom <= 0:
            borrow = base + s1 + s2
        else:
            borrow = base + s1 + s2 * (u - u_star) / denom
    rf = Decimal(reserve_factor_bps) / BPS
    supply = borrow * u * (ONE - rf)
    return int(borrow * BPS), int(supply * BPS)


def utilization_bps(supplied: Decimal, borrowed: Decimal) -> int:
    if supplied <= 0:
        return 0
    ratio = borrowed / supplied
    if ratio < 0:
        return 0
    if ratio > ONE:
        ratio = ONE
    return int(ratio * BPS)


def serialise_pool(row: sqlite3.Row) -> dict:
    return {
        "asset": row["asset"],
        "total_supplied": row["total_supplied"],
        "total_borrowed": row["total_borrowed"],
        "reserves": row["reserves"],
        "utilization_bps": int(row["utilization_bps"]),
        "utilization_pct": float(Decimal(row["utilization_bps"]) / Decimal("100")),
        "supply_apy_bps": int(row["supply_apy_bps"]),
        "supply_apy_pct": float(Decimal(row["supply_apy_bps"]) / Decimal("100")),
        "borrow_apy_bps": int(row["borrow_apy_bps"]),
        "borrow_apy_pct": float(Decimal(row["borrow_apy_bps"]) / Decimal("100")),
        "collateral_factor_bps": int(row["collateral_factor_bps"]),
        "max_ltv_pct": float(Decimal(row["collateral_factor_bps"]) / Decimal("100")),
        "liquidation_threshold_bps": int(row["liquidation_threshold_bps"]),
        "liquidation_threshold_pct": float(
            Decimal(row["liquidation_threshold_bps"]) / Decimal("100")
        ),
        "liquidation_bonus_bps": int(row["liquidation_bonus_bps"]),
        "reserve_factor_bps": int(row["reserve_factor_bps"]),
        "optimal_utilization_bps": int(row["optimal_utilization_bps"]),
        "rate_slope1_bps": int(row["rate_slope1_bps"]),
        "rate_slope2_bps": int(row["rate_slope2_bps"]),
        "base_rate_bps": int(row["base_rate_bps"]),
        "active": bool(row["active"]),
        "supply_cap_usdt": dstr(SUPPLY_CAPS.get(row["asset"], ZERO)),
        "price_usdt": dstr(price_usdt(row["asset"])),
    }


# --- Wallet bridge ---------------------------------------------------------
def _wallet_transfer(
    *,
    amount: Decimal,
    asset: str,
    from_user: str,
    to_user: str,
    ref: str,
    description: str,
    category: str = "WITHDRAW_REQUEST",
) -> tuple[bool, str]:
    """POST /v2/transfer/<amount>_<asset>/from/<user>_MAIN/to/<vault>_MAIN.

    Supports fractional amounts (wallet's BigDecimal path-variable parser
    handles the literal ``0.5`` form fine). Returns (ok, message).
    """
    if amount <= 0:
        return False, "transfer amount must be > 0"
    # Internal wallet uses ETH / USDT directly. There's no separate symbol
    # mapping for the demo's BTC pool (its wallet won't be hit unless you
    # arrange it).
    amount_str = format(amount, "f")
    if "." in amount_str:
        amount_str = amount_str.rstrip("0").rstrip(".")
    if not amount_str:
        amount_str = "0"
    amount_path = urllib.parse.quote(amount_str, safe="")
    url = (
        f"{WALLET_BASE}/v2/transfer/{amount_path}_{urllib.parse.quote(asset)}"
        f"/from/{urllib.parse.quote(from_user)}_MAIN"
        f"/to/{urllib.parse.quote(to_user)}_MAIN"
    )
    body = json.dumps(
        {
            "description": description,
            "transferRef": ref,
            "transferCategory": category,
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
            txt = e.read().decode("utf-8", errors="replace")[:300]
        except Exception as read_error:  # noqa: BLE001
            log(f"wallet error body read failed: {read_error!r}")
        return False, f"wallet HTTP {e.code}: {txt}"
    except Exception as e:  # noqa: BLE001
        return False, f"wallet transport: {e!r}"
    return True, "ok"


def _wallet_get_balance(opex_user: str, asset: str) -> Decimal:
    """Best-effort: read the user's available spot balance for ``asset``."""
    try:
        with _http_urlopen(
            f"{WALLET_BASE}/v1/owner/{urllib.parse.quote(opex_user)}/wallets",
            timeout=4,
        ) as resp:
            wallets = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        log(f"wallet balance lookup skipped for {opex_user}: {e!r}")
        return ZERO
    if not isinstance(wallets, list):
        wallets = wallets.get("wallets", []) if isinstance(wallets, dict) else []
    for w in wallets:
        if str(w.get("asset")) == asset:
            try:
                return Decimal(str(w.get("balance", "0")))
            except Exception:  # noqa: BLE001
                return ZERO
    return ZERO


# --- History helper --------------------------------------------------------
def _record_event(
    conn: sqlite3.Connection,
    *,
    opex_user: str,
    event: str,
    asset: str,
    amount: Decimal,
    metadata: dict | None = None,
) -> None:
    conn.execute(
        "INSERT INTO lending_history (ts, opex_user, event, asset, amount, metadata_json) "
        "VALUES (?,?,?,?,?,?)",
        (
            now_s(),
            opex_user,
            event,
            asset,
            dstr(amount),
            json.dumps(metadata, separators=(",", ":")) if metadata else None,
        ),
    )


# --- Pool accounting helpers ----------------------------------------------
def _update_pool_aggregates(
    conn: sqlite3.Connection,
    asset: str,
    *,
    delta_supplied: Decimal = ZERO,
    delta_borrowed: Decimal = ZERO,
    delta_reserves: Decimal = ZERO,
) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM lending_pools WHERE asset=?", (asset,)).fetchone()
    if not row:
        raise ValueError(f"unknown pool: {asset}")
    new_supplied = D(row["total_supplied"]) + delta_supplied
    new_borrowed = D(row["total_borrowed"]) + delta_borrowed
    new_reserves = D(row["reserves"]) + delta_reserves
    if new_supplied < 0:
        new_supplied = ZERO
    if new_borrowed < 0:
        new_borrowed = ZERO
    if new_reserves < 0:
        new_reserves = ZERO
    util = utilization_bps(new_supplied, new_borrowed)
    borrow_apy, supply_apy = compute_apy_bps(
        util,
        int(row["base_rate_bps"]),
        int(row["optimal_utilization_bps"]),
        int(row["rate_slope1_bps"]),
        int(row["rate_slope2_bps"]),
        int(row["reserve_factor_bps"]),
    )
    conn.execute(
        "UPDATE lending_pools SET total_supplied=?, total_borrowed=?, "
        "reserves=?, utilization_bps=?, supply_apy_bps=?, borrow_apy_bps=? "
        "WHERE asset=?",
        (
            dstr(new_supplied),
            dstr(new_borrowed),
            dstr(new_reserves),
            util,
            supply_apy,
            borrow_apy,
            asset,
        ),
    )
    return conn.execute("SELECT * FROM lending_pools WHERE asset=?", (asset,)).fetchone()


# --- LTV computation -------------------------------------------------------
def _current_ltv_bps(
    borrowed_asset: str, borrowed_value: Decimal, collateral_asset: str, collateral_amount: Decimal
) -> int:
    """LTV = (debt USDT) / (collateral USDT)."""
    debt_usdt = usdt_value(borrowed_asset, borrowed_value)
    coll_usdt = usdt_value(collateral_asset, collateral_amount)
    if coll_usdt <= 0:
        return 999999  # effectively "underwater"
    ratio = debt_usdt / coll_usdt
    if ratio < 0:
        return 0
    return int(ratio * BPS)


def _total_borrow_notional_usdt(opex_user: str) -> Decimal:
    """Sum the user's outstanding debt across all open borrow positions, in
    USDT-equivalent. Used to enforce KYC-tiered borrow ceilings."""
    total = ZERO
    with db() as c:
        rows = c.execute(
            "SELECT borrowed_asset, borrowed_principal, borrowed_interest "
            "FROM borrow_positions WHERE opex_user=? AND status='OPEN'",
            (opex_user,),
        ).fetchall()
    for r in rows:
        out = D(r["borrowed_principal"]) + D(r["borrowed_interest"])
        total += usdt_value(r["borrowed_asset"], out)
    return total


# --- Background accrual + liquidation engine ------------------------------
_engine_lock = threading.Lock()  # only one accrual tick at a time
_runtime_state: dict[str, Any] = {
    "last_accrual_at": 0,
    "last_accrual_ms": 0,
    "n_accrual_runs": 0,
    "n_liquidations": 0,
    "last_error": None,
}


def _accrue_pool_rates(conn: sqlite3.Connection) -> None:
    """Recompute utilisation + APYs on every active pool."""
    rows = conn.execute("SELECT * FROM lending_pools WHERE active=1").fetchall()
    for row in rows:
        supplied = D(row["total_supplied"])
        borrowed = D(row["total_borrowed"])
        util = utilization_bps(supplied, borrowed)
        borrow_apy, supply_apy = compute_apy_bps(
            util,
            int(row["base_rate_bps"]),
            int(row["optimal_utilization_bps"]),
            int(row["rate_slope1_bps"]),
            int(row["rate_slope2_bps"]),
            int(row["reserve_factor_bps"]),
        )
        conn.execute(
            "UPDATE lending_pools SET utilization_bps=?, supply_apy_bps=?, "
            "borrow_apy_bps=? WHERE asset=?",
            (util, supply_apy, borrow_apy, row["asset"]),
        )


def _accrue_supply_positions(conn: sqlite3.Connection, ts: int) -> int:
    """Add ``principal * supply_apy * dt / year`` to each open supply
    position's accrued_interest. Returns number of rows touched."""
    rows = conn.execute(
        "SELECT sp.id, sp.principal, sp.accrued_interest, sp.last_accrual_at, "
        "sp.asset, p.supply_apy_bps "
        "FROM supply_positions sp JOIN lending_pools p ON p.asset=sp.asset "
        "WHERE sp.status='OPEN'"
    ).fetchall()
    n = 0
    for r in rows:
        dt = max(0, ts - int(r["last_accrual_at"]))
        if dt <= 0:
            continue
        rate = Decimal(int(r["supply_apy_bps"])) / BPS
        delta = D(r["principal"]) * rate * Decimal(dt) / SECONDS_PER_YEAR
        if delta < 0:
            delta = ZERO
        new_int = D(r["accrued_interest"]) + delta
        conn.execute(
            "UPDATE supply_positions SET accrued_interest=?, last_accrual_at=? WHERE id=?",
            (dstr(new_int), ts, int(r["id"])),
        )
        n += 1
    return n


def _accrue_borrow_positions(conn: sqlite3.Connection, ts: int) -> tuple[int, list[int]]:
    """Add ``principal * borrow_apy * dt / year`` to each open borrow
    position's borrowed_interest. Also recompute current_ltv_bps and
    return the list of position_ids that are now past the liquidation
    threshold. Reserves are credited the ``reserve_factor`` share of the
    interest. The remaining interest accrues into the pool's
    total_supplied (i.e. lender yield).
    """
    rows = conn.execute(
        "SELECT bp.id, bp.opex_user, bp.borrowed_asset, bp.borrowed_principal, "
        "bp.borrowed_interest, bp.collateral_asset, bp.collateral_amount, "
        "bp.last_accrual_at, p.borrow_apy_bps, p.reserve_factor_bps, "
        "p.liquidation_threshold_bps "
        "FROM borrow_positions bp JOIN lending_pools p ON p.asset=bp.borrowed_asset "
        "WHERE bp.status='OPEN'"
    ).fetchall()
    n = 0
    at_risk: list[int] = []
    # Bucket per-asset interest deltas so we can update aggregates once.
    asset_supply_delta: dict[str, Decimal] = {}
    asset_reserves_delta: dict[str, Decimal] = {}
    asset_borrow_delta: dict[str, Decimal] = {}
    for r in rows:
        dt = max(0, ts - int(r["last_accrual_at"]))
        if dt <= 0:
            # Still recompute LTV in case price moved.
            outstanding = D(r["borrowed_principal"]) + D(r["borrowed_interest"])
            ltv = _current_ltv_bps(
                r["borrowed_asset"],
                outstanding,
                r["collateral_asset"],
                D(r["collateral_amount"]),
            )
            conn.execute(
                "UPDATE borrow_positions SET current_ltv_bps=? WHERE id=?",
                (ltv, int(r["id"])),
            )
            if ltv >= int(r["liquidation_threshold_bps"]):
                at_risk.append(int(r["id"]))
            continue
        rate = Decimal(int(r["borrow_apy_bps"])) / BPS
        delta = D(r["borrowed_principal"]) * rate * Decimal(dt) / SECONDS_PER_YEAR
        if delta < 0:
            delta = ZERO
        new_int = D(r["borrowed_interest"]) + delta
        outstanding = D(r["borrowed_principal"]) + new_int
        ltv = _current_ltv_bps(
            r["borrowed_asset"],
            outstanding,
            r["collateral_asset"],
            D(r["collateral_amount"]),
        )
        conn.execute(
            "UPDATE borrow_positions SET borrowed_interest=?, last_accrual_at=?, "
            "current_ltv_bps=? WHERE id=?",
            (dstr(new_int), ts, ltv, int(r["id"])),
        )
        n += 1
        # Split delta into reserves + supply yield.
        rf = Decimal(int(r["reserve_factor_bps"])) / BPS
        reserve_share = delta * rf
        supply_share = delta - reserve_share
        a = r["borrowed_asset"]
        asset_reserves_delta[a] = asset_reserves_delta.get(a, ZERO) + reserve_share
        asset_supply_delta[a] = asset_supply_delta.get(a, ZERO) + supply_share
        asset_borrow_delta[a] = asset_borrow_delta.get(a, ZERO) + delta
        if ltv >= int(r["liquidation_threshold_bps"]):
            at_risk.append(int(r["id"]))
    # Apply pool aggregate updates.
    for asset in set(
        list(asset_supply_delta) + list(asset_reserves_delta) + list(asset_borrow_delta)
    ):
        _update_pool_aggregates(
            conn,
            asset,
            delta_supplied=asset_supply_delta.get(asset, ZERO),
            delta_borrowed=asset_borrow_delta.get(asset, ZERO),
            delta_reserves=asset_reserves_delta.get(asset, ZERO),
        )
    return n, at_risk


def _engine_tick() -> dict:
    """One pass of: pool rate recompute, supply accrual, borrow accrual,
    liquidation triggering. Returns a summary dict.
    """
    started = time.monotonic()
    ts = now_s()
    summary: dict[str, Any] = {
        "ts": ts,
        "supply_accrued": 0,
        "borrow_accrued": 0,
        "at_risk": 0,
        "liquidated": [],
    }
    with _engine_lock:
        with _db_lock, db() as conn:
            _accrue_pool_rates(conn)
            summary["supply_accrued"] = _accrue_supply_positions(conn, ts)
            n_bor, at_risk = _accrue_borrow_positions(conn, ts)
            summary["borrow_accrued"] = n_bor
            summary["at_risk"] = len(at_risk)
        # Liquidate per-position outside the global DB lock — each
        # liquidation takes a per-row lock and reads its own snapshot.
        for pid in at_risk:
            try:
                res = _liquidate_position(pid, reason="ltv_exceeded")
                if res.get("ok"):
                    summary["liquidated"].append(pid)
                    _runtime_state["n_liquidations"] += 1
            except Exception as e:  # noqa: BLE001
                log(f"liquidate {pid} failed: {e!r}")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    _runtime_state["last_accrual_at"] = ts
    _runtime_state["last_accrual_ms"] = elapsed_ms
    _runtime_state["n_accrual_runs"] += 1
    summary["elapsed_ms"] = elapsed_ms
    return summary


def _engine_loop() -> None:
    """Background accrual + liquidation thread."""
    log(f"engine loop start, interval={ACCRUAL_INTERVAL_S}s")
    # First tick after a short delay so the wallet API can come up.
    time.sleep(2)
    while True:
        try:
            res = _engine_tick()
            if res["supply_accrued"] or res["borrow_accrued"] or res["liquidated"]:
                log(
                    f"tick supply={res['supply_accrued']} borrow={res['borrow_accrued']} "
                    f"at_risk={res['at_risk']} liq={len(res['liquidated'])} "
                    f"in {res['elapsed_ms']}ms"
                )
            _runtime_state["last_error"] = None
        except Exception as e:  # noqa: BLE001
            _runtime_state["last_error"] = repr(e)
            log(f"engine tick error: {e!r}")
        time.sleep(ACCRUAL_INTERVAL_S)


# --- Liquidation ----------------------------------------------------------
def _liquidate_position(position_id: int, *, reason: str) -> dict:
    """Force-close one borrow position. Sells collateral worth
    ``outstanding_debt + liquidation_bonus`` and uses it to repay the debt
    + give the bonus to the liquidator (the protocol). Any leftover
    collateral is returned to the borrower's spot wallet.

    The protocol acts as its own liquidator here — there's no third-party
    auction in this demo. The seized collateral amount is computed in
    USDT-equivalent and the matching position fields are updated.
    """
    lk = _pos_lock(position_id)
    if not lk.acquire(timeout=5):
        return {"ok": False, "error": "position_locked"}
    try:
        with _db_lock, db() as conn:
            row = conn.execute(
                "SELECT * FROM borrow_positions WHERE id=?", (position_id,)
            ).fetchone()
            if not row:
                return {"ok": False, "error": "not_found"}
            if row["status"] != "OPEN":
                return {"ok": False, "error": f"status={row['status']}"}
            pool = conn.execute(
                "SELECT * FROM lending_pools WHERE asset=?",
                (row["borrowed_asset"],),
            ).fetchone()
            if not pool:
                return {"ok": False, "error": "pool_missing"}
            outstanding = D(row["borrowed_principal"]) + D(row["borrowed_interest"])
            outstanding_usdt = usdt_value(row["borrowed_asset"], outstanding)
            bonus_bps = int(pool["liquidation_bonus_bps"])
            seize_usdt = outstanding_usdt * (ONE + Decimal(bonus_bps) / BPS)
            coll_px = price_usdt(row["collateral_asset"])
            if coll_px <= 0:
                return {"ok": False, "error": "no_collateral_price"}
            seize_coll = seize_usdt / coll_px
            held = D(row["collateral_amount"])
            if seize_coll > held:
                # Under-collateralised: seize all collateral; protocol eats
                # the shortfall. In a real system the SAFU / insurance
                # fund would top this up. Here we just record the bad debt.
                seize_coll = held
                shortfall_usdt = outstanding_usdt - seize_coll * coll_px
            else:
                shortfall_usdt = ZERO
            remaining_coll = held - seize_coll
            ts = now_s()
            ref_prefix = f"liq-{position_id}-{ts}"
            # 1. Repay debt: reduce pool.total_borrowed by ``outstanding``.
            _update_pool_aggregates(
                conn,
                row["borrowed_asset"],
                delta_borrowed=-outstanding,
            )
            # 2. The protocol keeps the bonus and the seized collateral
            #    (notional — no chain transfer). Mark the row liquidated.
            conn.execute(
                "UPDATE borrow_positions SET status='LIQUIDATED', "
                "liquidated_at=?, borrowed_interest=?, collateral_amount=?, "
                "current_ltv_bps=? WHERE id=?",
                (ts, "0", dstr(remaining_coll), 0, position_id),
            )
            # 3. Return any leftover collateral to the user's MAIN wallet.
            _record_event(
                conn,
                opex_user=row["opex_user"],
                event="liquidation",
                asset=row["collateral_asset"],
                amount=seize_coll,
                metadata={
                    "position_id": position_id,
                    "reason": reason,
                    "borrowed_asset": row["borrowed_asset"],
                    "outstanding": dstr(outstanding),
                    "outstanding_usdt": dstr(outstanding_usdt),
                    "seized_collateral": dstr(seize_coll),
                    "bonus_bps": bonus_bps,
                    "remaining_collateral_returned": dstr(remaining_coll),
                    "shortfall_usdt": dstr(shortfall_usdt),
                },
            )
        # Outside DB lock: do the chain-side collateral release (best effort).
        if remaining_coll > 0:
            ok, msg = _wallet_transfer(
                amount=remaining_coll,
                asset=row["collateral_asset"],
                from_user=VAULT_OPEX_USER,
                to_user=row["opex_user"],
                ref=f"{ref_prefix}-release",
                description=f"zkcex-lending liquidation collateral return pos={position_id}",
                category="WITHDRAW_REQUEST",
            )
            if not ok:
                log(f"liquidation collateral release pos={position_id} wallet fail: {msg}")
        log(
            f"liquidated pos={position_id} user={row['opex_user']} "
            f"debt={dstr(outstanding)} {row['borrowed_asset']} "
            f"seized={dstr(seize_coll)} {row['collateral_asset']} "
            f"shortfall_usdt={dstr(shortfall_usdt)}"
        )
        return {
            "ok": True,
            "position_id": position_id,
            "outstanding": dstr(outstanding),
            "outstanding_usdt": dstr(outstanding_usdt),
            "seized_collateral": dstr(seize_coll),
            "remaining_collateral_returned": dstr(remaining_coll),
            "shortfall_usdt": dstr(shortfall_usdt),
        }
    finally:
        lk.release()


# --- Core actions ----------------------------------------------------------
def action_supply(opex_user: str, asset: str, amount: Decimal) -> dict:
    """Lock ``amount`` of ``asset`` from the user's MAIN wallet into the
    pool's vault. Create a supply_position. Returns the created row."""
    if amount <= 0:
        return {"ok": False, "error": "amount must be > 0"}
    with db() as conn:
        pool = conn.execute("SELECT * FROM lending_pools WHERE asset=?", (asset,)).fetchone()
    if not pool or not pool["active"]:
        return {"ok": False, "error": f"pool inactive or unknown: {asset}"}
    # Soft cap: refuse if the pool is at its supply cap (USDT-equivalent).
    cap = SUPPLY_CAPS.get(asset, ZERO)
    if cap > 0:
        cap_usdt_eq = cap
        cur_usdt_eq = usdt_value(asset, D(pool["total_supplied"]) + amount)
        if cur_usdt_eq > cap_usdt_eq:
            return {
                "ok": False,
                "error": "pool_supply_cap_reached",
                "cap_usdt": dstr(cap_usdt_eq),
                "current_usdt": dstr(usdt_value(asset, D(pool["total_supplied"]))),
            }
    # 1. Move funds from user.MAIN -> VAULT.MAIN.
    ts = now_s()
    ref = f"supply-{opex_user}-{asset}-{ts}-{secrets.token_hex(4)}"
    ok, msg = _wallet_transfer(
        amount=amount,
        asset=asset,
        from_user=opex_user,
        to_user=VAULT_OPEX_USER,
        ref=ref,
        description=f"zkcex-lending supply {asset}",
        category="WITHDRAW_REQUEST",
    )
    if not ok:
        return {"ok": False, "error": "wallet_debit_failed", "detail": msg}
    # 2. Create position + update pool aggregates.
    with _db_lock, db() as conn:
        cur = conn.execute(
            "INSERT INTO supply_positions "
            "(opex_user, asset, principal, supplied_at, last_accrual_at, status) "
            "VALUES (?,?,?,?,?,'OPEN')",
            (opex_user, asset, dstr(amount), ts, ts),
        )
        pid = int(cur.lastrowid)
        new_pool = _update_pool_aggregates(conn, asset, delta_supplied=amount)
        _record_event(
            conn,
            opex_user=opex_user,
            event="supply",
            asset=asset,
            amount=amount,
            metadata={"position_id": pid, "ref": ref},
        )
        position = conn.execute("SELECT * FROM supply_positions WHERE id=?", (pid,)).fetchone()
    return {
        "ok": True,
        "position": serialise_supply(position),
        "pool": serialise_pool(new_pool),
    }


def action_withdraw_supply(opex_user: str, position_id: int, amount: Decimal | None) -> dict:
    """Burn part or all of a supply_position. Releases principal + accrued
    interest proportionally."""
    lk = _pos_lock(position_id)
    if not lk.acquire(timeout=5):
        return {"ok": False, "error": "position_locked"}
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT * FROM supply_positions WHERE id=? AND opex_user=?",
                (position_id, opex_user),
            ).fetchone()
            if not row:
                return {"ok": False, "error": "not_found"}
            if row["status"] != "OPEN":
                return {"ok": False, "error": f"status={row['status']}"}
            pool = conn.execute(
                "SELECT * FROM lending_pools WHERE asset=?",
                (row["asset"],),
            ).fetchone()
        principal = D(row["principal"])
        interest = D(row["accrued_interest"])
        total = principal + interest
        if amount is None or amount >= total:
            amount = total
        if amount <= 0:
            return {"ok": False, "error": "amount must be > 0"}
        # Liquidity check: the pool must have enough idle to repay.
        idle = D(pool["total_supplied"]) - D(pool["total_borrowed"])
        if amount > idle:
            return {
                "ok": False,
                "error": "pool_illiquid",
                "available": dstr(idle),
                "requested": dstr(amount),
            }
        ts = now_s()
        ref = f"withdraw-{position_id}-{ts}"
        ok, msg = _wallet_transfer(
            amount=amount,
            asset=row["asset"],
            from_user=VAULT_OPEX_USER,
            to_user=opex_user,
            ref=ref,
            description=f"zkcex-lending withdraw {row['asset']}",
            category="WITHDRAW_REQUEST",
        )
        if not ok:
            return {"ok": False, "error": "wallet_credit_failed", "detail": msg}
        # Apportion the burn: take from interest first, then principal.
        take_interest = min(interest, amount)
        take_principal = amount - take_interest
        new_principal = principal - take_principal
        new_interest = interest - take_interest
        with _db_lock, db() as conn:
            closed = new_principal <= 0 and new_interest <= 0
            if closed:
                conn.execute(
                    "UPDATE supply_positions SET principal='0', accrued_interest='0', "
                    "withdrawn_at=?, status='CLOSED' WHERE id=?",
                    (ts, position_id),
                )
            else:
                conn.execute(
                    "UPDATE supply_positions SET principal=?, accrued_interest=?, "
                    "last_accrual_at=? WHERE id=?",
                    (dstr(new_principal), dstr(new_interest), ts, position_id),
                )
            # Pool: total_supplied loses both the principal AND interest
            # that's being withdrawn (interest was added in via borrow accrual
            # earlier, so this is symmetric).
            _update_pool_aggregates(conn, row["asset"], delta_supplied=-amount)
            _record_event(
                conn,
                opex_user=opex_user,
                event="withdraw_supply",
                asset=row["asset"],
                amount=amount,
                metadata={
                    "position_id": position_id,
                    "took_principal": dstr(take_principal),
                    "took_interest": dstr(take_interest),
                    "closed": closed,
                    "ref": ref,
                },
            )
            position = conn.execute(
                "SELECT * FROM supply_positions WHERE id=?", (position_id,)
            ).fetchone()
            new_pool = conn.execute(
                "SELECT * FROM lending_pools WHERE asset=?",
                (row["asset"],),
            ).fetchone()
        return {
            "ok": True,
            "withdrawn": dstr(amount),
            "took_principal": dstr(take_principal),
            "took_interest": dstr(take_interest),
            "position": serialise_supply(position),
            "pool": serialise_pool(new_pool),
        }
    finally:
        lk.release()


def action_borrow(
    opex_user: str,
    kyc_status: str,
    borrowed_asset: str,
    borrow_amount: Decimal,
    collateral_asset: str,
    collateral_amount: Decimal,
) -> dict:
    if borrow_amount <= 0 or collateral_amount <= 0:
        return {"ok": False, "error": "amounts must be > 0"}
    if borrowed_asset == collateral_asset:
        return {"ok": False, "error": "borrowed_asset must differ from collateral_asset"}
    # 1. Pool existence + activity.
    with db() as conn:
        pool = conn.execute(
            "SELECT * FROM lending_pools WHERE asset=?",
            (borrowed_asset,),
        ).fetchone()
        coll_pool = conn.execute(
            "SELECT * FROM lending_pools WHERE asset=?",
            (collateral_asset,),
        ).fetchone()
    if not pool or not pool["active"]:
        return {"ok": False, "error": f"borrow pool inactive: {borrowed_asset}"}
    if not coll_pool or not coll_pool["active"]:
        return {"ok": False, "error": f"collateral asset not supported: {collateral_asset}"}
    # 2. LTV validation against the COLLATERAL pool's collateral_factor (the
    #    "you can use this asset as collateral up to N% of its value" param).
    debt_usdt = usdt_value(borrowed_asset, borrow_amount)
    coll_usdt = usdt_value(collateral_asset, collateral_amount)
    if coll_usdt <= 0:
        return {"ok": False, "error": "no_collateral_price", "collateral_asset": collateral_asset}
    cf_bps = int(coll_pool["collateral_factor_bps"])
    max_debt_usdt = coll_usdt * Decimal(cf_bps) / BPS
    ltv_bps = _current_ltv_bps(borrowed_asset, borrow_amount, collateral_asset, collateral_amount)
    if debt_usdt > max_debt_usdt:
        return {
            "ok": False,
            "error": "ltv_exceeds_collateral_factor",
            "max_ltv_bps": cf_bps,
            "requested_ltv_bps": ltv_bps,
            "max_borrow_usdt": dstr(max_debt_usdt),
            "requested_borrow_usdt": dstr(debt_usdt),
        }
    # 3. Pool liquidity check (we can't lend out more than exists).
    idle = D(pool["total_supplied"]) - D(pool["total_borrowed"])
    if borrow_amount > idle:
        return {
            "ok": False,
            "error": "pool_illiquid",
            "available": dstr(idle),
            "requested": dstr(borrow_amount),
        }
    # 4. KYC-tiered ceiling.
    tier_cap = BORROW_LIMITS_USDT.get(kyc_status or "none", ZERO)
    cur_debt_usdt = _total_borrow_notional_usdt(opex_user)
    if tier_cap <= 0:
        return {
            "ok": False,
            "error": "kyc_required",
            "kyc_status": kyc_status,
            "tier_cap_usdt": dstr(tier_cap),
        }
    if cur_debt_usdt + debt_usdt > tier_cap:
        return {
            "ok": False,
            "error": "borrow_limit_exceeded",
            "kyc_status": kyc_status,
            "tier_cap_usdt": dstr(tier_cap),
            "current_debt_usdt": dstr(cur_debt_usdt),
            "requested_usdt": dstr(debt_usdt),
        }
    # 5. Lock the collateral (user.MAIN -> VAULT.MAIN).
    ts = now_s()
    ref_coll = f"collateral-{opex_user}-{ts}-{secrets.token_hex(4)}"
    ok, msg = _wallet_transfer(
        amount=collateral_amount,
        asset=collateral_asset,
        from_user=opex_user,
        to_user=VAULT_OPEX_USER,
        ref=ref_coll,
        description=f"zkcex-lending collateral {collateral_asset}",
        category="WITHDRAW_REQUEST",
    )
    if not ok:
        return {"ok": False, "error": "wallet_collateral_lock_failed", "detail": msg}
    # 6. Send borrowed asset out of the vault to the user.
    ref_borrow = f"borrow-{opex_user}-{ts}-{secrets.token_hex(4)}"
    ok2, msg2 = _wallet_transfer(
        amount=borrow_amount,
        asset=borrowed_asset,
        from_user=VAULT_OPEX_USER,
        to_user=opex_user,
        ref=ref_borrow,
        description=f"zkcex-lending borrow {borrowed_asset}",
        category="WITHDRAW_REQUEST",
    )
    if not ok2:
        # Compensating transfer: refund the collateral so we don't strand
        # user funds in the vault.
        refund_ref = f"refund-{ref_coll}"
        refund_ok, refund_msg = _wallet_transfer(
            amount=collateral_amount,
            asset=collateral_asset,
            from_user=VAULT_OPEX_USER,
            to_user=opex_user,
            ref=refund_ref,
            description="zkcex-lending borrow-rollback refund",
            category="WITHDRAW_REQUEST",
        )
        if not refund_ok:
            log(f"borrow rollback refund failed: {refund_msg}")
        return {"ok": False, "error": "wallet_borrow_credit_failed", "detail": msg2}
    # 7. Record position + update pool aggregates.
    with _db_lock, db() as conn:
        cur = conn.execute(
            "INSERT INTO borrow_positions "
            "(opex_user, borrowed_asset, borrowed_principal, "
            " collateral_asset, collateral_amount, borrowed_at, "
            " last_accrual_at, status, ltv_at_open_bps, current_ltv_bps) "
            "VALUES (?,?,?,?,?,?,?,'OPEN',?,?)",
            (
                opex_user,
                borrowed_asset,
                dstr(borrow_amount),
                collateral_asset,
                dstr(collateral_amount),
                ts,
                ts,
                ltv_bps,
                ltv_bps,
            ),
        )
        pid = int(cur.lastrowid)
        new_pool = _update_pool_aggregates(conn, borrowed_asset, delta_borrowed=borrow_amount)
        _record_event(
            conn,
            opex_user=opex_user,
            event="borrow",
            asset=borrowed_asset,
            amount=borrow_amount,
            metadata={
                "position_id": pid,
                "collateral_asset": collateral_asset,
                "collateral_amount": dstr(collateral_amount),
                "ltv_at_open_bps": ltv_bps,
                "ref_borrow": ref_borrow,
                "ref_coll": ref_coll,
            },
        )
        position = conn.execute("SELECT * FROM borrow_positions WHERE id=?", (pid,)).fetchone()
    return {
        "ok": True,
        "position": serialise_borrow(position),
        "pool": serialise_pool(new_pool),
        "liquidation_price_usdt": _liquidation_price_usdt(position, pool),
    }


def _liquidation_price_usdt(borrow_row: sqlite3.Row, pool_row: sqlite3.Row) -> str:
    """USDT price of the collateral asset at which this position's LTV
    crosses the liquidation_threshold. Returns "0" if no meaningful
    collateral price exists.

    LTV = debt_usdt / coll_usdt = liq_threshold
      => coll_usdt = debt_usdt / liq_threshold
      => coll_price = debt_usdt / (liq_threshold * collateral_amount)
    """
    coll_amt = D(borrow_row["collateral_amount"])
    if coll_amt <= 0:
        return "0"
    outstanding = D(borrow_row["borrowed_principal"]) + D(borrow_row["borrowed_interest"])
    debt_usdt = usdt_value(borrow_row["borrowed_asset"], outstanding)
    threshold = Decimal(int(pool_row["liquidation_threshold_bps"])) / BPS
    if threshold <= 0:
        return "0"
    px = debt_usdt / (threshold * coll_amt)
    return dstr(px)


def action_repay(opex_user: str, position_id: int, amount: Decimal | None) -> dict:
    lk = _pos_lock(position_id)
    if not lk.acquire(timeout=5):
        return {"ok": False, "error": "position_locked"}
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT * FROM borrow_positions WHERE id=? AND opex_user=?",
                (position_id, opex_user),
            ).fetchone()
            if not row:
                return {"ok": False, "error": "not_found"}
            if row["status"] != "OPEN":
                return {"ok": False, "error": f"status={row['status']}"}
        principal = D(row["borrowed_principal"])
        interest = D(row["borrowed_interest"])
        outstanding = principal + interest
        if amount is None or amount >= outstanding:
            amount = outstanding
        if amount <= 0:
            return {"ok": False, "error": "amount must be > 0"}
        ts = now_s()
        ref = f"repay-{position_id}-{ts}"
        # Pull repayment from the user's wallet into the vault.
        ok, msg = _wallet_transfer(
            amount=amount,
            asset=row["borrowed_asset"],
            from_user=opex_user,
            to_user=VAULT_OPEX_USER,
            ref=ref,
            description=f"zkcex-lending repay {row['borrowed_asset']}",
            category="WITHDRAW_REQUEST",
        )
        if not ok:
            return {"ok": False, "error": "wallet_repay_debit_failed", "detail": msg}
        # Apportion: take interest first, then principal.
        take_interest = min(interest, amount)
        take_principal = amount - take_interest
        new_principal = principal - take_principal
        new_interest = interest - take_interest
        # Proportional collateral release.
        full = new_principal <= 0 and new_interest <= 0
        coll_amount = D(row["collateral_amount"])
        if full:
            coll_release = coll_amount
        else:
            release_frac = (amount / outstanding) if outstanding > 0 else ZERO
            coll_release = coll_amount * release_frac
        new_coll = coll_amount - coll_release
        if new_coll < 0:
            new_coll = ZERO
        # Wallet: release collateral chunk back to user.
        if coll_release > 0:
            ref_rel = f"{ref}-coll-release"
            ok2, msg2 = _wallet_transfer(
                amount=coll_release,
                asset=row["collateral_asset"],
                from_user=VAULT_OPEX_USER,
                to_user=opex_user,
                ref=ref_rel,
                description=f"zkcex-lending collateral release {row['collateral_asset']}",
                category="WITHDRAW_REQUEST",
            )
            if not ok2:
                log(f"collateral release pos={position_id} wallet fail: {msg2}")
                # Position is repaid but collateral release failed — surface
                # the failure but keep the repay applied so the user's debt
                # is reduced. Ops can replay the release.
        with _db_lock, db() as conn:
            new_status = "REPAID" if full else "OPEN"
            conn.execute(
                "UPDATE borrow_positions SET borrowed_principal=?, "
                "borrowed_interest=?, collateral_amount=?, "
                "last_accrual_at=?, status=?, repaid_at=? "
                "WHERE id=?",
                (
                    dstr(new_principal),
                    dstr(new_interest),
                    dstr(new_coll),
                    ts,
                    new_status,
                    ts if full else row["repaid_at"],
                    position_id,
                ),
            )
            _update_pool_aggregates(
                conn,
                row["borrowed_asset"],
                delta_borrowed=-(take_principal + take_interest),
            )
            _record_event(
                conn,
                opex_user=opex_user,
                event="repay",
                asset=row["borrowed_asset"],
                amount=amount,
                metadata={
                    "position_id": position_id,
                    "took_principal": dstr(take_principal),
                    "took_interest": dstr(take_interest),
                    "collateral_released": dstr(coll_release),
                    "closed": full,
                    "ref": ref,
                },
            )
            position = conn.execute(
                "SELECT * FROM borrow_positions WHERE id=?", (position_id,)
            ).fetchone()
            new_pool = conn.execute(
                "SELECT * FROM lending_pools WHERE asset=?",
                (row["borrowed_asset"],),
            ).fetchone()
        return {
            "ok": True,
            "repaid": dstr(amount),
            "collateral_released": dstr(coll_release),
            "closed": full,
            "position": serialise_borrow(position),
            "pool": serialise_pool(new_pool),
        }
    finally:
        lk.release()


def action_add_collateral(opex_user: str, position_id: int, amount: Decimal) -> dict:
    if amount <= 0:
        return {"ok": False, "error": "amount must be > 0"}
    lk = _pos_lock(position_id)
    if not lk.acquire(timeout=5):
        return {"ok": False, "error": "position_locked"}
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT * FROM borrow_positions WHERE id=? AND opex_user=?",
                (position_id, opex_user),
            ).fetchone()
        if not row:
            return {"ok": False, "error": "not_found"}
        if row["status"] != "OPEN":
            return {"ok": False, "error": f"status={row['status']}"}
        ts = now_s()
        ref = f"addcoll-{position_id}-{ts}"
        ok, msg = _wallet_transfer(
            amount=amount,
            asset=row["collateral_asset"],
            from_user=opex_user,
            to_user=VAULT_OPEX_USER,
            ref=ref,
            description=f"zkcex-lending add-collateral {row['collateral_asset']}",
            category="WITHDRAW_REQUEST",
        )
        if not ok:
            return {"ok": False, "error": "wallet_debit_failed", "detail": msg}
        new_coll = D(row["collateral_amount"]) + amount
        outstanding = D(row["borrowed_principal"]) + D(row["borrowed_interest"])
        new_ltv = _current_ltv_bps(
            row["borrowed_asset"], outstanding, row["collateral_asset"], new_coll
        )
        with _db_lock, db() as conn:
            conn.execute(
                "UPDATE borrow_positions SET collateral_amount=?, "
                "current_ltv_bps=?, last_accrual_at=? WHERE id=?",
                (dstr(new_coll), new_ltv, ts, position_id),
            )
            _record_event(
                conn,
                opex_user=opex_user,
                event="add_collateral",
                asset=row["collateral_asset"],
                amount=amount,
                metadata={"position_id": position_id, "new_ltv_bps": new_ltv, "ref": ref},
            )
            position = conn.execute(
                "SELECT * FROM borrow_positions WHERE id=?", (position_id,)
            ).fetchone()
        return {
            "ok": True,
            "added": dstr(amount),
            "new_ltv_bps": new_ltv,
            "position": serialise_borrow(position),
        }
    finally:
        lk.release()


# --- Serialisation ---------------------------------------------------------
def serialise_supply(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "opex_user": row["opex_user"],
        "asset": row["asset"],
        "principal": row["principal"],
        "accrued_interest": row["accrued_interest"],
        "total": dstr(D(row["principal"]) + D(row["accrued_interest"])),
        "supplied_at": int(row["supplied_at"]),
        "last_accrual_at": int(row["last_accrual_at"]),
        "withdrawn_at": int(row["withdrawn_at"]) if row["withdrawn_at"] else None,
        "status": row["status"],
    }


def serialise_borrow(row: sqlite3.Row) -> dict:
    outstanding = D(row["borrowed_principal"]) + D(row["borrowed_interest"])
    return {
        "id": int(row["id"]),
        "opex_user": row["opex_user"],
        "borrowed_asset": row["borrowed_asset"],
        "borrowed_principal": row["borrowed_principal"],
        "borrowed_interest": row["borrowed_interest"],
        "outstanding": dstr(outstanding),
        "collateral_asset": row["collateral_asset"],
        "collateral_amount": row["collateral_amount"],
        "borrowed_at": int(row["borrowed_at"]),
        "last_accrual_at": int(row["last_accrual_at"]),
        "repaid_at": int(row["repaid_at"]) if row["repaid_at"] else None,
        "liquidated_at": int(row["liquidated_at"]) if row["liquidated_at"] else None,
        "status": row["status"],
        "ltv_at_open_bps": int(row["ltv_at_open_bps"]),
        "current_ltv_bps": int(row["current_ltv_bps"])
        if row["current_ltv_bps"] is not None
        else None,
    }


# --- HTTP server ----------------------------------------------------------
def _client_is_loopback(handler: http.server.BaseHTTPRequestHandler) -> bool:
    ip = handler.client_address[0] if handler.client_address else ""
    return ip in LOOPBACK_HOSTS or ip.startswith("127.")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "zkcex-lending/1.0"

    def log_message(self, fmt, *args):  # noqa: A003
        sys.stderr.write(f"[lending] {self.address_string()} - {fmt % args}\n")

    # ---- low-level helpers ---------------------------------------------
    def _send_json(self, status: int, payload: Any) -> None:
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
        except Exception:  # noqa: BLE001
            return {}

    def _bearer(self) -> str | None:
        h = self.headers.get("Authorization") or ""
        if h.lower().startswith("bearer "):
            return h[7:].strip()
        return None

    def _require_user(self) -> dict | None:
        token = self._bearer()
        user = resolve_user_from_token(token)
        if not user:
            self._send_json(401, {"error": "unauthorized", "message": "Bearer token required"})
            return None
        return user

    def _require_admin(self) -> bool:
        tok = self._bearer()
        if tok and tok == get_admin_token():
            return True
        if _client_is_loopback(self):
            return True
        self._send_json(
            401, {"error": "unauthorized", "message": "Bearer LENDING_ADMIN_TOKEN required"}
        )
        return False

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-Opex-User",
        )
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    # ---- routing -------------------------------------------------------
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        try:
            if path == "/lending/health":
                return self._send_json(
                    200,
                    {
                        "ok": True,
                        "uptime_s": now_s() - START_TS,
                        "last_accrual_at": _runtime_state["last_accrual_at"],
                        "n_accrual_runs": _runtime_state["n_accrual_runs"],
                        "n_liquidations": _runtime_state["n_liquidations"],
                        "last_error": _runtime_state["last_error"],
                    },
                )
            if path == "/lending/pools":
                return self.h_list_pools()
            if path.startswith("/lending/pools/"):
                asset = path[len("/lending/pools/") :]
                return self.h_pool_detail(asset)
            if path == "/lending/my-positions":
                return self.h_my_positions()
            if path == "/lending/admin/health":
                return self.h_admin_health()
            return self._send_json(404, {"error": "not_found", "path": path})
        except Exception as e:  # noqa: BLE001
            log(f"GET {path} crash: {e!r}")
            return self._send_json(500, {"error": "internal_error", "message": str(e)})

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        try:
            if path == "/lending/supply":
                return self.h_supply()
            if path == "/lending/withdraw-supply":
                return self.h_withdraw_supply()
            if path == "/lending/borrow":
                return self.h_borrow()
            if path == "/lending/repay":
                return self.h_repay()
            if path == "/lending/add-collateral":
                return self.h_add_collateral()
            if path.startswith("/lending/internal/liquidate/"):
                if not _client_is_loopback(self) and not self._require_admin():
                    return
                pid_s = path[len("/lending/internal/liquidate/") :]
                try:
                    pid = int(pid_s)
                except ValueError:
                    return self._send_json(400, {"error": "bad_id"})
                res = _liquidate_position(pid, reason="manual")
                return self._send_json(200 if res.get("ok") else 400, res)
            if path == "/lending/admin/tick":
                if not self._require_admin():
                    return
                return self._send_json(200, _engine_tick())
            return self._send_json(404, {"error": "not_found", "path": path})
        except Exception as e:  # noqa: BLE001
            log(f"POST {path} crash: {e!r}")
            return self._send_json(500, {"error": "internal_error", "message": str(e)})

    # ---- public handlers ----------------------------------------------
    def h_list_pools(self) -> None:
        with db() as conn:
            rows = conn.execute("SELECT * FROM lending_pools ORDER BY asset").fetchall()
        out = [serialise_pool(r) for r in rows]
        # Top-line totals (USDT-eq).
        total_supplied_usdt = sum(
            (usdt_value(r["asset"], D(r["total_supplied"])) for r in rows),
            start=ZERO,
        )
        total_borrowed_usdt = sum(
            (usdt_value(r["asset"], D(r["total_borrowed"])) for r in rows),
            start=ZERO,
        )
        self._send_json(
            200,
            {
                "pools": out,
                "totals_usdt": {
                    "supplied": dstr(total_supplied_usdt),
                    "borrowed": dstr(total_borrowed_usdt),
                    "tvl": dstr(total_supplied_usdt - total_borrowed_usdt),
                },
            },
        )

    def h_pool_detail(self, asset: str) -> None:
        asset = asset.upper()
        with db() as conn:
            row = conn.execute("SELECT * FROM lending_pools WHERE asset=?", (asset,)).fetchone()
            if not row:
                return self._send_json(404, {"error": "pool_not_found", "asset": asset})
            n_supply = conn.execute(
                "SELECT COUNT(*) c FROM supply_positions " "WHERE asset=? AND status='OPEN'",
                (asset,),
            ).fetchone()["c"]
            n_borrow = conn.execute(
                "SELECT COUNT(*) c FROM borrow_positions "
                "WHERE borrowed_asset=? AND status='OPEN'",
                (asset,),
            ).fetchone()["c"]
        pool = serialise_pool(row)
        pool["n_open_supply_positions"] = int(n_supply)
        pool["n_open_borrow_positions"] = int(n_borrow)
        self._send_json(200, pool)

    # ---- user handlers -------------------------------------------------
    def h_supply(self) -> None:
        user = self._require_user()
        if not user:
            return
        body = self._read_json()
        asset = str(body.get("asset") or "").upper()
        if not asset:
            return self._send_json(400, {"error": "asset is required"})
        try:
            amount = D(body.get("amount"))
        except Exception:  # noqa: BLE001
            return self._send_json(400, {"error": "invalid amount"})
        if amount <= 0:
            return self._send_json(400, {"error": "amount must be > 0"})
        res = action_supply(user["opex_user"], asset, amount)
        return self._send_json(200 if res.get("ok") else 400, res)

    def h_withdraw_supply(self) -> None:
        user = self._require_user()
        if not user:
            return
        body = self._read_json()
        pid = body.get("position_id")
        try:
            pid = int(pid)
        except Exception:  # noqa: BLE001
            return self._send_json(400, {"error": "position_id required"})
        amount = body.get("amount")
        amt = D(amount) if amount not in (None, "") else None
        res = action_withdraw_supply(user["opex_user"], pid, amt)
        return self._send_json(200 if res.get("ok") else 400, res)

    def h_borrow(self) -> None:
        user = self._require_user()
        if not user:
            return
        body = self._read_json()
        b_asset = str(body.get("borrowed_asset") or "").upper()
        c_asset = str(body.get("collateral_asset") or "").upper()
        try:
            b_amt = D(body.get("borrow_amount"))
            c_amt = D(body.get("collateral_amount"))
        except Exception:  # noqa: BLE001
            return self._send_json(400, {"error": "invalid amounts"})
        if not b_asset or not c_asset:
            return self._send_json(
                400,
                {"error": "borrowed_asset and collateral_asset required"},
            )
        if b_amt <= 0 or c_amt <= 0:
            return self._send_json(400, {"error": "amounts must be > 0"})
        kyc = user.get("kyc_status") or "none"
        res = action_borrow(user["opex_user"], kyc, b_asset, b_amt, c_asset, c_amt)
        return self._send_json(200 if res.get("ok") else 400, res)

    def h_repay(self) -> None:
        user = self._require_user()
        if not user:
            return
        body = self._read_json()
        try:
            pid = int(body.get("position_id"))
        except Exception:  # noqa: BLE001
            return self._send_json(400, {"error": "position_id required"})
        amount = body.get("amount")
        amt = D(amount) if amount not in (None, "") else None
        res = action_repay(user["opex_user"], pid, amt)
        return self._send_json(200 if res.get("ok") else 400, res)

    def h_add_collateral(self) -> None:
        user = self._require_user()
        if not user:
            return
        body = self._read_json()
        try:
            pid = int(body.get("position_id"))
        except Exception:  # noqa: BLE001
            return self._send_json(400, {"error": "position_id required"})
        try:
            amt = D(body.get("amount"))
        except Exception:  # noqa: BLE001
            return self._send_json(400, {"error": "invalid amount"})
        if amt <= 0:
            return self._send_json(400, {"error": "amount must be > 0"})
        res = action_add_collateral(user["opex_user"], pid, amt)
        return self._send_json(200 if res.get("ok") else 400, res)

    def h_my_positions(self) -> None:
        user = self._require_user()
        if not user:
            return
        opex = user["opex_user"]
        with db() as conn:
            sup_rows = conn.execute(
                "SELECT * FROM supply_positions WHERE opex_user=? "
                "AND status='OPEN' ORDER BY id ASC",
                (opex,),
            ).fetchall()
            bor_rows = conn.execute(
                "SELECT * FROM borrow_positions WHERE opex_user=? "
                "AND status='OPEN' ORDER BY id ASC",
                (opex,),
            ).fetchall()
            recent = conn.execute(
                "SELECT * FROM lending_history WHERE opex_user=? " "ORDER BY id DESC LIMIT 50",
                (opex,),
            ).fetchall()
        # Enrich borrow rows with liquidation_distance and liq price.
        pools_by_asset: dict[str, sqlite3.Row] = {}
        with db() as conn:
            for asset in {r["borrowed_asset"] for r in bor_rows} | {
                r["collateral_asset"] for r in bor_rows
            }:
                p = conn.execute(
                    "SELECT * FROM lending_pools WHERE asset=?",
                    (asset,),
                ).fetchone()
                if p:
                    pools_by_asset[asset] = p
        bor_out = []
        debt_total_usdt = ZERO
        for r in bor_rows:
            d = serialise_borrow(r)
            outstanding = D(r["borrowed_principal"]) + D(r["borrowed_interest"])
            debt_total_usdt += usdt_value(r["borrowed_asset"], outstanding)
            pool = pools_by_asset.get(r["borrowed_asset"])
            if pool:
                d["liquidation_price_usdt"] = _liquidation_price_usdt(r, pool)
                liq_t = int(pool["liquidation_threshold_bps"])
                cur = int(r["current_ltv_bps"]) if r["current_ltv_bps"] else 0
                d["liquidation_threshold_bps"] = liq_t
                d["distance_to_liq_bps"] = max(0, liq_t - cur)
                if liq_t > 0:
                    d["distance_to_liq_pct"] = float(
                        Decimal(liq_t - cur) / Decimal(liq_t) * Decimal("100")
                    )
                else:
                    d["distance_to_liq_pct"] = 0.0
                # Health colour band consumed by the UI bar.
                pct = max(0.0, d["distance_to_liq_pct"])
                d["health_band"] = "green" if pct >= 20 else ("amber" if pct >= 10 else "red")
            bor_out.append(d)
        sup_out = [serialise_supply(r) for r in sup_rows]
        sup_total_usdt = sum(
            (
                usdt_value(r["asset"], D(r["principal"]) + D(r["accrued_interest"]))
                for r in sup_rows
            ),
            start=ZERO,
        )
        # Tier cap for the borrow side.
        kyc = user.get("kyc_status") or "none"
        tier_cap = BORROW_LIMITS_USDT.get(kyc, ZERO)
        self._send_json(
            200,
            {
                "supply_positions": sup_out,
                "borrow_positions": bor_out,
                "totals_usdt": {
                    "supplied": dstr(sup_total_usdt),
                    "borrowed": dstr(debt_total_usdt),
                },
                "borrow_limit": {
                    "kyc_status": kyc,
                    "tier_cap_usdt": dstr(tier_cap),
                    "available_usdt": dstr(max(ZERO, tier_cap - debt_total_usdt)),
                },
                "history": [
                    {
                        "id": int(h["id"]),
                        "ts": int(h["ts"]),
                        "event": h["event"],
                        "asset": h["asset"],
                        "amount": h["amount"],
                        "metadata": (
                            json.loads(h["metadata_json"]) if h["metadata_json"] else None
                        ),
                    }
                    for h in recent
                ],
            },
        )

    # ---- admin --------------------------------------------------------
    def h_admin_health(self) -> None:
        if not self._require_admin():
            return
        with db() as conn:
            rows = conn.execute("SELECT * FROM lending_pools ORDER BY asset").fetchall()
            per_asset = []
            for r in rows:
                n_supply = conn.execute(
                    "SELECT COUNT(*) c FROM supply_positions " "WHERE asset=? AND status='OPEN'",
                    (r["asset"],),
                ).fetchone()["c"]
                n_borrow = conn.execute(
                    "SELECT COUNT(*) c FROM borrow_positions "
                    "WHERE borrowed_asset=? AND status='OPEN'",
                    (r["asset"],),
                ).fetchone()["c"]
                # At-risk = LTV within 500bps (5%) of threshold OR over.
                lt = int(r["liquidation_threshold_bps"])
                at_risk = conn.execute(
                    "SELECT COUNT(*) c FROM borrow_positions "
                    "WHERE borrowed_asset=? AND status='OPEN' "
                    "AND current_ltv_bps IS NOT NULL "
                    "AND current_ltv_bps >= ?",
                    (r["asset"], max(0, lt - 500)),
                ).fetchone()["c"]
                tvl_usdt = usdt_value(r["asset"], D(r["total_supplied"]))
                borrowed_usdt = usdt_value(r["asset"], D(r["total_borrowed"]))
                per_asset.append(
                    {
                        "asset": r["asset"],
                        "total_supplied": r["total_supplied"],
                        "total_borrowed": r["total_borrowed"],
                        "reserves": r["reserves"],
                        "utilization_bps": int(r["utilization_bps"]),
                        "supply_apy_bps": int(r["supply_apy_bps"]),
                        "borrow_apy_bps": int(r["borrow_apy_bps"]),
                        "n_open_supply_positions": int(n_supply),
                        "n_open_borrow_positions": int(n_borrow),
                        "n_at_risk_positions": int(at_risk),
                        "tvl_usdt": dstr(tvl_usdt),
                        "borrowed_usdt": dstr(borrowed_usdt),
                    }
                )
        self._send_json(
            200,
            {
                "ok": True,
                "vault_user": VAULT_OPEX_USER,
                "now": now_s(),
                "uptime_s": now_s() - START_TS,
                "last_accrual_at": _runtime_state["last_accrual_at"],
                "last_accrual_ms": _runtime_state["last_accrual_ms"],
                "n_accrual_runs": _runtime_state["n_accrual_runs"],
                "n_liquidations": _runtime_state["n_liquidations"],
                "last_error": _runtime_state["last_error"],
                "pools": per_asset,
            },
        )


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    init_db()
    get_admin_token()  # surface a random token on first boot if env unset
    t = threading.Thread(target=_engine_loop, daemon=True, name="lending-engine")
    t.start()
    with ThreadingServer(("", port), Handler) as srv:
        log(f"lending engine listening on :{port}")
        log(f"  db -> {DB_PATH}")
        log(f"  accrual every {ACCRUAL_INTERVAL_S}s, vault user = {VAULT_OPEX_USER}")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
