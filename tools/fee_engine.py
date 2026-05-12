#!/usr/bin/env python3
"""VIP fee tier engine for the zkCEX demo.

Listens on :5710 by default and is reverse-proxied by ``serve_homepage.py``
under ``/fees/``. Stores per-user 30-day trading volume, current VIP tier,
and a fee_history audit log in ``tools/.local/fee_engine.db`` (SQLite).

The fee model is shaped after Binance's published 9-tier VIP ladder — 30-day
rolling spot+futures volume in USDT determines the level, each level cutting
maker/taker bps. Pricing is in basis points (0.01%) stored as decimal strings
so the math is exact (we never lose a half-bp to float rounding).

Routes
------
GET  /fees/tiers                                  public        ladder
GET  /fees/my-tier                                Bearer        user's tier
GET  /fees/quote                                  Bearer        fee estimate
POST /fees/internal/record                        loopback      record a fill
POST /fees/internal/recompute-tier/<opex_user>    loopback      force recompute
GET  /fees/admin/leaderboard                      admin Bearer  top users
GET  /fees/health                                 public        liveness

The matching engines (perp_engine.py, matching-gateway) keep their existing
flat fee logic unless ``VIP_FEES_ENABLED=1`` is set in their environment, at
which point they should consult ``/fees/quote`` before applying the fee.
A documented integration pattern is at the bottom of this file.
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
from decimal import Decimal, getcontext
from typing import Any

# Plenty of head-room: USDT volumes can hit 25B at VIP9 and we multiply
# by per-bp ratios that have small fractional parts.
getcontext().prec = 40

# --------------------------------------------------------------------------
# Config / paths
# --------------------------------------------------------------------------

DEFAULT_PORT = 5710
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("FEE_ENGINE_DB", os.path.join(HERE, ".local", "fee_engine.db"))


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
PUSH_BASE = _validated_http_base_url(
    "PUSH_BASE", os.environ.get("PUSH_BASE", "http://127.0.0.1:5580")
)
REFERRAL_BASE = _validated_http_base_url(
    "REFERRAL_BASE", os.environ.get("REFERRAL_BASE", "http://127.0.0.1:5695")
)
AUTH_TTL_SECONDS = 30
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
# 30-day window in seconds. Sliding — we re-aggregate every 60s and tier-bump
# the user without recomputing per-trade.
WINDOW_SECONDS = 30 * 24 * 3600
AGGREGATOR_INTERVAL_SECONDS = 60
ADMIN_TOKEN_ENV = "FEE_ADMIN_TOKEN"  # noqa: S105 - environment variable name, not a secret value.

ZERO = Decimal("0")


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------


def log(*args: object) -> None:
    sys.stderr.write("[fees] " + " ".join(str(a) for a in args) + "\n")


# --------------------------------------------------------------------------
# Pre-seed VIP tiers (shape matches a published 9-tier exchange ladder)
# Tuple order: level, name, min_30d_volume_usdt, min_holdings_usdt,
#              spot_maker_bps, spot_taker_bps, futures_maker_bps,
#              futures_taker_bps, withdraw_discount_bps, description
# --------------------------------------------------------------------------

DEFAULT_TIERS = [
    (0, "Regular", "0", "0", "10", "10", "2", "5", "0", "기본 등급 / Default tier for new users"),
    (
        1,
        "VIP 1",
        "50000",
        "100",
        "9",
        "10",
        "1.8",
        "4.5",
        "5",
        "거래량 5만 USDT 이상 / 30d volume ≥ 50K USDT",
    ),
    (
        2,
        "VIP 2",
        "500000",
        "1000",
        "8",
        "9",
        "1.5",
        "4",
        "10",
        "거래량 50만 USDT 이상 / 30d volume ≥ 500K USDT",
    ),
    (
        3,
        "VIP 3",
        "5000000",
        "10000",
        "7",
        "8",
        "1.2",
        "3.5",
        "15",
        "거래량 500만 USDT 이상 / 30d volume ≥ 5M USDT",
    ),
    (
        4,
        "VIP 4",
        "20000000",
        "50000",
        "6",
        "7",
        "1.0",
        "3",
        "20",
        "거래량 2000만 USDT 이상 / 30d volume ≥ 20M USDT",
    ),
    (
        5,
        "VIP 5",
        "100000000",
        "200000",
        "5",
        "6",
        "0.8",
        "2.5",
        "25",
        "거래량 1억 USDT 이상 / 30d volume ≥ 100M USDT",
    ),
    (
        6,
        "VIP 6",
        "500000000",
        "500000",
        "4",
        "5",
        "0.6",
        "2",
        "30",
        "거래량 5억 USDT 이상 / 30d volume ≥ 500M USDT",
    ),
    (
        7,
        "VIP 7",
        "2500000000",
        "1000000",
        "3",
        "4",
        "0.4",
        "1.5",
        "35",
        "거래량 25억 USDT 이상 / 30d volume ≥ 2.5B USDT",
    ),
    (
        8,
        "VIP 8",
        "10000000000",
        "5000000",
        "2",
        "3",
        "0.2",
        "1",
        "40",
        "거래량 100억 USDT 이상 / 30d volume ≥ 10B USDT",
    ),
    (
        9,
        "VIP 9 (Market Maker)",
        "25000000000",
        "10000000",
        "0",
        "2",
        "0",
        "0.5",
        "50",
        "마켓 메이커 등급 / Designated market maker",
    ),
]


# --------------------------------------------------------------------------
# DB helpers
# --------------------------------------------------------------------------

_db_lock = threading.RLock()


def db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    with _db_lock, db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS vip_tiers (
          level INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          min_30d_volume_usdt TEXT NOT NULL,
          min_holdings_usdt TEXT NOT NULL DEFAULT '0',
          spot_maker_bps TEXT NOT NULL,
          spot_taker_bps TEXT NOT NULL,
          futures_maker_bps TEXT NOT NULL,
          futures_taker_bps TEXT NOT NULL,
          withdraw_discount_bps TEXT NOT NULL DEFAULT '0',
          description TEXT
        );

        CREATE TABLE IF NOT EXISTS user_volume_30d (
          opex_user TEXT PRIMARY KEY,
          spot_volume_usdt TEXT NOT NULL DEFAULT '0',
          futures_volume_usdt TEXT NOT NULL DEFAULT '0',
          total_volume_usdt TEXT NOT NULL DEFAULT '0',
          holdings_usdt TEXT NOT NULL DEFAULT '0',
          current_tier INTEGER NOT NULL DEFAULT 0,
          last_recompute_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS fee_history (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts INTEGER NOT NULL,
          opex_user TEXT NOT NULL,
          tier INTEGER NOT NULL,
          trade_id TEXT,
          market TEXT NOT NULL,
          symbol TEXT NOT NULL,
          side TEXT NOT NULL,
          notional_usdt TEXT NOT NULL,
          fee_bps TEXT NOT NULL,
          fee_usdt TEXT NOT NULL,
          is_maker INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_fee_history_user_ts
          ON fee_history(opex_user, ts);
        CREATE INDEX IF NOT EXISTS ix_fee_history_ts
          ON fee_history(ts);
        """)
        # Seed VIP tiers only when the table is empty; we never overwrite an
        # operator's tuning between restarts.
        n = c.execute("SELECT COUNT(*) AS n FROM vip_tiers").fetchone()["n"]
        if n == 0:
            c.executemany(
                "INSERT INTO vip_tiers(level, name, min_30d_volume_usdt, min_holdings_usdt,"
                " spot_maker_bps, spot_taker_bps, futures_maker_bps, futures_taker_bps,"
                " withdraw_discount_bps, description) VALUES (?,?,?,?,?,?,?,?,?,?)",
                DEFAULT_TIERS,
            )
            log(f"seeded {len(DEFAULT_TIERS)} VIP tiers")


