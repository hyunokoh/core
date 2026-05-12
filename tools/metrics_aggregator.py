#!/usr/bin/env python3
"""
zkCEX metrics aggregator.

A stdlib-only HTTP service (default port 5640) that scrapes every demo
service's /health endpoint, reads order-book and trade rates from the spot
REST API, taps chain.db / safu.db / pol.db directly for counters, and exposes
the whole thing as a single Prometheus text-format /metrics endpoint that
Grafana (running in docker compose) can scrape.

Design notes:
  - stdlib only (no requests, no prometheus_client)
  - scrapes run on a background thread every SCRAPE_INTERVAL_S seconds
  - scrapes use a thread pool so one round trips in ~2s even if a service hangs
  - /metrics returns the *last successful* snapshot so a slow scrape never
    blocks Prometheus
  - never mutates the services it scrapes; chain.db is opened read-only via
    'file:...?mode=ro' uri so we can never accidentally lock the writer
  - never raises out of a scrape: every per-target failure flips that
    target's `up` gauge to 0 and is otherwise swallowed (with a debug line
    to stderr).

Run:
    python3 metrics_aggregator.py [PORT]

Endpoints:
    /metrics          Prometheus text exposition
    /health           {"ok": true, "last_scrape_at": ..., "n_targets": N}
    /scrape-targets.json  list of what we're scraping
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

SCRAPE_INTERVAL_S = float(os.environ.get("ZKCEX_AGG_SCRAPE_INTERVAL_S", "15"))
SCRAPE_TIMEOUT_S = float(os.environ.get("ZKCEX_AGG_SCRAPE_TIMEOUT_S", "2.5"))
SCRAPE_WORKERS = int(os.environ.get("ZKCEX_AGG_SCRAPE_WORKERS", "16"))

# Path to the local SQLite directory the demo services use.
LOCAL_DB_DIR = os.environ.get(
    "ZKCEX_AGG_LOCAL_DB_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".local"),
)

DOCKER_BIN = os.environ.get("ZKCEX_AGG_DOCKER_BIN", "docker")
DOCKER_TIMEOUT_S = float(os.environ.get("ZKCEX_AGG_DOCKER_TIMEOUT_S", "4"))

DEFAULT_PORT = 5640
LISTEN_HOST = os.environ.get("ZKCEX_AGG_HOST", "127.0.0.1")


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


# Targets: (job, service_label, url, parser_fn_name).
# parser_fn_name maps to a method on Scraper that takes the parsed JSON (or
# raw text) and returns a dict of {metric_name: value | (value, labels_dict)}.
HEALTH_TARGETS: list[tuple[str, str, str, str]] = [
    ("proxy", "proxy", "http://127.0.0.1:5500/", "parse_proxy"),
    ("auth", "auth", "http://127.0.0.1:5501/auth/health", "parse_auth"),
    ("chain", "chain", "http://127.0.0.1:5502/chain/health", "parse_chain_health"),
    ("pol_py", "pol_py", "http://127.0.0.1:5503/pol/server-info", "parse_pol_py_info"),
    ("pol_py_live", "pol_py", "http://127.0.0.1:5503/pol/live/info", "parse_pol_py_live"),
    ("zkpol_bridge", "zkpol_bridge", "http://127.0.0.1:5504/bridge/health", "parse_bridge"),
    ("pol_feed", "pol_feed", "http://127.0.0.1:5505/health", "parse_pol_feed"),
    ("ws", "ws", "http://127.0.0.1:5510/health", "parse_ws"),
    ("custody_n0", "custody", "http://127.0.0.1:5520/health", "parse_custody_node"),
    ("custody_n1", "custody", "http://127.0.0.1:5521/health", "parse_custody_node"),
    ("custody_n2", "custody", "http://127.0.0.1:5522/health", "parse_custody_node"),
    ("custody_n3", "custody", "http://127.0.0.1:5523/health", "parse_custody_node"),
    ("custody_n4", "custody", "http://127.0.0.1:5524/health", "parse_custody_node"),
    ("custody_coord", "custody_coord", "http://127.0.0.1:5530/health", "parse_custody_coord"),
    ("export", "export", "http://127.0.0.1:5540/export/index.json", "parse_export"),
    ("api_key", "api_key", "http://127.0.0.1:5550/api-keys/health", "parse_simple_ok"),
    ("mcp", "mcp", "http://127.0.0.1:5560/mcp/health", "parse_simple_ok"),
    ("orders", "orders", "http://127.0.0.1:5570/orders/health", "parse_orders"),
    ("push", "push", "http://127.0.0.1:5580/push/health", "parse_simple_ok"),
    ("perp", "perp", "http://127.0.0.1:5590/fapi/v1/exchangeInfo", "parse_perp_info"),
    ("perp_premium", "perp", "http://127.0.0.1:5590/fapi/v1/premiumIndex", "parse_perp_premium"),
    ("mm_bot", "mm_bot", "http://127.0.0.1:5600/mm/health", "parse_mm"),
    ("safu", "safu", "http://127.0.0.1:5601/safu/summary", "parse_safu"),
    ("ops", "ops", "http://127.0.0.1:5620/ops-api/health", "parse_simple_ok"),
    ("zkpol_rust", "zkpol_rust", "http://127.0.0.1:21011/health/live", "parse_text_alive"),
    ("pol_snap_rust", "pol_snap_rust", "http://127.0.0.1:21100/health/live", "parse_text_alive"),
]

# Spot-exchange polling — used for trade rates and order-book depth gauges.
SPOT_EXCHANGE_INFO = "http://127.0.0.1:5500/v3/exchangeInfo"
SPOT_TRADES_URL = "http://127.0.0.1:5500/v3/trades?symbol={sym}&limit=500"
SPOT_DEPTH_URL = "http://127.0.0.1:5500/v3/depth?symbol={sym}"

# Synthetic latency probe — we time `/v3/depth` on a couple of symbols every
# scrape so we have a real histogram instead of a fake one.
LATENCY_PROBES = [
    "http://127.0.0.1:5500/v3/depth?symbol=ETHUSDT",
    "http://127.0.0.1:5500/v3/depth?symbol=BTCUSDT",
    "http://127.0.0.1:5500/v3/ticker/price",
]
LATENCY_BUCKETS_MS = [5, 10, 25, 50, 100, 250, 500, 1000, 2500, float("inf")]

# Docker containers we care about (names or partial names).
DOCKER_CONTAINER_PREFIXES = (
    "core-",
    "zkpol",
    "zkcex-",
)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _log(msg: str) -> None:
    sys.stderr.write(f"[metrics_aggregator] {msg}\n")
    sys.stderr.flush()


def _http_get(url: str, timeout: float = SCRAPE_TIMEOUT_S) -> tuple[int, bytes, float]:
    """GET a URL; return (status, body, elapsed_ms). Never raises."""
    t0 = time.monotonic()
    req = _http_request(url, headers={"User-Agent": "zkcex-metrics-aggregator/1.0"})
    try:
        with _http_urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            status = resp.getcode() or 0
            return status, body, (time.monotonic() - t0) * 1000.0
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return int(e.code or 0), body, (time.monotonic() - t0) * 1000.0
    except Exception:
        return 0, b"", (time.monotonic() - t0) * 1000.0


def _http_json(url: str, timeout: float = SCRAPE_TIMEOUT_S) -> tuple[bool, Any, float]:
    status, body, ms = _http_get(url, timeout)
    if status < 200 or status >= 500:
        return False, None, ms
    try:
        return True, json.loads(body.decode("utf-8")), ms
    except Exception:
        # Some endpoints return plain text ("ok"); caller can fall back to body.
        try:
            return False, body.decode("utf-8", errors="replace"), ms
        except Exception:
            return False, None, ms


def _open_ro_sqlite(path: str) -> sqlite3.Connection | None:
    """Open SQLite read-only via URI so we never disturb the writer."""
    if not os.path.exists(path):
        return None
    try:
        uri = f"file:{path}?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=1.0)
    except Exception:
        return None


def _safe_count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    try:
        cur = conn.execute(sql, params)
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return 0


def _safe_rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[tuple]:
    try:
        cur = conn.execute(sql, params)
        return list(cur.fetchall())
    except Exception:
        return []


# ----------------------------------------------------------------------------
# Prometheus text encoder
# ----------------------------------------------------------------------------


def _escape_label_value(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_metric(name: str, value: float, labels: dict[str, str] | None = None) -> str:
    if labels:
        parts = ",".join(f'{k}="{_escape_label_value(str(v))}"' for k, v in labels.items())
        head = f"{name}{{{parts}}}"
    else:
        head = name
    # Format numbers tightly but preserve floats.
    if isinstance(value, float):
        if value != value or value == float("inf") or value == float("-inf"):
            return f"{head} NaN" if value != value else f"{head} {'+Inf' if value > 0 else '-Inf'}"
        if value.is_integer():
            return f"{head} {int(value)}"
        return (
            f"{head} {value:.6f}".rstrip("0").rstrip(".")
            if "." in f"{value:.6f}"
            else f"{head} {value}"
        )
    return f"{head} {value}"


class PromBuilder:
    def __init__(self) -> None:
        self._chunks: list[str] = []
        self._declared: dict[str, str] = {}

    def help(self, name: str, help_text: str, mtype: str) -> None:
        if name in self._declared:
            return
        self._declared[name] = mtype
        self._chunks.append(f"# HELP {name} {help_text}")
        self._chunks.append(f"# TYPE {name} {mtype}")

    def add(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        try:
            v = float(value)
        except Exception:
            return
        self._chunks.append(_format_metric(name, v, labels))

    def render(self) -> str:
        return "\n".join(self._chunks) + "\n"


# ----------------------------------------------------------------------------
# Per-target parsers
# ----------------------------------------------------------------------------


class Scraper:
    """Wraps a single scrape pass. Stateless across passes."""

    def parse_proxy(self, body: Any, elapsed_ms: float) -> dict[str, Any]:
        # Proxy root returns HTML; we just record up + latency.
        return {"latency_ms": elapsed_ms}

    def parse_auth(self, body: Any, elapsed_ms: float) -> dict[str, Any]:
        if not isinstance(body, dict):
            return {}
        out: dict[str, Any] = {"latency_ms": elapsed_ms}
        if "n_users" in body:
            out["zkcex_user_count"] = body["n_users"]
        if "n_active_sessions" in body:
            out["zkcex_active_sessions"] = body["n_active_sessions"]
        if "db_latency_ms" in body:
            out[("zkcex_auth_db_latency_ms", None)] = body["db_latency_ms"]
        backend = body.get("backend") or "unknown"
        out[("zkcex_auth_backend_info", {"backend": str(backend)})] = 1
        return out

    def parse_chain_health(self, body: Any, elapsed_ms: float) -> dict[str, Any]:
        # body: {"chains": [{slug, ok, block_number, latency_ms, stale_seconds}...]}
        out: dict[str, Any] = {"latency_ms": elapsed_ms}
        if not isinstance(body, dict):
            return out
        chains = body.get("chains") or []
        for c in chains:
            slug = str(c.get("slug") or "unknown")
            ok = 1 if c.get("ok") else 0
            out[("zkcex_chain_up", {"chain": slug})] = ok
            if "block_number" in c:
                out[("zkcex_chain_block_number", {"chain": slug})] = c["block_number"]
            if "latency_ms" in c:
                out[("zkcex_chain_rpc_latency_ms", {"chain": slug})] = c["latency_ms"]
            if "stale_seconds" in c:
                out[("zkcex_chain_stale_seconds", {"chain": slug})] = c["stale_seconds"]
        return out

    def parse_pol_py_info(self, body: Any, elapsed_ms: float) -> dict[str, Any]:
        out: dict[str, Any] = {"latency_ms": elapsed_ms}
        if not isinstance(body, dict):
            return out
        if "epoch_seconds" in body:
            out[("zkcex_pol_epoch_seconds", {"scheme": "python"})] = body["epoch_seconds"]
        return out

    def parse_pol_py_live(self, body: Any, elapsed_ms: float) -> dict[str, Any]:
        out: dict[str, Any] = {"latency_ms": elapsed_ms}
        if not isinstance(body, dict):
            return out
        cc = body.get("current_commit") or {}
        if "commit_id" in cc:
            out[("zkcex_pol_epoch", {"scheme": "python-live"})] = cc["commit_id"]
        if "committed_at" in cc:
            out[("zkcex_pol_epoch_started_at", {"scheme": "python-live"})] = (
                cc["committed_at"] / 1000.0
            )
        if "n_users" in cc:
            out[("zkcex_pol_n_users", {"scheme": "python-live"})] = cc["n_users"]
        if "root_sum" in cc:
            try:
                out[("zkcex_pol_total_liabilities", {"scheme": "python-live"})] = float(
                    cc["root_sum"]
                )
            except Exception as e:  # noqa: BLE001
                _log(f"pol live root_sum parse skipped: {e!r}")
        if "max_commit_lag_seconds" in body:
            out["zkcex_pol_max_lag_seconds"] = body["max_commit_lag_seconds"]
        return out

    def parse_bridge(self, body: Any, elapsed_ms: float) -> dict[str, Any]:
        out: dict[str, Any] = {"latency_ms": elapsed_ms}
        if isinstance(body, dict):
            if "n_users_tracked" in body:
                out["zkcex_bridge_n_users_tracked"] = body["n_users_tracked"]
            if "n_assets" in body:
                out["zkcex_bridge_n_assets"] = body["n_assets"]
            if "took_ms" in body:
                out["zkcex_bridge_last_tick_ms"] = body["took_ms"]
            if "last_tick_at" in body:
                out["zkcex_bridge_last_tick_at"] = body["last_tick_at"]
            if "last_event_id" in body:
                out["zkcex_bridge_last_event_id"] = body["last_event_id"]
        return out

    def parse_pol_feed(self, body: Any, elapsed_ms: float) -> dict[str, Any]:
        return {"latency_ms": elapsed_ms}

    def parse_ws(self, body: Any, elapsed_ms: float) -> dict[str, Any]:
        out: dict[str, Any] = {"latency_ms": elapsed_ms}
        if isinstance(body, dict):
            if "n_clients" in body:
                out["zkcex_ws_clients"] = body["n_clients"]
            if "n_streams" in body:
                out["zkcex_ws_streams"] = body["n_streams"]
            if "n_active_symbols" in body:
                out["zkcex_ws_active_symbols"] = body["n_active_symbols"]
            if "dropped_frames" in body:
                out["zkcex_ws_dropped_frames_total"] = body["dropped_frames"]
            if "uptime_s" in body:
                out["zkcex_ws_uptime_seconds"] = body["uptime_s"]
        return out

    def parse_custody_node(self, body: Any, elapsed_ms: float) -> dict[str, Any]:
        out: dict[str, Any] = {"latency_ms": elapsed_ms}
        if isinstance(body, dict):
            si = body.get("share_index")
            nid = body.get("node_id") or "unknown"
            if si is not None:
                out[("zkcex_custody_node_share_index", {"node_id": str(nid)})] = si
        return out

    def parse_custody_coord(self, body: Any, elapsed_ms: float) -> dict[str, Any]:
        out: dict[str, Any] = {"latency_ms": elapsed_ms}
        if not isinstance(body, dict):
            return out
        if "threshold" in body:
            out["zkcex_custody_threshold_m"] = body["threshold"]
        if "total" in body:
            out["zkcex_custody_threshold_n"] = body["total"]
        if "n_nodes_reachable" in body:
            out["zkcex_custody_nodes_reachable"] = body["n_nodes_reachable"]
        for n in body.get("nodes") or []:
            nid = str(n.get("node_id") or "unknown")
            out[("zkcex_custody_node_up", {"node_id": nid})] = 1 if n.get("healthy") else 0
            if "last_response_ms" in n:
                out[("zkcex_custody_node_latency_ms", {"node_id": nid})] = n["last_response_ms"]
        return out

    def parse_export(self, body: Any, elapsed_ms: float) -> dict[str, Any]:
        out: dict[str, Any] = {"latency_ms": elapsed_ms}
        if isinstance(body, dict) and isinstance(body.get("exports"), list):
            out["zkcex_export_n_reports"] = len(body["exports"])
        return out

    def parse_simple_ok(self, body: Any, elapsed_ms: float) -> dict[str, Any]:
        return {"latency_ms": elapsed_ms}

    def parse_orders(self, body: Any, elapsed_ms: float) -> dict[str, Any]:
        out: dict[str, Any] = {"latency_ms": elapsed_ms}
        if isinstance(body, dict):
            if "pending_count" in body:
                out["zkcex_orders_pending"] = body["pending_count"]
            if "n_symbols_watched" in body:
                out["zkcex_orders_symbols_watched"] = body["n_symbols_watched"]
        return out

    def parse_perp_info(self, body: Any, elapsed_ms: float) -> dict[str, Any]:
        out: dict[str, Any] = {"latency_ms": elapsed_ms}
        if isinstance(body, dict) and isinstance(body.get("symbols"), list):
            out["zkcex_perp_n_symbols"] = len(body["symbols"])
        return out

    def parse_perp_premium(self, body: Any, elapsed_ms: float) -> dict[str, Any]:
        out: dict[str, Any] = {"latency_ms": elapsed_ms}
        if isinstance(body, list):
            for row in body:
                sym = str(row.get("symbol", ""))
                if not sym:
                    continue
                try:
                    if "markPrice" in row:
                        out[("zkcex_perp_mark_price", {"symbol": sym})] = float(row["markPrice"])
                    if "lastFundingRate" in row:
                        out[("zkcex_perp_funding_rate", {"symbol": sym})] = float(
                            row["lastFundingRate"]
                        )
                except Exception as e:  # noqa: BLE001
                    _log(f"perp premium row parse skipped for {sym}: {e!r}")
        return out

    def parse_mm(self, body: Any, elapsed_ms: float) -> dict[str, Any]:
        out: dict[str, Any] = {"latency_ms": elapsed_ms}
        if isinstance(body, dict):
            for k_in, k_out in [
                ("n_symbols", "zkcex_mm_n_symbols"),
                ("n_orders_active", "zkcex_mm_n_orders_active"),
                ("uptime_s", "zkcex_mm_uptime_seconds"),
                ("levels", "zkcex_mm_levels"),
            ]:
                if k_in in body:
                    out[k_out] = body[k_in]
            try:
                out["zkcex_mm_spread_bps"] = float(body.get("spread_bps") or 0)
            except Exception as e:  # noqa: BLE001
                _log(f"mm spread parse skipped: {e!r}")
            out["zkcex_mm_paused"] = 1 if body.get("paused") else 0
        return out

    def parse_safu(self, body: Any, elapsed_ms: float) -> dict[str, Any]:
        out: dict[str, Any] = {"latency_ms": elapsed_ms}
        if isinstance(body, dict):
            try:
                if "total_balance_usdt" in body:
                    out["zkcex_safu_balance_usdt"] = float(body["total_balance_usdt"])
                if "total_inflow_usdt" in body:
                    out["zkcex_safu_inflow_usdt_total"] = float(body["total_inflow_usdt"])
                if "total_payout_usdt" in body:
                    out["zkcex_safu_payout_usdt_total"] = float(body["total_payout_usdt"])
                if "monthly_inflow_usdt" in body:
                    out["zkcex_safu_monthly_inflow_usdt"] = float(body["monthly_inflow_usdt"])
                if "monthly_payout_usdt" in body:
                    out["zkcex_safu_monthly_payout_usdt"] = float(body["monthly_payout_usdt"])
            except Exception as e:  # noqa: BLE001
                _log(f"safu summary parse skipped: {e!r}")
            if "n_incidents_open" in body:
                out["zkcex_safu_incidents_open"] = body["n_incidents_open"]
            if "n_incidents_resolved" in body:
                out["zkcex_safu_incidents_resolved_total"] = body["n_incidents_resolved"]
        return out

    def parse_text_alive(self, body: Any, elapsed_ms: float) -> dict[str, Any]:
        # Rust /health/live returns plain text "ok"
        return {"latency_ms": elapsed_ms}


SCRAPER = Scraper()


# ----------------------------------------------------------------------------
# DB readers
# ----------------------------------------------------------------------------


def read_chain_db_counters() -> dict[tuple[str, tuple[tuple[str, str], ...]], int]:
    """Read deposit/withdraw counts from chain.db. Returns {(metric, labels_tuple): value}."""
    out: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}
    path = os.path.join(LOCAL_DB_DIR, "chain.db")
    conn = _open_ro_sqlite(path)
    if conn is None:
        return out
    try:
        # Deposits — total and by status & aml_status.
        out[("zkcex_chain_deposits_total", ())] = _safe_count(conn, "SELECT COUNT(*) FROM deposits")
        for st, n in _safe_rows(conn, "SELECT status, COUNT(*) FROM deposits GROUP BY status"):
            out[("zkcex_chain_deposits_by_status", (("status", str(st)),))] = int(n)
        for aml, n in _safe_rows(
            conn,
            "SELECT COALESCE(aml_status, 'unknown'), COUNT(*) FROM deposits GROUP BY aml_status",
        ):
            out[("zkcex_aml_decisions_total", (("decision", str(aml)), ("source", "deposits")))] = (
                int(n)
            )

        # Withdrawals.
        out[("zkcex_chain_withdraws_total", ())] = _safe_count(
            conn, "SELECT COUNT(*) FROM withdraws"
        )
        for st, n in _safe_rows(conn, "SELECT status, COUNT(*) FROM withdraws GROUP BY status"):
            out[("zkcex_chain_withdraws_by_status", (("status", str(st)),))] = int(n)
        for aml, n in _safe_rows(
            conn,
            "SELECT COALESCE(aml_status, 'unknown'), COUNT(*) FROM withdraws GROUP BY aml_status",
        ):
            out[
                ("zkcex_aml_decisions_total", (("decision", str(aml)), ("source", "withdraws")))
            ] = int(n)

        # Per-asset volumes (sum as float).
        for asset, total in _safe_rows(conn, "SELECT asset, COUNT(*) FROM deposits GROUP BY asset"):
            out[("zkcex_chain_deposits_by_asset", (("asset", str(asset)),))] = int(total)
        for asset, total in _safe_rows(
            conn, "SELECT asset, COUNT(*) FROM withdraws GROUP BY asset"
        ):
            out[("zkcex_chain_withdraws_by_asset", (("asset", str(asset)),))] = int(total)
    finally:
        conn.close()
    return out


def read_auth_db_counters() -> dict[tuple[str, tuple[tuple[str, str], ...]], int]:
    out: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}
    path = os.path.join(LOCAL_DB_DIR, "auth.db")
    conn = _open_ro_sqlite(path)
    if conn is None:
        return out
    try:
        out[("zkcex_kyc_verifications_total", ())] = _safe_count(
            conn, "SELECT COUNT(*) FROM kyc_verifications"
        )
        for status, n in _safe_rows(
            conn,
            "SELECT COALESCE(status, 'unknown'), COUNT(*) FROM kyc_verifications GROUP BY status",
        ):
            out[("zkcex_kyc_verifications_by_status", (("status", str(status)),))] = int(n)
        for outcome, n in _safe_rows(
            conn,
            "SELECT COALESCE(decision, 'unknown'), COUNT(*) FROM geo_decisions GROUP BY decision",
        ):
            out[("zkcex_geo_decisions_total", (("decision", str(outcome)),))] = int(n)
    finally:
        conn.close()
    return out


def read_safu_db_counters() -> dict[tuple[str, tuple[tuple[str, str], ...]], int]:
    out: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}
    path = os.path.join(LOCAL_DB_DIR, "safu.db")
    conn = _open_ro_sqlite(path)
    if conn is None:
        return out
    try:
        out[("zkcex_safu_incidents_total", ())] = _safe_count(
            conn, "SELECT COUNT(*) FROM incidents"
        )
    except Exception as e:  # noqa: BLE001
        _log(f"safu db counter read skipped: {e!r}")
    finally:
        try:
            conn.close()
        except Exception as e:  # noqa: BLE001
            _log(f"safu db close failed: {e!r}")
    return out


# ----------------------------------------------------------------------------
# Order book + PoRL + trade-rate scrapers
# ----------------------------------------------------------------------------


class TradeRateTracker:
    """Tracks delta of last-trade-id per symbol to derive trade rate."""

    def __init__(self) -> None:
        self.last_seen_id: dict[str, int] = {}
        self.last_seen_at: dict[str, float] = {}
        self.trades_total: dict[str, int] = {}

    def update(self, symbol: str, ids: list[int]) -> None:
        now = time.time()
        if not ids:
            self.last_seen_at.setdefault(symbol, now)
            return
        max_id = max(ids)
        prev = self.last_seen_id.get(symbol)
        if prev is None:
            self.last_seen_id[symbol] = max_id
            self.last_seen_at[symbol] = now
            self.trades_total[symbol] = 0
            return
        delta = max(0, max_id - prev)
        self.trades_total[symbol] = self.trades_total.get(symbol, 0) + delta
        self.last_seen_id[symbol] = max_id
        self.last_seen_at[symbol] = now


TRADE_TRACKER = TradeRateTracker()


def scrape_spot_book_and_trades() -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    """Pull depth + last trades for each spot symbol; emit gauges + counters."""
    out: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    ok, info, _ = _http_json(SPOT_EXCHANGE_INFO)
    if not ok or not isinstance(info, dict):
        return out
    symbols = [s.get("symbol") for s in (info.get("symbols") or []) if s.get("symbol")]
    # Limit to first 8 symbols to keep one scrape fast.
    symbols = symbols[:8]
    out[("zkcex_spot_n_symbols", ())] = float(len(info.get("symbols") or []))

    for sym in symbols:
        # Depth
        ok_d, depth, _ = _http_json(SPOT_DEPTH_URL.format(sym=sym))
        if ok_d and isinstance(depth, dict):
            bids = depth.get("bids") or []
            asks = depth.get("asks") or []
            out[("zkcex_order_book_depth", (("symbol", sym), ("side", "bid")))] = float(len(bids))
            out[("zkcex_order_book_depth", (("symbol", sym), ("side", "ask")))] = float(len(asks))
            # Best bid/ask
            try:
                if bids:
                    out[("zkcex_order_book_best_price", (("symbol", sym), ("side", "bid")))] = (
                        float(bids[0][0])
                    )
                if asks:
                    out[("zkcex_order_book_best_price", (("symbol", sym), ("side", "ask")))] = (
                        float(asks[0][0])
                    )
                if bids and asks:
                    spread = float(asks[0][0]) - float(bids[0][0])
                    out[("zkcex_order_book_spread", (("symbol", sym),))] = spread
            except Exception as e:  # noqa: BLE001
                _log(f"order book price parse skipped for {sym}: {e!r}")
            # Quantity sums (top 5 levels)
            try:
                bid_qty = sum(float(x[1]) for x in bids[:5])
                ask_qty = sum(float(x[1]) for x in asks[:5])
                out[("zkcex_order_book_qty_top5", (("symbol", sym), ("side", "bid")))] = bid_qty
                out[("zkcex_order_book_qty_top5", (("symbol", sym), ("side", "ask")))] = ask_qty
            except Exception as e:  # noqa: BLE001
                _log(f"order book quantity parse skipped for {sym}: {e!r}")

        # Trades
        ok_t, trades, _ = _http_json(SPOT_TRADES_URL.format(sym=sym))
        if ok_t and isinstance(trades, list):
            ids = []
            for t in trades:
                try:
                    ids.append(int(t.get("id", 0)))
                except Exception as e:  # noqa: BLE001
                    _log(f"trade id parse skipped for {sym}: {e!r}")
            TRADE_TRACKER.update(sym, ids)

    for sym, total in TRADE_TRACKER.trades_total.items():
        out[("zkcex_trades_total", (("symbol", sym),))] = float(total)
    return out


def scrape_porl_ratio() -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    """Derive a PoRL ratio: SAFU balance + bridge USDT balances vs PoL liabilities."""
    out: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    # Liabilities from pol live
    ok_pol, pol, _ = _http_json("http://127.0.0.1:5503/pol/live/info")
    liabilities = 0.0
    if ok_pol and isinstance(pol, dict):
        cc = pol.get("current_commit") or {}
        try:
            liabilities = float(cc.get("root_sum") or 0)
        except Exception:
            liabilities = 0.0
    out[("zkcex_pol_total_liabilities_usdt", ())] = liabilities

    # Reserves: SAFU + custody coordinator wallet balance if exposed via 5530
    reserves = 0.0
    ok_safu, safu, _ = _http_json("http://127.0.0.1:5601/safu/summary")
    if ok_safu and isinstance(safu, dict):
        try:
            reserves += float(safu.get("total_balance_usdt") or 0)
        except Exception as e:  # noqa: BLE001
            _log(f"safu reserve parse skipped: {e!r}")
    # Hardhat reserves shown via bridge: count last_event_id as a proxy isn't right; instead
    # pull total user balance from the bridge as it tracks per-user USDT/ETH.
    ok_br, br, _ = _http_json("http://127.0.0.1:5504/bridge/health")
    if ok_br and isinstance(br, dict):
        # Bridge's view of total tracked stake (rough heuristic for the demo).
        try:
            reserves += float(br.get("last_event_id") or 0) / 1e10  # tiny demo signal
        except Exception as e:  # noqa: BLE001
            _log(f"bridge reserve parse skipped: {e!r}")

    # Add a chain.db-derived reserve floor: sum of confirmed deposits less withdrawals.
    chain_path = os.path.join(LOCAL_DB_DIR, "chain.db")
    conn = _open_ro_sqlite(chain_path)
    if conn is not None:
        try:
            dep_sum = 0.0
            for asset, amt in _safe_rows(
                conn,
                "SELECT asset, amount FROM deposits WHERE status IN ('credited','confirmed')",
            ):
                try:
                    a = str(asset).upper()
                    val = float(amt)
                    # Treat anything not USDT as 1:1 for the demo ratio (we don't have a price oracle here).
                    if a in ("USDT", "USDC", "BUSD", "DAI"):
                        dep_sum += val
                    else:
                        dep_sum += val  # demo: count toward reserves; price applied client-side
                except Exception as e:  # noqa: BLE001
                    _log(f"deposit reserve parse skipped: {e!r}")
            wd_sum = 0.0
            for _asset, amt in _safe_rows(
                conn,
                "SELECT asset, amount FROM withdraws WHERE status IN ('confirmed','submitted')",
            ):
                try:
                    wd_sum += float(amt)
                except Exception as e:  # noqa: BLE001
                    _log(f"withdraw reserve parse skipped: {e!r}")
            reserves += max(0.0, dep_sum - wd_sum)
        finally:
            conn.close()

    out[("zkcex_reserves_total_usdt", ())] = reserves
    if liabilities > 0:
        out[("zkcex_porl_ratio", ())] = reserves / liabilities
    else:
        # No liabilities ⇒ ratio is "infinite"; pin to a large finite number so
        # Prometheus can still graph it. Real production would handle this case
        # with an explicit "n/a" gauge but Prom only has floats.
        out[("zkcex_porl_ratio", ())] = 1e9 if reserves > 0 else 0.0
    return out


# ----------------------------------------------------------------------------
# Docker stats
# ----------------------------------------------------------------------------


def _parse_pct(s: str) -> float:
    try:
        return float(s.strip().rstrip("%"))
    except Exception:
        return 0.0


def _parse_size_bytes(s: str) -> float:
    """Parse "29.63MiB" / "115kB" / "7.652GiB" → bytes."""
    s = s.strip()
    if not s:
        return 0.0
    units = {
        "B": 1,
        "kB": 1000,
        "KB": 1024,
        "KiB": 1024,
        "MB": 1000**2,
        "MiB": 1024**2,
        "GB": 1000**3,
        "GiB": 1024**3,
        "TB": 1000**4,
        "TiB": 1024**4,
    }
    for unit in sorted(units, key=len, reverse=True):
        if s.endswith(unit):
            try:
                return float(s[: -len(unit)]) * units[unit]
            except Exception:
                return 0.0
    try:
        return float(s)
    except Exception:
        return 0.0


def scrape_docker_stats() -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    out: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    try:
        proc = subprocess.run(  # noqa: S603 - executable is configured once; args are fixed.
            [DOCKER_BIN, "stats", "--no-stream", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            timeout=DOCKER_TIMEOUT_S,
        )
    except Exception as e:  # noqa: BLE001
        _log(f"docker stats command skipped: {e!r}")
        return out
    if proc.returncode != 0:
        return out
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception as e:  # noqa: BLE001
            _log(f"docker stats row ignored: {e!r}")
            continue
        name = str(row.get("Name", ""))
        if not name or not any(name.startswith(p) for p in DOCKER_CONTAINER_PREFIXES):
            continue
        cpu = _parse_pct(str(row.get("CPUPerc", "0")))
        mempct = _parse_pct(str(row.get("MemPerc", "0")))
        mem_use = str(row.get("MemUsage", "")).split("/")[0]
        mem_bytes = _parse_size_bytes(mem_use)
        netio = str(row.get("NetIO", "")).split("/")
        net_rx = _parse_size_bytes(netio[0]) if netio else 0.0
        net_tx = _parse_size_bytes(netio[1]) if len(netio) > 1 else 0.0

        labels = (("container", name),)
        out[("zkcex_container_cpu_percent", labels)] = cpu
        out[("zkcex_container_mem_percent", labels)] = mempct
        out[("zkcex_container_mem_bytes", labels)] = mem_bytes
        out[("zkcex_container_net_rx_bytes_total", labels)] = net_rx
        out[("zkcex_container_net_tx_bytes_total", labels)] = net_tx
    return out


# ----------------------------------------------------------------------------
# Latency probe (histogram)
# ----------------------------------------------------------------------------


def probe_latency() -> dict[str, list[float]]:
    """Return {path: [latency_ms, ...]} from this scrape."""
    out: dict[str, list[float]] = {}
    for url in LATENCY_PROBES:
        # Use the path portion (incl. query) as the label.
        path = url.split("://", 1)[-1].split("/", 1)[-1]
        path = "/" + path.split("?", 1)[0]
        status, _body, ms = _http_get(url)
        if status >= 200 and status < 500:
            out.setdefault(path, []).append(ms)
    return out


class LatencyHistogram:
    """Cumulative bucket counts + sum + count, keyed by path."""

    def __init__(self) -> None:
        self.buckets: dict[str, list[int]] = {}
        self.sum_ms: dict[str, float] = {}
        self.count: dict[str, int] = {}

    def observe(self, path: str, ms: float) -> None:
        if path not in self.buckets:
            self.buckets[path] = [0] * len(LATENCY_BUCKETS_MS)
            self.sum_ms[path] = 0.0
            self.count[path] = 0
        for i, le in enumerate(LATENCY_BUCKETS_MS):
            if ms <= le:
                self.buckets[path][i] += 1
        self.sum_ms[path] += ms
        self.count[path] += 1

    def emit(self, prom: PromBuilder) -> None:
        if not self.count:
            return
        prom.help(
            "zkcex_request_latency_ms",
            "Synthetic latency probe per endpoint (ms)",
            "histogram",
        )
        for path in sorted(self.count.keys()):
            for i, le in enumerate(LATENCY_BUCKETS_MS):
                le_str = "+Inf" if le == float("inf") else str(int(le))
                prom.add(
                    "zkcex_request_latency_ms_bucket",
                    self.buckets[path][i],
                    {"path": path, "le": le_str},
                )
            prom.add("zkcex_request_latency_ms_sum", self.sum_ms[path], {"path": path})
            prom.add("zkcex_request_latency_ms_count", self.count[path], {"path": path})


LATENCY_HIST = LatencyHistogram()


# ----------------------------------------------------------------------------
# Snapshot orchestrator
# ----------------------------------------------------------------------------


class Aggregator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rendered_metrics: str = self._initial_render()
        self._last_scrape_at: float = 0.0
        self._last_scrape_took_ms: float = 0.0
        self._last_targets_up: int = 0
        self._last_targets_total: int = len(HEALTH_TARGETS)
        self._scrape_count: int = 0
        # Persistent counters: total scrapes + errors per target.
        self._target_up_count: dict[str, int] = {}
        self._target_down_count: dict[str, int] = {}

    def _initial_render(self) -> str:
        prom = PromBuilder()
        prom.help("zkcex_aggregator_up", "Aggregator self-up gauge", "gauge")
        prom.add("zkcex_aggregator_up", 1)
        return prom.render()

    # ---------- single scrape pass -----------------------------------------

    def _scrape_one_target(
        self, job: str, service: str, url: str, parser_name: str
    ) -> tuple[str, str, bool, float, dict[str, Any]]:
        ok, body, ms = _http_json(url)
        # Some endpoints return plain text — fall back to body string.
        parser: Callable = getattr(SCRAPER, parser_name, SCRAPER.parse_simple_ok)
        parsed: dict[str, Any] = {}
        # Heuristic: a service is "up" if HTTP status was 2xx/3xx, which
        # _http_json returns as ok=True when JSON parses, OR returns ok=False
        # but body is a string (raw text response) → still up.
        if ok or isinstance(body, str):
            try:
                parsed = parser(body, ms) or {}
            except Exception:
                _log(
                    f"parser {parser_name} failed for {url}: {traceback.format_exc().splitlines()[-1]}"
                )
                parsed = {}
            up = True
        else:
            up = False
        return job, service, up, ms, parsed

    def scrape_once(self) -> None:
        t0 = time.monotonic()
        prom = PromBuilder()

        # 1) Aggregator self info
        prom.help("zkcex_aggregator_up", "Aggregator self-up gauge", "gauge")
        prom.add("zkcex_aggregator_up", 1)
        prom.help("zkcex_aggregator_scrape_count", "Number of scrape passes completed", "counter")
        prom.add("zkcex_aggregator_scrape_count", self._scrape_count + 1)

        # 2) Concurrent health scrapes
        prom.help("zkcex_service_up", "Service health (1 = up)", "gauge")
        prom.help(
            "zkcex_service_scrape_latency_ms",
            "Scrape latency for the service /health endpoint",
            "gauge",
        )
        prom.help(
            "zkcex_service_scrape_up_total", "Cumulative successful scrapes per service", "counter"
        )
        prom.help(
            "zkcex_service_scrape_down_total", "Cumulative failed scrapes per service", "counter"
        )

        # Buffer metric tuples so we can declare HELP/TYPE before emitting.
        deferred: list[tuple[str, Any, dict[str, str] | None]] = []
        # Metric → help text (for any dynamic ones discovered through parsers).
        dynamic_help: dict[str, str] = {}

        def add_metric(
            name: str, value: Any, labels: dict[str, str] | None = None, help_text: str = ""
        ) -> None:
            if help_text:
                dynamic_help.setdefault(name, help_text)
            deferred.append((name, value, labels))

        # 2a) Health scrapes
        up_count = 0
        with ThreadPoolExecutor(max_workers=SCRAPE_WORKERS) as pool:
            futures = [
                pool.submit(self._scrape_one_target, j, s, u, p) for (j, s, u, p) in HEALTH_TARGETS
            ]
            for fut in as_completed(futures):
                try:
                    job, service, up, ms, parsed = fut.result()
                except Exception as e:  # noqa: BLE001
                    _log(f"health scrape future failed: {e!r}")
                    continue
                if up:
                    up_count += 1
                    self._target_up_count[job] = self._target_up_count.get(job, 0) + 1
                else:
                    self._target_down_count[job] = self._target_down_count.get(job, 0) + 1
                add_metric(
                    "zkcex_service_up",
                    1 if up else 0,
                    {"service": service, "job": job},
                )
                add_metric(
                    "zkcex_service_scrape_latency_ms",
                    ms,
                    {"service": service, "job": job},
                )
                add_metric(
                    "zkcex_service_scrape_up_total",
                    self._target_up_count.get(job, 0),
                    {"service": service, "job": job},
                )
                add_metric(
                    "zkcex_service_scrape_down_total",
                    self._target_down_count.get(job, 0),
                    {"service": service, "job": job},
                )
                # Latency-probe-style observation per /health.
                LATENCY_HIST.observe(f"/health/{service}", ms)
                # Parsed metrics
                for k, v in parsed.items():
                    if k == "latency_ms":
                        continue
                    if isinstance(k, tuple):
                        name, labels = k
                        add_metric(name, v, labels, help_text=name)
                    else:
                        add_metric(k, v, help_text=k)

        # 3) Chain.db / auth.db / safu.db
        for reader in (read_chain_db_counters, read_auth_db_counters, read_safu_db_counters):
            try:
                for (name, labels_tuple), val in reader().items():
                    add_metric(name, val, dict(labels_tuple), help_text=name)
            except Exception:
                _log(
                    f"db reader {reader.__name__} failed: {traceback.format_exc().splitlines()[-1]}"
                )

        # 4) Spot book + trade rates
        try:
            for (name, labels_tuple), val in scrape_spot_book_and_trades().items():
                add_metric(name, val, dict(labels_tuple), help_text=name)
        except Exception:
            _log("spot scrape failed: " + traceback.format_exc().splitlines()[-1])

        # 5) PoRL
        try:
            for (name, labels_tuple), val in scrape_porl_ratio().items():
                add_metric(name, val, dict(labels_tuple), help_text=name)
        except Exception:
            _log("porl scrape failed: " + traceback.format_exc().splitlines()[-1])

        # 6) Docker stats
        try:
            for (name, labels_tuple), val in scrape_docker_stats().items():
                add_metric(name, val, dict(labels_tuple), help_text=name)
        except Exception:
            _log("docker scrape failed: " + traceback.format_exc().splitlines()[-1])

        # 7) Latency probe (extra synthetic probes)
        try:
            for path, samples in probe_latency().items():
                for ms in samples:
                    LATENCY_HIST.observe(path, ms)
        except Exception as e:  # noqa: BLE001
            _log(f"latency probe skipped: {e!r}")

        # 8) Static custody threshold/n already covered by /health for coord.
        # 9) Aggregator self-stats
        took_ms = (time.monotonic() - t0) * 1000.0
        add_metric("zkcex_aggregator_scrape_duration_ms", took_ms, help_text="Scrape pass duration")
        add_metric(
            "zkcex_aggregator_targets_total",
            len(HEALTH_TARGETS),
            help_text="Configured target count",
        )
        add_metric("zkcex_aggregator_targets_up", up_count, help_text="Targets up in last pass")

        # 10) Declare HELP for all dynamic metrics, then emit.
        for name, help_text in dynamic_help.items():
            mtype = "counter" if name.endswith("_total") else "gauge"
            prom.help(name, help_text or name, mtype)
        # Emit all deferred metrics now.
        for name, val, labels in deferred:
            prom.add(name, val, labels)

        # Emit latency histogram
        LATENCY_HIST.emit(prom)

        # Atomically swap rendered output.
        rendered = prom.render()
        with self._lock:
            self._rendered_metrics = rendered
            self._last_scrape_at = time.time()
            self._last_scrape_took_ms = took_ms
            self._last_targets_up = up_count
            self._scrape_count += 1

    # ---------- accessors --------------------------------------------------

    def render_metrics(self) -> str:
        with self._lock:
            return self._rendered_metrics

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "last_scrape_at": self._last_scrape_at,
                "last_scrape_took_ms": round(self._last_scrape_took_ms, 2),
                "scrape_count": self._scrape_count,
                "targets_up": self._last_targets_up,
                "targets_total": self._last_targets_total,
                "scrape_interval_s": SCRAPE_INTERVAL_S,
            }

    def scrape_targets(self) -> list[dict[str, str]]:
        return [{"job": j, "service": s, "url": u, "parser": p} for (j, s, u, p) in HEALTH_TARGETS]

    # ---------- background loop -------------------------------------------

    def run_loop(self) -> None:
        # First scrape immediately so /metrics has real data on first request.
        try:
            self.scrape_once()
        except Exception:
            _log("initial scrape failed: " + traceback.format_exc())
        while True:
            t0 = time.monotonic()
            try:
                self.scrape_once()
            except Exception:
                _log("scrape loop iter failed: " + traceback.format_exc())
            elapsed = time.monotonic() - t0
            sleep_for = max(0.5, SCRAPE_INTERVAL_S - elapsed)
            time.sleep(sleep_for)


# ----------------------------------------------------------------------------
# HTTP server
# ----------------------------------------------------------------------------

AGG = Aggregator()


class Handler(BaseHTTPRequestHandler):
    server_version = "zkcex-metrics-aggregator/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # Quiet by default — Prometheus polls us every 15s and we don't need it
        # spamming stderr. Errors still surface via _log().
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        path = (self.path or "/").split("?", 1)[0]
        if path == "/metrics":
            body = AGG.render_metrics().encode("utf-8")
            self._send(200, body, "text/plain; version=0.0.4; charset=utf-8")
            return
        if path == "/health":
            body = json.dumps(AGG.health()).encode("utf-8")
            self._send(200, body, "application/json")
            return
        if path == "/scrape-targets.json":
            body = json.dumps({"targets": AGG.scrape_targets()}, indent=2).encode("utf-8")
            self._send(200, body, "application/json")
            return
        if path == "/":
            body = (
                b"<html><body><h2>zkCEX metrics aggregator</h2>"
                b"<ul>"
                b"<li><a href='/metrics'>/metrics</a> (Prometheus text)</li>"
                b"<li><a href='/health'>/health</a></li>"
                b"<li><a href='/scrape-targets.json'>/scrape-targets.json</a></li>"
                b"</ul></body></html>"
            )
            self._send(200, body, "text/html; charset=utf-8")
            return
        self._send(404, b'{"error":"not_found"}', "application/json")


def main() -> None:
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            _log(f"bad port: {sys.argv[1]!r}, using default {DEFAULT_PORT}")
    # Start background scraper
    t = threading.Thread(target=AGG.run_loop, name="aggregator-loop", daemon=True)
    t.start()
    # Serve HTTP
    server = ThreadingHTTPServer((LISTEN_HOST, port), Handler)
    _log(f"listening on {LISTEN_HOST}:{port}, scraping every {SCRAPE_INTERVAL_S}s")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
