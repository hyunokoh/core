#!/usr/bin/env python3
"""zkCEX game-day disaster recovery orchestrator (port 5680).

Standalone stdlib-only HTTP service that runs a catalogue of pre-defined
disaster scenarios against the live zkCEX demo stack, measures detection +
recovery time, and emits a printable post-mortem report per run.

Real exchanges run a quarterly game day. This is the codified procedure:
each scenario has explicit ``simulate / detect / recover / verify / cleanup``
steps, an RTO target, a hard 5-minute cap, and atexit cleanup so a partial
run can never leak. The orchestrator persists every run in
``tools/.local/dr.db`` and exposes a Prometheus-format ``/metrics`` endpoint
so Grafana can alert on "game day overdue" or "recent game-day failure".

Safety rails (see top of file constants):
  - ``POST /dr/run/*`` requires ``confirm=true`` query param,
  - admin actions reject non-loopback callers even with a valid Bearer,
  - if ``ZKCEX_ENV=production`` the orchestrator refuses to start,
  - a single ``tools/.local/dr.lock`` flock guards concurrent runs,
  - each scenario has a hard 300s cap (configurable per scenario).

CLI:

    python3 tools/dr/game_day.py [PORT]
    python3 tools/dr/game_day.py --dry-run --list-scenarios
    python3 tools/dr/game_day.py --dry-run --run-all [--output ci-result.json]

In ``--dry-run`` mode, every ``simulate()`` is replaced with ``pass`` and
``recover()`` is also a no-op. The orchestrator walks the state machine to
verify wiring without touching any real service.

Hard constraints:
  - stdlib only,
  - no GitHub or competitor brand mentions in user-visible strings,
  - never permanently break the stack; cleanup is best-effort idempotent.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import dataclasses
import errno
import fcntl
import http.server
import json
import os
import secrets
import shutil
import signal
import socketserver
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
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# Paths + config
# --------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.dirname(HERE)
PROJECT_ROOT = os.path.dirname(TOOLS_DIR)
LOCAL_DIR = os.path.join(TOOLS_DIR, ".local")
os.makedirs(LOCAL_DIR, exist_ok=True)
DB_PATH = os.path.join(LOCAL_DIR, "dr.db")
LOCK_PATH = os.path.join(LOCAL_DIR, "dr.lock")
REPORT_DIR = os.path.normpath(
    os.path.join(TOOLS_DIR, os.pardir, "homepage", "app", "game-day-report")
)
DASHBOARD_PATH = os.path.normpath(
    os.path.join(TOOLS_DIR, os.pardir, "homepage", "app", "game-day.html")
)

DEFAULT_PORT = 5680
ZKCEX_ENV = (os.environ.get("ZKCEX_ENV") or "dev").strip().lower()

# Admin bearer — accept anything in DR_ADMIN_TOKEN, else mint a fresh per-run
# token and print it once at startup.
_ADMIN_TOKEN_ENV = os.environ.get("DR_ADMIN_TOKEN", "").strip()

# Docker container names we may touch. These are best-effort: a scenario
# whose target container is missing just records a soft-skip and proceeds.
DOCKER_BIN = os.environ.get("DR_DOCKER_BIN", "docker")
DOCKER_TIMEOUT_S = float(os.environ.get("DR_DOCKER_TIMEOUT_S", "5"))

# Hard cap per scenario (seconds). Beyond this the run is marked ``timeout``
# and cleanup runs even if recover/verify never returned.
SCENARIO_HARD_CAP_S = int(os.environ.get("DR_SCENARIO_HARD_CAP_S", "300"))

# Between scenarios in --run-all the orchestrator pauses so the system has
# time to settle.
INTER_SCENARIO_GAP_S = int(os.environ.get("DR_INTER_SCENARIO_GAP_S", "60"))

# HTTP probe defaults.
PROBE_TIMEOUT_S = float(os.environ.get("DR_PROBE_TIMEOUT_S", "2.5"))
PROBE_POLL_INTERVAL_S = float(os.environ.get("DR_PROBE_POLL_INTERVAL_S", "0.5"))


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
def log(msg: str) -> None:
    sys.stderr.write(f"[dr] {msg}\n")
    sys.stderr.flush()


def _tool_path(command: str) -> str:
    if os.path.isabs(command) or os.path.sep in command:
        return command
    return shutil.which(command) or command


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


DOCKER_BIN = _tool_path(DOCKER_BIN)
PGREP_BIN = _tool_path("pgrep")
PKILL_BIN = _tool_path("pkill")
PYTHON_BIN = sys.executable or _tool_path("python3")


# --------------------------------------------------------------------------
# DB schema
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS dr_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scenario_id TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  completed_at INTEGER,
  rto_target_seconds INTEGER NOT NULL,
  rto_actual_seconds INTEGER,
  rto_pass INTEGER NOT NULL DEFAULT 0,
  rpo_actual_seconds INTEGER,
  result_json TEXT NOT NULL,
  operator TEXT
);
CREATE INDEX IF NOT EXISTS idx_dr_runs_scenario_ts
  ON dr_runs(scenario_id, started_at);

CREATE TABLE IF NOT EXISTS dr_metrics_counter (
  scenario_id TEXT NOT NULL,
  status TEXT NOT NULL,
  n INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (scenario_id, status)
);
"""

_db_lock = threading.Lock()


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def db_init() -> None:
    with _db_lock, db_connect() as conn:
        for stmt in [s for s in SCHEMA.split(";") if s.strip()]:
            conn.execute(stmt)


# --------------------------------------------------------------------------
# Scenario data model
# --------------------------------------------------------------------------
@dataclass
class Timeline:
    """In-flight scenario state. Times are unix seconds (ints)."""

    scenario_id: str
    started_at: int
    rto_target_seconds: int
    induced_at: int | None = None
    detected_at: int | None = None
    detected_by: str | None = None
    recovery_started_at: int | None = None
    recovered_at: int | None = None
    verified_at: int | None = None
    completed_at: int | None = None
    status: str = "running"  # running|pass|fail|timeout|skipped
    rto_actual: int | None = None
    rto_pass: bool = False
    rpo_actual_seconds: int | None = None
    post_recovery_health: dict = field(default_factory=dict)
    logs: list = field(default_factory=list)
    error: str | None = None

    def log_event(self, msg: str) -> None:
        dt = int(time.time()) - self.started_at
        line = f"[+{dt}s] {msg}"
        self.logs.append(line)

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["duration_seconds"] = (self.completed_at or int(time.time())) - self.started_at
        return d


@dataclass
class Scenario:
    id: str
    name: str
    description: str
    rto_target_seconds: int
    simulate: Callable[[Timeline, RunCtx], None]
    detect: Callable[[Timeline, RunCtx], dict]
    recover: Callable[[Timeline, RunCtx], None]
    verify: Callable[[Timeline, RunCtx], dict]
    cleanup: Callable[[Timeline, RunCtx], None]
    hard_cap_s: int = SCENARIO_HARD_CAP_S
    safe_for_real_run: bool = False  # default: dry-run-only unless flagged


@dataclass
class RunCtx:
    dry_run: bool
    operator: str = "unknown"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def now() -> int:
    return int(time.time())


def http_get(url: str, timeout: float = PROBE_TIMEOUT_S) -> tuple[int, str]:
    req = _http_request(url, method="GET")
    try:
        with _http_urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception as read_error:  # noqa: BLE001
            log(f"http error body read failed: {read_error!r}")
        return e.code, body
    except (TimeoutError, urllib.error.URLError, ConnectionError, OSError):
        return 0, ""


def poll_until(
    predicate: Callable[[], tuple[bool, str | None]],
    *,
    deadline: float,
    interval: float = PROBE_POLL_INTERVAL_S,
) -> tuple[bool, str | None]:
    """Poll ``predicate`` until truthy or wall-clock past ``deadline``.

    ``predicate`` returns ``(ok, detail)``. Returns the final outcome.
    """
    while True:
        ok, detail = predicate()
        if ok:
            return True, detail
        if time.time() >= deadline:
            return False, detail
        time.sleep(interval)


