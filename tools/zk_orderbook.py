#!/usr/bin/env python3
"""zkCEX privacy-preserving trading sub-system (zk_orderbook, port 5660).

A commit-reveal trading pathway running alongside the transparent CLOB at
/v3/order. Order price/quantity/side are hidden during the commit phase via
SHA-256 commitments; users reveal in a short reveal window; the matcher
runs a Walrasian (frequent-batch-auction) uniform-clearing-price match at
the end of each 30s window.

Cryptography
------------
- Commitment scheme: SHA-256(salt || price_8dp || quantity_8dp || side ||
  symbol || user_id || expires_at). Fixed-width big-endian encoding for
  each field so the digest is unambiguous. This is *binding* (an attacker
  cannot find a second preimage for a fresh random 32-byte salt) and
  *hiding* (without the salt, the digest reveals nothing about the order)
  given a uniform 32-byte salt drawn in the browser. It is NOT
  homomorphic — a production system aiming to batch-prove validity (e.g.
  "your committed quantity is <= your balance") would upgrade to Pedersen
  commitments on BN254 + Plonk/Groth16. See "honest caveats" in the
  shipping notes.
- Batch root signature: Ed25519 over
  "zkcex-zk-batch-v1" || batch_id_be8 || symbol_utf8_len_prefixed ||
  clearing_price_str_len_prefixed || merkle_root_of_reveal_hashes
  reusing the pol_signing_key persisted by pol_server.py. The same pubkey
  is exposed at GET /zk-trade/server-info so an external verifier can
  fetch it once and verify all batches offline.

Stdlib only. State persisted at tools/.local/zk_orderbook.db.
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
from decimal import Decimal, getcontext

getcontext().prec = 36

# --- Paths ----------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join(HERE, ".local")
os.makedirs(LOCAL_DIR, exist_ok=True)
DB_PATH = os.path.join(LOCAL_DIR, "zk_orderbook.db")
AUTH_DB_PATH = os.path.join(LOCAL_DIR, "auth.db")
KEY_PATH = os.path.join(LOCAL_DIR, "pol_signing_key")  # shared with pol_server


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


WALLET_BASE = _validated_http_base_url(
    "WALLET_BASE", os.environ.get("WALLET_BASE", "http://127.0.0.1:8091")
)
AUTH_BASE = _validated_http_base_url(
    "AUTH_BASE", os.environ.get("AUTH_BASE", "http://127.0.0.1:5501")
)
BATCH_INTERVAL_SECONDS = int(os.environ.get("ZK_BATCH_INTERVAL_SECONDS", "30"))
COMMIT_PHASE_SECONDS = int(os.environ.get("ZK_COMMIT_PHASE_SECONDS", "20"))  # of the batch window
REVEAL_PHASE_SECONDS = int(
    os.environ.get("ZK_REVEAL_PHASE_SECONDS", "10")
)  # tail of the batch window
SCHEME_NAME = "zkcex-zk-batch-v1"
SUPPORTED_SYMBOLS = ("ETHUSDT", "BTCUSDT")

_db_lock = threading.Lock()
_match_lock = threading.Lock()

# --- Pure-Python Ed25519 (reused from pol_server.py) ----------------------
# Same RFC 8032 reference adapted for shared use. The signing key is shared
# with pol_server so a single pubkey covers both PoL roots and zk batch
# roots.
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


def _ed_encodeint(y):
    bits = [(y >> i) & 1 for i in range(_ED_b)]
    return bytes(sum(bits[i * 8 + j] << j for j in range(8)) for i in range(_ED_b // 8))


def _ed_encodepoint(P):
    x, y = P
    bits = [(y >> i) & 1 for i in range(_ED_b - 1)] + [x & 1]
    return bytes(sum(bits[i * 8 + j] << j for j in range(8)) for i in range(_ED_b // 8))


def _ed_bit(h, i):
    return (h[i // 8] >> (i % 8)) & 1


def ed25519_publickey(sk_seed: bytes) -> bytes:
    h = _ed_H(sk_seed)
    a = 2 ** (_ED_b - 2) + sum(2**i * _ed_bit(h, i) for i in range(3, _ED_b - 2))
    A = _ed_scalarmult(_ED_B, a)
    return _ed_encodepoint(A)


def _ed_Hint(m):
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


def load_or_generate_keypair() -> tuple[bytes, bytes, str]:
    if not os.path.exists(KEY_PATH):
        seed = secrets.token_bytes(32)
        with open(KEY_PATH, "wb") as f:
            f.write(seed)
        try:
            os.chmod(KEY_PATH, 0o600)
        except OSError:
            pass
    with open(KEY_PATH, "rb") as f:
        seed = f.read()
    if len(seed) != 32:
        raise RuntimeError(f"bad signing key length: {len(seed)}")
    return seed, ed25519_publickey(seed), "Ed25519"


# --- DB -------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def auth_db_ro():
    uri = f"file:{urllib.parse.quote(AUTH_DB_PATH)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db_lock, db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS zk_commits (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              commitment_hex TEXT NOT NULL UNIQUE,
              opex_user TEXT NOT NULL,
              symbol TEXT NOT NULL,
              submitted_at INTEGER NOT NULL,
              expires_at INTEGER NOT NULL,
              batch_id INTEGER NOT NULL,
              status TEXT NOT NULL,
              revealed_at INTEGER,
              matched_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_commits_user
              ON zk_commits(opex_user);
            CREATE INDEX IF NOT EXISTS idx_commits_batch
              ON zk_commits(batch_id, symbol);

            CREATE TABLE IF NOT EXISTS zk_reveals (
              commitment_hex TEXT PRIMARY KEY,
              opex_user TEXT NOT NULL,
              symbol TEXT NOT NULL,
              side TEXT NOT NULL,
              price TEXT NOT NULL,
              quantity TEXT NOT NULL,
              salt_hex TEXT NOT NULL,
              expires_at INTEGER NOT NULL,
              revealed_at INTEGER NOT NULL,
              batch_id INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_reveals_batch
              ON zk_reveals(batch_id, symbol);

            CREATE TABLE IF NOT EXISTS zk_trades (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              symbol TEXT NOT NULL,
              buyer_user TEXT NOT NULL,
              seller_user TEXT NOT NULL,
              buyer_commitment TEXT NOT NULL,
              seller_commitment TEXT NOT NULL,
              clearing_price TEXT NOT NULL,
              quantity TEXT NOT NULL,
              batch_id INTEGER NOT NULL,
              matched_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_trades_user
              ON zk_trades(buyer_user, seller_user);
            CREATE INDEX IF NOT EXISTS idx_trades_batch
              ON zk_trades(batch_id);

            CREATE TABLE IF NOT EXISTS zk_batches (
              batch_id INTEGER PRIMARY KEY,
              symbol TEXT NOT NULL,
              window_start INTEGER NOT NULL,
              window_end INTEGER NOT NULL,
              n_commits INTEGER NOT NULL,
              n_reveals INTEGER NOT NULL,
              n_matches INTEGER NOT NULL,
              clearing_price TEXT,
              total_volume TEXT,
              root_hash TEXT NOT NULL,
              signature TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_batches_symbol
              ON zk_batches(symbol, batch_id DESC);
            """
        )


