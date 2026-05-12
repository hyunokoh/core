"""Stdlib-only OpenTelemetry-compatible tracer.

Implements the W3C Trace Context spec (traceparent header) and emits OTLP/HTTP
spans to a configured collector. Without otel-sdk we can't get the official
SDK's full feature set, but we can produce traceparent-propagating spans with
correct IDs that any collector (Tempo, Jaeger, Datadog, Honeycomb) will
ingest.

Wire format: each span is POSTed to {OTEL_EXPORTER_OTLP_ENDPOINT}/v1/traces as
a single-resource-single-scope OTLP HTTP/JSON payload.

Configuration (via env vars):
    OTEL_EXPORTER_OTLP_ENDPOINT  default: http://localhost:4318
    OTEL_SERVICE_NAME            default: zkcex-unknown
    OTEL_ENABLED                 default: 1   ("0"/"false"/"no" disables export)

Notes:
    * 100% sampling. Production setups should layer a tail-based sampler in
      the collector (see README).
    * The exporter is best-effort: if the collector is unreachable, batches
      are silently dropped so request handling is never blocked.
    * trace_id is 16 random bytes (32 hex chars), span_id is 8 random bytes
      (16 hex chars), matching the W3C spec.
"""

from __future__ import annotations

import contextvars
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


def _validated_http_url(raw_url: str, *, name: str = "url") -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return raw_url


def _validated_http_base_url(name: str, raw_url: str) -> str:
    return _validated_http_url(raw_url, name=name).rstrip("/")


def _http_request(url: str, **kwargs) -> urllib.request.Request:
    return urllib.request.Request(_validated_http_url(url), **kwargs)  # noqa: S310


def _log(msg: str) -> None:
    sys.stderr.write(f"[otel.trace] {msg}\n")


OTEL_ENDPOINT = _validated_http_base_url(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"),
)
OTEL_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "zkcex-unknown")
OTEL_ENABLED = os.environ.get("OTEL_ENABLED", "1").lower() in ("1", "true", "yes")


_OTLP_KINDS = {
    "INTERNAL": 1,
    "SERVER": 2,
    "CLIENT": 3,
    "PRODUCER": 4,
    "CONSUMER": 5,
}


# Per-async-context active span. ContextVars survive threading.Thread
# boundaries on CPython only if you propagate them; we accept that
# limitation and instead read trace_id from headers on inbound RPCs.
_current_span: contextvars.ContextVar = contextvars.ContextVar("current_span", default=None)

_export_queue: list[dict] = []
_queue_lock = threading.Lock()
_exporter_started = False
_exporter_start_lock = threading.Lock()


def new_trace_id() -> str:
    """16-byte random ID rendered as 32 lowercase hex chars."""
    return secrets.token_hex(16)


def new_span_id() -> str:
    """8-byte random ID rendered as 16 lowercase hex chars."""
    return secrets.token_hex(8)


class Span:
    """Single span. Created via start_span() and finalized on __exit__."""

    __slots__ = (
        "name",
        "kind",
        "trace_id",
        "span_id",
        "parent_span_id",
        "start_ns",
        "end_ns",
        "attributes",
        "events",
        "status",
    )

    def __init__(self, name, parent=None, kind="INTERNAL", attributes=None):
        self.name = name
        self.kind = kind
        self.trace_id = parent.trace_id if parent else new_trace_id()
        self.span_id = new_span_id()
        self.parent_span_id = parent.span_id if parent else None
        self.start_ns = time.time_ns()
        self.end_ns = None
        self.attributes = dict(attributes or {})
        self.events = []
        self.status = "OK"

    def set_attribute(self, k, v):
        self.attributes[k] = v

    def add_event(self, name, attrs=None):
        self.events.append({"name": name, "ts": time.time_ns(), "attributes": dict(attrs or {})})

    def end(self):
        if self.end_ns is not None:
            return  # idempotent
        self.end_ns = time.time_ns()
        _enqueue(self.to_otlp())

    def to_otlp(self) -> dict:
        # OTLP/HTTP+JSON: modern collectors (Tempo >= 2.4, Jaeger,
        # OpenTelemetry Collector) accept hex-string trace/span IDs and
        # this is now the recommended encoding for OTLP/JSON (the old
        # base64-protobuf-bytes encoding tripped strict length validators).
        return {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_span_id or "",
            "name": self.name,
            "kind": _OTLP_KINDS.get(self.kind, 1),
            "startTimeUnixNano": str(self.start_ns),
            "endTimeUnixNano": str(self.end_ns),
            "attributes": [
                {"key": k, "value": {"stringValue": str(v)}} for k, v in self.attributes.items()
            ],
            "events": [
                {
                    "name": e["name"],
                    "timeUnixNano": str(e["ts"]),
                    "attributes": [
                        {"key": k, "value": {"stringValue": str(v)}}
                        for k, v in e["attributes"].items()
                    ],
                }
                for e in self.events
            ],
            "status": {"code": 1 if self.status == "OK" else 2},
        }


