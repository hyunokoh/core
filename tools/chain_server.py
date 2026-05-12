#!/usr/bin/env python3
"""zkCEX chain bridge — talks to a local hardhat node and the existing
wallet API to make on-chain deposits / withdrawals feel real to the demo
user.

  Browser  -->  homepage proxy (:5500)  -->  chain_server (:5502)
                                                |
                                                +--> hardhat JSON-RPC :8545
                                                +--> auth_server :5501 (token -> opex_user)
                                                +--> wallet API :8091 (internal credit / debit)

Stdlib only. Talks to hardhat over raw JSON-RPC; no web3.py.

Dev account[0] of hardhat is unlocked and we use it as both deployer (mint)
and exchange custodial wallet (signs Transfers on withdrawal). Everything is
deterministic and reset on each `run.sh` invocation.
"""

from __future__ import annotations

import http.server
import json
import logging
import os
import re
import secrets
import shutil
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from typing import Any

# ----- paths and constants --------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
# providers/ is a sibling package next to this file -- ensure importable
# regardless of the cwd the server was launched from.
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from asset_precisions import (  # noqa: E402  (after sys.path mutation)
    asset_spec,
    to_wei_for_chain,
    validate_withdraw_amount,
)
from providers import aml_provider, geo_provider  # noqa: E402  (after sys.path mutation)

# OpenTelemetry tracing (stdlib-only). Service name MUST be set before the
# otel package is imported (its module-level config snapshots env vars).
os.environ.setdefault("OTEL_SERVICE_NAME", "zkcex-chain")
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


HARDHAT_DIR = os.path.join(HERE, "hardhat-sim")
DEPLOYMENT_PATH = os.path.join(HARDHAT_DIR, ".local", "deployment.json")
DERIVE_SCRIPT = os.path.join(HARDHAT_DIR, "scripts", "derive.js")
CHAIN_DB_PATH = os.path.join(HERE, ".local", "chain.db")


def _validated_http_url(raw_url: str, *, name: str = "url") -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return raw_url


def _validated_http_base_url(name: str, raw_url: str) -> str:
    return _validated_http_url(raw_url, name=name).rstrip("/")


def _validated_optional_http_base_url(name: str, raw_url: str) -> str:
    return _validated_http_base_url(name, raw_url) if raw_url else ""


def _http_request(url: str, **kwargs) -> urllib.request.Request:
    return urllib.request.Request(_validated_http_url(url), **kwargs)  # noqa: S310


def _http_urlopen(target, **kwargs):
    if isinstance(target, str):
        target = _validated_http_url(target)
    return urllib.request.urlopen(target, **kwargs)  # noqa: S310


AUTH_BASE = _validated_http_base_url("AUTH_BASE", "http://127.0.0.1:5501")
WALLET_BASE = _validated_http_base_url("WALLET_BASE", "http://127.0.0.1:8091")
DEFAULT_RPC = _validated_http_base_url("DEFAULT_RPC", "http://127.0.0.1:8545")
PUSH_BASE = _validated_http_base_url(
    "PUSH_BASE", os.environ.get("PUSH_BASE", "http://127.0.0.1:5580")
)
# ops_server for large-withdraw review
OPS_BASE = _validated_http_base_url("OPS_BASE", os.environ.get("OPS_BASE", "http://127.0.0.1:5620"))
TRAVEL_RULE_BASE = _validated_http_base_url(
    "TRAVEL_RULE_BASE", os.environ.get("TRAVEL_RULE_BASE", "http://127.0.0.1:5630")
)
WD_REVIEW_USDT = Decimal(os.environ.get("WITHDRAW_REVIEW_THRESHOLD_USDT", "1000"))


def _push_notify(opex_user: str, payload: dict) -> None:
    """Best-effort fan-out to push_server.

    Wrapped in try/except so a push-server outage never breaks the credit
    path. Logs at debug to keep deposit flows quiet.
    """
    if not opex_user:
        return
    try:
        body = json.dumps({"opex_user": opex_user, "payload": payload}).encode("utf-8")
        req = _http_request(
            f"{PUSH_BASE}/push/send",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        _http_urlopen(req, timeout=2).read()
    except Exception as exc:
        # Log at info, not error — push is decorative.
        log.info("push_notify skipped: %s", exc)


NOTIF_BASE = _validated_http_base_url(
    "NOTIF_BASE", os.environ.get("NOTIF_BASE", "http://127.0.0.1:5691")
)


def _notif_send(
    opex_user: str, ntype: str, category: str, title: str, body: str, metadata: dict | None = None
) -> None:
    """Best-effort inbox + email + push fan-out via notification_center."""
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
    except Exception as exc:
        log.info("notif_send skipped: %s", exc)


# ---- threshold-custody integration ----------------------------------------
# When CUSTODY_COORDINATOR_URL is set, withdrawal broadcasts are routed
# through the M-of-N custody coordinator (see tools/custody/) instead of the
# unlocked hardhat dev account.
#
# When unset, behaviour is unchanged from the original demo: the custodial
# address is hardhat's account[0], which is unlocked by default, so
# `eth_sendTransaction` works as-is. This keeps the one-click `run.sh`
# experience intact for reviewers who don't want to spin up the custody
# fleet.
CUSTODY_COORDINATOR_URL = _validated_optional_http_base_url(
    "CUSTODY_COORDINATOR_URL", os.environ.get("CUSTODY_COORDINATOR_URL", "").strip()
)
CUSTODY_COORDINATOR_TOKEN = os.environ.get("CUSTODY_COORDINATOR_TOKEN", "").strip()


def custody_enabled() -> bool:
    return bool(CUSTODY_COORDINATOR_URL and CUSTODY_COORDINATOR_TOKEN)


# Asset -> internal wallet symbol used by the wallet API. The wallet API uses
# the symbols ETH / USDT (no zk- prefix), so we route ZETH -> ETH and ZUSDT ->
# USDT when crediting internal balances.
ASSET_TO_INTERNAL_SYMBOL = {"ZETH": "ETH", "ZUSDT": "USDT"}

# The /deposit/{amount}_test-ethereum_{symbol}/{user}_MAIN endpoint caps each
# call at 10 units. We chunk larger deposits across multiple calls.
INTERNAL_DEPOSIT_CAP = 10

POLL_INTERVAL_SECONDS = 3

# Per-chain background probe cadence. Public-RPC etiquette: don't hammer.
CHAIN_PROBE_INTERVAL_SECONDS = 60.0
CHAIN_PROBE_TIMEOUT = 6.0
CHAIN_STALE_RED_SECONDS = 600  # >10min cache -> UI shows red

getcontext().prec = 60  # decimal precision for token math


# ----- multi-chain registry -------------------------------------------------
#
# zkCEX runs against a local hardhat node for the demo's writable flow, plus a
# handful of public testnets that we read live balances from. Public testnet
# RPCs ship without API keys and our demo doesn't actually hold testnet funds,
# so write-style endpoints (faucet, send-from-user, withdraw) are blocked on
# every chain other than hardhat with a clean 503 explanation.
#
# Each chain has a list of RPC URLs we try in order; on RPC failure we mark
# the chain `degraded` and rotate to the next URL. A background thread re-
# probes every CHAIN_PROBE_INTERVAL_SECONDS seconds.
@dataclass
class ChainConfig:
    chain_id: int
    name: str  # human-readable, e.g. "Hardhat (Demo)"
    slug: str  # routing key, e.g. "hardhat" / "sepolia"
    rpc_urls: list[str]  # try in order; on RPCError move to next
    native_symbol: str  # "ETH" | "MATIC"
    explorer_tx_url: str  # template like "https://sepolia.etherscan.io/tx/{tx}"
    is_demo: bool  # True only for hardhat
    erc20s: dict = field(default_factory=dict)  # {"USDC": "0x..."} for sepolia, etc.
    # Public testnet faucet links surfaced in the UI for read-only chains.
    faucets: list[dict] = field(default_factory=list)


CHAINS: list[ChainConfig] = [
    ChainConfig(
        chain_id=31337,
        name="Hardhat (Demo)",
        slug="hardhat",
        rpc_urls=[DEFAULT_RPC],
        native_symbol="ETH",
        # Hardhat has no public block explorer; leave the template inert.
        explorer_tx_url="about:blank#tx={tx}",
        is_demo=True,
        erc20s={},  # filled at runtime from deployment.json
        faucets=[],
    ),
    ChainConfig(
        chain_id=11155111,
        name="Sepolia",
        slug="sepolia",
        # The two URLs the spec listed are now non-functional in 2026 —
        # blastapi.io's free tier has been retired and rpc.sepolia.org returns
        # 404. We try them anyway (so the spec'd hosts remain in the list)
        # and fall through to a small set of well-known no-key public
        # endpoints (publicnode.com, drpc.org, 1rpc.io) on rotation.
        rpc_urls=[
            "https://eth-sepolia.public.blastapi.io",
            "https://rpc.sepolia.org",
            "https://ethereum-sepolia-rpc.publicnode.com",
            "https://sepolia.drpc.org",
            "https://1rpc.io/sepolia",
        ],
        native_symbol="ETH",
        explorer_tx_url="https://sepolia.etherscan.io/tx/{tx}",
        is_demo=False,
        erc20s={
            # Circle's testnet USDC on Sepolia.
            "USDC": "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238",
        },
        faucets=[
            {"label": "sepoliafaucet.com", "url": "https://sepoliafaucet.com"},
            {"label": "sepolia-faucet.pk910.de", "url": "https://sepolia-faucet.pk910.de"},
        ],
    ),
    ChainConfig(
        chain_id=80002,
        name="Polygon Amoy",
        slug="polygon-amoy",
        rpc_urls=[
            "https://rpc-amoy.polygon.technology",
            "https://polygon-amoy-bor-rpc.publicnode.com",
        ],
        native_symbol="MATIC",
        explorer_tx_url="https://amoy.polygonscan.com/tx/{tx}",
        is_demo=False,
        erc20s={},
        faucets=[
            {"label": "faucet.polygon.technology", "url": "https://faucet.polygon.technology/"},
            {"label": "mumbaifaucet.com", "url": "https://mumbaifaucet.com"},
        ],
    ),
    ChainConfig(
        chain_id=421614,
        name="Arbitrum Sepolia",
        slug="arbitrum-sepolia",
        rpc_urls=[
            "https://sepolia-rollup.arbitrum.io/rpc",
            "https://arbitrum-sepolia-rpc.publicnode.com",
        ],
        native_symbol="ETH",
        explorer_tx_url="https://sepolia.arbiscan.io/tx/{tx}",
        is_demo=False,
        erc20s={},
        faucets=[
            {"label": "sepoliafaucet.com (then bridge)", "url": "https://sepoliafaucet.com"},
        ],
    ),
]
DEFAULT_CHAIN = "hardhat"
CHAIN_NOT_WRITABLE_MSG = (
    "this demo only writes to the local hardhat chain. "
    "Sepolia/Amoy/Arbitrum show real read-only balances."
)


def chain_by_slug(slug: str | None) -> ChainConfig | None:
    if not slug:
        return None
    for c in CHAINS:
        if c.slug == slug:
            return c
    return None


# Per-chain runtime status, populated by the prober and consulted by every
# read endpoint. Keys mirror ChainConfig.slug.
@dataclass
class ChainState:
    slug: str
    rpc_status: str = "unknown"  # "ok" | "degraded" | "unknown"
    active_rpc: str | None = None
    block_number: int | None = None
    last_ok_at: float | None = None
    last_attempt_at: float | None = None
    latency_ms: int | None = None


_chain_state: dict[str, ChainState] = {c.slug: ChainState(slug=c.slug) for c in CHAINS}
_chain_state_lock = threading.Lock()

logging.basicConfig(
    level=os.environ.get("CHAIN_LOG", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("chain_server")

# ----- JSON-RPC client (stdlib only) ----------------------------------------


class RpcError(RuntimeError):
    pass


def rpc_call(
    method: str,
    params: list[Any] | None = None,
    *,
    rpc_url: str | None = None,
    timeout: float = 15.0,
) -> Any:
    rpc_url = rpc_url or DEFAULT_RPC
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000) & 0xFFFFFFFF,
            "method": method,
            "params": params or [],
        }
    ).encode()
    # Some public RPC providers (BlastAPI, etc.) reject the default Python
    # urllib User-Agent with HTTP 403, so set a friendly one. Local hardhat
    # doesn't care.
    req = _http_request(
        rpc_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "zkCEX-chain/0.2",
            "Accept": "application/json",
        },
    )
    try:
        with _http_urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
    except urllib.error.URLError as e:
        raise RpcError(f"rpc transport error: {e}") from e
    except (TimeoutError, OSError) as e:
        raise RpcError(f"rpc transport error: {e}") from e
    try:
        obj = json.loads(body)
    except Exception as e:
        raise RpcError(f"rpc bad json: {body[:200]}") from e
    if "error" in obj and obj["error"]:
        raise RpcError(f"rpc error: {obj['error']}")
    return obj.get("result")


