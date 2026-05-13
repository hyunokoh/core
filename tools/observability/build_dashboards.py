#!/usr/bin/env python3
"""
Generate the zkCEX Grafana dashboards as deterministic JSON.

Grafana dashboards stored in version control are notoriously painful because
the UI re-shuffles panel IDs and timestamps on every save. We keep things
deterministic by *generating* them here from Python.

Run:
    python3 build_dashboards.py

Output:
    dashboards/01-overview.json
    dashboards/02-trading.json
    dashboards/03-compliance.json
    dashboards/04-pol-porl.json
    dashboards/05-custody.json
    dashboards/06-services.json
"""

from __future__ import annotations

import json
import os
from typing import Any

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboards")
DS_PROM = {"type": "prometheus", "uid": "prometheus"}
DS_LOKI = {"type": "loki", "uid": "loki"}

# Layout helpers ---------------------------------------------------------------
# Grafana uses a 24-col grid. We arrange panels with simple helpers.


def _id(counter: list[int]) -> int:
    counter[0] += 1
    return counter[0]


def stat_panel(
    pid: int,
    title: str,
    expr: str,
    *,
    x: int,
    y: int,
    w: int = 4,
    h: int = 4,
    unit: str = "short",
    thresholds: list[dict[str, Any]] | None = None,
    decimals: int = 2,
    ds: dict[str, str] | None = None,
    color_mode: str = "value",
    legend: str = "",
) -> dict[str, Any]:
    thresholds = thresholds or [{"color": "green", "value": None}]
    return {
        "id": pid,
        "type": "stat",
        "title": title,
        "datasource": ds or DS_PROM,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "orientation": "auto",
            "textMode": "auto",
            "colorMode": color_mode,
            "graphMode": "area",
            "justifyMode": "auto",
        },
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "decimals": decimals,
                "thresholds": {"mode": "absolute", "steps": thresholds},
                "color": {"mode": "thresholds"},
            },
            "overrides": [],
        },
        "targets": [
            {
                "expr": expr,
                "refId": "A",
                "legendFormat": legend,
            }
        ],
    }


def timeseries_panel(
    pid: int,
    title: str,
    queries: list[dict[str, str]],
    *,
    x: int,
    y: int,
    w: int = 12,
    h: int = 8,
    unit: str = "short",
    stacking: str | None = None,
    fill_opacity: int = 10,
    ds: dict[str, str] | None = None,
) -> dict[str, Any]:
    custom: dict[str, Any] = {
        "drawStyle": "line",
        "lineInterpolation": "linear",
        "spanNulls": True,
        "lineWidth": 1,
        "fillOpacity": fill_opacity,
        "pointSize": 4,
        "showPoints": "never",
    }
    if stacking:
        custom["stacking"] = {"mode": stacking, "group": "A"}
    return {
        "id": pid,
        "type": "timeseries",
        "title": title,
        "datasource": ds or DS_PROM,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": custom,
                "color": {"mode": "palette-classic"},
            },
            "overrides": [],
        },
        "options": {
            "tooltip": {"mode": "multi", "sort": "desc"},
            "legend": {"showLegend": True, "displayMode": "table", "placement": "right"},
        },
        "targets": [
            {
                "expr": q["expr"],
                "refId": chr(65 + i),
                "legendFormat": q.get("legend", "{{ __name__ }}"),
            }
            for i, q in enumerate(queries)
        ],
    }


def gauge_panel(
    pid: int,
    title: str,
    expr: str,
    *,
    x: int,
    y: int,
    w: int = 6,
    h: int = 6,
    unit: str = "short",
    min_v: float = 0,
    max_v: float = 100,
    thresholds: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    thresholds = thresholds or [
        {"color": "red", "value": None},
        {"color": "orange", "value": 1},
        {"color": "green", "value": 1.05},
    ]
    return {
        "id": pid,
        "type": "gauge",
        "title": title,
        "datasource": DS_PROM,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "showThresholdLabels": False,
            "showThresholdMarkers": True,
            "orientation": "auto",
        },
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "min": min_v,
                "max": max_v,
                "thresholds": {"mode": "absolute", "steps": thresholds},
                "color": {"mode": "thresholds"},
            }
        },
        "targets": [{"expr": expr, "refId": "A"}],
    }


