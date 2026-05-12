"""Simulate a multi-service request flow to exercise distributed tracing.

This script doesn't require the real zkCEX services to be running. It
fakes the proxy -> auth -> wallet -> sqlite hop chain in-process by:

    1. Starting one trace tree from the "proxy" perspective.
    2. Recording the traceparent header that *would* be sent on each
       outbound call.
    3. In the "downstream" services, reconstructing a remote parent from
       that header and continuing the same trace_id.
    4. Closing a SQLite span at the deepest level.

After running, the same trace_id appears in Tempo with spans from four
different ``service.name`` values (because each section sets its own
OTEL_SERVICE_NAME via a small subprocess-like helper).

Use it to manually exercise the trace-tree rendering in Grafana Explore
without standing up the full zkCEX stack.
"""

from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(HERE))


def _reload_trace_with_service(name: str):
    """Force-reload the trace module with a new OTEL_SERVICE_NAME.

    The module reads OTEL_SERVICE_NAME at import time; we re-import it
    so each "service" gets a distinct resource label in OTLP.
    """
    os.environ["OTEL_SERVICE_NAME"] = name
    # Forcibly drop and re-import.
    sys.modules.pop("otel.trace", None)
    sys.modules.pop("otel", None)
    from otel import trace  # noqa: F401

    return trace


def main() -> int:
    os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    os.environ.setdefault("OTEL_ENABLED", "1")

    # --- "proxy" (zkcex-proxy) ---
    proxy_trace = _reload_trace_with_service("zkcex-proxy-demo")
    with proxy_trace.start_span(
        "GET /v3/account",
        kind="SERVER",
        attributes={"http.method": "GET", "http.path": "/v3/account"},
    ) as proxy_span:
        trace_id = proxy_span.trace_id
        # The proxy makes a CLIENT call to auth_server.
        with proxy_trace.start_span(
            "HTTP GET http://auth/auth/session",
            kind="CLIENT",
            attributes={"http.url": "http://127.0.0.1:5501/auth/session"},
        ) as client_span:
            outbound_traceparent = f"00-{client_span.trace_id}-{client_span.span_id}-01"
            time.sleep(0.005)
    # Flush proxy BEFORE we drop its module from sys.modules.
    proxy_trace.flush()

    # --- "auth_server" (zkcex-auth) ---
    auth_trace = _reload_trace_with_service("zkcex-auth-demo")
    parent_from_proxy = auth_trace.extract_trace_context({"traceparent": outbound_traceparent})
    with auth_trace.start_span(
        "GET /auth/session",
        kind="SERVER",
        attributes={"http.method": "GET", "http.path": "/auth/session"},
        parent=parent_from_proxy,
    ):
        # Auth calls wallet API.
        with auth_trace.start_span(
            "HTTP GET http://wallet/v1/owner/usdt",
            kind="CLIENT",
            attributes={"http.url": "http://127.0.0.1:8091/v1/owner/usdt"},
        ) as wallet_client:
            wallet_traceparent = f"00-{wallet_client.trace_id}-{wallet_client.span_id}-01"
            time.sleep(0.005)
    auth_trace.flush()

    # --- "wallet" (zkcex-wallet) ---
    wallet_trace = _reload_trace_with_service("zkcex-wallet-demo")
    parent_from_auth = wallet_trace.extract_trace_context({"traceparent": wallet_traceparent})
    with wallet_trace.start_span(
        "GET /v1/owner/usdt",
        kind="SERVER",
        attributes={"http.method": "GET"},
        parent=parent_from_auth,
    ):
        with wallet_trace.start_span(
            "sqlite SELECT",
            kind="CLIENT",
            attributes={
                "db.system": "sqlite",
                "db.statement": "SELECT amount FROM balances WHERE owner=? AND asset=?",
            },
        ):
            time.sleep(0.002)
    wallet_trace.flush()

    print(f"emitted distributed trace_id={trace_id}")
    print(f"proxy -> auth header: traceparent={outbound_traceparent}")
    print(f"auth  -> wallet hdr : traceparent={wallet_traceparent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