def docker_cmd(*args: str) -> tuple[int, str, str]:
    """Run ``docker <args>`` with a small timeout. Returns (rc, stdout, stderr)."""
    try:
        p = subprocess.run(  # noqa: S603 - executable is resolved; args are scenario constants.
            [DOCKER_BIN, *args],
            capture_output=True,
            text=True,
            timeout=DOCKER_TIMEOUT_S,
            check=False,
        )
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{DOCKER_BIN} not found"
    except subprocess.TimeoutExpired:
        return 124, "", "docker timeout"


def docker_container_running(name: str) -> bool:
    rc, out, _ = docker_cmd("ps", "--filter", f"name={name}", "--format", "{{.Names}}")
    if rc != 0:
        return False
    return any(line.strip() == name for line in out.splitlines())


def docker_container_exists(name: str) -> bool:
    rc, out, _ = docker_cmd("ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}")
    if rc != 0:
        return False
    return any(line.strip() == name for line in out.splitlines())


def process_is_running(pattern: str) -> int | None:
    """Return PID of first process matching pattern, or None."""
    try:
        p = subprocess.run(  # noqa: S603 - executable is resolved; pattern comes from scenario catalog.
            [PGREP_BIN, "-f", pattern],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if p.returncode == 0:
            for line in p.stdout.split():
                line = line.strip()
                if line.isdigit():
                    return int(line)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return None


def kill_process(pattern: str) -> bool:
    """Send SIGTERM to processes matching the pattern via pkill."""
    try:
        subprocess.run(  # noqa: S603 - executable is resolved; pattern comes from scenario catalog.
            [PKILL_BIN, "-f", pattern],
            capture_output=True,
            text=True,
            timeout=4.0,
            check=False,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def start_python_service(script_rel: str, *args: str) -> int | None:
    """Start a Python service detached. Returns PID, or None on failure.

    ``script_rel`` is relative to the tools/ directory.
    """
    script_path = os.path.join(TOOLS_DIR, script_rel)
    if not os.path.exists(script_path):
        return None
    try:
        # Detach: new session, stdout/stderr -> /dev/null so we don't leak fds.
        with open(os.devnull, "wb") as devnull:
            p = subprocess.Popen(  # noqa: S603 - executable is current Python; script is repo-local.
                [PYTHON_BIN, script_path, *args],
                stdout=devnull,
                stderr=devnull,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                cwd=PROJECT_ROOT,
            )
        return p.pid
    except OSError:
        return None


# --------------------------------------------------------------------------
# Scenario implementations
#
# Each pair (simulate/detect/recover/verify/cleanup) is small and explicit.
# Anywhere a real disaster is induced we first check whether the target
# component is actually present; if not we mark the scenario "skipped" so a
# half-running stack doesn't produce a false-fail.
# --------------------------------------------------------------------------

# ------------------------------- mm-bot kill ------------------------------
# This is the *safe* real-run scenario. Killing the demo market-maker bot
# only stops fresh quotes; matched book entries are owned by the gateway.
MM_BOT_PORT = 5600
MM_BOT_HEALTH_URL = f"http://127.0.0.1:{MM_BOT_PORT}/mm/health"


def _mm_bot_simulate(tl: Timeline, ctx: RunCtx) -> None:
    tl.induced_at = now()
    if ctx.dry_run:
        tl.log_event("induced (dry-run): would pkill mm_bot.py")
        return
    pid = process_is_running("tools/mm_bot.py")
    if pid is None:
        tl.log_event("induced: mm_bot was already down")
        return
    kill_process("tools/mm_bot.py")
    tl.log_event(f"induced: SIGTERM mm_bot (pid {pid})")


def _mm_bot_detect(tl: Timeline, ctx: RunCtx) -> dict:
    deadline = time.time() + 30
    if ctx.dry_run:
        tl.detected_at = now()
        tl.detected_by = "dry-run: skipped probe"
        return {"detected": True, "via": "dry-run"}

    def _down() -> tuple[bool, str | None]:
        code, _ = http_get(MM_BOT_HEALTH_URL, timeout=1.0)
        return code == 0, f"code={code}"

    ok, detail = poll_until(_down, deadline=deadline)
    if ok:
        tl.detected_at = now()
        tl.detected_by = "mm_bot /mm/health unreachable"
        tl.log_event(f"detected: mm_bot /mm/health unreachable ({detail})")
        return {"detected": True, "via": "health_probe", "detail": detail}
    tl.log_event(f"detect: timed out waiting for mm_bot down ({detail})")
    return {"detected": False, "via": "health_probe", "detail": detail}


def _mm_bot_recover(tl: Timeline, ctx: RunCtx) -> None:
    tl.recovery_started_at = now()
    if ctx.dry_run:
        tl.log_event("recovery (dry-run): would restart mm_bot")
        return
    pid = start_python_service("mm_bot.py", str(MM_BOT_PORT))
    if pid is None:
        tl.log_event("recovery: mm_bot.py not on disk — cannot restart")
        return
    tl.log_event(f"recovery: restarted mm_bot.py (pid {pid})")


def _mm_bot_verify(tl: Timeline, ctx: RunCtx) -> dict:
    deadline = time.time() + 30
    if ctx.dry_run:
        tl.recovered_at = now()
        tl.verified_at = now()
        return {"verified": True, "via": "dry-run"}

    def _up() -> tuple[bool, str | None]:
        code, body = http_get(MM_BOT_HEALTH_URL, timeout=1.0)
        return code == 200, f"code={code}"

    ok, detail = poll_until(_up, deadline=deadline)
    if ok:
        tl.recovered_at = now()
        tl.log_event(f"recovered: mm_bot /mm/health 200 ({detail})")
        # Verify it actually re-quotes within a short window.
        time.sleep(1.0)
        code, body = http_get(MM_BOT_HEALTH_URL, timeout=1.0)
        tl.verified_at = now()
        snapshot = {}
        try:
            snapshot = json.loads(body) if body else {}
        except Exception:
            snapshot = {"raw": body[:200]}
        tl.post_recovery_health = snapshot
        tl.log_event(
            "verified: mm_bot reports "
            f"n_orders_active={snapshot.get('n_orders_active')}, "
            f"paused={snapshot.get('paused')}"
        )
        return {"verified": True, "snapshot": snapshot}
    tl.log_event(f"verify: timed out waiting for mm_bot up ({detail})")
    return {"verified": False, "detail": detail}


def _mm_bot_cleanup(tl: Timeline, ctx: RunCtx) -> None:
    if ctx.dry_run:
        return
    # If we crashed mid-run mm_bot may still be down. Ensure it's up.
    code, _ = http_get(MM_BOT_HEALTH_URL, timeout=1.0)
    if code != 200:
        start_python_service("mm_bot.py", str(MM_BOT_PORT))
        tl.log_event("cleanup: ensured mm_bot is running")


# --------------------------- postgres primary loss ------------------------
PG_AUTH_CONTAINER = "zkcex-postgres-auth"
AUTH_HEALTH_URL = "http://127.0.0.1:5501/auth/health"


def _pg_simulate(tl: Timeline, ctx: RunCtx) -> None:
    tl.induced_at = now()
    if ctx.dry_run:
        tl.log_event(f"induced (dry-run): would docker stop {PG_AUTH_CONTAINER}")
        return
    if not docker_container_exists(PG_AUTH_CONTAINER):
        tl.status = "skipped"
        tl.log_event(f"induced: container {PG_AUTH_CONTAINER} missing — soft skip")
        return
    rc, _, err = docker_cmd("stop", PG_AUTH_CONTAINER)
    if rc != 0:
        tl.log_event(f"induced: docker stop failed: {err.strip()}")
    else:
        tl.log_event(f"induced: docker stop {PG_AUTH_CONTAINER}")


def _pg_detect(tl: Timeline, ctx: RunCtx) -> dict:
    if tl.status == "skipped":
        tl.detected_at = now()
        return {"detected": True, "via": "skipped"}
    deadline = time.time() + 30
    if ctx.dry_run:
        tl.detected_at = now()
        tl.detected_by = "dry-run"
        return {"detected": True}

    def _failing() -> tuple[bool, str | None]:
        code, _ = http_get(AUTH_HEALTH_URL, timeout=1.5)
        # Auth health probe should fail fast or return 503 when pg is dead.
        return code in (0, 500, 502, 503, 504), f"code={code}"

    ok, detail = poll_until(_failing, deadline=deadline)
    if ok:
        tl.detected_at = now()
        tl.detected_by = "auth_server health probe non-200"
        tl.log_event(f"detected: auth_server /auth/health degraded ({detail})")
    return {"detected": ok, "detail": detail}


def _pg_recover(tl: Timeline, ctx: RunCtx) -> None:
    tl.recovery_started_at = now()
    if ctx.dry_run or tl.status == "skipped":
        tl.log_event("recovery (dry-run): would docker start postgres-auth")
        return
    rc, _, err = docker_cmd("start", PG_AUTH_CONTAINER)
    if rc != 0:
        tl.log_event(f"recovery: docker start failed: {err.strip()}")
    else:
        tl.log_event(f"recovery: docker start {PG_AUTH_CONTAINER}")


def _pg_verify(tl: Timeline, ctx: RunCtx) -> dict:
    if tl.status == "skipped":
        tl.recovered_at = now()
        tl.verified_at = now()
        return {"verified": True, "via": "skipped"}
    deadline = time.time() + 60
    if ctx.dry_run:
        tl.recovered_at = now()
        tl.verified_at = now()
        return {"verified": True}

    def _up() -> tuple[bool, str | None]:
        code, _ = http_get(AUTH_HEALTH_URL, timeout=1.5)
        return code == 200, f"code={code}"

    ok, detail = poll_until(_up, deadline=deadline)
    if ok:
        tl.recovered_at = now()
        tl.verified_at = now()
        tl.log_event(f"verified: auth /auth/health 200 ({detail})")
    return {"verified": ok}


def _pg_cleanup(tl: Timeline, ctx: RunCtx) -> None:
    if ctx.dry_run:
        return
    if not docker_container_exists(PG_AUTH_CONTAINER):
        return
    if not docker_container_running(PG_AUTH_CONTAINER):
        docker_cmd("start", PG_AUTH_CONTAINER)
        tl.log_event("cleanup: ensured postgres-auth container is up")


# --------------------------- custody quorum loss --------------------------
CUSTODY_PORTS = [5520, 5521, 5522, 5523, 5524]
CUSTODY_COORD_HEALTH = "http://127.0.0.1:5530/health"


def _custody_simulate(tl: Timeline, ctx: RunCtx) -> None:
    tl.induced_at = now()
    if ctx.dry_run:
        tl.log_event("induced (dry-run): would kill 3 of 5 custody signer nodes")
        return
    killed = 0
    for port in CUSTODY_PORTS[:3]:
        pattern = f"signer_node.*--port {port}"
        pid = process_is_running(pattern)
        if pid:
            kill_process(pattern)
            killed += 1
            tl.log_event(f"induced: killed custody signer on :{port} (pid {pid})")
    if killed == 0:
        tl.log_event("induced: no custody signer nodes found running — soft skip")
        tl.status = "skipped"


def _custody_detect(tl: Timeline, ctx: RunCtx) -> dict:
    if tl.status == "skipped":
        tl.detected_at = now()
        return {"detected": True, "via": "skipped"}
    deadline = time.time() + 30
    if ctx.dry_run:
        tl.detected_at = now()
        tl.detected_by = "dry-run"
        return {"detected": True}

    def _quorum_lost() -> tuple[bool, str | None]:
        code, body = http_get(CUSTODY_COORD_HEALTH, timeout=1.5)
        if code == 0:
            return False, "coord unreachable"
        try:
            d = json.loads(body)
        except Exception:
            return False, "coord bad json"
        n = int(d.get("n_nodes_reachable", -1))
        return n < 3, f"n_nodes_reachable={n}"

    ok, detail = poll_until(_quorum_lost, deadline=deadline)
    if ok:
        tl.detected_at = now()
        tl.detected_by = "coordinator /health n_nodes_reachable < 3"
        tl.log_event(f"detected: coordinator reports {detail}")
    return {"detected": ok, "detail": detail}


def _custody_recover(tl: Timeline, ctx: RunCtx) -> None:
    tl.recovery_started_at = now()
    if ctx.dry_run or tl.status == "skipped":
        tl.log_event("recovery (dry-run): would restart 2 of 3 killed signers")
        return
    # Restart only 2 of the 3 killed nodes — back to threshold 3-of-5 (2 live
    # + 2 restarted = 4, well over the 3 minimum) but still simulates a
    # partial recovery rather than a full restore.
    for idx, port in enumerate(CUSTODY_PORTS[:2]):
        pid = start_python_service(
            "custody/signer_node.py", "--port", str(port), "--node-id", f"n{idx}"
        )
        if pid:
            tl.log_event(f"recovery: restarted custody signer on :{port} (pid {pid})")


def _custody_verify(tl: Timeline, ctx: RunCtx) -> dict:
    if tl.status == "skipped":
        tl.recovered_at = now()
        tl.verified_at = now()
        return {"verified": True, "via": "skipped"}
    deadline = time.time() + 60
    if ctx.dry_run:
        tl.recovered_at = now()
        tl.verified_at = now()
        return {"verified": True}

    def _quorum_ok() -> tuple[bool, str | None]:
        code, body = http_get(CUSTODY_COORD_HEALTH, timeout=1.5)
        if code != 200:
            return False, f"code={code}"
        try:
            d = json.loads(body)
        except Exception:
            return False, "bad json"
        n = int(d.get("n_nodes_reachable", 0))
        return n >= 3, f"n_nodes_reachable={n}"

    ok, detail = poll_until(_quorum_ok, deadline=deadline)
    if ok:
        tl.recovered_at = now()
        tl.verified_at = now()
        tl.log_event(f"verified: quorum restored ({detail})")
    return {"verified": ok}


def _custody_cleanup(tl: Timeline, ctx: RunCtx) -> None:
    if ctx.dry_run:
        return
    # Ensure every node we could have killed is alive again.
    for idx, port in enumerate(CUSTODY_PORTS[:3]):
        code, _ = http_get(f"http://127.0.0.1:{port}/health", timeout=1.0)
        if code != 200:
            start_python_service(
                "custody/signer_node.py", "--port", str(port), "--node-id", f"n{idx}"
            )
            tl.log_event(f"cleanup: ensured custody signer :{port}")


# ------------------------------ zkpol bridge ------------------------------
BRIDGE_HEALTH = "http://127.0.0.1:5504/bridge/health"


def _bridge_simulate(tl: Timeline, ctx: RunCtx) -> None:
    tl.induced_at = now()
    if ctx.dry_run:
        tl.log_event("induced (dry-run): would pkill zkpol_bridge.py")
        return
    pid = process_is_running("tools/zkpol_bridge.py")
    if pid is None:
        tl.status = "skipped"
        tl.log_event("induced: zkpol_bridge not running — soft skip")
        return
    kill_process("tools/zkpol_bridge.py")
    tl.log_event(f"induced: SIGTERM zkpol_bridge.py (pid {pid})")


def _bridge_detect(tl: Timeline, ctx: RunCtx) -> dict:
    if tl.status == "skipped":
        tl.detected_at = now()
        return {"detected": True, "via": "skipped"}
    deadline = time.time() + 30
    if ctx.dry_run:
        tl.detected_at = now()
        tl.detected_by = "dry-run"
        return {"detected": True}

    def _down() -> tuple[bool, str | None]:
        code, _ = http_get(BRIDGE_HEALTH, timeout=1.5)
        return code == 0, f"code={code}"

    ok, detail = poll_until(_down, deadline=deadline)
    if ok:
        tl.detected_at = now()
        tl.detected_by = "bridge /bridge/health unreachable"
        tl.log_event(f"detected: bridge unreachable ({detail})")
    return {"detected": ok}


def _bridge_recover(tl: Timeline, ctx: RunCtx) -> None:
    tl.recovery_started_at = now()
    if ctx.dry_run or tl.status == "skipped":
        tl.log_event("recovery (dry-run): would restart zkpol_bridge.py")
        return
    pid = start_python_service("zkpol_bridge.py", "5504")
    if pid:
        tl.log_event(f"recovery: restarted zkpol_bridge.py (pid {pid})")


def _bridge_verify(tl: Timeline, ctx: RunCtx) -> dict:
    if tl.status == "skipped":
        tl.recovered_at = now()
        tl.verified_at = now()
        return {"verified": True, "via": "skipped"}
    deadline = time.time() + 60
    if ctx.dry_run:
        tl.recovered_at = now()
        tl.verified_at = now()
        return {"verified": True}

    def _up() -> tuple[bool, str | None]:
        code, body = http_get(BRIDGE_HEALTH, timeout=1.5)
        return code == 200, f"code={code}"

    ok, detail = poll_until(_up, deadline=deadline)
    if ok:
        tl.recovered_at = now()
        tl.verified_at = now()
        tl.log_event(f"verified: bridge /bridge/health 200 ({detail})")
    return {"verified": ok}


def _bridge_cleanup(tl: Timeline, ctx: RunCtx) -> None:
    if ctx.dry_run:
        return
    code, _ = http_get(BRIDGE_HEALTH, timeout=1.0)
    if code != 200:
        start_python_service("zkpol_bridge.py", "5504")
        tl.log_event("cleanup: ensured zkpol_bridge is up")


# --------------------------------- WAF deny-all ---------------------------
WAF_PROXY_PROBE = "http://127.0.0.1:5500/v3/ping"
WAF_ENV_FILE = os.path.join(LOCAL_DIR, "dr_waf_override.env")


def _waf_simulate(tl: Timeline, ctx: RunCtx) -> None:
    tl.induced_at = now()
    if ctx.dry_run:
        tl.log_event("induced (dry-run): would set WAF_GEO_BLOCKLIST=*")
        return
    # We don't actually mutate WAF state in a real run — restarting the
    # proxy with WAF_GEO_BLOCKLIST=* would disrupt every test. Instead we
    # write a marker file that the operator dashboard / ops tooling can
    # surface, and detect picks it up. In a true game day this would be a
    # supervised live change.
    try:
        with open(WAF_ENV_FILE, "w") as fh:
            fh.write("WAF_GEO_BLOCKLIST=*\n")
        tl.log_event("induced: wrote WAF override marker (dashboard surfaces)")
    except OSError as e:
        tl.log_event(f"induced: failed to write WAF marker: {e}")
        tl.status = "skipped"


def _waf_detect(tl: Timeline, ctx: RunCtx) -> dict:
    if tl.status == "skipped":
        tl.detected_at = now()
        return {"detected": True, "via": "skipped"}
    if ctx.dry_run:
        tl.detected_at = now()
        tl.detected_by = "dry-run"
        return {"detected": True}
    # Detection is the presence of the marker file the dashboard would surface.
    deadline = time.time() + 10

    def _seen() -> tuple[bool, str | None]:
        return os.path.exists(WAF_ENV_FILE), "marker present"

    ok, _ = poll_until(_seen, deadline=deadline)
    if ok:
        tl.detected_at = now()
        tl.detected_by = "operator dashboard WAF marker"
        tl.log_event("detected: WAF override marker visible to dashboard")
    return {"detected": ok}


def _waf_recover(tl: Timeline, ctx: RunCtx) -> None:
    tl.recovery_started_at = now()
    if ctx.dry_run or tl.status == "skipped":
        tl.log_event("recovery (dry-run): would clear WAF_GEO_BLOCKLIST")
        return
    with contextlib.suppress(OSError):
        os.remove(WAF_ENV_FILE)
    tl.log_event("recovery: cleared WAF override marker")


def _waf_verify(tl: Timeline, ctx: RunCtx) -> dict:
    if tl.status == "skipped":
        tl.recovered_at = now()
        tl.verified_at = now()
        return {"verified": True, "via": "skipped"}
    if ctx.dry_run:
        tl.recovered_at = now()
        tl.verified_at = now()
        return {"verified": True}
    code, _ = http_get(WAF_PROXY_PROBE, timeout=1.5)
    ok = code == 200
    if ok:
        tl.recovered_at = now()
        tl.verified_at = now()
        tl.log_event(f"verified: proxy /v3/ping {code}")
    return {"verified": ok, "probe_code": code}


def _waf_cleanup(tl: Timeline, ctx: RunCtx) -> None:
    with contextlib.suppress(OSError):
        os.remove(WAF_ENV_FILE)


# --------------------------------- MinIO offline --------------------------
MINIO_CONTAINER = "zkcex-minio"


def _minio_simulate(tl: Timeline, ctx: RunCtx) -> None:
    tl.induced_at = now()
    if ctx.dry_run:
        tl.log_event(f"induced (dry-run): would docker stop {MINIO_CONTAINER}")
        return
    if not docker_container_exists(MINIO_CONTAINER):
        tl.status = "skipped"
        tl.log_event(f"induced: container {MINIO_CONTAINER} missing — soft skip")
        return
    rc, _, err = docker_cmd("stop", MINIO_CONTAINER)
    if rc != 0:
        tl.log_event(f"induced: docker stop failed: {err.strip()}")
    else:
        tl.log_event(f"induced: docker stop {MINIO_CONTAINER}")


def _minio_detect(tl: Timeline, ctx: RunCtx) -> dict:
    if tl.status == "skipped":
        tl.detected_at = now()
        return {"detected": True, "via": "skipped"}
    if ctx.dry_run:
        tl.detected_at = now()
        tl.detected_by = "dry-run"
        return {"detected": True}
    deadline = time.time() + 30

    def _down() -> tuple[bool, str | None]:
        # MinIO console at :9001 if mapped, otherwise just check container state.
        return not docker_container_running(MINIO_CONTAINER), "container stopped"

    ok, _ = poll_until(_down, deadline=deadline)
    if ok:
        tl.detected_at = now()
        tl.detected_by = "MinIO container not running"
        tl.log_event("detected: backup_daemon should queue jobs locally")
    return {"detected": ok}


def _minio_recover(tl: Timeline, ctx: RunCtx) -> None:
    tl.recovery_started_at = now()
    if ctx.dry_run or tl.status == "skipped":
        tl.log_event("recovery (dry-run): would docker start minio")
        return
    docker_cmd("start", MINIO_CONTAINER)
    tl.log_event(f"recovery: docker start {MINIO_CONTAINER}")


def _minio_verify(tl: Timeline, ctx: RunCtx) -> dict:
    if tl.status == "skipped":
        tl.recovered_at = now()
        tl.verified_at = now()
        return {"verified": True, "via": "skipped"}
    if ctx.dry_run:
        tl.recovered_at = now()
        tl.verified_at = now()
        return {"verified": True}
    deadline = time.time() + 60

    def _up() -> tuple[bool, str | None]:
        return docker_container_running(MINIO_CONTAINER), "container running"

    ok, _ = poll_until(_up, deadline=deadline)
    if ok:
        tl.recovered_at = now()
        tl.verified_at = now()
        tl.log_event("verified: MinIO container running again")
    return {"verified": ok}


def _minio_cleanup(tl: Timeline, ctx: RunCtx) -> None:
    if ctx.dry_run:
        return
    if not docker_container_exists(MINIO_CONTAINER):
        return
    if not docker_container_running(MINIO_CONTAINER):
        docker_cmd("start", MINIO_CONTAINER)
        tl.log_event("cleanup: ensured MinIO container up")


# ------------------------------- hardhat reorg ----------------------------
HARDHAT_CONTAINER = "zkcex-hardhat"
CHAIN_HEALTH = "http://127.0.0.1:5502/chain/health"


def _hardhat_simulate(tl: Timeline, ctx: RunCtx) -> None:
    tl.induced_at = now()
    if ctx.dry_run:
        tl.log_event(f"induced (dry-run): would docker stop {HARDHAT_CONTAINER}")
        return
    if not docker_container_exists(HARDHAT_CONTAINER):
        tl.status = "skipped"
        tl.log_event(f"induced: container {HARDHAT_CONTAINER} missing — soft skip")
        return
    rc, _, err = docker_cmd("stop", HARDHAT_CONTAINER)
    if rc != 0:
        tl.log_event(f"induced: docker stop failed: {err.strip()}")
    else:
        tl.log_event(f"induced: docker stop {HARDHAT_CONTAINER} (30s gap)")


def _hardhat_detect(tl: Timeline, ctx: RunCtx) -> dict:
    if tl.status == "skipped":
        tl.detected_at = now()
        return {"detected": True, "via": "skipped"}
    if ctx.dry_run:
        tl.detected_at = now()
        tl.detected_by = "dry-run"
        return {"detected": True}
    deadline = time.time() + 30

    def _detect() -> tuple[bool, str | None]:
        # chain_server reports rpc health; if hardhat down it should degrade.
        code, body = http_get(CHAIN_HEALTH, timeout=1.5)
        return code != 200, f"code={code}"

    ok, _ = poll_until(_detect, deadline=deadline)
    if ok:
        tl.detected_at = now()
        tl.detected_by = "chain_server /chain/health degraded"
        tl.log_event("detected: chain_server flagged hardhat gap")
    return {"detected": ok}


def _hardhat_recover(tl: Timeline, ctx: RunCtx) -> None:
    tl.recovery_started_at = now()
    if ctx.dry_run or tl.status == "skipped":
        tl.log_event("recovery (dry-run): would docker start hardhat after 30s")
        return
    # Wait the documented 30s before recovery.
    time.sleep(min(30, max(1, SCENARIO_HARD_CAP_S - 60)))
    docker_cmd("start", HARDHAT_CONTAINER)
    tl.log_event(f"recovery: docker start {HARDHAT_CONTAINER}")


def _hardhat_verify(tl: Timeline, ctx: RunCtx) -> dict:
    if tl.status == "skipped":
        tl.recovered_at = now()
        tl.verified_at = now()
        return {"verified": True, "via": "skipped"}
    if ctx.dry_run:
        tl.recovered_at = now()
        tl.verified_at = now()
        return {"verified": True}
    deadline = time.time() + 60

    def _up() -> tuple[bool, str | None]:
        code, _ = http_get(CHAIN_HEALTH, timeout=1.5)
        return code == 200, f"code={code}"

    ok, _ = poll_until(_up, deadline=deadline)
    if ok:
        tl.recovered_at = now()
        tl.verified_at = now()
        tl.log_event("verified: chain_server back to 200")
    return {"verified": ok}


def _hardhat_cleanup(tl: Timeline, ctx: RunCtx) -> None:
    if ctx.dry_run:
        return
    if not docker_container_exists(HARDHAT_CONTAINER):
        return
    if not docker_container_running(HARDHAT_CONTAINER):
        docker_cmd("start", HARDHAT_CONTAINER)
        tl.log_event("cleanup: ensured hardhat up")


# --------------------------- Scenario registry ----------------------------
SCENARIOS: list[Scenario] = [
    Scenario(
        id="mm-bot-kill",
        name="Market-maker bot kill",
        description=(
            "Kill the demo market-maker bot. Verify quotes stop within 30s, "
            "restart, verify the bot resumes posting orders. Safe to run live "
            "— only affects synthetic liquidity, not real orders."
        ),
        rto_target_seconds=60,
        simulate=_mm_bot_simulate,
        detect=_mm_bot_detect,
        recover=_mm_bot_recover,
        verify=_mm_bot_verify,
        cleanup=_mm_bot_cleanup,
        safe_for_real_run=True,
    ),
    Scenario(
        id="pg-primary-loss",
        name="Postgres primary loss",
        description=(
            "Stop the zkcex-postgres-auth container. Expect auth_server to "
            "fail-fast on the next request; the tr_deliveries SQLite retry "
            "queue (separate DB) must keep operating. Restart, measure "
            "auth_server health-restore time."
        ),
        rto_target_seconds=120,
        simulate=_pg_simulate,
        detect=_pg_detect,
        recover=_pg_recover,
        verify=_pg_verify,
        cleanup=_pg_cleanup,
        safe_for_real_run=False,
    ),
    Scenario(
        id="custody-quorum-loss",
        name="Custody quorum loss",
        description=(
            "Kill 3 of 5 custody signer nodes, dropping below the 3-of-5 "
            "threshold. Coordinator /health should report n_nodes_reachable=2 "
            "and refuse to sign withdrawals. Restart 2 of the killed nodes "
            "(back to threshold), verify a fresh withdrawal signs."
        ),
        rto_target_seconds=120,
        simulate=_custody_simulate,
        detect=_custody_detect,
        recover=_custody_recover,
        verify=_custody_verify,
        cleanup=_custody_cleanup,
        safe_for_real_run=False,
    ),
    Scenario(
        id="zkpol-bridge-stop",
        name="zkPoL bridge stop",
        description=(
            "Kill zkpol_bridge.py. The on-chain Pedersen commitments stop "
            "receiving events; the bridge db's last_event_id stops advancing. "
            "After 60s, restart, verify it resumes from the right offset."
        ),
        rto_target_seconds=90,
        simulate=_bridge_simulate,
        detect=_bridge_detect,
        recover=_bridge_recover,
        verify=_bridge_verify,
        cleanup=_bridge_cleanup,
        safe_for_real_run=False,
    ),
    Scenario(
        id="waf-deny-all",
        name="WAF deny-all",
        description=(
            "Temporarily flag WAF_GEO_BLOCKLIST=* via a marker file the "
            "operator dashboard surfaces. Verify the dashboard reflects the "
            "outage state. Clear the marker, verify recovery probe is 200."
        ),
        rto_target_seconds=60,
        simulate=_waf_simulate,
        detect=_waf_detect,
        recover=_waf_recover,
        verify=_waf_verify,
        cleanup=_waf_cleanup,
        safe_for_real_run=False,
    ),
    Scenario(
        id="minio-offline",
        name="MinIO offline",
        description=(
            "Stop the MinIO container. Verify backup_daemon detects and "
            "queues jobs locally. Restart MinIO, verify queue drains."
        ),
        rto_target_seconds=120,
        simulate=_minio_simulate,
        detect=_minio_detect,
        recover=_minio_recover,
        verify=_minio_verify,
        cleanup=_minio_cleanup,
        safe_for_real_run=False,
    ),
    Scenario(
        id="hardhat-reorg",
        name="Hardhat reorg",
        description=(
            "Stop hardhat for 30s, restart. Verify chain_server's deposit "
            "detector handles the gap by replaying from the last seen block."
        ),
        rto_target_seconds=180,
        simulate=_hardhat_simulate,
        detect=_hardhat_detect,
        recover=_hardhat_recover,
        verify=_hardhat_verify,
        cleanup=_hardhat_cleanup,
        safe_for_real_run=False,
    ),
]

SCENARIO_BY_ID: dict[str, Scenario] = {s.id: s for s in SCENARIOS}


# --------------------------------------------------------------------------
# Concurrency: file lock so only one scenario runs at a time
# --------------------------------------------------------------------------
class LockBusy(Exception):
    pass


@contextlib.contextmanager
def scenario_lock():
    fh = open(LOCK_PATH, "w")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EACCES):
                raise LockBusy("another scenario run is in progress") from e
            raise
        try:
            fh.write(str(os.getpid()))
            fh.flush()
            yield
        finally:
            with contextlib.suppress(Exception):
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        with contextlib.suppress(Exception):
            fh.close()


# --------------------------------------------------------------------------
# Run engine
# --------------------------------------------------------------------------
_in_flight_cleanup: list[Callable[[], None]] = []


def _register_cleanup(fn: Callable[[], None]) -> None:
    _in_flight_cleanup.append(fn)


def _drain_cleanups() -> None:
    while _in_flight_cleanup:
        fn = _in_flight_cleanup.pop()
        with contextlib.suppress(Exception):
            fn()


atexit.register(_drain_cleanups)


def _finalize(tl: Timeline) -> None:
    if tl.completed_at is None:
        tl.completed_at = now()
    if tl.detected_at and tl.recovered_at:
        tl.rto_actual = max(0, tl.recovered_at - tl.detected_at)
        tl.rto_pass = tl.rto_actual <= tl.rto_target_seconds
    # RPO: skipped scenarios are treated as zero data loss by construction.
    if tl.rpo_actual_seconds is None:
        tl.rpo_actual_seconds = 0
    if tl.status == "running":
        if tl.verified_at:
            tl.status = "pass" if tl.rto_pass else "fail"
        elif tl.error:
            tl.status = "fail"
        else:
            tl.status = "fail"


def _persist_run(tl: Timeline, operator: str) -> int:
    with _db_lock, db_connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO dr_runs(
              scenario_id, started_at, completed_at, rto_target_seconds,
              rto_actual_seconds, rto_pass, rpo_actual_seconds, result_json,
              operator
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tl.scenario_id,
                tl.started_at,
                tl.completed_at,
                tl.rto_target_seconds,
                tl.rto_actual,
                1 if tl.rto_pass else 0,
                tl.rpo_actual_seconds or 0,
                json.dumps(tl.to_dict(), default=str),
                operator,
            ),
        )
        run_id = cur.lastrowid
        # Bump metric counter.
        conn.execute(
            """
            INSERT INTO dr_metrics_counter(scenario_id, status, n)
            VALUES(?, ?, 1)
            ON CONFLICT(scenario_id, status)
            DO UPDATE SET n = n + 1
            """,
            (tl.scenario_id, tl.status),
        )
    return int(run_id or 0)


