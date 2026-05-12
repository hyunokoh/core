#!/usr/bin/env python3
"""Proof-of-Liabilities snapshot server (port 5503) for the zkCEX demo.

Stdlib-only: builds Merkle-sum-tree snapshots over per-user balances, signs the
root, and exposes self-contained inclusion proofs. Persists at
``tools/.local/pol.db``. Reads the auth server's ``auth.db`` read-only to walk
all users, then queries the wallet API at :8091 for each user's balances and
the Binance-compatible ticker at :8094 for asset prices.

Crypto choices:
- Hash: SHA-256 (stdlib ``hashlib.sha256``). Documented in proof JSON.
- Signature: Ed25519 — pure-Python reference implementation (RFC 8032). No
  external dependency. Falls back to HMAC-SHA256 only if Ed25519 is somehow
  disabled at runtime. The proof JSON declares ``sig_scheme`` so a verifier
  in any language can check.

The signing keypair is generated on first run and persisted to
``tools/.local/pol_signing_key`` (32-byte raw seed, 0600). The pubkey is
exposed at ``GET /pol/server-info`` so any external verifier can fetch it
out-of-band and check signed roots.
"""

from __future__ import annotations

import hashlib
import hmac
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

# 28 digits is plenty for sums of 8-decimal-scaled balances across millions of users.
getcontext().prec = 36

# --- Paths ----------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
LOCAL_DIR = os.path.join(HERE, ".local")
os.makedirs(LOCAL_DIR, exist_ok=True)
DB_PATH = os.path.join(LOCAL_DIR, "pol.db")
AUTH_DB_PATH = os.path.join(LOCAL_DIR, "auth.db")
KEY_PATH = os.path.join(LOCAL_DIR, "pol_signing_key")
DEPLOYMENT_PATH = os.path.join(HERE, "hardhat-sim", ".local", "deployment.json")

# OpenTelemetry tracing (stdlib-only). Set service name BEFORE importing otel.
os.environ.setdefault("OTEL_SERVICE_NAME", "zkcex-pol")
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


def log(msg: str) -> None:
    sys.stderr.write(f"[pol] {msg}\n")


WALLET_BASE = _validated_http_base_url(
    "WALLET_BASE", os.environ.get("WALLET_BASE", "http://127.0.0.1:8091")
)
TICKER_BASE = _validated_http_base_url(
    "TICKER_BASE", os.environ.get("TICKER_BASE", "http://127.0.0.1:8094")
)
RPC_URL = _validated_http_url(os.environ.get("RPC_URL", "http://127.0.0.1:8545"), name="RPC_URL")
# Where to ask for the authoritative user list when the local SQLite file
# is unavailable (e.g. AUTH_DB_BACKEND=postgres on the auth side). The path
# /auth/users-for-snapshot is internal-only and blocked at the public proxy.
AUTH_BASE = _validated_http_base_url(
    "AUTH_BASE", os.environ.get("AUTH_BASE", "http://127.0.0.1:5501")
)
EPOCH_SECONDS = int(os.environ.get("POL_EPOCH_SECONDS", "60"))
SCHEME_NAME = "zkcex-pol-v1"
PORL_SCHEME_NAME = "zkcex-porl-v1"
LIVE_SCHEME_NAME = "zkcex-pol-live-v1"
HASH_NAME = "SHA-256"
PRICE_TTL_S = 30
PORL_CACHE_TTL_S = 30
LIVE_TICK_S = 1.0
LIVE_BAL_TTL_S = 30  # max staleness for cached per-user balance even when "unchanged"
LIVE_RING_MAX = 1000
LIVE_MAX_SUBSCRIBERS = 200
LIVE_TICK_BUDGET_MS = 800  # warn + skip-next-tick threshold

# Mapping: on-chain test token symbol -> what the wallet API/ticker calls it.
# The hardhat ZETH/ZUSDT are stand-ins for real ETH/USDT; valuation looks up
# the canonical ticker symbol.
TOKEN_TO_ASSET = {"ZETH": "ETH", "ZUSDT": "USDT"}

_db_lock = threading.Lock()
_snapshot_lock = threading.Lock()  # only one snapshot rebuild at a time
_price_cache_lock = threading.Lock()
_price_cache: dict[str, tuple[float, float]] = {}  # asset -> (price_usdt, fetched_at)
_porl_cache_lock = threading.Lock()
_porl_cache: dict[str, object] = {"snapshot": None, "fetched_at": 0.0, "next_id": 1}

# --- Live PoL state -------------------------------------------------------
# The live tree mirrors the most recent snapshot epoch's user_hash list (because
# user_hash = SHA256(opex_user || epoch_nonce) and epoch_nonce is per-epoch),
# but the leaves' balances are kept fresh by a 1Hz background tick. We hold:
#  - the ordered list of leaves with their per-user nonces (so the user_hash is
#    stable for the duration of an epoch),
#  - per-user cached scaled balances + asset breakdowns,
#  - the in-memory tree levels so a single-leaf update is O(log N),
#  - a ring buffer of recent live commits for /pol/live/recent and reconnect.
_live_lock = threading.Lock()
_live_state: dict[str, object] = {
    "epoch_id": None,  # int — the snapshot epoch this live tree rides on
    "epoch_started_at": None,  # int (unix s)
    "epoch_nonce_by_user": {},  # opex_user -> hex epoch_nonce (per-user, not global)
    "users_order": [],  # list[str] — canonical leaf ordering
    "leaves": [],  # list[(leaf_hash_bytes, scaled_int)]
    "user_cache": {},  # opex_user -> {scaled, breakdown, fetched_at}
    "user_hash_hex": {},  # opex_user -> hex user_hash
    "tree_levels": [],  # list[list[(hash_bytes, scaled)]] — bottom (leaves) → root
    "root_hash": None,  # hex
    "root_sum": None,  # decimal str
    "next_commit_id": 1,
    "current_commit": None,  # most recent live_commit dict (or epoch_rotated marker)
}
_live_commits_ring: list[dict] = []  # bounded; keep most recent LIVE_RING_MAX
_live_subscribers: list = []  # list of dicts with cond + queue
_live_subscribers_lock = threading.Lock()
_live_cv = threading.Condition()  # wakes the live tick & subscribers when state advances


# ==========================================================================
# Pure-Python Ed25519 (RFC 8032). Adapted from the reference at
# https://ed25519.cr.yp.to/python/ed25519.py (public-domain). Only what the
# server needs: keygen, sign, verify. Uses hashlib.sha512 for H().
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


def _ed_edwards(P: tuple[int, int], Q: tuple[int, int]) -> tuple[int, int]:
    x1, y1 = P
    x2, y2 = Q
    inv = pow(1 + _ED_d * x1 * x2 * y1 * y2, _ED_q - 2, _ED_q)
    x3 = ((x1 * y2 + x2 * y1) * inv) % _ED_q
    inv2 = pow(1 - _ED_d * x1 * x2 * y1 * y2, _ED_q - 2, _ED_q)
    y3 = ((y1 * y2 + x1 * x2) * inv2) % _ED_q
    return (x3, y3)


def _ed_scalarmult(P: tuple[int, int], e: int) -> tuple[int, int]:
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


