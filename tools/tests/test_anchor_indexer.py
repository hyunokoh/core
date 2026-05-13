"""Unit tests for ``tools/anchor_indexer.py``.

These tests don't need a running EVM. ``urllib.request.urlopen`` is stubbed
out so a single ``_scan_once()`` tick produces canned eth_blockNumber /
eth_getLogs / eth_getBlockByNumber responses.

Run with:  python3 -m unittest core.tools.tests.test_anchor_indexer -v
"""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.abspath(os.path.join(HERE, ".."))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)


# Test fixtures: matches BatchAppended and CommitmentPosted layouts.
# topic[0] is derived in setUp from the live module so we don't repeat the
# keccak constant here.

# Pre-baked values used in the fake responses.
_TOKEN_KEY = "0x" + "11" * 32  # bytes32 token key
_BATCH_HASH = "0x" + "aa" * 32
_TX_HASH = "0x" + "bb" * 32
_ADDR_KEY_1 = "0x" + "c1" * 32
_ADDR_KEY_2 = "0x" + "c2" * 32
_ADDR_KEY_3 = "0x" + "c3" * 32


def _i256_to_hex(n: int) -> str:
    """Two's complement int256 as 64-char hex (no 0x)."""
    if n < 0:
        n = (1 << 256) + n
    return hex(n & ((1 << 256) - 1))[2:].rjust(64, "0")


