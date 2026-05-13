"""Auto-instrumentation shims for outbound HTTP and SQLite.

Service code calls ``install()`` once at startup. After install:
    * urllib.request.urlopen wraps each request in a CLIENT span and injects
      the traceparent header on the outgoing request.
    * sqlite3.Connection.execute / executemany emit INTERNAL spans tagged
      with the SQL operation type (best-effort; first token only).

Both shims are idempotent and safe to install twice (the second install is
a no-op).

The shims are intentionally small. The official opentelemetry-instrumentation
packages cover many more libraries (requests, httpx, aiohttp, psycopg2,
asyncpg, redis-py, kafka-python, Flask, FastAPI, Django, ...). See the README
for a fuller list of what we don't cover.
"""

from __future__ import annotations

import sqlite3
import urllib.error
import urllib.parse
import urllib.request

from .trace import current_trace_context, start_span

_installed = False


# -- urllib.request.urlopen --------------------------------------------------

_orig_urlopen = urllib.request.urlopen


def _validated_http_url(raw_url: str, *, name: str = "url") -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return raw_url


def _instrumented_urlopen(req, *args, **kwargs):
    # ``req`` may be a string URL or a Request. Normalize for span naming
    # while keeping the original object for the actual call.
    if isinstance(req, str):
        url = _validated_http_url(req)
        request_obj = urllib.request.Request(url)  # noqa: S310
        # If caller passed args[0]/kwargs['data'], propagate via Request.
        if args and args[0] is not None:
            request_obj.data = args[0]
        elif "data" in kwargs and kwargs["data"] is not None:
            request_obj.data = kwargs["data"]
    else:
        url = req.full_url
        request_obj = req

    method = getattr(request_obj, "get_method", lambda: "GET")()
    name = f"HTTP {method} {url.split('?')[0]}"
    with start_span(
        name,
        kind="CLIENT",
        attributes={
            "http.url": url,
            "http.method": method,
        },
    ) as span:
        # Inject traceparent into outgoing headers.
        for k, v in current_trace_context().items():
            if not request_obj.has_header(k.capitalize()) and not request_obj.has_header(k):
                request_obj.add_header(k, v)
        try:
            # Forward only the timeout kwarg (positional args other than data
            # are not part of urlopen's stable signature for our purposes).
            timeout = kwargs.get("timeout")
            if timeout is not None:
                resp = _orig_urlopen(request_obj, timeout=timeout)
            else:
                resp = _orig_urlopen(request_obj)
            status = getattr(resp, "status", None)
            if status is not None:
                span.set_attribute("http.status_code", status)
            return resp
        except urllib.error.HTTPError as e:
            span.set_attribute("http.status_code", e.code)
            if e.code >= 500:
                span.status = "ERROR"
            raise


# -- sqlite3.Connection.execute ---------------------------------------------

_orig_sqlite_execute = sqlite3.Connection.execute
_orig_sqlite_executemany = sqlite3.Connection.executemany


def _sql_op(sql: str) -> str:
    s = sql.lstrip()
    # First word is the operation (SELECT/INSERT/UPDATE/DELETE/PRAGMA/...).
    i = 0
    while i < len(s) and not s[i].isspace():
        i += 1
    return s[:i].upper() or "SQL"


def _instrumented_execute(self, sql, *args, **kwargs):
    op = _sql_op(sql)
    with start_span(
        f"sqlite {op}",
        kind="CLIENT",
        attributes={"db.system": "sqlite", "db.statement": sql[:512]},
    ):
        return _orig_sqlite_execute(self, sql, *args, **kwargs)


def _instrumented_executemany(self, sql, *args, **kwargs):
    op = _sql_op(sql)
    with start_span(
        f"sqlite {op} (many)",
        kind="CLIENT",
        attributes={"db.system": "sqlite", "db.statement": sql[:512]},
    ):
        return _orig_sqlite_executemany(self, sql, *args, **kwargs)


def install():
    """Monkey-patch outbound HTTP and SQLite. Idempotent."""
    global _installed
    if _installed:
        return
    urllib.request.urlopen = _instrumented_urlopen
    try:
        sqlite3.Connection.execute = _instrumented_execute  # type: ignore[assignment]
        sqlite3.Connection.executemany = _instrumented_executemany  # type: ignore[assignment]
    except (AttributeError, TypeError):
        # Some Python builds restrict monkey-patching builtin types; degrade
        # gracefully — HTTP instrumentation still works.
        pass
    _installed = True
