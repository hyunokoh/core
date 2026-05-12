#!/usr/bin/env python3
"""Token staking product for zkCEX (port 5692).

Locks user assets for a term and accrues yield at a fixed APY. The same idea
as Binance Earn / Coinbase Earn / Kraken Staking. Two flavours:

  * Flexible       — withdraw anytime, lower APY (e.g. 3.00% for ETH).
  * Fixed-term     — 30/90/180 days, higher APY, early-unstake penalty.

CAVEAT — accounting only
------------------------
This service does not run validators or DeFi positions. Yield is *minted*
into the user's wallet at redemption time, and the matching protocol fee
is credited to a ``zkcex-staking-revenue`` synthetic wallet. In production
the yield would be sourced from staking real validators (e.g. Lido for ETH),
lending to market makers, or running on-chain LP positions — here the books
just record the obligation. The principal is moved out of the user's MAIN
wallet into a ``zkcex-staking_MAIN`` synthetic wallet on stake, and moved
back on redeem.

Stdlib only. Persisted at ``tools/.local/staking.db``.
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
import uuid
from decimal import Decimal, InvalidOperation, getcontext
from typing import Any

getcontext().prec = 36

# --- Paths ----------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join(HERE, ".local")
os.makedirs(LOCAL_DIR, exist_ok=True)
DB_PATH = os.path.join(LOCAL_DIR, "staking.db")


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
    "STAKING_AUTH_BASE", os.environ.get("STAKING_AUTH_BASE", "http://127.0.0.1:5501")
)
WALLET_BASE = _validated_http_base_url(
    "STAKING_WALLET_BASE", os.environ.get("STAKING_WALLET_BASE", "http://127.0.0.1:8091")
)
ACCRUAL_INTERVAL_S = int(os.environ.get("STAKING_ACCRUAL_SECONDS", "3600"))
PROTOCOL_FEE_BPS = int(os.environ.get("STAKING_PROTOCOL_FEE_BPS", "2000"))  # 20% of yield
AUTH_TTL = 30.0
STAKING_HOLDING_WALLET = "zkcex-staking"
REVENUE_WALLET = "zkcex-staking-revenue"
# Internal symbol normalisation: the wallet API uses bare symbols (ETH/USDT/BTC)
# without the "Z" prefix used by chain_server's hardhat path.
ASSET_TO_INTERNAL_SYMBOL = {
    "ETH": "ETH",
    "ZETH": "ETH",
    "USDT": "USDT",
    "ZUSDT": "USDT",
    "BTC": "BTC",
}
SECONDS_PER_DAY = 86400

START_TS = int(time.time())

_db_lock = threading.Lock()


def log(msg: str) -> None:
    sys.stderr.write(f"[staking] {msg}\n")
    sys.stderr.flush()


# ==========================================================================
# DB
# ==========================================================================
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS staking_products (
  product_id TEXT PRIMARY KEY,
  asset TEXT NOT NULL,
  term_days INTEGER NOT NULL,
  apy_bps INTEGER NOT NULL,
  min_stake TEXT NOT NULL,
  max_stake_per_user TEXT,
  total_capacity TEXT,
  total_staked TEXT NOT NULL DEFAULT '0',
  active INTEGER NOT NULL DEFAULT 1,
  early_unstake_penalty_bps INTEGER NOT NULL DEFAULT 0,
  description TEXT,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS staking_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  opex_user TEXT NOT NULL,
  product_id TEXT NOT NULL,
  asset TEXT NOT NULL,
  amount TEXT NOT NULL,
  accrued_yield TEXT NOT NULL DEFAULT '0',
  apy_bps_at_stake INTEGER NOT NULL,
  staked_at INTEGER NOT NULL,
  matures_at INTEGER NOT NULL,
  unstaked_at INTEGER,
  unstake_reason TEXT,
  redeemed_at INTEGER,
  redeemed_amount TEXT,
  status TEXT NOT NULL,
  FOREIGN KEY (product_id) REFERENCES staking_products(product_id)
);
CREATE INDEX IF NOT EXISTS idx_staking_positions_user
  ON staking_positions(opex_user, status);
CREATE INDEX IF NOT EXISTS idx_staking_positions_status
  ON staking_positions(status, matures_at);
CREATE TABLE IF NOT EXISTS staking_rewards (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER NOT NULL,
  accrual_day INTEGER NOT NULL,
  asset TEXT NOT NULL,
  amount TEXT NOT NULL,
  apy_bps_used INTEGER NOT NULL,
  posted_at INTEGER NOT NULL,
  FOREIGN KEY (position_id) REFERENCES staking_positions(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_staking_rewards_unique
  ON staking_rewards(position_id, accrual_day);
CREATE TABLE IF NOT EXISTS staking_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  opex_user TEXT NOT NULL,
  event TEXT NOT NULL,
  position_id INTEGER,
  asset TEXT NOT NULL,
  amount TEXT NOT NULL,
  metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_staking_history_user
  ON staking_history(opex_user, ts DESC);
"""

