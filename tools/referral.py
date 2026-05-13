#!/usr/bin/env python3
"""Referral / affiliate ledger (port 5695).

A standalone stdlib-only service that mints per-user referral codes, records
referrer<>referee relationships at signup, and pays out lifetime fee-share
rewards as referees keep trading. Persistence lives at
``tools/.local/referral.db``.

Endpoints (public unless noted):

  GET  /referral/health                        liveness
  GET  /referral/my-code                       Bearer
  POST /referral/customize                     Bearer  (rename once)
  POST /referral/apply                         loopback only
  GET  /referral/stats                         Bearer  (dashboard JSON)
  GET  /referral/leaderboard                   public  (30d top earners)
  GET  /referral/campaigns                     public  (active campaign meta)

Loopback-only side-channels invoked by other services:
  POST /referral/internal/record-signup        (auth_server -> here)
  POST /referral/internal/record-kyc           (auth_server -> here)
  POST /referral/internal/record-first-trade   (fee_engine -> here)
  POST /referral/internal/record-fee           (fee_engine -> here)

The service is purely additive: nothing else in zkCEX *requires* it to run.
If a sibling integration call fails (e.g. wallet API down), the earning is
left ``paid_at IS NULL`` and a background thread retries every 60 s.
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
from decimal import Decimal, getcontext

getcontext().prec = 36

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join(HERE, ".local")
os.makedirs(LOCAL_DIR, exist_ok=True)
DB_PATH = os.path.join(LOCAL_DIR, "referral.db")


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
    "REFERRAL_AUTH_BASE", os.environ.get("REFERRAL_AUTH_BASE", "http://127.0.0.1:5501")
)
WALLET_BASE = _validated_http_base_url(
    "REFERRAL_WALLET_BASE", os.environ.get("REFERRAL_WALLET_BASE", "http://127.0.0.1:8091")
)
PUSH_BASE = _validated_http_base_url(
    "REFERRAL_PUSH_BASE", os.environ.get("REFERRAL_PUSH_BASE", "http://127.0.0.1:5580")
)
LISTEN_HOST = os.environ.get("REFERRAL_HOST", "127.0.0.1")
PAYOUT_INTERVAL_S = int(os.environ.get("REFERRAL_PAYOUT_INTERVAL_S", "60"))
PAYOUT_ASSET_INTERNAL = os.environ.get("REFERRAL_PAYOUT_ASSET", "USDT")

# Anti-abuse / regulatory caps. Decimal-string so they fit cleanly into SQL.
PER_REFERRER_DAILY_CAP_USDT = Decimal(os.environ.get("REFERRAL_DAILY_CAP", "10000"))
PROGRAM_LIFETIME_CAP_USDT = Decimal(os.environ.get("REFERRAL_LIFETIME_CAP", "1000000"))
FIRST_TRADE_MIN_USDT = Decimal(os.environ.get("REFERRAL_MIN_FIRST_TRADE", "50"))

START_TS = int(time.time())
_db_lock = threading.Lock()
_payout_lock = threading.Lock()


def log(msg: str) -> None:
    sys.stderr.write(f"[referral] {msg}\n")
    sys.stderr.flush()


# ===========================================================================
# DB
# ===========================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS referral_codes (
  code TEXT PRIMARY KEY,
  opex_user TEXT NOT NULL UNIQUE,
  custom_name INTEGER NOT NULL DEFAULT 0,
  total_signups INTEGER NOT NULL DEFAULT 0,
  total_referred_volume_usdt TEXT NOT NULL DEFAULT '0',
  total_earnings_usdt TEXT NOT NULL DEFAULT '0',
  active INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_codes_user ON referral_codes(opex_user);

CREATE TABLE IF NOT EXISTS referral_relationships (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  referrer_opex_user TEXT NOT NULL,
  referee_opex_user TEXT NOT NULL,
  code_used TEXT NOT NULL,
  referee_signup_at INTEGER NOT NULL,
  referee_kyc_verified_at INTEGER,
  referee_first_trade_at INTEGER,
  status TEXT NOT NULL,
  bonus_paid_signup INTEGER NOT NULL DEFAULT 0,
  bonus_paid_first_trade INTEGER NOT NULL DEFAULT 0,
  signup_ip_redacted TEXT,
  UNIQUE(referee_opex_user)
);
CREATE INDEX IF NOT EXISTS idx_rel_referrer ON referral_relationships(referrer_opex_user);
CREATE INDEX IF NOT EXISTS idx_rel_status   ON referral_relationships(status);

CREATE TABLE IF NOT EXISTS referral_earnings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  referrer_opex_user TEXT NOT NULL,
  referee_opex_user TEXT NOT NULL,
  earning_type TEXT NOT NULL,
  amount_usdt TEXT NOT NULL,
  trade_id TEXT,
  related_fee_usdt TEXT,
  paid_at INTEGER,
  payout_tx TEXT
);
CREATE INDEX IF NOT EXISTS idx_earn_referrer ON referral_earnings(referrer_opex_user, ts DESC);
CREATE INDEX IF NOT EXISTS idx_earn_unpaid   ON referral_earnings(paid_at) WHERE paid_at IS NULL;

CREATE TABLE IF NOT EXISTS referral_campaigns (
  campaign_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT,
  start_at INTEGER NOT NULL,
  end_at INTEGER NOT NULL,
  signup_bonus_usdt TEXT NOT NULL DEFAULT '0',
  kyc_bonus_usdt TEXT NOT NULL DEFAULT '0',
  first_trade_bonus_usdt TEXT NOT NULL DEFAULT '0',
  first_trade_min_volume_usdt TEXT NOT NULL DEFAULT '50',
  fee_share_lvl1_bps INTEGER NOT NULL DEFAULT 2000,
  fee_share_lvl2_bps INTEGER NOT NULL DEFAULT 500,
  max_total_per_referrer_usdt TEXT,
  active INTEGER NOT NULL DEFAULT 1
);
"""

