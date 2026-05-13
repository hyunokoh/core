#!/usr/bin/env python3
"""On-chain anchor indexer for the zkPoL BulletinBoard contract.

Polls a local EVM RPC for new ``BatchAppended`` + ``CommitmentPosted`` events
from the BulletinBoard, persists them into a tiny SQLite database, and serves
an "explorer-style" HTTP API the zkCEX UI uses to render the on-chain trace
for each user.

Pure stdlib — no pip dependencies. eth_getLogs is hit over plain JSON-RPC and
the Keccak-256 used for event-topic derivation is implemented inline (see
``_keccak256`` below).

Configuration via env:

  * ``BULLETIN_BOARD_RPC``      EVM JSON-RPC, default ``http://127.0.0.1:8545``
  * ``BULLETIN_BOARD_ADDRESS``  0x-hex contract address (no default).
                                When empty the service starts in "degraded"
                                mode: ``/anchor/health`` is still 200 but
                                every other endpoint returns 503.
  * ``BULLETIN_BOARD_START_BLOCK``  default 0
  * ``ANCHOR_POLL_INTERVAL``    seconds, default 3.0
  * ``ANCHOR_CONFIRMATIONS``    reorg buffer (the indexer re-scans the last
                                N blocks each tick), default 12

Endpoints (when configured):

  GET  /anchor/health                — last block scanned, head lag, counts
  GET  /anchor/latest                — current latestBatchHash + chain tip
  GET  /anchor/batches?limit=50      — recent batches (newest first)
  GET  /anchor/batch/{hash}          — one batch + every commitment in it
  GET  /anchor/account/{addr_key}    — bearer-gated, per-account history
  POST /anchor/internal/log-pending  — loopback only; zkpol_bridge calls this
  POST /anchor/internal/reindex      — loopback only; drop cursor & rescan
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
from typing import Any


def _validated_http_url(raw_url: str, *, name: str = "url") -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return raw_url


def _http_request(url: str, **kwargs) -> urllib.request.Request:
    return urllib.request.Request(_validated_http_url(url), **kwargs)  # noqa: S310


def _http_urlopen(target, **kwargs):
    if isinstance(target, str):
        target = _validated_http_url(target)
    return urllib.request.urlopen(target, **kwargs)  # noqa: S310


# --------------------------------------------------------------------------
# Paths & constants
# --------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join(HERE, ".local")
os.makedirs(LOCAL_DIR, exist_ok=True)

DB_PATH = os.environ.get("ANCHOR_DB_PATH", os.path.join(LOCAL_DIR, "anchor_index.db"))
AUTH_DB_PATH = os.path.join(LOCAL_DIR, "auth.db")

RPC_URL = _validated_http_url(
    os.environ.get("BULLETIN_BOARD_RPC", "http://127.0.0.1:8545"),
    name="BULLETIN_BOARD_RPC",
)
CONTRACT_ADDRESS = (os.environ.get("BULLETIN_BOARD_ADDRESS", "") or "").strip().lower()
START_BLOCK = int(os.environ.get("BULLETIN_BOARD_START_BLOCK", "0"))
POLL_INTERVAL = float(os.environ.get("ANCHOR_POLL_INTERVAL", "3.0"))
CONFIRMATIONS = int(os.environ.get("ANCHOR_CONFIRMATIONS", "12"))
GETLOGS_RANGE = int(os.environ.get("ANCHOR_GETLOGS_RANGE", "2000"))


# --------------------------------------------------------------------------
# Keccak-256 (stdlib-only, self-contained).
#
# We need Keccak-256 (not SHA3-256, which differs in padding) to derive the
# topic[0] = keccak256(event_signature) values for eth_getLogs filtering and
# to print BatchAppended log hashes for the UI. Python's hashlib ships SHA3
# but not raw Keccak, so we vendor the standard sponge construction with
# Keccak-f[1600]. The implementation below is approximately 80 lines; the
# round-constants and rotation offsets come straight from the FIPS-202 spec
# (https://nvlpubs.nist.gov/nistpubs/FIPS/NIST.FIPS.202.pdf).
#
# Test vectors:
#   keccak256(b"")     = c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470
#   keccak256(b"abc")  = 4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45
# --------------------------------------------------------------------------

_KECCAK_ROUNDS = 24
_KECCAK_RC = (
    0x0000000000000001,
    0x0000000000008082,
    0x800000000000808A,
    0x8000000080008000,
    0x000000000000808B,
    0x0000000080000001,
    0x8000000080008081,
    0x8000000000008009,
    0x000000000000008A,
    0x0000000000000088,
    0x0000000080008009,
    0x000000008000000A,
    0x000000008000808B,
    0x800000000000008B,
    0x8000000000008089,
    0x8000000000008003,
    0x8000000000008002,
    0x8000000000000080,
    0x000000000000800A,
    0x800000008000000A,
    0x8000000080008081,
    0x8000000000008080,
    0x0000000080000001,
    0x8000000080008008,
)
_KECCAK_R = (
    (0, 1, 62, 28, 27),
    (36, 44, 6, 55, 20),
    (3, 10, 43, 25, 39),
    (41, 45, 15, 21, 8),
    (18, 2, 61, 56, 14),
)


def _rotl64(v: int, n: int) -> int:
    n &= 63
    return ((v << n) | (v >> (64 - n))) & 0xFFFFFFFFFFFFFFFF


def _keccak_f1600(s: list[int]) -> None:
    """In-place Keccak-f[1600] permutation. ``s`` is a flat list of 25 lanes."""
    for rc in _KECCAK_RC:
        # theta
        c = [s[x] ^ s[x + 5] ^ s[x + 10] ^ s[x + 15] ^ s[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl64(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(0, 25, 5):
                s[x + y] ^= d[x]
        # rho + pi
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + ((2 * x + 3 * y) % 5) * 5] = _rotl64(s[x + 5 * y], _KECCAK_R[y][x])
        # chi
        for y in range(0, 25, 5):
            t0, t1, t2, t3, t4 = b[y], b[y + 1], b[y + 2], b[y + 3], b[y + 4]
            s[y] = t0 ^ ((~t1) & 0xFFFFFFFFFFFFFFFF & t2)
            s[y + 1] = t1 ^ ((~t2) & 0xFFFFFFFFFFFFFFFF & t3)
            s[y + 2] = t2 ^ ((~t3) & 0xFFFFFFFFFFFFFFFF & t4)
            s[y + 3] = t3 ^ ((~t4) & 0xFFFFFFFFFFFFFFFF & t0)
            s[y + 4] = t4 ^ ((~t0) & 0xFFFFFFFFFFFFFFFF & t1)
        # iota
        s[0] ^= rc


def _keccak256(data: bytes) -> bytes:
    """Keccak-256 (NOT SHA3-256). Padding byte is 0x01, per Ethereum's
    convention; SHA3-256 would use 0x06."""
    rate = 136  # bytes (1088 bits) for 256-bit output
    state = [0] * 25
    # absorb full blocks
    offset = 0
    while offset + rate <= len(data):
        for i in range(rate // 8):
            lane = int.from_bytes(data[offset + 8 * i : offset + 8 * i + 8], "little")
            state[i] ^= lane
        _keccak_f1600(state)
        offset += rate
    # pad last block
    tail = bytearray(data[offset:])
    tail.append(0x01)
    while len(tail) < rate:
        tail.append(0x00)
    tail[-1] |= 0x80
    for i in range(rate // 8):
        lane = int.from_bytes(tail[8 * i : 8 * i + 8], "little")
        state[i] ^= lane
    _keccak_f1600(state)
    # squeeze 32 bytes
    out = bytearray()
    for i in range(4):  # 4 * 8 = 32 bytes
        out += state[i].to_bytes(8, "little")
    return bytes(out)


# Event signatures + topic[0]
EV_BATCH_APPENDED_SIG = b"BatchAppended(bytes32,bytes32,int256)"
EV_COMMITMENT_POSTED_SIG = b"CommitmentPosted(bytes32,bytes32,uint256,uint256)"
TOPIC_BATCH_APPENDED = "0x" + _keccak256(EV_BATCH_APPENDED_SIG).hex()
TOPIC_COMMITMENT_POSTED = "0x" + _keccak256(EV_COMMITMENT_POSTED_SIG).hex()


# --------------------------------------------------------------------------
# SQLite store
# --------------------------------------------------------------------------

_DB_LOCK = threading.Lock()


def _open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS batches (
            batch_hash      TEXT PRIMARY KEY,
            epoch           INTEGER,
            token_key       TEXT,
            liability_old   TEXT,
            liability_new   TEXT,
            delta           TEXT,
            tx_hash         TEXT,
            block_number    INTEGER,
            block_timestamp INTEGER,
            indexed_at      INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS commitments (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            addr_key        TEXT,
            token_key       TEXT,
            commitment_x    TEXT,
            commitment_y    TEXT,
            batch_hash      TEXT,
            tx_hash         TEXT,
            block_number    INTEGER,
            block_timestamp INTEGER,
            indexed_at      INTEGER
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS commitments_addr ON commitments(addr_key, indexed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS commitments_batch ON commitments(batch_hash)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_changes (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            opex_user         TEXT,
            asset             TEXT,
            delta             TEXT,
            expected_addr_key TEXT,
            inserted_at       INTEGER,
            resolved_tx_hash  TEXT,
            resolved_at       INTEGER
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS pending_addr ON pending_changes(expected_addr_key, resolved_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    return conn


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


# --------------------------------------------------------------------------
# Auth: bearer -> opex_user resolution (read-only on auth.db)
# --------------------------------------------------------------------------


def _verify_bearer(token: str) -> dict[str, Any] | None:
    if not token or not os.path.exists(AUTH_DB_PATH):
        return None
    try:
        uri = f"file:{urllib.parse.quote(AUTH_DB_PATH)}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        try:
            now = int(time.time())
            row = conn.execute(
                "SELECT u.id, u.opex_user, u.email "
                "FROM sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.token=? AND s.expires_at > ?",
                (token, now),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return {
            "id": int(row["id"]),
            "opex_user": row["opex_user"],
            "email": row["email"],
        }
    except sqlite3.OperationalError:
        return None


def _opex_to_addr_key(opex_user: str) -> str:
    """Mirror zkPoL's ``address_key_bytes`` = SHA-256(account_id_bytes).

    See ``zkpol/src/infrastructure/chain/chain_data_codec.rs::address_key_bytes``.
    Returns a 0x-prefixed 64-char hex string.
    """
    import hashlib

    return "0x" + hashlib.sha256(opex_user.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# JSON-RPC client
# --------------------------------------------------------------------------


class RpcError(RuntimeError):
    pass


_RPC_ID = 0
_RPC_LOCK = threading.Lock()


def _rpc(method: str, params: list[Any], *, timeout: float = 8.0) -> Any:
    global _RPC_ID
    with _RPC_LOCK:
        _RPC_ID += 1
        rid = _RPC_ID
    body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}).encode(
        "utf-8"
    )
    req = _http_request(
        RPC_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with _http_urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RpcError(f"HTTP {exc.code} from RPC") from exc
    except urllib.error.URLError as exc:
        raise RpcError(f"RPC unreachable: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise RpcError(f"RPC timeout: {exc}") from exc
    except ValueError as exc:
        raise RpcError(f"RPC bad JSON: {exc}") from exc
    if "error" in data and data["error"]:
        raise RpcError(f"RPC error: {data['error']}")
    return data.get("result")


def _hex_to_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    s = str(value)
    if not s:
        return 0
    return int(s, 16) if s.startswith(("0x", "0X")) else int(s)


def _hex_to_signed_int(value: Any, bits: int = 256) -> int:
    """Decode a hex bytes32 as a two's-complement signed integer."""
    n = _hex_to_int(value)
    mod = 1 << bits
    return n - mod if n & (1 << (bits - 1)) else n