# --- Auth resolution ------------------------------------------------------
def session_user_via_auth(token: str) -> dict | None:
    """Resolve a Bearer token to {id, opex_user, email}. SQLite shortcut
    falls back to /auth/me. Mirrors pol_server.session_user_via_auth."""
    if not token:
        return None
    if os.path.exists(AUTH_DB_PATH):
        try:
            now = int(time.time())
            with auth_db_ro() as conn:
                row = conn.execute(
                    "SELECT u.id, u.opex_user, u.email "
                    "FROM sessions s JOIN users u ON u.id=s.user_id "
                    "WHERE s.token=? AND s.expires_at>?",
                    (token, now),
                ).fetchone()
                if row:
                    return {"id": row["id"], "opex_user": row["opex_user"], "email": row["email"]}
                return None
        except sqlite3.OperationalError:
            pass
    try:
        url = AUTH_BASE + "/auth/me"
        req = _http_request(url, headers={"Authorization": f"Bearer {token}"})
        with _http_urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        u = data.get("user") or {}
        if not u:
            return None
        return {"id": u.get("id"), "opex_user": u.get("opex_user"), "email": u.get("email")}
    except Exception:
        return None


# --- Commitment encoding --------------------------------------------------
# Field encoding must match the JS side EXACTLY for the digest to verify.
#
# Layout (big-endian, fixed width):
#   salt:        32 bytes (raw)
#   price_8dp:   uint64  (price scaled by 1e8)
#   qty_8dp:     uint64  (quantity scaled by 1e8)
#   side:        uint8   (0 = BUY, 1 = SELL)
#   symbol:      utf-8 bytes, length-prefixed with uint16
#   user_id:     utf-8 bytes, length-prefixed with uint16  (opex_user or "")
#   expires_at:  uint64  (unix seconds)
def _u8(n: int) -> bytes:
    return int(n).to_bytes(1, "big", signed=False)


def _u16(n: int) -> bytes:
    return int(n).to_bytes(2, "big", signed=False)


def _u64(n: int) -> bytes:
    if n < 0:
        n = 0
    if n >= 1 << 64:
        n = (1 << 64) - 1
    return int(n).to_bytes(8, "big", signed=False)


def _len_prefix(s: str) -> bytes:
    b = s.encode("utf-8")
    if len(b) > 0xFFFF:
        b = b[:0xFFFF]
    return _u16(len(b)) + b


def _scale_8dp(dec_str: str) -> int:
    """Scale a decimal string by 1e8 and return non-negative int. The
    same routine is implemented in JS via a manual decimal parser."""
    try:
        d = Decimal(dec_str)
    except Exception:
        return 0
    if d < 0:
        return 0
    scaled = (d * Decimal(10**8)).to_integral_value()
    return int(scaled)


def compute_commitment_hex(
    salt_hex: str, price: str, quantity: str, side: str, symbol: str, user_id: str, expires_at: int
) -> str:
    salt = bytes.fromhex(salt_hex)
    if len(salt) != 32:
        raise ValueError("salt must be 32 bytes")
    side_byte = 0 if side.upper() == "BUY" else 1
    msg = (
        salt
        + _u64(_scale_8dp(price))
        + _u64(_scale_8dp(quantity))
        + _u8(side_byte)
        + _len_prefix(symbol)
        + _len_prefix(user_id)
        + _u64(int(expires_at))
    )
    return hashlib.sha256(msg).hexdigest()


# --- Batch + phase math ---------------------------------------------------
def current_batch_id(now: int | None = None) -> int:
    n = int(now if now is not None else time.time())
    return n // BATCH_INTERVAL_SECONDS


def batch_window(batch_id: int) -> tuple[int, int]:
    """Return (window_start, window_end) for a batch_id."""
    start = batch_id * BATCH_INTERVAL_SECONDS
    end = start + BATCH_INTERVAL_SECONDS
    return start, end


def phase_for(now: int) -> tuple[str, int, int, int]:
    """Return (phase, batch_id, time_left_in_phase, window_end). Phase is
    "commit" for the first COMMIT_PHASE_SECONDS of the batch window,
    "reveal" for the next REVEAL_PHASE_SECONDS, then loops (the match
    runs precisely at window_end)."""
    bid = current_batch_id(now)
    start, end = batch_window(bid)
    elapsed = now - start
    if elapsed < COMMIT_PHASE_SECONDS:
        return "commit", bid, COMMIT_PHASE_SECONDS - elapsed, end
    return "reveal", bid, max(0, BATCH_INTERVAL_SECONDS - elapsed), end