def table_panel(
    pid: int,
    title: str,
    expr: str,
    *,
    x: int,
    y: int,
    w: int = 12,
    h: int = 8,
) -> dict[str, Any]:
    return {
        "id": pid,
        "type": "table",
        "title": title,
        "datasource": DS_PROM,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "options": {"showHeader": True},
        "fieldConfig": {"defaults": {"custom": {"align": "auto"}}, "overrides": []},
        "targets": [{"expr": expr, "refId": "A", "instant": True, "format": "table"}],
    }


def heatmap_panel(
    pid: int,
    title: str,
    expr: str,
    *,
    x: int,
    y: int,
    w: int = 12,
    h: int = 8,
) -> dict[str, Any]:
    return {
        "id": pid,
        "type": "heatmap",
        "title": title,
        "datasource": DS_PROM,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "options": {
            "calculate": False,
            "cellGap": 1,
            "color": {"scheme": "Spectral", "mode": "scheme", "steps": 64},
            "yAxis": {"unit": "ms"},
        },
        "targets": [
            {
                "expr": expr,
                "refId": "A",
                "format": "heatmap",
                "legendFormat": "{{ le }}",
            }
        ],
    }


def row(pid: int, title: str, y: int) -> dict[str, Any]:
    return {
        "id": pid,
        "type": "row",
        "title": title,
        "collapsed": False,
        "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
        "panels": [],
    }


def logs_panel(
    pid: int,
    title: str,
    expr: str,
    *,
    x: int,
    y: int,
    w: int = 24,
    h: int = 8,
) -> dict[str, Any]:
    return {
        "id": pid,
        "type": "logs",
        "title": title,
        "datasource": DS_LOKI,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "options": {
            "showTime": True,
            "wrapLogMessage": False,
            "sortOrder": "Descending",
            "dedupStrategy": "none",
            "enableLogDetails": True,
        },
        "targets": [{"expr": expr, "refId": "A"}],
    }


def base_dashboard(
    uid: str, title: str, panels: list[dict[str, Any]], tags: list[str]
) -> dict[str, Any]:
    return {
        "annotations": {"list": []},
        "editable": True,
        "fiscalYearStartMonth": 0,
        "graphTooltip": 1,
        "id": None,
        "links": [],
        "liveNow": True,
        "panels": panels,
        "refresh": "15s",
        "schemaVersion": 39,
        "tags": ["zkcex"] + tags,
        "templating": {"list": []},
        "time": {"from": "now-30m", "to": "now"},
        "timepicker": {},
        "timezone": "",
        "title": title,
        "uid": uid,
        "version": 1,
        "weekStart": "",
    }


# ----------------------------------------------------------------------------
# Dashboards
# ----------------------------------------------------------------------------

UP_STEPS = [
    {"color": "red", "value": None},
    {"color": "green", "value": 0.5},
]
LATENCY_STEPS = [
    {"color": "green", "value": None},
    {"color": "orange", "value": 100},
    {"color": "red", "value": 500},
]
RATIO_STEPS = [
    {"color": "red", "value": None},
    {"color": "orange", "value": 1.0},
    {"color": "green", "value": 1.05},
]


