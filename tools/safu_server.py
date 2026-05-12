#!/usr/bin/env python3
"""SAFU (Secure Asset Fund for Users) self-insurance ledger (port 5601).

Stdlib-only accounting service that periodically absorbs 10% of every
trading fee on the spot + perp engines into a transparent on-ledger fund.
Backstops users against:

  * security incidents (hacks, key compromise),
  * system errors (matching engine bugs, accounting glitches),
  * liquidation cascades (perp-engine insurance fund running dry),
  * goodwill recoveries for affected users.

CAVEAT — accounting only
------------------------
This service does NOT move real funds out of the wallet API; it is a
*notional ledger* against the exchange's general-ledger balance. In
production a real wallet transfer to a cold-stored SAFU address would
happen here; for the demo we accrue the obligation on a separate
sqlite database and surface it publicly. Payouts are real (they call
chain_server to debit from the operational wallet) but inflows are
purely an accounting entry derived from upstream trade data.

The signing key is shared with pol_server (``tools/.local/pol_signing_key``)
so external verifiers see one custodian pubkey signing both PoL roots
and SAFU attestations. If the file is missing/unreadable, a separate
key is generated and a warning is emitted.

Persisted at ``tools/.local/safu.db``.
"""

from __future__ import annotations

import hashlib
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
from decimal import Decimal, getcontext

getcontext().prec = 36

# --- Paths ----------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join(HERE, ".local")
os.makedirs(LOCAL_DIR, exist_ok=True)
DB_PATH = os.path.join(LOCAL_DIR, "safu.db")
POL_KEY_PATH = os.path.join(LOCAL_DIR, "pol_signing_key")
SAFU_KEY_PATH = os.path.join(LOCAL_DIR, "safu_signing_key")
PERP_DB_PATH = os.path.join(LOCAL_DIR, "perp.db")


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


MARKET_BASE = _validated_http_base_url(
    "SAFU_MARKET_BASE", os.environ.get("SAFU_MARKET_BASE", "http://127.0.0.1:8094")
)
TICKER_BASE = _validated_http_base_url(
    "SAFU_TICKER_BASE", os.environ.get("SAFU_TICKER_BASE", "http://127.0.0.1:8094")
)
CHAIN_BASE = _validated_http_base_url(
    "SAFU_CHAIN_BASE", os.environ.get("SAFU_CHAIN_BASE", "http://127.0.0.1:5502")
)
SKIM_INTERVAL_S = int(os.environ.get("SAFU_SKIM_INTERVAL_S", "30"))
ATTEST_INTERVAL_S = int(os.environ.get("SAFU_ATTEST_INTERVAL_S", "60"))
SAFU_FRACTION = Decimal(os.environ.get("SAFU_FRACTION", "0.10"))  # 10% of every fee
# Spot fee from /v3/exchangeInfo is 1% (0.01) for all pairs in the demo.
# The matching engine doesn't surface a per-fill commission in /v3/trades,
# so we re-derive it from quoteQty * SPOT_TAKER_FEE.
SPOT_TAKER_FEE = Decimal(os.environ.get("SAFU_SPOT_TAKER_FEE", "0.01"))
PERP_TAKER_FEE = Decimal(os.environ.get("SAFU_PERP_TAKER_FEE", "0.0004"))
PERP_MAKER_FEE = Decimal(os.environ.get("SAFU_PERP_MAKER_FEE", "0.0002"))
PRICE_TTL_S = 30
SCHEME_NAME = "zkcex-safu-v1"
NOTIF_BASE = _validated_http_base_url(
    "NOTIF_BASE", os.environ.get("NOTIF_BASE", "http://127.0.0.1:5691")
)


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


# Token name conversions (the demo's hardhat tokens vs ticker symbols).
TOKEN_TO_ASSET = {"ZETH": "ETH", "ZUSDT": "USDT"}

# Active symbols to poll for spot trades. We discover from exchangeInfo on
# first run; if that fails we fall back to the canonical demo list.
DEFAULT_SPOT_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "DOGEUSDT",
    "TONUSDT",
)

# Per-tick skim caps (defensive — keep the API call cost predictable even
# if the exchange has been quiet for a long time and we have to backfill).
SPOT_TRADES_PER_TICK = 500
PERP_ROWS_PER_TICK = 500

START_TS = int(time.time())

_db_lock = threading.Lock()
_skim_lock = threading.Lock()  # only one skimmer run at a time
_attest_lock = threading.Lock()
_price_cache_lock = threading.Lock()
_price_cache: dict[str, tuple[float, float]] = {}

_runtime_state: dict[str, object] = {
    "last_skim_at": 0,
    "last_skim_ok": True,
    "last_skim_error": None,
    "last_attest_at": 0,
    "spot_symbols": list(DEFAULT_SPOT_SYMBOLS),
}


def log(msg: str) -> None:
    sys.stderr.write(f"[safu] {msg}\n")
    sys.stderr.flush()


