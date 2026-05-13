"""Tiny helper so service files can do one-line wiring.

    from otel.shim import server_span, install

Both are guaranteed to be importable and to no-op gracefully if anything
inside the tracer package raises.
"""

from __future__ import annotations


class _NoopSpan:
    def set_attribute(self, *a, **kw):
        pass

    def add_event(self, *a, **kw):
        pass

    @property
    def trace_id(self):
        return ""

    @property
    def span_id(self):
        return ""


class _NoopCtx:
    def __enter__(self):
        return _NoopSpan()

    def __exit__(self, *a):
        return False


def _noop_server_span(_handler):
    return _NoopCtx()


def _noop_install():
    pass


try:
    from .instrument import install as _real_install
    from .server import server_span as _real_server_span

    server_span = _real_server_span
    install = _real_install
except Exception:  # noqa: BLE001
    server_span = _noop_server_span  # type: ignore[assignment]
    install = _noop_install  # type: ignore[assignment]
