#!/usr/bin/env python3
"""zkCEX NFT marketplace — ERC721 + ERC1155 listing / browse / buy / sell.

  Browser  -->  homepage proxy (:5500)  -->  nft_marketplace.py (:5694)
                                                |
                                                +--> chain JSON-RPC :8545
                                                +--> auth_server :5501 (bearer -> opex_user)
                                                +--> wallet API   :8091 (debit/credit USDT for buy)
                                                +--> reads deployment.json
                                                    for the sample ZkNFT + ZkNFT1155
                                                    contract addresses

Design choices (and what we intentionally don't ship):
  * Custodial trading. When a user "mints", we transfer a sample NFT from
    the custodial wallet to the user's chain wallet. When a user lists for
    sale, the NFT is moved to the custodial address (escrow). On buy, the
    custodial address transfers it to the buyer. We do not ship signed
    Seaport-style off-chain orders or per-user approvals — the spec calls
    for a usable demo, not a production marketplace.
  * Platform fee is a flat 2.5% credited to the synthetic
    ``zkcex-nft-fees`` wallet, which lives on the existing wallet API
    alongside ``zkcex-custody`` / ``zkcex-futures``.
  * No royalties beyond the platform fee, no off-chain matching, no rarity
    scoring. The metadata files include a Rarity attribute but it's only
    decorative.
  * For atomicity we follow the chain_server pattern: debit the buyer's
    internal balance first, then perform the on-chain transfer, then
    credit the seller. If the chain transfer fails we refund the buyer
    before raising. If the seller credit fails (extremely rare) we log
    and surface a 502.

Stdlib only.
"""

from __future__ import annotations

import http.server
import json
import logging
import os
import re
import socketserver
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, getcontext
from typing import Any

# ----- paths and constants --------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

HARDHAT_DIR = os.path.join(HERE, "hardhat-sim")
DEPLOYMENT_PATH = os.path.join(HARDHAT_DIR, ".local", "deployment.json")
DB_PATH = os.environ.get("NFT_DB", os.path.join(HERE, ".local", "nft.db"))


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
    "WALLET_BASE", os.environ.get("WALLET_BASE", "http://127.0.0.1:8091")
)
RPC_URL = _validated_http_url(
    os.environ.get("NFT_RPC_URL", "http://127.0.0.1:8545"), name="NFT_RPC_URL"
)
CHAIN_BASE = _validated_http_base_url(
    "CHAIN_BASE", os.environ.get("CHAIN_BASE", "http://127.0.0.1:5502")
)

DEFAULT_PORT = 5694
PLATFORM_FEE_BPS = 250  # 2.5%
NFT_FEES_WALLET = "zkcex-nft-fees"
NFT_CUSTODY_WALLET = "zkcex-nft-escrow"  # internal escrow for listed NFTs (USDT side)

LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
AUTH_TTL_SECONDS = 30

# Default listing TTL.
MAX_EXPIRES_DAYS = 60
DEFAULT_EXPIRES_DAYS = 7

getcontext().prec = 60


def _log_setup() -> logging.Logger:
    logging.basicConfig(
        level=os.environ.get("NFT_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return logging.getLogger("nft")


log = _log_setup()


# ----- deployment cache ----------------------------------------------------

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
        except Exception as e:
            log.warning("deployment.json load failed: %s", e)
            return None
        return _deployment_cache


def custodial_address() -> str:
    d = load_deployment()
    if not d:
        raise RuntimeError("deployment.json not loaded — hardhat not deployed yet")
    return str(d["custodial"]).lower()


def deployer_address() -> str:
    d = load_deployment()
    if not d:
        raise RuntimeError("deployment.json not loaded")
    return str(d["deployer"]).lower()


def known_collections() -> list[dict]:
    d = load_deployment()
    if not d or "nft" not in d:
        return []
    return list(d["nft"].get("collections", []))


def collection_by_address(addr: str) -> dict | None:
    addr_l = addr.lower()
    for c in known_collections():
        if str(c.get("address", "")).lower() == addr_l:
            return c
    return None


# ----- ABI / RPC helpers (mini, just what we need) ------------------------


class RpcError(Exception):
    pass


def rpc_call(method: str, params: list[Any] | None = None, *, timeout: float = 15.0) -> Any:
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000) & 0xFFFFFFFF,
            "method": method,
            "params": params or [],
        }
    ).encode()
    req = _http_request(
        RPC_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "zkCEX-nft/1.0",
        },
    )
    try:
        with _http_urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RpcError(f"rpc transport error: {e}") from e
    try:
        obj = json.loads(body)
    except Exception as e:
        raise RpcError(f"rpc bad json: {body[:200]}") from e
    if "error" in obj and obj["error"]:
        raise RpcError(f"rpc error: {obj['error']}")
    return obj.get("result")


# Keccak imported from chain_server for selector / topic derivation. Falls
# back to a small inline copy if chain_server isn't importable for any
# reason (e.g. unit test contexts).
try:
    from chain_server import (  # type: ignore
        addr_to_hex,
        decode_address,
        decode_uint256,
        encode_address,
        encode_uint256,
        event_topic,
        fn_selector,
    )
except Exception:  # pragma: no cover — extremely unlikely in production layout

    def _fallback_keccak(data: bytes) -> bytes:
        # Lazy fallback — re-import chain_server module by file path. If even
        # that fails, raise on use.
        raise RuntimeError("chain_server module unavailable; cannot derive selectors")

    def fn_selector(sig: str) -> str:  # type: ignore[no-redef]
        return "0x" + _fallback_keccak(sig.encode()).hex()[:8]

    def event_topic(sig: str) -> str:  # type: ignore[no-redef]
        return "0x" + _fallback_keccak(sig.encode()).hex()

    def addr_to_hex(addr: str) -> str:  # type: ignore[no-redef]
        a = addr.lower()
        if a.startswith("0x"):
            a = a[2:]
        return "0x" + a

    def encode_address(addr: str) -> str:  # type: ignore[no-redef]
        return "0".zfill(24) + addr_to_hex(addr)[2:]

    def encode_uint256(n: int) -> str:  # type: ignore[no-redef]
        return f"{n:064x}"

    def decode_uint256(h: str) -> int:  # type: ignore[no-redef]
        return int(h[2:] if h.startswith("0x") else h, 16) if h else 0

    def decode_address(h: str) -> str:  # type: ignore[no-redef]
        return "0x" + (h[2:] if h.startswith("0x") else h)[-40:]


# ERC721 selectors
SEL_721_OWNER_OF = fn_selector("ownerOf(uint256)")
SEL_721_BALANCE_OF = fn_selector("balanceOf(address)")
SEL_721_SAFE_TRANSFER = fn_selector("safeTransferFrom(address,address,uint256)")
SEL_721_TOKEN_URI = fn_selector("tokenURI(uint256)")
SEL_721_NAME = fn_selector("name()")
SEL_721_SYMBOL = fn_selector("symbol()")

# ERC1155 selectors
SEL_1155_BALANCE_OF = fn_selector("balanceOf(address,uint256)")
SEL_1155_SAFE_TRANSFER = fn_selector("safeTransferFrom(address,address,uint256,uint256,bytes)")
SEL_1155_URI = fn_selector("uri(uint256)")

# Events
TOPIC_721_TRANSFER = event_topic("Transfer(address,address,uint256)")
TOPIC_1155_TRANSFER_SINGLE = event_topic("TransferSingle(address,address,address,uint256,uint256)")


def decode_string(hex_word: str) -> str:
    """Decode an ABI-encoded dynamic string return value."""
    if not hex_word:
        return ""
    h = hex_word[2:] if hex_word.startswith("0x") else hex_word
    if len(h) < 128:
        return ""
    # word 0 = offset (always 0x20 for a single dynamic return), word 1 = length
    try:
        length = int(h[64:128], 16)
    except ValueError:
        return ""
    if length == 0:
        return ""
    raw_hex = h[128 : 128 + length * 2]
    try:
        return bytes.fromhex(raw_hex).decode("utf-8", errors="replace")
    except Exception:
        return ""


def eth_call(to: str, data: str) -> str:
    return rpc_call("eth_call", [{"to": to, "data": data}, "latest"])


