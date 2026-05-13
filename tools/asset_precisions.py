"""Per-asset precision / minimum-withdraw spec table.

The chain bridge (`chain_server.py`) historically accepted only integer amounts
on `/chain/withdraw`. Real exchanges withdraw fractional amounts: 0.0042 ETH,
1.23 USDT, etc. This module is the single source of truth for:

  * on-chain `decimals` (matches ERC20 `decimals()`, used for wei conversion),
  * `withdraw_precision` (how many decimal digits a user may submit -- looser
    UIs would just trust the on-chain decimals, but exchanges round down so
    nobody ships 0.000000000000000001 ETH because their balance had a tail),
  * `min_withdraw` (the dust floor; rejected at the API boundary),
  * `step` (the smallest user-visible increment; matches `withdraw_precision`,
    surfaced to the HTML `<input step="…">`).

Aliases (`ZETH`, `ZUSDT`) map onto their canonical underlying asset's spec so
the demo's synthetic tokens reuse the same rules.

Everything here uses `decimal.Decimal` -- never float -- because the user's
input may be `"0.1"` (which is *not* exactly representable as binary float)
and we MUST NOT silently round it before the integer-wei step.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

ASSET_PRECISIONS: dict = {
    # symbol: {decimals, withdraw_precision, min_withdraw, step}
    "ETH": {"decimals": 18, "withdraw_precision": 6, "min_withdraw": "0.001", "step": "0.000001"},
    "BTC": {
        "decimals": 8,
        "withdraw_precision": 8,
        "min_withdraw": "0.00001",
        "step": "0.00000001",
    },
    "USDT": {"decimals": 6, "withdraw_precision": 2, "min_withdraw": "1", "step": "0.01"},
    "USDC": {"decimals": 6, "withdraw_precision": 2, "min_withdraw": "1", "step": "0.01"},
    "SOL": {"decimals": 9, "withdraw_precision": 4, "min_withdraw": "0.01", "step": "0.0001"},
    "DOGE": {"decimals": 8, "withdraw_precision": 4, "min_withdraw": "5", "step": "0.0001"},
    # Demo synthetic tokens: route to the canonical underlying spec.
    "ZETH": "ETH",
    "ZUSDT": "USDT",
}


def asset_spec(symbol: str) -> dict:
    """Return the precision spec for a symbol; resolve aliases.

    Unknown symbols fall back to a generic 18-decimal / 6dp spec so the
    function never raises -- callers get to handle precision violations as
    400s, not 500s.
    """
    s = (symbol or "").upper()
    spec = ASSET_PRECISIONS.get(s)
    if isinstance(spec, str):  # alias -> canonical
        spec = ASSET_PRECISIONS.get(spec)
    if not isinstance(spec, dict):
        return {"decimals": 18, "withdraw_precision": 6, "min_withdraw": "0", "step": "0.000001"}
    return spec


def validate_withdraw_amount(
    symbol: str,
    amount: str | Decimal | int,
) -> tuple[bool, str]:
    """Validate `amount` for a withdraw of `symbol`.

    Returns `(ok, reason)`. On failure the reason is one of:
        amount_invalid          -- can't parse / negative / zero
        amount_below_minimum    -- below per-asset `min_withdraw`
        amount_precision_exceeded -- more decimals than `withdraw_precision`
    """
    spec = asset_spec(symbol)
    try:
        d = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError):
        return False, "amount_invalid"
    if not d.is_finite() or d <= 0:
        return False, "amount_invalid"

    # Precision check: scale by 10^withdraw_precision and require integer.
    prec = int(spec["withdraw_precision"])
    scaled = d * (Decimal(10) ** prec)
    if scaled != scaled.to_integral_value():
        return False, "amount_precision_exceeded"

    if d < Decimal(spec["min_withdraw"]):
        return False, "amount_below_minimum"

    return True, ""


def to_wei_for_chain(symbol: str, amount: str | Decimal) -> int:
    """Decimal amount -> on-chain integer (wei-equivalent for any decimals).

    Trusts the caller to have already validated precision; we still raise
    if the value would round (defensive: callers may bypass validation).
    """
    spec = asset_spec(symbol)
    decimals = int(spec["decimals"])
    d = Decimal(str(amount))
    scaled = d * (Decimal(10) ** decimals)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"amount has more than {decimals} decimals")
    return int(scaled)


def from_wei_for_chain(symbol: str, wei: int) -> str:
    """Inverse of `to_wei_for_chain` -- canonical decimal string."""
    spec = asset_spec(symbol)
    decimals = int(spec["decimals"])
    if decimals == 0:
        return str(int(wei))
    s = f"{int(wei):0{decimals + 1}d}"
    whole, frac = s[:-decimals], s[-decimals:]
    frac = frac.rstrip("0")
    return whole if not frac else f"{whole}.{frac}"