DEFAULT_CAMPAIGN = (
    "FOREVER",
    "Forever Bonus",
    "Standard referral rewards",
    0,
    2**31,
    "10",
    "10",
    "20",
    "50",
    2000,
    500,
    "10000",
)


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _db_lock, db() as c:
        c.executescript(SCHEMA)
        row = c.execute(
            "SELECT campaign_id FROM referral_campaigns WHERE campaign_id=?",
            (DEFAULT_CAMPAIGN[0],),
        ).fetchone()
        if not row:
            c.execute(
                "INSERT INTO referral_campaigns (campaign_id, name, description,"
                " start_at, end_at, signup_bonus_usdt, kyc_bonus_usdt,"
                " first_trade_bonus_usdt, first_trade_min_volume_usdt,"
                " fee_share_lvl1_bps, fee_share_lvl2_bps,"
                " max_total_per_referrer_usdt, active)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
                DEFAULT_CAMPAIGN,
            )
            log(f"seeded default campaign {DEFAULT_CAMPAIGN[0]}")


# ===========================================================================
# Helpers
# ===========================================================================
CODE_RE = re.compile(r"^[A-Z0-9]{4,12}$")


def normalize_code(raw: str) -> str:
    return (raw or "").strip().upper()


def gen_code(opex_user: str) -> str:
    """Generate a system code: short alpha prefix + numeric suffix.

    Always 8 chars. Collision-retry on the PRIMARY KEY constraint.
    """
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # drop 0/O/1/I for human-readability
    for _ in range(8):
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        if CODE_RE.match(code):
            return code
    # fall back (extremely unlikely): include a piece of the opex_user hash
    return ("Z" + secrets.token_hex(4).upper())[:8]


def D(x) -> Decimal:
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal("0")


def dstr(x: Decimal) -> str:
    # Render with up to 8 decimal places, no trailing zeros / exponents.
    s = format(x.normalize(), "f") if x != 0 else "0"
    return s


def redact_opex(opex_user: str) -> str:
    if not opex_user:
        return "u-***"
    if "-" in opex_user:
        head, _ = opex_user.split("-", 1)
        return f"{head}-***"
    return f"{opex_user[:3]}***"


def now_ts() -> int:
    return int(time.time())


def is_loopback(handler: http.server.BaseHTTPRequestHandler) -> bool:
    try:
        ip = handler.client_address[0]
    except Exception:
        return False
    return ip in ("127.0.0.1", "::1", "localhost") or ip.startswith("127.")