def _topic_to_bytes32_hex(topic: str) -> str:
    """Topics are always 32-byte hex. Return canonical 0x-lowercase form."""
    t = topic.lower()
    if not t.startswith("0x"):
        t = "0x" + t
    return t


# --------------------------------------------------------------------------
# Indexer state machine
# --------------------------------------------------------------------------


class IndexerState:
    __slots__ = (
        "configured",
        "head_block",
        "scanned_to",
        "lag_blocks",
        "batches_count",
        "commitments_count",
        "pending_open_count",
        "last_tick_at",
        "last_tick_ok",
        "last_error",
        "took_ms",
    )

    def __init__(self) -> None:
        self.configured = bool(CONTRACT_ADDRESS)
        self.head_block: int = 0
        self.scanned_to: int = 0
        self.lag_blocks: int = 0
        self.batches_count: int = 0
        self.commitments_count: int = 0
        self.pending_open_count: int = 0
        self.last_tick_at: float = 0.0
        self.last_tick_ok: bool = False
        self.last_error: str | None = None
        self.took_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "degraded": not self.configured,
            "rpc_url": RPC_URL,
            "contract_address": CONTRACT_ADDRESS or None,
            "topic_batch_appended": TOPIC_BATCH_APPENDED,
            "topic_commitment_posted": TOPIC_COMMITMENT_POSTED,
            "head_block": self.head_block,
            "scanned_to": self.scanned_to,
            "lag_blocks": self.lag_blocks,
            "batches_count": self.batches_count,
            "commitments_count": self.commitments_count,
            "pending_open_count": self.pending_open_count,
            "last_tick_at": self.last_tick_at,
            "last_tick_at_iso": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.last_tick_at))
                if self.last_tick_at
                else None
            ),
            "last_tick_ok": self.last_tick_ok,
            "last_error": self.last_error,
            "took_ms": self.took_ms,
            "poll_interval_s": POLL_INTERVAL,
            "confirmations": CONFIRMATIONS,
            "message": (
                None
                if self.configured
                else "BulletinBoard not configured; set BULLETIN_BOARD_ADDRESS"
            ),
        }