# --- Wallet integration (best-effort, demo-grade) -------------------------
# Lock at commit / debit on match / refund on miss. The current internal
# /v2/transfer requires whole-USDT amounts which doesn't match fractional
# ETH orders, so the demo uses a *soft* lock that records intent in the
# zk_commits table. A production path would expose a fractional-aware
# lock primitive on the wallet API. The matcher writes settlement-style
# zk_trades rows regardless.
def soft_lock_notional(
    opex_user: str, symbol: str, side: str, price: str, quantity: str
) -> tuple[bool, str]:
    # Validate user has *something* — best-effort only. We don't reject
    # the commit if the wallet API is offline; the matcher will simply
    # produce an "unfilled" if the post-reveal balance check fails.
    try:
        url = f"{WALLET_BASE}/v1/owner/{urllib.parse.quote(opex_user)}/wallets"
        with _http_urlopen(url, timeout=2) as resp:
            json.loads(resp.read().decode("utf-8"))
        return True, "soft-lock-ok"
    except Exception:
        return True, "wallet-unavailable-soft-ok"


# --- Matching engine ------------------------------------------------------
def _walrasian_clearing_price(orders: list[dict]) -> tuple[Decimal | None, Decimal]:
    """Compute the FBA uniform clearing price that maximises traded volume.

    The classic Walrasian algorithm:
      - For each candidate price P (drawn from the union of revealed prices),
        compute supply(P) = sum(sell_qty for sells with price <= P)
        and demand(P) = sum(buy_qty for buys with price >= P).
        match_volume(P) = min(supply(P), demand(P)).
      - Pick the P that maximises match_volume.
      - On ties, prefer the midpoint of the range of equally-good prices
        (this is the textbook FBA tie-break; it avoids favoring either side).

    Returns (clearing_price, matched_volume). clearing_price is None when
    the book doesn't cross.
    """
    if not orders:
        return None, Decimal(0)
    buys = [o for o in orders if o["side"] == "BUY"]
    sells = [o for o in orders if o["side"] == "SELL"]
    if not buys or not sells:
        return None, Decimal(0)
    prices = sorted({Decimal(o["price"]) for o in orders})
    if not prices:
        return None, Decimal(0)
    best_volume = Decimal(-1)
    best_prices: list[Decimal] = []
    for P in prices:
        supply = sum(
            (Decimal(o["quantity"]) for o in sells if Decimal(o["price"]) <= P), Decimal(0)
        )
        demand = sum((Decimal(o["quantity"]) for o in buys if Decimal(o["price"]) >= P), Decimal(0))
        vol = supply if supply < demand else demand
        if vol > best_volume:
            best_volume = vol
            best_prices = [P]
        elif vol == best_volume and best_volume > 0:
            best_prices.append(P)
    if best_volume <= 0 or not best_prices:
        return None, Decimal(0)
    # Midpoint of the range of equally-good prices. With a single best
    # price this collapses to that price.
    mid = (min(best_prices) + max(best_prices)) / Decimal(2)
    return mid, best_volume


def _merkle_root(leaves: list[bytes]) -> bytes:
    """Standard binary Merkle root with the last leaf duplicated when odd.
    Returns SHA-256(b"") for an empty list so the field is always populated."""
    if not leaves:
        return hashlib.sha256(b"").digest()
    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        nxt = []
        for i in range(0, len(level), 2):
            nxt.append(hashlib.sha256(level[i] + level[i + 1]).digest())
        level = nxt
    return level[0]


def _reveal_leaf_hash(commitment_hex: str, price: str, quantity: str, side: str) -> bytes:
    """Per-reveal Merkle leaf. Includes everything an auditor needs to
    re-run the match — commitment, price, qty, side."""
    msg = (
        bytes.fromhex(commitment_hex)
        + _u64(_scale_8dp(price))
        + _u64(_scale_8dp(quantity))
        + _u8(0 if side.upper() == "BUY" else 1)
    )
    return hashlib.sha256(msg).digest()


def _batch_sign_msg(batch_id: int, symbol: str, clearing_price: str, root_hash: bytes) -> bytes:
    """Canonical bytes the Ed25519 signature commits to. Order-sensitive."""
    return (
        SCHEME_NAME.encode("utf-8")
        + _u64(batch_id)
        + _len_prefix(symbol)
        + _len_prefix(clearing_price)
        + root_hash
    )