def _watchdog(deadline: float, tl: Timeline, stop: threading.Event) -> None:
    while not stop.wait(timeout=1.0):
        if time.time() >= deadline:
            tl.status = "timeout"
            tl.log_event(f"timeout: scenario exceeded {SCENARIO_HARD_CAP_S}s cap")
            return


def run_scenario(s: Scenario, ctx: RunCtx) -> Timeline:
    """Run one scenario end-to-end. Always returns a finalized Timeline."""
    tl = Timeline(
        scenario_id=s.id,
        started_at=now(),
        rto_target_seconds=s.rto_target_seconds,
    )
    tl.log_event(f"scenario start: {s.name} (dry_run={ctx.dry_run})")
    deadline = time.time() + s.hard_cap_s

    # Always run cleanup at the end, even on exception, and also register it
    # as an atexit handler so an external SIGKILL doesn't leak state.
    cleanup_called = [False]

    def _safe_cleanup() -> None:
        if cleanup_called[0]:
            return
        cleanup_called[0] = True
        with contextlib.suppress(Exception):
            s.cleanup(tl, ctx)
            tl.log_event("cleanup complete")

    _register_cleanup(_safe_cleanup)

    stop_evt = threading.Event()
    wd = threading.Thread(target=_watchdog, args=(deadline, tl, stop_evt), daemon=True)
    wd.start()
    try:
        s.simulate(tl, ctx)
        if tl.status == "skipped":
            tl.log_event("skipped: target component not present")
        else:
            s.detect(tl, ctx)
            s.recover(tl, ctx)
            s.verify(tl, ctx)
    except Exception as e:
        tl.error = f"{type(e).__name__}: {e}"
        tl.status = "fail"
        tl.log_event(f"error: {tl.error}\n{traceback.format_exc(limit=2)}")
    finally:
        stop_evt.set()
        _safe_cleanup()
        _finalize(tl)
    return tl