def build_overview() -> dict[str, Any]:
    c = [0]
    panels: list[dict[str, Any]] = []

    panels.append(row(_id(c), "At a glance", 0))
    services = [
        ("auth", "Auth"),
        ("chain", "Chain"),
        ("pol_py", "PoL"),
        ("zkpol_bridge", "PoL Bridge"),
        ("ws", "WebSocket"),
        ("custody_coord", "Custody"),
        ("orders", "Orders"),
        ("perp", "Perp"),
        ("safu", "SAFU"),
        ("mm_bot", "MM Bot"),
        ("mcp", "MCP"),
        ("api_key", "API Keys"),
    ]
    x = 0
    y = 1
    for svc, label in services:
        panels.append(
            stat_panel(
                _id(c),
                label,
                f'min(zkcex_service_up{{service="{svc}"}})',
                x=x,
                y=y,
                w=2,
                h=3,
                unit="short",
                thresholds=UP_STEPS,
                color_mode="background",
            )
        )
        x += 2
        if x >= 24:
            x = 0
            y += 3

    y += 4
    panels.append(row(_id(c), "Key business indicators", y))
    y += 1
    panels.append(
        stat_panel(
            _id(c),
            "Registered users",
            "zkcex_user_count",
            x=0,
            y=y,
            w=4,
            h=4,
            unit="short",
            decimals=0,
        )
    )
    panels.append(
        stat_panel(
            _id(c),
            "Active sessions",
            "zkcex_active_sessions",
            x=4,
            y=y,
            w=4,
            h=4,
            unit="short",
            decimals=0,
        )
    )
    panels.append(
        stat_panel(
            _id(c),
            "Total deposits",
            "zkcex_chain_deposits_total",
            x=8,
            y=y,
            w=4,
            h=4,
            unit="short",
            decimals=0,
        )
    )
    panels.append(
        stat_panel(
            _id(c),
            "Total withdrawals",
            "zkcex_chain_withdraws_total",
            x=12,
            y=y,
            w=4,
            h=4,
            unit="short",
            decimals=0,
        )
    )
    panels.append(
        stat_panel(
            _id(c),
            "SAFU balance (USDT)",
            "zkcex_safu_balance_usdt",
            x=16,
            y=y,
            w=4,
            h=4,
            unit="none",
            decimals=2,
        )
    )
    panels.append(
        gauge_panel(
            _id(c),
            "PoRL ratio",
            "zkcex_porl_ratio",
            x=20,
            y=y,
            w=4,
            h=8,
            unit="none",
            min_v=0,
            max_v=10,
            thresholds=RATIO_STEPS,
        )
    )

    y += 4
    panels.append(
        stat_panel(
            _id(c),
            "Order book symbols",
            "zkcex_spot_n_symbols",
            x=0,
            y=y,
            w=4,
            h=4,
            unit="short",
            decimals=0,
        )
    )
    panels.append(
        stat_panel(
            _id(c),
            "Custody nodes reachable",
            "zkcex_custody_nodes_reachable",
            x=4,
            y=y,
            w=4,
            h=4,
            unit="short",
            decimals=0,
            thresholds=[
                {"color": "red", "value": None},
                {"color": "orange", "value": 3},
                {"color": "green", "value": 5},
            ],
        )
    )
    panels.append(
        stat_panel(
            _id(c),
            "WebSocket clients",
            "zkcex_ws_clients",
            x=8,
            y=y,
            w=4,
            h=4,
            unit="short",
            decimals=0,
        )
    )
    panels.append(
        stat_panel(
            _id(c),
            "Open orders (orders svc)",
            "zkcex_orders_pending",
            x=12,
            y=y,
            w=4,
            h=4,
            unit="short",
            decimals=0,
        )
    )
    panels.append(
        stat_panel(
            _id(c),
            "PoL liabilities (USDT)",
            "zkcex_pol_total_liabilities_usdt",
            x=16,
            y=y,
            w=4,
            h=4,
            unit="none",
            decimals=2,
        )
    )

    y += 4
    panels.append(row(_id(c), "Trends", y))
    y += 1
    panels.append(
        timeseries_panel(
            _id(c),
            "Spot trade rate (1m)",
            [
                {
                    "expr": "sum by (symbol) (rate(zkcex_trades_total[1m]))",
                    "legend": "{{ symbol }}",
                },
            ],
            x=0,
            y=y,
            w=12,
            h=8,
            unit="reqps",
        )
    )
    panels.append(
        timeseries_panel(
            _id(c),
            "AML decisions/min by outcome",
            [
                {
                    "expr": "sum by (decision) (rate(zkcex_aml_decisions_total[5m]) * 60)",
                    "legend": "{{ decision }}",
                },
            ],
            x=12,
            y=y,
            w=12,
            h=8,
            unit="short",
            stacking="normal",
        )
    )

    y += 8
    panels.append(
        timeseries_panel(
            _id(c),
            "Service scrape latency",
            [
                {"expr": "zkcex_service_scrape_latency_ms", "legend": "{{ service }}"},
            ],
            x=0,
            y=y,
            w=12,
            h=8,
            unit="ms",
        )
    )
    panels.append(
        timeseries_panel(
            _id(c),
            "Spot p95/p99 latency",
            [
                {
                    "expr": "histogram_quantile(0.95, sum by (le) (rate(zkcex_request_latency_ms_bucket[5m])))",
                    "legend": "p95",
                },
                {
                    "expr": "histogram_quantile(0.99, sum by (le) (rate(zkcex_request_latency_ms_bucket[5m])))",
                    "legend": "p99",
                },
            ],
            x=12,
            y=y,
            w=12,
            h=8,
            unit="ms",
        )
    )

    return base_dashboard("zkcex-overview", "zkCEX — Overview", panels, ["overview"])