def all_tiers() -> list[sqlite3.Row]:
    with _db_lock, db() as c:
        return list(c.execute("SELECT * FROM vip_tiers ORDER BY level ASC").fetchall())


def tier_row(level: int) -> sqlite3.Row | None:
    with _db_lock, db() as c:
        return c.execute("SELECT * FROM vip_tiers WHERE level=?", (level,)).fetchone()


# --------------------------------------------------------------------------
# Auth: validate Bearer against the auth_server. Cache briefly.
# --------------------------------------------------------------------------

_auth_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_auth_cache_lock = threading.Lock()


def resolve_user(token: str | None) -> dict | None:
    if not token:
        return None
    now = time.time()
    with _auth_cache_lock:
        hit = _auth_cache.get(token)
        if hit and now - hit[0] < AUTH_TTL_SECONDS:
            return hit[1]
    try:
        req = _http_request(
            f"{AUTH_BASE}/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        with _http_urlopen(req, timeout=4) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log("auth/me", e.code, "token=", token[:8])
        return None
    except Exception as e:
        log("auth/me transport error:", e)
        return None
    user = obj.get("user") or {}
    if not user.get("opex_user"):
        return None
    with _auth_cache_lock:
        _auth_cache[token] = (now, user)
    return user


# --------------------------------------------------------------------------
# Tier resolution
# --------------------------------------------------------------------------


def D(x: Any) -> Decimal:
    """Tolerant Decimal coercion."""
    if x is None or x == "":
        return ZERO
    try:
        return Decimal(str(x))
    except Exception:
        return ZERO


def determine_tier(total_volume: Decimal, holdings: Decimal) -> sqlite3.Row:
    """Walk the ladder top-down: highest level whose volume threshold is met
    qualifies. Real exchanges treat the holdings requirement as a separate
    shortcut path rather than a strict AND — a user with $50K volume gets
    VIP 1 regardless of holdings; a user with $0 volume but enough holdings
    also climbs (the second path is enabled via ``holdings`` parameter so
    explicit zero holdings doesn't block volume-based promotion). Tier 0 is
    the floor."""
    rows = all_tiers()
    # rows are ASC by level; walk descending so the first match is the top.
    for r in sorted(rows, key=lambda x: x["level"], reverse=True):
        meets_vol = total_volume >= D(r["min_30d_volume_usdt"])
        # Volume alone qualifies; holdings is an OR alternative for users
        # who hold the platform token in size but trade less.
        meets_holdings = holdings > 0 and holdings >= D(r["min_holdings_usdt"])
        if meets_vol or meets_holdings:
            return r
    # If somehow nothing matched (shouldn't happen since level 0 has 0/0
    # thresholds) fall back to level 0.
    return rows[0]


def user_volume_row(opex_user: str) -> sqlite3.Row:
    with _db_lock, db() as c:
        row = c.execute("SELECT * FROM user_volume_30d WHERE opex_user=?", (opex_user,)).fetchone()
        if row is None:
            c.execute(
                "INSERT INTO user_volume_30d(opex_user, last_recompute_at) VALUES (?, ?)",
                (opex_user, int(time.time())),
            )
            row = c.execute(
                "SELECT * FROM user_volume_30d WHERE opex_user=?", (opex_user,)
            ).fetchone()
    return row


def recompute_user(opex_user: str) -> dict:
    """Re-aggregate the user's 30-day volume from fee_history, update the
    user_volume_30d row, and bump their tier. Returns the new row as a dict.
    If the tier changed, fire a /push/send notification.
    """
    now = int(time.time())
    cutoff = now - WINDOW_SECONDS
    with _db_lock, db() as c:
        # Window-sum spot + futures volume separately, plus the total.
        rows = c.execute(
            "SELECT market, SUM(CAST(notional_usdt AS REAL)) AS vol"
            " FROM fee_history WHERE opex_user=? AND ts>=? GROUP BY market",
            (opex_user, cutoff),
        ).fetchall()
        spot_vol = ZERO
        fut_vol = ZERO
        for r in rows:
            if r["market"] == "spot":
                spot_vol = D(r["vol"] or 0)
            elif r["market"] == "futures":
                fut_vol = D(r["vol"] or 0)
        total_vol = spot_vol + fut_vol
        # Holdings live in the user_volume_30d row (deposit/PoL pipeline would
        # update it; for now we trust whatever's there).
        existing = c.execute(
            "SELECT current_tier, holdings_usdt FROM user_volume_30d WHERE opex_user=?",
            (opex_user,),
        ).fetchone()
        prev_tier = existing["current_tier"] if existing else 0
        holdings = D(existing["holdings_usdt"]) if existing else ZERO
        new_tier_row = determine_tier(total_vol, holdings)
        new_tier = int(new_tier_row["level"])

        c.execute(
            "INSERT INTO user_volume_30d(opex_user, spot_volume_usdt, futures_volume_usdt,"
            " total_volume_usdt, holdings_usdt, current_tier, last_recompute_at)"
            " VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(opex_user) DO UPDATE SET"
            "   spot_volume_usdt=excluded.spot_volume_usdt,"
            "   futures_volume_usdt=excluded.futures_volume_usdt,"
            "   total_volume_usdt=excluded.total_volume_usdt,"
            "   current_tier=excluded.current_tier,"
            "   last_recompute_at=excluded.last_recompute_at",
            (
                opex_user,
                str(spot_vol),
                str(fut_vol),
                str(total_vol),
                str(holdings),
                new_tier,
                now,
            ),
        )
    if new_tier != prev_tier:
        promo = new_tier > prev_tier
        try:
            _push_tier_change(opex_user, prev_tier, new_tier, promoted=promo)
        except Exception as e:
            log("push notify failed:", e)
    return {
        "opex_user": opex_user,
        "spot_volume_usdt": str(spot_vol),
        "futures_volume_usdt": str(fut_vol),
        "total_volume_usdt": str(total_vol),
        "holdings_usdt": str(holdings),
        "current_tier": new_tier,
        "previous_tier": prev_tier,
        "tier_changed": new_tier != prev_tier,
    }


def _push_tier_change(opex_user: str, prev: int, new: int, promoted: bool) -> None:
    """Best-effort notification via push_server's loopback /push/send."""
    title = (
        "VIP 등급 상승 / VIP level promoted" if promoted else "VIP 등급 조정 / VIP level adjusted"
    )
    new_row = tier_row(new)
    new_name = new_row["name"] if new_row else f"VIP {new}"
    body = (
        f"Your VIP level changed from {prev} to {new} ({new_name})."
        if not promoted
        else f"Promoted to VIP {new} ({new_name}). Lower trading fees now apply."
    )
    payload = {
        "opex_user": opex_user,
        "payload": {
            "title": title,
            "body": body,
            "tag": "vip-tier",
            "data": {"url": "/app/fees.html", "tier": new},
        },
    }
    req = _http_request(
        f"{PUSH_BASE}/push/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        _http_urlopen(req, timeout=3).read()
    except Exception as e:  # noqa: BLE001
        # push_server may not be running; not fatal.
        log("vip push skipped:", e)


def _forward_referral_fee(opex_user: str, fee_usdt, trade_id, notional_usdt) -> None:
    """Notify referral.py that ``opex_user`` paid a fee. Loopback POST."""
    if not opex_user or fee_usdt is None:
        return
    try:
        body = json.dumps(
            {
                "referee_opex_user": opex_user,
                "fee_usdt": str(fee_usdt),
                "trade_id": trade_id,
                "notional_usdt": str(notional_usdt) if notional_usdt is not None else None,
            }
        ).encode("utf-8")
        req = _http_request(
            f"{REFERRAL_BASE}/referral/internal/record-fee",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        _http_urlopen(req, timeout=2).read()
    except Exception as e:  # noqa: BLE001
        # referral.py may not be running; not fatal.
        log("referral fee forward skipped:", e)


# --------------------------------------------------------------------------
# Fee computation
# --------------------------------------------------------------------------


def _bps_for(tier: sqlite3.Row, market: str, is_maker: bool) -> Decimal:
    market = (market or "spot").lower()
    if market == "futures":
        col = "futures_maker_bps" if is_maker else "futures_taker_bps"
    else:
        col = "spot_maker_bps" if is_maker else "spot_taker_bps"
    return D(tier[col])


def compute_fee(notional_usdt: Decimal, bps: Decimal) -> Decimal:
    """1 bps = 0.01%. fee = notional * bps / 10000."""
    if notional_usdt <= 0 or bps <= 0:
        return ZERO
    return (notional_usdt * bps) / Decimal("10000")


def get_user_tier(opex_user: str) -> sqlite3.Row:
    row = user_volume_row(opex_user)
    t = tier_row(int(row["current_tier"]))
    if t is None:
        # corrupted FK; fall back to floor.
        t = tier_row(0)
    return t


# --------------------------------------------------------------------------
# Background aggregator: every 60s, rescan every user that has either a
# fee_history fill in the window OR a user_volume_30d row. Push tier-bumps.
# --------------------------------------------------------------------------


def _aggregator_loop() -> None:
    while True:
        try:
            now = int(time.time())
            cutoff = now - WINDOW_SECONDS
            with _db_lock, db() as c:
                users = c.execute(
                    "SELECT DISTINCT opex_user FROM fee_history WHERE ts>=?"
                    " UNION SELECT opex_user FROM user_volume_30d",
                    (cutoff,),
                ).fetchall()
            for r in users:
                try:
                    recompute_user(r["opex_user"])
                except Exception as e:
                    log("aggregator recompute err:", r["opex_user"], e)
        except Exception as e:
            log("aggregator loop err:", e)
        time.sleep(AGGREGATOR_INTERVAL_SECONDS)


# --------------------------------------------------------------------------
# Admin token (lazy)
# --------------------------------------------------------------------------

_admin_token_lock = threading.Lock()
_admin_token_cache: list[str] = []


def get_admin_token() -> str:
    env = os.environ.get(ADMIN_TOKEN_ENV)
    if env:
        return env
    with _admin_token_lock:
        if _admin_token_cache:
            return _admin_token_cache[0]
        import secrets

        tok = "fee_admin_" + secrets.token_urlsafe(20)
        _admin_token_cache.append(tok)
        log(f"{ADMIN_TOKEN_ENV}={tok}  (set in env to make it stable)")
        return tok


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------


def _json(handler: http.server.BaseHTTPRequestHandler, status: int, body: Any) -> None:
    payload = json.dumps(body, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _read_body(handler: http.server.BaseHTTPRequestHandler) -> dict:
    n = int(handler.headers.get("Content-Length") or 0)
    if n <= 0:
        return {}
    raw = handler.rfile.read(n)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def _bearer(handler: http.server.BaseHTTPRequestHandler) -> str | None:
    h = handler.headers.get("Authorization") or ""
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return None


def _client_is_loopback(handler: http.server.BaseHTTPRequestHandler) -> bool:
    ip = handler.client_address[0] if handler.client_address else ""
    return ip in LOOPBACK_HOSTS or ip.startswith("127.")


def _serialise_tier(t: sqlite3.Row) -> dict:
    return {
        "level": int(t["level"]),
        "name": t["name"],
        "min_30d_volume_usdt": t["min_30d_volume_usdt"],
        "min_holdings_usdt": t["min_holdings_usdt"],
        "spot_maker_bps": t["spot_maker_bps"],
        "spot_taker_bps": t["spot_taker_bps"],
        "futures_maker_bps": t["futures_maker_bps"],
        "futures_taker_bps": t["futures_taker_bps"],
        "withdraw_discount_bps": t["withdraw_discount_bps"],
        "description": t["description"],
        # Convenience % representations (1 bps = 0.01%).
        "spot_maker_pct": str(D(t["spot_maker_bps"]) / Decimal("100")),
        "spot_taker_pct": str(D(t["spot_taker_bps"]) / Decimal("100")),
        "futures_maker_pct": str(D(t["futures_maker_bps"]) / Decimal("100")),
        "futures_taker_pct": str(D(t["futures_taker_bps"]) / Decimal("100")),
    }


def _next_tier_target(current_level: int, total_volume: Decimal, holdings: Decimal) -> dict | None:
    """Return the gap to the next tier above the user's current level, or
    None if the user is already at the top."""
    rows = all_tiers()
    higher = [r for r in rows if int(r["level"]) > current_level]
    if not higher:
        return None
    nxt = higher[0]  # rows are sorted ASC
    need_vol = D(nxt["min_30d_volume_usdt"]) - total_volume
    need_hold = D(nxt["min_holdings_usdt"]) - holdings
    return {
        "level": int(nxt["level"]),
        "name": nxt["name"],
        "volume_needed_usdt": str(max(ZERO, need_vol)),
        "holdings_needed_usdt": str(max(ZERO, need_hold)),
        "spot_maker_bps": nxt["spot_maker_bps"],
        "spot_taker_bps": nxt["spot_taker_bps"],
        "futures_maker_bps": nxt["futures_maker_bps"],
        "futures_taker_bps": nxt["futures_taker_bps"],
    }


def _quote_fee_for_tier(tier: sqlite3.Row, market: str, notional: Decimal) -> dict:
    maker_bps = _bps_for(tier, market, True)
    taker_bps = _bps_for(tier, market, False)
    maker_fee = compute_fee(notional, maker_bps)
    taker_fee = compute_fee(notional, taker_bps)
    return {
        "notional_usdt": str(notional),
        "tier": int(tier["level"]),
        "tier_name": tier["name"],
        "market": market,
        "maker_fee_bps": str(maker_bps),
        "maker_fee_usdt": str(maker_fee),
        "taker_fee_bps": str(taker_bps),
        "taker_fee_usdt": str(taker_fee),
    }


# --------------------------------------------------------------------------
# Handler
# --------------------------------------------------------------------------


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "zkcex-fees/1.0"

    def log_message(self, fmt, *args):
        log(self.client_address[0], fmt % args)

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/fees/health":
            return _json(self, 200, {"ok": True, "now": int(time.time())})
        if path == "/fees/tiers":
            return self._get_tiers()
        if path == "/fees/my-tier":
            return self._get_my_tier()
        if path == "/fees/quote":
            return self._get_quote()
        if path == "/fees/admin/leaderboard":
            return self._admin_leaderboard()
        return _json(self, 404, {"error": "not_found"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/fees/internal/record":
            return self._record_fill()
        if path.startswith("/fees/internal/recompute-tier/"):
            return self._recompute(path[len("/fees/internal/recompute-tier/") :])
        return _json(self, 404, {"error": "not_found"})

    # ---- GET /fees/tiers -----------------------------------------------
    def _get_tiers(self):
        rows = all_tiers()
        return _json(self, 200, {"tiers": [_serialise_tier(r) for r in rows]})

    # ---- GET /fees/my-tier ---------------------------------------------
    def _get_my_tier(self):
        token = _bearer(self)
        user = resolve_user(token)
        if not user:
            return _json(self, 401, {"error": "unauthorized"})
        opex = user["opex_user"]
        # Lazy recompute so the response is fresh even if the aggregator
        # hasn't ticked yet for this user.
        recompute_user(opex)
        vrow = user_volume_row(opex)
        tier = get_user_tier(opex)
        total_vol = D(vrow["total_volume_usdt"])
        holdings = D(vrow["holdings_usdt"])
        nxt = _next_tier_target(int(tier["level"]), total_vol, holdings)

        # 30d history (recent 50 rows) for the page chart + list.
        cutoff = int(time.time()) - WINDOW_SECONDS
        with _db_lock, db() as c:
            hist_rows = c.execute(
                "SELECT id, ts, tier, market, symbol, side, notional_usdt, fee_bps,"
                " fee_usdt, is_maker FROM fee_history"
                " WHERE opex_user=? AND ts>=? ORDER BY ts DESC LIMIT 50",
                (opex, cutoff),
            ).fetchall()
            # Daily aggregates for the SVG sparkline (last 30 days).
            daily_rows = c.execute(
                "SELECT (ts/86400) AS day, SUM(CAST(notional_usdt AS REAL)) AS vol,"
                " SUM(CAST(fee_usdt AS REAL)) AS fees FROM fee_history"
                " WHERE opex_user=? AND ts>=? GROUP BY day ORDER BY day ASC",
                (opex, cutoff),
            ).fetchall()
        history = [
            {
                "id": r["id"],
                "ts": r["ts"],
                "tier": r["tier"],
                "market": r["market"],
                "symbol": r["symbol"],
                "side": r["side"],
                "notional_usdt": r["notional_usdt"],
                "fee_bps": r["fee_bps"],
                "fee_usdt": r["fee_usdt"],
                "is_maker": bool(r["is_maker"]),
            }
            for r in hist_rows
        ]
        # Estimate "savings vs Regular" — sum over the window of how much
        # less the user paid because they're above tier 0.
        regular = tier_row(0)
        savings = ZERO
        if regular and history:
            for h in history:
                mkt = h["market"]
                is_m = bool(h["is_maker"])
                paid = D(h["fee_usdt"])
                regular_bps = _bps_for(regular, mkt, is_m)
                regular_fee = compute_fee(D(h["notional_usdt"]), regular_bps)
                diff = regular_fee - paid
                if diff > 0:
                    savings += diff

        daily = [
            {
                "day": int(r["day"]),
                "date": time.strftime("%Y-%m-%d", time.gmtime(int(r["day"]) * 86400)),
                "volume_usdt": str(D(r["vol"] or 0)),
                "fees_usdt": str(D(r["fees"] or 0)),
            }
            for r in daily_rows
        ]
        return _json(
            self,
            200,
            {
                "opex_user": opex,
                "current_tier": int(tier["level"]),
                "tier_name": tier["name"],
                "30d_volume_usdt": str(total_vol),
                "30d_spot_volume_usdt": vrow["spot_volume_usdt"],
                "30d_futures_volume_usdt": vrow["futures_volume_usdt"],
                "holdings_usdt": str(holdings),
                "spot_maker_bps": tier["spot_maker_bps"],
                "spot_taker_bps": tier["spot_taker_bps"],
                "futures_maker_bps": tier["futures_maker_bps"],
                "futures_taker_bps": tier["futures_taker_bps"],
                "withdraw_discount_bps": tier["withdraw_discount_bps"],
                "next_tier": nxt,
                "savings_vs_regular_usdt": str(savings),
                "30d_history": history,
                "30d_daily": daily,
                "last_recompute_at": vrow["last_recompute_at"],
            },
        )

    # ---- GET /fees/quote -----------------------------------------------
    def _get_quote(self):
        token = _bearer(self)
        user = resolve_user(token)
        if not user:
            return _json(self, 401, {"error": "unauthorized"})
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        market = (qs.get("market", ["spot"])[0] or "spot").lower()
        if market not in ("spot", "futures"):
            return _json(self, 400, {"error": "bad_market", "hint": "spot or futures"})
        symbol = qs.get("symbol", [""])[0] or ""
        side = (qs.get("side", ["BUY"])[0] or "BUY").upper()
        try:
            qty = D(qs.get("qty", ["0"])[0])
            price = D(qs.get("price", ["0"])[0])
        except Exception:
            return _json(self, 400, {"error": "bad_numbers"})
        if qty <= 0 or price <= 0:
            return _json(self, 400, {"error": "qty_and_price_required"})
        notional = qty * price
        tier = get_user_tier(user["opex_user"])
        out = _quote_fee_for_tier(tier, market, notional)
        out.update(
            {
                "symbol": symbol,
                "side": side,
                "qty": str(qty),
                "price": str(price),
                "opex_user": user["opex_user"],
            }
        )
        return _json(self, 200, out)

    # ---- POST /fees/internal/record (loopback) -------------------------
    def _record_fill(self):
        if not _client_is_loopback(self):
            return _json(self, 403, {"error": "loopback_only"})
        body = _read_body(self)
        opex = (body.get("opex_user") or "").strip()
        market = (body.get("market") or "spot").lower()
        symbol = (body.get("symbol") or "").strip()
        side = (body.get("side") or "BUY").upper()
        trade_id = body.get("trade_id")
        try:
            notional = D(body.get("notional_usdt", "0"))
        except Exception:
            return _json(self, 400, {"error": "bad_notional"})
        is_maker = bool(body.get("is_maker", False))
        if not opex or notional <= 0:
            return _json(self, 400, {"error": "missing_user_or_notional"})
        if market not in ("spot", "futures"):
            return _json(self, 400, {"error": "bad_market"})
        # Resolve fee from the user's current tier at fill time.
        tier = get_user_tier(opex)
        bps = _bps_for(tier, market, is_maker)
        fee_usdt = compute_fee(notional, bps)
        now = int(time.time())
        with _db_lock, db() as c:
            c.execute(
                "INSERT INTO fee_history(ts, opex_user, tier, trade_id, market, symbol,"
                " side, notional_usdt, fee_bps, fee_usdt, is_maker)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    now,
                    opex,
                    int(tier["level"]),
                    str(trade_id) if trade_id is not None else None,
                    market,
                    symbol,
                    side,
                    str(notional),
                    str(bps),
                    str(fee_usdt),
                    1 if is_maker else 0,
                ),
            )
        # Referral fee-share fan-out (best-effort, never blocks the fee record).
        _forward_referral_fee(opex, fee_usdt, trade_id, notional)
        return _json(
            self,
            200,
            {
                "tier": int(tier["level"]),
                "fee_bps": str(bps),
                "fee_usdt": str(fee_usdt),
                "notional_usdt": str(notional),
                "is_maker": is_maker,
            },
        )

    # ---- POST /fees/internal/recompute-tier/<opex_user> ----------------
    def _recompute(self, opex_user: str):
        if not _client_is_loopback(self):
            return _json(self, 403, {"error": "loopback_only"})
        opex_user = urllib.parse.unquote(opex_user).strip("/")
        if not opex_user:
            return _json(self, 400, {"error": "missing_opex_user"})
        out = recompute_user(opex_user)
        return _json(self, 200, out)

    # ---- GET /fees/admin/leaderboard -----------------------------------
    def _admin_leaderboard(self):
        token = _bearer(self)
        if not (token and token == get_admin_token()) and not _client_is_loopback(self):
            return _json(self, 403, {"error": "admin_only"})
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        try:
            limit = max(1, min(200, int(qs.get("limit", ["20"])[0])))
        except ValueError:
            limit = 20
        with _db_lock, db() as c:
            rows = c.execute(
                "SELECT opex_user, total_volume_usdt, spot_volume_usdt,"
                " futures_volume_usdt, holdings_usdt, current_tier"
                " FROM user_volume_30d"
                " ORDER BY CAST(total_volume_usdt AS REAL) DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = [
            {
                "opex_user": r["opex_user"],
                "tier": int(r["current_tier"]),
                "total_volume_usdt": r["total_volume_usdt"],
                "spot_volume_usdt": r["spot_volume_usdt"],
                "futures_volume_usdt": r["futures_volume_usdt"],
                "holdings_usdt": r["holdings_usdt"],
            }
            for r in rows
        ]
        return _json(self, 200, {"leaderboard": out, "count": len(out)})


# --------------------------------------------------------------------------
# Server bootstrap
# --------------------------------------------------------------------------


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    init_db()
    # Force admin token to materialise so the first /fees/admin/leaderboard
    # caller can find a value in the log if they didn't set the env var.
    get_admin_token()
    t = threading.Thread(target=_aggregator_loop, daemon=True, name="fees-aggregator")
    t.start()
    with ThreadingServer(("", port), Handler) as srv:
        log(f"fee engine listening on :{port}")
        log(f"  db -> {DB_PATH}")
        log(f"  30d window = {WINDOW_SECONDS}s, aggregator every {AGGREGATOR_INTERVAL_SECONDS}s")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


# ============================================================================
# Matching-engine integration pattern (documented, not wired)
# ----------------------------------------------------------------------------
# The existing perp_engine.py uses two module constants:
#
#     TAKER_FEE = Decimal(os.environ.get("PERP_TAKER_FEE", "0.0005"))
#     MAKER_FEE = Decimal(os.environ.get("PERP_MAKER_FEE", "0.0002"))
#
# and computes ``fee = notional * fee_rate`` inside ``_apply_fill`` (around
# line 1345 in perp_engine.py at the time of writing). To wire VIP fees:
#
#   1. At the top of perp_engine.py add a feature flag:
#
#        VIP_FEES_ENABLED = os.environ.get("VIP_FEES_ENABLED") == "1"
#        FEE_ENGINE_BASE = os.environ.get("FEE_ENGINE_BASE", "http://127.0.0.1:5710")
#
#   2. In ``_apply_fill``, replace the ``fee_rate = ...`` line with:
#
#        if VIP_FEES_ENABLED:
#            try:
#                req = urllib.request.Request(
#                    f"{FEE_ENGINE_BASE}/fees/internal/record",
#                    data=json.dumps({
#                        "opex_user": opex_user,
#                        "market": "futures",
#                        "symbol": symbol,
#                        "side": fill_side,
#                        "notional_usdt": str(notional),
#                        "is_maker": is_maker,
#                        "trade_id": str(order_id),
#                    }).encode(),
#                    headers={"Content-Type": "application/json"},
#                )
#                with urllib.request.urlopen(req, timeout=2) as resp:
#                    out = json.loads(resp.read())
#                fee = Decimal(out["fee_usdt"])
#            except Exception:
#                fee = notional * (MAKER_FEE if is_maker else TAKER_FEE)
#        else:
#            fee = notional * (MAKER_FEE if is_maker else TAKER_FEE)
#
# The same shape works for the matching-gateway (Kotlin) — call /fees/quote
# before the trade or /fees/internal/record after the fill, and fall back to
# the flat fee on any error so the trade flow never blocks on the fee
# engine being unavailable.
# ============================================================================


if __name__ == "__main__":
    main()