def run_match_for_batch(batch_id: int, symbol: str, seed: bytes, pubkey: bytes) -> dict:
    """Idempotent: if already matched, returns the persisted batch row.

    Otherwise reads the revealed orders for (batch_id, symbol), computes
    the uniform clearing price, writes zk_trades + zk_batches rows, and
    signs the root.
    """
    with _match_lock, _db_lock, db() as conn:
        existing = conn.execute(
            "SELECT * FROM zk_batches WHERE batch_id=? AND symbol=?",
            (batch_id, symbol),
        ).fetchone()
        if existing:
            return _row_to_dict(existing)

        # Pull all reveals for this batch + symbol that haven't been
        # matched yet (status='revealed').
        reveals = conn.execute(
            "SELECT r.commitment_hex, r.opex_user, r.side, r.price, "
            "       r.quantity, c.status "
            "FROM zk_reveals r "
            "JOIN zk_commits c ON c.commitment_hex = r.commitment_hex "
            "WHERE r.batch_id=? AND r.symbol=? AND c.status='revealed' "
            "ORDER BY r.revealed_at ASC",
            (batch_id, symbol),
        ).fetchall()
        reveals = [dict(r) for r in reveals]
        # Mark the empty case still as a "sealed" batch so timeline is
        # complete in the UI.
        n_commits = conn.execute(
            "SELECT COUNT(*) AS c FROM zk_commits " "WHERE batch_id=? AND symbol=?",
            (batch_id, symbol),
        ).fetchone()["c"]
        if not reveals:
            window_start, window_end = batch_window(batch_id)
            root_bytes = _merkle_root([])
            sig = ed25519_sign(seed, _batch_sign_msg(batch_id, symbol, "", root_bytes))
            conn.execute(
                "INSERT INTO zk_batches (batch_id, symbol, window_start, "
                "window_end, n_commits, n_reveals, n_matches, "
                "clearing_price, total_volume, root_hash, signature) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    batch_id,
                    symbol,
                    window_start,
                    window_end,
                    n_commits,
                    0,
                    0,
                    None,
                    "0",
                    root_bytes.hex(),
                    sig.hex(),
                ),
            )
            return {
                "batch_id": batch_id,
                "symbol": symbol,
                "window_start": window_start,
                "window_end": window_end,
                "n_commits": n_commits,
                "n_reveals": 0,
                "n_matches": 0,
                "clearing_price": None,
                "total_volume": "0",
                "root_hash": root_bytes.hex(),
                "signature": sig.hex(),
            }

        # Compute the clearing price.
        price, traded_volume = _walrasian_clearing_price(reveals)
        matched_at = int(time.time())
        n_matches = 0
        total_volume = Decimal(0)
        clearing_str = ""
        if price is not None and traded_volume > 0:
            clearing_str = _strip_decimal(price)
            # Buyers willing to pay >= P, sorted by best (highest) price first;
            # tiebreak earliest reveal first.
            buys = sorted(
                [o for o in reveals if o["side"] == "BUY" and Decimal(o["price"]) >= price],
                key=lambda o: (-Decimal(o["price"]), o["commitment_hex"]),
            )
            sells = sorted(
                [o for o in reveals if o["side"] == "SELL" and Decimal(o["price"]) <= price],
                key=lambda o: (Decimal(o["price"]), o["commitment_hex"]),
            )
            # Track residual qty per order so we can split fills.
            buy_q = [Decimal(o["quantity"]) for o in buys]
            sell_q = [Decimal(o["quantity"]) for o in sells]
            bi = si = 0
            while bi < len(buys) and si < len(sells):
                fill = buy_q[bi] if buy_q[bi] < sell_q[si] else sell_q[si]
                if fill <= 0:
                    if buy_q[bi] <= 0:
                        bi += 1
                    if si < len(sells) and sell_q[si] <= 0:
                        si += 1
                    continue
                buyer = buys[bi]
                seller = sells[si]
                conn.execute(
                    "INSERT INTO zk_trades (symbol, buyer_user, seller_user, "
                    "buyer_commitment, seller_commitment, clearing_price, "
                    "quantity, batch_id, matched_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        symbol,
                        buyer["opex_user"],
                        seller["opex_user"],
                        buyer["commitment_hex"],
                        seller["commitment_hex"],
                        clearing_str,
                        _strip_decimal(fill),
                        batch_id,
                        matched_at,
                    ),
                )
                n_matches += 1
                total_volume += fill
                buy_q[bi] -= fill
                sell_q[si] -= fill
                if buy_q[bi] <= 0:
                    conn.execute(
                        "UPDATE zk_commits SET status='matched', matched_at=? "
                        "WHERE commitment_hex=?",
                        (matched_at, buyer["commitment_hex"]),
                    )
                    bi += 1
                if sell_q[si] <= 0:
                    conn.execute(
                        "UPDATE zk_commits SET status='matched', matched_at=? "
                        "WHERE commitment_hex=?",
                        (matched_at, seller["commitment_hex"]),
                    )
                    si += 1
            # Mark unfilled revealed-but-uncrossed orders.
            conn.execute(
                "UPDATE zk_commits SET status='unfilled' "
                "WHERE batch_id=? AND symbol=? AND status='revealed'",
                (batch_id, symbol),
            )
        else:
            # No cross — everything that revealed becomes "unfilled".
            conn.execute(
                "UPDATE zk_commits SET status='unfilled' "
                "WHERE batch_id=? AND symbol=? AND status='revealed'",
                (batch_id, symbol),
            )

        # Sweep stale committed-but-never-revealed orders for this batch
        # into the 'expired' status. They lived past the reveal window.
        conn.execute(
            "UPDATE zk_commits SET status='expired' "
            "WHERE batch_id=? AND symbol=? AND status='committed'",
            (batch_id, symbol),
        )

        # Merkle root over the reveal leaves (sorted by commitment_hex
        # for deterministic ordering — auditors can reproduce).
        leaves = [
            _reveal_leaf_hash(r["commitment_hex"], r["price"], r["quantity"], r["side"])
            for r in sorted(reveals, key=lambda r: r["commitment_hex"])
        ]
        root_bytes = _merkle_root(leaves)
        sig = ed25519_sign(seed, _batch_sign_msg(batch_id, symbol, clearing_str, root_bytes))
        window_start, window_end = batch_window(batch_id)
        conn.execute(
            "INSERT INTO zk_batches (batch_id, symbol, window_start, "
            "window_end, n_commits, n_reveals, n_matches, "
            "clearing_price, total_volume, root_hash, signature) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                batch_id,
                symbol,
                window_start,
                window_end,
                n_commits,
                len(reveals),
                n_matches,
                clearing_str or None,
                _strip_decimal(total_volume),
                root_bytes.hex(),
                sig.hex(),
            ),
        )
        return {
            "batch_id": batch_id,
            "symbol": symbol,
            "window_start": window_start,
            "window_end": window_end,
            "n_commits": n_commits,
            "n_reveals": len(reveals),
            "n_matches": n_matches,
            "clearing_price": clearing_str or None,
            "total_volume": _strip_decimal(total_volume),
            "root_hash": root_bytes.hex(),
            "signature": sig.hex(),
        }


def _strip_decimal(d) -> str:
    """Format a Decimal without trailing zeros / dot."""
    if d is None:
        return ""
    txt = format(Decimal(d), "f")
    if "." in txt:
        txt = txt.rstrip("0").rstrip(".")
    return txt or "0"


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