def rpc_call_for_chain(
    chain: ChainConfig, method: str, params: list[Any] | None = None, *, timeout: float = 8.0
) -> Any:
    """Same as rpc_call but rotates through the chain's rpc_urls on failure
    and updates the chain-state cache. Raises RpcError only if every URL
    in the chain's list fails.
    """
    if not chain.rpc_urls:
        raise RpcError(f"chain {chain.slug}: no rpc urls configured")
    last_err: Exception | None = None
    # Prefer the currently-active URL; fall back through the rest in order.
    with _chain_state_lock:
        active = _chain_state[chain.slug].active_rpc
    ordered = list(chain.rpc_urls)
    if active and active in ordered:
        ordered.remove(active)
        ordered.insert(0, active)
    for url in ordered:
        try:
            t0 = time.time()
            result = rpc_call(method, params, rpc_url=url, timeout=timeout)
            latency_ms = int((time.time() - t0) * 1000)
            # Refresh the cached active URL and (if this was a block-number
            # probe) the cached height. Other call sites also benefit.
            with _chain_state_lock:
                st = _chain_state[chain.slug]
                st.rpc_status = "ok"
                st.active_rpc = url
                st.last_ok_at = time.time()
                st.last_attempt_at = st.last_ok_at
                st.latency_ms = latency_ms
                if method == "eth_blockNumber" and isinstance(result, str):
                    try:
                        st.block_number = int(result, 16)
                    except Exception as e:  # noqa: BLE001
                        log.debug("invalid block number from %s: %r (%s)", url, result, e)
            return result
        except RpcError as e:
            last_err = e
            log.warning("rpc fail %s %s -> %s; rotating", chain.slug, url, e)
            continue
    # All URLs failed: mark degraded (keep cached block_number for grace).
    with _chain_state_lock:
        st = _chain_state[chain.slug]
        st.rpc_status = "degraded"
        st.last_attempt_at = time.time()
    raise RpcError(f"chain {chain.slug}: all rpc urls failed: {last_err}")


# ----- ABI encoding helpers (just what we need) -----------------------------


def keccak256(data: bytes) -> bytes:
    # Python's stdlib hashlib has keccak via sha3_256? No — sha3_256 is FIPS
    # SHA3 (with 0x06 padding) which is *different* from Ethereum's keccak256
    # (0x01 padding). Implement the keccak permutation manually. It's small.
    return _keccak_f1600(data, capacity=512, suffix=0x01, output_len=32)


# Minimal Keccak-f[1600] permutation. Adapted from the public-domain
# pseudocode in NIST FIPS 202 / Keccak reference. Pure python; not fast but
# we only call it on short inputs (event topics, function selectors).
_RC = (
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
_R = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)


def _rotl64(x: int, n: int) -> int:
    n &= 63
    return ((x << n) | (x >> (64 - n))) & 0xFFFFFFFFFFFFFFFF