def build_trading() -> dict[str, Any]:
    c = [0]
    panels: list[dict[str, Any]] = []
    panels.append(row(_id(c), "Order book", 0))
    y = 1
    panels.append(
        timeseries_panel(
            _id(c),
            "Order book depth (bids)",
            [
                {"expr": 'zkcex_order_book_depth{side="bid"}', "legend": "{{ symbol }}"},
            ],
            x=0,
            y=y,
            w=12,
            h=8,
        )
    )
    panels.append(
        timeseries_panel(
            _id(c),
            "Order book depth (asks)",
            [
                {"expr": 'zkcex_order_book_depth{side="ask"}', "legend": "{{ symbol }}"},
            ],
            x=12,
            y=y,
            w=12,
            h=8,
        )
    )
    y += 8
    panels.append(
        timeseries_panel(
            _id(c),
            "Best bid",
            [
                {"expr": 'zkcex_order_book_best_price{side="bid"}', "legend": "{{ symbol }}"},
            ],
            x=0,
            y=y,
            w=12,
            h=8,
        )
    )
    panels.append(
        timeseries_panel(
            _id(c),
            "Best ask",
            [
                {"expr": 'zkcex_order_book_best_price{side="ask"}', "legend": "{{ symbol }}"},
            ],
            x=12,
            y=y,
            w=12,
            h=8,
        )
    )
    y += 8
    panels.append(
        timeseries_panel(
            _id(c),
            "Bid/ask spread",
            [
                {"expr": "zkcex_order_book_spread", "legend": "{{ symbol }}"},
            ],
            x=0,
            y=y,
            w=12,
            h=8,
        )
    )
    panels.append(
        timeseries_panel(
            _id(c),
            "Top-5 quantity (bid + ask)",
            [
                {"expr": "zkcex_order_book_qty_top5", "legend": "{{ symbol }} {{ side }}"},
            ],
            x=12,
            y=y,
            w=12,
            h=8,
        )
    )

    y += 8
    panels.append(row(_id(c), "Trade flow", y))
    y += 1
    panels.append(
        timeseries_panel(
            _id(c),
            "Trades/sec by symbol",
            [
                {
                    "expr": "sum by (symbol) (rate(zkcex_trades_total[1m]))",
                    "legend": "{{ symbol }}",
                },
            ],
            x=0,
            y=y,
            w=12,
            h=8,
            unit="reqps",
            stacking="normal",
        )
    )
    panels.append(
        stat_panel(
            _id(c),
            "Cumulative trades observed",
            "sum(zkcex_trades_total)",
            x=12,
            y=y,
            w=6,
            h=4,
            unit="short",
            decimals=0,
        )
    )
    panels.append(
        stat_panel(
            _id(c),
            "Symbols listed",
            "zkcex_spot_n_symbols",
            x=18,
            y=y,
            w=6,
            h=4,
            unit="short",
            decimals=0,
        )
    )
    panels.append(
        stat_panel(
            _id(c),
            "MM bot orders active",
            "zkcex_mm_n_orders_active",
            x=12,
            y=y + 4,
            w=6,
            h=4,
            unit="short",
            decimals=0,
        )
    )
    panels.append(
        stat_panel(
            _id(c),
            "MM spread (bps)",
            "zkcex_mm_spread_bps",
            x=18,
            y=y + 4,
            w=6,
            h=4,
            unit="short",
            decimals=1,
        )
    )

    y += 8
    panels.append(row(_id(c), "Latency", y))
    y += 1
    panels.append(
        heatmap_panel(
            _id(c),
            "Spot latency heatmap",
            "sum by (le) (rate(zkcex_request_latency_ms_bucket[1m]))",
            x=0,
            y=y,
            w=12,
            h=8,
        )
    )
    panels.append(
        timeseries_panel(
            _id(c),
            "Spot p50/p95/p99 latency",
            [
                {
                    "expr": "histogram_quantile(0.50, sum by (le) (rate(zkcex_request_latency_ms_bucket[5m])))",
                    "legend": "p50",
                },
                {
                    "expr": "histogram_quantile(0.95, sum by (le) (rate(zkcex_request_latency_ms_bucket[5m])))",
                    "legend": "p95",
                },
                {
                    "expr": "histogram_quantile(0.99, sum by (le) (rate(zkcex_request_latency_ms_bucket[5m])))",
                    "legend": "p99",
                },
            ],
            x=12,
            y=y,
            w=12,
            h=8,
            unit="ms",
        )
    )

    y += 8
    panels.append(row(_id(c), "WebSocket", y))
    y += 1
    panels.append(
        stat_panel(
            _id(c), "Connected WS clients", "zkcex_ws_clients", x=0, y=y, w=6, h=4, decimals=0
        )
    )
    panels.append(stat_panel(_id(c), "Streams", "zkcex_ws_streams", x=6, y=y, w=6, h=4, decimals=0))
    panels.append(
        stat_panel(
            _id(c), "Active symbols", "zkcex_ws_active_symbols", x=12, y=y, w=6, h=4, decimals=0
        )
    )
    panels.append(
        stat_panel(
            _id(c),
            "Dropped frames (lifetime)",
            "zkcex_ws_dropped_frames_total",
            x=18,
            y=y,
            w=6,
            h=4,
            decimals=0,
            thresholds=[
                {"color": "green", "value": None},
                {"color": "orange", "value": 1},
                {"color": "red", "value": 100},
            ],
        )
    )
    panels.append(
        timeseries_panel(
            _id(c),
            "WS dropped frames/sec",
            [
                {"expr": "rate(zkcex_ws_dropped_frames_total[1m])", "legend": "drops/s"},
            ],
            x=0,
            y=y + 4,
            w=24,
            h=6,
            unit="reqps",
        )
    )

    return base_dashboard("zkcex-trading", "zkCEX — Trading", panels, ["trading"])


