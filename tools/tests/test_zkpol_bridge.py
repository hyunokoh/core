import unittest
from decimal import Decimal

from zkpol_bridge.zkpol_bridge import (
    bridge_event_id,
    decimal_to_scaled_int,
    normalize_event_type,
    parse_allowlist,
    parse_event_type_map,
)


class ZkPolBridgeTest(unittest.TestCase):
    def test_decimal_to_scaled_int_uses_precision_digits(self) -> None:
        self.assertEqual(decimal_to_scaled_int(Decimal("12.34"), 2), 1234)
        self.assertEqual(decimal_to_scaled_int(Decimal("0.00000001"), 8), 1)

    def test_parse_allowlist_normalizes_symbols(self) -> None:
        self.assertEqual(parse_allowlist("usdt, btc"), {"USDT", "BTC"})
        self.assertIsNone(parse_allowlist(""))

    def test_event_type_mapping_uses_defaults_and_overrides(self) -> None:
        mapping = parse_event_type_map("trade:conversion")
        self.assertEqual(normalize_event_type("withdraw", mapping), "withdrawal")
        self.assertEqual(normalize_event_type("trade", mapping), "conversion")
        self.assertEqual(normalize_event_type("deposit", mapping), "deposit")

    def test_bridge_event_id_applies_offset(self) -> None:
        self.assertEqual(bridge_event_id(42, 1_000_000_000_000), 1_000_000_000_042)


if __name__ == "__main__":
    unittest.main()