def _u256_to_hex(n: int) -> str:
    return hex(n)[2:].rjust(64, "0")


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class AnchorIndexerScanOnceTest(unittest.TestCase):
    def setUp(self) -> None:
        # Each test gets its own temp DB and a fresh contract config.
        self.tmpdir = tempfile.mkdtemp(prefix="anchor-idx-test-")
        self.db_path = os.path.join(self.tmpdir, "anchor.db")
        os.environ["ANCHOR_DB_PATH"] = self.db_path
        os.environ["BULLETIN_BOARD_ADDRESS"] = "0x" + "ab" * 20
        os.environ["BULLETIN_BOARD_START_BLOCK"] = "0"
        os.environ["BULLETIN_BOARD_RPC"] = "http://127.0.0.1:65535"  # bogus, urlopen is stubbed
        os.environ["ANCHOR_CONFIRMATIONS"] = "0"
        os.environ["ANCHOR_GETLOGS_RANGE"] = "10000"

        # Reload the module so env vars take effect and module-level globals
        # (CONTRACT_ADDRESS, DB_PATH, etc.) re-init against the temp dir.
        if "anchor_indexer" in sys.modules:
            del sys.modules["anchor_indexer"]
        self.mod = importlib.import_module("anchor_indexer")

        # Wipe any leftover state so counts start from zero.
        self.mod._BLOCK_TS_CACHE.clear()

    def tearDown(self) -> None:
        # Drop the temp file. The tempdir itself is cheap to leave.
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass
        # Reset env so the next test isn't influenced.
        for k in (
            "ANCHOR_DB_PATH",
            "BULLETIN_BOARD_ADDRESS",
            "BULLETIN_BOARD_START_BLOCK",
            "BULLETIN_BOARD_RPC",
            "ANCHOR_CONFIRMATIONS",
            "ANCHOR_GETLOGS_RANGE",
        ):
            os.environ.pop(k, None)

    # --- helpers ------------------------------------------------------

    def _make_responder(self):
        """Return a callable urlopen replacement that dispatches by JSON-RPC method."""
        topic_batch = self.mod.TOPIC_BATCH_APPENDED
        topic_commit = self.mod.TOPIC_COMMITMENT_POSTED

        # The fixture: at block 100 a single appendBatch tx emits one
        # BatchAppended and three CommitmentPosted events.
        head_block_hex = hex(100)
        block_ts = 1715000000
        liability_new = 12345

        batch_log = {
            "blockNumber": hex(100),
            "transactionHash": _TX_HASH,
            "logIndex": "0x0",
            "address": os.environ["BULLETIN_BOARD_ADDRESS"],
            "topics": [topic_batch, _TOKEN_KEY, _BATCH_HASH],
            "data": "0x" + _i256_to_hex(liability_new),
        }

        def comm_log(idx: int, addr_key: str, x: int, y: int):
            return {
                "blockNumber": hex(100),
                "transactionHash": _TX_HASH,
                "logIndex": hex(idx),
                "address": os.environ["BULLETIN_BOARD_ADDRESS"],
                "topics": [topic_commit, _TOKEN_KEY, addr_key],
                "data": "0x" + _u256_to_hex(x) + _u256_to_hex(y),
            }

        commits = [
            comm_log(1, _ADDR_KEY_1, 11, 12),
            comm_log(2, _ADDR_KEY_2, 21, 22),
            comm_log(3, _ADDR_KEY_3, 31, 32),
        ]
        logs = [batch_log] + commits

        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            body = json.loads(req.data.decode("utf-8"))
            method = body.get("method")
            rid = body.get("id")
            if method == "eth_blockNumber":
                return FakeResponse({"jsonrpc": "2.0", "id": rid, "result": head_block_hex})
            if method == "eth_getLogs":
                return FakeResponse({"jsonrpc": "2.0", "id": rid, "result": logs})
            if method == "eth_getBlockByNumber":
                return FakeResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {"timestamp": hex(block_ts), "number": body["params"][0]},
                    }
                )
            return FakeResponse({"jsonrpc": "2.0", "id": rid, "result": None})

        return fake_urlopen

    # --- the actual test cases ----------------------------------------

    def test_keccak_topics(self):
        # Sanity check: topic[0] for BatchAppended matches the well-known
        # keccak of its signature string.
        self.assertEqual(len(self.mod.TOPIC_BATCH_APPENDED), 66)
        self.assertTrue(self.mod.TOPIC_BATCH_APPENDED.startswith("0x"))
        self.assertNotEqual(self.mod.TOPIC_BATCH_APPENDED, self.mod.TOPIC_COMMITMENT_POSTED)

    def test_keccak_empty_vector(self):
        self.assertEqual(
            self.mod._keccak256(b"").hex(),
            "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470",
        )

    def test_keccak_abc_vector(self):
        self.assertEqual(
            self.mod._keccak256(b"abc").hex(),
            "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45",
        )

    def test_addr_key_matches_zkpol_sha256(self):
        # zkPoL derives addrKey = sha256(account_id_bytes). The bridge logs a
        # pending change with expected_addr_key computed by the same recipe.
        import hashlib

        expected = "0x" + hashlib.sha256(b"alice").hexdigest()
        self.assertEqual(self.mod._opex_to_addr_key("alice"), expected)

    def test_scan_once_inserts_batch_and_commitments(self):
        # Pre-seed a pending_change for one of the addr_keys so we can verify
        # resolution.
        import hashlib  # noqa: F401 — for documentation only
        import time as _time

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pending_changes ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " opex_user TEXT, asset TEXT, delta TEXT,"
            " expected_addr_key TEXT, inserted_at INTEGER,"
            " resolved_tx_hash TEXT, resolved_at INTEGER)"
        )
        conn.commit()
        conn.close()

        # Open via the module so the schema for everything else lines up.
        conn = self.mod._open_db()
        conn.execute(
            "INSERT INTO pending_changes "
            "(opex_user, asset, delta, expected_addr_key, inserted_at) "
            "VALUES (?,?,?,?,?)",
            ("alice", "USDT", "100", _ADDR_KEY_2, int(_time.time()) - 10),
        )
        conn.close()

        with mock.patch("urllib.request.urlopen", side_effect=self._make_responder()):
            self.mod._scan_once()

        # Verify counts.
        conn = sqlite3.connect(self.db_path)
        n_batches = conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
        n_commits = conn.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]
        self.assertEqual(n_batches, 1, "exactly one batch should be persisted")
        self.assertEqual(n_commits, 3, "all three commitments should be persisted")

        # Verify batch contents.
        row = conn.execute(
            "SELECT batch_hash, token_key, liability_new, delta, tx_hash FROM batches"
        ).fetchone()
        self.assertEqual(row[0], _BATCH_HASH)
        self.assertEqual(row[1], _TOKEN_KEY)
        self.assertEqual(row[2], "12345")  # liability_new
        # First batch ever, liability_old = 0, so delta == liability_new.
        self.assertEqual(row[3], "12345")
        self.assertEqual(row[4], _TX_HASH)

        # Commitments should join to the batch via tx_hash.
        cm_batches = [r[0] for r in conn.execute("SELECT batch_hash FROM commitments").fetchall()]
        self.assertEqual(set(cm_batches), {_BATCH_HASH})

        # The pending_change for ADDR_KEY_2 should be resolved.
        resolved = conn.execute(
            "SELECT resolved_tx_hash FROM pending_changes WHERE expected_addr_key=?",
            (_ADDR_KEY_2,),
        ).fetchone()
        self.assertEqual(resolved[0], _TX_HASH)
        conn.close()

        # The IndexerState should have non-zero counts now.
        self.assertEqual(self.mod._STATE.batches_count, 1)
        self.assertEqual(self.mod._STATE.commitments_count, 3)
        self.assertEqual(self.mod._STATE.pending_open_count, 0)
        self.assertTrue(self.mod._STATE.last_tick_ok)

    def test_degraded_mode_when_address_unset(self):
        # Reload the module without BULLETIN_BOARD_ADDRESS.
        os.environ.pop("BULLETIN_BOARD_ADDRESS", None)
        del sys.modules["anchor_indexer"]
        mod = importlib.import_module("anchor_indexer")
        self.assertFalse(mod._STATE.configured)
        health = mod._STATE.as_dict()
        self.assertTrue(health["degraded"])
        self.assertIn("BulletinBoard not configured", health["message"] or "")


if __name__ == "__main__":
    unittest.main()