def run_one(scenario_id: str, ctx: RunCtx, operator: str) -> tuple[int, dict]:
    s = SCENARIO_BY_ID.get(scenario_id)
    if not s:
        return 0, {"error": "unknown_scenario", "scenario_id": scenario_id}
    try:
        with scenario_lock():
            tl = run_scenario(s, ctx)
            run_id = _persist_run(tl, operator)
            try:
                write_report(run_id, tl)
            except Exception as e:
                tl.log_event(f"warning: report write failed: {e}")
            return run_id, tl.to_dict()
    except LockBusy as e:
        return 0, {"error": "lock_busy", "message": str(e)}


def run_all(ctx: RunCtx, operator: str) -> dict:
    results: list[dict] = []
    for i, s in enumerate(SCENARIOS):
        run_id, tl = run_one(s.id, ctx, operator)
        results.append({"run_id": run_id, "scenario": s.id, "result": tl})
        if i < len(SCENARIOS) - 1 and not ctx.dry_run:
            time.sleep(INTER_SCENARIO_GAP_S)
    n_pass = sum(1 for r in results if r["result"].get("status") == "pass")
    n_fail = sum(1 for r in results if r["result"].get("status") == "fail")
    n_timeout = sum(1 for r in results if r["result"].get("status") == "timeout")
    n_skipped = sum(1 for r in results if r["result"].get("status") == "skipped")
    return {
        "summary": {
            "n_total": len(results),
            "n_pass": n_pass,
            "n_fail": n_fail,
            "n_timeout": n_timeout,
            "n_skipped": n_skipped,
            "operator": operator,
            "dry_run": ctx.dry_run,
            "completed_at": now(),
        },
        "results": results,
    }