def build_compliance() -> dict[str, Any]:
    c = [0]
    panels: list[dict[str, Any]] = []
    panels.append(row(_id(c), "AML", 0))
    y = 1
    panels.append(
        timeseries_panel(
            _id(c),
            "AML decisions (stacked, per minute)",
            [
                {
                    "expr": "sum by (decision) (rate(zkcex_aml_decisions_total[5m]) * 60)",
                    "legend": "{{ decision }}",
                },
            ],
            x=0,
            y=y,
            w=18,
            h=8,
            unit="short",
            stacking="normal",
        )
    )
    panels.append(
        stat_panel(
            _id(c),
            "Blocked total",
            'sum(zkcex_aml_decisions_total{decision="block"})',
            x=18,
            y=y,
            w=6,
            h=4,
            unit="short",
            decimals=0,
            thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 10}],
        )
    )
    panels.append(
        stat_panel(
            _id(c),
            "Allowed total",
            'sum(zkcex_aml_decisions_total{decision="allow"})',
            x=18,
            y=y + 4,
            w=6,
            h=4,
            unit="short",
            decimals=0,
        )
    )

    y += 8
    panels.append(
        table_panel(
            _id(c),
            "AML decisions by source × outcome",
            "sum by (source, decision) (zkcex_aml_decisions_total)",
            x=0,
            y=y,
            w=12,
            h=8,
        )
    )
    panels.append(
        timeseries_panel(
            _id(c),
            "Block rate (1m)",
            [
                {
                    "expr": 'rate(zkcex_aml_decisions_total{decision="block"}[1m])',
                    "legend": "{{ source }}",
                },
            ],
            x=12,
            y=y,
            w=12,
            h=8,
            unit="reqps",
        )
    )

    y += 8
    panels.append(row(_id(c), "KYC", y))
    y += 1
    panels.append(
        stat_panel(
            _id(c),
            "Total KYC verifications",
            "zkcex_kyc_verifications_total",
            x=0,
            y=y,
            w=6,
            h=4,
            decimals=0,
        )
    )
    panels.append(
        timeseries_panel(
            _id(c),
            "KYC by status",
            [
                {"expr": "zkcex_kyc_verifications_by_status", "legend": "{{ status }}"},
            ],
            x=6,
            y=y,
            w=18,
            h=8,
        )
    )

    y += 8
    panels.append(row(_id(c), "Geo / withdraw flow", y))
    y += 1
    panels.append(
        timeseries_panel(
            _id(c),
            "Geo decisions/min",
            [
                {
                    "expr": "sum by (decision) (rate(zkcex_geo_decisions_total[5m]) * 60)",
                    "legend": "{{ decision }}",
                },
            ],
            x=0,
            y=y,
            w=12,
            h=8,
            stacking="normal",
        )
    )
    panels.append(
        timeseries_panel(
            _id(c),
            "Withdraw rate (1m)",
            [
                {"expr": "rate(zkcex_chain_withdraws_total[1m])", "legend": "withdraws/s"},
            ],
            x=12,
            y=y,
            w=12,
            h=8,
            unit="reqps",
        )
    )
    y += 8
    panels.append(
        table_panel(
            _id(c),
            "Withdraws by status",
            "sum by (status) (zkcex_chain_withdraws_by_status)",
            x=0,
            y=y,
            w=12,
            h=6,
        )
    )
    panels.append(
        table_panel(
            _id(c),
            "Deposits by status",
            "sum by (status) (zkcex_chain_deposits_by_status)",
            x=12,
            y=y,
            w=12,
            h=6,
        )
    )

    return base_dashboard("zkcex-compliance", "zkCEX — Compliance", panels, ["compliance"])


