#!/usr/bin/env python3
"""Build an HTML load-test report from result JSONs.

Reads every ``results/*.json`` (both ``--summary-export`` output from k6 and
``python-<scenario>.json`` written by ``python_runner.py``) and renders an
HTML report at ``homepage/app/load-report.html`` with:

  * per-scenario throughput + latency tables
  * top-line summary cards
  * bottleneck identification using a one-shot ``docker stats`` snapshot
  * comparison vs the previous run (if ``results/_previous.json`` exists)

The previous run is saved as ``results/_previous.json`` after each successful
build, so subsequent runs auto-diff.
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE / "results"
PREV_FILE = RESULTS_DIR / "_previous.json"
HOMEPAGE_DIR = HERE.parent.parent / "homepage" / "app"
OUT_HTML = HOMEPAGE_DIR / "load-report.html"


# ---------------------------------------------------------------------------
# normalize whichever JSON shape we got into one common record
# ---------------------------------------------------------------------------


def _safe_get(d: dict, *path, default=None):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def parse_summary(name: str, raw: dict) -> dict:
    """Normalize either k6 summary-export or our python_runner.py JSON."""
    if "metrics" in raw and isinstance(raw["metrics"], dict):
        # k6 shape
        m = raw["metrics"]
        req = _safe_get(m, "http_reqs", "count") or _safe_get(
            m, "http_reqs", "values", "count", default=0
        )
        rate = _safe_get(m, "http_reqs", "rate") or _safe_get(
            m, "http_reqs", "values", "rate", default=0.0
        )
        fail = _safe_get(m, "http_req_failed", "value") or _safe_get(
            m, "http_req_failed", "values", "rate", default=0.0
        )
        dur_vals = _safe_get(m, "http_req_duration", "values", default={}) or m.get(
            "http_req_duration", {}
        )
        return {
            "name": name,
            "source": "k6",
            "total_requests": int(req or 0),
            "rps": float(rate or 0),
            "error_rate": float(fail or 0),
            "latency_ms": {
                "avg": float(dur_vals.get("avg", 0)),
                "min": float(dur_vals.get("min", 0)),
                "max": float(dur_vals.get("max", 0)),
                "p50": float(dur_vals.get("med", dur_vals.get("p(50)", 0))),
                "p90": float(dur_vals.get("p(90)", 0)),
                "p95": float(dur_vals.get("p(95)", 0)),
                "p99": float(dur_vals.get("p(99)", 0)),
            },
            "status_codes": {},
            "duration_sec": _safe_get(raw, "state", "testRunDurationMs", default=0) / 1000.0,
            "users": _safe_get(raw, "options", "vus", default=0),
        }
    # python_runner.py shape
    return {
        "name": name,
        "source": "python_runner",
        "total_requests": int(raw.get("total_requests", 0)),
        "rps": float(raw.get("rps", 0)),
        "error_rate": float(raw.get("error_rate", 0)),
        "latency_ms": raw.get("latency_ms", {}),
        "status_codes": raw.get("status_codes", {}),
        "duration_sec": float(raw.get("duration_sec", 0)),
        "users": int(raw.get("users", 0)),
    }


def load_all_runs() -> list[dict]:
    runs = []
    if not RESULTS_DIR.exists():
        return runs
    for p in sorted(RESULTS_DIR.glob("*.json")):
        if p.name.startswith("_"):
            continue
        try:
            raw = json.loads(p.read_text())
        except Exception as e:
            print(f"warn: cannot parse {p}: {e}", file=sys.stderr)
            continue
        runs.append(parse_summary(p.stem, raw))
    return runs


# ---------------------------------------------------------------------------
# docker stats snapshot for bottleneck identification
# ---------------------------------------------------------------------------


def docker_stats() -> list[dict]:
    docker = shutil.which("docker")
    if not docker:
        return []
    try:
        out = subprocess.run(  # noqa: S603 - docker executable is resolved before invocation.
            [
                docker,
                "stats",
                "--no-stream",
                "--format",
                "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.NetIO}}|{{.BlockIO}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return []
    rows = []
    for line in out.stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 5:
            continue
        name, cpu, mem, netio, blkio = parts
        cpu_num = 0.0
        try:
            cpu_num = float(cpu.rstrip("%"))
        except ValueError:
            pass
        rows.append(
            {"name": name, "cpu_pct": cpu_num, "mem": mem, "net_io": netio, "block_io": blkio}
        )
    rows.sort(key=lambda r: r["cpu_pct"], reverse=True)
    return rows


def identify_bottleneck(stats: list[dict]) -> str:
    if not stats:
        return (
            "Docker is not available, so per-container CPU was not "
            'captured.  Run "docker stats --no-stream" while a load test '
            "is in progress to identify the hottest service."
        )
    top = stats[0]
    return (
        f'Hottest container during snapshot: <strong>{html.escape(top["name"])}</strong> '
        f'at {top["cpu_pct"]:.1f}% CPU.  If you saw degradation above this '
        f'point, this is the first place to look.'
    )


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

CSS = """
:root {
  --bg:#0a0d14; --panel:#11151f; --line:#1d2330; --muted:#7a869a;
  --fg:#e6e9f0; --accent:#5b8cff; --ok:#3ecf8e; --warn:#ffb454; --err:#ff5371;
}
* { box-sizing: border-box; }
body { background:var(--bg); color:var(--fg); font-family:Inter,system-ui,sans-serif;
       margin:0; padding:32px; }
