"""HTTP server-side helpers.

Used by BaseHTTPRequestHandler subclasses to open a SERVER span per request
that links to the upstream parent via the W3C traceparent header.

Typical use inside a do_GET / do_POST / _dispatch:

    from tools.otel.server import server_span
    with server_span(self) as span:
        ...
        span.set_attribute("http.status_code", code)
"""

from __future__ import annotations

from .trace import extract_trace_context, start_span


def server_span(handler):
    """Open a SERVER span for an inbound request.

    ``handler`` is a BaseHTTPRequestHandler. Extracts traceparent from
    ``handler.headers`` and uses it as the remote parent when present.
    """
    parent = extract_trace_context(getattr(handler, "headers", None))
    path = getattr(handler, "path", "/")
    method = getattr(handler, "command", "GET") or "GET"
    name = f"{method} {path.split('?')[0]}"
    attrs = {
        "http.method": method,
        "http.path": path,
        "http.user_agent": (
            handler.headers.get("User-Agent", "") if getattr(handler, "headers", None) else ""
        ),
    }
    return start_span(name, kind="SERVER", attributes=attrs, parent=parent)