def build_pol_porl() -> dict[str, Any]:
    c = [0]
    panels: list[dict[str, Any]] = []
    panels.append(row(_id(c), "Proof of Reserves vs Liabilities", 0))
    y = 1
    panels.append(
        gauge_panel(
            _id(c),
            "PoRL ratio (R/L)",
            "zkcex_porl_ratio",
            x=0,
            y=y,
            w=8,
            h=8,
            unit="none",
            min_v=0,
            max_v=10,
            thresholds=RATIO_STEPS,
        )
    )
    panels.append(
        stat_panel(
            _id(c),
            "Total reserves (USDT)",
            "zkcex_reserves_total_usdt",
            x=8,
            y=y,
            w=8,
            h=4,
            unit="none",
            decimals=2,
        )
    )
    panels.append(
        stat_panel(
            _id(c),
            "Total liabilities (USDT)",
            "zkcex_pol_total_liabilities_usdt",
            x=16,
            y=y,
            w=8,
            h=4,
            unit="none",
            decimals=2,
        )
    )
    panels.append(
        stat_panel(
            _id(c),
            "PoL current commit",
            'max(zkcex_pol_epoch{scheme="python-live"})',
            x=8,
            y=y + 4,
            w=8,
            h=4,
            unit="short",
            decimals=0,
        )
    )
    panels.append(
        stat_panel(
            _id(c),
            "PoL n_users",
            "max(zkcex_pol_n_users)",
            x=16,
            y=y + 4,
            w=8,
            h=4,
            unit="short",
            decimals=0,
        )
    )

    y += 8
    panels.append(
        timeseries_panel(
            _id(c),
            "PoRL ratio over time",
            [
                {"expr": "zkcex_porl_ratio", "legend": "R/L"},
            ],
            x=0,
            y=y,
            w=12,
            h=8,
            unit="none",
        )
    )
    panels.append(
        timeseries_panel(
            _id(c),
            "Reserves vs Liabilities (USDT)",
            [
                {"expr": "zkcex_reserves_total_usdt", "legend": "reserves"},
                {"expr": "zkcex_pol_total_liabilities_usdt", "legend": "liabilities"},
            ],
            x=12,
            y=y,
            w=12,
            h=8,
            unit="none",
        )
    )

    y += 8
    panels.append(row(_id(c), "PoL epoch freshness", y))
    y += 1
    panels.append(
        timeseries_panel(
            _id(c),
            "PoL epoch advance",
            [
                {"expr": "zkcex_pol_epoch", "legend": "{{ scheme }}"},
            ],
            x=0,
            y=y,
            w=12,
            h=8,
            unit="short",
        )
    )
    panels.append(
        timeseries_panel(
            _id(c),
            "PoL commit age (seconds)",
            [
                {"expr": "time() - zkcex_pol_epoch_started_at", "legend": "{{ scheme }} age"},
            ],
            x=12,
            y=y,
            w=12,
            h=8,
            unit="s",
        )
    )

    y += 8
    panels.append(
        stat_panel(
            _id(c),
            "PoL max commit lag (s)",
            "zkcex_pol_max_lag_seconds",
            x=0,
            y=y,
            w=6,
            h=4,
            unit="s",
            decimals=2,
            thresholds=[
                {"color": "green", "value": None},
                {"color": "orange", "value": 60},
                {"color": "red", "value": 300},
            ],
        )
    )
    panels.append(
        stat_panel(
            _id(c),
            "Bridge last tick age (s)",
            "time() - zkcex_bridge_last_tick_at",
            x=6,
            y=y,
            w=6,
            h=4,
            unit="s",
            decimals=0,
        )
    )
    panels.append(
        stat_panel(
            _id(c),
            "Bridge users tracked",
            "zkcex_bridge_n_users_tracked",
            x=12,
            y=y,
            w=6,
            h=4,
            decimals=0,
        )
    )
    panels.append(
        stat_panel(
            _id(c),
            "Bridge last event id",
            "zkcex_bridge_last_event_id",
            x=18,
            y=y,
            w=6,
            h=4,
            decimals=0,
        )
    )

    return base_dashboard("zkcex-pol-porl", "zkCEX — PoL / PoRL", panels, ["pol", "porl"])


