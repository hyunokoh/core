"""Stub adapter — the default when ``TR_PROVIDER`` is unset.

This is the legacy ``post_ivms_to_counterparty()`` behavior extracted
into the adapter interface. It NEVER opens a network connection so it's
safe to run in CI / offline demo. Every delivery returns
``status='unreachable'`` with a stable error string, which the retry
loop will treat as "try again later".
"""

from __future__ import annotations

from typing import Any

from .base import InboundIvms, TransportResult


class StubAdapter:
    """No-network, always-defer adapter. Used in demo / CI."""

    name = "stub"

    def __init__(self) -> None:
        self.configured = False

    def post_ivms(
        self,
        ivms_message: dict,
        signature: str,
        public_key: str,
        counterparty_vasp_id: str,
    ) -> TransportResult:
        # Intentionally does not open a socket — see module docstring.
        return TransportResult(
            delivered=False,
            counterparty_id=counterparty_vasp_id,
            counterparty_ack_id=None,
            status="unreachable",
            error="stub_adapter:no_live_network",
            response_payload={
                "note": (
                    "TR_PROVIDER not set; install sumsub/notabene/trisa "
                    "credentials and set TR_PROVIDER to enable real delivery"
                ),
                "counterparty_vasp_id": counterparty_vasp_id,
            },
            rtt_ms=0,
        )

    def fetch_inbox(self, since_ts: int) -> list[InboundIvms]:
        return []

    def health_check(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "configured": False,
            "live_network": False,
            "note": "stub adapter — no provider credentials configured",
        }
