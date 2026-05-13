"""Custody coordinator.

This is the service the exchange app (chain_server.py) talks to instead of
calling `eth_sendTransaction` with an unlocked dev account. The coordinator:

  1. Receives an unsigned EIP-1559 transaction.
  2. Fetches a Shamir share from each registered signer node, in parallel.
  3. As soon as `threshold` distinct shares are in, runs Lagrange
     interpolation to reconstruct the secp256k1 private key.
  4. Verifies that keccak256(secp256k1_pub(reconstructed))[12:] equals the
     publicly-pinned custodial address. If not, aborts (corrupted shard).
  5. Signs the transaction (deterministic ECDSA, low-S, EIP-1559 envelope).
  6. Broadcasts via eth_sendRawTransaction to the chain RPC supplied in the
     request.
  7. **Wipes** the reconstructed key from memory.
  8. Returns the broadcast tx hash + signature metadata to the caller.

Endpoints:

  GET /health
        -> {"ok": true, "n_nodes_reachable": int, "threshold": int,
            "m_of_n": "3-of-5", ...}

  POST /sign-and-broadcast    (Bearer-token-protected)
        Body: {
          "rpc_url": "http://127.0.0.1:8545",
          "from": "0x...",          # for sanity-check (must equal pinned addr)
          "to": "0x...",
          "value_hex": "0x0",
          "data_hex": "0x...",
          "gas_hex": "0xf4240",
          "nonce_hex": "0x0",       # if omitted, fetched via eth_getTransactionCount
          "chain_id": 31337,
          "max_fee_per_gas_hex": "0x...",          # optional
          "max_priority_fee_per_gas_hex": "0x..."  # optional
        }
        Returns: {
          "tx": "0x...",
          "signed_raw_tx": "0x02...",
          "signature": {"r": "0x..", "s": "0x..", "v": 0|1},
          "signer_addr_check": "0x..."   # the address derived after combine
        }

  GET /audit?limit=50         (Bearer-token-protected)
        Returns the last N sign-request audit rows, redacted.

WARNING — read this if you are reviewing this code for production:

  - This implementation reconstructs the key in-memory then signs. That is a
    single point of compromise: anyone with code-exec on the coordinator
    machine could exfiltrate the key during the brief window. Mitigations
    here (zeroing memory, audit log) are best-effort. Real production
    custody uses HSMs or threshold ECDSA where the full key never exists in
    one place.
  - We do best-effort key zeroing via bytearray overwrite. CPython does
    not expose a guaranteed secure-zero primitive — copies in interpreter
    state may persist until garbage collection.
  - Node->coordinator traffic is loopback-only (127.0.0.1) for the demo.
    Production must use mTLS with mutually pinned client certs.
"""

from __future__ import annotations

import argparse
import http.server
import json
import logging
import os
import socketserver
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

# Allow direct invocation as a script (python3 tools/custody/coordinator.py)
# as well as module form (-m tools.custody.coordinator).
if __package__ in (None, ""):
    HERE = os.path.dirname(os.path.abspath(__file__))
    PARENT = os.path.dirname(HERE)
    if PARENT not in sys.path:
        sys.path.insert(0, PARENT)
    from custody import ethsign as _ethsign  # type: ignore
    from custody import shamir as _shamir  # type: ignore
else:
    from . import ethsign as _ethsign
    from . import shamir as _shamir

log = logging.getLogger("zkcex.custody.coordinator")


def _validated_http_url(raw_url: str, *, name: str = "url") -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return raw_url


# ----- audit DB ----------------------------------------------------------

_DB_LOCK = threading.Lock()