h1, h2, h3 { margin-top: 0; }
.muted { color: var(--muted); }
.cards { display: grid; grid-template-columns: repeat(auto-fit,minmax(220px,1fr));
         gap: 16px; margin-bottom: 24px; }
.card { background: var(--panel); border:1px solid var(--line); border-radius: 12px;
        padding: 18px; }
.card .label { font-size: 12px; color: var(--muted); text-transform: uppercase; }
.card .value { font-size: 28px; font-weight: 700; margin-top: 4px; }
.card .delta { font-size: 12px; margin-top: 2px; }
.delta.up   { color: var(--ok); }
.delta.down { color: var(--err); }
table { width: 100%; border-collapse: collapse; background: var(--panel);
        border:1px solid var(--line); border-radius: 12px; overflow: hidden; }
th, td { padding: 10px 14px; text-align: right; font-variant-numeric: tabular-nums;
         border-bottom: 1px solid var(--line); }
th:first-child, td:first-child { text-align: left; }
th { background: #0e131e; font-weight: 600; font-size: 12px; text-transform: uppercase;
     color: var(--muted); }
tr:last-child td { border-bottom: none; }
.bad  { color: var(--err); font-weight: 600; }
.warn { color: var(--warn); }
.ok   { color: var(--ok); }
.section { margin: 32px 0; }
.section h2 { border-left: 3px solid var(--accent); padding-left: 10px; }
small.note { color: var(--muted); font-size: 11px; }
.meta { color: var(--muted); font-size: 12px; }
.pill { display: inline-block; padding: 2px 8px; border-radius: 6px;
        font-size: 11px; background: #1c2435; color: var(--muted); margin-left: 6px; }
"""


def fmt_ms(v: float) -> str:
    return f"{v:.1f} ms" if v else "—"


def color_for_p95(v: float) -> str:
    if v == 0:
        return ""
    if v >= 2000:
        return "bad"
    if v >= 500:
        return "warn"
    return "ok"


def color_for_err(v: float) -> str:
    if v >= 0.05:
        return "bad"
    if v >= 0.01:
        return "warn"
    return "ok"


def delta(curr: float, prev: float | None, lower_better: bool = True) -> str:
    if prev is None or prev == 0:
        return ""
    diff = curr - prev
    pct = (diff / prev) * 100 if prev else 0.0
    arrow_up = "&#9650;"
    arrow_down = "&#9660;"
    # for lower_better metrics (latency / error rate), down = good
    if (diff < 0 and lower_better) or (diff > 0 and not lower_better):
        return f'<span class="delta up">{arrow_down} {abs(pct):.1f}%</span>'
    if diff != 0:
        return f'<span class="delta down">{arrow_up} {abs(pct):.1f}%</span>'
    return ""


def render(runs: list[dict], prev_runs: dict[str, dict], docker_rows: list[dict]) -> str:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    total = sum(r["total_requests"] for r in runs)
    rps = sum(r["rps"] for r in runs)
    if runs:
        avg_err = sum(r["error_rate"] for r in runs) / len(runs)
        worst_p95 = max((r["latency_ms"].get("p95", 0) for r in runs), default=0)
    else:
        avg_err = 0.0
        worst_p95 = 0.0

    # rows
    body_rows = []
    for r in sorted(runs, key=lambda x: x["name"]):
        lat = r["latency_ms"]
        p95 = lat.get("p95", 0)
        err = r["error_rate"]
        prev = prev_runs.get(r["name"])
        body_rows.append(f"""
        <tr>
          <td>{html.escape(r['name'])}
              <span class="pill">{html.escape(r['source'])}</span></td>
          <td>{r['total_requests']:,}</td>
          <td>{r['rps']:.1f}</td>
          <td>{fmt_ms(lat.get('avg', 0))}</td>
          <td>{fmt_ms(lat.get('p50', 0))}</td>
          <td>{fmt_ms(lat.get('p90', 0))}</td>
          <td class="{color_for_p95(p95)}">{fmt_ms(p95)}
              {delta(p95, (prev or {}).get('latency_ms', {}).get('p95'))}</td>
          <td>{fmt_ms(lat.get('p99', 0))}</td>
          <td class="{color_for_err(err)}">{err*100:.2f}%
              {delta(err, (prev or {}).get('error_rate'))}</td>
        </tr>
        """)

    docker_rows_html = ""
    if docker_rows:
        docker_rows_html = "".join(
            f'<tr><td>{html.escape(d["name"])}</td>'
            f'<td>{d["cpu_pct"]:.1f}%</td>'
            f'<td>{html.escape(d["mem"])}</td>'
            f'<td>{html.escape(d["net_io"])}</td>'
            f'<td>{html.escape(d["block_io"])}</td></tr>'
            for d in docker_rows[:10]
        )
    else:
        docker_rows_html = (
            '<tr><td colspan="5" class="muted">'
            "docker stats unavailable (docker not on PATH)."
            "</td></tr>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Load Test Report — zkCEX</title>
<style>{CSS}</style>
</head>
<body>
<h1>Load Test Report</h1>
<p class="meta">Generated {html.escape(now)} from {len(runs)} scenario file(s).</p>

<div class="cards">
  <div class="card"><div class="label">Total Requests</div>
    <div class="value">{total:,}</div></div>
  <div class="card"><div class="label">Combined RPS</div>
    <div class="value">{rps:.0f}</div></div>
  <div class="card"><div class="label">Avg Error Rate</div>
    <div class="value {color_for_err(avg_err)}">{avg_err*100:.2f}%</div></div>
  <div class="card"><div class="label">Worst p95</div>
    <div class="value {color_for_p95(worst_p95)}">{fmt_ms(worst_p95)}</div></div>
</div>

<div class="section">
  <h2>Per-Scenario Results</h2>
  <table>
    <thead>
      <tr>
        <th>Scenario</th><th>Reqs</th><th>RPS</th>
        <th>avg</th><th>p50</th><th>p90</th><th>p95</th><th>p99</th>
        <th>err</th>
      </tr>
    </thead>
    <tbody>{''.join(body_rows) or '<tr><td colspan="9" class="muted">No results found.</td></tr>'}</tbody>
  </table>
  <p><small class="note">p95 colours: green &lt;500 ms, orange &lt;2 s, red ≥2 s.&nbsp;
     Error colours: green &lt;1%, orange &lt;5%, red ≥5%.&nbsp;
     Δ vs previous run is shown next to p95 and err.</small></p>
</div>

<div class="section">
  <h2>Bottleneck Snapshot</h2>
  <p>{identify_bottleneck(docker_rows)}</p>
  <table>
    <thead><tr><th>Container</th><th>CPU</th><th>Memory</th><th>Net I/O</th><th>Disk I/O</th></tr></thead>
    <tbody>{docker_rows_html}</tbody>
  </table>
</div>

<div class="section">
  <h2>How to read this</h2>
  <ul>
    <li><strong>p95</strong> matters most — it's the latency your slowest 5% of users see.</li>
    <li><strong>Error rate</strong> &gt; 1% on read endpoints almost always means a downstream
        service is saturated or down.</li>
    <li><strong>Bottleneck</strong>: the container with the highest CPU during the run is
        the first place to optimize; everything else is usually downstream of it.</li>
    <li>Re-run with <code>build_report.py</code> after a change to see the Δ.</li>
  </ul>
</div>

</body>
</html>
"""


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="Build HTML load report.")
    ap.add_argument("--out", default=str(OUT_HTML), help=f"Output path (default: {OUT_HTML}).")
    args = ap.parse_args()

    runs = load_all_runs()
    print(f"loaded {len(runs)} runs from {RESULTS_DIR}")

    prev_runs: dict[str, dict] = {}
    if PREV_FILE.exists():
        try:
            prev_list = json.loads(PREV_FILE.read_text())
            prev_runs = {r["name"]: r for r in prev_list}
        except Exception:
            prev_runs = {}

    docker_rows = docker_stats()
    html_out = render(runs, prev_runs, docker_rows)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_out)
    print(f"wrote {out_path}  ({len(html_out):,} bytes)")

    # save current as previous for next diff
    try:
        PREV_FILE.write_text(json.dumps(runs, indent=2))
    except OSError:
        pass

    # print a tiny inline summary so this is useful in CI logs
    if runs:
        print("\nsummary:")
        for r in runs:
            p95 = r["latency_ms"].get("p95", 0)
            print(
                f"  {r['name']:30s}  rps={r['rps']:7.1f}  "
                f"p95={p95:7.1f}ms  err={r['error_rate']*100:5.2f}%"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