# ===========================================================================
# Auth (delegated to auth_server /auth/me)
# ===========================================================================
def resolve_bearer(token: str | None) -> dict | None:
    if not token:
        return None
    req = _http_request(
        f"{AUTH_BASE}/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with _http_urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
            user = data.get("user") or {}
            if not user.get("opex_user"):
                return None
            return user
    except urllib.error.HTTPError:
        return None
    except Exception as e:
        log(f"auth lookup failed: {e!r}")
        return None


# ===========================================================================
# Campaign accessor
# ===========================================================================
def active_campaign() -> dict | None:
    now = now_ts()
    with db() as c:
        row = c.execute(
            "SELECT * FROM referral_campaigns WHERE active=1"
            " AND start_at <= ? AND end_at >= ?"
            " ORDER BY start_at DESC LIMIT 1",
            (now, now),
        ).fetchone()
    return dict(row) if row else None


# ===========================================================================
# Code issuance
# ===========================================================================
def ensure_code(opex_user: str) -> dict:
    with _db_lock, db() as c:
        row = c.execute("SELECT * FROM referral_codes WHERE opex_user=?", (opex_user,)).fetchone()
        if row:
            return dict(row)
        # generate w/ collision retry
        for _ in range(20):
            code = gen_code(opex_user)
            try:
                c.execute(
                    "INSERT INTO referral_codes (code, opex_user, custom_name, created_at)"
                    " VALUES (?,?,0,?)",
                    (code, opex_user, now_ts()),
                )
                row = c.execute(
                    "SELECT * FROM referral_codes WHERE opex_user=?", (opex_user,)
                ).fetchone()
                return dict(row)
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("could not allocate a referral code after 20 attempts")


def customize_code(opex_user: str, new_code: str) -> tuple[bool, str, dict | None]:
    nc = normalize_code(new_code)
    if not CODE_RE.match(nc):
        return False, "invalid_code", None
    with _db_lock, db() as c:
        row = c.execute("SELECT * FROM referral_codes WHERE opex_user=?", (opex_user,)).fetchone()
        if not row:
            return False, "no_code", None
        if int(row["custom_name"]) == 1:
            return False, "already_customized", dict(row)
        # uniqueness
        clash = c.execute("SELECT 1 FROM referral_codes WHERE code=?", (nc,)).fetchone()
        if clash:
            return False, "code_taken", dict(row)
        c.execute(
            "UPDATE referral_codes SET code=?, custom_name=1 WHERE opex_user=?",
            (nc, opex_user),
        )
        updated = c.execute(
            "SELECT * FROM referral_codes WHERE opex_user=?", (opex_user,)
        ).fetchone()
    return True, "ok", dict(updated)


# ===========================================================================
# Apply / signup wiring
# ===========================================================================
def apply_code(
    code: str, referee_opex_user: str, signup_ip_redacted: str | None = None
) -> tuple[bool, str, dict | None]:
    code = normalize_code(code)
    if not CODE_RE.match(code):
        return False, "invalid_code", None
    if not referee_opex_user:
        return False, "missing_referee", None
    with _db_lock, db() as c:
        owner = c.execute(
            "SELECT opex_user FROM referral_codes WHERE code=? AND active=1", (code,)
        ).fetchone()
        if not owner:
            return False, "code_not_found", None
        referrer = owner["opex_user"]
        if referrer == referee_opex_user:
            return False, "self_referral", None
        existing = c.execute(
            "SELECT id FROM referral_relationships WHERE referee_opex_user=?",
            (referee_opex_user,),
        ).fetchone()
        if existing:
            return False, "already_referred", None
        c.execute(
            "INSERT INTO referral_relationships (referrer_opex_user, referee_opex_user,"
            " code_used, referee_signup_at, status, signup_ip_redacted)"
            " VALUES (?,?,?,?, 'pending', ?)",
            (referrer, referee_opex_user, code, now_ts(), signup_ip_redacted),
        )
        c.execute(
            "UPDATE referral_codes SET total_signups = total_signups + 1 WHERE code=?",
            (code,),
        )
        rel = c.execute(
            "SELECT * FROM referral_relationships WHERE referee_opex_user=?",
            (referee_opex_user,),
        ).fetchone()
    # Schedule the signup bonus immediately (the bonus is unconditional —
    # the more restrictive KYC + first-trade events fire later).
    camp = active_campaign()
    if camp:
        bonus = D(camp.get("signup_bonus_usdt") or "0")
        if bonus > 0:
            _record_earning(referrer, referee_opex_user, "signup_bonus", bonus, None, None)
    return True, "ok", dict(rel)


def record_kyc(referee_opex_user: str) -> tuple[bool, str]:
    if not referee_opex_user:
        return False, "missing_referee"
    with _db_lock, db() as c:
        row = c.execute(
            "SELECT * FROM referral_relationships WHERE referee_opex_user=?",
            (referee_opex_user,),
        ).fetchone()
        if not row:
            return False, "no_relationship"
        if row["referee_kyc_verified_at"]:
            return True, "already_recorded"
        c.execute(
            "UPDATE referral_relationships SET referee_kyc_verified_at=? WHERE id=?",
            (now_ts(), row["id"]),
        )
        referrer = row["referrer_opex_user"]
    camp = active_campaign()
    if camp:
        bonus = D(camp.get("kyc_bonus_usdt") or "0")
        if bonus > 0:
            _record_earning(referrer, referee_opex_user, "kyc_bonus", bonus, None, None)
    return True, "ok"


def record_first_trade(referee_opex_user: str, notional_usdt: Decimal) -> tuple[bool, str]:
    if not referee_opex_user:
        return False, "missing_referee"
    with _db_lock, db() as c:
        row = c.execute(
            "SELECT * FROM referral_relationships WHERE referee_opex_user=?",
            (referee_opex_user,),
        ).fetchone()
        if not row:
            return False, "no_relationship"
        if row["referee_first_trade_at"]:
            return True, "already_recorded"
        camp = active_campaign()
        min_v = D(camp["first_trade_min_volume_usdt"]) if camp else FIRST_TRADE_MIN_USDT
        if notional_usdt < min_v:
            return False, "below_minimum"
        c.execute(
            "UPDATE referral_relationships SET referee_first_trade_at=?, status='qualified'"
            " WHERE id=?",
            (now_ts(), row["id"]),
        )
        referrer = row["referrer_opex_user"]
    if camp:
        bonus = D(camp.get("first_trade_bonus_usdt") or "0")
        if bonus > 0:
            _record_earning(referrer, referee_opex_user, "first_trade_bonus", bonus, None, None)
    return True, "ok"


# ===========================================================================
# Earnings ledger + caps
# ===========================================================================
def _today_paid_usdt(referrer_opex_user: str, conn) -> Decimal:
    cutoff = now_ts() - 86400
    rows = conn.execute(
        "SELECT amount_usdt FROM referral_earnings" " WHERE referrer_opex_user=? AND ts>=?",
        (referrer_opex_user, cutoff),
    ).fetchall()
    total = Decimal("0")
    for r in rows:
        total += D(r["amount_usdt"])
    return total


def _program_lifetime_usdt(conn) -> Decimal:
    rows = conn.execute(
        "SELECT SUM(CAST(amount_usdt AS REAL)) AS s FROM referral_earnings"
    ).fetchone()
    if not rows or rows["s"] is None:
        return Decimal("0")
    return D(rows["s"])


def _record_earning(
    referrer: str,
    referee: str,
    earning_type: str,
    amount: Decimal,
    trade_id: str | None,
    related_fee: Decimal | None,
) -> None:
    if amount <= 0:
        return
    with _db_lock, db() as c:
        # Caps. Per-referrer daily.
        today = _today_paid_usdt(referrer, c)
        if today + amount > PER_REFERRER_DAILY_CAP_USDT:
            remaining = PER_REFERRER_DAILY_CAP_USDT - today
            if remaining <= 0:
                log(f"daily cap hit for {referrer}, skipping {earning_type} {amount}")
                return
            amount = remaining
        # Program lifetime.
        lifetime = _program_lifetime_usdt(c)
        if lifetime + amount > PROGRAM_LIFETIME_CAP_USDT:
            remaining = PROGRAM_LIFETIME_CAP_USDT - lifetime
            if remaining <= 0:
                log(f"program lifetime cap hit, skipping {earning_type} {amount}")
                return
            amount = remaining
        c.execute(
            "INSERT INTO referral_earnings (ts, referrer_opex_user, referee_opex_user,"
            " earning_type, amount_usdt, trade_id, related_fee_usdt, paid_at, payout_tx)"
            " VALUES (?,?,?,?,?,?,?, NULL, NULL)",
            (
                now_ts(),
                referrer,
                referee,
                earning_type,
                dstr(amount),
                trade_id,
                dstr(related_fee) if related_fee is not None else None,
            ),
        )
        c.execute(
            "UPDATE referral_codes SET total_earnings_usdt = CAST("
            " (CAST(total_earnings_usdt AS REAL) + CAST(? AS REAL)) AS TEXT)"
            " WHERE opex_user=?",
            (dstr(amount), referrer),
        )


def record_fee(referee: str, fee_usdt: Decimal, trade_id: str | None) -> dict:
    """Compute level-1 + level-2 fee shares on a referee trade fee."""
    if fee_usdt <= 0:
        return {"recorded": False, "reason": "non_positive_fee"}
    camp = active_campaign()
    if not camp:
        return {"recorded": False, "reason": "no_active_campaign"}
    lvl1_bps = int(camp.get("fee_share_lvl1_bps") or 0)
    lvl2_bps = int(camp.get("fee_share_lvl2_bps") or 0)

    out = {"recorded": False, "level_1": None, "level_2": None}
    # find direct referrer
    with db() as c:
        rel = c.execute(
            "SELECT referrer_opex_user FROM referral_relationships WHERE referee_opex_user=?",
            (referee,),
        ).fetchone()
    if not rel:
        return {"recorded": False, "reason": "not_referred"}
    referrer1 = rel["referrer_opex_user"]
    # also update aggregate volume best-effort
    # (we don't get notional here; track only the fee × bps proxy for now)
    amt1 = (fee_usdt * Decimal(lvl1_bps) / Decimal(10000)).quantize(Decimal("0.00000001"))
    if amt1 > 0:
        _record_earning(referrer1, referee, "fee_share_lvl1", amt1, trade_id, fee_usdt)
        out["level_1"] = {"referrer": referrer1, "amount_usdt": dstr(amt1)}
    out["recorded"] = True

    # level 2 = the referrer's referrer
    with db() as c:
        rel2 = c.execute(
            "SELECT referrer_opex_user FROM referral_relationships WHERE referee_opex_user=?",
            (referrer1,),
        ).fetchone()
    if rel2 and lvl2_bps > 0:
        referrer2 = rel2["referrer_opex_user"]
        if referrer2 and referrer2 != referee:
            amt2 = (fee_usdt * Decimal(lvl2_bps) / Decimal(10000)).quantize(Decimal("0.00000001"))
            if amt2 > 0:
                _record_earning(referrer2, referee, "fee_share_lvl2", amt2, trade_id, fee_usdt)
                out["level_2"] = {"referrer": referrer2, "amount_usdt": dstr(amt2)}
    return out


# ===========================================================================
# Payout worker
# ===========================================================================
def credit_wallet(opex_user: str, amount_usdt: Decimal, *, ref: str) -> str | None:
    """Credit the referrer's MAIN wallet with USDT. Returns the payout ref on success.

    The demo wallet's /deposit endpoint takes integer units capped at 10/call,
    so we floor + chunk. Fractional cents are accrued on the ledger only.
    """
    units = int(amount_usdt)
    if units <= 0:
        # We still mark "paid" with a placeholder so accrual under 1 USDT
        # doesn't get retried forever.
        return f"accrued-{ref}"
    remaining = units
    chunk_i = 0
    # Add a nonce so retries after a partial failure don't collide on the
    # wallet API's transferRef uniqueness constraint.
    nonce = secrets.token_hex(4)
    while remaining > 0:
        amt = min(10, remaining)
        chunk_ref = f"{ref}-{nonce}-{chunk_i}"
        path = (
            f"/deposit/{amt}_test-ethereum_{PAYOUT_ASSET_INTERNAL}/"
            f"{urllib.parse.quote(opex_user)}_MAIN"
            f"?description=zkcex-referral-payout"
            f"&transferRef={urllib.parse.quote(chunk_ref)}"
        )
        url = f"{WALLET_BASE}{path}"
        req = _http_request(url, method="POST")
        try:
            with _http_urlopen(req, timeout=10) as resp:
                if resp.status >= 300:
                    return None
        except Exception as e:
            log(f"wallet credit {url} failed: {e!r}")
            return None
        remaining -= amt
        chunk_i += 1
    return f"wallet-{ref}-{nonce}"


def push_notify(opex_user: str, payload: dict) -> None:
    try:
        body = json.dumps({"opex_user": opex_user, "payload": payload}).encode("utf-8")
        req = _http_request(
            f"{PUSH_BASE}/push/send",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with _http_urlopen(req, timeout=4) as resp:
            resp.read()
    except Exception as e:  # noqa: BLE001
        # never block payouts on push delivery
        log(f"push notify skipped: {e!r}")


def payout_tick() -> dict:
    if not _payout_lock.acquire(blocking=False):
        return {"skipped": True}
    paid_n = 0
    paid_total = Decimal("0")
    try:
        with db() as c:
            unpaid = c.execute(
                "SELECT id, referrer_opex_user, referee_opex_user, earning_type,"
                " amount_usdt FROM referral_earnings WHERE paid_at IS NULL"
                " ORDER BY id ASC LIMIT 50"
            ).fetchall()
        for r in unpaid:
            amt = D(r["amount_usdt"])
            ref = f"earn-{r['id']}"
            tx = credit_wallet(r["referrer_opex_user"], amt, ref=ref)
            if not tx:
                continue
            with _db_lock, db() as c:
                c.execute(
                    "UPDATE referral_earnings SET paid_at=?, payout_tx=? WHERE id=?",
                    (now_ts(), tx, r["id"]),
                )
            paid_n += 1
            paid_total += amt
            push_notify(
                r["referrer_opex_user"],
                {
                    "title": "리퍼럴 보상 / Referral payout",
                    "body": f"You earned ${dstr(amt)} from {redact_opex(r['referee_opex_user'])} ({r['earning_type']}).",
                    "tag": "referral-payout",
                },
            )
    except Exception as e:
        log(f"payout tick error: {e!r}")
    finally:
        _payout_lock.release()
    return {"paid_n": paid_n, "paid_total_usdt": dstr(paid_total)}


def payout_loop() -> None:
    while True:
        try:
            payout_tick()
        except Exception as e:
            log(f"payout loop fatal: {e!r}")
        time.sleep(PAYOUT_INTERVAL_S)


# ===========================================================================
# Stats / leaderboard
# ===========================================================================
def build_stats(opex_user: str) -> dict:
    code_row = ensure_code(opex_user)
    code = code_row["code"]
    with db() as c:
        rels = c.execute(
            "SELECT * FROM referral_relationships WHERE referrer_opex_user=?"
            " ORDER BY id DESC LIMIT 50",
            (opex_user,),
        ).fetchall()
        n_total = c.execute(
            "SELECT COUNT(*) AS n FROM referral_relationships WHERE referrer_opex_user=?",
            (opex_user,),
        ).fetchone()["n"]
        n_qual = c.execute(
            "SELECT COUNT(*) AS n FROM referral_relationships"
            " WHERE referrer_opex_user=? AND status='qualified'",
            (opex_user,),
        ).fetchone()["n"]
        breakdown_rows = c.execute(
            "SELECT earning_type, SUM(CAST(amount_usdt AS REAL)) AS s"
            " FROM referral_earnings WHERE referrer_opex_user=?"
            " GROUP BY earning_type",
            (opex_user,),
        ).fetchall()
        total_earn = c.execute(
            "SELECT SUM(CAST(amount_usdt AS REAL)) AS s FROM referral_earnings"
            " WHERE referrer_opex_user=?",
            (opex_user,),
        ).fetchone()
        cutoff_30d = now_ts() - 30 * 86400
        month_earn = c.execute(
            "SELECT SUM(CAST(amount_usdt AS REAL)) AS s FROM referral_earnings"
            " WHERE referrer_opex_user=? AND ts >= ?",
            (opex_user, cutoff_30d),
        ).fetchone()
        per_rel_earn = {}
        if rels:
            ids = [r["referee_opex_user"] for r in rels]
            placeholders = ",".join("?" * len(ids))
            erows = c.execute(
                f"SELECT referee_opex_user, SUM(CAST(amount_usdt AS REAL)) AS s"  # noqa: S608
                f" FROM referral_earnings WHERE referrer_opex_user=?"
                f"   AND referee_opex_user IN ({placeholders})"
                f" GROUP BY referee_opex_user",
                (opex_user, *ids),
            ).fetchall()
            for er in erows:
                per_rel_earn[er["referee_opex_user"]] = D(er["s"])
        # total volume — we don't store notional directly; approximate from
        # related_fee × 100 (1% spot taker). Stored as exchange-side proxy only.
        vol_rows = c.execute(
            "SELECT SUM(CAST(related_fee_usdt AS REAL)) AS s FROM referral_earnings"
            " WHERE referrer_opex_user=? AND related_fee_usdt IS NOT NULL",
            (opex_user,),
        ).fetchone()
        approx_vol = (
            D(vol_rows["s"]) * Decimal("100") if vol_rows and vol_rows["s"] else Decimal("0")
        )

    breakdown = {
        "signup_bonus": "0",
        "kyc_bonus": "0",
        "first_trade_bonus": "0",
        "fee_share_lvl1": "0",
        "fee_share_lvl2": "0",
    }
    for r in breakdown_rows:
        breakdown[r["earning_type"]] = dstr(D(r["s"]))

    recent = []
    for r in rels:
        recent.append(
            {
                "referee": redact_opex(r["referee_opex_user"]),
                "signup_at": r["referee_signup_at"],
                "kyc_at": r["referee_kyc_verified_at"],
                "first_trade_at": r["referee_first_trade_at"],
                "status": r["status"],
                "earned_usdt": dstr(per_rel_earn.get(r["referee_opex_user"], Decimal("0"))),
            }
        )

    share_link = f"/app/signin.html?tab=signup&ref={urllib.parse.quote(code)}"
    return {
        "code": code,
        "custom_name": bool(int(code_row["custom_name"])),
        "share_link": share_link,
        "total_signups": n_total,
        "qualified_signups": n_qual,
        "total_referred_volume_usdt": dstr(approx_vol),
        "total_earnings_usdt": dstr(
            D(total_earn["s"]) if total_earn["s"] is not None else Decimal("0")
        ),
        "month_earnings_usdt": dstr(
            D(month_earn["s"]) if month_earn["s"] is not None else Decimal("0")
        ),
        "lifetime_breakdown": breakdown,
        "recent_referrals": recent,
        "caps": {
            "per_day_usdt": dstr(PER_REFERRER_DAILY_CAP_USDT),
            "program_lifetime_usdt": dstr(PROGRAM_LIFETIME_CAP_USDT),
        },
    }


def leaderboard(limit: int = 20) -> dict:
    limit = max(1, min(100, int(limit)))
    cutoff = now_ts() - 30 * 86400
    with db() as c:
        rows = c.execute(
            "SELECT referrer_opex_user, SUM(CAST(amount_usdt AS REAL)) AS s,"
            " COUNT(DISTINCT referee_opex_user) AS n_referees"
            " FROM referral_earnings WHERE ts >= ?"
            " GROUP BY referrer_opex_user"
            " ORDER BY s DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
    out = []
    for i, r in enumerate(rows, 1):
        out.append(
            {
                "rank": i,
                "referrer": redact_opex(r["referrer_opex_user"]),
                "earnings_30d_usdt": dstr(D(r["s"])),
                "n_referees": r["n_referees"],
            }
        )
    return {"window_days": 30, "limit": limit, "leaderboard": out, "count": len(out)}


# ===========================================================================
# HTTP
# ===========================================================================
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "zkcex-referral/1.0"

    def _send(self, status: int, payload):
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
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _bearer(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        if not auth.lower().startswith("bearer "):
            return None
        return auth.split(None, 1)[1].strip()

    def _require_user(self) -> dict | None:
        user = resolve_bearer(self._bearer())
        if not user:
            self._send(401, {"error": "unauthorized"})
            return None
        return user

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[referral] {self.address_string()} - {fmt % args}\n")

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query or "")
        if path == "/referral/health":
            return self._send(
                200,
                {
                    "ok": True,
                    "uptime_s": now_ts() - START_TS,
                    "payout_interval_s": PAYOUT_INTERVAL_S,
                },
            )
        if path == "/referral/my-code":
            user = self._require_user()
            if not user:
                return
            row = ensure_code(user["opex_user"])
            return self._send(
                200,
                {
                    "code": row["code"],
                    "custom_name": bool(int(row["custom_name"])),
                    "share_link": f"/app/signin.html?tab=signup&ref={urllib.parse.quote(row['code'])}",
                    "created_at": row["created_at"],
                },
            )
        if path == "/referral/stats":
            user = self._require_user()
            if not user:
                return
            return self._send(200, build_stats(user["opex_user"]))
        if path == "/referral/leaderboard":
            try:
                limit = int((q.get("limit") or ["20"])[0])
            except ValueError:
                limit = 20
            return self._send(200, leaderboard(limit))
        if path == "/referral/campaigns":
            camp = active_campaign()
            return self._send(200, {"active": camp})
        return self._send(404, {"error": "not_found", "path": path})

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/referral/customize":
            user = self._require_user()
            if not user:
                return
            body = self._read_json()
            ok, reason, row = customize_code(user["opex_user"], body.get("custom_code") or "")
            if not ok:
                return self._send(
                    400 if reason in ("invalid_code", "no_code") else 409,
                    {"error": reason},
                )
            return self._send(
                200,
                {
                    "ok": True,
                    "code": row["code"],
                    "custom_name": bool(int(row["custom_name"])),
                },
            )
        if path == "/referral/apply":
            if not is_loopback(self):
                return self._send(403, {"error": "loopback_only"})
            body = self._read_json()
            ok, reason, rel = apply_code(
                body.get("code") or "",
                body.get("opex_user") or "",
                body.get("signup_ip_redacted"),
            )
            if not ok:
                # 200 + applied:false so signup keeps succeeding when the code
                # is just wrong / unknown / self-referral. We only 400 on
                # outright malformed inputs.
                return self._send(200, {"applied": False, "reason": reason})
            return self._send(
                200,
                {
                    "applied": True,
                    "relationship_id": rel["id"],
                    "referrer_opex_user_redacted": redact_opex(rel["referrer_opex_user"]),
                    "code": rel["code_used"],
                },
            )
        if path == "/referral/internal/record-signup":
            if not is_loopback(self):
                return self._send(403, {"error": "loopback_only"})
            body = self._read_json()
            ok, reason, rel = apply_code(
                body.get("code") or "",
                body.get("opex_user") or "",
                body.get("signup_ip_redacted"),
            )
            return self._send(200, {"applied": ok, "reason": reason})
        if path == "/referral/internal/record-kyc":
            if not is_loopback(self):
                return self._send(403, {"error": "loopback_only"})
            body = self._read_json()
            ok, reason = record_kyc((body.get("referee_opex_user") or "").strip())
            return self._send(200, {"ok": ok, "reason": reason})
        if path == "/referral/internal/record-first-trade":
            if not is_loopback(self):
                return self._send(403, {"error": "loopback_only"})
            body = self._read_json()
            try:
                notional = D(body.get("notional_usdt", "0"))
            except Exception:
                return self._send(400, {"error": "bad_notional"})
            ok, reason = record_first_trade((body.get("referee_opex_user") or "").strip(), notional)
            return self._send(200, {"ok": ok, "reason": reason})
        if path == "/referral/internal/record-fee":
            if not is_loopback(self):
                return self._send(403, {"error": "loopback_only"})
            body = self._read_json()
            referee = (body.get("referee_opex_user") or "").strip()
            try:
                fee = D(body.get("fee_usdt", "0"))
            except Exception:
                return self._send(400, {"error": "bad_fee"})
            trade_id = body.get("trade_id")
            # Side-effect: if this is the referee's first trade-with-volume,
            # also fire the first-trade bonus. notional ≈ fee × 100 (1% spot).
            approx_notional = fee * Decimal("100")
            record_first_trade(referee, approx_notional)
            return self._send(
                200,
                record_fee(referee, fee, str(trade_id) if trade_id else None),
            )
        if path == "/referral/internal/payout-now":
            if not is_loopback(self):
                return self._send(403, {"error": "loopback_only"})
            return self._send(200, payout_tick())
        return self._send(404, {"error": "not_found", "path": path})


# ===========================================================================
# Main
# ===========================================================================
class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5695
    init_db()
    threading.Thread(target=payout_loop, daemon=True).start()
    httpd = ThreadingServer((LISTEN_HOST, port), Handler)
    log(f"listening on {LISTEN_HOST}:{port}")
    log(f"auth upstream={AUTH_BASE}  wallet={WALLET_BASE}  push={PUSH_BASE}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("shutdown")


if __name__ == "__main__":
    main()