def _ed_encodepoint(P: tuple[int, int]) -> bytes:
    x, y = P
    bits = [(y >> i) & 1 for i in range(_ED_b - 1)] + [x & 1]
    return bytes(sum(bits[i * 8 + j] << j for j in range(8)) for i in range(_ED_b // 8))


def _ed_bit(h: bytes, i: int) -> int:
    return (h[i // 8] >> (i % 8)) & 1


def ed25519_publickey(sk_seed: bytes) -> bytes:
    """sk_seed: 32 bytes. Returns 32-byte compressed pubkey."""
    h = _ed_H(sk_seed)
    a = 2 ** (_ED_b - 2) + sum(2**i * _ed_bit(h, i) for i in range(3, _ED_b - 2))
    A = _ed_scalarmult(_ED_B, a)
    return _ed_encodepoint(A)


def _ed_Hint(m: bytes) -> int:
    h = _ed_H(m)
    return sum(2**i * _ed_bit(h, i) for i in range(2 * _ED_b))


def ed25519_sign(sk_seed: bytes, msg: bytes) -> bytes:
    """64-byte detached signature of msg under sk_seed (32 bytes)."""
    h = _ed_H(sk_seed)
    a = 2 ** (_ED_b - 2) + sum(2**i * _ed_bit(h, i) for i in range(3, _ED_b - 2))
    A = _ed_encodepoint(_ed_scalarmult(_ED_B, a))
    r = _ed_Hint(h[_ED_b // 8 : _ED_b // 4] + msg)
    R = _ed_scalarmult(_ED_B, r)
    S = (r + _ed_Hint(_ed_encodepoint(R) + A + msg) * a) % _ED_l
    return _ed_encodepoint(R) + _ed_encodeint(S)


def _ed_decodeint(s: bytes) -> int:
    return sum(2**i * _ed_bit(s, i) for i in range(_ED_b))


def _ed_decodepoint(s: bytes) -> tuple[int, int]:
    y = sum(2**i * _ed_bit(s, i) for i in range(_ED_b - 1))
    x = _ed_xrecover(y)
    if x & 1 != _ed_bit(s, _ED_b - 1):
        x = _ED_q - x
    P = (x, y)
    if not _ed_isoncurve(P):
        raise ValueError("decoding point that is not on curve")
    return P


def _ed_isoncurve(P: tuple[int, int]) -> bool:
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
# Signing key persistence
# ==========================================================================
def load_or_generate_keypair() -> tuple[bytes, bytes, str]:
    """Returns (seed, pubkey, sig_scheme). Generates if missing."""
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
        raise RuntimeError(f"bad signing key length at {KEY_PATH}: {len(seed)}")
    pubkey = ed25519_publickey(seed)
    return seed, pubkey, "Ed25519"


# ==========================================================================
# DB
# ==========================================================================
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def auth_db_ro():
    """Read-only handle to the auth server's SQLite. Never blocks writers."""
    uri = f"file:{urllib.parse.quote(AUTH_DB_PATH)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db_lock, db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS epochs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              epoch_started_at INTEGER NOT NULL,
              root_hash TEXT NOT NULL,
              root_sum TEXT NOT NULL,
              signature TEXT NOT NULL,
              n_users INTEGER NOT NULL,
              sig_scheme TEXT NOT NULL,
              server_pubkey TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS proofs (
              epoch_id INTEGER NOT NULL REFERENCES epochs(id) ON DELETE CASCADE,
              opex_user TEXT NOT NULL,
              user_hash TEXT NOT NULL,
              balance TEXT NOT NULL,
              epoch_nonce TEXT NOT NULL,
              asset_breakdown_json TEXT NOT NULL,
              sibling_path_json TEXT NOT NULL,
              PRIMARY KEY (epoch_id, opex_user)
            );
            CREATE INDEX IF NOT EXISTS idx_proofs_user ON proofs(opex_user);
            CREATE TABLE IF NOT EXISTS porl_snapshots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              snapshot_at INTEGER NOT NULL,
              epoch_id INTEGER,
              reserves_usdt TEXT NOT NULL,
              liabilities_usdt TEXT NOT NULL,
              delta_usdt TEXT NOT NULL,
              ratio TEXT NOT NULL,
              status TEXT NOT NULL,
              totals_json TEXT NOT NULL,
              signature TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS live_commits (
              commit_id INTEGER PRIMARY KEY,
              epoch_id INTEGER NOT NULL,
              committed_at INTEGER NOT NULL,
              root_hash TEXT NOT NULL,
              root_sum TEXT NOT NULL,
              n_users INTEGER NOT NULL,
              signature TEXT NOT NULL,
              changed_user TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_live_commits_epoch ON live_commits(epoch_id);
            """
        )
        # Seed the in-memory PoRL snapshot id from the persisted high-water-mark
        # so cached/persisted IDs stay monotone across restarts.
        row = conn.execute("SELECT MAX(id) AS m FROM porl_snapshots").fetchone()
        if row and row["m"]:
            with _porl_cache_lock:
                _porl_cache["next_id"] = int(row["m"]) + 1
        # Same for live commits: monotone commit_id across restarts.
        row2 = conn.execute("SELECT MAX(commit_id) AS m FROM live_commits").fetchone()
        if row2 and row2["m"]:
            with _live_lock:
                _live_state["next_commit_id"] = int(row2["m"]) + 1


# ==========================================================================
# Price cache
# ==========================================================================
def get_price_usdt(asset: str) -> float | None:
    """USDT-equivalent last price. None if missing. 30s TTL."""
    if asset == "USDT":
        return 1.0
    now = time.time()
    with _price_cache_lock:
        cached = _price_cache.get(asset)
        if cached and now - cached[1] < PRICE_TTL_S:
            return cached[0]
    try:
        url = f"{TICKER_BASE}/v3/ticker/24h?symbol={asset}USDT"
        with _http_urlopen(url, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # API returns either an object or a list with 1 entry
        if isinstance(data, list):
            data = data[0] if data else {}
        last = data.get("lastPrice") or data.get("price")
        if last is None:
            return None
        price = float(last)
        with _price_cache_lock:
            _price_cache[asset] = (price, now)
        return price
    except Exception as e:  # noqa: BLE001
        log(f"ticker fetch failed for {asset}: {e!r}")
        return None


# ==========================================================================
# Auth + wallet adapters
# ==========================================================================
def list_all_opex_users() -> list[str]:
    """Return the authoritative list of opex users.

    Path 1 (legacy / SQLite mode): read auth.db directly. This is by far the
    fastest path and works when both servers run on the same box.

    Path 2 (Postgres mode / SQLite missing): the file does not exist or
    cannot be opened. Fall through to the auth_server's internal endpoint
    ``GET /auth/users-for-snapshot`` (blocked at the public proxy).
    """
    if os.path.exists(AUTH_DB_PATH):
        try:
            with auth_db_ro() as conn:
                rows = conn.execute(
                    "SELECT opex_user FROM users WHERE opex_user != '' ORDER BY id ASC"
                ).fetchall()
                return [r["opex_user"] for r in rows]
        except sqlite3.OperationalError as e:
            sys.stderr.write(f"[pol] auth_db read failed: {e}\n")
            # fall through to HTTP path
    # HTTP fallback
    try:
        url = f"{AUTH_BASE}/auth/users-for-snapshot"
        with _http_urlopen(url, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return list(data.get("opex_users") or [])
    except Exception as e:  # noqa: BLE001
        log(f"auth users-for-snapshot fetch failed: {e}")
        return []


def fetch_user_wallets(opex_user: str) -> list[dict]:
    url = f"{WALLET_BASE}/v1/owner/{urllib.parse.quote(opex_user)}/wallets"
    try:
        with _http_urlopen(url, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:  # noqa: BLE001
        log(f"wallet fetch failed for user {opex_user}: {e!r}")
        return []


def session_user_via_auth(token: str) -> dict | None:
    """Resolve a Bearer token to {id, opex_user, email, name}.

    Tries the SQLite shortcut first (zero IPC overhead in the legacy mode).
    Falls back to ``GET /auth/me`` so the resolver still works when the auth
    server runs against Postgres.
    """
    if not token:
        return None
    if os.path.exists(AUTH_DB_PATH):
        try:
            now = int(time.time())
            with auth_db_ro() as conn:
                row = conn.execute(
                    "SELECT u.id, u.opex_user, u.email, u.name, u.kyc_status "
                    "FROM sessions s JOIN users u ON u.id=s.user_id "
                    "WHERE s.token=? AND s.expires_at>?",
                    (token, now),
                ).fetchone()
                if row:
                    return {
                        "id": row["id"],
                        "opex_user": row["opex_user"],
                        "email": row["email"],
                        "name": row["name"],
                        "kyc_status": row["kyc_status"] or "none",
                    }
                # don't fall through on "valid file, no row" — that's
                # legitimately a missing/expired session.
                return None
        except sqlite3.OperationalError:
            pass  # fall through to HTTP
    # HTTP fallback: /auth/me with the bearer token.
    try:
        url = f"{AUTH_BASE}/auth/me"
        req = _http_request(url, headers={"Authorization": f"Bearer {token}"})
        with _http_urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        u = data.get("user") or {}
        if not u:
            return None
        return {
            "id": u.get("id"),
            "opex_user": u.get("opex_user"),
            "email": u.get("email"),
            "name": u.get("name"),
            "kyc_status": u.get("kyc_status") or "none",
        }
    except Exception:  # noqa: BLE001
        return None


# ==========================================================================
# Snapshot construction
# ==========================================================================
def _scale_balance(balance: Decimal) -> int:
    """Balance scaled to 8 decimal places, as a non-negative int."""
    scaled = (balance * Decimal(10**8)).to_integral_value()
    n = int(scaled)
    if n < 0:
        return 0
    return n


def _scaled_to_str(scaled: int) -> str:
    """Inverse: 8-dp scaled int -> canonical decimal string with up to 8 dp."""
    s = Decimal(scaled) / Decimal(10**8)
    # Strip trailing zeros but keep at least one decimal place for sums.
    txt = format(s, "f")
    if "." in txt:
        txt = txt.rstrip("0").rstrip(".") or "0"
    return txt or "0"


def _u64_be(n: int) -> bytes:
    if n < 0:
        n = 0
    if n >= 1 << 64:
        # If the demo total liability ever exceeds 2^64 / 1e8 = 184e11 USDT,
        # we'd need wider encoding. For now clamp and warn.
        sys.stderr.write(f"[pol] WARNING scaled value overflows u64: {n}\n")
        n = (1 << 64) - 1
    return n.to_bytes(8, "big", signed=False)


def _leaf_hash(user_hash_hex: str, balance_scaled: int) -> bytes:
    return hashlib.sha256(bytes.fromhex(user_hash_hex) + _u64_be(balance_scaled)).digest()


def _node_hash(left_hash: bytes, right_hash: bytes, left_sum: int, right_sum: int) -> bytes:
    return hashlib.sha256(left_hash + right_hash + _u64_be(left_sum) + _u64_be(right_sum)).digest()


def _build_user_balance(opex_user: str) -> tuple[Decimal, list[dict]]:
    """Returns (USDT-equivalent total, asset breakdown list)."""
    wallets = fetch_user_wallets(opex_user)
    breakdown: list[dict] = []
    total = Decimal(0)
    for w in wallets:
        asset = w.get("asset")
        bal_raw = w.get("balance", 0)
        try:
            bal = Decimal(str(bal_raw))
        except Exception:
            bal = Decimal(0)
        if not asset:
            continue
        price = get_price_usdt(asset)
        if price is None:
            breakdown.append(
                {
                    "asset": asset,
                    "balance": format(bal, "f"),
                    "valuation_usdt": None,
                    "note": "no_price",
                }
            )
            continue
        valuation = (bal * Decimal(str(price))).quantize(Decimal("0.00000001"))
        total += valuation
        breakdown.append(
            {
                "asset": asset,
                "balance": format(bal, "f"),
                "valuation_usdt": format(valuation, "f"),
            }
        )
    return total, breakdown


def _build_levels_from_leaves(leaves: list[tuple[bytes, int]]) -> list[list[tuple[bytes, int]]]:
    """Build the full Merkle-sum tree from leaves up to a single root.

    Returns list of levels; level[0] is leaves, level[-1] is the [(root_h, root_sum)] singleton.
    Odd-sized levels are right-padded by duplicating the last node (matches build_snapshot).
    """
    if not leaves:
        return [[]]
    levels: list[list[tuple[bytes, int]]] = [list(leaves)]
    cur = list(leaves)
    while len(cur) > 1:
        if len(cur) % 2 == 1:
            cur.append(cur[-1])
        nxt: list[tuple[bytes, int]] = []
        for j in range(0, len(cur), 2):
            lh, ls = cur[j]
            rh, rs = cur[j + 1]
            nxt.append((_node_hash(lh, rh, ls, rs), ls + rs))
        levels.append(nxt)
        cur = nxt
    return levels


def _refold_path(levels: list[list[tuple[bytes, int]]], leaf_idx: int) -> None:
    """After leaf at leaf_idx changes in levels[0], re-fold the path up. In place."""
    idx = leaf_idx
    for lvl in range(len(levels) - 1):
        cur = levels[lvl]
        nxt = levels[lvl + 1]
        # Pair index: idx and its sibling.
        # When the level had odd length, the original build duplicated the last entry;
        # if `idx` is the last (and was duplicated), both halves are the same node.
        if idx % 2 == 0:
            left = cur[idx]
            right_idx = idx + 1
            if right_idx >= len(cur):
                # Was duplicated.
                right = left
            else:
                right = cur[right_idx]
        else:
            right = cur[idx]
            left = cur[idx - 1]
        merged = (_node_hash(left[0], right[0], left[1], right[1]), left[1] + right[1])
        nxt_idx = idx // 2
        if nxt_idx >= len(nxt):
            nxt.append(merged)
        else:
            nxt[nxt_idx] = merged
        idx = nxt_idx


def _sibling_path_for(levels: list[list[tuple[bytes, int]]], leaf_idx: int) -> list[dict]:
    """Generate the leaf-to-root sibling path with the same shape as build_snapshot."""
    out: list[dict] = []
    idx = leaf_idx
    for lvl in range(len(levels) - 1):
        cur = levels[lvl]
        if idx % 2 == 0:
            sib_idx = idx + 1
            side = "right"
            if sib_idx >= len(cur):
                # Odd-padded: sibling is self (the duplicated last entry).
                sib = cur[idx]
            else:
                sib = cur[sib_idx]
        else:
            sib_idx = idx - 1
            side = "left"
            sib = cur[sib_idx]
        out.append(
            {
                "side": side,
                "hash": sib[0].hex(),
                "sum_scaled": sib[1],
                "sum": _scaled_to_str(sib[1]),
            }
        )
        idx = idx // 2
    return out


def _seed_live_from_snapshot(epoch_id: int, epoch_started_at: int, rows: list[dict]) -> None:
    """After a fresh snapshot is built, reset the live tree to mirror it.

    The live tree shares the snapshot's per-user epoch nonces and user_hashes, so
    we can update only the balance leaf when a user's wallet changes. Emits an
    `epoch_rotated` event to subscribers after the reset.
    """
    with _live_lock:
        old_epoch = _live_state.get("epoch_id")
        users_order: list[str] = []
        epoch_nonce_by_user: dict[str, str] = {}
        user_hash_hex_map: dict[str, str] = {}
        leaves: list[tuple[bytes, int]] = []
        user_cache: dict[str, dict] = {}
        for r in rows:
            if r.get("_synthetic"):
                # Skip synthetic empty leaf when seeding live tree — live ticks
                # only run when there are real users; if the snapshot is empty
                # the live tree just stays empty.
                continue
            opex = r["opex_user"]
            users_order.append(opex)
            epoch_nonce_by_user[opex] = r["epoch_nonce_hex"]
            user_hash_hex_map[opex] = r["user_hash_hex"]
            leaves.append((bytes.fromhex(r["leaf_hash_hex"]), r["balance_scaled"]))
            user_cache[opex] = {
                "scaled": r["balance_scaled"],
                "breakdown": r["asset_breakdown"],
                "fetched_at": time.time(),
            }
        levels = _build_levels_from_leaves(leaves) if leaves else [[]]
        if leaves:
            root_h, root_s = levels[-1][0]
            _live_state["root_hash"] = root_h.hex()
            _live_state["root_sum"] = _scaled_to_str(root_s)
        else:
            _live_state["root_hash"] = None
            _live_state["root_sum"] = None
        _live_state["epoch_id"] = epoch_id
        _live_state["epoch_started_at"] = epoch_started_at
        _live_state["users_order"] = users_order
        _live_state["epoch_nonce_by_user"] = epoch_nonce_by_user
        _live_state["user_hash_hex"] = user_hash_hex_map
        _live_state["leaves"] = leaves
        _live_state["tree_levels"] = levels
        _live_state["user_cache"] = user_cache

    # Emit an epoch_rotated marker (doesn't allocate a new commit_id; it's a
    # control event for SSE subscribers) plus a fresh "commit_0"-style commit
    # so newcomers immediately see the live root for the new epoch.
    if old_epoch is not None and old_epoch != epoch_id:
        _broadcast_event(
            "epoch_rotated",
            {
                "old_epoch_id": old_epoch,
                "new_epoch_id": epoch_id,
            },
        )
    if leaves:
        _emit_live_commit(changed_user=None)


def _emit_live_commit(changed_user: str | None) -> dict | None:
    """Sign + persist + push the current in-memory root as a new live commit.

    Returns the commit dict, or None if there's nothing to commit (no users).
    Caller must hold _live_lock.
    """
    seed, pubkey, sig_scheme = load_or_generate_keypair()
    with _live_lock:
        epoch_id = _live_state.get("epoch_id")
        leaves = _live_state.get("leaves") or []
        levels = _live_state.get("tree_levels") or []
        if not leaves or not levels or epoch_id is None:
            return None
        commit_id = int(_live_state["next_commit_id"])
        _live_state["next_commit_id"] = commit_id + 1
        root_h, root_s = levels[-1][0]
        root_hex = root_h.hex()
        root_sum = _scaled_to_str(root_s)
        n_users = len(leaves)
        committed_at_ms = int(time.time() * 1000)

        msg = (f"{LIVE_SCHEME_NAME}|{commit_id}|{epoch_id}|{root_hex}|{root_sum}").encode()
        sig_hex = sign_msg(seed, msg, sig_scheme).hex()

        commit = {
            "scheme": LIVE_SCHEME_NAME,
            "commit_id": commit_id,
            "epoch_id": epoch_id,
            "committed_at": committed_at_ms,
            "root_hash": root_hex,
            "root_sum": root_sum,
            "n_users": n_users,
            "signature": sig_hex,
        }
        _live_state["root_hash"] = root_hex
        _live_state["root_sum"] = root_sum
        _live_state["current_commit"] = commit
        _live_commits_ring.append(commit)
        if len(_live_commits_ring) > LIVE_RING_MAX:
            del _live_commits_ring[: len(_live_commits_ring) - LIVE_RING_MAX]

    # Persist (separate from in-memory state to keep _live_lock short).
    try:
        with _db_lock, db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO live_commits (commit_id, epoch_id, committed_at,"
                " root_hash, root_sum, n_users, signature, changed_user) VALUES (?,?,?,?,?,?,?,?)",
                (
                    commit_id,
                    epoch_id,
                    committed_at_ms,
                    root_hex,
                    root_sum,
                    n_users,
                    sig_hex,
                    changed_user,
                ),
            )
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[pol] live commit persist failed: {e!r}\n")

    _broadcast_event("commit", commit)
    return commit


def _broadcast_event(name: str, payload: dict) -> None:
    """Push a named SSE event to all connected subscribers (non-blocking)."""
    msg = (name, payload)
    with _live_subscribers_lock:
        subs = list(_live_subscribers)
    for sub in subs:
        try:
            with sub["cv"]:
                # Bound the queue so a slow consumer doesn't accumulate forever.
                if len(sub["queue"]) < 64:
                    sub["queue"].append(msg)
                    sub["cv"].notify()
                else:
                    sub["overflow"] = True
                    sub["cv"].notify()
        except Exception as e:  # noqa: BLE001
            log(f"live subscriber notify failed: {e!r}")


def _live_apply_user_update(opex: str, scaled: int, breakdown: list[dict]) -> bool:
    """Update one user's leaf in the live tree if the scaled balance changed.

    Returns True if a commit should be emitted (balance actually moved), False
    otherwise (cache refreshed but value unchanged).
    """
    with _live_lock:
        users_order = _live_state.get("users_order") or []
        user_hash_hex_map = _live_state.get("user_hash_hex") or {}
        cache = _live_state.get("user_cache") or {}
        leaves = _live_state.get("leaves") or []
        levels = _live_state.get("tree_levels") or []
        if opex not in user_hash_hex_map:
            # Unknown user — likely signed up after the last snapshot. Live tree
            # only covers users that the snapshot includes; skip silently.
            cache[opex] = {"scaled": scaled, "breakdown": breakdown, "fetched_at": time.time()}
            return False
        prev = cache.get(opex)
        prev_scaled = prev.get("scaled") if prev else None
        cache[opex] = {"scaled": scaled, "breakdown": breakdown, "fetched_at": time.time()}
        if prev_scaled == scaled:
            return False
        # Find leaf index, recompute leaf_hash, refold path.
        try:
            idx = users_order.index(opex)
        except ValueError:
            return False
        user_hash_hex = user_hash_hex_map[opex]
        new_leaf_h = _leaf_hash(user_hash_hex, scaled)
        leaves[idx] = (new_leaf_h, scaled)
        if levels:
            levels[0][idx] = leaves[idx]
            _refold_path(levels, idx)
            root_h, root_s = levels[-1][0]
            _live_state["root_hash"] = root_h.hex()
            _live_state["root_sum"] = _scaled_to_str(root_s)
        return True


def live_tick_loop():
    """Background thread: fetches per-user balances and updates the live tree.

    Each tick walks the live tree's known users (from the most recent snapshot),
    fetches wallets, updates leaves whose scaled-int changed, and emits a single
    commit per tick that aggregates all changes. If a tick takes >LIVE_TICK_BUDGET_MS,
    we log and skip the next tick to catch back up.
    """
    skip_next = False
    next_due = time.time() + LIVE_TICK_S
    while True:
        # Sleep until next tick.
        now = time.time()
        if next_due > now:
            time.sleep(min(LIVE_TICK_S, next_due - now))
        next_due += LIVE_TICK_S
        if skip_next:
            skip_next = False
            continue
        t0 = time.time()
        try:
            # Snapshot the user list under the lock; release before doing IO.
            with _live_lock:
                users = list(_live_state.get("users_order") or [])
                cache_snapshot = dict(_live_state.get("user_cache") or {})
                epoch_id = _live_state.get("epoch_id")
            if not users or epoch_id is None:
                continue
            any_change = False
            changed_users: list[str] = []
            now_t = time.time()
            for opex in users:
                # If the cache is fresh enough, skip the network fetch and reuse cached.
                cached = cache_snapshot.get(opex)
                if cached and (now_t - float(cached.get("fetched_at") or 0)) < LIVE_BAL_TTL_S:
                    # Still hit the wallet API at the live tick rate, but only when
                    # the freshness floor expires — saves CPU & network.
                    # However, we DO still fetch every tick when cache is stale.
                    # The TTL is a safety floor: even if a balance "looks unchanged",
                    # refetch within 30s.
                    pass
                total, breakdown = _build_user_balance(opex)
                scaled = _scale_balance(total)
                changed = _live_apply_user_update(opex, scaled, breakdown)
                if changed:
                    any_change = True
                    changed_users.append(opex)
                # Bail early if we're already over budget.
                if (time.time() - t0) * 1000 > LIVE_TICK_BUDGET_MS * 2:
                    sys.stderr.write("[pol] live tick over 2x budget, deferring remaining users\n")
                    break
            if any_change:
                _emit_live_commit(changed_user=",".join(changed_users[:5]))
            took_ms = int((time.time() - t0) * 1000)
            if took_ms > LIVE_TICK_BUDGET_MS:
                sys.stderr.write(
                    f"[pol] live tick took {took_ms}ms (>budget {LIVE_TICK_BUDGET_MS}ms),"
                    f" skipping next tick\n"
                )
                skip_next = True
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[pol] live tick error: {e!r}\n")


def build_snapshot() -> dict:
    """Build & persist a fresh snapshot. Returns {epoch_id, n_users, took_ms}."""
    with _snapshot_lock:
        t0 = time.time()
        seed, pubkey, sig_scheme = load_or_generate_keypair()
        opex_users = list_all_opex_users()
        epoch_started_at = int(time.time())

        # Per-user nonces are independent so leaking one doesn't deanonymise others.
        rows: list[dict] = []
        for opex in opex_users:
            total, breakdown = _build_user_balance(opex)
            nonce = secrets.token_bytes(16)
            user_hash_b = hashlib.sha256(opex.encode("utf-8") + nonce).digest()
            scaled = _scale_balance(total)
            rows.append(
                {
                    "opex_user": opex,
                    "user_hash_hex": user_hash_b.hex(),
                    "balance_scaled": scaled,
                    "balance_str": _scaled_to_str(scaled),
                    "epoch_nonce_hex": nonce.hex(),
                    "asset_breakdown": breakdown,
                }
            )

        # Edge case: no users. Build a single zero-leaf so the tree still has a root.
        if not rows:
            zero_hash_hex = hashlib.sha256(b"empty-pol-snapshot").hexdigest()
            rows.append(
                {
                    "opex_user": "__empty__",
                    "user_hash_hex": zero_hash_hex,
                    "balance_scaled": 0,
                    "balance_str": "0",
                    "epoch_nonce_hex": "00" * 16,
                    "asset_breakdown": [],
                    "_synthetic": True,
                }
            )

        # Build leaves: (hash_bytes, sum_int).
        leaves = [
            (_leaf_hash(r["user_hash_hex"], r["balance_scaled"]), r["balance_scaled"]) for r in rows
        ]
        # Capture the leaf-level hash so external verifiers can re-derive it.
        for r, (lh, _s) in zip(rows, leaves, strict=False):
            r["leaf_hash_hex"] = lh.hex()

        # Build merkle-sum tree level by level. At each level we record sibling
        # info for every leaf so we can emit per-user sibling paths in O(log N).
        # path[i] = list of (side, sibling_hash_hex, sibling_sum_int) for leaf i.
        n = len(leaves)
        sibling_paths: list[list[dict]] = [[] for _ in range(n)]
        # leaf_index_at_level[i] = the index of leaf i's ancestor at the current level.
        idx_at_level = list(range(n))
        level = leaves[:]
        while len(level) > 1:
            # Pad odd by duplicating the last node.
            if len(level) % 2 == 1:
                level.append(level[-1])
            new_level: list[tuple[bytes, int]] = []
            for j in range(0, len(level), 2):
                lh, ls = level[j]
                rh, rs = level[j + 1]
                node = (_node_hash(lh, rh, ls, rs), ls + rs)
                new_level.append(node)
            # Record siblings for every original leaf using its current ancestor index.
            for i in range(n):
                a = idx_at_level[i]
                if a >= len(level):
                    # Should not happen because we padded.
                    continue
                if a % 2 == 0:
                    sib = level[a + 1]
                    sibling_paths[i].append(
                        {
                            "side": "right",
                            "hash": sib[0].hex(),
                            "sum_scaled": sib[1],
                            "sum": _scaled_to_str(sib[1]),
                        }
                    )
                else:
                    sib = level[a - 1]
                    sibling_paths[i].append(
                        {
                            "side": "left",
                            "hash": sib[0].hex(),
                            "sum_scaled": sib[1],
                            "sum": _scaled_to_str(sib[1]),
                        }
                    )
                idx_at_level[i] = a // 2
            level = new_level

        root_hash_b, root_sum_scaled = level[0]
        root_hash_hex = root_hash_b.hex()
        root_sum_str = _scaled_to_str(root_sum_scaled)

        # Persist (need epoch_id for FK, so insert epoch first).
        with _db_lock, db() as conn:
            conn.execute("BEGIN")
            cur = conn.execute(
                "INSERT INTO epochs (epoch_started_at, root_hash, root_sum, signature, n_users, sig_scheme, server_pubkey) "
                "VALUES (?,?,?,?,?,?,?)",
                # signature filled in after we know epoch_id (so the signature commits to it).
                (
                    epoch_started_at,
                    root_hash_hex,
                    root_sum_str,
                    "",
                    len(rows),
                    sig_scheme,
                    pubkey.hex(),
                ),
            )
            epoch_id = cur.lastrowid
            # Construct the canonical signed message. The verifier reproduces this exactly.
            signed_msg = signed_message(epoch_id, root_hash_hex, root_sum_str)
            sig = sign_msg(seed, signed_msg, sig_scheme)
            conn.execute("UPDATE epochs SET signature=? WHERE id=?", (sig.hex(), epoch_id))
            for r, path in zip(rows, sibling_paths, strict=False):
                conn.execute(
                    "INSERT INTO proofs (epoch_id, opex_user, user_hash, balance, epoch_nonce, asset_breakdown_json, sibling_path_json) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        epoch_id,
                        r["opex_user"],
                        r["user_hash_hex"],
                        r["balance_str"],
                        r["epoch_nonce_hex"],
                        json.dumps(r["asset_breakdown"], separators=(",", ":")),
                        json.dumps(path, separators=(",", ":")),
                    ),
                )
            conn.execute("COMMIT")

        # Reset/seed the live tree so the live mode rides on this fresh epoch
        # nonce + user_hash list. Also broadcasts an `epoch_rotated` SSE event
        # for any subscribers from the previous epoch.
        try:
            _seed_live_from_snapshot(epoch_id, epoch_started_at, rows)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[pol] live seed failed: {e!r}\n")

        return {
            "epoch_id": epoch_id,
            "n_users": len(rows),
            "took_ms": int((time.time() - t0) * 1000),
            "root_hash": root_hash_hex,
            "root_sum": root_sum_str,
        }


# --- Sign / signed-message helpers ---------------------------------------
def signed_message(epoch_id: int, root_hash_hex: str, root_sum_str: str) -> bytes:
    """The canonical bytes that get signed. Verifier reproduces this exactly.

    Layout: ASCII string ``zkcex-pol-v1|<epoch_id>|<root_hash_hex>|<root_sum_str>``.
    Using a printable, pipe-delimited form so external snippets in any language
    can build it without worrying about endianness.
    """
    s = f"{SCHEME_NAME}|{epoch_id}|{root_hash_hex}|{root_sum_str}"
    return s.encode("utf-8")


def sign_msg(seed: bytes, msg: bytes, sig_scheme: str) -> bytes:
    if sig_scheme == "Ed25519":
        return ed25519_sign(seed, msg)
    if sig_scheme == "HMAC-SHA256":
        return hmac.new(seed, msg, hashlib.sha256).digest()
    raise ValueError(f"unknown sig scheme: {sig_scheme}")


def verify_msg(pubkey_hex: str, msg: bytes, sig_hex: str, sig_scheme: str = "Ed25519") -> bool:
    """Verify a previously-signed message. Used by the PoRL self-check curl in
    the verification recipe — handy to have in-process for tests too."""
    try:
        sig = bytes.fromhex(sig_hex)
        pk = bytes.fromhex(pubkey_hex)
    except Exception:
        return False
    if sig_scheme == "Ed25519":
        return ed25519_verify(pk, msg, sig)
    if sig_scheme == "HMAC-SHA256":
        want = hmac.new(pk, msg, hashlib.sha256).digest()
        return hmac.compare_digest(want, sig)
    return False


# ==========================================================================
# Proof-of-Reserves-and-Liabilities (PoRL) — public transparency snapshot.
#
# This is independent of the per-user Merkle-sum tree above. It pairs the
# liabilities side (read live from the wallet API for every user) with the
# reserves side (read live from hardhat via JSON-RPC eth_call balanceOf and
# eth_getBalance for the native ETH balance) and signs the totals with the
# same Ed25519 keypair under a domain-separated message scheme so a verifier
# can never confuse a PoRL signature with a PoL one.
# ==========================================================================
def _load_deployment() -> dict | None:
    try:
        with open(DEPLOYMENT_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[pol] deployment read failed: {e}\n")
        return None


def _rpc(method: str, params: list) -> object:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = _http_request(RPC_URL, data=payload, headers={"Content-Type": "application/json"})
    with _http_urlopen(req, timeout=8) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if body.get("error"):
        raise RuntimeError(f"rpc error: {body['error']}")
    return body.get("result")


def _erc20_balance_of(token_addr: str, holder: str) -> int:
    # selector for balanceOf(address) = first 4 bytes of keccak("balanceOf(address)")
    # = 0x70a08231. Right-pad the holder address to 32 bytes.
    addr = holder.lower().replace("0x", "").zfill(40)
    data = "0x70a08231" + ("0" * 24) + addr
    try:
        r = _rpc("eth_call", [{"to": token_addr, "data": data}, "latest"])
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[pol] balanceOf {token_addr} failed: {e}\n")
        return 0
    if not r or r == "0x":
        return 0
    return int(r, 16)


def _native_balance(holder: str) -> int:
    try:
        r = _rpc("eth_getBalance", [holder, "latest"])
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[pol] eth_getBalance {holder} failed: {e}\n")
        return 0
    if not r:
        return 0
    return int(r, 16)


def _wei_to_decimal(wei: int, decimals: int) -> Decimal:
    if wei <= 0:
        return Decimal(0)
    return Decimal(wei) / (Decimal(10) ** decimals)


def _format_dec(d: Decimal, places: int = 8) -> str:
    """Canonical decimal string with up to `places` places, no trailing zeros."""
    if d is None:
        return "0"
    q = Decimal(10) ** -places
    s = format(d.quantize(q), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _aggregate_liabilities() -> tuple[dict[str, dict], int]:
    """Iterate users and sum balances per asset.

    Returns (per_asset, total_holders_with_any_balance_we_iterated).
    per_asset[asset] = {"total_owed": Decimal, "n_holders": int}
    """
    per_asset: dict[str, dict] = {}
    users = list_all_opex_users()
    for opex in users:
        wallets = fetch_user_wallets(opex)
        for w in wallets:
            asset = (w.get("asset") or "").upper()
            if not asset:
                continue
            try:
                bal = Decimal(str(w.get("balance", 0)))
            except Exception:
                bal = Decimal(0)
            row = per_asset.setdefault(
                asset,
                {
                    "total_owed": Decimal(0),
                    "n_holders": 0,
                },
            )
            row["total_owed"] += bal
            if bal > 0:
                row["n_holders"] += 1
    return per_asset, len(users)


def build_porl_snapshot() -> dict:
    """Build the public PoRL snapshot JSON. Persists a row in porl_snapshots."""
    seed, pubkey, sig_scheme = load_or_generate_keypair()
    deployment = _load_deployment()
    custodial_addr = (deployment or {}).get("custodial", "")
    custodial_addr_lc = custodial_addr.lower() if custodial_addr else ""

    # ---- liabilities side (live wallet API) -----------------------------
    liabilities, _n_users = _aggregate_liabilities()

    # ---- reserves side (live hardhat) -----------------------------------
    # Map each on-chain token to a canonical asset symbol (ZETH -> ETH).
    reserves: dict[str, dict] = {}
    if deployment and custodial_addr_lc:
        tokens = deployment.get("tokens", {}) or {}
        decimals_map = deployment.get("decimals", {}) or {}
        for tok_sym, tok_addr in tokens.items():
            asset = TOKEN_TO_ASSET.get(tok_sym, tok_sym)
            dec = int(decimals_map.get(tok_sym, 18))
            wei = _erc20_balance_of(tok_addr, custodial_addr_lc)
            balance = _wei_to_decimal(wei, dec)
            row = reserves.setdefault(
                asset,
                {
                    "on_chain_balance": Decimal(0),
                    "source_addresses": [],
                    "tokens": [],
                },
            )
            row["on_chain_balance"] += balance
            if custodial_addr not in row["source_addresses"]:
                row["source_addresses"].append(custodial_addr)
            row["tokens"].append({"symbol": tok_sym, "address": tok_addr, "decimals": dec})

        # Add native ETH balance to the ETH reserves row.
        native_wei = _native_balance(custodial_addr_lc)
        if native_wei > 0:
            native_bal = _wei_to_decimal(native_wei, 18)
            row = reserves.setdefault(
                "ETH",
                {
                    "on_chain_balance": Decimal(0),
                    "source_addresses": [],
                    "tokens": [],
                },
            )
            row["on_chain_balance"] += native_bal
            if custodial_addr not in row["source_addresses"]:
                row["source_addresses"].append(custodial_addr)
            row["tokens"].append({"symbol": "ETH (native)", "address": None, "decimals": 18})

    # ---- merge into per-asset rows --------------------------------------
    all_assets = set(reserves) | set(liabilities)
    by_asset_rows: list[dict] = []
    total_reserves_usdt = Decimal(0)
    total_liabilities_usdt = Decimal(0)
    for asset in sorted(all_assets):
        price = get_price_usdt(asset)
        price_dec = Decimal(str(price)) if price is not None else None

        res = reserves.get(
            asset, {"on_chain_balance": Decimal(0), "source_addresses": [], "tokens": []}
        )
        liab = liabilities.get(asset, {"total_owed": Decimal(0), "n_holders": 0})

        on_chain = res["on_chain_balance"]
        owed = liab["total_owed"]

        res_usdt = (on_chain * price_dec) if price_dec is not None else Decimal(0)
        liab_usdt = (owed * price_dec) if price_dec is not None else Decimal(0)
        total_reserves_usdt += res_usdt
        total_liabilities_usdt += liab_usdt

        delta_units = on_chain - owed
        if owed > 0:
            ratio = on_chain / owed
        elif on_chain > 0:
            ratio = Decimal("999999")  # over-reserved with no liabilities
        else:
            ratio = Decimal(1)
        status = "solvent" if ratio >= 1 else "undercollateralized"

        by_asset_rows.append(
            {
                "asset": asset,
                "reserves": {
                    "on_chain_balance": _format_dec(on_chain),
                    "valuation_usdt": _format_dec(res_usdt, 2),
                    "source_addresses": res["source_addresses"],
                    "tokens": res["tokens"],
                },
                "liabilities": {
                    "total_owed": _format_dec(owed),
                    "valuation_usdt": _format_dec(liab_usdt, 2),
                    "n_holders": liab["n_holders"],
                },
                "solvency": {
                    "ratio": _format_dec(ratio, 4),
                    "delta_units": _format_dec(delta_units),
                    "delta_usdt": _format_dec(res_usdt - liab_usdt, 2),
                    "status": status,
                },
                "price_usdt": _format_dec(price_dec, 4) if price_dec is not None else None,
            }
        )

    # ---- totals ---------------------------------------------------------
    delta_total = total_reserves_usdt - total_liabilities_usdt
    if total_liabilities_usdt > 0:
        total_ratio = total_reserves_usdt / total_liabilities_usdt
    elif total_reserves_usdt > 0:
        total_ratio = Decimal("999999")
    else:
        total_ratio = Decimal(1)
    total_status = "solvent" if total_ratio >= 1 else "undercollateralized"

    totals = {
        "reserves_usdt": _format_dec(total_reserves_usdt, 2),
        "liabilities_usdt": _format_dec(total_liabilities_usdt, 2),
        "delta_usdt": _format_dec(delta_total, 2),
        "ratio": _format_dec(total_ratio, 4),
        "status": total_status,
    }

    # ---- canonical hash + signature -------------------------------------
    # `by_asset` is canonicalised: rows are sorted by 'asset' (already the case
    # by sorted(all_assets)) and JSON is dumped with sorted keys & no
    # whitespace. External verifiers reproduce this exactly.
    canon = json.dumps(by_asset_rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    asset_breakdown_hash = hashlib.sha256(canon).hexdigest()

    # Allocate snapshot id (monotone, db-backed).
    with _porl_cache_lock:
        snapshot_id = int(_porl_cache.get("next_id", 1))
        _porl_cache["next_id"] = snapshot_id + 1

    snapshot_at_ms = int(time.time() * 1000)

    # Linked epoch — most recent published liabilities root, if any.
    epoch_id: int | None = None
    try:
        with db() as conn:
            row = conn.execute("SELECT id FROM epochs ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                epoch_id = int(row["id"])
    except Exception:
        epoch_id = None

    msg = (
        f"{PORL_SCHEME_NAME}|{snapshot_id}|{totals['reserves_usdt']}"
        f"|{totals['liabilities_usdt']}|{asset_breakdown_hash}"
    ).encode()
    sig = sign_msg(seed, msg, sig_scheme).hex()

    bundle = {
        "scheme": PORL_SCHEME_NAME,
        "snapshot_id": snapshot_id,
        "snapshot_at": snapshot_at_ms,
        "epoch_id": epoch_id,
        "by_asset": by_asset_rows,
        "totals": totals,
        "signature": sig,
        "server_pubkey": pubkey.hex(),
        "sig_scheme": sig_scheme,
        "hash": HASH_NAME,
        "asset_breakdown_hash": asset_breakdown_hash,
        "verification_recipe": [
            "1. Fetch /pol/server-info to get pubkey + sig_scheme.",
            "2. Recompute asset_breakdown_hash: SHA-256 of canonical JSON of by_asset (sorted keys, no whitespace, asset rows in alphabetical order by 'asset').",
            "3. Build the signed message: 'zkcex-porl-v1|' + snapshot_id + '|' + totals.reserves_usdt + '|' + totals.liabilities_usdt + '|' + asset_breakdown_hash",
            "4. Verify signature against server_pubkey using sig_scheme.",
            "5. Independently confirm reserves: each source_address can be read on chain via eth_call balanceOf — the reserves figure must match (or be greater, if reserves grew between snapshots).",
            "6. Independently confirm liabilities: the sum of all per-user balances on /v1/owner/<u>/wallets must equal liabilities.total_owed for each asset.",
            "7. If ratio >= 1.0 the exchange is fully reserved at the snapshot moment.",
        ],
    }

    # ---- persist a history row -----------------------------------------
    try:
        with _db_lock, db() as conn:
            conn.execute(
                "INSERT INTO porl_snapshots (id, snapshot_at, epoch_id, reserves_usdt,"
                " liabilities_usdt, delta_usdt, ratio, status, totals_json, signature)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    snapshot_id,
                    snapshot_at_ms,
                    epoch_id,
                    totals["reserves_usdt"],
                    totals["liabilities_usdt"],
                    totals["delta_usdt"],
                    totals["ratio"],
                    totals["status"],
                    json.dumps(totals, separators=(",", ":")),
                    sig,
                ),
            )
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[pol] porl persist failed: {e}\n")

    return bundle


def get_porl_snapshot(force: bool = False) -> dict:
    """Public entry: returns a cached PoRL snapshot or rebuilds if stale."""
    now = time.time()
    with _porl_cache_lock:
        cached = _porl_cache.get("snapshot")
        fetched_at = float(_porl_cache.get("fetched_at") or 0.0)
        fresh = cached is not None and (now - fetched_at) < PORL_CACHE_TTL_S
    if cached and fresh and not force:
        return cached  # type: ignore[return-value]
    bundle = build_porl_snapshot()
    with _porl_cache_lock:
        _porl_cache["snapshot"] = bundle
        _porl_cache["fetched_at"] = time.time()
    return bundle


# ==========================================================================
# Background scheduler
# ==========================================================================
def epoch_scheduler():
    while True:
        try:
            res = build_snapshot()
            sys.stderr.write(
                f"[pol] epoch #{res['epoch_id']} built: "
                f"n_users={res['n_users']} root_sum={res['root_sum']} took={res['took_ms']}ms\n"
            )
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[pol] scheduler error: {e!r}\n")
        time.sleep(EPOCH_SECONDS)


# ==========================================================================
# HTTP layer
# ==========================================================================
VERIFY_INSTRUCTIONS_MD = """\
# zkCEX Proof-of-Liabilities — external verification

The JSON returned by `GET /pol/my-proof` is **self-contained**: anyone with
SHA-256 and Ed25519 (or HMAC-SHA256) can verify it offline without contacting
this server.

## Recipe

1. Fetch `server_pubkey` once from `GET /pol/server-info` (or read it from the
   proof JSON; both are public).
2. Build the canonical signed message:
   ```
   msg = utf8("zkcex-pol-v1|" + epoch.id + "|" + epoch.root_hash + "|" + epoch.root_sum)
   ```
   Verify `epoch.signature` over `msg` with `server_pubkey` using `sig_scheme`.
3. Recompute the leaf hash:
   ```
   balance_scaled = round(leaf.balance * 1e8)         # u64, 8-decimal fixed-point
   leaf_hash = SHA256(hex_to_bytes(leaf.user_hash) || u64_be(balance_scaled))
   ```
4. Fold up sibling_path. Maintain `(curr_hash, curr_sum_scaled)`, starting at
   `(leaf_hash, balance_scaled)`. At each level:
   - if `side == "right"`: `curr_hash = SHA256(curr_hash || sib.hash || u64_be(curr_sum) || u64_be(sib.sum_scaled))`
   - if `side == "left"`:  `curr_hash = SHA256(sib.hash || curr_hash || u64_be(sib.sum_scaled) || u64_be(curr_sum))`
   - `curr_sum_scaled += sib.sum_scaled`
5. After folding the entire path, `curr_hash` must equal `epoch.root_hash` and
   `curr_sum_scaled / 1e8` (formatted) must equal `epoch.root_sum`.

## What the proof does and doesn't cover

- It proves your balance is included in the published total liability for
  this epoch. An auditor who sees the same `epoch.signature` on a different
  proof for some other user can be sure both leaves sum into the same root.
- It does *not* prove the exchange holds matching reserves on-chain. That's a
  separate proof-of-reserves question.

## Demo caveats

- Hash is SHA-256 (stdlib in every language), not Keccak. Documented in
  `proof.hash`.
- The signing key on this demo server is generated on first run and stored in
  `tools/.local/pol_signing_key`. In production the key would live in an HSM.
"""


PYTHON_VERIFIER_SNIPPET = """\
# Verify a zkCEX PoL proof JSON. Stdlib only.
import hashlib, json, sys
from pathlib import Path

# Pure-Python Ed25519 verify (RFC 8032). Adapted from the public-domain
# reference at https://ed25519.cr.yp.to/python/ed25519.py.
_q = 2**255 - 19
_l = 2**252 + 27742317777372353535851937790883648493
_d = (-121665) * pow(121666, _q-2, _q) % _q
_I = pow(2, (_q-1)//4, _q)
def _xrec(y):
    xx = (y*y-1) * pow(_d*y*y+1, _q-2, _q)
    x = pow(xx, (_q+3)//8, _q)
    if (x*x - xx) % _q != 0: x = (x*_I) % _q
    if x % 2: x = _q - x
    return x
_By = 4 * pow(5, _q-2, _q) % _q
_Bx = _xrec(_By)
_B = (_Bx % _q, _By % _q)
def _edw(P,Q):
    x1,y1=P; x2,y2=Q
    inv = pow(1+_d*x1*x2*y1*y2, _q-2, _q)
    inv2= pow(1-_d*x1*x2*y1*y2, _q-2, _q)
    return (((x1*y2+x2*y1)*inv)%_q, ((y1*y2+x1*x2)*inv2)%_q)
def _smul(P,e):
    if e==0: return (0,1)
    Q=_smul(P, e//2); Q=_edw(Q,Q)
    return _edw(Q,P) if e&1 else Q
def _bit(h,i): return (h[i//8]>>(i%8))&1
def _enc(P):
    x,y=P
    bits=[(y>>i)&1 for i in range(255)]+[x&1]
    return bytes(sum(bits[i*8+j]<<j for j in range(8)) for i in range(32))
def _decint(s): return sum(2**i*_bit(s,i) for i in range(256))
def _decpt(s):
    y=sum(2**i*_bit(s,i) for i in range(255))
    x=_xrec(y)
    if x&1 != _bit(s,255): x=_q-x
    return (x,y)
def _Hint(m):
    h=hashlib.sha512(m).digest()
    return sum(2**i*_bit(h,i) for i in range(512))
def ed25519_verify(pk, msg, sig):
    if len(sig)!=64 or len(pk)!=32: return False
    try:
        R=_decpt(sig[:32]); A=_decpt(pk); S=_decint(sig[32:])
        h=_Hint(_enc(R)+pk+msg)
        return _smul(_B,S) == _edw(R, _smul(A,h))
    except Exception:
        return False

def u64be(n): return int(n).to_bytes(8, "big")
def sha256(b): return hashlib.sha256(b).digest()
def scaled(s): return int(round(float(s) * 10**8))

def _verify_sig(p, msg, sig_hex):
    sig = bytes.fromhex(sig_hex)
    pubkey = bytes.fromhex(p["server_pubkey"])
    if p["sig_scheme"] == "Ed25519":
        return ed25519_verify(pubkey, msg, sig)
    if p["sig_scheme"] == "HMAC-SHA256":
        import hmac
        want = hmac.new(pubkey, msg, hashlib.sha256).digest()
        return hmac.compare_digest(want, sig)
    return False

def _fold(leaf, sibling_path):
    bal = scaled(leaf["balance"])
    cur_h = sha256(bytes.fromhex(leaf["user_hash"]) + u64be(bal))
    cur_s = bal
    for step in sibling_path:
        sib_h = bytes.fromhex(step["hash"])
        sib_s = scaled(step["sum"])
        if step["side"] == "right":
            cur_h = sha256(cur_h + sib_h + u64be(cur_s) + u64be(sib_s))
        elif step["side"] == "left":
            cur_h = sha256(sib_h + cur_h + u64be(sib_s) + u64be(cur_s))
        else:
            return None, None, f"bad side {step['side']}"
        cur_s += sib_s
    return cur_h, cur_s, None

def main(path):
    p = json.loads(Path(path).read_text())
    if p.get("hash") != "SHA-256":
        return f"FAIL: wrong hash {p.get('hash')}"
    scheme = p.get("scheme")

    # ---- snapshot proof (zkcex-pol-v1) ----
    if scheme == "zkcex-pol-v1":
        e = p["epoch"]; leaf = p["leaf"]
        msg = f"zkcex-pol-v1|{e['id']}|{e['root_hash']}|{e['root_sum']}".encode()
        if not _verify_sig(p, msg, e["signature"]):
            return f"FAIL: signature invalid (sig_scheme={p['sig_scheme']})"
        cur_h, cur_s, err = _fold(leaf, p["sibling_path"])
        if err: return f"FAIL: {err}"
        if cur_h.hex() != e["root_hash"]:
            return f"FAIL: root hash mismatch {cur_h.hex()} vs {e['root_hash']}"
        if scaled(e["root_sum"]) != cur_s:
            return f"FAIL: root sum mismatch {cur_s} vs {scaled(e['root_sum'])}"
        return "OK"

    # ---- live proof (zkcex-pol-live-v1) ----
    if scheme == "zkcex-pol-live-v1":
        c = p["live_commit"]; leaf = p["leaf"]
        msg = (f"zkcex-pol-live-v1|{c['commit_id']}|{c['epoch_id']}"
               f"|{c['root_hash']}|{c['root_sum']}").encode()
        if not _verify_sig(p, msg, c["signature"]):
            return f"FAIL: signature invalid (sig_scheme={p['sig_scheme']})"
        cur_h, cur_s, err = _fold(leaf, p["sibling_path"])
        if err: return f"FAIL: {err}"
        if cur_h.hex() != c["root_hash"]:
            return f"FAIL: root hash mismatch {cur_h.hex()} vs {c['root_hash']}"
        if scaled(c["root_sum"]) != cur_s:
            return f"FAIL: root sum mismatch {cur_s} vs {scaled(c['root_sum'])}"
        return "OK"

    return f"FAIL: unknown scheme {scheme}"

if __name__ == "__main__":
    print(main(sys.argv[1] if len(sys.argv) > 1 else "proof.json"))
"""


# Node.js verifier — reads proof.json from argv[2] and prints OK / FAIL.
JS_VERIFIER_SNIPPET = """\
// Verify a zkCEX PoL proof JSON. Node.js stdlib only (crypto + fs).
const fs = require("fs");
const crypto = require("crypto");

function u64be(n){ const b=Buffer.alloc(8); b.writeBigUInt64BE(BigInt(n)); return b; }
function sha256(b){ return crypto.createHash("sha256").update(b).digest(); }
function scaled(s){ return Math.round(Number(s) * 1e8); }

function verifySig(p, msg, sigHex){
  const sig = Buffer.from(sigHex, "hex");
  const pubHex = p.server_pubkey;
  if (p.sig_scheme === "Ed25519") {
    const der = Buffer.concat([Buffer.from("302a300506032b6570032100", "hex"), Buffer.from(pubHex, "hex")]);
    const pk = crypto.createPublicKey({ key: der, format: "der", type: "spki" });
    return crypto.verify(null, msg, pk, sig);
  } else if (p.sig_scheme === "HMAC-SHA256") {
    const want = crypto.createHmac("sha256", Buffer.from(pubHex, "hex")).update(msg).digest();
    return sig.length === want.length && crypto.timingSafeEqual(want, sig);
  }
  return false;
}

function fold(leaf, siblingPath){
  const bal = scaled(leaf.balance);
  let cur_h = sha256(Buffer.concat([Buffer.from(leaf.user_hash, "hex"), u64be(bal)]));
  let cur_s = BigInt(bal);
  for (const step of siblingPath) {
    const sh = Buffer.from(step.hash, "hex");
    const ss = BigInt(scaled(step.sum));
    if (step.side === "right") cur_h = sha256(Buffer.concat([cur_h, sh, u64be(cur_s), u64be(ss)]));
    else if (step.side === "left") cur_h = sha256(Buffer.concat([sh, cur_h, u64be(ss), u64be(cur_s)]));
    else return { err: "bad side " + step.side };
    cur_s += ss;
  }
  return { cur_h, cur_s };
}

function verify(p){
  if (p.hash !== "SHA-256") return "FAIL: wrong hash " + p.hash;
  if (p.scheme === "zkcex-pol-v1") {
    const e = p.epoch, leaf = p.leaf;
    const msg = Buffer.from(`zkcex-pol-v1|${e.id}|${e.root_hash}|${e.root_sum}`, "utf8");
    if (!verifySig(p, msg, e.signature)) return "FAIL: signature invalid";
    const r = fold(leaf, p.sibling_path);
    if (r.err) return "FAIL: " + r.err;
    if (r.cur_h.toString("hex") !== e.root_hash) return `FAIL: root hash mismatch ${r.cur_h.toString("hex")} vs ${e.root_hash}`;
    if (BigInt(scaled(e.root_sum)) !== r.cur_s)  return `FAIL: root sum mismatch ${r.cur_s} vs ${scaled(e.root_sum)}`;
    return "OK";
  }
  if (p.scheme === "zkcex-pol-live-v1") {
    const c = p.live_commit, leaf = p.leaf;
    const msg = Buffer.from(`zkcex-pol-live-v1|${c.commit_id}|${c.epoch_id}|${c.root_hash}|${c.root_sum}`, "utf8");
    if (!verifySig(p, msg, c.signature)) return "FAIL: signature invalid";
    const r = fold(leaf, p.sibling_path);
    if (r.err) return "FAIL: " + r.err;
    if (r.cur_h.toString("hex") !== c.root_hash) return `FAIL: root hash mismatch ${r.cur_h.toString("hex")} vs ${c.root_hash}`;
    if (BigInt(scaled(c.root_sum)) !== r.cur_s)  return `FAIL: root sum mismatch ${r.cur_s} vs ${scaled(c.root_sum)}`;
    return "OK";
  }
  return "FAIL: unknown scheme " + p.scheme;
}

const path = process.argv[2] || "proof.json";
const proof = JSON.parse(fs.readFileSync(path, "utf8"));
console.log(verify(proof));
"""


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "zkcex-pol/1.0"

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

    def _send_text(self, status: int, txt: str, ctype: str = "text/markdown; charset=utf-8"):
        body = txt.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        if not auth.lower().startswith("bearer "):
            return None
        return auth.split(None, 1)[1].strip()

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[pol] {self.address_string()} - {fmt % args}\n")

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
        with _otel_server_span(self):
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            if path == "/pol/server-info":
                return self.h_server_info()
            if path == "/pol/latest-epoch":
                return self.h_latest_epoch()
            if path == "/pol/my-proof":
                return self.h_my_proof()
            if path == "/pol/verify-instructions":
                return self.h_verify_instructions()
            if path == "/pol/verifier/python":
                return self._send_text(200, PYTHON_VERIFIER_SNIPPET, "text/x-python; charset=utf-8")
            if path == "/pol/verifier/javascript":
                return self._send_text(200, JS_VERIFIER_SNIPPET, "text/javascript; charset=utf-8")
            if path == "/pol/reserves-vs-liabilities":
                return self.h_reserves_vs_liabilities()
            if path == "/pol/reserves-history":
                return self.h_reserves_history(parsed.query)
            if path == "/pol/live/info":
                return self.h_live_info()
            if path == "/pol/live/stream":
                return self.h_live_stream()
            if path == "/pol/live/my-proof":
                return self.h_live_my_proof()
            if path == "/pol/live/recent":
                return self.h_live_recent(parsed.query)
            return self._send_json(404, {"error": "not_found", "path": path})

    def do_POST(self):  # noqa: N802
        with _otel_server_span(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == "/pol/refresh":
                return self.h_refresh()
            if path == "/pol/reserves-vs-liabilities/refresh":
                return self.h_reserves_vs_liabilities(force=True)
            return self._send_json(404, {"error": "not_found", "path": path})

    # ---- handlers --------------------------------------------------------
    def h_server_info(self):
        seed, pub, scheme = load_or_generate_keypair()
        return self._send_json(
            200,
            {
                "scheme_name": SCHEME_NAME,
                "hash": HASH_NAME,
                "sig_scheme": scheme,
                "pubkey": pub.hex(),
                "epoch_seconds": EPOCH_SECONDS,
            },
        )

    def h_latest_epoch(self):
        with db() as conn:
            row = conn.execute(
                "SELECT id, epoch_started_at, root_hash, root_sum, signature, n_users, sig_scheme, server_pubkey "
                "FROM epochs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if not row:
            return self._send_json(404, {"error": "no_snapshot_yet"})
        return self._send_json(
            200,
            {
                "epoch_id": row["id"],
                "epoch_started_at": row["epoch_started_at"],
                "root_hash": row["root_hash"],
                "root_sum": row["root_sum"],
                "signature": row["signature"],
                "sig_scheme": row["sig_scheme"],
                "server_pubkey": row["server_pubkey"],
                "n_users": row["n_users"],
            },
        )

    def h_my_proof(self):
        token = self._bearer()
        u = session_user_via_auth(token or "")
        if not u:
            return self._send_json(401, {"error": "unauthorized"})
        opex = u["opex_user"]

        # Find latest proof row for this user; if none, build a snapshot now.
        proof_row, epoch_row = self._load_proof(opex)
        if not proof_row:
            try:
                build_snapshot()
            except Exception as e:  # noqa: BLE001
                return self._send_json(503, {"error": "snapshot_failed", "message": str(e)})
            proof_row, epoch_row = self._load_proof(opex)
        if not proof_row or not epoch_row:
            return self._send_json(
                503,
                {
                    "error": "no_snapshot_yet",
                    "message": "snapshot rebuild did not include this user",
                },
            )

        sibling_path_full = json.loads(proof_row["sibling_path_json"])
        # Strip the internal sum_scaled field — keep the canonical decimal.
        sibling_path = [
            {"side": s["side"], "hash": s["hash"], "sum": s["sum"]} for s in sibling_path_full
        ]

        proof = {
            "scheme": SCHEME_NAME,
            "hash": HASH_NAME,
            "sig_scheme": epoch_row["sig_scheme"],
            "server_pubkey": epoch_row["server_pubkey"],
            "epoch": {
                "id": epoch_row["id"],
                "started_at": epoch_row["epoch_started_at"],
                "root_hash": epoch_row["root_hash"],
                "root_sum": epoch_row["root_sum"],
                "n_users": epoch_row["n_users"],
                "signature": epoch_row["signature"],
            },
            "leaf": {
                "user_hash": proof_row["user_hash"],
                "balance": proof_row["balance"],
                "asset_breakdown": json.loads(proof_row["asset_breakdown_json"]),
                "epoch_nonce": proof_row["epoch_nonce"],
            },
            "sibling_path": sibling_path,
            "verification_recipe": [
                "1. Build msg = utf8('zkcex-pol-v1|' + epoch.id + '|' + epoch.root_hash + '|' + epoch.root_sum). Verify epoch.signature over msg with server_pubkey using sig_scheme.",
                "2. Recompute leaf hash: H = SHA-256(hex_to_bytes(leaf.user_hash) || u64_be(round(leaf.balance * 1e8))). Take that as the starting curr_hash; starting curr_sum_scaled = round(leaf.balance * 1e8).",
                "3. Fold sibling_path top-down: at each step, if side='right' set curr_hash = SHA-256(curr_hash || sib.hash || u64_be(curr_sum) || u64_be(round(sib.sum*1e8))); if side='left' swap the two sides. Then curr_sum_scaled += round(sib.sum*1e8).",
                "4. Final curr_hash must equal epoch.root_hash and curr_sum_scaled / 1e8 (formatted) must equal epoch.root_sum. If both match, the proof is valid.",
            ],
            "issued_at": int(time.time()),
        }
        return self._send_json(200, proof)

    def _load_proof(self, opex: str) -> tuple:
        with db() as conn:
            proof_row = conn.execute(
                "SELECT * FROM proofs WHERE opex_user=? ORDER BY epoch_id DESC LIMIT 1",
                (opex,),
            ).fetchone()
            if not proof_row:
                return (None, None)
            epoch_row = conn.execute(
                "SELECT * FROM epochs WHERE id=?",
                (proof_row["epoch_id"],),
            ).fetchone()
            return (proof_row, epoch_row)

    def h_refresh(self):
        token = self._bearer()
        u = session_user_via_auth(token or "")
        if not u:
            return self._send_json(401, {"error": "unauthorized"})
        try:
            res = build_snapshot()
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"error": "snapshot_failed", "message": str(e)})
        return self._send_json(200, res)

    def h_verify_instructions(self):
        return self._send_text(200, VERIFY_INSTRUCTIONS_MD, "text/markdown; charset=utf-8")

    def h_reserves_vs_liabilities(self, force: bool = False):
        """Public, unauthenticated. Returns the signed reserves-vs-liabilities
        snapshot, rebuilding if the cache is older than PORL_CACHE_TTL_S (or if
        force=True via the POST /refresh endpoint)."""
        try:
            bundle = get_porl_snapshot(force=force)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[pol] porl build failed: {e!r}\n")
            return self._send_json(503, {"error": "porl_failed", "message": str(e)})
        return self._send_json(200, bundle)

    def h_reserves_history(self, query: str):
        """Public, unauthenticated. Returns the last N porl_snapshots rows for
        charting solvency over time. Default 50, max 500."""
        params = urllib.parse.parse_qs(query or "")
        try:
            limit = int((params.get("limit") or ["50"])[0])
        except Exception:
            limit = 50
        if limit < 1:
            limit = 1
        if limit > 500:
            limit = 500
        rows: list[dict] = []
        try:
            with db() as conn:
                cur = conn.execute(
                    "SELECT id, snapshot_at, epoch_id, reserves_usdt, liabilities_usdt,"
                    " delta_usdt, ratio, status, signature"
                    " FROM porl_snapshots ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
                for r in cur.fetchall():
                    rows.append(
                        {
                            "snapshot_id": r["id"],
                            "snapshot_at": r["snapshot_at"],
                            "epoch_id": r["epoch_id"],
                            "reserves_usdt": r["reserves_usdt"],
                            "liabilities_usdt": r["liabilities_usdt"],
                            "delta_usdt": r["delta_usdt"],
                            "ratio": r["ratio"],
                            "status": r["status"],
                            "signature": r["signature"],
                        }
                    )
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"error": "history_failed", "message": str(e)})
        # Return oldest-first so a chart can plot left-to-right without reversing.
        rows.reverse()
        return self._send_json(200, {"limit": limit, "count": len(rows), "snapshots": rows})

    # ---- live mode -------------------------------------------------------
    def h_live_info(self):
        """Public discovery endpoint for live PoL: tick rate, current commit,
        signing parameters. Useful for the verify page to know what to expect."""
        seed, pub, scheme = load_or_generate_keypair()
        with _live_lock:
            current = _live_state.get("current_commit")
        return self._send_json(
            200,
            {
                "scheme_name": LIVE_SCHEME_NAME,
                "live_tick_seconds": LIVE_TICK_S,
                "max_commit_lag_seconds": LIVE_TICK_BUDGET_MS / 1000.0 + LIVE_TICK_S,
                "current_commit": current,
                "server_pubkey": pub.hex(),
                "sig_scheme": scheme,
                "hash": HASH_NAME,
                "ring_buffer_size": LIVE_RING_MAX,
                "max_subscribers": LIVE_MAX_SUBSCRIBERS,
            },
        )

    def h_live_stream(self):
        """SSE: stream live commits + epoch_rotated + 15s heartbeats.

        Uses a per-subscriber Condition variable: the live tick / snapshot
        rebuild calls _broadcast_event() which appends to each subscriber's
        queue and notifies. We block in a loop and emit chunked text.
        """
        # Back-pressure: cap concurrent subscribers.
        with _live_subscribers_lock:
            if len(_live_subscribers) >= LIVE_MAX_SUBSCRIBERS:
                return self._send_json(
                    503,
                    {
                        "error": "too_many_subscribers",
                        "max": LIVE_MAX_SUBSCRIBERS,
                    },
                )

        sub = {
            "cv": threading.Condition(),
            "queue": [],
            "overflow": False,
            "closed": False,
        }
        with _live_subscribers_lock:
            _live_subscribers.append(sub)

        # SSE response headers. We write directly to wfile to bypass the
        # framework's Content-Length default. Chunked-style: just keep flushing.
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")  # nginx hint, harmless elsewhere
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
        except Exception:
            self._remove_subscriber(sub)
            return

        def write_event(name: str, payload) -> bool:
            try:
                line = f"event: {name}\ndata: {json.dumps(payload)}\n\n"
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
                return True
            except Exception:
                return False

        # On connect, send the current commit (if any) so newcomers can render
        # immediately without waiting for the next tick.
        with _live_lock:
            current = _live_state.get("current_commit")
        if current is not None:
            if not write_event("commit", current):
                self._remove_subscriber(sub)
                return

        last_hb = time.time()
        try:
            while True:
                # Wait for either a queued event, an overflow flag, or 15s for HB.
                with sub["cv"]:
                    if not sub["queue"] and not sub["overflow"] and not sub["closed"]:
                        sub["cv"].wait(timeout=15.0)
                    pending = list(sub["queue"])
                    sub["queue"].clear()
                    overflow = sub["overflow"]
                    sub["overflow"] = False
                    closed = sub["closed"]
                if closed:
                    break
                ok = True
                for name, payload in pending:
                    if not write_event(name, payload):
                        ok = False
                        break
                if not ok:
                    break
                if overflow:
                    write_event("warn", {"message": "queue_overflow_some_events_dropped"})
                # Heartbeat every ~15s so proxies don't drop the conn.
                now = time.time()
                if now - last_hb >= 15.0:
                    if not write_event("ping", {"t": int(now * 1000)}):
                        break
                    last_hb = now
        finally:
            self._remove_subscriber(sub)

    def _remove_subscriber(self, sub) -> None:
        with _live_subscribers_lock:
            try:
                _live_subscribers.remove(sub)
            except ValueError:
                pass

    def h_live_my_proof(self):
        """Returns the user's inclusion proof against the current live root."""
        token = self._bearer()
        u = session_user_via_auth(token or "")
        if not u:
            return self._send_json(401, {"error": "unauthorized"})
        opex = u["opex_user"]

        # Rebuild the proof from the live tree under the lock.
        with _live_lock:
            users_order: list[str] = list(_live_state.get("users_order") or [])
            user_hash_hex_map: dict[str, str] = dict(_live_state.get("user_hash_hex") or {})
            cache: dict[str, dict] = dict(_live_state.get("user_cache") or {})
            epoch_nonce_by_user: dict[str, str] = dict(_live_state.get("epoch_nonce_by_user") or {})
            levels = [list(lvl) for lvl in (_live_state.get("tree_levels") or [])]
            current_commit = _live_state.get("current_commit")
            epoch_id = _live_state.get("epoch_id")
            _live_state.get("epoch_started_at")

        if opex not in user_hash_hex_map:
            # User is signed in but the live tree doesn't include them yet
            # (signed up after the latest snapshot). Trigger a snapshot rebuild
            # so they're picked up next tick — then ask them to retry.
            return self._send_json(
                503,
                {
                    "error": "not_in_live_tree",
                    "message": "user not yet in live tree; trigger /pol/refresh and retry",
                },
            )

        if not current_commit or not levels:
            return self._send_json(503, {"error": "no_live_commit_yet"})

        idx = users_order.index(opex)
        sibling_full = _sibling_path_for(levels, idx)
        sibling_path = [
            {"side": s["side"], "hash": s["hash"], "sum": s["sum"]} for s in sibling_full
        ]

        cached = cache.get(opex) or {}
        balance_scaled = int(cached.get("scaled") or 0)
        breakdown = cached.get("breakdown") or []
        epoch_nonce = epoch_nonce_by_user.get(opex, "")

        # Also fetch the snapshot epoch row so the verifier can cross-check
        # the snapshot signature too if it wants. The live commit has its own
        # signature under LIVE_SCHEME_NAME — independent and sufficient.
        epoch_payload: dict | None = None
        try:
            with db() as conn:
                row = conn.execute(
                    "SELECT id, epoch_started_at, root_hash, root_sum, signature, n_users, sig_scheme, server_pubkey "
                    "FROM epochs WHERE id=?",
                    (epoch_id,),
                ).fetchone()
                if row:
                    epoch_payload = {
                        "id": row["id"],
                        "started_at": row["epoch_started_at"],
                        "root_hash": row["root_hash"],
                        "root_sum": row["root_sum"],
                        "signature": row["signature"],
                        "n_users": row["n_users"],
                    }
        except Exception:
            epoch_payload = None

        seed, pub, scheme = load_or_generate_keypair()
        proof = {
            "scheme": LIVE_SCHEME_NAME,
            "hash": HASH_NAME,
            "sig_scheme": scheme,
            "server_pubkey": pub.hex(),
            "live_commit": current_commit,
            "epoch": epoch_payload,
            "leaf": {
                "user_hash": user_hash_hex_map[opex],
                "balance": _scaled_to_str(balance_scaled),
                "asset_breakdown": breakdown,
                "epoch_nonce": epoch_nonce,
            },
            "sibling_path": sibling_path,
            "issued_at": int(time.time() * 1000),
            "verification_recipe": [
                "1. Build msg = utf8('zkcex-pol-live-v1|' + live_commit.commit_id + '|' + live_commit.epoch_id + '|' + live_commit.root_hash + '|' + live_commit.root_sum). Verify live_commit.signature over msg with server_pubkey using sig_scheme.",
                "2. Recompute user_hash if you wish: user_hash = SHA-256(opex_user || hex_to_bytes(leaf.epoch_nonce)). Compare with leaf.user_hash.",
                "3. Recompute leaf hash: H = SHA-256(hex_to_bytes(leaf.user_hash) || u64_be(round(leaf.balance * 1e8))). Take that as the starting curr_hash; starting curr_sum_scaled = round(leaf.balance * 1e8).",
                "4. Fold sibling_path top-down: at each step, if side='right' set curr_hash = SHA-256(curr_hash || sib.hash || u64_be(curr_sum) || u64_be(round(sib.sum*1e8))); if side='left' swap the two sides. Then curr_sum_scaled += round(sib.sum*1e8).",
                "5. Final curr_hash must equal live_commit.root_hash and curr_sum_scaled / 1e8 (formatted) must equal live_commit.root_sum. If both match, the proof is valid against the live root for the current commit.",
            ],
        }
        return self._send_json(200, proof)

    def h_live_recent(self, query: str):
        """Returns the most recent live commits (default 50, max 500)."""
        params = urllib.parse.parse_qs(query or "")
        try:
            limit = int((params.get("limit") or ["50"])[0])
        except Exception:
            limit = 50
        if limit < 1:
            limit = 1
        if limit > 500:
            limit = 500
        # Prefer in-memory ring (fastest); fall back to DB for restarts/cold-cache.
        with _live_lock:
            in_mem = list(_live_commits_ring[-limit:])
        if len(in_mem) >= limit:
            return self._send_json(200, {"limit": limit, "count": len(in_mem), "commits": in_mem})
        commits: list[dict] = []
        try:
            with db() as conn:
                cur = conn.execute(
                    "SELECT commit_id, epoch_id, committed_at, root_hash, root_sum,"
                    " n_users, signature FROM live_commits ORDER BY commit_id DESC LIMIT ?",
                    (limit,),
                )
                for r in cur.fetchall():
                    commits.append(
                        {
                            "scheme": LIVE_SCHEME_NAME,
                            "commit_id": r["commit_id"],
                            "epoch_id": r["epoch_id"],
                            "committed_at": r["committed_at"],
                            "root_hash": r["root_hash"],
                            "root_sum": r["root_sum"],
                            "n_users": r["n_users"],
                            "signature": r["signature"],
                        }
                    )
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"error": "history_failed", "message": str(e)})
        commits.reverse()
        return self._send_json(200, {"limit": limit, "count": len(commits), "commits": commits})


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5503
    try:
        _otel_install()
    except Exception as e:  # noqa: BLE001
        log(f"otel install skipped: {e!r}")
    init_db()
    seed, pub, scheme = load_or_generate_keypair()
    sys.stderr.write(f"[pol] db={DB_PATH}\n")
    sys.stderr.write(f"[pol] auth_db={AUTH_DB_PATH}\n")
    sys.stderr.write(f"[pol] sig_scheme={scheme} pubkey={pub.hex()}\n")
    sys.stderr.write(f"[pol] epoch_seconds={EPOCH_SECONDS}\n")
    # Build an initial snapshot at boot (best-effort).
    try:
        res = build_snapshot()
        sys.stderr.write(
            f"[pol] initial snapshot epoch=#{res['epoch_id']} n_users={res['n_users']}\n"
        )
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[pol] initial snapshot failed: {e!r}\n")
    threading.Thread(target=epoch_scheduler, daemon=True).start()
    threading.Thread(target=live_tick_loop, daemon=True).start()
    sys.stderr.write(
        f"[pol] live tick {LIVE_TICK_S}s, ring={LIVE_RING_MAX},"
        f" max_subscribers={LIVE_MAX_SUBSCRIBERS}\n"
    )
    sys.stderr.write(f"[pol] listening on :{port}\n")
    with ThreadingServer(("", port), Handler) as srv:
        srv.serve_forever()


if __name__ == "__main__":
    main()