def build_custody() -> dict[str, Any]:
    c = [0]
    panels: list[dict[str, Any]] = []
    panels.append(row(_id(c), "Threshold custody fleet", 0))
    y = 1
    panels.append(
        stat_panel(
            _id(c),
            "Reachable nodes",
            "zkcex_custody_nodes_reachable",
            x=0,
            y=y,
            w=6,
            h=6,
            decimals=0,
            thresholds=[
                {"color": "red", "value": None},
                {"color": "orange", "value": 3},
                {"color": "green", "value": 5},
            ],
        )
    )
    panels.append(
        stat_panel(
            _id(c), "Threshold (m)", "zkcex_custody_threshold_m", x=6, y=y, w=6, h=6, decimals=0
        )
    )
    panels.append(
        stat_panel(
            _id(c), "Total (n)", "zkcex_custody_threshold_n", x=12, y=y, w=6, h=6, decimals=0
        )
    )
    panels.append(
        gauge_panel(
            _id(c),
            "Fleet capacity",
            "zkcex_custody_nodes_reachable / zkcex_custody_threshold_n",
            x=18,
            y=y,
            w=6,
            h=6,
            unit="percentunit",
            min_v=0,
            max_v=1,
            thresholds=[
                {"color": "red", "value": None},
                {"color": "orange", "value": 0.6},
                {"color": "green", "value": 1.0},
            ],
        )
    )

    y += 6
    panels.append(
        table_panel(
            _id(c), "Per-node share index", "zkcex_custody_node_share_index", x=0, y=y, w=12, h=8
        )
    )
    panels.append(
        timeseries_panel(
            _id(c),
            "Per-node up/down",
            [
                {"expr": "zkcex_custody_node_up", "legend": "{{ node_id }}"},
            ],
            x=12,
            y=y,
            w=12,
            h=8,
        )
    )

    y += 8
    panels.append(
        timeseries_panel(
            _id(c),
            "Per-node /health latency",
            [
                {
                    "expr": 'zkcex_service_scrape_latency_ms{service="custody"}',
                    "legend": "{{ job }}",
                },
            ],
            x=0,
            y=y,
            w=12,
            h=8,
            unit="ms",
        )
    )
    panels.append(
        timeseries_panel(
            _id(c),
            "Coord-reported node latency",
            [
                {"expr": "zkcex_custody_node_latency_ms", "legend": "{{ node_id }}"},
            ],
            x=12,
            y=y,
            w=12,
            h=8,
            unit="ms",
        )
    )

    y += 8
    panels.append(
        timeseries_panel(
            _id(c),
            "Successful scrapes (cum) per node",
            [
                {"expr": 'zkcex_service_scrape_up_total{service="custody"}', "legend": "{{ job }}"},
            ],
            x=0,
            y=y,
            w=24,
            h=6,
        )
    )

    return base_dashboard("zkcex-custody", "zkCEX — Custody", panels, ["custody"])


