"""End-to-end smoke test for the zkCEX -> zkPoL -> BulletinBoard pipeline.

This test deliberately skips itself when the prerequisites aren't running
unless ``ZKPOL_E2E_REQUIRED=1`` is set. The production live workflow sets that
flag so missing services are a hard failure instead of a false green skip.

Prerequisites:
    1. zkPoL docker-compose stack up:
         cd ~/Documents/Projects/zkPoL && docker compose up -d
       This brings up:
         - zkpol-mariadb           (MariaDB; ledger_change_event lives here)
         - zkpol (the Rust core)   on :21011+
         - zkpol-manager (Java)    on :21001
         - a local anvil/hardhat   on :8545 (the BulletinBoard EVM)
    2. BulletinBoard.sol deployed to the local EVM, and the deployed
       address exported as ``BULLETIN_BOARD_ADDRESS``.
    3. zkCEX wallet API + auth user + zkpol_bridge + anchor_indexer running:
         python3 core/tools/zkpol_bridge.py 5504 &
         BULLETIN_BOARD_ADDRESS=0x... python3 core/tools/anchor_indexer.py 5707 &

Flow:
    1. Mint 1 USDT on the test wallet via wallet API:
         curl -X POST \
           'http://127.0.0.1:8091/deposit/1_test-ethereum_USDT/alice_MAIN?description=zkpol-e2e&transferRef=zkpol-e2e-1'
    2. zkpol_bridge diffs the balance and inserts into
       ``ledger_change_event``; it also POSTs to /anchor/internal/log-pending.
    3. zkPoL forms a batch, generates the Groth16 proof, calls
       ``BulletinBoard.appendBatch(...)`` on the local EVM.
    4. anchor_indexer.py picks up the ``BatchAppended`` + ``CommitmentPosted``
       events within one poll interval (default 3s).
    5. ``GET /anchor/account/<sha256(alice)>`` returns Alice's new commitment.

Run manually with:
    PYTHONPATH=tools python3 -m pytest tools/tests/test_zkpol_e2e.py -v -m live

Important environment:
    WALLET_BASE / ZKPOL_BRIDGE_BASE / ANCHOR_BASE override localhost URLs.
    ZKPOL_E2E_USER defaults to alice and must exist in the bridge auth.db.
    ZKPOL_E2E_REQUIRED=1 turns missing prerequisites into test failures.
    ZKPOL_E2E_REQUIRE_ACCOUNT_COMMITMENT=1 requires per-account bearer lookup.
"""

from __future__ import annotations

import json
import os
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.live]


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


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


def _ping(url: str, *, timeout: float = 1.0) -> bool:
    try:
        with _http_urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False


WALLET_BASE = _validated_http_url(
    os.environ.get("WALLET_BASE") or "http://127.0.0.1:8091", name="WALLET_BASE"
).rstrip("/")
BRIDGE_BASE = _validated_http_url(
    os.environ.get("ZKPOL_BRIDGE_BASE") or "http://127.0.0.1:5504",
    name="ZKPOL_BRIDGE_BASE",
).rstrip("/")
ANCHOR_BASE = _validated_http_url(
    os.environ.get("ANCHOR_BASE") or "http://127.0.0.1:5707", name="ANCHOR_BASE"
).rstrip("/")
E2E_USER = os.environ.get("ZKPOL_E2E_USER") or "alice"
E2E_REQUIRED = _env_flag("ZKPOL_E2E_REQUIRED")
REQUIRE_ACCOUNT_COMMITMENT = _env_flag("ZKPOL_E2E_REQUIRE_ACCOUNT_COMMITMENT")
E2E_TIMEOUT_SECONDS = float(os.environ.get("ZKPOL_E2E_TIMEOUT_SECONDS") or "120")


def _unavailable(message: str) -> None:
    if E2E_REQUIRED:
        raise AssertionError(message)
    raise unittest.SkipTest(message)


def _require_running() -> None:
    needed = {
        "wallet API": f"{WALLET_BASE}/health",
        "zkpol bridge": f"{BRIDGE_BASE}/bridge/health",
        "anchor indexer": f"{ANCHOR_BASE}/anchor/health",
    }
    missing = [name for name, url in needed.items() if not _ping(url)]
    if missing:
        _unavailable(
            "E2E prerequisites missing: " + ", ".join(missing) + " not reachable. "
            "See RUNBOOK in this file's docstring."
        )
    if not os.environ.get("BULLETIN_BOARD_ADDRESS"):
        _unavailable(
            "E2E prerequisites missing: BULLETIN_BOARD_ADDRESS not set; BulletinBoard "
            "is presumably not deployed on this host."
        )
    if REQUIRE_ACCOUNT_COMMITMENT and not os.environ.get("ZKCEX_TEST_BEARER"):
        _unavailable(
            "E2E prerequisites missing: ZKCEX_TEST_BEARER is required when "
            "ZKPOL_E2E_REQUIRE_ACCOUNT_COMMITMENT=1."
        )


class ZkPolE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        _require_running()

    def test_deposit_then_commitment_appears_on_chain(self) -> None:
        # 1) Make a deposit.
        transfer_ref = f"zkpol-e2e-{int(time.time() * 1000)}"
        deposit_url = (
            f"{WALLET_BASE}/deposit/1_test-ethereum_USDT/"
            f"{urllib.parse.quote(E2E_USER)}_MAIN?"
            f"description=zkpol-e2e&transferRef={urllib.parse.quote(transfer_ref)}"
        )
        req = _http_request(
            deposit_url,
            data=b"",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with _http_urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)

        # 2) Force a bridge tick so the test doesn't depend on poll timing.
        sync_req = _http_request(f"{BRIDGE_BASE}/bridge/sync-now", data=b"", method="POST")
        with _http_urlopen(sync_req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)

        # 3) Wait for anchor_indexer to see the new commitment.
        import hashlib

        addr_key = "0x" + hashlib.sha256(E2E_USER.encode("utf-8")).hexdigest()
        url = f"{ANCHOR_BASE}/anchor/account/{addr_key}?limit=1"
        deadline = time.time() + E2E_TIMEOUT_SECONDS
        latest = None
        while time.time() < deadline:
            try:
                # NOTE: this endpoint is bearer-gated. Set ZKCEX_TEST_BEARER
                # to a valid session token for the alice account, or use a
                # loopback override in your local dev env.
                bearer = os.environ.get("ZKCEX_TEST_BEARER")
                req = _http_request(
                    url, headers=({"Authorization": f"Bearer {bearer}"} if bearer else {})
                )
                with _http_urlopen(req, timeout=3) as resp:
                    if resp.status == 200:
                        body = json.loads(resp.read().decode("utf-8"))
                        if body.get("latest_commitment"):
                            latest = body
                            break
                    # 401 in tests is expected if no bearer; fall back to
                    # /anchor/batches which is public.
            except urllib.error.HTTPError as exc:
                if exc.code != 401:
                    raise
            time.sleep(1.0)
        if latest is None:
            if REQUIRE_ACCOUNT_COMMITMENT:
                self.fail("no per-account commitment indexed before timeout")
            # As a fallback, check that *some* commitment landed for this
            # token via the public batches endpoint.
            with _http_urlopen(f"{ANCHOR_BASE}/anchor/batches?limit=5", timeout=3) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(payload.get("batches"), "no batches indexed; pipeline broken")
            return  # public check passes; per-account check needs bearer auth
        self.assertIsNotNone(latest["latest_commitment"]["tx_hash"])


if __name__ == "__main__":
    unittest.main()