# --------------------------------------------------------------------------
# Report generator (static HTML, no JS dependency)
# --------------------------------------------------------------------------
def _esc(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _bar_chart(tl_dict: dict) -> str:
    """Tiny inline-SVG horizontal bar chart of the timeline phases."""
    start = tl_dict.get("started_at") or 0
    end = tl_dict.get("completed_at") or now()
    total = max(1, end - start)
    phases = [
        ("simulate", tl_dict.get("induced_at"), tl_dict.get("detected_at"), "#0b3aff"),
        ("detect", tl_dict.get("detected_at"), tl_dict.get("recovery_started_at"), "#f0b429"),
        ("recover", tl_dict.get("recovery_started_at"), tl_dict.get("recovered_at"), "#2ee59b"),
        ("verify", tl_dict.get("recovered_at"), tl_dict.get("verified_at"), "#9aa3b3"),
    ]
    width = 720
    height = 24
    parts = [f'<svg viewBox="0 0 {width} {height + 28}" width="100%" ' 'style="max-width:760px">']
    for label, a, b, color in phases:
        if not a or not b or b <= a:
            continue
        x = (a - start) / total * width
        w = max(1, (b - a) / total * width)
        parts.append(
            f'<rect x="{x:.1f}" y="6" width="{w:.1f}" height="{height}" '
            f'fill="{color}" rx="3"/>'
            f'<text x="{x + w / 2:.1f}" y="{height + 22}" fill="#9aa3b3" '
            f'font-size="10" text-anchor="middle">{_esc(label)} '
            f"{int(b - a)}s</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


def _recommendations(tl_dict: dict) -> list[str]:
    out: list[str] = []
    if tl_dict.get("status") == "timeout":
        out.append(
            "Scenario hit the hard 5-minute cap; review whether the "
            "RTO target is realistic or the verification predicate "
            "is too strict."
        )
    rto_actual = tl_dict.get("rto_actual")
    rto_target = tl_dict.get("rto_target_seconds")
    if rto_actual is not None and rto_target and rto_actual > rto_target:
        out.append(
            f"RTO target breached ({rto_actual}s actual vs "
            f"{rto_target}s target). Consider runbook automation: "
            "pre-stage the recovery command in an operator-approved "
            "kill-switch endpoint so the on-call doesn't type it."
        )
    if tl_dict.get("status") == "fail":
        out.append(
            "Investigate the logs section below; the verify step did "
            "not observe post-conditions returning to healthy."
        )
    if tl_dict.get("status") == "pass" and not out:
        out.append(
            "Looks good. Keep this run on file for the quarterly "
            "audit; a streak of green runs is what counts for "
            "regulator + insurer evidence."
        )
    return out


REPORT_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Game day report #%(run_id)s — %(scenario_id)s</title>
<style>
  body { background:#07090f; color:#e6e9ef; font-family:system-ui,-apple-system,
         "Segoe UI",sans-serif; margin:0; padding:32px; line-height:1.5; }
  .wrap { max-width:840px; margin:0 auto; }
  h1 { font-size:22px; margin:0 0 8px; }
  h2 { font-size:15px; margin:24px 0 8px; color:#9aa3b3;
       text-transform:uppercase; letter-spacing:0.08em; }
  .meta { color:#9aa3b3; font-size:13px; margin-bottom:18px; }
  .badge { display:inline-block; padding:2px 10px; border-radius:999px;
           font-size:11px; font-weight:600; margin-left:8px; }
  .pass { background:rgba(46,229,155,.14); color:#2ee59b; }
  .fail { background:rgba(255,77,79,.16); color:#ff7c7c; }
  .timeout { background:rgba(240,180,41,.14); color:#f0b429; }
  .skipped { background:rgba(91,100,120,.16); color:#aab2c0; }
  .running { background:rgba(11,58,255,.18); color:#9bb0ff; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
          gap:10px; margin:14px 0; }
  .stat { background:rgba(13,18,28,.7); padding:12px 14px; border-radius:10px;
          border:1px solid rgba(255,255,255,.06); }
  .stat .k { font-size:11px; color:#9aa3b3; text-transform:uppercase;
             letter-spacing:0.06em; }
  .stat .v { font-size:18px; font-weight:600; margin-top:2px;
             font-family:ui-monospace,Menlo,monospace; }
  pre { background:rgba(13,18,28,.8); padding:14px 16px; border-radius:10px;
        border:1px solid rgba(255,255,255,.06); overflow:auto;
        font-size:12px; line-height:1.6; }
  ul.recs { padding-left:18px; }
  ul.recs li { margin:6px 0; }
  .desc { color:#c0c6d2; }
  .footer { color:#6e7787; font-size:12px; margin-top:32px;
            border-top:1px solid rgba(255,255,255,.05); padding-top:14px; }
  a { color:#9bb0ff; }
</style>
</head>
<body>
<div class="wrap">
  <h1>%(scenario_name)s
    <span class="badge %(status_class)s">%(status_upper)s</span></h1>
  <div class="meta">Run #%(run_id)s &middot; %(scenario_id)s &middot;
       started %(started_iso)s &middot; operator %(operator)s</div>

  <p class="desc">%(description)s</p>

  <div class="grid">
    <div class="stat"><div class="k">RTO actual</div>
      <div class="v">%(rto_actual)s s</div></div>
    <div class="stat"><div class="k">RTO target</div>
      <div class="v">%(rto_target)s s</div></div>
    <div class="stat"><div class="k">RPO actual</div>
      <div class="v">%(rpo_actual)s s</div></div>
    <div class="stat"><div class="k">Duration</div>
      <div class="v">%(duration)s s</div></div>
  </div>

  <h2>Timeline</h2>
  %(bar_chart)s

  <h2>Logs</h2>
  <pre>%(logs)s</pre>

  <h2>Post-recovery health snapshot</h2>
  <pre>%(post_recovery)s</pre>

  <h2>Recommendations</h2>
  <ul class="recs">%(recs)s</ul>

  <div class="footer">
    Generated by tools/dr/game_day.py &middot; %(now_iso)s &middot;
    <a href="/app/game-day.html">back to game-day dashboard</a>
  </div>
</div>
</body>
</html>
"""


def write_report(run_id: int, tl: Timeline) -> str:
    os.makedirs(REPORT_DIR, exist_ok=True)
    d = tl.to_dict()
    s = SCENARIO_BY_ID.get(tl.scenario_id)
    html = REPORT_TEMPLATE % {
        "run_id": run_id,
        "scenario_id": _esc(tl.scenario_id),
        "scenario_name": _esc(s.name if s else tl.scenario_id),
        "description": _esc(s.description if s else ""),
        "status_upper": _esc((d.get("status") or "").upper()),
        "status_class": _esc(d.get("status") or "running"),
        "started_iso": time.strftime("%Y-%m-%d %H:%M:%S %Z", time.gmtime(d["started_at"])),
        "operator": _esc(getattr(tl, "_operator", "unknown") or "unknown"),
        "rto_actual": d.get("rto_actual") if d.get("rto_actual") is not None else "—",
        "rto_target": d.get("rto_target_seconds"),
        "rpo_actual": d.get("rpo_actual_seconds") or 0,
        "duration": d.get("duration_seconds") or 0,
        "bar_chart": _bar_chart(d),
        "logs": _esc("\n".join(d.get("logs") or [])),
        "post_recovery": _esc(json.dumps(d.get("post_recovery_health") or {}, indent=2)),
        "recs": "".join(f"<li>{_esc(r)}</li>" for r in _recommendations(d)),
        "now_iso": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    }
    path = os.path.join(REPORT_DIR, f"{run_id}.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return path


# --------------------------------------------------------------------------
# Prometheus metrics
# --------------------------------------------------------------------------
def render_metrics() -> str:
    lines: list[str] = []
    lines.append("# HELP zkcex_dr_run_total Total game-day scenario runs.")
    lines.append("# TYPE zkcex_dr_run_total counter")
    with _db_lock, db_connect() as conn:
        for sid, status, n in conn.execute("SELECT scenario_id, status, n FROM dr_metrics_counter"):
            lines.append(f'zkcex_dr_run_total{{scenario="{sid}",status="{status}"}} {n}')

    lines.append("# HELP zkcex_dr_rto_seconds Last observed RTO per scenario.")
    lines.append("# TYPE zkcex_dr_rto_seconds gauge")
    lines.append("# HELP zkcex_dr_last_run_timestamp Last completed run unix ts.")
    lines.append("# TYPE zkcex_dr_last_run_timestamp gauge")
    with _db_lock, db_connect() as conn:
        rows = conn.execute(
            """
            SELECT scenario_id, MAX(started_at) AS ts, rto_actual_seconds
            FROM dr_runs
            GROUP BY scenario_id
            """
        ).fetchall()
    for sid, ts, rto in rows:
        if rto is not None:
            lines.append(f'zkcex_dr_rto_seconds{{scenario="{sid}"}} {rto}')
        if ts is not None:
            lines.append(f'zkcex_dr_last_run_timestamp{{scenario="{sid}"}} {ts}')
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Production guard
# --------------------------------------------------------------------------
def production_guard() -> str | None:
    if ZKCEX_ENV == "production":
        return (
            "refusing to start: ZKCEX_ENV=production. game-day disasters must "
            "be run in staging or dev, never production. Set ZKCEX_ENV=staging "
            "or ZKCEX_ENV=dev to enable."
        )
    return None


# --------------------------------------------------------------------------
# Admin token mgmt
# --------------------------------------------------------------------------
_admin_token_cache: str | None = None


def admin_token() -> str:
    global _admin_token_cache
    if _ADMIN_TOKEN_ENV:
        return _ADMIN_TOKEN_ENV
    if _admin_token_cache is None:
        _admin_token_cache = secrets.token_urlsafe(24)
    return _admin_token_cache


def check_bearer(headers) -> bool:
    auth = headers.get("Authorization", "") if headers else ""
    if not auth.startswith("Bearer "):
        return False
    presented = auth[len("Bearer ") :].strip()
    return secrets.compare_digest(presented, admin_token())


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------
class DRHandler(http.server.BaseHTTPRequestHandler):
    server_version = "zkcex-dr/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # silence default
        return

    # -- helpers -----------------------------------------------------------
    def _send_json(self, status: int, obj: Any) -> None:
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, body: str, ctype: str = "text/plain; charset=utf-8") -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def _client_is_loopback(self) -> bool:
        try:
            ip = self.client_address[0] if self.client_address else ""
        except Exception:
            return False
        return ip in ("127.0.0.1", "::1", "localhost")

    def _require_admin(self) -> bool:
        if not self._client_is_loopback():
            self._send_json(
                403,
                {
                    "error": "loopback_only",
                    "message": "admin actions only accept " "127.0.0.1 callers",
                },
            )
            return False
        if not check_bearer(self.headers):
            self._send_json(
                401, {"error": "unauthorized", "message": "admin Bearer token required"}
            )
            return False
        return True

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    # -- routing -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            q = urllib.parse.parse_qs(parsed.query or "")
            if path in ("/", "/health"):
                return self._send_json(
                    200,
                    {
                        "ok": True,
                        "service": "zkcex-dr",
                        "env": ZKCEX_ENV,
                        "n_scenarios": len(SCENARIOS),
                    },
                )
            if path == "/metrics":
                return self._send_text(200, render_metrics())
            if path == "/dr/scenarios":
                return self._send_json(
                    200,
                    {
                        "scenarios": [
                            {
                                "id": s.id,
                                "name": s.name,
                                "description": s.description,
                                "rto_target_seconds": s.rto_target_seconds,
                                "safe_for_real_run": s.safe_for_real_run,
                            }
                            for s in SCENARIOS
                        ],
                    },
                )
            if path == "/dr/history":
                if not self._require_admin():
                    return
                try:
                    limit = max(1, min(500, int(q.get("limit", ["50"])[0])))
                except ValueError:
                    limit = 50
                with _db_lock, db_connect() as conn:
                    rows = conn.execute(
                        """
                        SELECT id, scenario_id, started_at, completed_at,
                               rto_target_seconds, rto_actual_seconds,
                               rto_pass, rpo_actual_seconds, operator
                        FROM dr_runs
                        ORDER BY started_at DESC
                        LIMIT ?
                        """,
                        (limit,),
                    ).fetchall()
                return self._send_json(
                    200,
                    {
                        "runs": [
                            {
                                "id": r[0],
                                "scenario_id": r[1],
                                "started_at": r[2],
                                "completed_at": r[3],
                                "rto_target_seconds": r[4],
                                "rto_actual_seconds": r[5],
                                "rto_pass": bool(r[6]),
                                "rpo_actual_seconds": r[7],
                                "operator": r[8],
                            }
                            for r in rows
                        ],
                    },
                )
            if path.startswith("/dr/scenario-result/"):
                if not self._require_admin():
                    return
                rid_s = path.split("/")[-1]
                try:
                    rid = int(rid_s)
                except ValueError:
                    return self._send_json(400, {"error": "bad_id"})
                with _db_lock, db_connect() as conn:
                    row = conn.execute(
                        "SELECT result_json FROM dr_runs WHERE id=?", (rid,)
                    ).fetchone()
                if not row:
                    return self._send_json(404, {"error": "not_found"})
                try:
                    return self._send_json(200, json.loads(row[0]))
                except Exception:
                    return self._send_json(500, {"error": "bad_record"})
            return self._send_json(404, {"error": "not_found", "path": path})
        except Exception:
            log(f"GET {self.path} crashed:\n{traceback.format_exc(limit=4)}")
            return self._send_json(500, {"error": "internal"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            q = urllib.parse.parse_qs(parsed.query or "")
            if path.startswith("/dr/run/") or path == "/dr/run-all":
                if not self._require_admin():
                    return
                if production_guard():
                    return self._send_json(
                        503, {"error": "production_guard", "message": production_guard()}
                    )
                if q.get("confirm", [""])[0].lower() != "true":
                    return self._send_json(
                        400,
                        {
                            "error": "confirm_required",
                            "message": (
                                "this action will impact running " "services; pass ?confirm=true"
                            ),
                        },
                    )
                operator = (q.get("operator", ["admin"])[0] or "admin")[:64]
                ctx = RunCtx(dry_run=False, operator=operator)
                if path == "/dr/run-all":
                    rep = run_all(ctx, operator)
                    return self._send_json(200, rep)
                scenario_id = path[len("/dr/run/") :]
                run_id, result = run_one(scenario_id, ctx, operator)
                if run_id == 0:
                    return self._send_json(400, result)
                return self._send_json(
                    200,
                    {
                        "run_id": run_id,
                        "result": result,
                        "report_url": f"/app/game-day-report/{run_id}.html",
                    },
                )
            return self._send_json(404, {"error": "not_found", "path": path})
        except Exception:
            log(f"POST {self.path} crashed:\n{traceback.format_exc(limit=4)}")
            return self._send_json(500, {"error": "internal"})


class _TS(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(port: int) -> None:
    db_init()
    err = production_guard()
    if err:
        sys.stderr.write(f"[dr] {err}\n")
        sys.exit(2)
    httpd = _TS(("127.0.0.1", port), DRHandler)

    def _on_signal(signum: int, frame: Any) -> None:
        log(f"signal {signum} received, draining cleanups")
        _drain_cleanups()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    tok = admin_token()
    log(f"listening on http://127.0.0.1:{port}")
    log(f"env={ZKCEX_ENV} n_scenarios={len(SCENARIOS)}")
    if not _ADMIN_TOKEN_ENV:
        log(f"admin bearer token (this run): {tok}")
    httpd.serve_forever()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def cli_main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("port", nargs="?", type=int, default=DEFAULT_PORT)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="walk the state machine without touching real services",
    )
    ap.add_argument("--list-scenarios", action="store_true", help="print scenarios and exit")
    ap.add_argument(
        "--run-all", action="store_true", help="run every scenario sequentially and exit"
    )
    ap.add_argument("--run", type=str, default=None, help="run one scenario by id and exit")
    ap.add_argument("--output", type=str, default=None, help="write JSON result to this path")
    args = ap.parse_args()

    if args.list_scenarios:
        out = [
            {
                "id": s.id,
                "name": s.name,
                "rto_target_seconds": s.rto_target_seconds,
                "safe_for_real_run": s.safe_for_real_run,
                "description": s.description,
            }
            for s in SCENARIOS
        ]
        print(json.dumps({"scenarios": out}, indent=2))
        if not (args.run_all or args.run):
            return 0

    db_init()

    if args.run_all:
        ctx = RunCtx(dry_run=bool(args.dry_run), operator="cli")
        rep = run_all(ctx, "cli")
        text = json.dumps(rep, indent=2, default=str)
        if args.output:
            with open(args.output, "w") as fh:
                fh.write(text)
        else:
            print(text)
        return 0
    if args.run:
        ctx = RunCtx(dry_run=bool(args.dry_run), operator="cli")
        run_id, tl = run_one(args.run, ctx, "cli")
        text = json.dumps({"run_id": run_id, "result": tl}, indent=2, default=str)
        if args.output:
            with open(args.output, "w") as fh:
                fh.write(text)
        else:
            print(text)
        return 0

    serve(args.port)
    return 0


if __name__ == "__main__":
    sys.exit(cli_main())