class _PseudoParent:
    """Stand-in for a remote parent reconstructed from a traceparent header.

    Not a real Span: it never ends and is never exported. Only its trace_id
    and span_id are used so that locally-created child spans link to the
    upstream caller.
    """

    __slots__ = ("trace_id", "span_id")

    def __init__(self, trace_id, span_id):
        self.trace_id = trace_id
        self.span_id = span_id


def start_span(name, kind="INTERNAL", attributes=None, parent=None):
    """Open a new span as the current context.

    Returns a context manager. The span automatically inherits the
    ContextVar-tracked current span as its parent unless ``parent`` is given
    explicitly (used when reconstructing a remote parent from an incoming
    traceparent header).
    """
    actual_parent = parent if parent is not None else _current_span.get()
    span = Span(name, parent=actual_parent, kind=kind, attributes=attributes)
    token = _current_span.set(span)

    class _Ctx:
        def __enter__(self):
            return span

        def __exit__(self, et, ev, tb):
            if et is not None:
                span.status = "ERROR"
                span.add_event(
                    "exception",
                    {
                        "exception.type": getattr(et, "__name__", str(et)),
                        "exception.message": str(ev),
                    },
                )
            span.end()
            try:
                _current_span.reset(token)
            except ValueError:
                # token created in a different context (thread); ignore.
                pass
            return False  # never swallow

    return _Ctx()


def current_span() -> Span | None:
    """Return the in-flight span for this context, or None."""
    return _current_span.get()


def current_trace_context() -> dict:
    """Return a dict containing the traceparent header for the current span.

    Returns ``{}`` when there is no active span (e.g. tracing disabled or
    outside any start_span block).
    """
    s = _current_span.get()
    if not s:
        return {}
    return {"traceparent": f"00-{s.trace_id}-{s.span_id}-01"}


def extract_trace_context(headers):
    """Build a pseudo-parent Span from an incoming traceparent header.

    Accepts a mapping (dict-like or http.client.HTTPMessage). Returns None
    if the header is missing or malformed.
    """
    if headers is None:
        return None
    tp = None
    # http.client.HTTPMessage is case-insensitive via get(); plain dicts aren't.
    if hasattr(headers, "get"):
        tp = headers.get("traceparent") or headers.get("Traceparent")
    if not tp:
        return None
    parts = tp.split("-")
    if len(parts) != 4:
        return None
    if len(parts[1]) != 32 or len(parts[2]) != 16:
        return None
    try:
        int(parts[1], 16)
        int(parts[2], 16)
    except ValueError:
        return None
    return _PseudoParent(parts[1], parts[2])


# -- exporter ----------------------------------------------------------------


def _enqueue(span_dict: dict) -> None:
    with _queue_lock:
        _export_queue.append(span_dict)


def _flush_once() -> int:
    """Drain the queue and POST a single OTLP/HTTP batch. Returns count sent."""
    with _queue_lock:
        if not _export_queue:
            return 0
        batch = list(_export_queue)
        _export_queue.clear()

    if not OTEL_ENABLED:
        return 0

    body = json.dumps(
        {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": OTEL_SERVICE_NAME},
                            }
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "zkcex.tools.otel", "version": "0.1.0"},
                            "spans": batch,
                        }
                    ],
                }
            ]
        }
    ).encode("utf-8")
    req = _http_request(
        f"{OTEL_ENDPOINT}/v1/traces",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        # Use the underlying opener to avoid re-entering our own instrumented
        # urlopen (which would create a recursive span loop).
        opener = urllib.request.build_opener()
        opener.open(req, timeout=2.0)
    except Exception as exc:  # noqa: BLE001 - best-effort exporter
        _log(f"export skipped: {exc!r}")
    return len(batch)


def _exporter_loop():
    while True:
        time.sleep(2.0)
        try:
            _flush_once()
        except Exception as exc:  # noqa: BLE001
            # Never let the exporter thread die from a transient error.
            _log(f"exporter loop recovered: {exc!r}")


def _start_exporter_thread():
    global _exporter_started
    with _exporter_start_lock:
        if _exporter_started:
            return
        t = threading.Thread(target=_exporter_loop, name="otel-exporter", daemon=True)
        t.start()
        _exporter_started = True


# Public API: tests may call flush() to force a synchronous flush.
def flush() -> int:
    """Force-drain the export queue synchronously. Returns count sent."""
    return _flush_once()


if OTEL_ENABLED:
    _start_exporter_thread()