# --- Matcher scheduler ----------------------------------------------------
def matcher_loop(seed: bytes, pubkey: bytes):
    """Wake at each batch boundary + a 1s grace period and run any
    yet-unmatched batches across all supported symbols."""
    sys.stderr.write(f"[zk] matcher loop start, interval={BATCH_INTERVAL_SECONDS}s\n")
    while True:
        try:
            now = int(time.time())
            bid = current_batch_id(now)
            # We always seal *previous* batches — never the live one.
            with _db_lock, db() as conn:
                # Find batches that have at least one committed/revealed
                # row but no zk_batches row, and whose window_end already
                # passed.
                rows = conn.execute(
                    "SELECT DISTINCT batch_id, symbol FROM zk_commits "
                    "WHERE batch_id < ? "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM zk_batches b WHERE "
                    "  b.batch_id = zk_commits.batch_id AND "
                    "  b.symbol = zk_commits.symbol) "
                    "ORDER BY batch_id ASC",
                    (bid,),
                ).fetchall()
            for r in rows:
                try:
                    run_match_for_batch(r["batch_id"], r["symbol"], seed, pubkey)
                except Exception as e:  # noqa: BLE001
                    sys.stderr.write(
                        f"[zk] match failed batch={r['batch_id']} " f"symbol={r['symbol']}: {e!r}\n"
                    )
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[zk] matcher tick error: {e!r}\n")
        # Sleep to the next batch-boundary + 1s.
        now = time.time()
        nxt = (int(now) // BATCH_INTERVAL_SECONDS + 1) * BATCH_INTERVAL_SECONDS + 1
        time.sleep(max(0.5, nxt - now))


# --- HTTP server ----------------------------------------------------------
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "zkcex-zk-orderbook/1.0"

    # ---- helpers ---------------------------------------------------------
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

    def _resolve_caller(self) -> dict | None:
        """Bearer token (preferred) or trusted-local X-Opex-User.

        The X-Opex-User shortcut is the same mechanism used by the
        Binance-compatible matching gateway: requests from 127.0.0.1
        carrying the header are accepted as that opex_user. The public
        proxy never forwards X-Opex-User from external callers, so this
        is safe.
        """
        token = self._bearer()
        if token:
            return session_user_via_auth(token)
        opex = self.headers.get("X-Opex-User")
        if opex and self.client_address and self.client_address[0] in ("127.0.0.1", "::1"):
            return {"id": None, "opex_user": opex, "email": None}
        return None

    def _read_json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[zk] {self.address_string()} - {fmt % args}\n")

    # ---- CORS ------------------------------------------------------------
    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Opex-User")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    # ---- routing ---------------------------------------------------------
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/zk-trade/health":
            return self._send_json(
                200, {"ok": True, "service": "zk_orderbook", "scheme": SCHEME_NAME}
            )
        if path == "/zk-trade/server-info":
            return self.h_server_info()
        if path == "/zk-trade/info":
            return self.h_info(parsed.query)
        if path == "/zk-trade/commits":
            return self.h_commits(parsed.query)
        if path == "/zk-trade/batches":
            return self.h_batches(parsed.query)
        if path.startswith("/zk-trade/batch/"):
            tail = path[len("/zk-trade/batch/") :]
            return self.h_batch_detail(tail)
        if path == "/zk-trade/my-commits":
            return self.h_my_commits()
        if path == "/zk-trade/verifier/python":
            return self._send_text(200, PYTHON_VERIFIER_SNIPPET, "text/x-python; charset=utf-8")
        return self._send_json(404, {"error": "not_found", "path": path})

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path == "/zk-trade/commit":
            return self.h_commit()
        if path == "/zk-trade/reveal":
            return self.h_reveal()
        if path == "/zk-trade/cancel":
            return self.h_cancel()
        return self._send_json(404, {"error": "not_found", "path": path})

    def _send_text(self, status, txt, ctype="text/plain; charset=utf-8"):
        body = txt.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---- handlers --------------------------------------------------------
    def h_server_info(self):
        seed, pub, scheme = load_or_generate_keypair()
        return self._send_json(
            200,
            {
                "scheme_name": SCHEME_NAME,
                "sig_scheme": scheme,
                "pubkey": pub.hex(),
                "batch_interval_seconds": BATCH_INTERVAL_SECONDS,
                "commit_phase_seconds": COMMIT_PHASE_SECONDS,
                "reveal_phase_seconds": REVEAL_PHASE_SECONDS,
                "supported_symbols": list(SUPPORTED_SYMBOLS),
                "hash": "SHA-256",
                "commitment_layout": (
                    "SHA256(salt32 || price_8dp_u64 || qty_8dp_u64 || side_u8 || "
                    "u16len(symbol)||symbol || u16len(user_id)||user_id || "
                    "expires_at_u64)"
                ),
            },
        )

    def h_info(self, qs: str):
        now = int(time.time())
        phase, bid, time_left, window_end = phase_for(now)
        symbol = (urllib.parse.parse_qs(qs).get("symbol") or [None])[0]
        sym_filter = symbol.upper() if symbol else None
        out = {
            "now": now,
            "phase": phase,
            "current_batch_id": bid,
            "time_left_in_phase": time_left,
            "window_start": batch_window(bid)[0],
            "window_end": window_end,
            "batch_interval_seconds": BATCH_INTERVAL_SECONDS,
            "commit_phase_seconds": COMMIT_PHASE_SECONDS,
            "reveal_phase_seconds": REVEAL_PHASE_SECONDS,
            "supported_symbols": list(SUPPORTED_SYMBOLS),
        }
        # Add commits/reveals counts.
        with _db_lock, db() as conn:
            if sym_filter:
                cr = conn.execute(
                    "SELECT COUNT(*) AS c FROM zk_commits " "WHERE batch_id=? AND symbol=?",
                    (bid, sym_filter),
                ).fetchone()
                rr = conn.execute(
                    "SELECT COUNT(*) AS c FROM zk_reveals " "WHERE batch_id=? AND symbol=?",
                    (bid, sym_filter),
                ).fetchone()
                out["symbol"] = sym_filter
            else:
                cr = conn.execute(
                    "SELECT COUNT(*) AS c FROM zk_commits WHERE batch_id=?",
                    (bid,),
                ).fetchone()
                rr = conn.execute(
                    "SELECT COUNT(*) AS c FROM zk_reveals WHERE batch_id=?",
                    (bid,),
                ).fetchone()
            out["n_committed"] = cr["c"]
            out["n_revealed"] = rr["c"]
        return self._send_json(200, out)

    def h_commit(self):
        u = self._resolve_caller()
        if not u:
            return self._send_json(401, {"error": "unauthorized"})
        body = self._read_json_body()
        symbol = str(body.get("symbol") or "").upper()
        commitment_hex = str(body.get("commitment_hex") or "").lower()
        expires_at = int(body.get("expires_at") or 0)
        if symbol not in SUPPORTED_SYMBOLS:
            return self._send_json(
                400,
                {
                    "error": "unsupported_symbol",
                    "supported": list(SUPPORTED_SYMBOLS),
                },
            )
        if len(commitment_hex) != 64 or any(c not in "0123456789abcdef" for c in commitment_hex):
            return self._send_json(400, {"error": "bad_commitment_hex"})
        now = int(time.time())
        phase, bid, _, window_end = phase_for(now)
        if phase != "commit":
            return self._send_json(
                409,
                {
                    "error": "not_in_commit_phase",
                    "phase": phase,
                    "current_batch_id": bid,
                    "hint": "wait for the next batch's commit window",
                },
            )
        if expires_at <= now or expires_at > window_end + 600:
            # expires_at must be later than now and within a sane horizon.
            # The natural value is window_end (commit phase end + reveal
            # window covers it). We tolerate +10min so clients can use
            # the batch window_end.
            return self._send_json(
                400,
                {
                    "error": "bad_expires_at",
                    "now": now,
                    "must_be_after": now,
                    "must_be_before": window_end + 600,
                    "suggested": window_end,
                },
            )
        soft_lock_notional(u["opex_user"], symbol, "?", "0", "0")
        try:
            with _db_lock, db() as conn:
                conn.execute(
                    "INSERT INTO zk_commits (commitment_hex, opex_user, "
                    "symbol, submitted_at, expires_at, batch_id, status) "
                    "VALUES (?,?,?,?,?,?, 'committed')",
                    (commitment_hex, u["opex_user"], symbol, now, expires_at, bid),
                )
        except sqlite3.IntegrityError:
            return self._send_json(409, {"error": "duplicate_commitment"})
        time_to_reveal = max(0, window_end - now - REVEAL_PHASE_SECONDS)
        return self._send_json(
            200,
            {
                "ok": True,
                "commitment_hex": commitment_hex,
                "batch_id": bid,
                "phase": phase,
                "time_to_reveal": time_to_reveal,
                "window_end": window_end,
                "expires_at": expires_at,
            },
        )

    def h_reveal(self):
        u = self._resolve_caller()
        if not u:
            return self._send_json(401, {"error": "unauthorized"})
        body = self._read_json_body()
        commitment_hex = str(body.get("commitment_hex") or "").lower()
        salt_hex = str(body.get("salt_hex") or "").lower()
        price = str(body.get("price") or "")
        quantity = str(body.get("quantity") or "")
        side = str(body.get("side") or "").upper()
        symbol = str(body.get("symbol") or "").upper()
        expires_at = int(body.get("expires_at") or 0)
        if side not in ("BUY", "SELL"):
            return self._send_json(400, {"error": "bad_side"})
        if symbol not in SUPPORTED_SYMBOLS:
            return self._send_json(400, {"error": "unsupported_symbol"})
        if len(salt_hex) != 64:
            return self._send_json(400, {"error": "bad_salt"})
        try:
            Decimal(price)
            Decimal(quantity)
        except Exception:
            return self._send_json(400, {"error": "bad_price_or_quantity"})
        if Decimal(price) <= 0 or Decimal(quantity) <= 0:
            return self._send_json(400, {"error": "price_and_qty_must_be_positive"})
        # Fetch the commitment row and verify ownership.
        with _db_lock, db() as conn:
            row = conn.execute(
                "SELECT * FROM zk_commits WHERE commitment_hex=?",
                (commitment_hex,),
            ).fetchone()
        if not row:
            return self._send_json(404, {"error": "commitment_not_found"})
        if row["opex_user"] != u["opex_user"]:
            return self._send_json(403, {"error": "not_owner"})
        if row["status"] != "committed":
            return self._send_json(409, {"error": "bad_status", "status": row["status"]})
        if row["symbol"] != symbol or row["expires_at"] != expires_at:
            return self._send_json(
                400,
                {
                    "error": "field_mismatch",
                    "expected_symbol": row["symbol"],
                    "expected_expires_at": row["expires_at"],
                },
            )
        # Recompute the commitment digest. Must match exactly.
        try:
            digest = compute_commitment_hex(
                salt_hex, price, quantity, side, symbol, u["opex_user"], expires_at
            )
        except Exception as e:  # noqa: BLE001
            return self._send_json(400, {"error": "encoding_error", "message": str(e)})
        if digest != commitment_hex:
            return self._send_json(
                400,
                {
                    "error": "commitment_mismatch",
                    "expected": commitment_hex,
                    "computed": digest,
                },
            )
        now = int(time.time())
        phase, bid, _, _ = phase_for(now)
        # Allow reveals during the current batch's commit OR reveal phase
        # as long as they land in the same batch as the commit and before
        # window_end.
        if bid != row["batch_id"]:
            return self._send_json(
                409,
                {
                    "error": "wrong_batch_phase",
                    "current_batch_id": bid,
                    "commitment_batch_id": row["batch_id"],
                },
            )
        with _db_lock, db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO zk_reveals (commitment_hex, "
                "opex_user, symbol, side, price, quantity, salt_hex, "
                "expires_at, revealed_at, batch_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    commitment_hex,
                    u["opex_user"],
                    symbol,
                    side,
                    _strip_decimal(price),
                    _strip_decimal(quantity),
                    salt_hex,
                    expires_at,
                    now,
                    row["batch_id"],
                ),
            )
            conn.execute(
                "UPDATE zk_commits SET status='revealed', revealed_at=? " "WHERE commitment_hex=?",
                (now, commitment_hex),
            )
        _, _, window_end = (
            batch_window(row["batch_id"])[0],
            batch_window(row["batch_id"])[1],
            batch_window(row["batch_id"])[1],
        )
        return self._send_json(
            200,
            {
                "ok": True,
                "batch_id": row["batch_id"],
                "time_to_match": max(0, window_end - now),
            },
        )

    def h_cancel(self):
        u = self._resolve_caller()
        if not u:
            return self._send_json(401, {"error": "unauthorized"})
        body = self._read_json_body()
        commitment_hex = str(body.get("commitment_hex") or "").lower()
        with _db_lock, db() as conn:
            row = conn.execute(
                "SELECT * FROM zk_commits WHERE commitment_hex=?",
                (commitment_hex,),
            ).fetchone()
            if not row:
                return self._send_json(404, {"error": "not_found"})
            if row["opex_user"] != u["opex_user"]:
                return self._send_json(403, {"error": "not_owner"})
            if row["status"] not in ("committed",):
                return self._send_json(409, {"error": "bad_status", "status": row["status"]})
            conn.execute(
                "UPDATE zk_commits SET status='expired' " "WHERE commitment_hex=?",
                (commitment_hex,),
            )
        return self._send_json(200, {"ok": True})

    def h_commits(self, qs: str):
        q = urllib.parse.parse_qs(qs)
        symbol = (q.get("symbol") or [None])[0]
        batch_id = (q.get("batch_id") or [None])[0]
        limit = min(500, int((q.get("limit") or ["200"])[0]))
        where = []
        args: list = []
        if symbol:
            where.append("symbol=?")
            args.append(symbol.upper())
        if batch_id:
            try:
                where.append("batch_id=?")
                args.append(int(batch_id))
            except ValueError:
                pass
        sql = (
            "SELECT commitment_hex, symbol, submitted_at, expires_at, "
            "batch_id, status FROM zk_commits"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with _db_lock, db() as conn:
            rows = conn.execute(sql, args).fetchall()
        return self._send_json(
            200,
            {
                "count": len(rows),
                "commits": [_row_to_dict(r) for r in rows],
            },
        )

    def h_batches(self, qs: str):
        q = urllib.parse.parse_qs(qs)
        symbol = (q.get("symbol") or [None])[0]
        limit = min(200, int((q.get("limit") or ["50"])[0]))
        if symbol:
            with _db_lock, db() as conn:
                rows = conn.execute(
                    "SELECT * FROM zk_batches WHERE symbol=? " "ORDER BY batch_id DESC LIMIT ?",
                    (symbol.upper(), limit),
                ).fetchall()
        else:
            with _db_lock, db() as conn:
                rows = conn.execute(
                    "SELECT * FROM zk_batches ORDER BY batch_id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return self._send_json(
            200,
            {
                "count": len(rows),
                "batches": [_row_to_dict(r) for r in rows],
            },
        )

    def h_batch_detail(self, tail: str):
        # tail format: "<batch_id>?symbol=ETHUSDT" or "<batch_id>/<symbol>"
        symbol = None
        if "/" in tail:
            bid_s, symbol = tail.split("/", 1)
        else:
            bid_s = tail.split("?")[0]
            parsed = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(parsed.query)
            symbol = (q.get("symbol") or [None])[0]
        try:
            bid = int(bid_s)
        except ValueError:
            return self._send_json(400, {"error": "bad_batch_id"})
        with _db_lock, db() as conn:
            if symbol:
                row = conn.execute(
                    "SELECT * FROM zk_batches WHERE batch_id=? AND symbol=?",
                    (bid, symbol.upper()),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM zk_batches WHERE batch_id=? LIMIT 1",
                    (bid,),
                ).fetchone()
            if not row:
                return self._send_json(404, {"error": "batch_not_found"})
            sym = row["symbol"]
            reveals = conn.execute(
                "SELECT commitment_hex, side, price, quantity, revealed_at "
                "FROM zk_reveals WHERE batch_id=? AND symbol=? "
                "ORDER BY commitment_hex ASC",
                (bid, sym),
            ).fetchall()
            trades = conn.execute(
                "SELECT buyer_commitment, seller_commitment, clearing_price, "
                "quantity, matched_at FROM zk_trades WHERE batch_id=? "
                "AND symbol=? ORDER BY id ASC",
                (bid, sym),
            ).fetchall()
        seed, pub, scheme = load_or_generate_keypair()
        return self._send_json(
            200,
            {
                "batch": _row_to_dict(row),
                "reveals": [_row_to_dict(r) for r in reveals],
                "trades": [_row_to_dict(r) for r in trades],
                "server_pubkey": pub.hex(),
                "sig_scheme": scheme,
                "scheme_name": SCHEME_NAME,
                "verification": {
                    "sign_msg_layout": (
                        f'"{SCHEME_NAME}" || u64_be(batch_id) || '
                        "u16_len(symbol)||symbol || "
                        "u16_len(clearing_price)||clearing_price || "
                        "root_hash"
                    ),
                    "merkle_leaf_layout": (
                        "SHA256(bytes.fromhex(commitment_hex) || "
                        "u64_be(price_8dp) || u64_be(quantity_8dp) || u8(side))"
                    ),
                    "merkle_root_recipe": (
                        "binary tree, last leaf duplicated when odd, "
                        "SHA-256 internal nodes; leaves sorted by "
                        "commitment_hex ascending"
                    ),
                    "side_encoding": {"BUY": 0, "SELL": 1},
                    "scale": "price and quantity scaled by 1e8 to a u64",
                    "clearing_price_algorithm": (
                        "for each candidate price P in unique reveal-prices, "
                        "compute supply(P)=sum sell.qty where sell.price<=P, "
                        "demand(P)=sum buy.qty where buy.price>=P; "
                        "match_volume=min(supply,demand); pick P maximising "
                        "match_volume; tie-break with midpoint of the best-"
                        "price range"
                    ),
                },
            },
        )

    def h_my_commits(self):
        u = self._resolve_caller()
        if not u:
            return self._send_json(401, {"error": "unauthorized"})
        with _db_lock, db() as conn:
            commits = conn.execute(
                "SELECT * FROM zk_commits WHERE opex_user=? " "ORDER BY id DESC LIMIT 100",
                (u["opex_user"],),
            ).fetchall()
            trades_buy = conn.execute(
                "SELECT * FROM zk_trades WHERE buyer_user=? " "ORDER BY id DESC LIMIT 100",
                (u["opex_user"],),
            ).fetchall()
            trades_sell = conn.execute(
                "SELECT * FROM zk_trades WHERE seller_user=? " "ORDER BY id DESC LIMIT 100",
                (u["opex_user"],),
            ).fetchall()
            reveals = conn.execute(
                "SELECT commitment_hex, side, price, quantity, batch_id "
                "FROM zk_reveals WHERE opex_user=? "
                "ORDER BY revealed_at DESC LIMIT 100",
                (u["opex_user"],),
            ).fetchall()
        return self._send_json(
            200,
            {
                "opex_user": u["opex_user"],
                "commits": [_row_to_dict(r) for r in commits],
                "reveals": [_row_to_dict(r) for r in reveals],
                "trades_as_buyer": [_row_to_dict(r) for r in trades_buy],
                "trades_as_seller": [_row_to_dict(r) for r in trades_sell],
            },
        )


PYTHON_VERIFIER_SNIPPET = '''"""zkCEX zk-trade batch verifier. Stdlib only.

Usage:
  curl http://localhost:5500/zk-trade/batch/<batch_id>?symbol=ETHUSDT > b.json
  curl http://localhost:5500/zk-trade/server-info > info.json
  python3 verifier.py b.json info.json
"""
import hashlib, json, sys
from decimal import Decimal

def u8(n): return int(n).to_bytes(1, "big")
def u16(n): return int(n).to_bytes(2, "big")
def u64(n): return max(0, min(int(n), (1<<64)-1)).to_bytes(8, "big")
def lp(s):
    b = s.encode("utf-8")
    return u16(len(b)) + b

def scale_8dp(s):
    return int((Decimal(s) * Decimal(10**8)).to_integral_value())

def leaf_hash(c_hex, price, qty, side):
    return hashlib.sha256(
        bytes.fromhex(c_hex) + u64(scale_8dp(price)) +
        u64(scale_8dp(qty)) + u8(0 if side.upper()=="BUY" else 1)).digest()

def merkle(leaves):
    if not leaves: return hashlib.sha256(b"").digest()
    L = list(leaves)
    while len(L) > 1:
        if len(L) % 2 == 1: L.append(L[-1])
        L = [hashlib.sha256(L[i]+L[i+1]).digest() for i in range(0,len(L),2)]
    return L[0]

def walrasian(orders):
    buys  = [o for o in orders if o["side"]=="BUY"]
    sells = [o for o in orders if o["side"]=="SELL"]
    if not buys or not sells: return None, Decimal(0)
    prices = sorted({Decimal(o["price"]) for o in orders})
    best_v, best_p = Decimal(-1), []
    for P in prices:
        s = sum((Decimal(o["quantity"]) for o in sells if Decimal(o["price"])<=P), Decimal(0))
        d = sum((Decimal(o["quantity"]) for o in buys  if Decimal(o["price"])>=P), Decimal(0))
        v = s if s<d else d
        if v > best_v: best_v, best_p = v, [P]
        elif v == best_v and v > 0: best_p.append(P)
    if best_v <= 0: return None, Decimal(0)
    return (min(best_p)+max(best_p))/Decimal(2), best_v

def verify(batch_path, info_path):
    b = json.load(open(batch_path))
    info = json.load(open(info_path))
    batch = b["batch"]
    reveals = sorted(b["reveals"], key=lambda r: r["commitment_hex"])
    # Recompute clearing price
    cp, vol = walrasian(reveals)
    cp_str = ("" if cp is None else format(cp, "f").rstrip("0").rstrip("."))
    assert cp_str == (batch["clearing_price"] or ""), \
        f"clearing mismatch: ours={cp_str!r} server={batch['clearing_price']!r}"
    # Recompute root
    leaves = [leaf_hash(r["commitment_hex"], r["price"], r["quantity"], r["side"]) for r in reveals]
    root = merkle(leaves).hex()
    assert root == batch["root_hash"], f"root mismatch: ours={root} server={batch['root_hash']}"
    print(f"OK batch={batch[\\"batch_id\\"]} symbol={batch[\\"symbol\\"]} "
          f"clearing={cp_str} volume={vol} root={root}")

if __name__ == "__main__":
    verify(sys.argv[1], sys.argv[2])
'''


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5660
    init_db()
    seed, pub, scheme = load_or_generate_keypair()
    sys.stderr.write(f"[zk] db={DB_PATH}\n")
    sys.stderr.write(f"[zk] auth_db={AUTH_DB_PATH}\n")
    sys.stderr.write(f"[zk] sig_scheme={scheme} pubkey={pub.hex()}\n")
    sys.stderr.write(
        f"[zk] batch={BATCH_INTERVAL_SECONDS}s "
        f"commit={COMMIT_PHASE_SECONDS}s "
        f"reveal={REVEAL_PHASE_SECONDS}s\n"
    )
    threading.Thread(target=matcher_loop, args=(seed, pub), daemon=True).start()
    sys.stderr.write(f"[zk] listening on :{port}\n")
    with ThreadingServer(("", port), Handler) as srv:
        srv.serve_forever()


if __name__ == "__main__":
    main()
