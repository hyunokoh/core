# tools/otel — Distributed tracing for zkCEX

A stdlib-only OpenTelemetry-compatible tracer that produces W3C-spec
traceparent propagation and OTLP/HTTP-JSON exports. Any OTLP collector
(Tempo, Jaeger, Datadog, Honeycomb, ...) ingests the output as if it came
from the official SDK.

## Architecture

```
   Browser
     |  (GET /v3/account)
     v
   serve_homepage.py   :5500  (proxy, ENTRY POINT — trace originates here)
     |  HTTP CLIENT span    (traceparent: 00-<tid>-<sid>-01)
     v
   auth_server.py      :5501  (SERVER span -- links to upstream sid)
     |  HTTP CLIENT span
     v
   wallet API          :8091
     |  sqlite CLIENT span
     v
   sqlite balances.db
```

Every hop becomes one span. The whole tree shares one ``trace_id``; each
span's ``parent_span_id`` points at its caller. Grafana Explore (Tempo
datasource) renders the tree end-to-end.

## How spans propagate

1. **Inbound HTTP** — every instrumented handler wraps its body in
   ``server_span()``. It reads ``traceparent`` from the request headers
   and uses it as the parent for the new SERVER span.
2. **Outbound HTTP** — ``instrument.install()`` monkey-patches
   ``urllib.request.urlopen`` so each call opens a CLIENT span and
   *injects* the current trace context into the outgoing request headers.
3. **SQLite** — the same install() patches ``sqlite3.Connection.execute``
   to emit a small INTERNAL span around each query, tagged with the SQL
   operation type.

The context lives in a ``contextvars.ContextVar``, so multiple concurrent
requests in the same process get isolated trace state.

## Adding tracing to a new service

Drop these lines at the top of the file (before the first otel import):

```python
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
os.environ.setdefault("OTEL_SERVICE_NAME", "zkcex-<myname>")

try:
    from otel.shim import install as _otel_install, server_span as _otel_server_span
except Exception:
    def _otel_install(): pass
    def _otel_server_span(_h):
        class _N:
            def __enter__(self): return type('S', (), {'set_attribute': lambda *a, **k: None})()
            def __exit__(self, *a): return False
        return _N()
```

Then in ``main()``:

```python
_otel_install()
```

And around the request handler:

```python
def do_GET(self):
    with _otel_server_span(self):
        ...
```

That's it. Outbound HTTP and SQLite are auto-instrumented; no other
changes needed.

## Sampling

100% in dev. The exporter has no head-based sampler — every span is
queued and POSTed.

For production, use **tail-based sampling at the collector**:

* Deploy the OpenTelemetry Collector in agent mode in front of Tempo.
* Configure the ``tail_sampling`` processor:
    - keep all errors (``status: ERROR``)
    - keep all slow traces (``latency > 200ms``)
    - probabilistic 5-10% of the rest

This keeps the high-signal traces and drops the bulk of "everything
worked fine in 12ms" trees. It also moves sampling out of every service,
so we don't have to redeploy code to tune retention.

To wire it up: set ``OTEL_EXPORTER_OTLP_ENDPOINT`` on each service to the
collector's address; point the collector at Tempo.

## Reading traces

* **Grafana Explore** → Tempo datasource → "Search" tab. Pick the
  service, set a time window, optionally filter by ``http.path``,
  ``http.status_code``, etc.
* **Trace ID** lookup: paste the 32-char hex into the "TraceID" field.
* **Jaeger UI** also works (just a different frontend on the same OTLP
  data) if Tempo is replaced with a Jaeger backend.

## Cost considerations (real production)

The demo writes every span as JSON to a local filesystem-backed Tempo.
A real deployment needs to think about:

* **Ingest volume**: at 100% sampling, a busy service emits megabytes/sec.
  Tail-sampling cuts this by 90%+.
* **Storage**: Tempo retains traces on object storage. 1h retention is
  the demo default. Real deploys want 7-30 days; this is 10-1000s of
  GB/day depending on traffic.
* **Query latency**: trace_id lookups are O(1) (Tempo indexes per
  trace_id). "Search by attribute" requires a sidecar metrics-generator
  or duckdb/parquet querier on top of object-store data.
* **PII leakage**: avoid putting raw user PII in span attributes. The
  demo blanks out everything except path, method, status, user-agent.
  Production should add a redaction filter in the collector pipeline.

## What this stdlib version does NOT do (vs the official SDK)

This is a ~250 LOC tracer. The real ``opentelemetry-sdk`` does
considerably more:

* **Auto-instrumentation packages** for requests, httpx, aiohttp,
  psycopg2/3, asyncpg, redis-py, kafka-python, Flask, FastAPI, Django,
  Falcon, Starlette, Tornado, ASGI/WSGI generally, SQLAlchemy, pymongo,
  pymysql, boto3, grpc, celery, pika, elasticsearch, ... We only patch
  ``urllib.request`` and ``sqlite3``.
* **OTLP gRPC** transport. We only do OTLP/HTTP-JSON. gRPC is more
  efficient over high-volume long-lived links to a collector.
* **Batching strategies**: the real BatchSpanProcessor has dynamic
  flushes, max-export-batch-size, schedule-delay, queue-overflow policy.
  Ours is a fixed 2-second interval with an unbounded in-memory queue;
  if the collector is down for a long time we'll silently lose spans.
* **Retries with backoff** on the exporter. Ours is best-effort — one
  shot, swallow on failure.
* **Sampling configs** (``OTEL_TRACES_SAMPLER`` parent-based, ratio,
  always-on/off, jaeger remote sampling). We're hardcoded 100%.
* **Resource detection** (host.name, k8s.pod.uid, container.id, AWS/GCP
  cloud attributes). We only set ``service.name``.
* **Metrics + logs exporters**. We're traces-only.
* **Log correlation**: the official SDK injects trace_id/span_id into
  the stdlib ``logging`` records so Loki can correlate logs to traces.
  We don't — though it's trivial to add: read
  ``otel.trace.current_span()`` from a logging filter.
* **OpenCensus / Zipkin / Jaeger Thrift compatibility shims**.
* **Async (``asyncio``) propagation** through cancel scopes and
  ``ContextVar`` copies on task creation. ``asyncio.create_task`` does
  copy contextvars, so simple cases work; complex ones (executor
  pools, trio, anyio bridges) need glue we don't provide.

## Verification

```bash
# 1) Start Tempo
docker compose -f tempo-compose.yml up -d

# 2) Wait for ingester (Tempo takes ~15s to be ready after start)
until curl -sf http://localhost:3200/ready >/dev/null; do sleep 1; done

# 3) Emit a trace
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
OTEL_SERVICE_NAME=test \
  python3 otel/test_trace.py

# 4) Look the trace up in Tempo (may take ~30s for block flush)
curl http://localhost:3200/api/traces/<trace_id>
```

The demo `run.sh` launcher passes ``OTEL_EXPORTER_OTLP_ENDPOINT`` and
``OTEL_SERVICE_NAME`` into each service automatically when Tempo is
running, so cross-service traces work end-to-end.
