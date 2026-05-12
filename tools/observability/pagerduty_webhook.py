#!/usr/bin/env python3
"""
PagerDuty webhook bridge for zkCEX.

Receives Prometheus Alertmanager webhook POSTs and translates each alert into
a PagerDuty Events API v2 event. Maintains a local SQLite audit trail so even
if PagerDuty is unreachable we have a record.

Env:
    PAGERDUTY_INTEGRATION_KEY  Integration key for the PagerDuty service.
                               If unset, alerts are logged to stderr only.
    PAGERDUTY_EVENTS_URL       Default https://events.pagerduty.com/v2/enqueue
    PAGERDUTY_DB_PATH          Default /tmp/zkcex_alerts.db
    PAGERDUTY_DEDUP_MINUTES    Default 60 (don't re-fire identical alerts more
                               often than this; we still log every one).

Endpoints:
    POST /webhook    Accepts Alertmanager v4 payload {"alerts": [...]}
    GET  /health     Aggregator-shape JSON
    GET  /metrics    Prometheus text format (so we can be scraped too)
    GET  /alerts     Last 100 alert events from the local audit DB

stdlib only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_PORT = 5641


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


PAGERDUTY_INTEGRATION_KEY = os.environ.get("PAGERDUTY_INTEGRATION_KEY", "").strip()
PAGERDUTY_EVENTS_URL = _validated_http_url(
    os.environ.get("PAGERDUTY_EVENTS_URL", "https://events.pagerduty.com/v2/enqueue"),
    name="PAGERDUTY_EVENTS_URL",
)
DB_PATH = os.environ.get(
    "PAGERDUTY_DB_PATH", os.path.join(tempfile.gettempdir(), "zkcex_alerts.db")
)
LISTEN_HOST = os.environ.get("PAGERDUTY_HOST", "127.0.0.1")
DEDUP_MINUTES = int(os.environ.get("PAGERDUTY_DEDUP_MINUTES", "60"))


def _log(msg: str) -> None:
    sys.stderr.write(f"[pagerduty_webhook] {msg}\n")
    sys.stderr.flush()


# ----------------------------------------------------------------------------
# Audit DB
# ----------------------------------------------------------------------------

_DB_LOCK = threading.Lock()


def _db_init() -> None:
    with _DB_LOCK:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    alertname TEXT NOT NULL,
                    severity TEXT,
                    dedup_key TEXT,
                    summary TEXT,
                    labels_json TEXT,
                    annotations_json TEXT,
                    forwarded INTEGER NOT NULL DEFAULT 0,
                    forward_status_code INTEGER,
                    forward_error TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_alerts_dedup_time "
                "ON alerts(dedup_key, received_at)"
            )
            conn.commit()
        finally:
            conn.close()


def _db_insert(record: dict[str, Any]) -> int:
    with _DB_LOCK:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.execute(
                """
                INSERT INTO alerts
                (received_at, status, alertname, severity, dedup_key, summary,
                 labels_json, annotations_json, forwarded,
                 forward_status_code, forward_error)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record["received_at"],
                    record["status"],
                    record["alertname"],
                    record.get("severity"),
                    record.get("dedup_key"),
                    record.get("summary"),
                    json.dumps(record.get("labels") or {}),
                    json.dumps(record.get("annotations") or {}),
                    1 if record.get("forwarded") else 0,
                    record.get("forward_status_code"),
                    record.get("forward_error"),
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
        finally:
            conn.close()


def _db_recent(limit: int = 100) -> list[dict[str, Any]]:
    with _DB_LOCK:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM alerts ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 1000)),),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def _db_recent_dedup(dedup_key: str, within_seconds: int) -> bool:
    with _DB_LOCK:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.execute(
                """
                SELECT 1 FROM alerts
                WHERE dedup_key = ? AND received_at > ? AND forwarded = 1
                LIMIT 1
                """,
                (dedup_key, time.time() - within_seconds),
            )
            return cur.fetchone() is not None
        finally:
            conn.close()