def build_services() -> dict[str, Any]:
    c = [0]
    panels: list[dict[str, Any]] = []
    panels.append(row(_id(c), "Service availability", 0))
    y = 1
    panels.append(
        table_panel(_id(c), "Service up status", "zkcex_service_up", x=0, y=y, w=12, h=10)
    )
    panels.append(
        timeseries_panel(
            _id(c),
            "Service uptime (1 = up)",
            [
                {"expr": "zkcex_service_up", "legend": "{{ service }} ({{ job }})"},
            ],
            x=12,
            y=y,
            w=12,
            h=10,
        )
    )
    y += 10
    panels.append(row(_id(c), "Scrape health", y))
    y += 1
    panels.append(
        timeseries_panel(
            _id(c),
            "Per-service scrape latency (ms)",
            [
                {"expr": "zkcex_service_scrape_latency_ms", "legend": "{{ service }} ({{ job }})"},
            ],
            x=0,
            y=y,
            w=12,
            h=8,
            unit="ms",
        )
    )
    panels.append(
        timeseries_panel(
            _id(c),
            "Aggregator scrape duration (ms)",
            [
                {"expr": "zkcex_aggregator_scrape_duration_ms", "legend": "duration"},
            ],
            x=12,
            y=y,
            w=12,
            h=8,
            unit="ms",
        )
    )
    y += 8
    panels.append(
        stat_panel(
            _id(c),
            "Aggregator scrape #",
            "zkcex_aggregator_scrape_count",
            x=0,
            y=y,
            w=6,
            h=4,
            decimals=0,
        )
    )
    panels.append(
        stat_panel(
            _id(c), "Targets up", "zkcex_aggregator_targets_up", x=6, y=y, w=6, h=4, decimals=0
        )
    )
    panels.append(
        stat_panel(
            _id(c),
            "Targets total",
            "zkcex_aggregator_targets_total",
            x=12,
            y=y,
            w=6,
            h=4,
            decimals=0,
        )
    )
    panels.append(
        stat_panel(
            _id(c),
            "Aggregator up",
            "zkcex_aggregator_up",
            x=18,
            y=y,
            w=6,
            h=4,
            decimals=0,
            thresholds=UP_STEPS,
        )
    )
    y += 4
    panels.append(row(_id(c), "Docker", y))
    y += 1
    panels.append(
        timeseries_panel(
            _id(c),
            "Container CPU %",
            [
                {"expr": "zkcex_container_cpu_percent", "legend": "{{ container }}"},
            ],
            x=0,
            y=y,
            w=12,
            h=8,
            unit="percent",
        )
    )
    panels.append(
        timeseries_panel(
            _id(c),
            "Container memory %",
            [
                {"expr": "zkcex_container_mem_percent", "legend": "{{ container }}"},
            ],
            x=12,
            y=y,
            w=12,
            h=8,
            unit="percent",
        )
    )
    y += 8
    panels.append(
        timeseries_panel(
            _id(c),
            "Container memory (bytes)",
            [
                {"expr": "zkcex_container_mem_bytes", "legend": "{{ container }}"},
            ],
            x=0,
            y=y,
            w=12,
            h=8,
            unit="bytes",
        )
    )
    panels.append(
        timeseries_panel(
            _id(c),
            "Container net rx/tx (bytes total)",
            [
                {"expr": "zkcex_container_net_rx_bytes_total", "legend": "{{ container }} rx"},
                {"expr": "zkcex_container_net_tx_bytes_total", "legend": "{{ container }} tx"},
            ],
            x=12,
            y=y,
            w=12,
            h=8,
            unit="bytes",
        )
    )
    y += 8
    panels.append(row(_id(c), "Logs (via Loki)", y))
    y += 1
    panels.append(
        logs_panel(
            _id(c), "Python service logs (/tmp/*.log)", '{job="zkcex-python"}', x=0, y=y, w=24, h=12
        )
    )
    return base_dashboard("zkcex-services", "zkCEX — Services", panels, ["services"])


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    dashboards = [
        ("01-overview.json", build_overview()),
        ("02-trading.json", build_trading()),
        ("03-compliance.json", build_compliance()),
        ("04-pol-porl.json", build_pol_porl()),
        ("05-custody.json", build_custody()),
        ("06-services.json", build_services()),
    ]
    for fname, dash in dashboards:
        path = os.path.join(OUT_DIR, fname)
        with open(path, "w") as f:
            json.dump(dash, f, indent=2, sort_keys=True)
        n_panels = sum(1 for p in dash["panels"] if p.get("type") != "row")
        n_rows = sum(1 for p in dash["panels"] if p.get("type") == "row")
        print(f"wrote {path}  ({n_panels} panels, {n_rows} rows)")


if __name__ == "__main__":
    main()