def _db(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _init_db(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _db(path) as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS sign_requests (
              request_id     TEXT PRIMARY KEY,
              unsigned_tx_json TEXT NOT NULL,
              signer_addr    TEXT NOT NULL,
              tx_hash        TEXT,
              status         TEXT NOT NULL,
              partial_count  INTEGER,
              threshold      INTEGER,
              requested_at   INTEGER NOT NULL,
              completed_at   INTEGER,
              error          TEXT
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS ix_sign_requests_at " "ON sign_requests(requested_at DESC)"
        )
        c.commit()


def _audit_insert(path: str, row: dict) -> None:
    with _DB_LOCK, _db(path) as c:
        c.execute(
            "INSERT OR REPLACE INTO sign_requests"
            "(request_id, unsigned_tx_json, signer_addr, tx_hash, status,"
            " partial_count, threshold, requested_at, completed_at, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                row["request_id"],
                row["unsigned_tx_json"],
                row["signer_addr"],
                row.get("tx_hash"),
                row["status"],
                row.get("partial_count"),
                row.get("threshold"),
                row["requested_at"],
                row.get("completed_at"),
                row.get("error"),
            ),
        )
        c.commit()


def _audit_recent(path: str, limit: int) -> list[dict]:
    with _DB_LOCK, _db(path) as c:
        rows = c.execute(
            "SELECT request_id, unsigned_tx_json, signer_addr, tx_hash, status,"
            "       partial_count, threshold, requested_at, completed_at, error"
            "  FROM sign_requests ORDER BY requested_at DESC LIMIT ?",
            (max(1, min(500, int(limit))),),
        ).fetchall()
    out = []
    for r in rows:
        try:
            tx = json.loads(r["unsigned_tx_json"])
        except Exception:
            tx = None
        out.append(
            {
                "request_id": r["request_id"],
                "signer_addr": r["signer_addr"],
                "tx_hash": r["tx_hash"],
                "status": r["status"],
                "partial_count": r["partial_count"],
                "threshold": r["threshold"],
                "requested_at": r["requested_at"],
                "completed_at": r["completed_at"],
                "error": r["error"],
                "unsigned_tx": _redact_tx(tx) if isinstance(tx, dict) else None,
            }
        )
    return out


def _redact_tx(tx: dict) -> dict:
    """Drop any internal-only or sensitive fields from the audit copy."""
    safe_keys = (
        "rpc_url",
        "from",
        "to",
        "value_hex",
        "data_hex",
        "gas_hex",
        "nonce_hex",
        "chain_id",
        "max_fee_per_gas_hex",
        "max_priority_fee_per_gas_hex",
    )
    return {k: tx[k] for k in safe_keys if k in tx}


# ----- coordinator state -------------------------------------------------


class CoordinatorState:
    def __init__(
        self,
        *,
        node_endpoints: list[dict],
        threshold: int,
        total: int,
        custodial_addr_pinned: str,
        coordinator_token: str,
        db_path: str,
    ) -> None:
        # node_endpoints is list of {"node_id", "url", "token"}.
        self.node_endpoints = node_endpoints
        self.threshold = int(threshold)
        self.total = int(total)
        self.custodial_addr_pinned = custodial_addr_pinned.lower()
        self.coordinator_token = coordinator_token
        self.db_path = db_path

    def m_of_n(self) -> str:
        return f"{self.threshold}-of-{self.total}"


# ----- node interaction --------------------------------------------------


def _http_get_json(url: str, timeout: float) -> Any:
    url = _validated_http_url(url)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


def _http_post_json(url: str, body: dict, *, bearer: str | None, timeout: float) -> tuple[int, Any]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    raw = json.dumps(body).encode()
    url = _validated_http_url(url)
    req = urllib.request.Request(url, data=raw, method="POST", headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, None


def _probe_nodes(state: CoordinatorState, *, timeout: float = 2.0) -> list[dict]:
    out = []
    for node in state.node_endpoints:
        try:
            t0 = time.time()
            j = _http_get_json(node["url"].rstrip("/") + "/health", timeout=timeout)
            latency = int((time.time() - t0) * 1000)
            out.append(
                {
                    "node_id": node["node_id"],
                    "url": node["url"],
                    "share_index": j.get("share_index"),
                    "healthy": bool(j.get("ok")),
                    "last_response_ms": latency,
                }
            )
        except Exception as e:
            out.append(
                {
                    "node_id": node["node_id"],
                    "url": node["url"],
                    "share_index": None,
                    "healthy": False,
                    "last_response_ms": None,
                    "error": str(e)[:120],
                }
            )
    return out


def _request_partial(
    node: dict, *, request_id: str, tx_hash_hex: str, timeout: float
) -> tuple[int, bytes] | None:
    """Returns (share_index, share_payload_bytes) on success, or None."""
    code, body = _http_post_json(
        node["url"].rstrip("/") + "/sign-partial",
        {"request_id": request_id, "tx_hash_hex": tx_hash_hex},
        bearer=node["token"],
        timeout=timeout,
    )
    if code != 200 or not isinstance(body, dict):
        log.warning(
            "node %s sign-partial code=%s body=%s", node.get("node_id"), code, str(body)[:120]
        )
        return None
    try:
        idx = int(body["share_index"])
        payload = bytes.fromhex(body["share_payload_hex"])
        if len(payload) != 32:
            return None
        return idx, payload
    except Exception:
        return None


def _collect_shares(
    state: CoordinatorState, *, request_id: str, pre_image_hash_hex: str
) -> list[tuple[int, bytes]]:
    """Fetch shares from all nodes in parallel, return when threshold met.

    We use first-N-wins semantics — the first `threshold` distinct share
    indices to come back are used. We still drain all in-flight responses
    for clean shutdown but only the first `threshold` are returned.
    """
    collected: list[tuple[int, bytes]] = []
    seen_idx: set[int] = set()
    threshold = state.threshold
    nodes = state.node_endpoints
    if not nodes:
        return []
    timeout_s = 5.0

    with ThreadPoolExecutor(max_workers=max(1, len(nodes))) as ex:
        futures = {
            ex.submit(
                _request_partial,
                n,
                request_id=request_id,
                tx_hash_hex=pre_image_hash_hex,
                timeout=timeout_s,
            ): n
            for n in nodes
        }
        for fut in as_completed(futures):
            try:
                res = fut.result()
            except Exception as e:
                log.warning("node future raised: %s", e)
                continue
            if not res:
                continue
            idx, payload = res
            if idx in seen_idx:
                continue
            seen_idx.add(idx)
            collected.append((idx, payload))
            if len(collected) >= threshold:
                # We have our quorum; cancel remaining (best effort).
                for f, _n in futures.items():
                    if not f.done():
                        f.cancel()
                break
    return collected


# ----- key reconstruction + zeroing --------------------------------------


def _zero_bytearray(b: bytearray) -> None:
    """Best-effort overwrite of a mutable byte buffer with zeros.

    CPython does not expose a guaranteed secure-zero. After this call we
    also explicitly del the reference so the GC can reclaim it. There is
    still no protection against page-cache or core-dump exposure — that's
    why real custody uses HSMs.
    """
    for i in range(len(b)):
        b[i] = 0


def _reconstruct_and_sign(
    state: CoordinatorState,
    *,
    shares: list[tuple[int, bytes]],
    tx_params: dict,
) -> dict:
    """Reconstruct private key, verify address pin, sign tx, zero key.

    Returns: { "raw_tx": "0x...", "tx_hash": "0x...", "r":..., "s":...,
               "v":..., "signer_addr_check": "0x..." }
    Raises ValueError on address pin mismatch.
    """
    secret = _shamir.combine(shares)  # bytes (immutable)
    # Move into a mutable buffer so we can zero it after use.
    key_buf = bytearray(secret)
    try:
        derived_addr = _ethsign.privkey_to_address(bytes(key_buf))
        if derived_addr.lower() != state.custodial_addr_pinned:
            raise ValueError(
                f"reconstructed key does not match pinned custodial address: "
                f"derived={derived_addr} pinned={state.custodial_addr_pinned}"
            )
        signed = _ethsign.sign_eip1559_tx(
            bytes(key_buf),
            chain_id=tx_params["chain_id"],
            nonce=tx_params["nonce_hex"],
            max_priority_fee_per_gas=tx_params["max_priority_fee_per_gas_hex"],
            max_fee_per_gas=tx_params["max_fee_per_gas_hex"],
            gas_limit=tx_params["gas_hex"],
            to=tx_params["to"],
            value=tx_params.get("value_hex", "0x0"),
            data=tx_params.get("data_hex", "0x"),
        )
        signed["signer_addr_check"] = derived_addr
        return signed
    finally:
        _zero_bytearray(key_buf)
        del key_buf
        # Best-effort overwrite of the original immutable secret too — we
        # cannot truly zero `bytes`, but we can drop the reference.
        del secret


# ----- chain RPC helpers (just what we need) -----------------------------


def _rpc(
    rpc_url: str, method: str, params: list[Any] | None = None, *, timeout: float = 10.0
) -> Any:
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or [],
        }
    ).encode()
    rpc_url = _validated_http_url(rpc_url, name="rpc_url")
    req = urllib.request.Request(  # noqa: S310
        rpc_url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        body = json.loads(resp.read().decode())
    if "error" in body and body["error"]:
        raise RuntimeError(f"rpc error: {body['error']}")
    return body.get("result")


def _fill_tx_defaults(rpc_url: str, tx: dict, signer_addr: str) -> dict:
    """Look up nonce / fees from the chain when not supplied."""
    out = dict(tx)
    if not out.get("nonce_hex"):
        nonce = _rpc(rpc_url, "eth_getTransactionCount", [signer_addr, "pending"])
        out["nonce_hex"] = nonce
    # EIP-1559 fees: ask the node if not supplied.
    if not out.get("max_priority_fee_per_gas_hex"):
        try:
            tip = _rpc(rpc_url, "eth_maxPriorityFeePerGas", [])
            out["max_priority_fee_per_gas_hex"] = tip
        except Exception:
            out["max_priority_fee_per_gas_hex"] = "0x3b9aca00"  # 1 gwei
    if not out.get("max_fee_per_gas_hex"):
        try:
            base = _rpc(rpc_url, "eth_gasPrice", [])
            # Pad: 2 * gasPrice + tip
            base_i = int(base, 16)
            tip_i = int(out["max_priority_fee_per_gas_hex"], 16)
            out["max_fee_per_gas_hex"] = hex(base_i * 2 + tip_i)
        except Exception:
            out["max_fee_per_gas_hex"] = "0x77359400"  # 2 gwei
    if not out.get("gas_hex"):
        out["gas_hex"] = "0xf4240"  # 1,000,000
    if not out.get("value_hex"):
        out["value_hex"] = "0x0"
    if not out.get("data_hex"):
        out["data_hex"] = "0x"
    return out


def _broadcast_raw(rpc_url: str, raw_tx_hex: str) -> str:
    return _rpc(rpc_url, "eth_sendRawTransaction", [raw_tx_hex])


# ----- HTTP layer --------------------------------------------------------


def _json_resp(handler, code: int, obj) -> None:
    body = json.dumps(obj).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler) -> dict:
    n = int(handler.headers.get("Content-Length") or 0)
    if n <= 0:
        return {}
    raw = handler.rfile.read(n)
    try:
        return json.loads(raw.decode())
    except Exception:
        return {}