def _db_stats() -> dict[str, int]:
    with _DB_LOCK:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.execute("SELECT COUNT(*), COALESCE(SUM(forwarded), 0) FROM alerts")
            row = cur.fetchone()
            total = int(row[0] or 0)
            forwarded = int(row[1] or 0)
            cur2 = conn.execute("SELECT severity, COUNT(*) FROM alerts GROUP BY severity")
            by_sev: dict[str, int] = {}
            for sev, n in cur2.fetchall():
                by_sev[str(sev or "unknown")] = int(n)
            return {
                "total": total,
                "forwarded": forwarded,
                **{f"sev_{k}": v for k, v in by_sev.items()},
            }
        finally:
            conn.close()


# ----------------------------------------------------------------------------
# PagerDuty forwarding
# ----------------------------------------------------------------------------


def _pd_event(status: str, alert: dict[str, Any]) -> dict[str, Any]:
    labels = alert.get("labels", {}) or {}
    annotations = alert.get("annotations", {}) or {}
    alertname = str(labels.get("alertname") or "UnknownAlert")
    severity = str(labels.get("severity") or "warning").lower()
    if severity not in ("critical", "error", "warning", "info"):
        severity = "warning"
    # Map alertmanager "firing"/"resolved" → PD trigger/resolve.
    action = "resolve" if status == "resolved" else "trigger"
    # Deduplication key — Alertmanager passes one via labels in real life but
    # we fall back to a stable derivation.
    dedup = alert.get("fingerprint") or _stable_dedup(alertname, labels)
    summary = str(annotations.get("summary") or alertname)
    payload = {
        "routing_key": PAGERDUTY_INTEGRATION_KEY,
        "event_action": action,
        "dedup_key": dedup,
        "payload": {
            "summary": summary[:1024],
            "source": str(labels.get("service") or labels.get("instance") or "zkcex"),
            "severity": severity,
            "component": str(labels.get("service") or "zkcex"),
            "group": str(labels.get("team") or "platform"),
            "class": alertname,
            "custom_details": {
                "labels": labels,
                "annotations": annotations,
                "starts_at": alert.get("startsAt"),
                "ends_at": alert.get("endsAt"),
                "generator_url": alert.get("generatorURL"),
            },
        },
    }
    return payload


def _stable_dedup(alertname: str, labels: dict[str, Any]) -> str:
    parts = [alertname]
    for k in sorted(labels.keys()):
        parts.append(f"{k}={labels[k]}")
    return "zkcex:" + ":".join(parts)


