"""Shared interface for Travel Rule transport adapters.

Every adapter (Sumsub TR, Notabene TRP, TRISA, the stub) implements the
same :class:`TravelRuleAdapter` protocol so the screening pipeline can
swap them out via the ``TR_PROVIDER`` env var without code changes.

Design notes:

* :class:`TransportResult` is a plain dataclass (no third-party deps).
  ``response_payload`` is whatever the provider gave us, JSON-decoded
  when feasible — we persist this verbatim so ops can reproduce wire
  errors without log diving.
* ``rtt_ms`` is measured locally around the urlopen() call; it does
  NOT include socket setup retries.
* ``status`` is the *delivery* status, not the AML decision. Adapter
  semantics:
    - 'accepted'    — provider returned 2xx and ACK'd the IVMS message.
    - 'pending'     — provider accepted bytes but is still routing /
                       waiting for the counterparty VASP to ACK.
    - 'rejected'    — provider returned 4xx/5xx that maps to a hard
                       refusal (bad signature, malformed IVMS, etc).
    - 'unreachable' — network error, DNS, TLS, timeouts.
    - 'unknown'     — provider returned 2xx with a body shape we don't
                       recognize. Persisted for ops triage.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

DeliveryStatus = Literal[
    "accepted",
    "pending",
    "rejected",
    "unknown",
    "unreachable",
]


def validated_http_base_url(name: str, raw_url: str) -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return raw_url.rstrip("/")


@dataclass
class TransportResult:
    """Result of attempting to deliver an IVMS message to a counterparty.

    Always populated even on failure — callers persist this row verbatim.
    """

    delivered: bool
    counterparty_id: str | None
    counterparty_ack_id: str | None
    status: DeliveryStatus
    error: str | None
    response_payload: dict | None
    rtt_ms: int


@dataclass
class InboundIvms:
    """One IVMS message pulled from a provider's inbox.

    Adapters that don't support pulling an inbox (e.g. stub) return an
    empty list from :meth:`TravelRuleAdapter.fetch_inbox`.
    """

    inbox_id: str
    from_vasp_id: str | None
    ivms_message: dict
    signature: str | None
    received_at: int
    raw: dict = field(default_factory=dict)


@runtime_checkable
class TravelRuleAdapter(Protocol):
    """The transport-side contract every Travel Rule provider must satisfy.

    Implementations live in sibling modules (sumsub.py, notabene.py,
    trisa.py, stub.py). The factory in
    :mod:`tools.travel_rule.adapters.__init__` picks one based on the
    ``TR_PROVIDER`` env var.
    """

    name: str  # 'sumsub' | 'notabene' | 'trisa' | 'stub'

    def post_ivms(
        self,
        ivms_message: dict,
        signature: str,
        public_key: str,
        counterparty_vasp_id: str,
    ) -> TransportResult:
        """Deliver a signed IVMS 101 message to the counterparty VASP.

        Args:
            ivms_message: The IVMS 101 envelope (already-built dict).
            signature: Hex-encoded Ed25519 signature over the canonical
                bytes of ``ivms_message``.
            public_key: Hex-encoded Ed25519 public key the counterparty
                should use to verify ``signature``.
            counterparty_vasp_id: Our internal VASP id (``vasp_id`` row
                in ``tr_vasp_directory``). Adapters that need a different
                identifier (Sumsub uses an opaque token) translate this
                via their own directory lookup.
        """
        ...

    def fetch_inbox(self, since_ts: int) -> list[InboundIvms]:
        """Pull inbound IVMS messages newer than ``since_ts`` from the
        provider's inbox. Adapters that push via webhook (Sumsub, Notabene)
        may return an empty list and rely on the webhook path instead.
        """
        ...

    def health_check(self) -> dict[str, Any]:
        """Best-effort liveness check. Should be cheap (HEAD/GET on a
        public endpoint, or a no-op for stub). Used by /adapter-info."""
        ...