_STATE = IndexerState()


def _resolve_pending(conn: sqlite3.Connection) -> None:
    """For each pending_changes row that still has resolved_at=NULL, look up
    whether a matching CommitmentPosted has shown up on-chain since it was
    inserted. The match key is ``expected_addr_key`` — we resolve to the
    most recent commitments row whose ``indexed_at`` >= ``inserted_at``."""
    pending = conn.execute(
        "SELECT id, expected_addr_key, inserted_at FROM pending_changes "
        "WHERE resolved_at IS NULL AND expected_addr_key IS NOT NULL"
    ).fetchall()
    for row in pending:
        match = conn.execute(
            "SELECT tx_hash, indexed_at FROM commitments "
            "WHERE addr_key=? AND indexed_at >= ? ORDER BY id ASC LIMIT 1",
            (row["expected_addr_key"], int(row["inserted_at"])),
        ).fetchone()
        if match:
            conn.execute(
                "UPDATE pending_changes SET resolved_tx_hash=?, resolved_at=? WHERE id=?",
                (match["tx_hash"], int(match["indexed_at"]), int(row["id"])),
            )


_BLOCK_TS_CACHE: dict[int, int] = {}


def _block_timestamp(block_number: int) -> int:
    if block_number in _BLOCK_TS_CACHE:
        return _BLOCK_TS_CACHE[block_number]
    try:
        blk = _rpc("eth_getBlockByNumber", [hex(block_number), False])
    except RpcError:
        return 0
    if not blk:
        return 0
    ts = _hex_to_int(blk.get("timestamp"))
    if len(_BLOCK_TS_CACHE) > 4096:
        _BLOCK_TS_CACHE.clear()
    _BLOCK_TS_CACHE[block_number] = ts
    return ts


