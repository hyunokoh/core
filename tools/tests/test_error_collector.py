"""Unit + integration tests for ``tools/error_collector.py``.

Run with::

    PYTHONPATH=tools python3 -m pytest tools/tests/test_error_collector.py -x

The collector is started in-process on an ephemeral port. Each test gets
a fresh sqlite DB and a fresh rate limiter so the suite is order-
independent.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.abspath(os.path.join(HERE, ".."))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)


def _validated_http_url(raw_url: str, *, name: str = "url") -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return raw_url


def _http_request(url: str, **kwargs) -> urllib.request.Request:
    return urllib.request.Request(_validated_http_url(url), **kwargs)  # noqa: S310


def _http_urlopen(target, **kwargs):
    if isinstance(target, str):
        target = _validated_http_url(target)
    return urllib.request.urlopen(target, **kwargs)  # noqa: S310


@pytest.fixture
def collector(monkeypatch):
    """Spin up an error_collector with an isolated DB on a random port.

    Yields a tuple of (port, admin_token, module_handle).
    """
    tmpdir = tempfile.mkdtemp(prefix="ec_test_")
    db_path = os.path.join(tmpdir, "errors.db")
    monkeypatch.setenv("ERROR_ADMIN_TOKEN", "test-admin-token-1234567890")
    monkeypatch.setenv("ERROR_RATE_LIMIT_PER_MIN", "31")  # used in rate-limit test
    monkeypatch.setenv("ERROR_RATE_LIMIT_PER_HOUR_USER", "1000")

    # Force module reload so env vars + module-level constants are re-read.
    if "error_collector" in sys.modules:
        del sys.modules["error_collector"]
    import error_collector as ec  # noqa: E402

    # Point the module at the temp DB and rebuild its rate limiter so a
    # previous test's bucket counters don't bleed in.
    monkeypatch.setattr(ec, "DB_PATH", db_path)
    monkeypatch.setattr(ec, "_runtime_state", {})
    ec._limiter = ec.RateLimiter(
        int(os.environ["ERROR_RATE_LIMIT_PER_MIN"]),
        int(os.environ["ERROR_RATE_LIMIT_PER_HOUR_USER"]),
    )
    ec.init_db()

    # Find a free port.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    srv = ec.ThreadingServer(("127.0.0.1", port), ec.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    # tiny settle so the port is accept()ing
    for _ in range(20):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.02)

    try:
        yield port, "test-admin-token-1234567890", ec
    finally:
        srv.shutdown()
        srv.server_close()


def _post(port, path, body, headers=None, timeout=2.0):
    headers = dict(headers or {})
    headers.setdefault("Content-Type", "application/json")
    data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
    req = _http_request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method="POST")
    try:
        with _http_urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode() or "{}")
        except Exception:
            payload = {}
        return e.code, payload


def _get(port, path, headers=None, timeout=2.0):
    req = _http_request(f"http://127.0.0.1:{port}{path}", headers=dict(headers or {}), method="GET")
    try:
        with _http_urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode() or "{}")
        except Exception:
            payload = {}
        return e.code, payload


# --------------------------------------------------------------------------
# Fingerprint
# --------------------------------------------------------------------------
def test_fingerprint_deterministic():
    import error_collector as ec  # already imported by the fixture autouse path; safe

    a = ec.compute_fingerprint(
        "browser-js",
        "TypeError",
        "at foo (/app/app.js:42:15)\nat bar (/app/app.js:51:12)",
        "Cannot read property of null",
    )
    b = ec.compute_fingerprint(
        "browser-js",
        "TypeError",
        "at foo (/app/app.js:42:15)\nat bar (/app/app.js:51:12)",
        "Cannot read property of null",
    )
    assert a == b
    # Different exception_type -> different fp.
    c = ec.compute_fingerprint(
        "browser-js",
        "ReferenceError",
        "at foo (/app/app.js:42:15)",
        "x",
    )
    assert c != a
    # Different first frame -> different fp.
    d = ec.compute_fingerprint(
        "browser-js",
        "TypeError",
        "at zoo (/app/app.js:42:15)",
        "x",
    )
    assert d != a


def test_redact_token_first_8():
    import error_collector as ec

    assert ec.redact_token("abcd1234efghijkl") == "abcd1234..."
    assert ec.redact_token("short") == "short"
    assert ec.redact_token(None) is None
    assert ec.redact_token("") is None


# --------------------------------------------------------------------------
# Ingest happy path
# --------------------------------------------------------------------------
def test_report_browser_payload(collector):
    port, tok, ec = collector
    payload = {
        "source": "browser-js",
        "level": "error",
        "message": "TypeError: Cannot read property 'x' of null",
        "exception_type": "TypeError",
        "stack_trace": "at Object.foo (/app/app.js:42:15)\nat ...",
        "url": "https://app.zkcex.io/app/trade.html",
        "user_agent": "Mozilla/5.0",
        "user_id": "u-23",
        "session_token": "abcd1234XXXXXXXXXX",
        "metadata": {"symbol": "ETHUSDT"},
    }
    status, body = _post(port, "/errors/report", payload)
    assert status == 200
    assert "event_id" in body
    assert "fingerprint" in body
    fp = body["fingerprint"]
    # Confirm aggregate has count=1, session token is redacted.
    status, body = _get(
        port, "/errors/aggregates?limit=10", headers={"Authorization": f"Bearer {tok}"}
    )
    assert status == 200
    aggs = body["aggregates"]
    assert any(a["fingerprint"] == fp and a["count"] == 1 for a in aggs)
    # Look at the raw event row through the admin event detail.
    eid = next(a["sample_event_id"] for a in aggs if a["fingerprint"] == fp)
    status, body = _get(port, f"/errors/event/{eid}", headers={"Authorization": f"Bearer {tok}"})
    assert status == 200
    expected_redaction = "abcd1234..."
    assert body["event"]["session_token_redacted"] == expected_redaction
    # Make sure full token never landed in the row.
    assert "XXXXXXXXXX" not in json.dumps(body["event"])


def test_dedup_increments_count(collector):
    port, tok, ec = collector
    payload = {
        "source": "browser-js",
        "level": "error",
        "message": "TypeError: x is null",
        "exception_type": "TypeError",
        "stack_trace": "at foo (/app/app.js:42:15)",
        "url": "/app/trade.html",
        "user_agent": "test",
    }
    seen_fp = None
    for _ in range(6):
        status, body = _post(port, "/errors/report", payload)
        assert status == 200
        seen_fp = body["fingerprint"]
    # Aggregate count should be 6, fingerprint stable across calls.
    status, body = _get(port, "/errors/aggregates", headers={"Authorization": f"Bearer {tok}"})
    assert status == 200
    aggs = [a for a in body["aggregates"] if a["fingerprint"] == seen_fp]
    assert len(aggs) == 1
    assert aggs[0]["count"] == 6


# --------------------------------------------------------------------------
# Rate limit
# --------------------------------------------------------------------------
def test_rate_limit_triggers(collector):
    # Capacity is 31 ip/min in this fixture; the 32nd request should 429.
    port, tok, ec = collector
    payload = {
        "source": "browser-js",
        "level": "error",
        "message": "x",
        "exception_type": "TypeError",
        "stack_trace": "at bar:1:1",
    }
    statuses = []
    for _i in range(33):
        s, _ = _post(port, "/errors/report", payload)
        statuses.append(s)
    # First 31 succeed, then 429.
    assert statuses[:31].count(200) >= 30
    assert 429 in statuses[31:]


# --------------------------------------------------------------------------
# Admin auth
# --------------------------------------------------------------------------
def test_admin_endpoints_require_bearer(collector):
    port, tok, ec = collector
    for path in ("/errors/events", "/errors/aggregates", "/errors/stats"):
        status, body = _get(port, path)
        assert status == 401, path
    # Wrong bearer also rejected.
    status, body = _get(port, "/errors/stats", headers={"Authorization": "Bearer wrong"})
    assert status == 401


def test_aggregate_resolve_mute_note(collector):
    port, tok, ec = collector
    payload = {
        "source": "browser-js",
        "level": "error",
        "message": "Boom",
        "exception_type": "Error",
        "stack_trace": "at boom:1:1",
    }
    _, body = _post(port, "/errors/report", payload)
    fp = body["fingerprint"]
    headers = {"Authorization": f"Bearer {tok}"}
    s, b = _post(port, f"/errors/aggregates/{fp}/resolve", {}, headers)
    assert s == 200 and b["ok"]
    s, b = _post(
        port,
        f"/errors/aggregates/{fp}/note",
        {"note": "investigated, fix landing in v1.2"},
        headers,
    )
    assert s == 200
    # The status now should be 'resolved' and the note should round-trip.
    s, b = _get(port, "/errors/aggregates?status=resolved", headers=headers)
    rec = [a for a in b["aggregates"] if a["fingerprint"] == fp][0]
    assert rec["status"] == "resolved"
    assert "investigated" in (rec["notes"] or "")
    # Re-mute it.
    s, b = _post(port, f"/errors/aggregates/{fp}/mute", {}, headers)
    assert s == 200
    s, b = _get(port, "/errors/aggregates?status=muted", headers=headers)
    assert any(a["fingerprint"] == fp for a in b["aggregates"])


# --------------------------------------------------------------------------
# Python internal endpoint
# --------------------------------------------------------------------------
def test_python_internal_endpoint_accepts_loopback(collector):
    port, tok, ec = collector
    body = {
        "source": "python-service:auth_server",
        "level": "error",
        "message": "ValueError: bad input",
        "exception_type": "ValueError",
        "stack_trace": 'File "auth_server.py", line 42, in handle\n  raise ValueError("bad input")',
    }
    s, b = _post(port, "/errors/internal/python-error", body)
    assert s == 200
    assert b["event_id"]


def test_stats_reflects_inserts(collector):
    port, tok, ec = collector
    headers = {"Authorization": f"Bearer {tok}"}
    s, b = _get(port, "/errors/stats", headers=headers)
    assert s == 200
    assert b["total_events"] == 0
    # 3 of fingerprint A, 1 of fingerprint B.
    for _ in range(3):
        _post(
            port,
            "/errors/report",
            {
                "source": "browser-js",
                "level": "error",
                "message": "Bang",
                "exception_type": "TypeError",
                "stack_trace": "at boom:42:1",
            },
        )
    _post(
        port,
        "/errors/report",
        {
            "source": "browser-js",
            "level": "error",
            "message": "Bang",
            "exception_type": "TypeError",
            "stack_trace": "at boom2:99:1",
        },
    )
    s, b = _get(port, "/errors/stats", headers=headers)
    assert b["total_events"] == 4
    assert b["total_aggregates"] == 2
    assert b["open_aggregates"] == 2
    assert b["events_last_24h"] == 4
    assert len(b["top_5_fingerprints"]) == 2


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------
def test_event_filter_by_source(collector):
    port, tok, ec = collector
    headers = {"Authorization": f"Bearer {tok}"}
    _post(
        port,
        "/errors/report",
        {
            "source": "browser-js",
            "level": "error",
            "message": "B",
            "exception_type": "E",
            "stack_trace": "at b:1:1",
        },
    )
    _post(
        port,
        "/errors/internal/python-error",
        {
            "source": "python-service:foo",
            "level": "error",
            "message": "P",
            "exception_type": "E",
            "stack_trace": "at p:1:1",
        },
    )
    s, b = _get(port, "/errors/events?source=browser-js&limit=10", headers=headers)
    assert all(e["source"] == "browser-js" for e in b["events"])
    assert len(b["events"]) == 1
    s, b = _get(port, "/errors/events?source=python-service:foo&limit=10", headers=headers)
    assert len(b["events"]) == 1
    assert b["events"][0]["source"] == "python-service:foo"