DEFAULT_PRODUCTS = [
    # product_id, asset, term, apy_bps, min, max_user, capacity, penalty_bps, desc
    ("ETH-FLEX", "ETH", 0, 300, "0.01", None, "10000", 0, "ETH Flexible — withdraw anytime"),
    ("ETH-30D", "ETH", 30, 450, "0.05", "500", "5000", 1500, "ETH 30-day lock-up"),
    ("ETH-90D", "ETH", 90, 650, "0.1", "1000", "3000", 2500, "ETH 90-day lock-up"),
    ("ETH-180D", "ETH", 180, 850, "0.5", "2000", "1500", 3500, "ETH 180-day lock-up"),
    ("USDT-FLEX", "USDT", 0, 200, "10", None, "1000000", 0, "USDT Flexible"),
    ("USDT-30D", "USDT", 30, 350, "100", "100000", "500000", 1500, "USDT 30-day"),
    ("USDT-90D", "USDT", 90, 500, "500", "200000", "300000", 2500, "USDT 90-day"),
    ("BTC-FLEX", "BTC", 0, 250, "0.001", None, "100", 0, "BTC Flexible"),
    ("BTC-90D", "BTC", 90, 500, "0.01", "10", "50", 2500, "BTC 90-day"),
]


def init_db() -> None:
    with _db_lock, db() as conn:
        conn.executescript(SCHEMA)
        now = int(time.time())
        for pid, asset, term, apy, min_stake, max_user, cap, pen, desc in DEFAULT_PRODUCTS:
            conn.execute(
                "INSERT OR IGNORE INTO staking_products "
                "(product_id, asset, term_days, apy_bps, min_stake, max_stake_per_user, "
                " total_capacity, total_staked, active, early_unstake_penalty_bps, "
                " description, created_at) "
                "VALUES (?,?,?,?,?,?,?, '0', 1, ?, ?, ?)",
                (pid, asset, term, apy, min_stake, max_user, cap, pen, desc, now),
            )