def erc721_owner_of(contract: str, token_id: int) -> str:
    data = SEL_721_OWNER_OF + encode_uint256(token_id)
    r = eth_call(contract, data)
    return decode_address(r).lower()


def erc721_balance_of(contract: str, owner: str) -> int:
    data = SEL_721_BALANCE_OF + encode_address(owner)
    return decode_uint256(eth_call(contract, data))


def erc721_token_uri(contract: str, token_id: int) -> str:
    data = SEL_721_TOKEN_URI + encode_uint256(token_id)
    return decode_string(eth_call(contract, data))


def erc1155_balance_of(contract: str, owner: str, token_id: int) -> int:
    data = SEL_1155_BALANCE_OF + encode_address(owner) + encode_uint256(token_id)
    return decode_uint256(eth_call(contract, data))


def erc1155_uri(contract: str, token_id: int) -> str:
    data = SEL_1155_URI + encode_uint256(token_id)
    return decode_string(eth_call(contract, data))


def send_erc721_transfer(contract: str, sender: str, *, frm: str, to: str, token_id: int) -> str:
    """safeTransferFrom(from,to,tokenId) via eth_sendTransaction.

    `sender` is the `from` account on the JSON-RPC envelope — that account
    must be unlocked on hardhat or impersonated first. For our flows
    `sender` is always the custodial address (hardhat dev account[0]).
    """
    data = (
        SEL_721_SAFE_TRANSFER + encode_address(frm) + encode_address(to) + encode_uint256(token_id)
    )
    return rpc_call(
        "eth_sendTransaction",
        [
            {
                "from": sender,
                "to": contract,
                "data": data,
                "gas": "0x1e8480",  # 2,000,000
            }
        ],
    )


def send_erc1155_transfer(
    contract: str,
    sender: str,
    *,
    frm: str,
    to: str,
    token_id: int,
    amount: int,
) -> str:
    # safeTransferFrom(from, to, id, amount, "") — encode an empty bytes arg.
    # An empty bytes ABI param is: offset (0xa0 = 5*32) + length=0.
    bytes_offset = encode_uint256(0xA0)
    bytes_length = encode_uint256(0)
    data = (
        SEL_1155_SAFE_TRANSFER
        + encode_address(frm)
        + encode_address(to)
        + encode_uint256(token_id)
        + encode_uint256(amount)
        + bytes_offset
        + bytes_length
    )
    return rpc_call(
        "eth_sendTransaction",
        [
            {
                "from": sender,
                "to": contract,
                "data": data,
                "gas": "0x1e8480",
            }
        ],
    )


def impersonate_and_fund(addr: str) -> None:
    """Required for sending from non-unlocked accounts on hardhat."""
    rpc_call("hardhat_impersonateAccount", [addr])
    rpc_call("hardhat_setBalance", [addr, "0x56bc75e2d63100000"])  # 100 ETH


# ----- per-user wallet (delegate to chain_server) --------------------------


def derive_user_address(opex_user: str) -> str | None:
    """Ask chain_server for the user's derived address.

    chain_server caches addresses in chain.db. We don't want to duplicate
    derive.js / Node here, so we call /chain/wallet (which auto-derives) and
    parse the address out.
    """
    try:
        # We're loopback. /chain/wallet wants Bearer auth. Use the auth server
        # to mint a server-side ephemeral token? Simpler: chain_server keeps a
        # cache in chain.db. Just read that directly.
        return _user_address_from_chain_db(opex_user)
    except Exception as e:
        log.warning("derive_user_address(%s) -> %s", opex_user, e)
        return None


def _user_address_from_chain_db(opex_user: str) -> str | None:
    chain_db = os.path.join(HERE, ".local", "chain.db")
    if not os.path.exists(chain_db):
        return None
    try:
        with sqlite3.connect(chain_db, timeout=5) as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT address FROM addr_map WHERE opex_user=?", (opex_user,)
            ).fetchone()
            if row:
                return str(row["address"]).lower()
    except Exception as e:
        log.warning("chain_db read failed: %s", e)
    return None