# ==========================================================================
# Ed25519 — copied from pol_server.py (pure stdlib, RFC 8032 reference impl).
# Keeping a local copy lets safu_server run standalone if pol_server hasn't
# been imported. Logic must stay identical so signatures cross-verify.
# ==========================================================================
_ED_b = 256
_ED_q = 2**255 - 19
_ED_l = 2**252 + 27742317777372353535851937790883648493
_ED_d = -121665 * pow(121666, _ED_q - 2, _ED_q) % _ED_q
_ED_I = pow(2, (_ED_q - 1) // 4, _ED_q)


def _ed_H(m: bytes) -> bytes:
    return hashlib.sha512(m).digest()


def _ed_xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_ED_d * y * y + 1, _ED_q - 2, _ED_q)
    x = pow(xx, (_ED_q + 3) // 8, _ED_q)
    if (x * x - xx) % _ED_q != 0:
        x = (x * _ED_I) % _ED_q
    if x % 2 != 0:
        x = _ED_q - x
    return x


_ED_By = 4 * pow(5, _ED_q - 2, _ED_q) % _ED_q
_ED_Bx = _ed_xrecover(_ED_By)
_ED_B = (_ED_Bx % _ED_q, _ED_By % _ED_q)


def _ed_edwards(P, Q):
    x1, y1 = P
    x2, y2 = Q
    inv = pow(1 + _ED_d * x1 * x2 * y1 * y2, _ED_q - 2, _ED_q)
    x3 = ((x1 * y2 + x2 * y1) * inv) % _ED_q
    inv2 = pow(1 - _ED_d * x1 * x2 * y1 * y2, _ED_q - 2, _ED_q)
    y3 = ((y1 * y2 + x1 * x2) * inv2) % _ED_q
    return (x3, y3)


def _ed_scalarmult(P, e):
    if e == 0:
        return (0, 1)
    Q = _ed_scalarmult(P, e // 2)
    Q = _ed_edwards(Q, Q)
    if e & 1:
        Q = _ed_edwards(Q, P)
    return Q


def _ed_encodeint(y: int) -> bytes:
    bits = [(y >> i) & 1 for i in range(_ED_b)]
    return bytes(sum(bits[i * 8 + j] << j for j in range(8)) for i in range(_ED_b // 8))


def _ed_encodepoint(P) -> bytes:
    x, y = P
    bits = [(y >> i) & 1 for i in range(_ED_b - 1)] + [x & 1]
    return bytes(sum(bits[i * 8 + j] << j for j in range(8)) for i in range(_ED_b // 8))


def _ed_bit(h: bytes, i: int) -> int:
    return (h[i // 8] >> (i % 8)) & 1


def ed25519_publickey(sk_seed: bytes) -> bytes:
    h = _ed_H(sk_seed)
    a = 2 ** (_ED_b - 2) + sum(2**i * _ed_bit(h, i) for i in range(3, _ED_b - 2))
    A = _ed_scalarmult(_ED_B, a)
    return _ed_encodepoint(A)


def _ed_Hint(m: bytes) -> int:
    h = _ed_H(m)
    return sum(2**i * _ed_bit(h, i) for i in range(2 * _ED_b))


def ed25519_sign(sk_seed: bytes, msg: bytes) -> bytes:
    h = _ed_H(sk_seed)
    a = 2 ** (_ED_b - 2) + sum(2**i * _ed_bit(h, i) for i in range(3, _ED_b - 2))
    A = _ed_encodepoint(_ed_scalarmult(_ED_B, a))
    r = _ed_Hint(h[_ED_b // 8 : _ED_b // 4] + msg)
    R = _ed_scalarmult(_ED_B, r)
    S = (r + _ed_Hint(_ed_encodepoint(R) + A + msg) * a) % _ED_l
    return _ed_encodepoint(R) + _ed_encodeint(S)


def _ed_decodeint(s: bytes) -> int:
    return sum(2**i * _ed_bit(s, i) for i in range(_ED_b))


def _ed_decodepoint(s: bytes):
    y = sum(2**i * _ed_bit(s, i) for i in range(_ED_b - 1))
    x = _ed_xrecover(y)
    if x & 1 != _ed_bit(s, _ED_b - 1):
        x = _ED_q - x
    P = (x, y)
    if not _ed_isoncurve(P):
        raise ValueError("decoding point that is not on curve")
    return P


def _ed_isoncurve(P) -> bool:
    x, y = P
    return (-x * x + y * y - 1 - _ED_d * x * x * y * y) % _ED_q == 0


def ed25519_verify(pk: bytes, msg: bytes, sig: bytes) -> bool:
    if len(sig) != _ED_b // 4 or len(pk) != _ED_b // 8:
        return False
    try:
        R = _ed_decodepoint(sig[: _ED_b // 8])
        A = _ed_decodepoint(pk)
        S = _ed_decodeint(sig[_ED_b // 8 : _ED_b // 4])
        h = _ed_Hint(_ed_encodepoint(R) + pk + msg)
        return _ed_scalarmult(_ED_B, S) == _ed_edwards(R, _ed_scalarmult(A, h))
    except Exception:
        return False


# ==========================================================================
# Key loading
# ==========================================================================
_KEY_CACHE: dict[str, object] = {}


def load_signing_key() -> tuple[bytes, bytes, str, bool]:
    """Returns (seed, pubkey, sig_scheme, shared_with_pol).

    Prefer the pol_server key so an external verifier sees one custodian
    pubkey across PoL roots and SAFU attestations. Fall back to a separate
    key file if pol's seed is missing/malformed, and log a warning.
    """
    if _KEY_CACHE.get("seed"):
        return (
            _KEY_CACHE["seed"],  # type: ignore[return-value]
            _KEY_CACHE["pub"],  # type: ignore[return-value]
            "Ed25519",
            bool(_KEY_CACHE.get("shared")),
        )
    seed: bytes | None = None
    shared = False
    if os.path.exists(POL_KEY_PATH):
        try:
            with open(POL_KEY_PATH, "rb") as f:
                buf = f.read()
            if len(buf) == 32:
                seed = buf
                shared = True
            else:
                log(f"WARNING pol signing key has unexpected length {len(buf)}; using local key")
        except OSError as e:
            log(f"WARNING could not read pol signing key ({e!r}); using local key")
    if seed is None:
        if not os.path.exists(SAFU_KEY_PATH):
            seed = secrets.token_bytes(32)
            with open(SAFU_KEY_PATH, "wb") as f:
                f.write(seed)
            try:
                os.chmod(SAFU_KEY_PATH, 0o600)
            except OSError:
                pass
            log(
                "WARNING generated separate SAFU signing key; PoL/SAFU pubkeys diverge "
                "(use HSM-backed shared key in production)"
            )
        with open(SAFU_KEY_PATH, "rb") as f:
            seed = f.read()
            if len(seed) != 32:
                raise RuntimeError(f"bad SAFU signing key length: {len(seed)}")
    pub = ed25519_publickey(seed)
    _KEY_CACHE["seed"] = seed
    _KEY_CACHE["pub"] = pub
    _KEY_CACHE["shared"] = shared
    return seed, pub, "Ed25519", shared


# ==========================================================================
# DB
# ==========================================================================
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def perp_db_ro():
    """Read-only handle to perp_engine's sqlite. None if file missing."""
    if not os.path.exists(PERP_DB_PATH):
        return None
    uri = f"file:{urllib.parse.quote(PERP_DB_PATH)}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=4.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError as e:
        log(f"perp_db open failed: {e!r}")
        return None


SCHEMA = """
CREATE TABLE IF NOT EXISTS safu_balances (
  asset TEXT PRIMARY KEY,
  balance TEXT NOT NULL DEFAULT '0',
  total_inflow TEXT NOT NULL DEFAULT '0',
  total_payout TEXT NOT NULL DEFAULT '0',
  last_updated INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS safu_inflow (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  asset TEXT NOT NULL,
  amount TEXT NOT NULL,
  source TEXT NOT NULL,
  reference TEXT,
  decision_json TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_safu_inflow_src_ref
  ON safu_inflow(source, reference);
CREATE INDEX IF NOT EXISTS idx_safu_inflow_ts ON safu_inflow(ts DESC);
CREATE TABLE IF NOT EXISTS safu_payout (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  asset TEXT NOT NULL,
  amount TEXT NOT NULL,
  recipient_opex_user TEXT,
  incident_id TEXT,
  reason TEXT,
  approved_by TEXT,
  tx_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_safu_payout_inc ON safu_payout(incident_id);
CREATE INDEX IF NOT EXISTS idx_safu_payout_ts ON safu_payout(ts DESC);
CREATE TABLE IF NOT EXISTS safu_incidents (
  incident_id TEXT PRIMARY KEY,
  opened_at INTEGER NOT NULL,
  closed_at INTEGER,
  category TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT,
  total_payout_usdt TEXT NOT NULL DEFAULT '0',
  affected_user_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS idx_safu_incidents_status ON safu_incidents(status);
CREATE TABLE IF NOT EXISTS safu_cursors (
  source TEXT PRIMARY KEY,
  position TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS safu_attestations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_at INTEGER NOT NULL,
  total_balance_usdt TEXT NOT NULL,
  total_inflow_usdt TEXT NOT NULL,
  total_payout_usdt TEXT NOT NULL,
  body_json TEXT NOT NULL,
  signature TEXT NOT NULL,
  server_pubkey TEXT NOT NULL
);
"""


def init_db() -> None:
    with _db_lock, db() as conn:
        conn.executescript(SCHEMA)


# ==========================================================================
# Price helper (USDT-equivalent for non-USDT inflows / balance).
# Falls back gracefully: if we can't price an asset we never make up a value.
# ==========================================================================
def get_price_usdt(asset: str) -> Decimal | None:
    if asset == "USDT":
        return Decimal(1)
    now = time.time()
    with _price_cache_lock:
        cached = _price_cache.get(asset)
        if cached and now - cached[1] < PRICE_TTL_S:
            return Decimal(str(cached[0]))
    try:
        url = f"{TICKER_BASE}/v3/ticker/24h?symbol={asset}USDT"
        with _http_urlopen(url, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, list):
            data = data[0] if data else {}
        last = data.get("lastPrice") or data.get("price")
        if last is None:
            return None
        with _price_cache_lock:
            _price_cache[asset] = (float(last), now)
        return Decimal(str(last))
    except Exception:
        return None


# ==========================================================================
# Decimal helpers
# ==========================================================================
def D(v) -> Decimal:
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(0)


def dstr(v) -> str:
    if not isinstance(v, Decimal):
        v = D(v)
    if v == 0:
        return "0"
    q = v.quantize(Decimal("0.00000001")) if abs(v) >= Decimal("0.00000001") else v
    s = format(q, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


# ==========================================================================
# Cursor helpers (per-source idempotent offsets)
# ==========================================================================
def _get_cursor(conn, source: str) -> str:
    row = conn.execute("SELECT position FROM safu_cursors WHERE source=?", (source,)).fetchone()
    return row["position"] if row else "0"


def _set_cursor(conn, source: str, position: str) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT INTO safu_cursors (source, position, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(source) DO UPDATE SET position=excluded.position, updated_at=excluded.updated_at",
        (source, position, now),
    )


def _all_cursors() -> dict[str, str]:
    with db() as conn:
        rows = conn.execute("SELECT source, position FROM safu_cursors").fetchall()
    return {r["source"]: r["position"] for r in rows}


# ==========================================================================
# Inflow + balance helpers (idempotent on UNIQUE(source, reference))
# ==========================================================================
def _credit_inflow(
    conn, *, ts: int, asset: str, amount: Decimal, source: str, reference: str, decision: dict
) -> bool:
    """Idempotent. Returns True iff a new row was inserted."""
    if amount <= 0:
        return False
    try:
        conn.execute(
            "INSERT INTO safu_inflow (ts, asset, amount, source, reference, decision_json) "
            "VALUES (?,?,?,?,?,?)",
            (
                ts,
                asset,
                dstr(amount),
                source,
                reference,
                json.dumps(decision, separators=(",", ":")),
            ),
        )
    except sqlite3.IntegrityError:
        return False  # duplicate (source, reference)
    row = conn.execute(
        "SELECT balance, total_inflow FROM safu_balances WHERE asset=?", (asset,)
    ).fetchone()
    if row:
        new_bal = D(row["balance"]) + amount
        new_in = D(row["total_inflow"]) + amount
        conn.execute(
            "UPDATE safu_balances SET balance=?, total_inflow=?, last_updated=? WHERE asset=?",
            (dstr(new_bal), dstr(new_in), ts, asset),
        )
    else:
        conn.execute(
            "INSERT INTO safu_balances (asset, balance, total_inflow, total_payout, last_updated) "
            "VALUES (?,?,?, '0', ?)",
            (asset, dstr(amount), dstr(amount), ts),
        )
    return True


def _debit_payout(
    conn,
    *,
    ts: int,
    asset: str,
    amount: Decimal,
    incident_id: str | None,
    recipient: str | None,
    reason: str,
    approved_by: str,
    tx_hash: str | None,
) -> int:
    """Returns the new payout row id. Raises ValueError on insufficient funds."""
    row = conn.execute(
        "SELECT balance, total_payout FROM safu_balances WHERE asset=?", (asset,)
    ).fetchone()
    have = D(row["balance"]) if row else Decimal(0)
    if amount > have:
        raise ValueError(f"insufficient SAFU balance: have {have} {asset}, need {amount}")
    cur = conn.execute(
        "INSERT INTO safu_payout (ts, asset, amount, recipient_opex_user, incident_id, reason, approved_by, tx_hash) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (ts, asset, dstr(amount), recipient, incident_id, reason, approved_by, tx_hash),
    )
    new_bal = have - amount
    new_pay = D(row["total_payout"] if row else Decimal(0)) + amount
    conn.execute(
        "UPDATE safu_balances SET balance=?, total_payout=?, last_updated=? WHERE asset=?",
        (dstr(new_bal), dstr(new_pay), ts, asset),
    )
    return int(cur.lastrowid)


# ==========================================================================
# Discovery: which spot symbols to skim. Pull from /v3/exchangeInfo at boot.
# ==========================================================================
def discover_spot_symbols() -> list[str]:
    try:
        with _http_urlopen(f"{MARKET_BASE}/v3/exchangeInfo", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        syms = []
        for s in data.get("symbols", []):
            if s.get("status") != "TRADING":
                continue
            # Only USDT-quoted pairs price cleanly; the rest can still skim,
            # but we need a USDT-equivalent path to compute the SAFU fee.
            sym = s.get("symbol") or ""
            if sym:
                syms.append(sym)
        if syms:
            return syms
    except Exception as e:
        log(f"exchangeInfo fetch failed: {e!r}; using default symbol list")
    return list(DEFAULT_SPOT_SYMBOLS)


# ==========================================================================
# Skimmer — spot trades
# ==========================================================================
def _split_symbol(sym: str) -> tuple[str, str]:
    """Best-effort split of e.g. 'BTCUSDT' -> ('BTC','USDT'). Handles the
    five quote suffixes seen in the demo."""
    for quote in ("USDT", "BUSD", "USDC", "BTC", "ETH", "IRT"):
        if sym.endswith(quote) and len(sym) > len(quote):
            return sym[: -len(quote)], quote
    # Fallback: assume last 3 chars are quote.
    return sym[:-3], sym[-3:]


def _fetch_spot_trades(symbol: str, fromId: int) -> list[dict]:
    """Fetch the public trade tape since ``fromId``. Returns [] on any failure."""
    # /v3/historicalTrades has fromId, but it may be auth-gated in some
    # deployments. /v3/trades returns the most recent N — we use it and
    # client-side filter by id. With limit=1000 + 30s skim cadence that
    # covers ~33 trades/s sustained, which is far above the demo's tape.
    url = (
        f"{MARKET_BASE}/v3/trades?symbol={urllib.parse.quote(symbol)}&limit={SPOT_TRADES_PER_TICK}"
    )
    try:
        with _http_urlopen(url, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log(f"spot trades fetch failed for {symbol}: {e!r}")
        return []
    if not isinstance(data, list):
        return []
    out = [t for t in data if int(t.get("id", -1)) > fromId]
    out.sort(key=lambda t: int(t.get("id", 0)))
    return out


def skim_spot() -> dict:
    """Per-symbol cursor on max trade_id seen. SAFU takes 10% of the 1%
    quote-asset fee on each fill — booked in quote asset (mostly USDT).
    Reference is ``<symbol>:<trade_id>`` so the same fill can never be
    double-credited even across server restarts."""
    symbols = list(_runtime_state.get("spot_symbols") or DEFAULT_SPOT_SYMBOLS)
    total_new = 0
    total_usdt_credited = Decimal(0)
    with _db_lock, db() as conn:
        for sym in symbols:
            cur_key = f"spot:{sym}"
            cur_pos = _get_cursor(conn, cur_key)
            try:
                last_id = int(cur_pos)
            except ValueError:
                last_id = 0
            trades = _fetch_spot_trades(sym, last_id)
            if not trades:
                continue
            base, quote = _split_symbol(sym)
            quote_asset = TOKEN_TO_ASSET.get(quote, quote)
            quote_price = get_price_usdt(quote_asset)  # None ok; we still credit in quote
            max_seen = last_id
            for t in trades:
                tid = int(t.get("id") or 0)
                if tid <= last_id:
                    continue
                quoteQty = D(t.get("quoteQty") or 0)
                if quoteQty <= 0:
                    p = D(t.get("price") or 0)
                    q = D(t.get("qty") or 0)
                    quoteQty = p * q
                if quoteQty <= 0:
                    max_seen = max(max_seen, tid)
                    continue
                fee_quote = quoteQty * SPOT_TAKER_FEE
                safu_cut = fee_quote * SAFU_FRACTION
                ref = f"{sym}:{tid}"
                decision = {
                    "symbol": sym,
                    "trade_id": tid,
                    "quoteQty": dstr(quoteQty),
                    "fee_rate": dstr(SPOT_TAKER_FEE),
                    "fee_quote": dstr(fee_quote),
                    "safu_fraction": dstr(SAFU_FRACTION),
                    "safu_cut": dstr(safu_cut),
                    "quote_asset": quote_asset,
                }
                inserted = _credit_inflow(
                    conn,
                    ts=int(t.get("time") // 1000) if t.get("time") else int(time.time()),
                    asset=quote_asset,
                    amount=safu_cut,
                    source="spot-fee",
                    reference=ref,
                    decision=decision,
                )
                if inserted:
                    total_new += 1
                    if quote_price is not None:
                        total_usdt_credited += safu_cut * quote_price
                max_seen = max(max_seen, tid)
            _set_cursor(conn, cur_key, str(max_seen))
    return {
        "symbols_polled": len(symbols),
        "new_inflows": total_new,
        "usdt_credited": dstr(total_usdt_credited),
    }


# ==========================================================================
# Skimmer — perp trades (read-only from perp_engine's sqlite)
# ==========================================================================
def skim_perp() -> dict:
    """Reads perp_trades since the last id we processed. Computes the SAFU
    cut as 10% of the commission column (commission is already in USDT)."""
    pconn = perp_db_ro()
    if pconn is None:
        return {"status": "perp_db_missing", "new_inflows": 0}
    try:
        with _db_lock, db() as conn:
            cur_pos = _get_cursor(conn, "perp:trades")
            try:
                last_id = int(cur_pos)
            except ValueError:
                last_id = 0
            rows = pconn.execute(
                "SELECT id, symbol, opex_user, commission, commission_asset, time "
                "FROM perp_trades WHERE id>? ORDER BY id ASC LIMIT ?",
                (last_id, PERP_ROWS_PER_TICK),
            ).fetchall()
            total_new = 0
            max_id = last_id
            for r in rows:
                tid = int(r["id"])
                commission = D(r["commission"])
                # commission is the fee paid by the user (or the liquidation
                # penalty). The SAFU take is 10% of this amount.
                if commission > 0:
                    safu_cut = commission * SAFU_FRACTION
                    asset = r["commission_asset"] or "USDT"
                    decision = {
                        "trade_id": tid,
                        "symbol": r["symbol"],
                        "opex_user": r["opex_user"],
                        "commission": dstr(commission),
                        "commission_asset": asset,
                        "safu_fraction": dstr(SAFU_FRACTION),
                        "safu_cut": dstr(safu_cut),
                    }
                    _credit_inflow(
                        conn,
                        ts=int(int(r["time"] or 0) // 1000) or int(time.time()),
                        asset=asset,
                        amount=safu_cut,
                        source="perp-fee",
                        reference=str(tid),
                        decision=decision,
                    )
                    total_new += 1
                max_id = max(max_id, tid)
            _set_cursor(conn, "perp:trades", str(max_id))
        return {"rows_scanned": len(rows), "new_inflows": total_new}
    finally:
        try:
            pconn.close()
        except Exception as e:  # noqa: BLE001
            log(f"perp db close failed: {e!r}")


# ==========================================================================
# Skimmer — liquidation surplus
# ==========================================================================
def skim_liq_surplus() -> dict:
    """Liquidation surplus = LIQUIDATION_CLEARANCE income rows (negative
    income, the excess margin the user gave up). We pull from
    perp_income_history at id>cursor and credit |income| to SAFU as a
    notional second-tier insurance fund (the perp engine still keeps its
    own ``zkcex-insurance`` bucket; SAFU is layered on top)."""
    pconn = perp_db_ro()
    if pconn is None:
        return {"status": "perp_db_missing", "new_inflows": 0}
    try:
        with _db_lock, db() as conn:
            cur_pos = _get_cursor(conn, "perp:liq")
            try:
                last_id = int(cur_pos)
            except ValueError:
                last_id = 0
            rows = pconn.execute(
                "SELECT id, symbol, opex_user, income, asset, time, related_order_id "
                "FROM perp_income_history "
                "WHERE income_type='LIQUIDATION_CLEARANCE' AND id>? ORDER BY id ASC LIMIT ?",
                (last_id, PERP_ROWS_PER_TICK),
            ).fetchall()
            total_new = 0
            max_id = last_id
            for r in rows:
                rid = int(r["id"])
                # Income is signed from the user's perspective (negative for
                # money they lost). SAFU absorbs the magnitude.
                amt = abs(D(r["income"]))
                # Same as perp fees, SAFU only takes a slice (10%) so the
                # majority still flows to the existing insurance counterparty.
                if amt > 0:
                    safu_cut = amt * SAFU_FRACTION
                    asset = r["asset"] or "USDT"
                    decision = {
                        "income_id": rid,
                        "symbol": r["symbol"],
                        "opex_user": r["opex_user"],
                        "income_abs": dstr(amt),
                        "asset": asset,
                        "safu_fraction": dstr(SAFU_FRACTION),
                        "safu_cut": dstr(safu_cut),
                    }
                    _credit_inflow(
                        conn,
                        ts=int(r["time"] or time.time()),
                        asset=asset,
                        amount=safu_cut,
                        source="liquidation-surplus",
                        reference=str(rid),
                        decision=decision,
                    )
                    total_new += 1
                max_id = max(max_id, rid)
            _set_cursor(conn, "perp:liq", str(max_id))
        return {"rows_scanned": len(rows), "new_inflows": total_new}
    finally:
        try:
            pconn.close()
        except Exception as e:  # noqa: BLE001
            log(f"perp db close failed: {e!r}")


# ==========================================================================
# Background threads
# ==========================================================================
def _skim_loop():
    while True:
        try:
            with _skim_lock:
                a = skim_spot()
                b = skim_perp()
                c = skim_liq_surplus()
            _runtime_state["last_skim_at"] = int(time.time())
            _runtime_state["last_skim_ok"] = True
            _runtime_state["last_skim_error"] = None
            log(f"skim: spot={a} perp={b} liq={c}")
        except Exception as e:  # noqa: BLE001
            _runtime_state["last_skim_at"] = int(time.time())
            _runtime_state["last_skim_ok"] = False
            _runtime_state["last_skim_error"] = repr(e)
            log(f"skim loop error: {e!r}")
        time.sleep(SKIM_INTERVAL_S)


def _attest_loop():
    while True:
        try:
            with _attest_lock:
                build_and_persist_attestation()
            _runtime_state["last_attest_at"] = int(time.time())
        except Exception as e:  # noqa: BLE001
            log(f"attest loop error: {e!r}")
        time.sleep(ATTEST_INTERVAL_S)


# ==========================================================================
# Summary builder (shared between /safu/summary and /safu/attestation)
# ==========================================================================
def _build_summary(include_breakdown: bool = True) -> dict:
    with db() as conn:
        bal_rows = conn.execute(
            "SELECT asset, balance, total_inflow, total_payout, last_updated "
            "FROM safu_balances ORDER BY asset ASC"
        ).fetchall()
        # 30-day windows
        cutoff = int(time.time()) - 30 * 24 * 3600
        in_30d_rows = conn.execute(
            "SELECT asset, SUM(CAST(amount AS REAL)) AS s FROM safu_inflow "
            "WHERE ts>=? GROUP BY asset",
            (cutoff,),
        ).fetchall()
        out_30d_rows = conn.execute(
            "SELECT asset, SUM(CAST(amount AS REAL)) AS s FROM safu_payout "
            "WHERE ts>=? GROUP BY asset",
            (cutoff,),
        ).fetchall()
        n_open = int(
            conn.execute("SELECT COUNT(*) AS n FROM safu_incidents WHERE status='open'").fetchone()[
                "n"
            ]
        )
        n_resolved = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM safu_incidents WHERE status='resolved'"
            ).fetchone()["n"]
        )
    by_asset = []
    total_bal_usdt = Decimal(0)
    total_in_usdt = Decimal(0)
    total_out_usdt = Decimal(0)
    monthly_in_usdt = Decimal(0)
    monthly_out_usdt = Decimal(0)
    in_30d_map = {r["asset"]: D(r["s"]) for r in in_30d_rows}
    out_30d_map = {r["asset"]: D(r["s"]) for r in out_30d_rows}
    for r in bal_rows:
        asset = r["asset"]
        price = get_price_usdt(asset)
        bal = D(r["balance"])
        tin = D(r["total_inflow"])
        tout = D(r["total_payout"])
        bal_usdt = (bal * price) if price is not None else None
        tin_usdt = (tin * price) if price is not None else None
        tout_usdt = (tout * price) if price is not None else None
        if bal_usdt is not None:
            total_bal_usdt += bal_usdt
        if tin_usdt is not None:
            total_in_usdt += tin_usdt
        if tout_usdt is not None:
            total_out_usdt += tout_usdt
        if price is not None:
            monthly_in_usdt += in_30d_map.get(asset, Decimal(0)) * price
            monthly_out_usdt += out_30d_map.get(asset, Decimal(0)) * price
        if include_breakdown:
            by_asset.append(
                {
                    "asset": asset,
                    "balance": dstr(bal),
                    "balance_usdt": dstr(bal_usdt) if bal_usdt is not None else None,
                    "total_inflow": dstr(tin),
                    "total_inflow_usdt": dstr(tin_usdt) if tin_usdt is not None else None,
                    "total_payout": dstr(tout),
                    "total_payout_usdt": dstr(tout_usdt) if tout_usdt is not None else None,
                    "last_updated": r["last_updated"],
                    "price_usdt": dstr(price) if price is not None else None,
                }
            )
    ratio = "0"
    if total_in_usdt > 0:
        ratio = dstr(total_out_usdt / total_in_usdt)
    return {
        "by_asset": by_asset,
        "total_balance_usdt": dstr(total_bal_usdt),
        "total_inflow_usdt": dstr(total_in_usdt),
        "total_payout_usdt": dstr(total_out_usdt),
        "monthly_inflow_usdt": dstr(monthly_in_usdt),
        "monthly_payout_usdt": dstr(monthly_out_usdt),
        "payout_to_inflow_ratio": ratio,
        "n_incidents_open": n_open,
        "n_incidents_resolved": n_resolved,
        "snapshot_at_ms": int(time.time() * 1000),
    }


# ==========================================================================
# Attestation: signed snapshot of the SAFU totals.
# ==========================================================================
def _canonical_attest_msg(
    snapshot_at_ms: int,
    total_balance_usdt: str,
    total_inflow_usdt: str,
    total_payout_usdt: str,
    by_asset: list[dict],
) -> bytes:
    """Build the canonical bytes to sign. External verifiers MUST reproduce
    this exact string from the JSON payload. The hash of by_asset folds the
    per-asset balances into the signature."""
    h = hashlib.sha256()
    for a in sorted(by_asset, key=lambda x: x["asset"]):
        # canonical line: asset|balance|total_inflow|total_payout
        h.update(f"{a['asset']}|{a['balance']}|{a['total_inflow']}|{a['total_payout']}\n".encode())
    by_asset_hash = h.hexdigest()
    return (
        f"{SCHEME_NAME}|{snapshot_at_ms}|{total_balance_usdt}|{total_inflow_usdt}|"
        f"{total_payout_usdt}|{by_asset_hash}"
    ).encode()


def build_attestation() -> dict:
    summary = _build_summary(include_breakdown=True)
    seed, pub, scheme, shared = load_signing_key()
    msg = _canonical_attest_msg(
        summary["snapshot_at_ms"],
        summary["total_balance_usdt"],
        summary["total_inflow_usdt"],
        summary["total_payout_usdt"],
        summary["by_asset"],
    )
    sig = ed25519_sign(seed, msg)
    return {
        "scheme": SCHEME_NAME,
        "snapshot_at": summary["snapshot_at_ms"],
        "by_asset": summary["by_asset"],
        "totals": {
            "total_balance_usdt": summary["total_balance_usdt"],
            "total_inflow_usdt": summary["total_inflow_usdt"],
            "total_payout_usdt": summary["total_payout_usdt"],
            "monthly_inflow_usdt": summary["monthly_inflow_usdt"],
            "monthly_payout_usdt": summary["monthly_payout_usdt"],
            "payout_to_inflow_ratio": summary["payout_to_inflow_ratio"],
            "n_incidents_open": summary["n_incidents_open"],
            "n_incidents_resolved": summary["n_incidents_resolved"],
        },
        "signature": sig.hex(),
        "server_pubkey": pub.hex(),
        "sig_scheme": scheme,
        "key_shared_with_pol": shared,
        "verification_recipe": [
            "1. Take by_asset, sort ascending by 'asset'.",
            "2. For each entry build a line: '<asset>|<balance>|<total_inflow>|<total_payout>\\n'.",
            "3. by_asset_hash = sha256(concat(lines)).hex()",
            "4. msg = utf8(scheme + '|' + snapshot_at + '|' + totals.total_balance_usdt + '|' + totals.total_inflow_usdt + '|' + totals.total_payout_usdt + '|' + by_asset_hash)",
            "5. Verify signature (hex) against server_pubkey (hex, 32 bytes Ed25519) using sig_scheme.",
        ],
    }


def build_and_persist_attestation() -> dict:
    att = build_attestation()
    with _db_lock, db() as conn:
        conn.execute(
            "INSERT INTO safu_attestations (snapshot_at, total_balance_usdt, total_inflow_usdt, "
            "total_payout_usdt, body_json, signature, server_pubkey) VALUES (?,?,?,?,?,?,?)",
            (
                att["snapshot_at"],
                att["totals"]["total_balance_usdt"],
                att["totals"]["total_inflow_usdt"],
                att["totals"]["total_payout_usdt"],
                json.dumps(att, separators=(",", ":")),
                att["signature"],
                att["server_pubkey"],
            ),
        )
    return att


# ==========================================================================
# Chain transfer helper for real payouts (best-effort; logged on failure).
# ==========================================================================
def _request_chain_payout(
    *, asset: str, amount: Decimal, recipient_opex: str, incident_id: str
) -> str | None:
    """Issue a real on-chain transfer via chain_server. Returns tx_hash on
    success, None otherwise. Network or signing failures DO NOT block the
    ledger entry — the books still record the obligation.

    We don't fail the API call on chain trouble; the demo deployment may
    not even have chain_server up.
    """
    try:
        payload = {
            "asset": asset,
            "amount": dstr(amount),
            "recipient_opex_user": recipient_opex,
            "memo": f"SAFU payout incident={incident_id}",
            "source": "safu",
        }
        req = _http_request(
            f"{CHAIN_BASE}/chain/safu-payout",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with _http_urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
        tx = data.get("tx_hash") or data.get("txHash")
        return tx
    except Exception as e:  # noqa: BLE001
        log(f"chain payout request failed (ledger entry still recorded): {e!r}")
        return None


# ==========================================================================
# Admin token
# ==========================================================================
def get_admin_token() -> str:
    tok = os.environ.get("SAFU_ADMIN_TOKEN")
    if tok:
        return tok
    # Generate one for the lifetime of this process and stash it on the
    # runtime state so the same token is reused across requests until
    # restart.
    cached = _runtime_state.get("admin_token")
    if cached:
        return str(cached)
    tok = "safu_" + secrets.token_urlsafe(24)
    _runtime_state["admin_token"] = tok
    log(f"SAFU_ADMIN_TOKEN={tok}  (set this in env to make it persistent)")
    return tok


# ==========================================================================
# HTTP server
# ==========================================================================
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "zkcex-safu/1.0"

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

    def _require_admin(self) -> str | None:
        """Returns the admin identifier on success; writes a 401/403 and
        returns None on failure."""
        tok = self._bearer()
        if not tok or tok != get_admin_token():
            self._send_json(
                401, {"error": "unauthorized", "message": "Bearer SAFU_ADMIN_TOKEN required"}
            )
            return None
        return "admin"

    def _read_json_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n > 0 else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[safu] {self.address_string()} - {fmt % args}\n")

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    # ---- Routing ------------------------------------------------------
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query or "")
        if path == "/safu/health":
            return self.h_health()
        if path == "/safu/summary":
            return self.h_summary()
        if path == "/safu/inflow":
            return self.h_inflow(q)
        if path == "/safu/payouts":
            return self.h_payouts(q)
        if path == "/safu/incidents":
            return self.h_incidents(q)
        if path.startswith("/safu/incidents/") and path.count("/") == 3:
            iid = path.rsplit("/", 1)[1]
            return self.h_incident_detail(iid)
        if path == "/safu/attestation":
            return self.h_attestation()
        if path == "/safu/server-info":
            return self.h_server_info()
        return self._send_json(404, {"error": "not_found", "path": path})

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/safu/incidents":
            return self.h_create_incident()
        if path.startswith("/safu/incidents/") and path.endswith("/payout"):
            iid = path[len("/safu/incidents/") : -len("/payout")]
            return self.h_payout(iid)
        if path.startswith("/safu/incidents/") and path.endswith("/close"):
            iid = path[len("/safu/incidents/") : -len("/close")]
            return self.h_close_incident(iid)
        if path == "/safu/topup":
            return self.h_topup()
        return self._send_json(404, {"error": "not_found", "path": path})

    # ---- Handlers (public) -------------------------------------------
    def h_health(self):
        cursors = _all_cursors()
        return self._send_json(
            200,
            {
                "ok": True,
                "uptime_s": int(time.time()) - START_TS,
                "last_skim_at": _runtime_state.get("last_skim_at") or 0,
                "last_skim_ok": _runtime_state.get("last_skim_ok") or False,
                "last_skim_error": _runtime_state.get("last_skim_error"),
                "last_attest_at": _runtime_state.get("last_attest_at") or 0,
                "skim_interval_s": SKIM_INTERVAL_S,
                "attest_interval_s": ATTEST_INTERVAL_S,
                "spot_symbols": list(_runtime_state.get("spot_symbols") or []),
                "cursors": cursors,
                "scheme": SCHEME_NAME,
            },
        )

    def h_server_info(self):
        _, pub, scheme, shared = load_signing_key()
        return self._send_json(
            200,
            {
                "scheme": SCHEME_NAME,
                "sig_scheme": scheme,
                "server_pubkey": pub.hex(),
                "key_shared_with_pol": shared,
                "safu_fraction": dstr(SAFU_FRACTION),
                "spot_taker_fee": dstr(SPOT_TAKER_FEE),
                "perp_taker_fee": dstr(PERP_TAKER_FEE),
                "perp_maker_fee": dstr(PERP_MAKER_FEE),
            },
        )

    def h_summary(self):
        return self._send_json(200, _build_summary(include_breakdown=True))

    def h_inflow(self, q: dict):
        asset = (q.get("asset") or [""])[0]
        try:
            limit = max(1, min(int((q.get("limit") or ["100"])[0]), 1000))
        except ValueError:
            limit = 100
        with db() as conn:
            if asset:
                rows = conn.execute(
                    "SELECT id, ts, asset, amount, source, reference, decision_json "
                    "FROM safu_inflow WHERE asset=? ORDER BY id DESC LIMIT ?",
                    (asset, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, ts, asset, amount, source, reference, decision_json "
                    "FROM safu_inflow ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        out = []
        for r in rows:
            try:
                decision = json.loads(r["decision_json"]) if r["decision_json"] else None
            except Exception:
                decision = None
            out.append(
                {
                    "id": r["id"],
                    "ts": r["ts"],
                    "asset": r["asset"],
                    "amount": r["amount"],
                    "source": r["source"],
                    "reference": r["reference"],
                    "decision": decision,
                }
            )
        return self._send_json(200, {"inflow": out, "count": len(out)})

    def h_payouts(self, q: dict):
        incident = (q.get("incident") or [""])[0]
        try:
            limit = max(1, min(int((q.get("limit") or ["100"])[0]), 1000))
        except ValueError:
            limit = 100
        with db() as conn:
            if incident:
                rows = conn.execute(
                    "SELECT * FROM safu_payout WHERE incident_id=? ORDER BY id DESC LIMIT ?",
                    (incident, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM safu_payout ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return self._send_json(200, {"payouts": [dict(r) for r in rows], "count": len(rows)})

    def h_incidents(self, q: dict):
        status = (q.get("status") or [""])[0]
        with db() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM safu_incidents WHERE status=? ORDER BY opened_at DESC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM safu_incidents ORDER BY opened_at DESC"
                ).fetchall()
        return self._send_json(200, {"incidents": [dict(r) for r in rows], "count": len(rows)})

    def h_incident_detail(self, incident_id: str):
        with db() as conn:
            row = conn.execute(
                "SELECT * FROM safu_incidents WHERE incident_id=?",
                (incident_id,),
            ).fetchone()
            if not row:
                return self._send_json(
                    404, {"error": "incident_not_found", "incident_id": incident_id}
                )
            payouts = conn.execute(
                "SELECT * FROM safu_payout WHERE incident_id=? ORDER BY id ASC",
                (incident_id,),
            ).fetchall()
        return self._send_json(
            200,
            {
                "incident": dict(row),
                "payouts": [dict(p) for p in payouts],
            },
        )

    def h_attestation(self):
        return self._send_json(200, build_attestation())

    # ---- Handlers (admin) --------------------------------------------
    def h_create_incident(self):
        if not self._require_admin():
            return
        body = self._read_json_body()
        category = (body.get("category") or "").strip()
        title = (body.get("title") or "").strip()
        if not category or not title:
            return self._send_json(
                400, {"error": "bad_request", "message": "category and title required"}
            )
        if category not in ("security-breach", "liquidation-cascade", "system-error", "goodwill"):
            return self._send_json(400, {"error": "bad_request", "message": "invalid category"})
        iid = body.get("incident_id") or f"inc-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        opened_at = int(time.time())
        description = body.get("description") or ""
        with _db_lock, db() as conn:
            try:
                conn.execute(
                    "INSERT INTO safu_incidents (incident_id, opened_at, category, title, "
                    "description, status) VALUES (?,?,?,?,?, 'open')",
                    (iid, opened_at, category, title, description),
                )
            except sqlite3.IntegrityError:
                return self._send_json(409, {"error": "incident_exists", "incident_id": iid})
        return self._send_json(
            200,
            {
                "incident_id": iid,
                "opened_at": opened_at,
                "category": category,
                "title": title,
                "status": "open",
            },
        )

    def h_payout(self, incident_id: str):
        if not self._require_admin():
            return
        body = self._read_json_body()
        try:
            asset = (body.get("asset") or "USDT").strip().upper()
            amount = D(body.get("amount") or 0)
            recipient = body.get("recipient_opex_user")  # nullable for write-offs
            reason = (body.get("reason") or "").strip()
        except Exception:
            return self._send_json(400, {"error": "bad_request"})
        if amount <= 0:
            return self._send_json(400, {"error": "bad_request", "message": "amount must be > 0"})
        # Look up incident.
        with db() as conn:
            inc = conn.execute(
                "SELECT * FROM safu_incidents WHERE incident_id=?",
                (incident_id,),
            ).fetchone()
        if not inc:
            return self._send_json(404, {"error": "incident_not_found"})
        if inc["status"] not in ("open",):
            return self._send_json(409, {"error": "incident_closed", "status": inc["status"]})
        # Optional real chain transfer.
        tx_hash = None
        if recipient and body.get("execute_chain_transfer"):
            tx_hash = _request_chain_payout(
                asset=asset,
                amount=amount,
                recipient_opex=recipient,
                incident_id=incident_id,
            )
        # Write ledger entry.
        try:
            with _db_lock, db() as conn:
                payout_id = _debit_payout(
                    conn,
                    ts=int(time.time()),
                    asset=asset,
                    amount=amount,
                    incident_id=incident_id,
                    recipient=recipient,
                    reason=reason,
                    approved_by="admin",
                    tx_hash=tx_hash,
                )
                # Update incident running totals.
                price = get_price_usdt(asset)
                amount_usdt = (amount * price) if price is not None else Decimal(0)
                cur_total = D(inc["total_payout_usdt"]) + amount_usdt
                cur_users = int(inc["affected_user_count"]) + (1 if recipient else 0)
                conn.execute(
                    "UPDATE safu_incidents SET total_payout_usdt=?, affected_user_count=? "
                    "WHERE incident_id=?",
                    (dstr(cur_total), cur_users, incident_id),
                )
        except ValueError as e:
            return self._send_json(409, {"error": "insufficient_funds", "message": str(e)})
        if recipient:
            _notif_send(
                recipient,
                "security",
                "critical",
                "SAFU payout credited",
                f"You received a {dstr(amount)} {asset} SAFU payout for incident {incident_id}.",
                {
                    "asset": asset,
                    "amount": dstr(amount),
                    "incident_id": incident_id,
                    "tx_hash": tx_hash or "",
                },
            )
        return self._send_json(
            200,
            {
                "payout_id": payout_id,
                "incident_id": incident_id,
                "asset": asset,
                "amount": dstr(amount),
                "recipient_opex_user": recipient,
                "tx_hash": tx_hash,
            },
        )

    def h_close_incident(self, incident_id: str):
        if not self._require_admin():
            return
        body = self._read_json_body() or {}
        final_status = body.get("status") or "resolved"
        if final_status not in ("resolved", "rejected"):
            return self._send_json(
                400, {"error": "bad_status", "message": "status must be resolved or rejected"}
            )
        now = int(time.time())
        with _db_lock, db() as conn:
            row = conn.execute(
                "SELECT status FROM safu_incidents WHERE incident_id=?",
                (incident_id,),
            ).fetchone()
            if not row:
                return self._send_json(404, {"error": "incident_not_found"})
            conn.execute(
                "UPDATE safu_incidents SET status=?, closed_at=? WHERE incident_id=?",
                (final_status, now, incident_id),
            )
        return self._send_json(
            200,
            {
                "incident_id": incident_id,
                "status": final_status,
                "closed_at": now,
            },
        )

    def h_topup(self):
        if not self._require_admin():
            return
        body = self._read_json_body()
        asset = (body.get("asset") or "USDT").strip().upper()
        amount = D(body.get("amount") or 0)
        memo = (body.get("memo") or "manual-topup").strip()
        if amount <= 0:
            return self._send_json(400, {"error": "bad_request", "message": "amount must be > 0"})
        ref = body.get("reference") or f"manual-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        decision = {"manual": True, "memo": memo, "approved_by": "admin"}
        with _db_lock, db() as conn:
            ok = _credit_inflow(
                conn,
                ts=int(time.time()),
                asset=asset,
                amount=amount,
                source="manual-topup",
                reference=ref,
                decision=decision,
            )
        if not ok:
            return self._send_json(409, {"error": "duplicate_reference", "reference": ref})
        return self._send_json(
            200, {"ok": True, "asset": asset, "amount": dstr(amount), "reference": ref}
        )


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5601
    init_db()
    seed, pub, scheme, shared = load_signing_key()
    log(f"db={DB_PATH}")
    log(f"sig_scheme={scheme} pubkey={pub.hex()} shared_with_pol={shared}")
    log(
        f"safu_fraction={dstr(SAFU_FRACTION)} spot_fee={dstr(SPOT_TAKER_FEE)} "
        f"perp_taker={dstr(PERP_TAKER_FEE)} perp_maker={dstr(PERP_MAKER_FEE)}"
    )
    # Discover spot symbols up front. Cached for the process lifetime — if
    # the operator adds a new pair, restart the service.
    _runtime_state["spot_symbols"] = discover_spot_symbols()
    log(f"spot symbols to skim: {_runtime_state['spot_symbols']}")
    # Ensure the admin token has been printed at least once before the
    # first request lands. get_admin_token() emits the token on first call.
    get_admin_token()
    # Best-effort initial skim so /safu/summary isn't empty on first hit.
    try:
        with _skim_lock:
            a = skim_spot()
            b = skim_perp()
            c = skim_liq_surplus()
        _runtime_state["last_skim_at"] = int(time.time())
        log(f"initial skim: spot={a} perp={b} liq={c}")
    except Exception as e:  # noqa: BLE001
        log(f"initial skim failed: {e!r}")
    try:
        build_and_persist_attestation()
        _runtime_state["last_attest_at"] = int(time.time())
    except Exception as e:  # noqa: BLE001
        log(f"initial attestation failed: {e!r}")
    threading.Thread(target=_skim_loop, daemon=True).start()
    threading.Thread(target=_attest_loop, daemon=True).start()
    log(f"listening on :{port}")
    with ThreadingServer(("", port), Handler) as srv:
        srv.serve_forever()


if __name__ == "__main__":
    main()
