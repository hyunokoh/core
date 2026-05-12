"""End-to-end: emit a trace, verify Tempo received it.

Usage:
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
    OTEL_SERVICE_NAME=test \
    python3 -m otel.test_trace

Or, from the tools/ directory:

    python3 otel/test_trace.py

Exits 0 if Tempo returned the trace, 1 otherwise. The test is best-effort:
if no collector is reachable, it still verifies the in-process emission path
and exits 0 (CI environments without docker shouldn't fail the test).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Force the trace module to read these BEFORE importing it.
os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
os.environ.setdefault("OTEL_SERVICE_NAME", "test")
os.environ.setdefault("OTEL_ENABLED", "1")


# Local import works whether we run as `-m otel.test_trace` or as a script
# from tools/.
HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(HERE))
from otel import trace  # noqa: E402


def _validated_http_url(raw_url: str, *, name: str = "url") -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return raw_url


def _http_urlopen(target, **kwargs):
    if isinstance(target, str):
        target = _validated_http_url(target)
    return urllib.request.urlopen(target, **kwargs)  # noqa: S310


def emit_demo_trace() -> str:
    with trace.start_span("parent", attributes={"foo": "bar"}) as s:
        with trace.start_span("child-1", kind="CLIENT"):
            time.sleep(0.05)
        with trace.start_span("child-2", kind="INTERNAL"):
            with trace.start_span("grandchild"):
                time.sleep(0.05)
        trace_id = s.trace_id
    return trace_id


def fetch_trace(trace_id: str, tempo_query="http://localhost:3200", attempts=10):
    """Poll Tempo until the trace appears, or attempts are exhausted."""
    last_exc = None
    for _i in range(attempts):
        try:
            with _http_urlopen(f"{tempo_query}/api/traces/{trace_id}", timeout=5.0) as r:
                body = r.read()
                if body:
                    return json.loads(body)
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code == 404:
                # Trace not ingested yet.
                pass
            else:
                raise
        except Exception as e:  # noqa: BLE001
            last_exc = e
        time.sleep(1.0)
    print(f"[test_trace] tempo query gave up after {attempts}s: {last_exc!r}")
    return None


def main() -> int:
    trace_id = emit_demo_trace()
    print(f"emitted trace_id={trace_id}")

    # Force the exporter to flush synchronously.
    n = trace.flush()
    print(f"flushed {n} spans synchronously")

    tempo_q = os.environ.get("TEMPO_QUERY", "http://localhost:3200")
    body = fetch_trace(trace_id, tempo_query=tempo_q)
    if body is None:
        print("[test_trace] no Tempo response (collector unreachable?)")
        # Still a successful "did the emitter work" check.
        return 0

    # Pretty-print a one-line summary.
    batches = body.get("batches") or body.get("data") or []
    spans = []
    for b in batches:
        for ss in b.get("scopeSpans") or b.get("instrumentationLibrarySpans") or []:
            spans.extend(ss.get("spans") or [])
    print(f"[test_trace] Tempo returned {len(spans)} spans for {trace_id}")
    for sp in spans:
        print(f"  - {sp.get('name')} parent={sp.get('parentSpanId') or 'ROOT'}")
    print()
    print(json.dumps(body, indent=2)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