def _ingest_log(conn: sqlite3.Connection, log: dict[str, Any]) -> None:
    topics = [(_topic_to_bytes32_hex(t)) for t in log.get("topics") or []]
    if not topics:
        return
    block_number = _hex_to_int(log.get("blockNumber"))
    tx_hash = (log.get("transactionHash") or "").lower()
    _hex_to_int(log.get("logIndex"))
    block_ts = _block_timestamp(block_number)
    now = int(time.time())

    topic0 = topics[0]
    data_hex = (log.get("data") or "0x").lower()
    data_hex_strip = data_hex[2:] if data_hex.startswith("0x") else data_hex

    if topic0 == TOPIC_BATCH_APPENDED:
        # topics: [sig, tokenKey, batchHash]
        # data:   [int256 liabilityNew]
        if len(topics) < 3:
            return
        token_key = topics[1]
        batch_hash = topics[2]
        liability_new = _hex_to_signed_int(data_hex[:66] if len(data_hex) >= 66 else data_hex)
        # The batches row stores liability_old/delta only if we can derive
        # them from the tx receipt (the on-chain BatchRecord struct is calldata,
        # not in the event). We approximate by carrying liability_new as the
        # latest value and back-filling delta from previous batches.
        prev = conn.execute(
            "SELECT liability_new FROM batches WHERE token_key=? "
            "ORDER BY block_number DESC, ROWID DESC LIMIT 1",
            (token_key,),
        ).fetchone()
        liab_old = int(prev["liability_new"]) if prev else 0
        delta = liability_new - liab_old
        epoch = conn.execute(
            "SELECT COUNT(*) AS n FROM batches WHERE token_key=?", (token_key,)
        ).fetchone()["n"]
        conn.execute(
            "INSERT OR IGNORE INTO batches "
            "(batch_hash, epoch, token_key, liability_old, liability_new, delta, "
            " tx_hash, block_number, block_timestamp, indexed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                batch_hash,
                int(epoch),
                token_key,
                str(liab_old),
                str(liability_new),
                str(delta),
                tx_hash,
                block_number,
                block_ts,
                now,
            ),
        )
    elif topic0 == TOPIC_COMMITMENT_POSTED:
        # topics: [sig, tokenKey, addrKey]
        # data:   [uint256 commitmentX, uint256 commitmentY]
        if len(topics) < 3:
            return
        token_key = topics[1]
        addr_key = topics[2]
        # data is exactly 64 bytes (= 128 hex chars after the 0x)
        if len(data_hex_strip) < 128:
            return
        x_hex = data_hex_strip[:64]
        y_hex = data_hex_strip[64:128]
        commitment_x = str(int(x_hex, 16))
        commitment_y = str(int(y_hex, 16))
        # Try to associate with the BatchAppended in the same tx. Both events
        # are emitted by the same appendBatch call, so they share txHash.
        batch_row = conn.execute(
            "SELECT batch_hash FROM batches WHERE tx_hash=? " "ORDER BY block_number DESC LIMIT 1",
            (tx_hash,),
        ).fetchone()
        batch_hash = batch_row["batch_hash"] if batch_row else None
        conn.execute(
            "INSERT INTO commitments "
            "(addr_key, token_key, commitment_x, commitment_y, batch_hash, "
            " tx_hash, block_number, block_timestamp, indexed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                addr_key,
                token_key,
                commitment_x,
                commitment_y,
                batch_hash,
                tx_hash,
                block_number,
                block_ts,
                now,
            ),
        )
    # Unknown topic — silently ignore.


