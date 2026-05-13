"""Travel Rule transport adapters.

Public surface:

  get_adapter(provider=None) -> TravelRuleAdapter
      Factory keyed by ``TR_PROVIDER`` env (or explicit ``provider``
      argument). Falls back to :class:`StubAdapter` on any configuration
      failure, with the reason logged to stderr.

Supported providers:

  - 'sumsub'    -> :class:`SumsubAdapter`  (Sumsub Travel Rule API)
  - 'notabene'  -> :class:`NotabeneAdapter` (Notabene TRP)
  - 'trisa'     -> :class:`TrisaAdapter`   (TRISA HTTP gateway fallback)
  - 'stub'      -> :class:`StubAdapter`    (no live network)
  -  None / unset / unknown -> StubAdapter
"""

from __future__ import annotations

import os
import sys

from .base import DeliveryStatus, InboundIvms, TransportResult, TravelRuleAdapter
from .notabene import NotabeneAdapter
from .stub import StubAdapter
from .sumsub import SumsubAdapter
from .trisa import TrisaAdapter

__all__ = [
    "DeliveryStatus",
    "InboundIvms",
    "TransportResult",
    "TravelRuleAdapter",
    "SumsubAdapter",
    "NotabeneAdapter",
    "TrisaAdapter",
    "StubAdapter",
    "get_adapter",
]


_ADAPTER_CLASSES = {
    "sumsub": SumsubAdapter,
    "notabene": NotabeneAdapter,
    "trisa": TrisaAdapter,
    "stub": StubAdapter,
}


def _log(msg: str) -> None:
    sys.stderr.write(f"[travel_rule.adapters] {msg}\n")
    sys.stderr.flush()


def get_adapter(provider: str | None = None) -> TravelRuleAdapter:
    """Resolve a Travel Rule adapter.

    Resolution order:
      1. ``provider`` argument (if non-empty, case-insensitive).
      2. ``TR_PROVIDER`` env (case-insensitive).
      3. Fallback to ``StubAdapter``.

    If the requested adapter raises during construction (missing env
    vars, malformed cert paths) we log the error and return
    ``StubAdapter`` so the server still boots.
    """
    name = (provider or os.environ.get("TR_PROVIDER") or "").strip().lower()
    if not name:
        return StubAdapter()
    cls = _ADAPTER_CLASSES.get(name)
    if cls is None:
        _log(
            f"unknown provider {name!r}; valid: {sorted(_ADAPTER_CLASSES)}. "
            "Falling back to stub."
        )
        return StubAdapter()
    try:
        return cls()
    except Exception as exc:
        _log(f"adapter {name!r} failed to initialize: {exc!r}; falling back to stub")
        return StubAdapter()