def _bearer_ok(handler, expected: str) -> bool:
    auth = handler.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return False
    return auth[len("Bearer ") :].strip() == expected


def make_handler(state: CoordinatorState):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.info("%s - %s", self.client_address[0], fmt % args)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/health":
                probe = _probe_nodes(state)
                reachable = sum(1 for n in probe if n["healthy"])
                ok = reachable >= state.threshold
                return _json_resp(
                    self,
                    200,
                    {
                        "ok": ok,
                        "n_nodes_reachable": reachable,
                        "threshold": state.threshold,
                        "total": state.total,
                        "m_of_n": state.m_of_n(),
                        "custodial_addr": state.custodial_addr_pinned,
                        "nodes": probe,
                    },
                )
            if parsed.path == "/audit":
                if not _bearer_ok(self, state.coordinator_token):
                    return _json_resp(self, 401, {"error": "unauthorized"})
                qs = urllib.parse.parse_qs(parsed.query)
                limit = int((qs.get("limit") or ["50"])[0])
                rows = _audit_recent(state.db_path, limit)
                return _json_resp(self, 200, {"rows": rows, "count": len(rows)})
            if parsed.path == "/audit-public":
                # Public, redacted variant for the /app/custody.html dashboard.
                # We strip the unsigned-tx body even further (just keep the
                # to/value/asset-shaped bits).
                rows = _audit_recent(state.db_path, 50)
                public = []
                for r in rows:
                    tx = r.get("unsigned_tx") or {}
                    public.append(
                        {
                            "request_id": r["request_id"][:8] + "...",
                            "to": tx.get("to"),
                            "value_hex": tx.get("value_hex"),
                            "tx_hash": r["tx_hash"],
                            "status": r["status"],
                            "partial_count": r["partial_count"],
                            "threshold": r["threshold"],
                            "requested_at": r["requested_at"],
                            "completed_at": r["completed_at"],
                        }
                    )
                return _json_resp(self, 200, {"rows": public})
            return _json_resp(self, 404, {"error": "not_found"})

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/sign-and-broadcast":
                return _json_resp(self, 404, {"error": "not_found"})
            if not _bearer_ok(self, state.coordinator_token):
                return _json_resp(self, 401, {"error": "unauthorized"})
            body = _read_json(self)
            request_id = body.get("request_id") or str(uuid.uuid4())
            rpc_url = (body.get("rpc_url") or "").strip()
            from_addr = (body.get("from") or state.custodial_addr_pinned).lower()
            to_addr = body.get("to")
            chain_id = body.get("chain_id")
            if not rpc_url or not to_addr or chain_id is None:
                return _json_resp(
                    self,
                    400,
                    {
                        "error": "bad_request",
                        "expected": "{rpc_url, to, chain_id, ...}",
                    },
                )
            if from_addr.lower() != state.custodial_addr_pinned:
                return _json_resp(
                    self,
                    403,
                    {
                        "error": "from_addr_mismatch",
                        "expected": state.custodial_addr_pinned,
                    },
                )

            # Persist 'pending' before any network calls — we want every sign
            # request, including failures, captured in the audit log.
            unsigned_tx = {
                "rpc_url": rpc_url,
                "from": from_addr,
                "to": to_addr,
                "chain_id": chain_id,
                "value_hex": body.get("value_hex"),
                "data_hex": body.get("data_hex"),
                "gas_hex": body.get("gas_hex"),
                "nonce_hex": body.get("nonce_hex"),
                "max_fee_per_gas_hex": body.get("max_fee_per_gas_hex"),
                "max_priority_fee_per_gas_hex": body.get("max_priority_fee_per_gas_hex"),
            }
            requested_at = int(time.time())
            _audit_insert(
                state.db_path,
                {
                    "request_id": request_id,
                    "unsigned_tx_json": json.dumps(unsigned_tx),
                    "signer_addr": state.custodial_addr_pinned,
                    "status": "pending",
                    "threshold": state.threshold,
                    "requested_at": requested_at,
                },
            )

            # Fill defaults from chain (nonce, fees, gas).
            try:
                tx_params = _fill_tx_defaults(rpc_url, unsigned_tx, state.custodial_addr_pinned)
            except Exception as e:
                _audit_insert(
                    state.db_path,
                    {
                        "request_id": request_id,
                        "unsigned_tx_json": json.dumps(unsigned_tx),
                        "signer_addr": state.custodial_addr_pinned,
                        "status": "failed",
                        "threshold": state.threshold,
                        "requested_at": requested_at,
                        "completed_at": int(time.time()),
                        "error": f"rpc_defaults_failed: {e}",
                    },
                )
                return _json_resp(self, 502, {"error": "rpc_unavailable", "message": str(e)})

            # Compute pre-image hash for the partial-share request:
            # Even though signer nodes do not actually use the tx hash to
            # compute a partial signature (they just return the share), we
            # include it so audit trails on each node tie the share release
            # to the txn that consumed it.
            pre_image_payload = json.dumps(
                {
                    "rid": request_id,
                    "to": tx_params["to"],
                    "v": tx_params["value_hex"],
                    "n": tx_params["nonce_hex"],
                    "c": tx_params["chain_id"],
                },
                sort_keys=True,
            ).encode()
            pre_image_hash = "0x" + _ethsign.keccak256(pre_image_payload).hex()

            t0 = time.time()
            shares = _collect_shares(
                state,
                request_id=request_id,
                pre_image_hash_hex=pre_image_hash,
            )
            collect_ms = int((time.time() - t0) * 1000)

            if len(shares) < state.threshold:
                _audit_insert(
                    state.db_path,
                    {
                        "request_id": request_id,
                        "unsigned_tx_json": json.dumps(unsigned_tx),
                        "signer_addr": state.custodial_addr_pinned,
                        "status": "failed",
                        "partial_count": len(shares),
                        "threshold": state.threshold,
                        "requested_at": requested_at,
                        "completed_at": int(time.time()),
                        "error": f"insufficient_shares: {len(shares)}/{state.threshold}",
                    },
                )
                return _json_resp(
                    self,
                    503,
                    {
                        "error": "insufficient_quorum",
                        "got": len(shares),
                        "needed": state.threshold,
                    },
                )

            try:
                signed = _reconstruct_and_sign(
                    state,
                    shares=shares,
                    tx_params=tx_params,
                )
            except ValueError as e:
                _audit_insert(
                    state.db_path,
                    {
                        "request_id": request_id,
                        "unsigned_tx_json": json.dumps(unsigned_tx),
                        "signer_addr": state.custodial_addr_pinned,
                        "status": "failed",
                        "partial_count": len(shares),
                        "threshold": state.threshold,
                        "requested_at": requested_at,
                        "completed_at": int(time.time()),
                        "error": f"reconstruct_failed: {e}",
                    },
                )
                return _json_resp(
                    self,
                    500,
                    {
                        "error": "reconstruct_failed",
                        "message": str(e),
                    },
                )

            try:
                tx_hash = _broadcast_raw(rpc_url, signed["raw_tx"])
            except Exception as e:
                _audit_insert(
                    state.db_path,
                    {
                        "request_id": request_id,
                        "unsigned_tx_json": json.dumps(unsigned_tx),
                        "signer_addr": state.custodial_addr_pinned,
                        "status": "failed",
                        "partial_count": len(shares),
                        "threshold": state.threshold,
                        "requested_at": requested_at,
                        "completed_at": int(time.time()),
                        "error": f"broadcast_failed: {e}",
                    },
                )
                return _json_resp(
                    self,
                    502,
                    {
                        "error": "broadcast_failed",
                        "message": str(e),
                    },
                )

            _audit_insert(
                state.db_path,
                {
                    "request_id": request_id,
                    "unsigned_tx_json": json.dumps(unsigned_tx),
                    "signer_addr": state.custodial_addr_pinned,
                    "tx_hash": tx_hash,
                    "status": "broadcast",
                    "partial_count": len(shares),
                    "threshold": state.threshold,
                    "requested_at": requested_at,
                    "completed_at": int(time.time()),
                },
            )

            return _json_resp(
                self,
                200,
                {
                    "tx": tx_hash,
                    "signed_raw_tx": signed["raw_tx"],
                    "signature": {
                        "r": hex(signed["r"]),
                        "s": hex(signed["s"]),
                        "v": signed["v"],
                    },
                    "signer_addr_check": signed["signer_addr_check"],
                    "request_id": request_id,
                    "shares_collected": len(shares),
                    "collect_ms": collect_ms,
                },
            )

    return Handler