# ==========================================================================
# Decimal helpers
# ==========================================================================
def D(v) -> Decimal:
    if v is None:
        return Decimal(0)
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def dstr(v) -> str:
    """Canonical short decimal string (trims trailing zeros)."""
    if not isinstance(v, Decimal):
        v = D(v)
    if v == 0:
        return "0"
    # quantize to 8 dp for display; keep precision for storage above this.
    q = v.quantize(Decimal("0.00000001")) if abs(v) >= Decimal("0.00000001") else v
    s = format(q, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


# ==========================================================================
# Bearer-token resolver (cached against auth_server /auth/me)
# ==========================================================================
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


# ==========================================================================
# Admin token
# ==========================================================================
def get_admin_token() -> str:
    tok = os.environ.get("STAKING_ADMIN_TOKEN")
    if tok:
        return tok
    cached = _runtime_state.get("admin_token")
    if cached:
        return str(cached)
    tok = "staking_" + secrets.token_urlsafe(24)
    _runtime_state["admin_token"] = tok
    log(f"STAKING_ADMIN_TOKEN={tok}  (set this in env to make it persistent)")
    return tok


_runtime_state: dict[str, Any] = {
    "last_accrual_at": 0,
    "last_accrual_ok": True,
    "last_accrual_error": None,
}


# ==========================================================================
# Wallet integration (best-effort against the demo wallet API)
# ==========================================================================
def wallet_get_balance(opex_user: str, internal_symbol: str) -> Decimal | None:
    """Look up the user's MAIN-wallet free balance for ``internal_symbol``.
    Returns None on transport failure (caller decides whether to surface as
    503 or proceed)."""
    try:
        with _http_urlopen(
            f"{WALLET_BASE}/v1/owner/{urllib.parse.quote(opex_user)}/wallets",
            timeout=5,
        ) as resp:
            wallets = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        log(f"wallet lookup failed for {opex_user}: {e!r}")
        return None
    rows = wallets if isinstance(wallets, list) else wallets.get("wallets", [])
    total = Decimal(0)
    for w in rows:
        sym = w.get("currency") or w.get("symbol") or w.get("asset")
        if not sym or str(sym).upper() != internal_symbol:
            continue
        try:
            bal = Decimal(str(w.get("balance", 0)))
            lck = Decimal(str(w.get("locked", 0)))
        except Exception as e:  # noqa: BLE001
            log(f"wallet balance row skipped for {opex_user}: {e!r}")
            continue
        total += bal - lck
    return total


def wallet_transfer(
    *,
    amount: Decimal,
    internal_symbol: str,
    from_owner: str,
    to_owner: str,
    description: str,
    ref: str,
) -> tuple[bool, str]:
    """Move tokens between two wallet owners via ``/v2/transfer``.

    The wallet API's ``{amount}`` path variable is bound to a Spring BigDecimal
    so fractional amounts like ``0.5`` pass through unchanged.
    """
    if amount <= 0:
        return False, "amount must be positive"
    amount_path = urllib.parse.quote(dstr(amount), safe="")
    url = (
        f"{WALLET_BASE}/v2/transfer/{amount_path}_{internal_symbol}"
        f"/from/{urllib.parse.quote(from_owner)}_MAIN"
        f"/to/{urllib.parse.quote(to_owner)}_MAIN"
    )
    body = json.dumps(
        {
            "description": description,
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
    except Exception as e:  # noqa: BLE001
        return False, f"wallet transport: {e!r}"
    return True, "ok"


def wallet_mint(
    *,
    amount: Decimal,
    internal_symbol: str,
    to_owner: str,
    description: str,
    ref: str,
) -> tuple[bool, str]:
    """Demo-only: mint fresh ``internal_symbol`` into ``to_owner_MAIN`` via the
    test-deposit endpoint. Used to materialise yield (production would source
    from validators / DeFi). The endpoint caps each call at 10 units and only
    accepts INTEGER amounts, so fractional yields use a /v2/transfer top-up
    from a pre-funded source instead.

    Strategy:
      1. Split ``amount`` into integer-units + fractional remainder.
      2. Mint integer-units in 10-unit chunks via ``/deposit/...``.
      3. If a fractional remainder exists, mint 1 extra unit and immediately
         transfer the excess (1 - remainder) back to the staking wallet to net
         the correct fractional crediton ``to_owner_MAIN``.

    Returns (ok, message). On partial failure we leave a clear log trail and
    return the message — the caller decides whether the ledger entry was
    already idempotently recorded.
    """
    if amount <= 0:
        return True, "noop"  # nothing to mint
    # Cap-aware chunking; the deposit endpoint accepts integer units only.
    integer_units = int(amount)  # floor
    fractional = amount - Decimal(integer_units)
    # Mint integer units in chunks of 10.
    remaining = integer_units
    chunk_idx = 0
    while remaining > 0:
        chunk = min(10, remaining)
        path = (
            f"/deposit/{chunk}_test-ethereum_{internal_symbol}/"
            f"{urllib.parse.quote(to_owner)}_MAIN"
            f"?description={urllib.parse.quote(description, safe='')}"
            f"&transferRef={urllib.parse.quote(ref + '-' + str(chunk_idx))}"
        )
        try:
            req = _http_request(f"{WALLET_BASE}{path}", method="POST")
            with _http_urlopen(req, timeout=10) as resp:
                if resp.status >= 300:
                    return False, f"wallet HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            return False, f"wallet HTTP {e.code}"
        except Exception as e:  # noqa: BLE001
            return False, f"wallet transport: {e!r}"
        remaining -= chunk
        chunk_idx += 1
    # Handle fractional remainder by minting 1 unit + transferring back the
    # excess. We only do this when fractional > 0; otherwise we're done.
    if fractional > 0:
        path = (
            f"/deposit/1_test-ethereum_{internal_symbol}/"
            f"{urllib.parse.quote(to_owner)}_MAIN"
            f"?description={urllib.parse.quote(description + '-frac', safe='')}"
            f"&transferRef={urllib.parse.quote(ref + '-frac')}"
        )
        try:
            req = _http_request(f"{WALLET_BASE}{path}", method="POST")
            with _http_urlopen(req, timeout=10) as resp:
                if resp.status >= 300:
                    return False, f"wallet HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            return False, f"wallet HTTP {e.code}"
        except Exception as e:  # noqa: BLE001
            return False, f"wallet transport: {e!r}"
        excess = Decimal(1) - fractional
        ok, msg = wallet_transfer(
            amount=excess,
            internal_symbol=internal_symbol,
            from_owner=to_owner,
            to_owner=STAKING_HOLDING_WALLET,
            description=description + "-frac-rebalance",
            ref=ref + "-frac-back",
        )
        if not ok:
            # If we couldn't return the excess, we over-credited the user by
            # (1 - fractional). Log it so an operator can reconcile.
            log(
                f"WARN over-credit user={to_owner} excess={dstr(excess)} "
                f"{internal_symbol}: {msg}"
            )
            return False, f"fractional rebalance failed: {msg}"
    return True, "ok"


# ==========================================================================
# Reward accrual
# ==========================================================================
def _unix_day(ts: int) -> int:
    return ts // SECONDS_PER_DAY


def _accrual_step(ts: int) -> int:
    """In demo-fast mode (ACCRUAL_INTERVAL_S < 3600) we treat each accrual
    tick as one logical day, so the demo can observe yield in minutes.
    Returns the integer-day index to use as the rewards UNIQUE key.
    """
    if ACCRUAL_INTERVAL_S >= 3600:
        return _unix_day(ts)
    # Use seconds-since-START / ACCRUAL_INTERVAL_S so each tick produces a
    # fresh accrual_day. Anchor on START_TS so re-launching the service mid-
    # demo doesn't double-credit.
    return (ts - START_TS) // max(ACCRUAL_INTERVAL_S, 1)


def accrue_rewards_once() -> dict:
    """Accrue one day's interest to every active position. Idempotent on
    (position_id, accrual_day) via UNIQUE INDEX — running twice in the same
    accrual_step is a no-op."""
    now = int(time.time())
    day = _accrual_step(now)
    accrued_total: dict[str, Decimal] = {}  # asset -> sum yield this tick
    matured_count = 0
    accrued_rows = 0
    with _db_lock, db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT id, asset, amount, accrued_yield, apy_bps_at_stake, matures_at, status "
                "FROM staking_positions WHERE status='active'"
            ).fetchall()
            for r in rows:
                pid = int(r["id"])
                amount = D(r["amount"])
                apy_bps = int(r["apy_bps_at_stake"])
                daily = amount * Decimal(apy_bps) / Decimal(10000) / Decimal(365)
                if daily > 0:
                    try:
                        conn.execute(
                            "INSERT INTO staking_rewards "
                            "(position_id, accrual_day, asset, amount, apy_bps_used, posted_at) "
                            "VALUES (?,?,?,?,?,?)",
                            (pid, day, r["asset"], dstr(daily), apy_bps, now),
                        )
                    except sqlite3.IntegrityError:
                        # Already accrued for this day — skip silently.
                        continue
                    new_yield = D(r["accrued_yield"]) + daily
                    conn.execute(
                        "UPDATE staking_positions SET accrued_yield=? WHERE id=?",
                        (dstr(new_yield), pid),
                    )
                    accrued_total[r["asset"]] = accrued_total.get(r["asset"], Decimal(0)) + daily
                    accrued_rows += 1
                # Flip matured positions to 'matured_pending' so the user can
                # redeem. Flexible positions (matures_at == staked_at) stay
                # 'active' forever.
                if now >= int(r["matures_at"]) and int(r["matures_at"]) > 0:
                    row_check = conn.execute(
                        "SELECT staked_at FROM staking_positions WHERE id=?", (pid,)
                    ).fetchone()
                    if row_check and int(row_check["staked_at"]) != int(r["matures_at"]):
                        conn.execute(
                            "UPDATE staking_positions SET status='matured_pending' "
                            "WHERE id=? AND status='active'",
                            (pid,),
                        )
                        matured_count += 1
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {
        "accrual_day": day,
        "rows": accrued_rows,
        "matured": matured_count,
        "yield_by_asset": {k: dstr(v) for k, v in accrued_total.items()},
    }


def _accrual_loop():
    while True:
        try:
            res = accrue_rewards_once()
            _runtime_state["last_accrual_at"] = int(time.time())
            _runtime_state["last_accrual_ok"] = True
            _runtime_state["last_accrual_error"] = None
            if res["rows"] or res["matured"]:
                log(f"accrual: {res}")
        except Exception as e:  # noqa: BLE001
            _runtime_state["last_accrual_at"] = int(time.time())
            _runtime_state["last_accrual_ok"] = False
            _runtime_state["last_accrual_error"] = repr(e)
            log(f"accrual loop error: {e!r}")
        time.sleep(ACCRUAL_INTERVAL_S)


# ==========================================================================
# Stake / unstake / redeem core logic
# ==========================================================================
def _row_to_position_dict(row: sqlite3.Row, product: sqlite3.Row | None = None) -> dict:
    out = {
        "id": int(row["id"]),
        "opex_user": row["opex_user"],
        "product_id": row["product_id"],
        "asset": row["asset"],
        "amount": row["amount"],
        "accrued_yield": row["accrued_yield"],
        "apy_bps_at_stake": int(row["apy_bps_at_stake"]),
        "staked_at": int(row["staked_at"]),
        "matures_at": int(row["matures_at"]),
        "unstaked_at": int(row["unstaked_at"]) if row["unstaked_at"] is not None else None,
        "unstake_reason": row["unstake_reason"],
        "redeemed_at": int(row["redeemed_at"]) if row["redeemed_at"] is not None else None,
        "redeemed_amount": row["redeemed_amount"],
        "status": row["status"],
    }
    if product is not None:
        out["term_days"] = int(product["term_days"])
        out["early_unstake_penalty_bps"] = int(product["early_unstake_penalty_bps"])
        out["product_description"] = product["description"]
    return out


def _record_history(
    conn,
    *,
    ts: int,
    opex_user: str,
    event: str,
    position_id: int | None,
    asset: str,
    amount: Decimal,
    metadata: dict,
) -> None:
    conn.execute(
        "INSERT INTO staking_history (ts, opex_user, event, position_id, asset, amount, metadata_json) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            ts,
            opex_user,
            event,
            position_id,
            asset,
            dstr(amount),
            json.dumps(metadata, separators=(",", ":")),
        ),
    )


def do_stake(opex_user: str, product_id: str, amount: Decimal) -> tuple[int, dict]:
    """Validate + open a new position. Returns (http_status, payload)."""
    if amount <= 0:
        return 400, {"error": "bad_amount", "message": "amount must be > 0"}
    now = int(time.time())
    # Capacity check + reservation in a single transaction so simultaneous
    # stakes to the last few units can't both succeed.
    with _db_lock, db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            prod = conn.execute(
                "SELECT * FROM staking_products WHERE product_id=?",
                (product_id,),
            ).fetchone()
            if not prod or not int(prod["active"]):
                conn.execute("ROLLBACK")
                return 404, {"error": "product_not_found", "product_id": product_id}
            asset = prod["asset"]
            if amount < D(prod["min_stake"]):
                conn.execute("ROLLBACK")
                return 400, {
                    "error": "below_min_stake",
                    "min_stake": prod["min_stake"],
                    "asset": asset,
                }
            # Per-user cap check.
            if prod["max_stake_per_user"]:
                user_total = D(
                    conn.execute(
                        "SELECT COALESCE(SUM(CAST(amount AS REAL)),0) AS s "
                        "FROM staking_positions "
                        "WHERE opex_user=? AND product_id=? AND status IN ('active','matured_pending')",
                        (opex_user, product_id),
                    ).fetchone()["s"]
                )
                if user_total + amount > D(prod["max_stake_per_user"]):
                    conn.execute("ROLLBACK")
                    return 400, {
                        "error": "exceeds_user_cap",
                        "max_stake_per_user": prod["max_stake_per_user"],
                        "current_user_total": dstr(user_total),
                    }
            # Total capacity check.
            cur_total = D(prod["total_staked"])
            if prod["total_capacity"] is not None:
                cap = D(prod["total_capacity"])
                if cur_total + amount > cap:
                    conn.execute("ROLLBACK")
                    return 400, {
                        "error": "exceeds_capacity",
                        "total_capacity": prod["total_capacity"],
                        "total_staked": dstr(cur_total),
                        "available": dstr(cap - cur_total),
                    }
            # Reserve capacity atomically. We do this BEFORE the wallet debit
            # so two simultaneous stakers can't both win the same slot. If
            # the wallet debit later fails we roll back this update.
            new_total = cur_total + amount
            conn.execute(
                "UPDATE staking_products SET total_staked=? WHERE product_id=?",
                (dstr(new_total), product_id),
            )
            # Reserve a position row so the id is known up front. status starts
            # 'pending_debit' to make this atomic with the wallet API call; we
            # flip it to 'active' once the debit returns OK, and delete it on
            # failure.
            term = int(prod["term_days"])
            staked_at = now
            matures_at = staked_at + term * SECONDS_PER_DAY if term > 0 else staked_at
            cur = conn.execute(
                "INSERT INTO staking_positions "
                "(opex_user, product_id, asset, amount, apy_bps_at_stake, "
                " staked_at, matures_at, status) "
                "VALUES (?,?,?,?,?,?,?, 'pending_debit')",
                (
                    opex_user,
                    product_id,
                    asset,
                    dstr(amount),
                    int(prod["apy_bps"]),
                    staked_at,
                    matures_at,
                ),
            )
            position_id = int(cur.lastrowid)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    # --- Outside the DB lock: call wallet API to debit MAIN -> staking.
    internal_symbol = ASSET_TO_INTERNAL_SYMBOL.get(asset.upper(), asset.upper())
    avail = wallet_get_balance(opex_user, internal_symbol)
    if avail is not None and avail < amount:
        # Insufficient — unwind the reservation.
        with _db_lock, db() as conn:
            conn.execute("DELETE FROM staking_positions WHERE id=?", (position_id,))
            conn.execute(
                "UPDATE staking_products SET total_staked=? WHERE product_id=?",
                (dstr(cur_total), product_id),
            )
        return 400, {
            "error": "insufficient_balance",
            "asset": asset,
            "available": dstr(avail),
            "requested": dstr(amount),
        }
    ref = f"stake-{position_id}-{uuid.uuid4().hex[:8]}"
    ok, msg = wallet_transfer(
        amount=amount,
        internal_symbol=internal_symbol,
        from_owner=opex_user,
        to_owner=STAKING_HOLDING_WALLET,
        description=f"zkcex-stake-{product_id}",
        ref=ref,
    )
    if not ok:
        # Unwind: drop the reservation and restore total_staked.
        with _db_lock, db() as conn:
            conn.execute("DELETE FROM staking_positions WHERE id=?", (position_id,))
            conn.execute(
                "UPDATE staking_products SET total_staked=? WHERE product_id=?",
                (dstr(cur_total), product_id),
            )
        return 502, {"error": "wallet_debit_failed", "message": msg}
    # Flip status to 'active' + log history.
    with _db_lock, db() as conn:
        conn.execute(
            "UPDATE staking_positions SET status='active' WHERE id=?",
            (position_id,),
        )
        _record_history(
            conn,
            ts=now,
            opex_user=opex_user,
            event="stake",
            position_id=position_id,
            asset=asset,
            amount=amount,
            metadata={"product_id": product_id, "transferRef": ref},
        )
        row = conn.execute("SELECT * FROM staking_positions WHERE id=?", (position_id,)).fetchone()
        prod = conn.execute(
            "SELECT * FROM staking_products WHERE product_id=?", (product_id,)
        ).fetchone()
    return 200, {"position": _row_to_position_dict(row, prod), "transferRef": ref}


def do_unstake_or_redeem(
    opex_user: str, position_id: int, *, force_early: bool = False
) -> tuple[int, dict]:
    """Close a position. Routes to either unstake-with-penalty (active,
    pre-maturity for fixed-term) or redeem (matured_pending or flex).
    ``force_early=True`` allows early-exit on an 'active' fixed-term position.
    """
    now = int(time.time())
    with _db_lock, db() as conn:
        row = conn.execute(
            "SELECT * FROM staking_positions WHERE id=? AND opex_user=?",
            (position_id, opex_user),
        ).fetchone()
        if not row:
            return 404, {"error": "position_not_found"}
        if row["status"] not in ("active", "matured_pending"):
            return 409, {"error": "not_redeemable", "status": row["status"]}
        prod = conn.execute(
            "SELECT * FROM staking_products WHERE product_id=?",
            (row["product_id"],),
        ).fetchone()
    asset = row["asset"]
    amount = D(row["amount"])
    accrued = D(row["accrued_yield"])
    term = int(prod["term_days"]) if prod else 0
    matures_at = int(row["matures_at"])
    is_flex = term == 0
    is_matured = (not is_flex) and (row["status"] == "matured_pending" or now >= matures_at)
    early = (not is_flex) and (not is_matured)
    if early and not force_early:
        # Caller hit /redeem on a fixed-term position before maturity; tell them
        # to call /unstake (with the penalty) instead.
        return 409, {
            "error": "not_matured",
            "matures_at": matures_at,
            "seconds_to_maturity": max(0, matures_at - now),
            "message": "use /unstake to exit early with penalty",
        }
    # Compute penalty + protocol fee + net amounts.
    penalty_bps = int(prod["early_unstake_penalty_bps"]) if (prod and early) else 0
    penalty = (amount * Decimal(penalty_bps) / Decimal(10000)) if penalty_bps else Decimal(0)
    protocol_fee = accrued * Decimal(PROTOCOL_FEE_BPS) / Decimal(10000)
    net_yield = accrued - protocol_fee
    redeemed_amount = amount + net_yield - penalty
    if redeemed_amount < 0:
        # Shouldn't happen in practice (penalty is bounded by amount), but be
        # defensive — clamp to zero and log.
        log(f"WARN redeemed_amount<0 for pid={position_id}, clamping")
        redeemed_amount = Decimal(0)
    internal_symbol = ASSET_TO_INTERNAL_SYMBOL.get(asset.upper(), asset.upper())
    # ---- 1) move principal+net_yield principal portion back to user.
    # The cleanest is two separate flows:
    #   * /v2/transfer principal from staking_holding -> user (already in books)
    #   * mint net_yield to user (demo-grade; production would source elsewhere)
    #   * mint protocol_fee to revenue wallet
    # The penalty stays in the staking_holding wallet (acts as buffer).
    # Net principal back to user = amount - penalty.
    principal_back = amount - penalty
    ref_base = f"redeem-{position_id}-{uuid.uuid4().hex[:8]}"
    ok, msg = wallet_transfer(
        amount=principal_back,
        internal_symbol=internal_symbol,
        from_owner=STAKING_HOLDING_WALLET,
        to_owner=opex_user,
        description=f"zkcex-redeem-principal-{row['product_id']}",
        ref=ref_base + "-prin",
    )
    if not ok:
        return 502, {"error": "wallet_credit_failed", "stage": "principal", "message": msg}
    # ---- 2) credit net yield to user (mint).
    if net_yield > 0:
        ok2, msg2 = wallet_mint(
            amount=net_yield,
            internal_symbol=internal_symbol,
            to_owner=opex_user,
            description=f"zkcex-yield-{row['product_id']}",
            ref=ref_base + "-yield",
        )
        if not ok2:
            log(f"WARN yield mint failed pid={position_id}: {msg2}")
            # Continue anyway — we already moved principal. Position will be
            # marked redeemed with the principal portion only; the operator
            # can investigate via /staking/history.
    # ---- 3) credit protocol fee to revenue wallet (mint).
    if protocol_fee > 0:
        ok3, msg3 = wallet_mint(
            amount=protocol_fee,
            internal_symbol=internal_symbol,
            to_owner=REVENUE_WALLET,
            description=f"zkcex-staking-fee-{row['product_id']}",
            ref=ref_base + "-fee",
        )
        if not ok3:
            log(f"WARN protocol-fee mint failed pid={position_id}: {msg3}")
    # ---- 4) update DB.
    reason = "matured" if is_matured else ("early" if early else "flex")
    with _db_lock, db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE staking_positions SET "
                "status='redeemed', unstaked_at=?, unstake_reason=?, "
                "redeemed_at=?, redeemed_amount=? WHERE id=? AND opex_user=?",
                (now, reason, now, dstr(redeemed_amount), position_id, opex_user),
            )
            # Decrement product TVL.
            prod2 = conn.execute(
                "SELECT total_staked FROM staking_products WHERE product_id=?",
                (row["product_id"],),
            ).fetchone()
            new_total = max(Decimal(0), D(prod2["total_staked"]) - amount)
            conn.execute(
                "UPDATE staking_products SET total_staked=? WHERE product_id=?",
                (dstr(new_total), row["product_id"]),
            )
            _record_history(
                conn,
                ts=now,
                opex_user=opex_user,
                event="redeem" if is_matured or is_flex else "unstake",
                position_id=position_id,
                asset=asset,
                amount=redeemed_amount,
                metadata={
                    "reason": reason,
                    "principal": dstr(amount),
                    "accrued_yield": dstr(accrued),
                    "protocol_fee": dstr(protocol_fee),
                    "net_yield": dstr(net_yield),
                    "penalty": dstr(penalty),
                    "transferRef": ref_base,
                },
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        row2 = conn.execute("SELECT * FROM staking_positions WHERE id=?", (position_id,)).fetchone()
    return 200, {
        "position": _row_to_position_dict(row2, prod),
        "principal": dstr(amount),
        "accrued_yield": dstr(accrued),
        "protocol_fee": dstr(protocol_fee),
        "net_yield": dstr(net_yield),
        "penalty": dstr(penalty),
        "redeemed_amount": dstr(redeemed_amount),
        "reason": reason,
    }


# ==========================================================================
# HTTP server
# ==========================================================================
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "zkcex-staking/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[staking] {self.address_string()} - {fmt % args}\n")

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

    def _bearer(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        if not auth.lower().startswith("bearer "):
            return None
        return auth.split(None, 1)[1].strip()

    def _require_user(self) -> dict | None:
        # Also accept X-Opex-User for trusted-local callers (mirrors the
        # convention of the matching gateway).
        opex = self.headers.get("X-Opex-User")
        if opex:
            return {"opex_user": opex, "trusted_local": True}
        tok = self._bearer()
        if not tok:
            self._send_json(
                401,
                {"error": "unauthorized", "message": "Bearer token or X-Opex-User required"},
            )
            return None
        user = resolve_user_from_token(tok)
        if not user:
            self._send_json(401, {"error": "unauthorized", "message": "invalid bearer token"})
            return None
        return user

    def _require_admin(self) -> str | None:
        tok = self._bearer()
        if not tok or tok != get_admin_token():
            self._send_json(
                401,
                {"error": "unauthorized", "message": "Bearer STAKING_ADMIN_TOKEN required"},
            )
            return None
        return "admin"

    def _read_json_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n > 0 else b""
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Opex-User")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    # ---- Routing ------------------------------------------------------
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query or "")
        if path == "/staking/health":
            return self.h_health()
        if path == "/staking/products":
            return self.h_list_products()
        if path.startswith("/staking/products/") and path.count("/") == 3:
            pid = path.rsplit("/", 1)[1]
            return self.h_product_detail(pid)
        if path == "/staking/positions":
            return self.h_my_positions(q)
        if path == "/staking/rewards":
            return self.h_my_rewards(q)
        if path == "/staking/history":
            return self.h_my_history(q)
        if path == "/staking/admin/tvl":
            return self.h_admin_tvl()
        return self._send_json(404, {"error": "not_found", "path": path})

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/staking/stake":
            return self.h_stake()
        # /staking/positions/<id>/unstake
        if path.startswith("/staking/positions/") and path.endswith("/unstake"):
            try:
                pid = int(path[len("/staking/positions/") : -len("/unstake")])
            except ValueError:
                return self._send_json(400, {"error": "bad_position_id"})
            return self.h_unstake(pid)
        if path.startswith("/staking/positions/") and path.endswith("/redeem"):
            try:
                pid = int(path[len("/staking/positions/") : -len("/redeem")])
            except ValueError:
                return self._send_json(400, {"error": "bad_position_id"})
            return self.h_redeem(pid)
        if path == "/staking/admin/accrue":
            return self.h_admin_accrue()
        return self._send_json(404, {"error": "not_found", "path": path})

    # ---- Handlers (public) -------------------------------------------
    def h_health(self):
        return self._send_json(
            200,
            {
                "ok": True,
                "uptime_s": int(time.time()) - START_TS,
                "last_accrual_at": _runtime_state.get("last_accrual_at"),
                "last_accrual_ok": _runtime_state.get("last_accrual_ok"),
                "last_accrual_error": _runtime_state.get("last_accrual_error"),
                "accrual_interval_s": ACCRUAL_INTERVAL_S,
                "protocol_fee_bps": PROTOCOL_FEE_BPS,
            },
        )

    def h_list_products(self):
        with db() as conn:
            rows = conn.execute(
                "SELECT * FROM staking_products WHERE active=1 " "ORDER BY asset ASC, term_days ASC"
            ).fetchall()
        out = []
        for r in rows:
            total_cap = D(r["total_capacity"]) if r["total_capacity"] is not None else None
            staked = D(r["total_staked"])
            out.append(
                {
                    "product_id": r["product_id"],
                    "asset": r["asset"],
                    "term_days": int(r["term_days"]),
                    "apy_bps": int(r["apy_bps"]),
                    "min_stake": r["min_stake"],
                    "max_stake_per_user": r["max_stake_per_user"],
                    "total_capacity": r["total_capacity"],
                    "total_staked": r["total_staked"],
                    "available_capacity": (
                        dstr(total_cap - staked) if total_cap is not None else None
                    ),
                    "early_unstake_penalty_bps": int(r["early_unstake_penalty_bps"]),
                    "description": r["description"],
                }
            )
        return self._send_json(200, out)

    def h_product_detail(self, product_id: str):
        with db() as conn:
            r = conn.execute(
                "SELECT * FROM staking_products WHERE product_id=?",
                (product_id,),
            ).fetchone()
        if not r:
            return self._send_json(404, {"error": "product_not_found"})
        total_cap = D(r["total_capacity"]) if r["total_capacity"] is not None else None
        staked = D(r["total_staked"])
        return self._send_json(
            200,
            {
                "product_id": r["product_id"],
                "asset": r["asset"],
                "term_days": int(r["term_days"]),
                "apy_bps": int(r["apy_bps"]),
                "min_stake": r["min_stake"],
                "max_stake_per_user": r["max_stake_per_user"],
                "total_capacity": r["total_capacity"],
                "total_staked": r["total_staked"],
                "available_capacity": (dstr(total_cap - staked) if total_cap is not None else None),
                "active": bool(r["active"]),
                "early_unstake_penalty_bps": int(r["early_unstake_penalty_bps"]),
                "description": r["description"],
                "created_at": int(r["created_at"]),
            },
        )

    # ---- Handlers (Bearer) -------------------------------------------
    def h_stake(self):
        user = self._require_user()
        if user is None:
            return
        opex = user["opex_user"]
        body = self._read_json_body()
        product_id = (body.get("product_id") or "").strip()
        amount_raw = body.get("amount")
        if not product_id or amount_raw is None:
            return self._send_json(
                400, {"error": "bad_request", "message": "product_id and amount required"}
            )
        try:
            amount = Decimal(str(amount_raw))
        except (InvalidOperation, ValueError):
            return self._send_json(400, {"error": "bad_amount"})
        status, payload = do_stake(opex, product_id, amount)
        return self._send_json(status, payload)

    def h_unstake(self, position_id: int):
        user = self._require_user()
        if user is None:
            return
        status, payload = do_unstake_or_redeem(user["opex_user"], position_id, force_early=True)
        return self._send_json(status, payload)

    def h_redeem(self, position_id: int):
        user = self._require_user()
        if user is None:
            return
        status, payload = do_unstake_or_redeem(user["opex_user"], position_id, force_early=False)
        return self._send_json(status, payload)

    def h_my_positions(self, q: dict):
        user = self._require_user()
        if user is None:
            return
        opex = user["opex_user"]
        try:
            limit = max(1, min(int((q.get("limit") or ["100"])[0]), 500))
        except ValueError:
            limit = 100
        with db() as conn:
            rows = conn.execute(
                "SELECT p.*, pr.term_days AS pr_term, pr.early_unstake_penalty_bps AS pr_pen, "
                " pr.description AS pr_desc "
                "FROM staking_positions p "
                "LEFT JOIN staking_products pr ON p.product_id = pr.product_id "
                "WHERE p.opex_user=? "
                "  AND (p.status IN ('active','matured_pending') "
                "       OR (p.status='redeemed' AND p.redeemed_at >= ?)) "
                "ORDER BY p.staked_at DESC LIMIT ?",
                (opex, int(time.time()) - 30 * SECONDS_PER_DAY, limit),
            ).fetchall()
        out = []
        for r in rows:
            d = {
                "id": int(r["id"]),
                "opex_user": r["opex_user"],
                "product_id": r["product_id"],
                "asset": r["asset"],
                "amount": r["amount"],
                "accrued_yield": r["accrued_yield"],
                "apy_bps_at_stake": int(r["apy_bps_at_stake"]),
                "staked_at": int(r["staked_at"]),
                "matures_at": int(r["matures_at"]),
                "unstaked_at": int(r["unstaked_at"]) if r["unstaked_at"] is not None else None,
                "unstake_reason": r["unstake_reason"],
                "redeemed_at": int(r["redeemed_at"]) if r["redeemed_at"] is not None else None,
                "redeemed_amount": r["redeemed_amount"],
                "status": r["status"],
                "term_days": int(r["pr_term"]) if r["pr_term"] is not None else None,
                "early_unstake_penalty_bps": (
                    int(r["pr_pen"]) if r["pr_pen"] is not None else None
                ),
                "product_description": r["pr_desc"],
            }
            out.append(d)
        return self._send_json(200, {"positions": out, "count": len(out)})

    def h_my_rewards(self, q: dict):
        user = self._require_user()
        if user is None:
            return
        opex = user["opex_user"]
        try:
            limit = max(1, min(int((q.get("limit") or ["500"])[0]), 5000))
        except ValueError:
            limit = 500
        with db() as conn:
            rows = conn.execute(
                "SELECT r.id, r.position_id, r.accrual_day, r.asset, r.amount, "
                "       r.apy_bps_used, r.posted_at, p.product_id "
                "FROM staking_rewards r "
                "JOIN staking_positions p ON p.id = r.position_id "
                "WHERE p.opex_user=? ORDER BY r.id DESC LIMIT ?",
                (opex, limit),
            ).fetchall()
        return self._send_json(
            200,
            {"rewards": [dict(r) for r in rows], "count": len(rows)},
        )

    def h_my_history(self, q: dict):
        user = self._require_user()
        if user is None:
            return
        opex = user["opex_user"]
        try:
            limit = max(1, min(int((q.get("limit") or ["100"])[0]), 1000))
        except ValueError:
            limit = 100
        with db() as conn:
            rows = conn.execute(
                "SELECT id, ts, opex_user, event, position_id, asset, amount, metadata_json "
                "FROM staking_history WHERE opex_user=? ORDER BY id DESC LIMIT ?",
                (opex, limit),
            ).fetchall()
        out = []
        for r in rows:
            try:
                md = json.loads(r["metadata_json"]) if r["metadata_json"] else None
            except Exception:
                md = None
            out.append(
                {
                    "id": int(r["id"]),
                    "ts": int(r["ts"]),
                    "event": r["event"],
                    "position_id": (
                        int(r["position_id"]) if r["position_id"] is not None else None
                    ),
                    "asset": r["asset"],
                    "amount": r["amount"],
                    "metadata": md,
                }
            )
        return self._send_json(200, {"history": out, "count": len(out)})

    # ---- Handlers (admin) --------------------------------------------
    def h_admin_tvl(self):
        if not self._require_admin():
            return
        cutoff = int(time.time()) - 30 * SECONDS_PER_DAY
        with db() as conn:
            prods = conn.execute(
                "SELECT product_id, asset, total_staked, total_capacity, apy_bps "
                "FROM staking_products ORDER BY asset ASC, term_days ASC"
            ).fetchall()
            # 30-day protocol revenue = sum(protocol_fee from redeem metadata).
            revenue_rows = conn.execute(
                "SELECT asset, metadata_json FROM staking_history "
                "WHERE event IN ('redeem','unstake') AND ts >= ?",
                (cutoff,),
            ).fetchall()
        rev_by_asset: dict[str, Decimal] = {}
        for r in revenue_rows:
            try:
                md = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
            except Exception:
                md = {}
            fee = D(md.get("protocol_fee") or 0)
            rev_by_asset[r["asset"]] = rev_by_asset.get(r["asset"], Decimal(0)) + fee
        per_product = []
        tvl_by_asset: dict[str, Decimal] = {}
        for p in prods:
            per_product.append(
                {
                    "product_id": p["product_id"],
                    "asset": p["asset"],
                    "total_staked": p["total_staked"],
                    "total_capacity": p["total_capacity"],
                    "apy_bps": int(p["apy_bps"]),
                }
            )
            tvl_by_asset[p["asset"]] = tvl_by_asset.get(p["asset"], Decimal(0)) + D(
                p["total_staked"]
            )
        return self._send_json(
            200,
            {
                "per_product": per_product,
                "tvl_by_asset": {k: dstr(v) for k, v in tvl_by_asset.items()},
                "revenue_30d_by_asset": {k: dstr(v) for k, v in rev_by_asset.items()},
                "protocol_fee_bps": PROTOCOL_FEE_BPS,
            },
        )

    def h_admin_accrue(self):
        if not self._require_admin():
            return
        try:
            res = accrue_rewards_once()
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"error": "accrual_failed", "message": repr(e)})
        return self._send_json(200, res)


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5692
    init_db()
    get_admin_token()  # print once at boot if not set in env
    # Best-effort initial accrual so the demo doesn't have to wait a full tick.
    try:
        accrue_rewards_once()
    except Exception as e:  # noqa: BLE001
        log(f"initial accrual failed: {e!r}")
    threading.Thread(target=_accrual_loop, daemon=True).start()
    log(f"db={DB_PATH}")
    log(
        f"accrual_interval_s={ACCRUAL_INTERVAL_S} "
        f"protocol_fee_bps={PROTOCOL_FEE_BPS} "
        f"wallet_base={WALLET_BASE} auth_base={AUTH_BASE}"
    )
    log(f"listening on :{port}")
    with ThreadingServer(("", port), Handler) as srv:
        srv.serve_forever()


if __name__ == "__main__":
    main()