def _scan_range(conn: sqlite3.Connection, from_block: int, to_block: int) -> None:
    """Fetch eth_getLogs for the [from, to] block window and persist hits."""
    if from_block > to_block:
        return
    params = [
        {
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "address": CONTRACT_ADDRESS,
            "topics": [[TOPIC_BATCH_APPENDED, TOPIC_COMMITMENT_POSTED]],
        }
    ]
    logs = _rpc("eth_getLogs", params, timeout=15.0) or []

    # Sort by (blockNumber, logIndex) so BatchAppended is processed before
    # CommitmentPosted within the same tx (Solidity emits BatchAppended last
    # actually, but we sort by logIndex ascending to be deterministic; the
    # ingest path handles either order via tx_hash join).
    def _sort_key(log: dict[str, Any]) -> tuple[int, int]:
        return (_hex_to_int(log.get("blockNumber")), _hex_to_int(log.get("logIndex")))

    # First pass: ingest BatchAppended. Second pass: CommitmentPosted. That way
    # commitments can be joined to their batch by tx_hash unconditionally.
    logs_sorted = sorted(logs, key=_sort_key)
    for log in logs_sorted:
        topics = log.get("topics") or []
        if topics and _topic_to_bytes32_hex(topics[0]) == TOPIC_BATCH_APPENDED:
            _ingest_log(conn, log)
    for log in logs_sorted:
        topics = log.get("topics") or []
        if topics and _topic_to_bytes32_hex(topics[0]) == TOPIC_COMMITMENT_POSTED:
            _ingest_log(conn, log)


def _scan_once() -> None:
    started = time.time()
    _STATE.last_tick_ok = False
    _STATE.last_error = None
    if not CONTRACT_ADDRESS:
        _STATE.last_tick_at = time.time()
        _STATE.last_error = "BulletinBoard not configured"
        _STATE.took_ms = int((time.time() - started) * 1000)
        return
    try:
        head_hex = _rpc("eth_blockNumber", [])
        head = _hex_to_int(head_hex)
    except RpcError as exc:
        _STATE.last_error = f"rpc_unreachable: {exc}"
        _STATE.last_tick_at = time.time()
        _STATE.took_ms = int((time.time() - started) * 1000)
        return
    _STATE.head_block = head
    safe_head = max(0, head - CONFIRMATIONS)

    with _DB_LOCK:
        conn = _open_db()
        try:
            cur = _meta_get(conn, "last_scanned_block")
            cursor = int(cur) if cur is not None else max(START_BLOCK - 1, -1)
            # On each tick we also re-scan the last CONFIRMATIONS blocks past
            # the cursor to catch tiny reorgs.
            from_block = max(START_BLOCK, cursor + 1 - CONFIRMATIONS)
            to_block = safe_head
            if to_block >= from_block:
                step = from_block
                while step <= to_block:
                    end = min(step + GETLOGS_RANGE - 1, to_block)
                    try:
                        _scan_range(conn, step, end)
                    except RpcError as exc:
                        _STATE.last_error = f"getlogs_failed: {exc}"
                        break
                    step = end + 1
                else:
                    _meta_set(conn, "last_scanned_block", str(to_block))
            _resolve_pending(conn)
            counts = conn.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM batches) AS b, "
                "(SELECT COUNT(*) FROM commitments) AS c, "
                "(SELECT COUNT(*) FROM pending_changes WHERE resolved_at IS NULL) AS p"
            ).fetchone()
            _STATE.batches_count = int(counts["b"])
            _STATE.commitments_count = int(counts["c"])
            _STATE.pending_open_count = int(counts["p"])
            saved = _meta_get(conn, "last_scanned_block")
            _STATE.scanned_to = int(saved) if saved else 0
            _STATE.lag_blocks = max(0, head - _STATE.scanned_to)
            _STATE.last_tick_ok = _STATE.last_error is None
        finally:
            conn.close()
    _STATE.last_tick_at = time.time()
    _STATE.took_ms = int((time.time() - started) * 1000)