class _ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _load_node_endpoints_from_env() -> list[dict]:
    """Read CUSTODY_NODE_URLS + CUSTODY_NODE_TOKENS from env.

    URLs and tokens are comma-separated and same-length. Each token is the
    bearer token that node will accept on /sign-partial.
    """
    urls = [u.strip() for u in os.environ.get("CUSTODY_NODE_URLS", "").split(",") if u.strip()]
    toks = [t.strip() for t in os.environ.get("CUSTODY_NODE_TOKENS", "").split(",") if t.strip()]
    if len(urls) != len(toks):
        raise ValueError("CUSTODY_NODE_URLS and CUSTODY_NODE_TOKENS must have same length")
    out = []
    for i, (u, t) in enumerate(zip(urls, toks, strict=False)):
        out.append({"node_id": f"n{i}", "url": u, "token": t})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="zkCEX custody coordinator")
    ap.add_argument(
        "--port", type=int, default=int(os.environ.get("CUSTODY_COORDINATOR_PORT", "5530"))
    )
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--threshold", type=int, default=int(os.environ.get("CUSTODY_THRESHOLD", "3")))
    ap.add_argument("--total", type=int, default=int(os.environ.get("CUSTODY_TOTAL", "5")))
    ap.add_argument(
        "--public-json",
        default=os.environ.get("CUSTODY_PUBLIC_JSON", ""),
        help="path to public.json containing the pinned custodial address",
    )
    ap.add_argument(
        "--coordinator-token",
        default=os.environ.get("CUSTODY_COORDINATOR_TOKEN", ""),
        help="bearer token chain_server / audit clients must present",
    )
    ap.add_argument(
        "--db-path",
        default=os.environ.get(
            "CUSTODY_AUDIT_DB",
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..", ".local", "custody_audit.db"
            ),
        ),
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [coordinator] %(levelname)s %(message)s",
    )

    if not args.public_json:
        print("ERROR: --public-json (or CUSTODY_PUBLIC_JSON env) is required", file=sys.stderr)
        sys.exit(2)
    with open(args.public_json) as f:
        pub = json.load(f)
    custodial_addr = pub["address"]

    if not args.coordinator_token:
        print(
            "ERROR: --coordinator-token (or CUSTODY_COORDINATOR_TOKEN env) " "must be set",
            file=sys.stderr,
        )
        sys.exit(2)

    nodes = _load_node_endpoints_from_env()
    if len(nodes) < args.total:
        print(f"WARN: configured nodes={len(nodes)} but total={args.total}", file=sys.stderr)

    db_path = os.path.abspath(args.db_path)
    _init_db(db_path)

    state = CoordinatorState(
        node_endpoints=nodes,
        threshold=args.threshold,
        total=args.total,
        custodial_addr_pinned=custodial_addr,
        coordinator_token=args.coordinator_token,
        db_path=db_path,
    )
    handler = make_handler(state)
    with _ThreadingServer((args.bind, args.port), handler) as srv:
        log.info(
            "coordinator listening on %s:%d (m-of-n=%s, addr=%s, db=%s)",
            args.bind,
            args.port,
            state.m_of_n(),
            custodial_addr,
            db_path,
        )
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