def _forward_to_pd(payload: dict[str, Any]) -> tuple[bool, int | None, str | None]:
    body = json.dumps(payload).encode("utf-8")
    req = _http_request(
        PAGERDUTY_EVENTS_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with _http_urlopen(req, timeout=5.0) as resp:
            return True, int(resp.getcode() or 0), None
    except urllib.error.HTTPError as e:
        return False, int(e.code or 0), f"HTTPError {e.code}"
    except Exception as e:
        return False, None, repr(e)


# ----------------------------------------------------------------------------
# State counters (for /metrics)
# ----------------------------------------------------------------------------

_STATE = {
    "started_at": time.time(),
    "alerts_received_total": 0,
    "alerts_forwarded_total": 0,
    "alerts_dedup_suppressed_total": 0,
    "alerts_logged_only_total": 0,
    "webhook_requests_total": 0,
    "webhook_errors_total": 0,
}
_STATE_LOCK = threading.Lock()


def _state_inc(key: str, n: int = 1) -> None:
    with _STATE_LOCK:
        _STATE[key] = int(_STATE.get(key, 0)) + n


# ----------------------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "zkcex-pagerduty-webhook/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return  # quiet

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        path = (self.path or "/").split("?", 1)[0]
        if path == "/health":
            self._send(200, json.dumps(self._health()).encode("utf-8"), "application/json")
            return
        if path == "/metrics":
            self._send(
                200, self._metrics().encode("utf-8"), "text/plain; version=0.0.4; charset=utf-8"
            )
            return
        if path == "/alerts":
            body = json.dumps({"alerts": _db_recent(100)}, default=str).encode("utf-8")
            self._send(200, body, "application/json")
            return
        if path == "/":
            html = (
                b"<html><body><h2>zkCEX PagerDuty webhook</h2>"
                b"<ul>"
                b"<li>POST /webhook (Alertmanager payload)</li>"
                b"<li><a href='/health'>/health</a></li>"
                b"<li><a href='/metrics'>/metrics</a></li>"
                b"<li><a href='/alerts'>/alerts</a> (last 100)</li>"
                b"</ul></body></html>"
            )
            self._send(200, html, "text/html; charset=utf-8")
            return
        self._send(404, b'{"error":"not_found"}', "application/json")

    def do_POST(self) -> None:  # noqa: N802
        path = (self.path or "/").split("?", 1)[0]
        if path != "/webhook":
            self._send(404, b'{"error":"not_found"}', "application/json")
            return
        _state_inc("webhook_requests_total")
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            _state_inc("webhook_errors_total")
            self._send(400, b'{"error":"invalid_json"}', "application/json")
            return

        # Alertmanager v4 payload: {"alerts": [{...}], "commonLabels":..., ...}
        alerts = body.get("alerts") or []
        if not isinstance(alerts, list):
            _state_inc("webhook_errors_total")
            self._send(400, b'{"error":"alerts_must_be_array"}', "application/json")
            return

        results: list[dict[str, Any]] = []
        for alert in alerts:
            if not isinstance(alert, dict):
                continue
            _state_inc("alerts_received_total")
            status = str(alert.get("status") or "firing").lower()
            labels = alert.get("labels") or {}
            annotations = alert.get("annotations") or {}
            alertname = str(labels.get("alertname") or "UnknownAlert")
            severity = str(labels.get("severity") or "warning")
            summary = str(annotations.get("summary") or alertname)
            dedup = alert.get("fingerprint") or _stable_dedup(alertname, labels)

            record: dict[str, Any] = {
                "received_at": time.time(),
                "status": status,
                "alertname": alertname,
                "severity": severity,
                "dedup_key": dedup,
                "summary": summary,
                "labels": labels,
                "annotations": annotations,
                "forwarded": False,
                "forward_status_code": None,
                "forward_error": None,
            }

            if not PAGERDUTY_INTEGRATION_KEY:
                _state_inc("alerts_logged_only_total")
                _log(
                    f"[no PD key, log only] {status.upper()} "
                    f"alert={alertname} severity={severity} summary={summary!r}"
                )
                row_id = _db_insert(record)
                results.append(
                    {
                        "id": row_id,
                        "alertname": alertname,
                        "status": status,
                        "forwarded": False,
                        "reason": "no_integration_key",
                    }
                )
                continue

            # Skip if we've recently forwarded the same dedup key (PD also dedups
            # but this saves outbound network traffic for noisy alerts).
            if status == "firing" and _db_recent_dedup(dedup, DEDUP_MINUTES * 60):
                _state_inc("alerts_dedup_suppressed_total")
                _log(
                    f"[dedup-suppress] alertname={alertname} dedup={dedup} "
                    f"(within {DEDUP_MINUTES}m)"
                )
                record["forward_error"] = "dedup_suppressed"
                row_id = _db_insert(record)
                results.append(
                    {
                        "id": row_id,
                        "alertname": alertname,
                        "status": status,
                        "forwarded": False,
                        "reason": "dedup_suppressed",
                    }
                )
                continue

            payload = _pd_event(status, alert)
            ok, code, err = _forward_to_pd(payload)
            record["forwarded"] = ok
            record["forward_status_code"] = code
            record["forward_error"] = err
            if ok:
                _state_inc("alerts_forwarded_total")
                _log(
                    f"[PD-{code}] {payload['event_action']} alertname={alertname} "
                    f"severity={severity}"
                )
            else:
                _log(f"[PD-FAIL] alertname={alertname} code={code} err={err}")
            row_id = _db_insert(record)
            results.append(
                {
                    "id": row_id,
                    "alertname": alertname,
                    "status": status,
                    "forwarded": ok,
                    "forward_status_code": code,
                    "forward_error": err,
                }
            )

        response = {"ok": True, "received": len(alerts), "results": results}
        self._send(200, json.dumps(response).encode("utf-8"), "application/json")

    # ---------- /health and /metrics --------------------------------------

    def _health(self) -> dict[str, Any]:
        with _STATE_LOCK:
            state = dict(_STATE)
        return {
            "ok": True,
            "pagerduty_configured": bool(PAGERDUTY_INTEGRATION_KEY),
            "events_url": PAGERDUTY_EVENTS_URL if PAGERDUTY_INTEGRATION_KEY else None,
            "audit_db": DB_PATH,
            "uptime_s": round(time.time() - state["started_at"], 1),
            "counters": {k: v for k, v in state.items() if k != "started_at"},
            "db_stats": _db_stats(),
        }

    def _metrics(self) -> str:
        with _STATE_LOCK:
            state = dict(_STATE)
        lines: list[str] = []
        lines.append("# HELP zkcex_pagerduty_webhook_up Webhook self-up")
        lines.append("# TYPE zkcex_pagerduty_webhook_up gauge")
        lines.append("zkcex_pagerduty_webhook_up 1")
        lines.append(
            "# HELP zkcex_pagerduty_alerts_received_total Alerts received from Alertmanager"
        )
        lines.append("# TYPE zkcex_pagerduty_alerts_received_total counter")
        lines.append(f'zkcex_pagerduty_alerts_received_total {state["alerts_received_total"]}')
        lines.append(
            "# HELP zkcex_pagerduty_alerts_forwarded_total Alerts forwarded to PagerDuty Events API"
        )
        lines.append("# TYPE zkcex_pagerduty_alerts_forwarded_total counter")
        lines.append(f'zkcex_pagerduty_alerts_forwarded_total {state["alerts_forwarded_total"]}')
        lines.append(
            "# HELP zkcex_pagerduty_alerts_logged_only_total Alerts logged but not forwarded (no key)"
        )
        lines.append("# TYPE zkcex_pagerduty_alerts_logged_only_total counter")
        lines.append(
            f'zkcex_pagerduty_alerts_logged_only_total {state["alerts_logged_only_total"]}'
        )
        lines.append(
            "# HELP zkcex_pagerduty_alerts_dedup_suppressed_total Alerts suppressed by local dedup"
        )
        lines.append("# TYPE zkcex_pagerduty_alerts_dedup_suppressed_total counter")
        lines.append(
            f'zkcex_pagerduty_alerts_dedup_suppressed_total {state["alerts_dedup_suppressed_total"]}'
        )
        lines.append("# HELP zkcex_pagerduty_webhook_requests_total Total POST /webhook requests")
        lines.append("# TYPE zkcex_pagerduty_webhook_requests_total counter")
        lines.append(f'zkcex_pagerduty_webhook_requests_total {state["webhook_requests_total"]}')
        lines.append(
            "# HELP zkcex_pagerduty_webhook_errors_total Total /webhook 4xx error responses"
        )
        lines.append("# TYPE zkcex_pagerduty_webhook_errors_total counter")
        lines.append(f'zkcex_pagerduty_webhook_errors_total {state["webhook_errors_total"]}')
        lines.append(f"zkcex_pagerduty_configured {1 if PAGERDUTY_INTEGRATION_KEY else 0}")
        return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------


def main() -> None:
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            _log(f"bad port: {sys.argv[1]!r}, using {DEFAULT_PORT}")
    _db_init()
    server = ThreadingHTTPServer((LISTEN_HOST, port), Handler)
    _log(
        f"listening on {LISTEN_HOST}:{port}, "
        f"pagerduty={'configured' if PAGERDUTY_INTEGRATION_KEY else 'log-only'}, "
        f"audit_db={DB_PATH}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