def _keccak_f(state: list[list[int]]) -> None:
    for rnd in range(24):
        # theta
        C = [state[x][0] ^ state[x][1] ^ state[x][2] ^ state[x][3] ^ state[x][4] for x in range(5)]
        D = [C[(x - 1) % 5] ^ _rotl64(C[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x][y] ^= D[x]
        # rho + pi
        B = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                B[y][(2 * x + 3 * y) % 5] = _rotl64(state[x][y], _R[x][y])
        # chi
        for x in range(5):
            for y in range(5):
                state[x][y] = B[x][y] ^ ((~B[(x + 1) % 5][y]) & B[(x + 2) % 5][y])
        # iota
        state[0][0] ^= _RC[rnd]


def _keccak_f1600(data: bytes, capacity: int, suffix: int, output_len: int) -> bytes:
    rate = (1600 - capacity) // 8  # in bytes
    state = [[0] * 5 for _ in range(5)]
    # absorb
    msg = data + bytes([suffix])
    pad_len = (-len(msg)) % rate
    msg += bytes(pad_len)
    msg = bytearray(msg)
    msg[-1] |= 0x80
    for offset in range(0, len(msg), rate):
        block = msg[offset : offset + rate]
        for i in range(rate // 8):
            lane = int.from_bytes(block[i * 8 : i * 8 + 8], "little")
            x = i % 5
            y = i // 5
            state[x][y] ^= lane
        _keccak_f(state)
    # squeeze
    out = bytearray()
    while len(out) < output_len:
        for y in range(5):
            for x in range(5):
                if len(out) >= output_len:
                    break
                out += state[x][y].to_bytes(8, "little")
                if (x + y * 5 + 1) * 8 >= rate:
                    break
            if len(out) >= output_len:
                break
        if len(out) < output_len:
            _keccak_f(state)
    return bytes(out[:output_len])


def fn_selector(signature: str) -> str:
    """eth function selector — first 4 bytes of keccak("name(types)")."""
    return "0x" + keccak256(signature.encode()).hex()[:8]


def event_topic(signature: str) -> str:
    return "0x" + keccak256(signature.encode()).hex()


def addr_to_hex(addr: str) -> str:
    a = addr.lower()
    if a.startswith("0x"):
        a = a[2:]
    if not re.fullmatch(r"[0-9a-f]{40}", a):
        raise ValueError(f"bad address: {addr}")
    return "0x" + a


def encode_address(addr: str) -> str:
    return "0".zfill(24) + addr_to_hex(addr)[2:]


def encode_uint256(n: int) -> str:
    if n < 0:
        raise ValueError("negative uint256")
    return f"{n:064x}"


def decode_uint256(hex_word: str) -> int:
    if hex_word.startswith("0x"):
        hex_word = hex_word[2:]
    return int(hex_word, 16) if hex_word else 0


def decode_address(hex_word: str) -> str:
    h = hex_word
    if h.startswith("0x"):
        h = h[2:]
    return "0x" + h[-40:]


# Common selectors / topics
SEL_BALANCE_OF = fn_selector("balanceOf(address)")
SEL_TRANSFER = fn_selector("transfer(address,uint256)")
SEL_MINT = fn_selector("mint(address,uint256)")
TOPIC_TRANSFER = event_topic("Transfer(address,address,uint256)")


def hex_block(n: int) -> str:
    return hex(n)


def hex_qty(n: int) -> str:
    return hex(n)


# ----- deployment + token metadata ------------------------------------------


_deployment_lock = threading.Lock()
_deployment_cache: dict | None = None


def load_deployment() -> dict | None:
    global _deployment_cache
    with _deployment_lock:
        if _deployment_cache:
            return _deployment_cache
        try:
            with open(DEPLOYMENT_PATH) as f:
                _deployment_cache = json.load(f)
        except FileNotFoundError:
            return None
        return _deployment_cache


def token_decimals(symbol: str) -> int:
    d = load_deployment()
    if d:
        return int(d["decimals"][symbol])
    return 18 if symbol == "ZETH" else 6


def token_address(symbol: str) -> str:
    d = load_deployment()
    if not d:
        raise RpcError("deployment not loaded")
    return d["tokens"][symbol]


def custodial_address() -> str:
    d = load_deployment()
    if not d:
        raise RpcError("deployment not loaded")
    return d["custodial"].lower()


def deployer_address() -> str:
    d = load_deployment()
    if not d:
        raise RpcError("deployment not loaded")
    return d["deployer"].lower()


# ----- decimal <-> wei conversion -------------------------------------------


def to_wei(amount_str: str, decimals: int) -> int:
    d = Decimal(str(amount_str))
    if d < 0:
        raise ValueError("negative amount")
    scaled = d * (Decimal(10) ** decimals)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"amount has more than {decimals} decimals")
    return int(scaled)


def from_wei(wei: int, decimals: int) -> str:
    s = f"{wei:0{decimals + 1}d}"
    if decimals == 0:
        return s
    whole, frac = s[:-decimals], s[-decimals:]
    frac = frac.rstrip("0")
    return whole if not frac else f"{whole}.{frac}"


# ----- SQLite ---------------------------------------------------------------

_db_lock = threading.Lock()


def db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(CHAIN_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(CHAIN_DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    with _db_lock, db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS addr_map (
          opex_user TEXT PRIMARY KEY,
          address TEXT NOT NULL,
          privkey TEXT NOT NULL,
          created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS deposits (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tx TEXT UNIQUE NOT NULL,
          opex_user TEXT NOT NULL,
          asset TEXT NOT NULL,
          amount TEXT NOT NULL,
          block INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          internal_credits INTEGER NOT NULL DEFAULT 0,
          credited_at INTEGER,
          observed_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS withdraws (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tx TEXT UNIQUE,
          opex_user TEXT NOT NULL,
          asset TEXT NOT NULL,
          amount TEXT NOT NULL,
          destination TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'submitted',
          submitted_at INTEGER NOT NULL,
          confirmed_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS scan_state (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          last_block INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO scan_state(id, last_block) VALUES (1, 0);
        CREATE INDEX IF NOT EXISTS idx_deposits_user ON deposits(opex_user);
        CREATE INDEX IF NOT EXISTS idx_withdraws_user ON withdraws(opex_user);
        """)
        # Idempotent column additions for the AML decision audit trail.
        # SQLite's ALTER TABLE has no IF NOT EXISTS, so we probe.
        _maybe_add_column(c, "deposits", "aml_status", "TEXT")
        _maybe_add_column(c, "deposits", "aml_decision_json", "TEXT")
        _maybe_add_column(c, "withdraws", "aml_status", "TEXT")
        _maybe_add_column(c, "withdraws", "aml_decision_json", "TEXT")
        # Mark pre-existing rows as 'legacy' so the UI can distinguish them
        # from rows screened under the new policy.
        c.execute("UPDATE deposits SET aml_status='legacy' WHERE aml_status IS NULL")
        c.execute("UPDATE withdraws SET aml_status='legacy' WHERE aml_status IS NULL")


def _maybe_add_column(conn, table: str, col: str, decl: str) -> None:
    """ALTER TABLE ... ADD COLUMN if the column isn't there yet. SQLite-specific."""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


# ----- auth (delegate to auth_server) ---------------------------------------


_auth_cache: dict[str, tuple[float, dict]] = {}
_auth_cache_lock = threading.Lock()
AUTH_TTL = 30

# ----- airdrop-to rate limiter ----------------------------------------------
#
# Per-user, per-token sliding-window limiter for /chain/airdrop-to. We keep a
# small in-memory dict keyed by (opex_user, token_address); the value is a
# list of unix-timestamp floats. Old entries (>60s) are dropped on each call.
# A janitor thread sweeps the whole dict every 5 minutes so abandoned users
# don't leak memory.
AIRDROP_TO_LIMIT = 5  # max calls
AIRDROP_TO_WINDOW = 60.0  # per this many seconds
AIRDROP_TO_SWEEP = 300.0  # sweep every 5 minutes

_airdrop_to_log: dict[tuple[str, str], list[float]] = {}
_airdrop_to_lock = threading.Lock()


def _airdrop_to_check(opex_user: str, token_addr: str) -> tuple[bool, int]:
    """Returns (allowed, retry_after_seconds). Records a hit on success."""
    now = time.time()
    key = (opex_user, token_addr.lower())
    with _airdrop_to_lock:
        bucket = _airdrop_to_log.get(key, [])
        # drop expired
        cutoff = now - AIRDROP_TO_WINDOW
        bucket = [t for t in bucket if t > cutoff]
        if len(bucket) >= AIRDROP_TO_LIMIT:
            oldest = bucket[0]
            retry = max(1, int(oldest + AIRDROP_TO_WINDOW - now) + 1)
            _airdrop_to_log[key] = bucket
            return False, retry
        bucket.append(now)
        _airdrop_to_log[key] = bucket
        return True, 0


def _airdrop_to_sweep_loop():
    while True:
        time.sleep(AIRDROP_TO_SWEEP)
        try:
            now = time.time()
            cutoff = now - AIRDROP_TO_WINDOW
            with _airdrop_to_lock:
                stale = []
                for k, v in _airdrop_to_log.items():
                    fresh = [t for t in v if t > cutoff]
                    if fresh:
                        _airdrop_to_log[k] = fresh
                    else:
                        stale.append(k)
                for k in stale:
                    _airdrop_to_log.pop(k, None)
        except Exception:
            log.exception("airdrop-to sweep failed")


def resolve_user_from_token(token: str, *, force: bool = False) -> dict | None:
    """Validate the bearer token against the auth server.
    Returns the `user` object on success, None on failure.

    If `force=True`, skip the local cache and always go upstream — used by the
    KYC-gated /chain/withdraw so a freshly verified user doesn't have to wait
    out the 30-second TTL.
    """
    if not token:
        return None
    now = time.time()
    if not force:
        with _auth_cache_lock:
            cached = _auth_cache.get(token)
            if cached and now - cached[0] < AUTH_TTL:
                # Cached "verified" is fine. But if the cache says non-verified
                # we always re-fetch — saves an annoying 30s "why is my KYC
                # not detected" demo flow.
                if cached[1].get("kyc_status") == "verified":
                    return cached[1]
    req = _http_request(
        f"{AUTH_BASE}/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with _http_urlopen(req, timeout=5) as resp:
            obj = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        log.warning("auth/me %s -> %s", token[:8], e.code)
        return None
    except Exception as e:
        log.warning("auth/me transport error: %s", e)
        return None
    user = obj.get("user")
    if not user or not user.get("opex_user"):
        return None
    with _auth_cache_lock:
        _auth_cache[token] = (now, user)
    return user


# ----- per-user wallet derivation -------------------------------------------


def derive_user_wallet(opex_user: str) -> tuple[str, str]:
    """Returns (address, privkey). Cached in chain.db."""
    with _db_lock, db() as c:
        row = c.execute(
            "SELECT address, privkey FROM addr_map WHERE opex_user=?",
            (opex_user,),
        ).fetchone()
        if row:
            return row["address"], row["privkey"]
    # Shell out to derive.js. It just hashes opex_user → 32-byte key, so this
    # is fully deterministic and we don't need the hardhat node running.
    node_bin = shutil.which("node")
    if not node_bin:
        raise RpcError("node executable not found on PATH")
    p = subprocess.run(  # noqa: S603 - argv is fixed and shell=False.
        [node_bin, DERIVE_SCRIPT, opex_user],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if p.returncode != 0:
        raise RpcError(f"derive.js failed: {p.stderr.strip() or p.stdout.strip()}")
    out = json.loads(p.stdout.strip())
    address = out["address"].lower()
    privkey = out["privateKey"]
    with _db_lock, db() as c:
        c.execute(
            "INSERT OR REPLACE INTO addr_map(opex_user, address, privkey, created_at) VALUES (?,?,?,?)",
            (opex_user, address, privkey, int(time.time())),
        )
    return address, privkey


# ----- token RPC helpers ----------------------------------------------------


def erc20_balance(token: str, addr: str) -> int:
    data = SEL_BALANCE_OF + encode_address(addr)
    r = rpc_call("eth_call", [{"to": token, "data": data}, "latest"])
    return decode_uint256(r)


def erc20_balance_for_chain(chain: ChainConfig, token: str, addr: str) -> int:
    data = SEL_BALANCE_OF + encode_address(addr)
    r = rpc_call_for_chain(chain, "eth_call", [{"to": token, "data": data}, "latest"])
    return decode_uint256(r)


def native_balance_for_chain(chain: ChainConfig, addr: str) -> int:
    r = rpc_call_for_chain(chain, "eth_getBalance", [addr_to_hex(addr), "latest"])
    return decode_uint256(r) if isinstance(r, str) else int(r or 0)


# ---- Probe loop ------------------------------------------------------------


def probe_chain_once(chain: ChainConfig) -> None:
    try:
        rpc_call_for_chain(chain, "eth_blockNumber", timeout=CHAIN_PROBE_TIMEOUT)
    except RpcError as e:
        log.info("probe %s degraded: %s", chain.slug, e)


def chain_probe_loop():
    """Background re-probe of every registered chain every CHAIN_PROBE_INTERVAL_SECONDS.

    On startup we eagerly probe all chains once so /chain/info has fresh data
    on the very first request. After that we sleep between cycles.
    """
    log.info("chain probe loop started for %d chains", len(CHAINS))
    # Eager first pass.
    for c in CHAINS:
        try:
            probe_chain_once(c)
        except Exception:
            log.exception("initial probe %s failed", c.slug)
    while True:
        time.sleep(CHAIN_PROBE_INTERVAL_SECONDS)
        for c in CHAINS:
            try:
                probe_chain_once(c)
            except Exception:
                log.exception("probe %s tick failed", c.slug)


def chain_state_snapshot(chain: ChainConfig) -> dict:
    """JSON-friendly snapshot of a single chain's runtime status."""
    with _chain_state_lock:
        st = _chain_state.get(chain.slug)
        st_block = st.block_number if st else None
        st_active = st.active_rpc if st else None
        st_status = st.rpc_status if st else "unknown"
        st_last_ok = st.last_ok_at if st else None
        st_latency = st.latency_ms if st else None
    stale_seconds: int | None = None
    if st_last_ok is not None:
        stale_seconds = int(time.time() - st_last_ok)
    # If we've never had a successful probe AND status is degraded, the cache
    # is genuinely empty. We still return what we have so the UI can render
    # gracefully ("amber pill, no block").
    return {
        "rpc_status": st_status,
        "active_rpc": st_active,
        "block_number": st_block,
        "stale_seconds": stale_seconds,
        "latency_ms": st_latency,
    }


def send_transfer(token: str, *, sender: str, to: str, value: int) -> str:
    """Submit an ERC20 Transfer(to, value) tx FROM the custodial address.

    Two backends are supported:

      1. Default: `eth_sendTransaction` with `from=sender`. Hardhat unlocks
         dev account[0] so this works without a private key on this side.

      2. Threshold custody: when CUSTODY_COORDINATOR_URL is set, build the
         tx data + chain id locally and POST it to the custody coordinator,
         which collects an M-of-N quorum of Shamir shares, reconstructs the
         secp256k1 key in volatile memory, signs the tx, broadcasts it, and
         zeroes the key. The coordinator's response carries the tx hash we
         return here. See tools/custody/coordinator.py for full details.
    """
    data = SEL_TRANSFER + encode_address(to) + encode_uint256(value)
    if custody_enabled():
        # `data` already starts with "0x" because fn_selector emits it; pass
        # through as-is. encode_address / encode_uint256 are bare hex, no
        # prefix, so the concatenation is well-formed.
        return _custody_sign_and_broadcast(
            to=token,
            value=0,
            data=data,
            gas_hex="0xf4240",
        )
    return rpc_call(
        "eth_sendTransaction",
        [
            {
                "from": sender,
                "to": token,
                "data": data,
                "gas": "0xf4240",  # 1,000,000
            }
        ],
    )


# ----- threshold-custody helpers -----------------------------------------

_CUSTODY_HEALTH_LOCK = threading.Lock()
_CUSTODY_HEALTH_CACHE: dict | None = None
_CUSTODY_HEALTH_AT: float = 0.0


def _custody_health_snapshot(*, max_age_s: float = 5.0) -> dict | None:
    """Cached health probe of the custody coordinator (called from /info).

    Cached for max_age_s to avoid hammering the coordinator on every
    page load. Returns None if no coordinator is configured.
    """
    global _CUSTODY_HEALTH_CACHE, _CUSTODY_HEALTH_AT
    if not custody_enabled():
        return None
    with _CUSTODY_HEALTH_LOCK:
        if _CUSTODY_HEALTH_CACHE is not None and (time.time() - _CUSTODY_HEALTH_AT) < max_age_s:
            return _CUSTODY_HEALTH_CACHE
    try:
        with _http_urlopen(
            CUSTODY_COORDINATOR_URL.rstrip("/") + "/health",
            timeout=2.0,
        ) as resp:
            j = json.loads(resp.read().decode())
    except Exception as e:
        j = {"ok": False, "error": str(e)[:120]}
    with _CUSTODY_HEALTH_LOCK:
        _CUSTODY_HEALTH_CACHE = j
        _CUSTODY_HEALTH_AT = time.time()
    return j


def _custody_sign_and_broadcast(*, to: str, value: int, data: str, gas_hex: str) -> str:
    """POST to the coordinator's /sign-and-broadcast and return the tx hash.

    Raises RpcError on coordinator failure so callers can render a sensible
    502 to the UI (matching how raw `eth_sendTransaction` failures surface).
    """
    cust = custodial_address()
    deployment = load_deployment()
    chain_id = int(deployment["chainId"]) if deployment else 31337
    payload = {
        "rpc_url": DEFAULT_RPC,
        "from": cust,
        "to": to,
        "value_hex": hex(value),
        "data_hex": data,
        "gas_hex": gas_hex,
        "chain_id": chain_id,
    }
    body = json.dumps(payload).encode()
    req = _http_request(
        CUSTODY_COORDINATOR_URL.rstrip("/") + "/sign-and-broadcast",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {CUSTODY_COORDINATOR_TOKEN}",
        },
    )
    try:
        with _http_urlopen(req, timeout=20) as resp:
            j = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode())
        except Exception:
            err = {"raw": "<unreadable>"}
        raise RpcError(f"custody coordinator {e.code}: {err}") from e
    except Exception as e:
        raise RpcError(f"custody coordinator transport error: {e}") from e
    tx = j.get("tx")
    if not tx:
        raise RpcError(f"custody coordinator returned no tx: {j}")
    log.info(
        "custody-signed tx=%s shares=%s addr_check=%s",
        tx,
        j.get("shares_collected"),
        j.get("signer_addr_check"),
    )
    return tx


def send_mint(token: str, *, deployer: str, to: str, value: int) -> str:
    data = SEL_MINT + encode_address(to) + encode_uint256(value)
    return rpc_call(
        "eth_sendTransaction",
        [
            {
                "from": deployer,
                "to": token,
                "data": data,
                "gas": "0xf4240",
            }
        ],
    )


# Hardhat ships dev accounts unlocked by default, but only the first 20.
# A user-derived address won't be unlocked, so the chain bridge needs to send
# raw signed transactions for "send-from-user-wallet". We import the priv key
# into the node via `hardhat_impersonateAccount` (works) — actually hardhat
# only supports impersonation of any address with the configured automine.
# Easier path: import the priv key into the node as an unlocked account using
# `hardhat_setBalance` to fund it then send transactions via `eth_sendTransaction`
# from the impersonated account. Let's try the impersonate approach.


def impersonate_and_fund(addr: str) -> None:
    rpc_call("hardhat_impersonateAccount", [addr])
    # Make sure it has gas. 100 ETH = 0x56bc75e2d63100000.
    rpc_call("hardhat_setBalance", [addr, "0x56bc75e2d63100000"])


# ----- internal credit (wallet API) -----------------------------------------


def post_internal_deposit(opex_user: str, internal_symbol: str, amount: int, *, ref: str) -> bool:
    """Posts a single (capped) deposit to the wallet API. Returns True on success."""
    if amount <= 0:
        return True
    path = (
        f"/deposit/{amount}_test-ethereum_{internal_symbol}/{urllib.parse.quote(opex_user)}_MAIN"
        f"?description=zkcex-chain-deposit&transferRef={urllib.parse.quote(ref)}"
    )
    url = WALLET_BASE + path
    req = _http_request(url, method="POST")
    try:
        with _http_urlopen(req, timeout=10) as resp:
            return resp.status < 300
    except urllib.error.HTTPError as e:
        log.warning("internal deposit %s -> %s: %s", url, e.code, e.read()[:200])
        return False
    except Exception as e:
        log.warning("internal deposit %s -> transport error %s", url, e)
        return False


def credit_internal(
    opex_user: str, asset: str, amount_wei: int, decimals: int, tx_hash: str
) -> int:
    """Convert wei amount → integer units, then chunk by INTERNAL_DEPOSIT_CAP.
    Returns the count of successful internal deposit calls.

    The wallet API's /deposit/{amount}_... endpoint expects an INTEGER amount of
    units (whole tokens). The fractional part of a chain transfer is dropped on
    the internal ledger but kept on chain (so users see it in their on-chain
    balance refresh). Good enough for a demo; real CEXes do the same with a
    dust threshold.
    """
    internal_symbol = ASSET_TO_INTERNAL_SYMBOL.get(asset)
    if not internal_symbol:
        return 0
    units_total = amount_wei // (10**decimals)  # integer floor
    if units_total <= 0:
        return 0
    credits = 0
    remaining = units_total
    chunk_idx = 0
    while remaining > 0:
        amt = min(INTERNAL_DEPOSIT_CAP, remaining)
        ref = f"{tx_hash}-{chunk_idx}"
        if post_internal_deposit(opex_user, internal_symbol, amt, ref=ref):
            credits += 1
            remaining -= amt
        else:
            log.warning(
                "internal credit failed for %s tx=%s remain=%s", opex_user, tx_hash, remaining
            )
            break
        chunk_idx += 1
    return credits


# ----- deposit indexer ------------------------------------------------------


def get_block_number() -> int:
    h = rpc_call("eth_blockNumber")
    return int(h, 16)


def fetch_logs(
    from_block: int, to_block: int, *, addresses: list[str], topics: list[str | list[str] | None]
) -> list[dict]:
    return rpc_call(
        "eth_getLogs",
        [
            {
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "address": addresses,
                "topics": topics,
            }
        ],
    )


def reverse_addr_lookup(address: str) -> str | None:
    address = address.lower()
    with _db_lock, db() as c:
        row = c.execute("SELECT opex_user FROM addr_map WHERE address=?", (address,)).fetchone()
        return row["opex_user"] if row else None


def scan_once() -> int:
    """Scan new blocks for Transfer(_, custodial, _) events and credit."""
    deployment = load_deployment()
    if not deployment:
        return 0
    custodial = custodial_address()
    token_addrs_map = deployment["tokens"]  # symbol -> address
    addr_to_symbol = {v.lower(): k for k, v in token_addrs_map.items()}
    addresses = list(token_addrs_map.values())

    try:
        latest = get_block_number()
    except RpcError:
        return 0

    with _db_lock, db() as c:
        last = c.execute("SELECT last_block FROM scan_state WHERE id=1").fetchone()["last_block"]
    if latest <= last:
        return latest

    from_block = last + 1
    to_block = min(latest, from_block + 500)

    # Filter by topic[2] = custodial (the `to` indexed param).
    custodial_topic = "0x" + ("0" * 24) + custodial[2:].lower()
    try:
        logs = fetch_logs(
            from_block,
            to_block,
            addresses=addresses,
            topics=[TOPIC_TRANSFER, None, custodial_topic],
        )
    except RpcError as e:
        log.warning("eth_getLogs failed: %s", e)
        return latest

    for lg in logs:
        token_addr = lg["address"].lower()
        symbol = addr_to_symbol.get(token_addr)
        if not symbol:
            continue
        topics = lg.get("topics", [])
        if len(topics) < 3:
            continue
        from_addr = decode_address(topics[1])
        to_addr = decode_address(topics[2])
        if to_addr.lower() != custodial:
            continue
        # Don't credit ourselves (custodial -> custodial is a withdraw refund).
        if from_addr.lower() == custodial:
            continue
        value = decode_uint256(lg.get("data", "0x"))
        tx_hash = lg["transactionHash"]
        block_num = int(lg["blockNumber"], 16)

        opex_user = reverse_addr_lookup(from_addr)
        if not opex_user:
            log.info("ignoring deposit from unmapped address %s tx=%s", from_addr, tx_hash)
            continue

        # idempotency: skip if we've already recorded this tx
        with _db_lock, db() as c:
            row = c.execute("SELECT id, status FROM deposits WHERE tx=?", (tx_hash,)).fetchone()
            if row:
                continue

        decimals = token_decimals(symbol)
        amount_str = from_wei(value, decimals)
        observed_at = int(time.time())

        # ---- AML screening on the originating address ------------------
        # Done before crediting. BLOCK -> never credit, mark aml_blocked.
        # REVIEW -> hold pending operator action, mark aml_review.
        # ALLOW -> proceed with normal crediting; still persist the decision.
        decision = aml_provider.screen_address(
            chain="hardhat-localhost",
            address=from_addr,
            customer_id=opex_user,
            kyc_level=None,
        )
        aml_status_lc = decision.decision.lower()  # 'allow'|'review'|'block'
        aml_json = json.dumps(decision.to_json())

        if decision.decision == "BLOCK":
            row_status = "aml_blocked"
        elif decision.decision == "REVIEW":
            row_status = "aml_review"
        else:
            row_status = "pending"

        with _db_lock, db() as c:
            c.execute(
                "INSERT OR IGNORE INTO deposits(tx, opex_user, asset, amount, block, status, observed_at, aml_status, aml_decision_json)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    tx_hash,
                    opex_user,
                    symbol,
                    amount_str,
                    block_num,
                    row_status,
                    observed_at,
                    aml_status_lc,
                    aml_json,
                ),
            )
        log.info(
            "deposit observed tx=%s user=%s asset=%s amount=%s aml=%s score=%s source=%s",
            tx_hash,
            opex_user,
            symbol,
            amount_str,
            decision.decision,
            decision.risk_score,
            decision.source,
        )

        if decision.decision != "ALLOW":
            log.warning(
                "deposit NOT credited (aml=%s) tx=%s reasons=%s",
                decision.decision,
                tx_hash,
                decision.reasons,
            )
            continue

        # credit internal
        credits = credit_internal(opex_user, symbol, value, decimals, tx_hash)
        new_status = "credited" if credits > 0 else "pending"
        with _db_lock, db() as c:
            c.execute(
                "UPDATE deposits SET status=?, internal_credits=?, credited_at=? WHERE tx=?",
                (new_status, credits, int(time.time()) if credits > 0 else None, tx_hash),
            )
        # Push-notify on successful credit. Best-effort.
        if credits > 0:
            _push_notify(
                opex_user,
                {
                    "title": "입금이 완료되었습니다 / Deposit credited",
                    "body": f"{amount_str} {symbol} 입금이 계정에 반영되었습니다.",
                    "tag": "deposit",
                    "data": {
                        "url": "/app/wallet.html",
                        "tx": tx_hash,
                        "asset": symbol,
                        "amount": amount_str,
                    },
                },
            )
            _notif_send(
                opex_user,
                "deposit_credited",
                "financial",
                "입금이 완료되었습니다 / Deposit credited",
                f"{amount_str} {symbol} has been credited to your account.",
                {"asset": symbol, "amount": amount_str, "tx": tx_hash},
            )

    with _db_lock, db() as c:
        c.execute("UPDATE scan_state SET last_block=? WHERE id=1", (to_block,))
    return latest


def confirm_withdraws_once() -> None:
    """Promote `submitted` withdraws to `confirmed` once their tx is mined."""
    with _db_lock, db() as c:
        rows = c.execute(
            "SELECT id, tx, opex_user, asset, amount, destination"
            " FROM withdraws WHERE status='submitted' AND tx IS NOT NULL"
        ).fetchall()
    for row in rows:
        tx = row["tx"]
        try:
            receipt = rpc_call("eth_getTransactionReceipt", [tx])
        except RpcError:
            continue
        if not receipt:
            continue
        status_word = "confirmed" if receipt.get("status") in ("0x1", 1) else "failed"
        with _db_lock, db() as c:
            c.execute(
                "UPDATE withdraws SET status=?, confirmed_at=? WHERE id=?",
                (status_word, int(time.time()), row["id"]),
            )
        if status_word == "confirmed":
            _notif_send(
                row["opex_user"],
                "withdraw_confirmed",
                "financial",
                "Withdrawal confirmed",
                f"{row['amount']} {row['asset']} withdrawal confirmed on-chain.",
                {
                    "asset": row["asset"],
                    "amount": str(row["amount"]),
                    "destination": row["destination"],
                    "tx": tx,
                },
            )


def indexer_loop():
    log.info("indexer loop started")
    while True:
        try:
            scan_once()
            confirm_withdraws_once()
        except Exception:
            log.exception("indexer tick failed")
        time.sleep(POLL_INTERVAL_SECONDS)


# ----- HTTP handler ---------------------------------------------------------


def _json(handler: http.server.BaseHTTPRequestHandler, status: int, body: dict | list) -> None:
    payload = json.dumps(body, default=str).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _read_json_body(handler: http.server.BaseHTTPRequestHandler) -> dict:
    cl = int(handler.headers.get("Content-Length") or 0)
    if cl <= 0:
        return {}
    raw = handler.rfile.read(cl)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode())
    except Exception:
        return {}


def _bearer(handler) -> str | None:
    h = handler.headers.get("Authorization") or ""
    if h.lower().startswith("bearer "):
        parts = h.split(None, 1)
        if len(parts) < 2:
            return None
        tok = parts[1].strip()
        return tok or None
    return None


def _verify_2fa_for_withdraw(opex_user: str, body: dict) -> tuple[bool, str]:
    """Forward a withdraw-step 2FA check to auth_server (loopback).

    Returns ``(ok, reason)``. ``reason`` is ``totp_required`` when the user
    is enrolled but no code was supplied, ``totp_wrong`` on a bad code, and
    ``""`` on success or when the user is not enrolled.
    """
    code = (body.get("totp_code") or body.get("code") or "").strip()
    rc = (body.get("recovery_code") or "").strip()
    payload = {"opex_user": opex_user, "code": code, "recovery_code": rc}
    resp: dict = {}
    try:
        req = _http_request(
            f"{AUTH_BASE}/auth/2fa/verify-withdraw",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with _http_urlopen(req, timeout=5) as r:
            resp = json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            resp = json.loads(e.read().decode() or "{}")
        except Exception:
            resp = {}
    except Exception:
        # Auth server unreachable -- fail open (consistent with the geo
        # enforce behaviour above); we don't want a 2FA-server outage to
        # cascade into a withdrawals outage. If you'd rather fail closed
        # for production, change REQUIRE_2FA_WITHDRAW handling here.
        return True, ""
    if resp.get("ok"):
        return True, ""
    if not resp.get("enrolled"):
        return True, ""
    return False, resp.get("error") or "totp_wrong"


def _require_user(handler) -> dict | None:
    tok = _bearer(handler)
    if not tok:
        _json(handler, 401, {"error": "missing_token"})
        return None
    user = resolve_user_from_token(tok)
    if not user:
        _json(handler, 401, {"error": "invalid_token"})
        return None
    return user


def _require_deployment(handler) -> dict | None:
    d = load_deployment()
    if not d:
        _json(
            handler,
            503,
            {
                "error": "chain_unavailable",
                "message": "hardhat node + deployment not ready; run tools/hardhat-sim/run.sh",
            },
        )
        return None
    return d


def _client_ip_from_handler(handler) -> str:
    remote = ""
    try:
        remote = handler.client_address[0] if handler.client_address else ""
    except Exception:  # noqa: BLE001
        remote = ""
    return geo_provider.resolve_client_ip(
        remote_addr=remote,
        x_forwarded_for=handler.headers.get("X-Forwarded-For"),
        x_real_ip=handler.headers.get("X-Real-IP"),
    )


def _geo_enforce(handler, *, endpoint: str) -> bool:
    """Forward the resolved client IP to auth_server's /auth/geo/check.

    Centralizing the verdict on auth_server keeps the audit trail in one
    place (auth.db.geo_decisions) and ensures the policy is consistent
    across services. Fail-open on auth_server outage: if we can't reach
    /auth/geo/check, we don't block the withdrawal -- the AML provider is
    the layered defense for the on-chain hop, and a hard fail here would
    let an auth outage cascade into withdrawal outages.

    Returns True if blocked (and the 451 response has been written).
    """
    if not geo_provider.is_enforcement_enabled():
        return False
    ip = _client_ip_from_handler(handler)
    # Local pre-check first so we don't even need to hit auth_server for
    # the simple cases (loopback, blocked CSV match). The auth_server hop
    # is mostly there so it can persist the audit row.
    info = geo_provider.lookup_country(ip)
    country = info.country_iso2 if info else None
    blocked, reason = geo_provider.is_blocked(country)
    # Best-effort: ask auth_server to log the decision. If auth is down
    # we still enforce locally.
    try:
        req = _http_request(
            f"{AUTH_BASE}/auth/geo/check",
            method="GET",
            headers={"X-Forwarded-For": ip},
        )
        with _http_urlopen(req, timeout=2) as r:
            payload = json.loads(r.read().decode())
            # auth_server is the canonical decision-maker: trust its verdict
            # over the local pre-check when reachable.
            blocked = bool(payload.get("blocked"))
            reason = payload.get("reason") or reason
            country = payload.get("country") or country
            info_name = payload.get("country_name") or (info.country_name if info else None)
    except Exception:  # noqa: BLE001
        info_name = info.country_name if info else None
    else:
        pass
    if blocked:
        _json(
            handler,
            451,
            {
                "error": "geo_blocked",
                "country": country,
                "country_name": info_name,
                "reason": reason,
                "message": (
                    f"Withdrawals are not available from {info_name or country}. "
                    "Please contact support if you believe this is an error."
                ),
            },
        )
        return True
    return False


# ----- handler dispatchers --------------------------------------------------


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "zkCEX-chain/0.1"

    # silence default access logging — we'll use our own
    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")

    def do_GET(self):
        with _otel_server_span(self):
            try:
                base = self.path.split("?", 1)[0]
                if base in ("/chain/info", "/info"):
                    return self._info()
                if base in ("/chain/wallet", "/wallet"):
                    return self._wallet()
                if base in ("/chain/deposits", "/deposits"):
                    return self._deposits()
                if base in ("/chain/withdraws", "/withdraws"):
                    return self._withdraws()
                if base in ("/chain/health", "/health"):
                    return self._chain_health()
                if base in ("/chain/custody", "/custody"):
                    return self._custody()
                return _json(self, 404, {"error": "not_found", "path": self.path})
            except Exception:
                log.exception("GET %s failed", self.path)
                return _json(
                    self, 500, {"error": "internal", "trace": traceback.format_exc(limit=3)}
                )

    def do_POST(self):
        with _otel_server_span(self):
            try:
                base = self.path.split("?", 1)[0]
                if base in ("/chain/airdrop", "/airdrop"):
                    return self._airdrop()
                if base in ("/chain/airdrop-to", "/airdrop-to"):
                    return self._airdrop_to()
                if base in ("/chain/send-from-user-wallet", "/send-from-user-wallet"):
                    return self._send_from_user()
                if base in ("/chain/deposit-detect", "/deposit-detect"):
                    return self._deposit_detect()
                if base in ("/chain/withdraw", "/withdraw"):
                    return self._withdraw()
                if base.startswith("/chain/withdraw/release/") or base.startswith(
                    "/chain/withdraw/cancel/"
                ):
                    return self._withdraw_review(base.split("/")[3], base.rsplit("/", 1)[1])
                return _json(self, 404, {"error": "not_found", "path": self.path})
            except Exception:
                log.exception("POST %s failed", self.path)
                return _json(
                    self, 500, {"error": "internal", "trace": traceback.format_exc(limit=3)}
                )

    # ---------------- helpers ----------------

    def _query_param(self, key: str) -> str | None:
        if "?" not in self.path:
            return None
        qs = urllib.parse.parse_qs(self.path.split("?", 1)[1])
        v = qs.get(key)
        return v[0] if v else None

    def _resolve_chain_param(
        self, body: dict | None = None
    ) -> tuple[ChainConfig | None, str | None]:
        """Read the requested chain slug from query string or body.

        Returns (ChainConfig, error_slug). On unknown slug returns (None, slug).
        On no slug at all, returns (DEFAULT_CHAIN's config, None).
        """
        slug = self._query_param("chain")
        if not slug and body:
            slug = body.get("chain")
        if not slug:
            return chain_by_slug(DEFAULT_CHAIN), None
        c = chain_by_slug(slug)
        if not c:
            return None, slug
        return c, None

    def _block_chain_not_writable(self, chain: ChainConfig) -> bool:
        """Send a 503 if the chain is not the writable demo chain. Returns True
        if the response was sent, so callers can early-return.
        """
        if chain.is_demo:
            return False
        _json(
            self,
            503,
            {
                "error": "chain_not_writable",
                "chain": chain.slug,
                "reason": CHAIN_NOT_WRITABLE_MSG,
            },
        )
        return True

    # ---------------- endpoint impls ----------------

    def _info(self):
        """Return the chain registry plus runtime status for each.

        The payload preserves the legacy hardhat-only fields at the top level
        (`ready`, `chainId`, `rpc`, `blockNumber`, `custodial`, `tokens`,
        `decimals`) so existing UI code that only knows the demo chain keeps
        working unchanged. The new `default_chain` + `chains` arrays carry the
        full multi-chain view.
        """
        d = load_deployment()
        chains_out: list[dict] = []
        for c in CHAINS:
            snap = chain_state_snapshot(c)
            tokens: dict[str, str] = {}
            decimals: dict[str, int] = {}
            custodial = None
            if c.slug == "hardhat" and d:
                tokens = dict(d.get("tokens", {}))
                decimals = dict(d.get("decimals", {}))
                custodial = d.get("custodial")
            else:
                tokens = dict(c.erc20s)
            chains_out.append(
                {
                    "chain_id": c.chain_id,
                    "slug": c.slug,
                    "name": c.name,
                    "native": c.native_symbol,
                    "writable": c.is_demo,
                    "is_demo": c.is_demo,
                    "rpc_status": snap["rpc_status"],
                    "active_rpc": snap["active_rpc"],
                    "block_number": snap["block_number"],
                    "stale_seconds": snap["stale_seconds"],
                    "latency_ms": snap["latency_ms"],
                    "custodial": custodial,
                    "tokens": tokens,
                    "decimals": decimals,
                    "explorer_tx_url": c.explorer_tx_url,
                    "faucets": list(c.faucets),
                }
            )

        # Legacy hardhat top-level fields (kept verbatim for back-compat).
        legacy: dict = {}
        if not d:
            legacy = {
                "ready": False,
                "message": "hardhat not deployed yet — run tools/hardhat-sim/run.sh",
            }
        else:
            try:
                block = get_block_number()
                legacy = {
                    "ready": True,
                    "chainId": d["chainId"],
                    "rpc": d["rpc"],
                    "blockNumber": block,
                    "custodial": d["custodial"],
                    "tokens": d["tokens"],
                    "decimals": d.get("decimals", {}),
                }
            except RpcError:
                legacy = {
                    "ready": False,
                    "chainId": d["chainId"],
                    "rpc": d["rpc"],
                    "custodial": d["custodial"],
                    "tokens": d["tokens"],
                    "decimals": d.get("decimals", {}),
                    "message": "hardhat unreachable",
                }

        out = {
            "default_chain": DEFAULT_CHAIN,
            "chains": chains_out,
        }
        out.update(legacy)

        # Custody mode summary so the UI can render a "single-key (demo) vs
        # threshold custody" badge above the withdraw form.
        if custody_enabled():
            health = _custody_health_snapshot() or {}
            out["custody"] = {
                "mode": "threshold",
                "threshold": health.get("threshold"),
                "total": health.get("total"),
                "m_of_n": health.get("m_of_n"),
                "coordinator_url": CUSTODY_COORDINATOR_URL,
                "coordinator_health": {
                    "ok": bool(health.get("ok")),
                    "n_nodes_reachable": health.get("n_nodes_reachable"),
                    "custodial_addr": health.get("custodial_addr"),
                    "nodes": health.get("nodes"),
                    "error": health.get("error"),
                },
            }
        else:
            out["custody"] = {
                "mode": "single-key",
                "threshold": 1,
                "total": 1,
                "m_of_n": "1-of-1",
                "note": (
                    "Demo mode. Set CUSTODY_COORDINATOR_URL to route "
                    "withdrawals through the M-of-N threshold custody fleet."
                ),
            }
        return _json(self, 200, out)

    def _custody(self):
        """Public dashboard data for /app/custody.html.

        Returns the coordinator's view of node health plus a redacted slice
        of the audit log. Always returns 200; if custody isn't configured
        we surface mode='single-key' so the page can render an honest
        "demo mode" banner instead of an error.
        """
        if not custody_enabled():
            return _json(
                self,
                200,
                {
                    "mode": "single-key",
                    "note": (
                        "Threshold custody not configured. Set "
                        "CUSTODY_COORDINATOR_URL and CUSTODY_COORDINATOR_TOKEN."
                    ),
                },
            )
        # Fan-out two short coordinator calls; do not require both.
        coord = CUSTODY_COORDINATOR_URL.rstrip("/")
        health: dict = {}
        try:
            with _http_urlopen(coord + "/health", timeout=2.0) as resp:
                health = json.loads(resp.read().decode())
        except Exception as e:
            health = {"ok": False, "error": str(e)[:120]}
        audit: list = []
        try:
            with _http_urlopen(coord + "/audit-public", timeout=2.0) as resp:
                audit = (json.loads(resp.read().decode()) or {}).get("rows", [])
        except Exception as e:  # noqa: BLE001
            log.debug("custody audit-public fetch failed: %s", e)
            audit = []
        return _json(
            self,
            200,
            {
                "mode": "threshold",
                "coordinator_url": coord,
                "health": health,
                "audit": audit,
            },
        )

    def _chain_health(self):
        """Public health summary for every registered chain."""
        out_chains = []
        for c in CHAINS:
            snap = chain_state_snapshot(c)
            ok = snap["rpc_status"] == "ok"
            out_chains.append(
                {
                    "slug": c.slug,
                    "name": c.name,
                    "ok": ok,
                    "rpc_status": snap["rpc_status"],
                    "active_rpc": snap["active_rpc"],
                    "latency_ms": snap["latency_ms"],
                    "block_number": snap["block_number"],
                    "stale_seconds": snap["stale_seconds"],
                }
            )
        return _json(self, 200, {"chains": out_chains})

    def _wallet(self):
        user = _require_user(self)
        if not user:
            return
        chain, bad = self._resolve_chain_param()
        if bad is not None:
            return _json(self, 400, {"error": "unknown_chain", "chain": bad})
        opex = user["opex_user"]
        # User addresses are derived from opex_user across every EVM chain,
        # so the same secp256k1 key gives the same checksum-lowercased addr.
        addr, _pk = derive_user_wallet(opex)

        # Hardhat path: keep the legacy ZETH/ZUSDT shape so old callers don't
        # need to change. Requires a successful deployment.json load.
        if chain.slug == "hardhat":
            if not _require_deployment(self):
                return
            balances: dict[str, str] = {}
            for sym in ("ZETH", "ZUSDT"):
                try:
                    wei = erc20_balance(token_address(sym), addr)
                except Exception as e:
                    log.warning("balanceOf failed: %s", e)
                    wei = 0
                balances[sym] = from_wei(wei, token_decimals(sym))
            return _json(
                self,
                200,
                {
                    "address": addr,
                    "chain": chain.slug,
                    "balances": balances,
                },
            )

        # Public testnet path: read the native balance + any registered ERC20s.
        # On RPC outage we still return a 200 with empty balances and the
        # rpc_status flag flipped, so the UI can render gracefully.
        balances: dict[str, str] = {}
        rpc_status = "ok"
        try:
            wei = native_balance_for_chain(chain, addr)
            balances[chain.native_symbol] = from_wei(wei, 18)
        except RpcError as e:
            log.warning("native bal %s -> %s", chain.slug, e)
            rpc_status = "degraded"
            balances[chain.native_symbol] = "0"
        for sym, contract in chain.erc20s.items():
            try:
                wei = erc20_balance_for_chain(chain, contract, addr)
                # USDC is 6 decimals; default to 6 for known stables, 18 for the rest.
                dec = 6 if sym in ("USDC", "USDT", "DAI") else 18
                balances[sym] = from_wei(wei, dec)
            except RpcError as e:
                log.warning("erc20 bal %s/%s -> %s", chain.slug, sym, e)
                rpc_status = "degraded"
                balances[sym] = "0"
        snap = chain_state_snapshot(chain)
        return _json(
            self,
            200,
            {
                "address": addr,
                "chain": chain.slug,
                "balances": balances,
                "rpc_status": snap["rpc_status"] if rpc_status == "ok" else "degraded",
                "block_number": snap["block_number"],
                "stale_seconds": snap["stale_seconds"],
            },
        )

    def _airdrop(self):
        user = _require_user(self)
        if not user:
            return
        body = _read_json_body(self)
        chain, bad = self._resolve_chain_param(body)
        if bad is not None:
            return _json(self, 400, {"error": "unknown_chain", "chain": bad})
        if self._block_chain_not_writable(chain):
            return
        if not _require_deployment(self):
            return
        asset = (body.get("asset") or "all").upper()
        opex = user["opex_user"]
        addr, _ = derive_user_wallet(opex)
        deployer = deployer_address()

        targets = []
        if asset in ("ALL", "ZETH"):
            targets.append(("ZETH", "100"))
        if asset in ("ALL", "ZUSDT"):
            targets.append(("ZUSDT", "1000"))
        if not targets:
            return _json(self, 400, {"error": "bad_asset", "expected": ["ZETH", "ZUSDT", "all"]})

        txs = []
        for sym, amt in targets:
            value = to_wei(amt, token_decimals(sym))
            try:
                tx = send_mint(token_address(sym), deployer=deployer, to=addr, value=value)
                txs.append(tx)
            except Exception as e:
                log.warning("mint %s failed: %s", sym, e)
        return _json(self, 200, {"address": addr, "txs": txs})

    def _airdrop_to(self):
        # Mint test funds to an arbitrary address (e.g. a MetaMask account)
        # so a user can demo the in-browser-signing deposit path end-to-end.
        # Same auth gate as /chain/airdrop, plus a per-user-per-token sliding
        # window rate limiter.
        user = _require_user(self)
        if not user:
            return
        body = _read_json_body(self)
        chain, bad = self._resolve_chain_param(body)
        if bad is not None:
            return _json(self, 400, {"error": "unknown_chain", "chain": bad})
        if self._block_chain_not_writable(chain):
            return
        if not _require_deployment(self):
            return
        target = (body.get("address") or "").strip()
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", target):
            return _json(self, 400, {"error": "bad_address"})
        target_lc = target.lower()
        deployer = deployer_address()
        opex = user["opex_user"]

        targets = [("ZETH", "100"), ("ZUSDT", "1000")]
        # Rate-limit each token independently. If any token is over its limit,
        # reject the whole request (so the user doesn't get a partial mint).
        for sym, _amt in targets:
            ok, retry = _airdrop_to_check(opex, token_address(sym))
            if not ok:
                return _json(
                    self,
                    429,
                    {
                        "error": "rate_limited",
                        "asset": sym,
                        "retry_after_s": retry,
                        "limit": AIRDROP_TO_LIMIT,
                        "window_s": int(AIRDROP_TO_WINDOW),
                    },
                )

        txs = []
        for sym, amt in targets:
            value = to_wei(amt, token_decimals(sym))
            try:
                tx = send_mint(token_address(sym), deployer=deployer, to=target_lc, value=value)
                txs.append(tx)
            except Exception as e:
                log.warning("airdrop-to mint %s -> %s failed: %s", sym, target_lc, e)
        return _json(self, 200, {"address": target_lc, "txs": txs})

    def _send_from_user(self):
        # Demo helper. In production, the user signs in their own wallet
        # (e.g. MetaMask) and the chain bridge only watches the resulting tx.
        user = _require_user(self)
        if not user:
            return
        body = _read_json_body(self)
        chain, bad = self._resolve_chain_param(body)
        if bad is not None:
            return _json(self, 400, {"error": "unknown_chain", "chain": bad})
        if self._block_chain_not_writable(chain):
            return
        if not _require_deployment(self):
            return
        asset = (body.get("asset") or "").upper()
        amount = body.get("amount")
        if asset not in ("ZETH", "ZUSDT") or not amount:
            return _json(self, 400, {"error": "bad_request", "expected": "{asset,amount}"})
        opex = user["opex_user"]
        addr, _pk = derive_user_wallet(opex)
        custodial = custodial_address()
        try:
            value = to_wei(amount, token_decimals(asset))
        except Exception as e:
            return _json(self, 400, {"error": "bad_amount", "message": str(e)})
        if value <= 0:
            return _json(self, 400, {"error": "bad_amount"})

        # Make sure hardhat lets us send from the user-derived address.
        try:
            impersonate_and_fund(addr)
        except RpcError as e:
            return _json(self, 502, {"error": "impersonate_failed", "message": str(e)})

        try:
            tx = send_transfer(token_address(asset), sender=addr, to=custodial, value=value)
        except RpcError as e:
            return _json(self, 502, {"error": "transfer_failed", "message": str(e)})

        return _json(
            self, 200, {"tx": tx, "from": addr, "to": custodial, "asset": asset, "amount": amount}
        )

    def _deposit_detect(self):
        user = _require_user(self)
        if not user:
            return
        latest = scan_once()
        return _json(self, 200, {"scanned": True, "latestBlock": latest})

    def _deposits(self):
        user = _require_user(self)
        if not user:
            return
        opex = user["opex_user"]
        with _db_lock, db() as c:
            rows = c.execute(
                "SELECT tx, asset, amount, block, status, internal_credits, credited_at, observed_at,"
                "       aml_status, aml_decision_json"
                " FROM deposits WHERE opex_user=? ORDER BY id DESC LIMIT 100",
                (opex,),
            ).fetchall()
        try:
            latest = get_block_number()
        except Exception:
            latest = 0
        out = []
        for r in rows:
            confs = max(0, latest - int(r["block"]) + 1) if r["block"] else 0
            aml_score = None
            aml_reasons = []
            aml_source = None
            try:
                if r["aml_decision_json"]:
                    obj = json.loads(r["aml_decision_json"])
                    aml_score = obj.get("risk_score")
                    aml_reasons = obj.get("reasons") or []
                    aml_source = obj.get("source")
            except Exception as e:  # noqa: BLE001
                log.debug("invalid AML decision JSON on deposit %s: %s", r["tx"], e)
            out.append(
                {
                    "tx": r["tx"],
                    "asset": r["asset"],
                    "amount": r["amount"],
                    "block": r["block"],
                    "status": r["status"],
                    "confirmations": confs,
                    "internal_credits": r["internal_credits"],
                    "credited_at": r["credited_at"],
                    "observed_at": r["observed_at"],
                    "aml_status": r["aml_status"],
                    "aml_risk_score": aml_score,
                    "aml_reasons": aml_reasons,
                    "aml_source": aml_source,
                }
            )
        return _json(self, 200, out)

    def _withdraws(self):
        user = _require_user(self)
        if not user:
            return
        opex = user["opex_user"]
        with _db_lock, db() as c:
            rows = c.execute(
                "SELECT tx, asset, amount, destination, status, submitted_at, confirmed_at,"
                "       aml_status, aml_decision_json"
                " FROM withdraws WHERE opex_user=? ORDER BY id DESC LIMIT 100",
                (opex,),
            ).fetchall()
        out = []
        for r in rows:
            aml_score = None
            aml_reasons = []
            aml_source = None
            try:
                if r["aml_decision_json"]:
                    obj = json.loads(r["aml_decision_json"])
                    aml_score = obj.get("risk_score")
                    aml_reasons = obj.get("reasons") or []
                    aml_source = obj.get("source")
            except Exception as e:  # noqa: BLE001
                log.debug("invalid AML decision JSON on withdraw %s: %s", r["tx"], e)
            out.append(
                {
                    "tx": r["tx"],
                    "asset": r["asset"],
                    "amount": r["amount"],
                    "destination": r["destination"],
                    "status": r["status"],
                    "submitted_at": r["submitted_at"],
                    "confirmed_at": r["confirmed_at"],
                    "aml_status": r["aml_status"],
                    "aml_risk_score": aml_score,
                    "aml_reasons": aml_reasons,
                    "aml_source": aml_source,
                }
            )
        return _json(self, 200, out)

    def _withdraw(self):
        # Geo-gate withdrawals: a user who signed up from KR but is now
        # withdrawing from a sanctioned IP must be refused. Current IP wins.
        if _geo_enforce(self, endpoint="/chain/withdraw"):
            return
        user = _require_user(self)
        if not user:
            return
        body = _read_json_body(self)
        chain, bad = self._resolve_chain_param(body)
        if bad is not None:
            return _json(self, 400, {"error": "unknown_chain", "chain": bad})
        if self._block_chain_not_writable(chain):
            return
        if not _require_deployment(self):
            return
        if user.get("kyc_status") != "verified":
            return _json(self, 403, {"error": "kyc_required"})

        asset = (body.get("asset") or "").upper()
        amount = body.get("amount")
        destination = (body.get("destination") or "").strip()
        if asset not in ("ZETH", "ZUSDT") or not amount or not destination:
            return _json(
                self, 400, {"error": "bad_request", "expected": "{asset,amount,destination}"}
            )
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", destination):
            return _json(self, 400, {"error": "bad_destination"})

        opex = user["opex_user"]

        # Per-asset precision / minimum / step validation. Real exchanges
        # accept fractional amounts (Binance: 0.0042 ETH) but enforce a
        # withdraw_precision lower than the on-chain decimals (so an 18-dp
        # ERC20 doesn't let you submit 1e-18 dust). We delegate to the
        # asset-precision table; specific error codes let the UI render a
        # precise hint ("below minimum" vs. "too many decimals").
        amount_str = str(amount).strip() if amount is not None else ""
        ok, reason = validate_withdraw_amount(asset, amount_str)
        if not ok:
            spec = asset_spec(asset)
            return _json(
                self,
                400,
                {
                    "error": reason,
                    "asset": asset,
                    "min_withdraw": spec["min_withdraw"],
                    "step": spec["step"],
                    "withdraw_precision": spec["withdraw_precision"],
                },
            )
        try:
            value = to_wei_for_chain(asset, amount_str)
        except Exception as e:
            # Defensive: validate_withdraw_amount should already have caught
            # this, but if the asset_spec disagrees with the on-chain decimals
            # we want a 400 not a 500.
            return _json(self, 400, {"error": "amount_invalid", "message": str(e)})
        if value <= 0:
            return _json(self, 400, {"error": "amount_invalid"})

        # ---- AML screen the DESTINATION before locking any funds ----------
        # Fail-closed: BLOCK / REVIEW -> 403. Don't even lock funds, don't
        # broadcast, don't write a withdraws row -- the destination is bad,
        # so the user must change it.
        decision = aml_provider.screen_address(
            chain="hardhat-localhost",
            address=destination,
            customer_id=opex,
            kyc_level=user.get("kyc_status"),
        )
        if decision.decision in ("BLOCK", "REVIEW"):
            err = "aml_blocked" if decision.decision == "BLOCK" else "aml_review"
            return _json(
                self,
                403,
                {
                    "error": err,
                    "decision": decision.decision,
                    "risk_score": decision.risk_score,
                    "sanctioned": decision.sanctioned,
                    "reasons": decision.reasons,
                    "source": decision.source,
                },
            )
        # Carry the ALLOW decision forward so it can be persisted on the row.
        aml_decision_json = json.dumps(decision.to_json())
        aml_status_lc = decision.decision.lower()

        # ---- 2FA gate: only enforced when the user has enrolled. ----
        # The client surfaces the TOTP modal on totp_required and retries
        # with body.totp_code (or body.recovery_code). Disable via env if
        # an operator needs to break the dependency in an incident.
        if os.environ.get("REQUIRE_2FA_WITHDRAW", "1") == "1":
            _twofa = (
                body.get("totp_code") or body.get("code") or body.get("recovery_code") or ""
            ).strip()
            ok_2fa, why = _verify_2fa_for_withdraw(opex, body)
            if why == "totp_required":
                return _json(self, 401, {"error": "totp_required", "step": "totp_required"})
            if not ok_2fa:
                return _json(self, 401, {"error": "totp_wrong" if _twofa else "totp_required"})

        # ---- FATF Travel Rule screen (gated by TRAVEL_RULE_ENABLED env) ----
        if os.environ.get("TRAVEL_RULE_ENABLED", "0") in ("1", "true", "yes"):
            _tr_usdt = (
                str(Decimal(str(amount)))
                if asset == "ZUSDT"
                else str(Decimal(str(amount)) * Decimal("2400"))
            )
            _tr_body = json.dumps(
                {
                    "withdraw_id": f"wd-pending-{opex}-{int(time.time()*1000)}",
                    "opex_user": opex,
                    "asset": asset,
                    "amount": str(amount),
                    "amount_usdt": _tr_usdt,
                    "destination_address": destination,
                }
            ).encode()
            try:
                with _http_urlopen(
                    _http_request(
                        TRAVEL_RULE_BASE + "/travel-rule/screen",
                        data=_tr_body,
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    ),
                    timeout=5,
                ) as _r:
                    tr = json.loads(_r.read().decode() or "{}")
            except Exception as _e:
                tr = {"status": "error", "reason": f"tr_unreachable:{_e}"}
            if tr.get("status") == "rejected":
                return _json(
                    self,
                    403,
                    {
                        "error": "travel_rule_blocked",
                        "reason": tr.get("reason"),
                        "tr_request_id": tr.get("tr_request_id"),
                    },
                )
            if tr.get("status") == "pending":
                return _json(
                    self,
                    202,
                    {
                        "status": "pending_travel_rule",
                        "tr_request_id": tr.get("tr_request_id"),
                        "decision": tr.get("decision"),
                        "reason": tr.get("reason"),
                    },
                )

        # Check internal balance availability via wallet API.
        # Internal symbols: ZETH -> ETH, ZUSDT -> USDT.
        internal_symbol = ASSET_TO_INTERNAL_SYMBOL.get(asset)
        try:
            with _http_urlopen(
                f"{WALLET_BASE}/v1/owner/{urllib.parse.quote(opex)}/wallets",
                timeout=5,
            ) as resp:
                wallets = json.loads(resp.read().decode())
        except Exception as e:
            return _json(self, 502, {"error": "wallet_unavailable", "message": str(e)})

        # Find the available balance for this asset across the user's wallet
        # entries. We look for the symbol match.
        avail_units = Decimal(0)
        for w in wallets if isinstance(wallets, list) else wallets.get("wallets", []):
            sym = w.get("currency") or w.get("symbol") or w.get("asset")
            if not sym or str(sym).upper() != internal_symbol:
                continue
            try:
                bal = Decimal(str(w.get("balance", 0)))
                lck = Decimal(str(w.get("locked", 0)))
            except Exception as e:  # noqa: BLE001
                log.debug("skipping malformed wallet row for %s: %s", opex, e)
                continue
            avail_units += bal - lck

        # The internal ledger uses integer units; any decimal amount > the
        # integer units means we don't have enough.
        # Compare in the *internal-symbol* unit space (whole tokens). Convert
        # the requested amount to a Decimal of whole units.
        requested = Decimal(str(amount))
        if requested > avail_units:
            return _json(
                self,
                400,
                {
                    "error": "insufficient_internal_balance",
                    "available": str(avail_units),
                    "requested": str(requested),
                },
            )

        # Debit internal first by transferring from the user's MAIN wallet to a
        # synthetic "zkcex-custody" sink wallet. We use the v2 transfer
        # endpoint with WITHDRAW_REQUEST category — this is more honest than
        # the /withdraw POST controller (which expects WithdrawCommand and a
        # full BC-gateway flow) and the wallet API has no per-call cap on it.
        ref = f"chain-withdraw-{opex}-{int(time.time()*1000)}"
        # The wallet API's path variable `{amount}` is bound to a Spring
        # BigDecimal — fractional values like `0.5` parse correctly when
        # passed as a path segment. We quote_plus the amount defensively so
        # any intermediate path-normalising proxy (one that collapses `.`
        # in segments) sees the value as a single literal token.
        amount_path = urllib.parse.quote(amount_str, safe="")
        url = (
            f"{WALLET_BASE}/v2/transfer/{amount_path}_{internal_symbol}"
            f"/from/{urllib.parse.quote(opex)}_MAIN"
            f"/to/zkcex-custody_MAIN"
        )
        body = json.dumps(
            {
                "description": "zkcex-chain-withdraw",
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
                    return _json(self, 502, {"error": "internal_debit_failed"})
        except urllib.error.HTTPError as e:
            return _json(
                self,
                502,
                {
                    "error": "internal_debit_failed",
                    "code": e.code,
                    "message": e.read()[:300].decode(errors="replace"),
                },
            )
        except Exception as e:
            return _json(self, 502, {"error": "internal_debit_failed", "message": str(e)})

        # Hold large withdraws for ops approval; ops_server.py picks them up via /ops/withdraws/_internal/hold.
        amt_usdt = Decimal(str(amount)) * (
            Decimal(1)
            if asset == "ZUSDT"
            else Decimal(os.environ.get("ZETH_USDT_PRICE_HINT", "3000"))
        )
        if WD_REVIEW_USDT > 0 and amt_usdt >= WD_REVIEW_USDT:
            wid = f"w-{int(time.time()*1000)}-{secrets.token_hex(4)}"
            with _db_lock, db() as c:
                c.execute(
                    "INSERT INTO withdraws(tx,opex_user,asset,amount,destination,status,submitted_at,aml_status,aml_decision_json) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        wid,
                        opex,
                        asset,
                        str(amount),
                        destination.lower(),
                        "review",
                        int(time.time()),
                        aml_status_lc,
                        aml_decision_json,
                    ),
                )
            try:
                _http_urlopen(
                    _http_request(
                        f"{OPS_BASE}/ops/withdraws/_internal/hold",
                        data=json.dumps(
                            {
                                "withdraw_id": wid,
                                "opex_user": opex,
                                "asset": asset,
                                "amount": str(amount),
                                "destination": destination.lower(),
                                "chain": "hardhat-localhost",
                                "amount_usdt": str(amt_usdt),
                                "aml_status": aml_status_lc,
                                "aml_decision": json.loads(aml_decision_json),
                            }
                        ).encode(),
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    ),
                    timeout=5,
                ).read()
            except Exception as e:
                log.warning("ops hold notify failed (row still held): %s", e)  # noqa: BLE001
            return _json(
                self,
                202,
                {
                    "status": "pending_review",
                    "withdraw_id": wid,
                    "reason": "large_withdraw_requires_approval",
                },
            )
        # Now broadcast on-chain.
        custodial = custodial_address()
        try:
            tx = send_transfer(token_address(asset), sender=custodial, to=destination, value=value)
        except RpcError as e:
            return _json(self, 502, {"error": "broadcast_failed", "message": str(e)})

        with _db_lock, db() as c:
            c.execute(
                "INSERT INTO withdraws(tx, opex_user, asset, amount, destination, status, submitted_at,"
                "                      aml_status, aml_decision_json)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    tx,
                    opex,
                    asset,
                    str(amount),
                    destination.lower(),
                    "submitted",
                    int(time.time()),
                    aml_status_lc,
                    aml_decision_json,
                ),
            )
        _notif_send(
            opex,
            "withdraw_submitted",
            "financial",
            "Withdrawal submitted",
            f"{amount} {asset} withdrawal to {destination[:12]}... has been submitted on-chain.",
            {"asset": asset, "amount": str(amount), "destination": destination.lower(), "tx": tx},
        )
        return _json(
            self,
            200,
            {
                "tx": tx,
                "status": "submitted",
                "aml_status": aml_status_lc,
                "aml_risk_score": decision.risk_score,
                "aml_source": decision.source,
            },
        )

    def _withdraw_review(
        self, verb: str, wid: str
    ):  # Loopback ops release/cancel of held withdraw.
        if (self.client_address[0] if self.client_address else "") not in ("127.0.0.1", "::1"):
            return _json(self, 403, {"error": "loopback_only"})
        with db() as c:
            row = c.execute(
                "SELECT * FROM withdraws WHERE tx=? AND status='review'", (wid,)
            ).fetchone()
        if not row:
            return _json(self, 404, {"error": "no_such_held_withdraw"})
        if verb == "release":
            try:
                tx = send_transfer(
                    token_address(row["asset"]),
                    sender=custodial_address(),
                    to=row["destination"],
                    value=to_wei_for_chain(row["asset"], str(row["amount"])),
                )
            except Exception as e:
                return _json(self, 502, {"error": "broadcast_failed", "message": str(e)})  # noqa: BLE001
            with _db_lock, db() as c:
                c.execute(
                    "UPDATE withdraws SET tx=?,status='submitted',submitted_at=? WHERE tx=?",
                    (tx, int(time.time()), wid),
                )
            return _json(self, 200, {"ok": True, "tx": tx, "status": "submitted"})
        with _db_lock, db() as c:
            c.execute("UPDATE withdraws SET status='rejected' WHERE tx=?", (wid,))
        try:
            _http_urlopen(
                _http_request(
                    f"{WALLET_BASE}/v2/transfer/{urllib.parse.quote(str(row['amount']), safe='')}_{ASSET_TO_INTERNAL_SYMBOL.get(row['asset'])}/from/zkcex-custody_MAIN/to/{urllib.parse.quote(row['opex_user'])}_MAIN",
                    method="POST",
                    data=json.dumps(
                        {
                            "description": "ops-rejected-refund",
                            "transferRef": f"refund-{wid}",
                            "transferCategory": "ADJUSTMENT",
                        }
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                ),
                timeout=10,
            ).read()
        except Exception as e:
            log.warning("refund failed for %s: %s", wid, e)  # noqa: BLE001
        return _json(self, 200, {"ok": True, "withdraw_id": wid, "status": "rejected"})


class ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    init_db()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5502
    # Forward uncaught exceptions to the central error_collector
    # (loopback-only POST to :5690). Best-effort, never raises.
    try:
        from _error_reporter import install_global_handler  # type: ignore

        install_global_handler()
    except Exception as e:  # noqa: BLE001
        log.info("error reporter install skipped: %s", e)
    # OpenTelemetry auto-instrumentation of outbound HTTP + SQLite.
    try:
        _otel_install()
    except Exception as e:  # noqa: BLE001
        log.info("otel install skipped: %s", e)
    # warm up the deployment cache so /info doesn't have to read from disk
    load_deployment()
    # spawn indexer
    t = threading.Thread(target=indexer_loop, daemon=True, name="chain-indexer")
    t.start()
    # spawn rate-limiter sweeper
    s = threading.Thread(target=_airdrop_to_sweep_loop, daemon=True, name="airdrop-to-sweep")
    s.start()
    # spawn multi-chain RPC prober (Sepolia, Amoy, Arbitrum-Sepolia, plus hardhat)
    p = threading.Thread(target=chain_probe_loop, daemon=True, name="chain-prober")
    p.start()
    with ThreadingServer(("127.0.0.1", port), Handler) as srv:
        log.info("chain bridge listening on :%d (rpc=%s, db=%s)", port, DEFAULT_RPC, CHAIN_DB_PATH)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