def ensure_user_address(opex_user: str, *, token: str | None = None) -> str:
    """Return the user's chain address, deriving it via chain_server if needed.

    The marketplace needs to know each user's chain address even before they
    open the wallet page. We POST to /chain/wallet (which derives + caches)
    if the address isn't already in chain.db.
    """
    cached = _user_address_from_chain_db(opex_user)
    if cached:
        return cached
    if not token:
        raise RuntimeError(f"chain wallet not derived for {opex_user} and no auth token provided")
    # GET /chain/wallet with the user's bearer triggers derive.
    req = _http_request(
        f"{CHAIN_BASE}/chain/wallet",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with _http_urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
            addr = body.get("address")
            if not addr:
                raise RuntimeError("chain/wallet returned no address")
            return str(addr).lower()
    except Exception as e:
        raise RuntimeError(f"chain wallet derive failed: {e}") from e


# ----- DB ------------------------------------------------------------------

_db_lock = threading.RLock()


def db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _db_lock, db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS nft_collections (
              contract_address TEXT PRIMARY KEY,
              standard TEXT NOT NULL,
              name TEXT NOT NULL,
              symbol TEXT,
              description TEXT,
              total_supply INTEGER,
              floor_price_usdt TEXT,
              total_volume_usdt TEXT NOT NULL DEFAULT '0'
            );

            CREATE TABLE IF NOT EXISTS nft_listings (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              contract_address TEXT NOT NULL,
              token_id TEXT NOT NULL,
              quantity TEXT NOT NULL DEFAULT '1',
              seller_opex_user TEXT NOT NULL,
              list_price TEXT NOT NULL,
              list_asset TEXT NOT NULL,
              status TEXT NOT NULL,
              listed_at INTEGER NOT NULL,
              expires_at INTEGER,
              sold_at INTEGER,
              buyer_opex_user TEXT,
              trade_tx TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_listings_collection
              ON nft_listings(contract_address, status);
            CREATE INDEX IF NOT EXISTS ix_listings_token
              ON nft_listings(contract_address, token_id);
            CREATE INDEX IF NOT EXISTS ix_listings_seller
              ON nft_listings(seller_opex_user);

            CREATE TABLE IF NOT EXISTS nft_bids (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              listing_id INTEGER NOT NULL,
              bidder_opex_user TEXT NOT NULL,
              bid_price TEXT NOT NULL,
              bid_asset TEXT NOT NULL,
              status TEXT NOT NULL,
              placed_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_bids_listing
              ON nft_bids(listing_id, status);
            CREATE INDEX IF NOT EXISTS ix_bids_bidder
              ON nft_bids(bidder_opex_user);

            CREATE TABLE IF NOT EXISTS nft_history (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts INTEGER NOT NULL,
              contract_address TEXT NOT NULL,
              token_id TEXT NOT NULL,
              event TEXT NOT NULL,
              from_user TEXT,
              to_user TEXT,
              price TEXT,
              asset TEXT,
              tx TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_history_token
              ON nft_history(contract_address, token_id);
            CREATE INDEX IF NOT EXISTS ix_history_user
              ON nft_history(from_user);
            """
        )
        # Idempotent column adds (kept for forward-compatibility).
        cols = {r[1] for r in c.execute("PRAGMA table_info(nft_history)").fetchall()}
        if "tx" not in cols:
            c.execute("ALTER TABLE nft_history ADD COLUMN tx TEXT")


def upsert_collections_from_deployment() -> None:
    """Seed nft_collections from deployment.json on each boot.

    The collection rows are mostly static (deploy-time facts) but
    `total_volume_usdt` accumulates over the marketplace lifetime, so we
    INSERT-OR-IGNORE rather than REPLACE.
    """
    cols = known_collections()
    if not cols:
        log.warning("no NFT collections found in deployment.json")
        return
    with _db_lock, db() as c:
        for col in cols:
            addr = str(col.get("address", "")).lower()
            if not addr:
                continue
            c.execute(
                "INSERT OR IGNORE INTO nft_collections"
                "  (contract_address, standard, name, symbol, description, total_supply)"
                " VALUES (?,?,?,?,?,?)",
                (
                    addr,
                    col.get("standard", "erc721"),
                    col.get("name", "Untitled"),
                    col.get("symbol"),
                    col.get("description"),
                    int(col.get("total_supply") or 0),
                ),
            )


def history_log(
    *,
    contract: str,
    token_id: str | int,
    event: str,
    from_user: str | None = None,
    to_user: str | None = None,
    price: str | None = None,
    asset: str | None = None,
    tx: str | None = None,
) -> None:
    with _db_lock, db() as c:
        c.execute(
            "INSERT INTO nft_history(ts, contract_address, token_id, event, from_user, to_user,"
            " price, asset, tx)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                int(time.time()),
                str(contract).lower(),
                str(token_id),
                event,
                from_user,
                to_user,
                price,
                asset,
                tx,
            ),
        )


def update_collection_volume(contract: str, delta_usdt: Decimal) -> None:
    """Adds delta_usdt to the cumulative volume. Idempotent on sign."""
    with _db_lock, db() as c:
        row = c.execute(
            "SELECT total_volume_usdt FROM nft_collections WHERE contract_address=?",
            (str(contract).lower(),),
        ).fetchone()
        if not row:
            return
        new_vol = (Decimal(str(row["total_volume_usdt"] or "0")) + delta_usdt).quantize(
            Decimal("0.000001")
        )
        c.execute(
            "UPDATE nft_collections SET total_volume_usdt=? WHERE contract_address=?",
            (str(new_vol), str(contract).lower()),
        )


def recompute_floor_price(contract: str) -> None:
    """Recompute floor_price_usdt as the minimum active listing's price."""
    contract_l = str(contract).lower()
    with _db_lock, db() as c:
        rows = c.execute(
            "SELECT list_price, list_asset FROM nft_listings"
            " WHERE contract_address=? AND status='active'",
            (contract_l,),
        ).fetchall()
        prices_usdt: list[Decimal] = []
        for r in rows:
            try:
                p = Decimal(str(r["list_price"]))
                if (r["list_asset"] or "USDT").upper() == "USDT":
                    prices_usdt.append(p)
                else:
                    # Approximate ETH->USDT at the env hint (same as chain_server).
                    rate = Decimal(os.environ.get("ZETH_USDT_PRICE_HINT", "3000"))
                    prices_usdt.append(p * rate)
            except Exception as e:  # noqa: BLE001
                log.debug("collection floor row skipped for %s: %r", contract_l, e)
        floor = min(prices_usdt) if prices_usdt else None
        c.execute(
            "UPDATE nft_collections SET floor_price_usdt=? WHERE contract_address=?",
            (str(floor) if floor is not None else None, contract_l),
        )


# ----- auth helpers --------------------------------------------------------

_auth_cache: dict[str, tuple[float, dict]] = {}
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
        with _http_urlopen(req, timeout=5) as resp:
            obj = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        log.info("auth/me HTTP %s", e.code)
        return None
    except Exception as e:
        log.info("auth/me transport error: %s", e)
        return None
    user = obj.get("user")
    if not user or not user.get("opex_user"):
        return None
    with _auth_cache_lock:
        _auth_cache[token] = (now, user)
    return user


# ----- wallet API helpers --------------------------------------------------


def _wallet_get_balance(opex_user: str, asset: str) -> Decimal:
    """Read the user's available balance for `asset` from the wallet API."""
    try:
        with _http_urlopen(
            f"{WALLET_BASE}/v1/owner/{urllib.parse.quote(opex_user)}/wallets",
            timeout=6,
        ) as resp:
            body = json.loads(resp.read().decode())
    except Exception as e:
        raise RuntimeError(f"wallet API unavailable: {e}") from e
    asset_u = asset.upper()
    rows = body if isinstance(body, list) else body.get("wallets", [])
    avail = Decimal(0)
    for w in rows or []:
        sym = w.get("currency") or w.get("symbol") or w.get("asset")
        if not sym or str(sym).upper() != asset_u:
            continue
        bal = Decimal(str(w.get("balance", 0)))
        locked = Decimal(str(w.get("locked", 0)))
        avail += bal - locked
    return avail


def _wallet_transfer(
    *,
    amount: Decimal,
    asset: str,
    from_wallet: str,
    to_wallet: str,
    ref: str,
    description: str,
    category: str = "TRADE",
) -> tuple[bool, str]:
    """Move `amount` of `asset` between two MAIN wallets via the v2 transfer API.

    Returns (ok, error_message). Amount is floored to int because the wallet
    API's path encodes the amount and most wallet endpoints expect whole
    integer units. The 2.5% platform fee on USDT trades is computed in
    integer USDT (so e.g. a 100 USDT trade -> 2 USDT fee, not 2.5).
    """
    amt_int = int(amount)
    if amt_int <= 0:
        return False, "amount must be a positive whole number of units"
    if Decimal(amt_int) != amount:
        return False, f"wallet transfers require whole units of {asset}; got {amount}"
    asset_u = asset.upper()
    url = (
        f"{WALLET_BASE}/v2/transfer/{amt_int}_{asset_u}"
        f"/from/{urllib.parse.quote(from_wallet)}_MAIN"
        f"/to/{urllib.parse.quote(to_wallet)}_MAIN"
    )
    body = json.dumps(
        {
            "description": description,
            "transferRef": ref,
            "transferCategory": category,
        }
    ).encode()
    req = _http_request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with _http_urlopen(req, timeout=10) as resp:
            if resp.status >= 300:
                return False, f"wallet HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        msg = ""
        try:
            msg = e.read().decode("utf-8", errors="replace")[:240]
        except Exception as read_error:  # noqa: BLE001
            log.warning("wallet error body read failed: %r", read_error)
        return False, f"wallet HTTP {e.code}: {msg}"
    except Exception as e:
        return False, f"wallet transport error: {e}"
    return True, ""


def _wallet_seed_deposit(opex_user_or_synth: str, asset: str, amount: int) -> tuple[bool, str]:
    """Create (or top up) a wallet via the seed-deposit endpoint.

    We use this once on first boot to make sure ``zkcex-nft-fees_MAIN``
    exists so transfer-to it doesn't 404 the first time someone buys.
    """
    asset_u = asset.upper()
    path = (
        f"/deposit/{amount}_test-ethereum_{asset_u}/{urllib.parse.quote(opex_user_or_synth)}_MAIN"
        f"?description=nft-marketplace-bootstrap&transferRef=nftboot-{int(time.time()*1000)}-{opex_user_or_synth}"
    )
    try:
        req = _http_request(f"{WALLET_BASE}{path}", data=b"", method="POST")
        with _http_urlopen(req, timeout=10) as resp:
            return resp.status < 300, ""
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)


# ----- HTTP utilities ------------------------------------------------------


def _json_resp(handler: http.server.BaseHTTPRequestHandler, status: int, body: Any) -> None:
    payload = json.dumps(body, default=str).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _read_json(handler: http.server.BaseHTTPRequestHandler) -> dict:
    n = int(handler.headers.get("Content-Length") or 0)
    if n <= 0:
        return {}
    raw = handler.rfile.read(n)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode())
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


def _qs(handler: http.server.BaseHTTPRequestHandler, key: str) -> str | None:
    if "?" not in handler.path:
        return None
    qs = urllib.parse.parse_qs(handler.path.split("?", 1)[1])
    v = qs.get(key)
    return v[0] if v else None


def _qs_all(handler: http.server.BaseHTTPRequestHandler) -> dict[str, str]:
    if "?" not in handler.path:
        return {}
    qs = urllib.parse.parse_qs(handler.path.split("?", 1)[1])
    return {k: v[0] for k, v in qs.items() if v}


# ----- collection / listing read helpers -----------------------------------


def _row_to_listing(row) -> dict:
    out = dict(row)
    # Ensure consistent types
    for k in ("listed_at", "expires_at", "sold_at"):
        if k in out and out[k] is not None:
            out[k] = int(out[k])
    return out


def _enrich_listing(row, *, include_meta: bool = True) -> dict:
    """Pad a listing row with collection + metadata details for the UI."""
    d = _row_to_listing(row)
    col = collection_by_address(d["contract_address"]) or {}
    d["collection_name"] = col.get("name")
    d["collection_symbol"] = col.get("symbol")
    d["collection_standard"] = col.get("standard")
    if include_meta:
        meta = _load_token_meta(d["contract_address"], d["token_id"])
        if meta:
            d["name"] = meta.get("name")
            d["image"] = meta.get("image")
    return d


# Local file fetch for nft-meta JSON (fast, cache-friendly).
META_DIR = os.path.abspath(os.path.join(HERE, "..", "homepage", "nft-meta"))
_meta_cache: dict[str, dict | None] = {}
_meta_lock = threading.RLock()


def _load_token_meta(contract: str, token_id: str | int) -> dict | None:
    key = f"{contract.lower()}:{token_id}"
    with _meta_lock:
        if key in _meta_cache:
            return _meta_cache[key]
    # Try chain tokenURI first, fall back to filesystem by id.
    meta: dict | None = None
    uri = ""
    try:
        col = collection_by_address(contract) or {}
        if col.get("standard") == "erc1155":
            uri = erc1155_uri(contract, int(token_id))
        else:
            uri = erc721_token_uri(contract, int(token_id))
    except Exception:
        uri = ""
    # URIs in our demo always look like /nft-meta/<id>.json
    path = None
    if uri and uri.startswith("/nft-meta/"):
        path = os.path.join(META_DIR, os.path.basename(uri))
    elif uri.startswith("http"):
        path = None  # external — skip
    else:
        # Fallback: try by id directly.
        path = os.path.join(META_DIR, f"{token_id}.json")
    if path and os.path.exists(path):
        try:
            with open(path) as f:
                meta = json.load(f)
        except Exception:
            meta = None
    with _meta_lock:
        _meta_cache[key] = meta
    return meta


# ----- ownership verification ---------------------------------------------


def _verify_ownership(
    contract: str, token_id: int, owner_addr: str, quantity: int, standard: str
) -> tuple[bool, str]:
    """Verify the on-chain owner of (contract, token_id) is `owner_addr`."""
    try:
        if standard == "erc721":
            if quantity != 1:
                return False, "ERC721 quantity must be 1"
            actual = erc721_owner_of(contract, token_id)
            if actual != owner_addr.lower():
                return False, f"on-chain owner is {actual}, not {owner_addr}"
            return True, ""
        elif standard == "erc1155":
            if quantity < 1:
                return False, "ERC1155 quantity must be >= 1"
            bal = erc1155_balance_of(contract, owner_addr, token_id)
            if bal < quantity:
                return False, f"on-chain balance {bal} < {quantity}"
            return True, ""
        return False, f"unknown standard {standard}"
    except RpcError as e:
        return False, f"RPC error: {e}"


# ----- handler -------------------------------------------------------------


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "zkcex-nft/1.0"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    # ---------------- routing ----------------
    def do_GET(self):  # noqa: N802
        try:
            return self._route_get()
        except Exception:
            log.exception("GET %s failed", self.path)
            return _json_resp(
                self,
                500,
                {"error": "internal", "trace": traceback.format_exc(limit=3)},
            )

    def do_POST(self):  # noqa: N802
        try:
            return self._route_post()
        except Exception:
            log.exception("POST %s failed", self.path)
            return _json_resp(
                self,
                500,
                {"error": "internal", "trace": traceback.format_exc(limit=3)},
            )

    def _route_get(self):
        base = self.path.split("?", 1)[0]
        # public
        if base in ("/nft/health", "/health"):
            return self._health()
        if base == "/nft/collections":
            return self._collections_list()
        m = re.match(r"^/nft/collections/(0x[0-9a-fA-F]{40})/?$", base)
        if m:
            return self._collection_detail(m.group(1))
        if base == "/nft/listings":
            return self._listings_list()
        m = re.match(r"^/nft/tokens/(0x[0-9a-fA-F]{40})/([^/]+)/?$", base)
        if m:
            return self._token_detail(m.group(1), m.group(2))
        # Bearer
        if base == "/nft/my-nfts":
            return self._my_nfts()
        if base == "/nft/my-listings":
            return self._my_listings()
        if base == "/nft/my-bids":
            return self._my_bids()
        return _json_resp(self, 404, {"error": "not_found", "path": self.path})

    def _route_post(self):
        base = self.path.split("?", 1)[0]
        if base == "/nft/list":
            return self._list_for_sale()
        m = re.match(r"^/nft/cancel-listing/(\d+)/?$", base)
        if m:
            return self._cancel_listing(int(m.group(1)))
        m = re.match(r"^/nft/buy/(\d+)/?$", base)
        if m:
            return self._buy(int(m.group(1)))
        if base == "/nft/bid":
            return self._bid()
        m = re.match(r"^/nft/accept-bid/(\d+)/?$", base)
        if m:
            return self._accept_bid(int(m.group(1)))
        m = re.match(r"^/nft/cancel-bid/(\d+)/?$", base)
        if m:
            return self._cancel_bid(int(m.group(1)))
        if base == "/nft/mint-sample":
            return self._mint_sample()
        return _json_resp(self, 404, {"error": "not_found", "path": self.path})

    # ---------------- endpoints ----------------

    def _health(self):
        d = load_deployment()
        return _json_resp(
            self,
            200,
            {
                "ok": True,
                "deployment_loaded": bool(d and d.get("nft")),
                "collections": len(known_collections()),
                "now": int(time.time()),
            },
        )

    def _collections_list(self):
        # Refresh from deployment.json (in case it changed since boot).
        upsert_collections_from_deployment()
        with db() as c:
            rows = c.execute("SELECT * FROM nft_collections ORDER BY name").fetchall()
            # Count active listings per collection.
            counts: dict[str, int] = {}
            for r in c.execute(
                "SELECT contract_address, COUNT(*) AS n FROM nft_listings"
                " WHERE status='active' GROUP BY contract_address"
            ).fetchall():
                counts[r["contract_address"]] = int(r["n"])
        out = []
        for r in rows:
            d = dict(r)
            d["active_listings"] = counts.get(d["contract_address"], 0)
            # Pull a representative image: first metadata file we can find.
            col = collection_by_address(d["contract_address"]) or {}
            sample_id = (col.get("edition_ids") or [None])[0]
            if not sample_id:
                sample_id = 1
            meta = _load_token_meta(d["contract_address"], sample_id)
            if meta:
                d["sample_image"] = meta.get("image")
            out.append(d)
        return _json_resp(self, 200, {"collections": out})

    def _collection_detail(self, address: str):
        addr_l = address.lower()
        with db() as c:
            row = c.execute(
                "SELECT * FROM nft_collections WHERE contract_address=?",
                (addr_l,),
            ).fetchone()
            if not row:
                return _json_resp(self, 404, {"error": "unknown_collection", "address": addr_l})
            listings = c.execute(
                "SELECT * FROM nft_listings WHERE contract_address=? AND status='active'"
                " ORDER BY listed_at DESC LIMIT 200",
                (addr_l,),
            ).fetchall()
            recent = c.execute(
                "SELECT * FROM nft_history WHERE contract_address=? AND event IN ('sale','list')"
                " ORDER BY ts DESC LIMIT 25",
                (addr_l,),
            ).fetchall()
        out = dict(row)
        out["active_listings"] = [_enrich_listing(r) for r in listings]
        out["recent_activity"] = [dict(r) for r in recent]
        return _json_resp(self, 200, out)

    def _listings_list(self):
        qs = _qs_all(self)
        collection = (qs.get("collection") or "").lower()
        sort = qs.get("sort") or "recent"
        try:
            min_price = Decimal(qs["min_price"]) if "min_price" in qs else None
        except Exception:
            min_price = None
        try:
            max_price = Decimal(qs["max_price"]) if "max_price" in qs else None
        except Exception:
            max_price = None
        asset = (qs.get("asset") or "").upper()
        standard = (qs.get("standard") or "").lower()
        limit = max(1, min(500, int(qs.get("limit") or 100)))

        sql = "SELECT * FROM nft_listings WHERE status='active'"
        params: list[Any] = []
        if collection:
            sql += " AND contract_address=?"
            params.append(collection)
        if asset:
            sql += " AND list_asset=?"
            params.append(asset)

        order = "listed_at DESC"
        if sort == "price_asc":
            order = "CAST(list_price AS REAL) ASC"
        elif sort == "price_desc":
            order = "CAST(list_price AS REAL) DESC"
        elif sort == "oldest":
            order = "listed_at ASC"
        sql += f" ORDER BY {order} LIMIT ?"
        params.append(limit)
        with db() as c:
            rows = c.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = _enrich_listing(r)
            # Filters applied post-query (since list_price is text).
            try:
                price = Decimal(d["list_price"])
            except Exception:
                price = Decimal(0)
            if min_price is not None and price < min_price:
                continue
            if max_price is not None and price > max_price:
                continue
            if standard and (d.get("collection_standard") or "").lower() != standard:
                continue
            out.append(d)
        return _json_resp(self, 200, {"listings": out, "count": len(out)})

    def _token_detail(self, contract: str, token_id: str):
        contract_l = contract.lower()
        col = collection_by_address(contract_l)
        if not col:
            return _json_resp(self, 404, {"error": "unknown_collection"})
        # On-chain owner / balance.
        owner_addr: str | None = None
        custody = custodial_address()
        if col["standard"] == "erc721":
            try:
                owner_addr = erc721_owner_of(contract_l, int(token_id))
            except Exception as e:
                log.info("ownerOf failed: %s", e)
        # else: ERC1155 has no single owner — leave None.

        with db() as c:
            current = c.execute(
                "SELECT * FROM nft_listings WHERE contract_address=? AND token_id=?"
                " AND status='active' ORDER BY listed_at DESC LIMIT 1",
                (contract_l, str(token_id)),
            ).fetchone()
            history = c.execute(
                "SELECT * FROM nft_history WHERE contract_address=? AND token_id=?"
                " ORDER BY ts DESC LIMIT 50",
                (contract_l, str(token_id)),
            ).fetchall()
            bids = []
            if current:
                bids = c.execute(
                    "SELECT * FROM nft_bids WHERE listing_id=? AND status='active'"
                    " ORDER BY CAST(bid_price AS REAL) DESC LIMIT 20",
                    (current["id"],),
                ).fetchall()
        meta = _load_token_meta(contract_l, token_id)
        result = {
            "contract_address": contract_l,
            "token_id": str(token_id),
            "standard": col["standard"],
            "collection_name": col.get("name"),
            "collection_symbol": col.get("symbol"),
            "owner_addr": owner_addr,
            "is_in_escrow": owner_addr == custody if owner_addr else None,
            "metadata": meta,
            "current_listing": _enrich_listing(current) if current else None,
            "history": [dict(r) for r in history],
            "bids": [dict(r) for r in bids],
        }
        return _json_resp(self, 200, result)

    def _my_nfts(self):
        user = self._require_user()
        if not user:
            return
        opex = user["opex_user"]
        token = _bearer(self)
        try:
            user_addr = ensure_user_address(opex, token=token)
        except Exception as e:
            return _json_resp(self, 502, {"error": "wallet_derivation_failed", "message": str(e)})
        owned: list[dict] = []
        # ERC721 — iterate all tokens. The contract's nextTokenId tells us the
        # range cheaply; for 100 tokens we just probe ownerOf in a loop.
        for col in known_collections():
            addr = str(col["address"]).lower()
            standard = col["standard"]
            total = int(col.get("total_supply") or 100)
            if standard == "erc721":
                for tid in range(1, total + 1):
                    try:
                        owner = erc721_owner_of(addr, tid)
                    except Exception as e:  # noqa: BLE001
                        log.debug("ownerOf skipped contract=%s token=%s: %r", addr, tid, e)
                        continue
                    if owner == user_addr:
                        meta = _load_token_meta(addr, tid)
                        owned.append(
                            {
                                "contract_address": addr,
                                "token_id": str(tid),
                                "standard": "erc721",
                                "quantity": "1",
                                "name": (meta or {}).get("name"),
                                "image": (meta or {}).get("image"),
                                "collection_name": col.get("name"),
                            }
                        )
            elif standard == "erc1155":
                for tid in col.get("edition_ids") or [1, 2, 3, 4, 5]:
                    # ERC1155 ids in our deployment are offset by 1000 in the
                    # metadata files but the on-chain id is the small 1..5.
                    try:
                        bal = erc1155_balance_of(addr, user_addr, tid)
                    except Exception:
                        bal = 0
                    if bal > 0:
                        meta_id = 1000 + tid if tid < 1000 else tid
                        meta = _load_token_meta(addr, meta_id)
                        owned.append(
                            {
                                "contract_address": addr,
                                "token_id": str(tid),
                                "standard": "erc1155",
                                "quantity": str(bal),
                                "name": (meta or {}).get("name") or f"Edition #{tid}",
                                "image": (meta or {}).get("image"),
                                "collection_name": col.get("name"),
                            }
                        )
        return _json_resp(
            self,
            200,
            {
                "opex_user": opex,
                "address": user_addr,
                "owned": owned,
                "count": len(owned),
            },
        )

    def _my_listings(self):
        user = self._require_user()
        if not user:
            return
        with db() as c:
            rows = c.execute(
                "SELECT * FROM nft_listings WHERE seller_opex_user=?"
                " ORDER BY listed_at DESC LIMIT 200",
                (user["opex_user"],),
            ).fetchall()
        return _json_resp(self, 200, {"listings": [_enrich_listing(r) for r in rows]})

    def _my_bids(self):
        user = self._require_user()
        if not user:
            return
        with db() as c:
            rows = c.execute(
                "SELECT b.*, l.contract_address, l.token_id, l.list_price, l.list_asset"
                " FROM nft_bids b JOIN nft_listings l ON b.listing_id=l.id"
                " WHERE b.bidder_opex_user=? ORDER BY b.placed_at DESC LIMIT 200",
                (user["opex_user"],),
            ).fetchall()
        return _json_resp(self, 200, {"bids": [dict(r) for r in rows]})

    # ---- mutations ----

    def _list_for_sale(self):
        user = self._require_user()
        if not user:
            return
        body = _read_json(self)
        contract = (body.get("contract_address") or "").lower().strip()
        token_id = body.get("token_id")
        quantity = body.get("quantity") or "1"
        list_price = body.get("list_price")
        list_asset = (body.get("list_asset") or "USDT").upper()
        expires_in_days = body.get("expires_in_days")

        if not re.fullmatch(r"0x[0-9a-f]{40}", contract):
            return _json_resp(self, 400, {"error": "bad_contract_address"})
        col = collection_by_address(contract)
        if not col:
            return _json_resp(self, 400, {"error": "unknown_collection"})
        standard = col["standard"]
        try:
            tid_i = int(token_id)
        except Exception:
            return _json_resp(self, 400, {"error": "bad_token_id"})
        try:
            qty_i = int(quantity)
        except Exception:
            return _json_resp(self, 400, {"error": "bad_quantity"})
        try:
            price_d = Decimal(str(list_price))
        except Exception:
            return _json_resp(self, 400, {"error": "bad_list_price"})
        if price_d <= 0:
            return _json_resp(self, 400, {"error": "list_price must be > 0"})
        if list_asset not in ("USDT", "ETH"):
            return _json_resp(self, 400, {"error": "list_asset must be USDT or ETH"})

        # TTL
        try:
            days_i = int(expires_in_days or DEFAULT_EXPIRES_DAYS)
        except Exception:
            days_i = DEFAULT_EXPIRES_DAYS
        days_i = max(1, min(MAX_EXPIRES_DAYS, days_i))

        opex = user["opex_user"]
        token = _bearer(self)
        try:
            seller_addr = ensure_user_address(opex, token=token)
        except Exception as e:
            return _json_resp(self, 502, {"error": "wallet_derivation_failed", "message": str(e)})

        # Verify ownership on chain (anti-spoof).
        ok, msg = _verify_ownership(contract, tid_i, seller_addr, qty_i, standard)
        if not ok:
            return _json_resp(self, 400, {"error": "not_owner", "message": msg})

        # Block double-listing of the same active (contract, token_id) by the same user.
        with _db_lock, db() as c:
            existing = c.execute(
                "SELECT id FROM nft_listings WHERE contract_address=? AND token_id=?"
                " AND seller_opex_user=? AND status='active'",
                (contract, str(tid_i), opex),
            ).fetchone()
            if existing:
                return _json_resp(
                    self,
                    409,
                    {"error": "already_listed", "listing_id": existing["id"]},
                )

        # Move the NFT into custodial escrow on-chain. Requires hardhat
        # impersonation since user-derived addresses aren't unlocked by default.
        custody = custodial_address()
        try:
            impersonate_and_fund(seller_addr)
            if standard == "erc721":
                tx = send_erc721_transfer(
                    contract, seller_addr, frm=seller_addr, to=custody, token_id=tid_i
                )
            else:
                tx = send_erc1155_transfer(
                    contract,
                    seller_addr,
                    frm=seller_addr,
                    to=custody,
                    token_id=tid_i,
                    amount=qty_i,
                )
        except RpcError as e:
            return _json_resp(self, 502, {"error": "escrow_transfer_failed", "message": str(e)})

        now = int(time.time())
        expires_at = now + days_i * 86400
        with _db_lock, db() as c:
            cur = c.execute(
                "INSERT INTO nft_listings"
                " (contract_address, token_id, quantity, seller_opex_user,"
                "  list_price, list_asset, status, listed_at, expires_at)"
                " VALUES (?,?,?,?,?,?, 'active', ?, ?)",
                (
                    contract,
                    str(tid_i),
                    str(qty_i),
                    opex,
                    str(price_d),
                    list_asset,
                    now,
                    expires_at,
                ),
            )
            listing_id = cur.lastrowid
        history_log(
            contract=contract,
            token_id=tid_i,
            event="list",
            from_user=opex,
            to_user=None,
            price=str(price_d),
            asset=list_asset,
            tx=tx,
        )
        recompute_floor_price(contract)
        return _json_resp(
            self,
            200,
            {
                "ok": True,
                "listing_id": listing_id,
                "escrow_tx": tx,
                "expires_at": expires_at,
            },
        )

    def _cancel_listing(self, listing_id: int):
        user = self._require_user()
        if not user:
            return
        opex = user["opex_user"]
        with _db_lock, db() as c:
            row = c.execute("SELECT * FROM nft_listings WHERE id=?", (listing_id,)).fetchone()
            if not row:
                return _json_resp(self, 404, {"error": "not_found"})
            if row["seller_opex_user"] != opex:
                return _json_resp(self, 403, {"error": "not_owner"})
            if row["status"] != "active":
                return _json_resp(self, 400, {"error": "not_active", "status": row["status"]})

        # Return the NFT from escrow back to the seller's chain wallet.
        token = _bearer(self)
        try:
            seller_addr = ensure_user_address(opex, token=token)
        except Exception as e:
            return _json_resp(self, 502, {"error": "wallet_derivation_failed", "message": str(e)})
        contract = row["contract_address"]
        tid_i = int(row["token_id"])
        qty_i = int(row["quantity"])
        col = collection_by_address(contract) or {}
        standard = col.get("standard", "erc721")
        custody = custodial_address()
        try:
            if standard == "erc721":
                tx = send_erc721_transfer(
                    contract, custody, frm=custody, to=seller_addr, token_id=tid_i
                )
            else:
                tx = send_erc1155_transfer(
                    contract,
                    custody,
                    frm=custody,
                    to=seller_addr,
                    token_id=tid_i,
                    amount=qty_i,
                )
        except RpcError as e:
            return _json_resp(self, 502, {"error": "return_transfer_failed", "message": str(e)})

        with _db_lock, db() as c:
            c.execute("UPDATE nft_listings SET status='cancelled' WHERE id=?", (listing_id,))
            # Also mark any open bids as cancelled.
            c.execute(
                "UPDATE nft_bids SET status='cancelled' WHERE listing_id=? AND status='active'",
                (listing_id,),
            )
        history_log(
            contract=contract,
            token_id=tid_i,
            event="cancel",
            from_user=opex,
            to_user=None,
            tx=tx,
        )
        recompute_floor_price(contract)
        return _json_resp(self, 200, {"ok": True, "return_tx": tx})

    def _buy(self, listing_id: int):
        user = self._require_user()
        if not user:
            return
        opex = user["opex_user"]
        token = _bearer(self)
        try:
            buyer_addr = ensure_user_address(opex, token=token)
        except Exception as e:
            return _json_resp(self, 502, {"error": "wallet_derivation_failed", "message": str(e)})

        with _db_lock, db() as c:
            row = c.execute("SELECT * FROM nft_listings WHERE id=?", (listing_id,)).fetchone()
            if not row:
                return _json_resp(self, 404, {"error": "not_found"})
            if row["status"] != "active":
                return _json_resp(self, 400, {"error": "not_active", "status": row["status"]})
            if row["seller_opex_user"] == opex:
                return _json_resp(self, 400, {"error": "self_buy_forbidden"})

        return self._settle_trade(
            listing=row,
            buyer_opex=opex,
            buyer_addr=buyer_addr,
            price=Decimal(str(row["list_price"])),
            asset=row["list_asset"],
        )

    def _settle_trade(
        self,
        *,
        listing,
        buyer_opex: str,
        buyer_addr: str,
        price: Decimal,
        asset: str,
    ):
        """Atomically: debit buyer, credit seller (minus 2.5% fee), transfer NFT.

        Strategy:
          1) Debit buyer's MAIN -> nft-fees_MAIN by the full price. We use
             the fees wallet as a temporary holding pen so all-or-nothing
             logic stays simple. Wallet API enforces "no overdraft".
          2) On-chain transfer NFT from custodial -> buyer.
          3) On success: split the price into fee (2.5%) and seller proceeds.
             Move proceeds from nft-fees_MAIN -> seller's MAIN. (Fee stays
             where it is.)
          4) On chain failure: refund buyer (nft-fees_MAIN -> buyer MAIN).
          5) Update the listing row, write history, recompute floor.
        """
        listing_id = listing["id"]
        contract = listing["contract_address"]
        tid_i = int(listing["token_id"])
        qty_i = int(listing["quantity"])
        seller_opex = listing["seller_opex_user"]
        col = collection_by_address(contract) or {}
        standard = col.get("standard", "erc721")
        custody = custodial_address()
        asset_u = asset.upper()

        # Quick balance precheck (the wallet API will also enforce this; the
        # precheck just gives a clean 400 instead of a generic 502).
        try:
            avail = _wallet_get_balance(buyer_opex, asset_u)
        except Exception as e:
            return _json_resp(self, 502, {"error": "wallet_unavailable", "message": str(e)})
        if avail < price:
            return _json_resp(
                self,
                400,
                {
                    "error": "insufficient_funds",
                    "asset": asset_u,
                    "available": str(avail),
                    "required": str(price),
                },
            )

        # 1) Debit buyer -> fees pool (full price, temporary holding).
        ref_buy = f"nft-buy-{listing_id}-{int(time.time()*1000)}"
        ok, msg = _wallet_transfer(
            amount=price,
            asset=asset_u,
            from_wallet=buyer_opex,
            to_wallet=NFT_FEES_WALLET,
            ref=ref_buy,
            description=f"nft-marketplace-buy listing={listing_id}",
            category="TRADE",
        )
        if not ok:
            return _json_resp(self, 502, {"error": "buyer_debit_failed", "message": msg})

        # 2) On-chain transfer custodial -> buyer.
        try:
            if standard == "erc721":
                tx = send_erc721_transfer(
                    contract,
                    custody,
                    frm=custody,
                    to=buyer_addr,
                    token_id=tid_i,
                )
            else:
                tx = send_erc1155_transfer(
                    contract,
                    custody,
                    frm=custody,
                    to=buyer_addr,
                    token_id=tid_i,
                    amount=qty_i,
                )
        except RpcError as e:
            # Refund buyer.
            log.warning("on-chain transfer failed: %s — refunding buyer", e)
            ref_refund = f"nft-buy-refund-{listing_id}-{int(time.time()*1000)}"
            _wallet_transfer(
                amount=price,
                asset=asset_u,
                from_wallet=NFT_FEES_WALLET,
                to_wallet=buyer_opex,
                ref=ref_refund,
                description=f"nft-marketplace-refund listing={listing_id}",
                category="ADJUSTMENT",
            )
            return _json_resp(self, 502, {"error": "chain_transfer_failed", "message": str(e)})

        # 3) Pay the seller. Compute fee in whole units (since wallet API
        # requires whole-unit transfers). We round the fee DOWN so the seller
        # is never short-changed below the disclosed bps; the leftover unit
        # stays in the fees pool, which is correct accounting.
        fee_amount = (price * Decimal(PLATFORM_FEE_BPS) / Decimal(10000)).quantize(
            Decimal(1), rounding="ROUND_FLOOR"
        )
        seller_proceeds = price - fee_amount
        if seller_proceeds > 0:
            ref_pay = f"nft-buy-pay-{listing_id}-{int(time.time()*1000)}"
            ok, msg = _wallet_transfer(
                amount=seller_proceeds,
                asset=asset_u,
                from_wallet=NFT_FEES_WALLET,
                to_wallet=seller_opex,
                ref=ref_pay,
                description=f"nft-marketplace-proceeds listing={listing_id}",
                category="TRADE",
            )
            if not ok:
                # We've already done the chain transfer — surface a 502 and
                # log loudly. Manual reconciliation is out of scope for the
                # demo but we record the issue in history.
                log.error("seller payout failed: %s (listing=%s)", msg, listing_id)
                history_log(
                    contract=contract,
                    token_id=tid_i,
                    event="payout_failed",
                    from_user=NFT_FEES_WALLET,
                    to_user=seller_opex,
                    price=str(seller_proceeds),
                    asset=asset_u,
                    tx=tx,
                )
                return _json_resp(
                    self,
                    502,
                    {
                        "error": "seller_payout_failed",
                        "trade_tx": tx,
                        "message": msg,
                        "note": "NFT transferred on-chain but seller credit failed — operator review required",
                    },
                )

        # 4) Mark listing sold, kill open bids, log history, update floor.
        now = int(time.time())
        with _db_lock, db() as c:
            c.execute(
                "UPDATE nft_listings SET status='sold', sold_at=?, buyer_opex_user=?, trade_tx=?"
                " WHERE id=?",
                (now, buyer_opex, tx, listing_id),
            )
            c.execute(
                "UPDATE nft_bids SET status='cancelled' WHERE listing_id=? AND status='active'",
                (listing_id,),
            )
        history_log(
            contract=contract,
            token_id=tid_i,
            event="sale",
            from_user=seller_opex,
            to_user=buyer_opex,
            price=str(price),
            asset=asset_u,
            tx=tx,
        )
        # Volume bookkeeping (rough USDT-equivalent for ETH listings).
        usdt_volume = price
        if asset_u == "ETH":
            usdt_volume = price * Decimal(os.environ.get("ZETH_USDT_PRICE_HINT", "3000"))
        update_collection_volume(contract, usdt_volume)
        recompute_floor_price(contract)

        return _json_resp(
            self,
            200,
            {
                "ok": True,
                "listing_id": listing_id,
                "trade_tx": tx,
                "price": str(price),
                "asset": asset_u,
                "fee_amount": str(fee_amount),
                "seller_proceeds": str(seller_proceeds),
            },
        )

    def _bid(self):
        user = self._require_user()
        if not user:
            return
        body = _read_json(self)
        try:
            listing_id = int(body.get("listing_id"))
        except Exception:
            return _json_resp(self, 400, {"error": "bad_listing_id"})
        try:
            bid_price = Decimal(str(body.get("bid_price")))
        except Exception:
            return _json_resp(self, 400, {"error": "bad_bid_price"})
        bid_asset = (body.get("bid_asset") or "USDT").upper()
        if bid_price <= 0:
            return _json_resp(self, 400, {"error": "bid must be > 0"})
        if bid_asset not in ("USDT", "ETH"):
            return _json_resp(self, 400, {"error": "bid_asset must be USDT or ETH"})

        opex = user["opex_user"]
        with _db_lock, db() as c:
            listing = c.execute("SELECT * FROM nft_listings WHERE id=?", (listing_id,)).fetchone()
            if not listing:
                return _json_resp(self, 404, {"error": "no_such_listing"})
            if listing["status"] != "active":
                return _json_resp(self, 400, {"error": "listing_not_active"})
            if listing["seller_opex_user"] == opex:
                return _json_resp(self, 400, {"error": "self_bid_forbidden"})

        # Bid locks the funds in nft-fees temporarily. On accept the funds are
        # split fee/proceeds same as buy; on cancel they're refunded.
        try:
            avail = _wallet_get_balance(opex, bid_asset)
        except Exception as e:
            return _json_resp(self, 502, {"error": "wallet_unavailable", "message": str(e)})
        if avail < bid_price:
            return _json_resp(
                self,
                400,
                {
                    "error": "insufficient_funds",
                    "available": str(avail),
                    "required": str(bid_price),
                },
            )
        ref = f"nft-bid-lock-{listing_id}-{int(time.time()*1000)}"
        ok, msg = _wallet_transfer(
            amount=bid_price,
            asset=bid_asset,
            from_wallet=opex,
            to_wallet=NFT_FEES_WALLET,
            ref=ref,
            description=f"nft-marketplace-bid listing={listing_id}",
            category="TRADE",
        )
        if not ok:
            return _json_resp(self, 502, {"error": "bid_lock_failed", "message": msg})

        with _db_lock, db() as c:
            cur = c.execute(
                "INSERT INTO nft_bids(listing_id, bidder_opex_user, bid_price, bid_asset, status, placed_at)"
                " VALUES (?,?,?,?, 'active', ?)",
                (listing_id, opex, str(bid_price), bid_asset, int(time.time())),
            )
            bid_id = cur.lastrowid
        history_log(
            contract=listing["contract_address"],
            token_id=listing["token_id"],
            event="bid",
            from_user=opex,
            to_user=None,
            price=str(bid_price),
            asset=bid_asset,
        )
        return _json_resp(self, 200, {"ok": True, "bid_id": bid_id})

    def _cancel_bid(self, bid_id: int):
        user = self._require_user()
        if not user:
            return
        opex = user["opex_user"]
        with _db_lock, db() as c:
            bid = c.execute("SELECT * FROM nft_bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                return _json_resp(self, 404, {"error": "no_such_bid"})
            if bid["bidder_opex_user"] != opex:
                return _json_resp(self, 403, {"error": "not_bidder"})
            if bid["status"] != "active":
                return _json_resp(self, 400, {"error": "not_active"})
        # Refund.
        ref = f"nft-bid-refund-{bid_id}-{int(time.time()*1000)}"
        ok, msg = _wallet_transfer(
            amount=Decimal(str(bid["bid_price"])),
            asset=bid["bid_asset"],
            from_wallet=NFT_FEES_WALLET,
            to_wallet=opex,
            ref=ref,
            description=f"nft-bid-refund {bid_id}",
            category="ADJUSTMENT",
        )
        if not ok:
            return _json_resp(self, 502, {"error": "refund_failed", "message": msg})
        with _db_lock, db() as c:
            c.execute("UPDATE nft_bids SET status='cancelled' WHERE id=?", (bid_id,))
        return _json_resp(self, 200, {"ok": True})

    def _accept_bid(self, bid_id: int):
        user = self._require_user()
        if not user:
            return
        opex = user["opex_user"]
        with _db_lock, db() as c:
            bid = c.execute("SELECT * FROM nft_bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                return _json_resp(self, 404, {"error": "no_such_bid"})
            if bid["status"] != "active":
                return _json_resp(self, 400, {"error": "bid_not_active"})
            listing = c.execute(
                "SELECT * FROM nft_listings WHERE id=?", (bid["listing_id"],)
            ).fetchone()
            if not listing:
                return _json_resp(self, 404, {"error": "no_such_listing"})
            if listing["seller_opex_user"] != opex:
                return _json_resp(self, 403, {"error": "not_seller"})
            if listing["status"] != "active":
                return _json_resp(self, 400, {"error": "listing_not_active"})

        # The buyer's funds are already locked in NFT_FEES_WALLET. We need to:
        #   - settle the chain transfer custodial -> bidder
        #   - move (locked - fee) from NFT_FEES_WALLET -> seller
        #   - mark bid 'accepted', listing 'sold', refund other open bids
        buyer_opex = bid["bidder_opex_user"]
        # We need the bidder's chain address — derive via chain.db. If unknown
        # we can't proceed (we'd need their bearer for /chain/wallet derive).
        bidder_addr = _user_address_from_chain_db(buyer_opex)
        if not bidder_addr:
            return _json_resp(
                self,
                400,
                {
                    "error": "bidder_address_unknown",
                    "message": "bidder must visit /app/wallet.html at least once so their on-chain address is derived",
                },
            )

        price = Decimal(str(bid["bid_price"]))
        asset_u = (bid["bid_asset"] or "USDT").upper()
        contract = listing["contract_address"]
        tid_i = int(listing["token_id"])
        qty_i = int(listing["quantity"])
        col = collection_by_address(contract) or {}
        standard = col.get("standard", "erc721")
        custody = custodial_address()
        listing_id = listing["id"]

        # On-chain transfer.
        try:
            if standard == "erc721":
                tx = send_erc721_transfer(
                    contract, custody, frm=custody, to=bidder_addr, token_id=tid_i
                )
            else:
                tx = send_erc1155_transfer(
                    contract,
                    custody,
                    frm=custody,
                    to=bidder_addr,
                    token_id=tid_i,
                    amount=qty_i,
                )
        except RpcError as e:
            return _json_resp(self, 502, {"error": "chain_transfer_failed", "message": str(e)})

        # Pay seller, less platform fee.
        fee_amount = (price * Decimal(PLATFORM_FEE_BPS) / Decimal(10000)).quantize(
            Decimal(1), rounding="ROUND_FLOOR"
        )
        seller_proceeds = price - fee_amount
        if seller_proceeds > 0:
            ref_pay = f"nft-accept-bid-pay-{bid_id}-{int(time.time()*1000)}"
            ok, msg = _wallet_transfer(
                amount=seller_proceeds,
                asset=asset_u,
                from_wallet=NFT_FEES_WALLET,
                to_wallet=opex,
                ref=ref_pay,
                description=f"nft-marketplace-bid-proceeds bid={bid_id}",
                category="TRADE",
            )
            if not ok:
                log.error("seller payout failed (bid accept): %s", msg)

        # Refund the *other* still-active bids on this listing (their funds
        # are also sitting in NFT_FEES_WALLET).
        with _db_lock, db() as c:
            other_bids = c.execute(
                "SELECT * FROM nft_bids WHERE listing_id=? AND status='active' AND id<>?",
                (listing_id, bid_id),
            ).fetchall()
        for ob in other_bids:
            ref_refund = f"nft-bid-loser-refund-{ob['id']}-{int(time.time()*1000)}"
            ok2, _ = _wallet_transfer(
                amount=Decimal(str(ob["bid_price"])),
                asset=ob["bid_asset"],
                from_wallet=NFT_FEES_WALLET,
                to_wallet=ob["bidder_opex_user"],
                ref=ref_refund,
                description=f"nft-loser-refund bid={ob['id']}",
                category="ADJUSTMENT",
            )
            with _db_lock, db() as c:
                c.execute("UPDATE nft_bids SET status='cancelled' WHERE id=?", (ob["id"],))

        # Mark sold + bid accepted.
        now = int(time.time())
        with _db_lock, db() as c:
            c.execute(
                "UPDATE nft_listings SET status='sold', sold_at=?, buyer_opex_user=?, trade_tx=?"
                " WHERE id=?",
                (now, buyer_opex, tx, listing_id),
            )
            c.execute("UPDATE nft_bids SET status='accepted' WHERE id=?", (bid_id,))
        history_log(
            contract=contract,
            token_id=tid_i,
            event="sale",
            from_user=opex,
            to_user=buyer_opex,
            price=str(price),
            asset=asset_u,
            tx=tx,
        )
        usdt_volume = price
        if asset_u == "ETH":
            usdt_volume = price * Decimal(os.environ.get("ZETH_USDT_PRICE_HINT", "3000"))
        update_collection_volume(contract, usdt_volume)
        recompute_floor_price(contract)
        return _json_resp(
            self,
            200,
            {
                "ok": True,
                "trade_tx": tx,
                "price": str(price),
                "asset": asset_u,
                "fee_amount": str(fee_amount),
                "seller_proceeds": str(seller_proceeds),
            },
        )

    def _mint_sample(self):
        """Demo-only: transfer one of the custodial's spare ERC721 tokens to
        the caller. Picks the lowest-id token that's still owned by the
        custodial address.
        """
        user = self._require_user()
        if not user:
            return
        opex = user["opex_user"]
        token = _bearer(self)
        try:
            user_addr = ensure_user_address(opex, token=token)
        except Exception as e:
            return _json_resp(self, 502, {"error": "wallet_derivation_failed", "message": str(e)})

        # Find a free ZkNFT (ERC721) currently owned by custody.
        custody = custodial_address()
        cols_721 = [c for c in known_collections() if c.get("standard") == "erc721"]
        if not cols_721:
            return _json_resp(self, 500, {"error": "no_erc721_collection_configured"})
        col = cols_721[0]
        contract = str(col["address"]).lower()
        total = int(col.get("total_supply") or 100)
        chosen_id: int | None = None
        for tid in range(1, total + 1):
            try:
                owner = erc721_owner_of(contract, tid)
            except Exception as e:  # noqa: BLE001
                log.debug("custody ownerOf skipped contract=%s token=%s: %r", contract, tid, e)
                continue
            if owner == custody:
                chosen_id = tid
                break
        if chosen_id is None:
            return _json_resp(self, 410, {"error": "no_sample_tokens_left"})

        try:
            tx = send_erc721_transfer(
                contract, custody, frm=custody, to=user_addr, token_id=chosen_id
            )
        except RpcError as e:
            return _json_resp(self, 502, {"error": "transfer_failed", "message": str(e)})

        history_log(
            contract=contract,
            token_id=chosen_id,
            event="mint",
            from_user="zkcex-custodial",
            to_user=opex,
            tx=tx,
        )
        meta = _load_token_meta(contract, chosen_id)
        return _json_resp(
            self,
            200,
            {
                "ok": True,
                "tx": tx,
                "contract_address": contract,
                "token_id": str(chosen_id),
                "to_address": user_addr,
                "metadata": meta,
            },
        )

    # ---- helpers ----

    def _require_user(self) -> dict | None:
        token = _bearer(self)
        if not token:
            _json_resp(self, 401, {"error": "missing_bearer"})
            return None
        user = resolve_user(token)
        if not user:
            _json_resp(self, 401, {"error": "unauthorized"})
            return None
        return user


# ----- server bootstrap ----------------------------------------------------


class ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _bootstrap_fees_wallet() -> None:
    """Make sure the platform-fee MAIN wallet exists (best-effort).

    The wallet API auto-creates wallets when you POST a /deposit/.../<user>_MAIN
    so a one-token seed deposit is enough. We tolerate failures silently —
    /v2/transfer will surface them with a useful message later.
    """
    for asset in ("USDT", "ETH"):
        try:
            _wallet_seed_deposit(NFT_FEES_WALLET, asset, 0)
        except Exception as e:  # noqa: BLE001
            log.debug("fee wallet seed skipped for %s: %r", asset, e)


def main() -> None:
    init_db()
    upsert_collections_from_deployment()
    # Forward uncaught exceptions to the central error_collector.
    try:
        from _error_reporter import install_global_handler  # type: ignore

        install_global_handler()
    except Exception as e:  # noqa: BLE001
        log.debug("error reporter install skipped: %r", e)

    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT

    # Best-effort bootstrap of the platform-fee wallet (separate thread so we
    # don't block startup on a slow wallet API).
    threading.Thread(target=_bootstrap_fees_wallet, daemon=True, name="nft-fees-boot").start()

    with ThreadingServer(("127.0.0.1", port), Handler) as srv:
        log.info(
            "nft marketplace listening on :%d (db=%s, rpc=%s)",
            port,
            DB_PATH,
            RPC_URL,
        )
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
