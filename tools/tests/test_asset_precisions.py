"""Unit tests for `tools/asset_precisions.py`.

These tests exercise the API boundary the chain server depends on:

  * alias resolution (ZETH -> ETH spec)
  * `validate_withdraw_amount` returns the documented error codes
  * `to_wei_for_chain` / `from_wei_for_chain` round-trip without float drift
  * Backward-compat: integer-string amounts still validate.

Run with:  PYTHONPATH=tools python3 -m pytest tools/tests/test_asset_precisions.py
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.abspath(os.path.join(HERE, ".."))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import asset_precisions as ap  # noqa: E402

# ----- asset_spec -----------------------------------------------------------


def test_zeth_alias_resolves_to_eth():
    s = ap.asset_spec("ZETH")
    assert s["decimals"] == 18
    assert s["withdraw_precision"] == 6
    assert s["min_withdraw"] == "0.001"


def test_zusdt_alias_resolves_to_usdt():
    s = ap.asset_spec("ZUSDT")
    assert s["decimals"] == 6
    assert s["withdraw_precision"] == 2
    assert s["min_withdraw"] == "1"


def test_unknown_symbol_falls_back():
    # Don't raise, just give a permissive-ish default.
    s = ap.asset_spec("XYZ")
    assert isinstance(s, dict)
    assert s["decimals"] >= 0


# ----- validate_withdraw_amount ---------------------------------------------


def test_valid_fractional_eth():
    ok, reason = ap.validate_withdraw_amount("ZETH", "0.5")
    assert ok, reason
    assert reason == ""


def test_valid_smallest_step():
    ok, _ = ap.validate_withdraw_amount("ZETH", "0.001")  # == min
    assert ok
    ok2, _ = ap.validate_withdraw_amount("ZUSDT", "1")
    assert ok2


def test_backward_compat_integer_amount():
    # Existing integer-amount UI submissions must still pass.
    for amt in ("1", "5", "10", "1000"):
        ok, reason = ap.validate_withdraw_amount("ZETH", amt)
        assert ok, f"{amt}: {reason}"


def test_below_minimum_rejected():
    ok, reason = ap.validate_withdraw_amount("ZETH", "0.0001")
    assert not ok and reason == "amount_below_minimum"


def test_precision_exceeded_rejected():
    ok, reason = ap.validate_withdraw_amount("ZETH", "0.1234567")  # 7dp, max is 6
    assert not ok and reason == "amount_precision_exceeded"


def test_usdt_precision_exceeded():
    ok, reason = ap.validate_withdraw_amount("ZUSDT", "1.234")  # 3dp, max is 2
    assert not ok and reason == "amount_precision_exceeded"


def test_zero_rejected():
    ok, reason = ap.validate_withdraw_amount("ZETH", "0")
    assert not ok and reason == "amount_invalid"


def test_negative_rejected():
    ok, reason = ap.validate_withdraw_amount("ZETH", "-1")
    assert not ok and reason == "amount_invalid"


def test_garbage_rejected():
    for bad in ("abc", "", "NaN", "Infinity", None):
        ok, reason = ap.validate_withdraw_amount("ZETH", bad)
        assert not ok and reason == "amount_invalid", f"{bad!r}: ok={ok}, {reason}"


# ----- to_wei / from_wei ----------------------------------------------------


def test_to_wei_exact_for_half_eth():
    # 0.5 ETH should be exactly 5 * 10^17, no float drift.
    assert ap.to_wei_for_chain("ZETH", "0.5") == 500_000_000_000_000_000


def test_to_wei_one_microeth():
    # 1e-6 ETH at 18 decimals = 1e12 wei.
    assert ap.to_wei_for_chain("ZETH", "0.000001") == 1_000_000_000_000


def test_to_wei_usdt():
    # USDT has 6 on-chain decimals; 1.23 USDT = 1_230_000.
    assert ap.to_wei_for_chain("ZUSDT", "1.23") == 1_230_000


def test_to_wei_rejects_excess_precision():
    # 18dp asset shouldn't actually raise (within precision), but a 19th
    # decimal digit MUST raise to avoid silent rounding.
    import pytest

    with pytest.raises(ValueError):
        ap.to_wei_for_chain("ZETH", "0.0000000000000000001")  # 19dp


def test_roundtrip_eth():
    for amt in ("0.001", "0.5", "1", "1.23456", "100"):
        wei = ap.to_wei_for_chain("ZETH", amt)
        back = ap.from_wei_for_chain("ZETH", wei)
        assert Decimal(back) == Decimal(amt)


def test_roundtrip_usdt():
    for amt in ("1", "1.23", "100", "0.01"):
        wei = ap.to_wei_for_chain("ZUSDT", amt)
        back = ap.from_wei_for_chain("ZUSDT", wei)
        assert Decimal(back) == Decimal(amt)


# ----- Decimal-input safety -------------------------------------------------


def test_accepts_decimal_input():
    ok, _ = ap.validate_withdraw_amount("ZETH", Decimal("0.5"))
    assert ok


def test_accepts_int_input():
    ok, _ = ap.validate_withdraw_amount("ZETH", 1)
    assert ok