def _scan_loop(stop: threading.Event) -> None:
    sys.stderr.write(
        f"[anchor-indexer] loop start interval={POLL_INTERVAL}s "
        f"rpc={RPC_URL} contract={CONTRACT_ADDRESS or '(unconfigured)'}\n"
    )
    while not stop.is_set():
        try:
            _scan_once()
        except Exception as exc:  # noqa: BLE001 — fail-soft
            _STATE.last_error = f"{type(exc).__name__}: {exc}"
            _STATE.last_tick_at = time.time()
        stop.wait(POLL_INTERVAL)


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------


class AnchorHandler(http.server.BaseHTTPRequestHandler):
    server_version = "anchor-indexer/1.0"

    # --- helpers -------------------------------------------------------

    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self) -> str | None:
        h = self.headers.get("Authorization") or ""
        if h.startswith("Bearer "):
            return h[7:].strip()
        return None

    def _is_loopback(self) -> bool:
        host = (self.client_address or ("", 0))[0]
        return host in ("127.0.0.1", "::1", "localhost")

    def _strip_prefix(self, path: str) -> str:
        if path.startswith("/anchor/"):
            return path[len("/anchor") :]
        return path

    def _require_configured(self) -> bool:
        if CONTRACT_ADDRESS:
            return True
        self._json(
            503,
            {
                "error": "degraded",
                "message": "BulletinBoard not deployed; set BULLETIN_BOARD_ADDRESS",
            },
        )
        return False

    # --- request entry points -----------------------------------------

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        path, _, query = self.path.partition("?")
        path = self._strip_prefix(path)
        if path == "/health":
            return self._json(200, _STATE.as_dict())
        if path.startswith("/internal/"):
            self._json(404, {"error": "not_found"})
            return
        if not self._require_configured():
            return
        qs = urllib.parse.parse_qs(query) if query else {}
        if path == "/latest":
            return self._handle_latest()
        if path == "/batches":
            limit = max(1, min(int((qs.get("limit") or [50])[0]), 500))
            return self._handle_batches(limit)
        if path.startswith("/batch/"):
            bh = path[len("/batch/") :].strip()
            return self._handle_batch(bh)
        if path.startswith("/account/"):
            addr = path[len("/account/") :].strip()
            return self._handle_account(addr, qs)
        self._json(404, {"error": "not_found", "path": self.path})

    def do_POST(self):  # noqa: N802
        path, _, _ = self.path.partition("?")
        path = self._strip_prefix(path)
        if not path.startswith("/internal/"):
            return self._json(404, {"error": "not_found"})
        if not self._is_loopback():
            return self._json(404, {"error": "not_found"})
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            return self._json(400, {"error": "bad_json"})
        if path == "/internal/log-pending":
            return self._handle_log_pending(body)
        if path == "/internal/reindex":
            return self._handle_reindex(body)
        self._json(404, {"error": "not_found"})

    # --- public handlers ----------------------------------------------

    def _handle_latest(self) -> None:
        try:
            tip = _hex_to_int(_rpc("eth_blockNumber", []))
        except RpcError as exc:
            return self._json(503, {"error": "rpc", "message": str(exc)})
        latest_hash: str | None = None
        # Try the live contract call first. If it fails (e.g. wrong ABI on the
        # peer), fall back to the most recent indexed batch.
        try:
            call_data = "0x" + _keccak256(b"latestBatchHash()")[:4].hex()
            res = _rpc(
                "eth_call",
                [{"to": CONTRACT_ADDRESS, "data": call_data}, "latest"],
            )
            if isinstance(res, str) and res.startswith("0x") and len(res) >= 66:
                latest_hash = "0x" + res[2:66].lower()
        except RpcError:
            latest_hash = None
        if latest_hash is None:
            with _DB_LOCK:
                conn = _open_db()
                try:
                    row = conn.execute(
                        "SELECT batch_hash FROM batches "
                        "ORDER BY block_number DESC, ROWID DESC LIMIT 1"
                    ).fetchone()
                finally:
                    conn.close()
            latest_hash = row["batch_hash"] if row else None
        return self._json(
            200,
            {
                "latest_batch_hash": latest_hash,
                "chain_head_block": tip,
                "scanned_to": _STATE.scanned_to,
                "lag_blocks": _STATE.lag_blocks,
                "contract_address": CONTRACT_ADDRESS,
            },
        )

    def _handle_batches(self, limit: int) -> None:
        with _DB_LOCK:
            conn = _open_db()
            try:
                rows = conn.execute(
                    "SELECT batch_hash, epoch, token_key, liability_old, liability_new, "
                    "       delta, tx_hash, block_number, block_timestamp "
                    "FROM batches ORDER BY block_number DESC, ROWID DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                totals_rows = conn.execute(
                    "SELECT token_key, liability_new FROM batches b "
                    "WHERE block_number = (SELECT MAX(block_number) FROM batches "
                    "                       WHERE token_key=b.token_key)"
                ).fetchall()
            finally:
                conn.close()
        out = [
            {
                "batch_hash": r["batch_hash"],
                "epoch": int(r["epoch"]),
                "token_key": r["token_key"],
                "liability_old": r["liability_old"],
                "liability_new": r["liability_new"],
                "delta": r["delta"],
                "tx_hash": r["tx_hash"],
                "block_number": int(r["block_number"]),
                "block_timestamp": int(r["block_timestamp"]),
            }
            for r in rows
        ]
        totals = {row["token_key"]: row["liability_new"] for row in totals_rows}
        return self._json(200, {"batches": out, "totals_by_token": totals})

    def _handle_batch(self, batch_hash: str) -> None:
        bh = batch_hash.lower()
        if not bh.startswith("0x") or len(bh) != 66:
            return self._json(400, {"error": "bad_batch_hash"})
        with _DB_LOCK:
            conn = _open_db()
            try:
                row = conn.execute(
                    "SELECT batch_hash, epoch, token_key, liability_old, liability_new, "
                    "       delta, tx_hash, block_number, block_timestamp "
                    "FROM batches WHERE batch_hash=?",
                    (bh,),
                ).fetchone()
                comm_rows = conn.execute(
                    "SELECT addr_key, commitment_x, commitment_y, tx_hash, "
                    "       block_number, block_timestamp "
                    "FROM commitments WHERE batch_hash=? "
                    "ORDER BY id ASC",
                    (bh,),
                ).fetchall()
            finally:
                conn.close()
        if not row:
            return self._json(404, {"error": "not_found"})
        return self._json(
            200,
            {
                "batch": {
                    "batch_hash": row["batch_hash"],
                    "epoch": int(row["epoch"]),
                    "token_key": row["token_key"],
                    "liability_old": row["liability_old"],
                    "liability_new": row["liability_new"],
                    "delta": row["delta"],
                    "tx_hash": row["tx_hash"],
                    "block_number": int(row["block_number"]),
                    "block_timestamp": int(row["block_timestamp"]),
                },
                "commitments": [
                    {
                        "addr_key": c["addr_key"],
                        "commitment_x": c["commitment_x"],
                        "commitment_y": c["commitment_y"],
                        "tx_hash": c["tx_hash"],
                        "block_number": int(c["block_number"]),
                        "block_timestamp": int(c["block_timestamp"]),
                    }
                    for c in comm_rows
                ],
            },
        )

    def _handle_account(self, addr_key: str, qs: dict[str, list[str]]) -> None:
        # Bearer auth required; only allow lookups of the caller's own addr_key.
        tok = self._bearer()
        user = _verify_bearer(tok or "")
        if not user:
            return self._json(401, {"error": "unauthorized"})
        ak = addr_key.lower()
        if not ak.startswith("0x"):
            ak = "0x" + ak
        if len(ak) != 66:
            return self._json(400, {"error": "bad_addr_key"})
        expected = _opex_to_addr_key(user["opex_user"])
        if ak != expected:
            # We deliberately do NOT surface the expected value — the UI
            # already knows it (it called us with the right one).
            return self._json(403, {"error": "forbidden"})
        limit = max(1, min(int((qs.get("limit") or [25])[0]), 500))
        with _DB_LOCK:
            conn = _open_db()
            try:
                rows = conn.execute(
                    "SELECT id, token_key, commitment_x, commitment_y, "
                    "       batch_hash, tx_hash, block_number, block_timestamp, indexed_at "
                    "FROM commitments WHERE addr_key=? "
                    "ORDER BY indexed_at DESC, id DESC LIMIT ?",
                    (ak, limit),
                ).fetchall()
                pending_rows = conn.execute(
                    "SELECT id, asset, delta, inserted_at, resolved_tx_hash, resolved_at "
                    "FROM pending_changes WHERE expected_addr_key=? "
                    "ORDER BY id DESC LIMIT 50",
                    (ak,),
                ).fetchall()
            finally:
                conn.close()
        latest = rows[0] if rows else None
        return self._json(
            200,
            {
                "opex_user": user["opex_user"],
                "addr_key": ak,
                "latest_commitment": (
                    {
                        "x": latest["commitment_x"],
                        "y": latest["commitment_y"],
                        "tx_hash": latest["tx_hash"],
                        "block_number": int(latest["block_number"]),
                    }
                    if latest
                    else None
                ),
                "history": [
                    {
                        "token_key": r["token_key"],
                        "commitment_x": r["commitment_x"],
                        "commitment_y": r["commitment_y"],
                        "batch_hash": r["batch_hash"],
                        "tx_hash": r["tx_hash"],
                        "block_number": int(r["block_number"]),
                        "block_timestamp": int(r["block_timestamp"]),
                    }
                    for r in rows
                ],
                "pending": [
                    {
                        "id": int(p["id"]),
                        "asset": p["asset"],
                        "delta": p["delta"],
                        "inserted_at": int(p["inserted_at"]),
                        "resolved_tx_hash": p["resolved_tx_hash"],
                        "resolved_at": int(p["resolved_at"]) if p["resolved_at"] else None,
                    }
                    for p in pending_rows
                ],
            },
        )

    # --- internal-only handlers ---------------------------------------

    def _handle_log_pending(self, body: dict[str, Any]) -> None:
        opex_user = (body.get("opex_user") or "").strip()
        asset = (body.get("asset") or "").strip()
        delta = body.get("delta")
        if not opex_user or not asset:
            return self._json(400, {"error": "missing_fields"})
        expected_addr_key = (body.get("expected_addr_key") or "").strip().lower()
        if not expected_addr_key:
            expected_addr_key = _opex_to_addr_key(opex_user)
        with _DB_LOCK:
            conn = _open_db()
            try:
                conn.execute(
                    "INSERT INTO pending_changes "
                    "(opex_user, asset, delta, expected_addr_key, inserted_at) "
                    "VALUES (?,?,?,?,?)",
                    (opex_user, asset, str(delta), expected_addr_key, int(time.time())),
                )
                new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            finally:
                conn.close()
        return self._json(200, {"id": int(new_id), "expected_addr_key": expected_addr_key})

    def _handle_reindex(self, body: dict[str, Any]) -> None:
        from_block = body.get("from_block")
        try:
            from_block_int = int(from_block) if from_block is not None else START_BLOCK
        except (TypeError, ValueError):
            return self._json(400, {"error": "bad_from_block"})
        with _DB_LOCK:
            conn = _open_db()
            try:
                _meta_set(conn, "last_scanned_block", str(max(0, from_block_int - 1)))
            finally:
                conn.close()
        return self._json(200, {"reset_to": from_block_int})

    # --- noise -------------------------------------------------------

    def log_message(self, fmt, *args):  # quiet
        sys.stderr.write(f"[anchor-indexer] {self.address_string()} - {fmt % args}\n")


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5707
    stop = threading.Event()
    if CONTRACT_ADDRESS:
        th = threading.Thread(target=_scan_loop, args=(stop,), daemon=True)
        th.start()
    else:
        sys.stderr.write(
            "[anchor-indexer] starting in DEGRADED mode "
            "(BULLETIN_BOARD_ADDRESS unset). /anchor/health works; "
            "other endpoints will return 503.\n"
        )
    server = _ThreadingServer(("127.0.0.1", port), AnchorHandler)
    sys.stderr.write(
        f"[anchor-indexer] listening on :{port}  (rpc={RPC_URL} "
        f"contract={CONTRACT_ADDRESS or '-'} start={START_BLOCK})\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("[anchor-indexer] shutting down\n")
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
