#!/usr/bin/env python3
"""zkCEX backup + PITR daemon (port 5670).

Standalone stdlib HTTP service that coordinates the full backup
pipeline: SQLite VACUUM-INTO snapshots, Postgres base + WAL streaming,
MariaDB full dumps + binlog snapshots, plus chain-state objects. All
backups are client-side encrypted before upload to MinIO under a
deterministic key layout::

   <source>/<date>/<sequence>.<ext>            (full + most data backups)
   postgres-auth/wal/<date>/<segment>.enc      (WAL stream)
   mariadb-zkpol/binlog/<date>/<segment>.enc   (binlog)
   postgres-auth/logical/<date>/<seq>.dump.enc (logical PITR fallback)

State at ``tools/.local/backup.db``. Master encryption key at
``tools/.local/backup_encryption.key`` (auto-generated, 0600).

Endpoints
---------
   GET  /backup/health                  -- public
   GET  /backup/jobs?limit=N            -- admin Bearer
   GET  /backup/inventory?source=...    -- admin
   GET  /backup/storage                 -- admin
   POST /backup/run/<source>            -- admin (trigger ad-hoc)
   POST /backup/restore                 -- admin (PITR restore)

The schedule loop runs in a background thread; jobs that fall in the
current UTC minute and haven't already run today are dispatched. It's
*not* a full cron implementation -- the demo just needs nightly
windows -- but it's deterministic and easy to extend.

Run::
   python3 -m tools.backup.backup_daemon 5670
   # or
   python3 tools/backup/backup_daemon.py 5670
"""

from __future__ import annotations

import datetime as _dt
import http.server
import json
import os
import secrets
import socketserver
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse

# Allow direct script invocation -- when started as
# ``python3 tools/backup/backup_daemon.py`` the package layout is fine
# because the parent directory is on sys.path; we only need to make sure
# imports work either way.
if __package__ in (None, ""):
    HERE = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.dirname(HERE))
    from backup import common as C
    from backup import crypto as K
    from backup import dr_drill as DR
    from backup import mariadb_backup as MB
    from backup import postgres_backup as PB
    from backup import restore as R
    from backup import sqlite_backup as SB
    from backup.s3_client import S3Client
else:
    from . import common as C
    from . import crypto as K
    from . import dr_drill as DR
    from . import mariadb_backup as MB
    from . import postgres_backup as PB
    from . import restore as R
    from . import sqlite_backup as SB
    from .s3_client import S3Client


_runtime_state: dict[str, object] = {}
_scheduler_state: dict[str, set] = {"done_today": set()}
_active_jobs_lock = threading.Lock()
_active_jobs: set[str] = set()
LISTEN_HOST = os.environ.get("BACKUP_HOST", "127.0.0.1")


def log(msg: str) -> None:
    sys.stderr.write(f"[backup-daemon] {msg}\n")
    sys.stderr.flush()


# ==========================================================================
# Admin token (matches the style used by safu_server / ops_server)
# ==========================================================================
def get_admin_token() -> str:
    tok = os.environ.get("BACKUP_ADMIN_TOKEN")
    if tok:
        return tok
    cached = _runtime_state.get("admin_token")
    if cached:
        return str(cached)
    tok = "backup_" + secrets.token_urlsafe(24)
    _runtime_state["admin_token"] = tok
    log(f"BACKUP_ADMIN_TOKEN={tok}  (set in env to make persistent)")
    return tok


# ==========================================================================
# Dispatch
# ==========================================================================
SOURCE_DISPATCH = {
    # SQLite databases
    **{f"sqlite-{n}": (lambda n=n: SB.backup_one_sqlite(n)) for n in C.SQLITE_SOURCES},
    "sqlite-all": SB.backup_all_sqlite,
    # Postgres
    "postgres-auth": PB.full_base_backup,
    "postgres-auth-logical": (lambda: PB._logical_dump(C.open_db(), time.time())),
    # MariaDB
    "mariadb-zkpol": MB.full_dump,
    "mariadb-zkpol-binlog": MB.binlog_snapshot,
    # Chain state
    "custody-shares": (
        lambda: [
            SB.backup_chain_state_file(f, "custody")
            for f in C.CHAIN_STATE_FILES
            if f.startswith("custody/") and os.path.exists(os.path.join(C.LOCAL_DIR, f))
        ]
    ),
    "pol-signing-key": (
        lambda: SB.backup_chain_state_file("pol_signing_key", "pol-signing-key")
        if os.path.exists(os.path.join(C.LOCAL_DIR, "pol_signing_key"))
        else None
    ),
    "vapid": (
        lambda: SB.backup_chain_state_file("vapid.json", "custody")
        if os.path.exists(os.path.join(C.LOCAL_DIR, "vapid.json"))
        else None
    ),
    # Drill (not a real backup; included so operators can trigger via API)
    "dr-drill": DR.run,
}


def _run_source(source: str) -> dict:
    fn = SOURCE_DISPATCH.get(source)
    if not fn:
        raise ValueError(f"unknown source {source!r}")
    with _active_jobs_lock:
        if source in _active_jobs:
            raise RuntimeError(f"backup of {source} already in progress")
        _active_jobs.add(source)
    try:
        result = fn()
        return {"source": source, "ok": True, "result": result}
    finally:
        with _active_jobs_lock:
            _active_jobs.discard(source)


# ==========================================================================
# Scheduler
# ==========================================================================
class Scheduler(threading.Thread):
    """Minimal cron-style scheduler keyed on UTC HH:MM strings.

    On each tick (60s) we look at every entry in ``schedules`` and ask:
    is the current UTC time >= the scheduled time and have we NOT run
    this entry today? If so, dispatch.

    All bookkeeping is in-memory; on daemon restart we re-evaluate. We
    don't double-fire because the DB inventory already encodes daily
    sequences and the SQLite VACUUM cycle is idempotent.
    """

    def __init__(self):
        super().__init__(daemon=True, name="backup-scheduler")
        self.stop_event = threading.Event()
        self.schedules = self._load_schedules()
        self.last_day = ""
        self.wal_streamer = None
        self._mariadb_binlog_last = 0.0

    def _load_schedules(self) -> dict[str, str]:
        s = dict(C.DEFAULT_SCHEDULES)
        # Allow env override: BACKUP_SCHEDULE_<source>=HH:MM
        for k, _ in list(s.items()):
            env_k = "BACKUP_SCHEDULE_" + k.upper().replace("-", "_")
            v = os.environ.get(env_k)
            if v:
                s[k] = v
        return s

    def stop(self) -> None:
        self.stop_event.set()
        if self.wal_streamer:
            try:
                self.wal_streamer.stop()
            except Exception as exc:  # noqa: BLE001
                log(f"wal streamer stop failed: {exc!r}")

    def run(self) -> None:
        # Start WAL streamer if Postgres is reachable.
        try:
            if PB.container_up():
                self.wal_streamer = PB.WalStreamer()
                self.wal_streamer.start()
                _runtime_state["wal_streamer"] = True
        except Exception as e:
            log(f"wal streamer not started: {e!r}")

        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                log(f"scheduler tick error: {e!r}")
            self.stop_event.wait(60)

    def _tick(self) -> None:
        now = _dt.datetime.now(_dt.UTC)
        hhmm_now = now.strftime("%H:%M")
        day = now.strftime("%Y-%m-%d")
        if day != self.last_day:
            _scheduler_state["done_today"] = set()
            self.last_day = day

        # Hourly: mariadb binlog
        if time.time() - self._mariadb_binlog_last > 3600:
            self._mariadb_binlog_last = time.time()
            try:
                threading.Thread(
                    target=lambda: _run_source("mariadb-zkpol-binlog"),
                    daemon=True,
                    name="binlog",
                ).start()
            except Exception as e:
                log(f"binlog kick failed: {e!r}")

        for source, sched in self.schedules.items():
            if source in _scheduler_state["done_today"]:
                continue
            # Weekly cron support: "weekly:Sun:04:00"
            if sched.startswith("weekly:"):
                _, dow, hhmm = sched.split(":", 2)
                if now.strftime("%a") != dow:
                    continue
                if hhmm_now < hhmm:
                    continue
            else:
                if hhmm_now < sched:
                    continue
            _scheduler_state["done_today"].add(source)
            log(f"scheduler dispatching {source} (sched={sched})")
            threading.Thread(
                target=self._dispatch_safe,
                args=(source,),
                daemon=True,
                name=f"sched-{source}",
            ).start()

    def _dispatch_safe(self, source: str) -> None:
        try:
            _run_source(source)
        except Exception as e:
            log(f"scheduled {source} failed: {e!r}")


# ==========================================================================
# HTTP handler
# ==========================================================================
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "zkcex-backup/1.0"

    def _send_json(self, status: int, payload):
        body = b"" if payload is None else json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _bearer(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        if not auth.lower().startswith("bearer "):
            return None
        return auth.split(None, 1)[1].strip()

    def _require_admin(self) -> bool:
        tok = self._bearer()
        if not tok or tok != get_admin_token():
            self._send_json(
                401, {"error": "unauthorized", "message": "Bearer BACKUP_ADMIN_TOKEN required"}
            )
            return False
        return True

    def _read_json_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n > 0 else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[backup] {self.address_string()} - {fmt % args}\n")

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            q = urllib.parse.parse_qs(parsed.query or "")
            if path == "/backup/health":
                return self.h_health()
            if path == "/backup/jobs":
                if not self._require_admin():
                    return
                return self.h_jobs(q)
            if path == "/backup/inventory":
                if not self._require_admin():
                    return
                return self.h_inventory(q)
            if path == "/backup/storage":
                if not self._require_admin():
                    return
                return self.h_storage()
            if path == "/backup/sources":
                if not self._require_admin():
                    return
                return self._send_json(200, {"sources": sorted(SOURCE_DISPATCH.keys())})
            self._send_json(404, {"error": "not_found"})
        except Exception as e:
            log("GET error: " + repr(e) + "\n" + traceback.format_exc())
            self._send_json(500, {"error": "internal", "message": str(e)})

    def do_POST(self):  # noqa: N802
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            if path.startswith("/backup/run/"):
                if not self._require_admin():
                    return
                source = path[len("/backup/run/") :]
                return self.h_run(source)
            if path == "/backup/restore":
                if not self._require_admin():
                    return
                return self.h_restore()
            if path == "/backup/drill":
                if not self._require_admin():
                    return
                out = DR.run()
                return self._send_json(200, out)
            self._send_json(404, {"error": "not_found"})
        except Exception as e:
            log("POST error: " + repr(e) + "\n" + traceback.format_exc())
            self._send_json(500, {"error": "internal", "message": str(e)})

    # ---- handlers ----------------------------------------------------
    def h_health(self) -> None:
        # Try MinIO reachability (cheap), tabulate active jobs.
        s3 = S3Client(
            C.MINIO_ENDPOINT,
            C.MINIO_ACCESS_KEY,
            C.MINIO_SECRET_KEY,
            region=C.MINIO_REGION,
            bucket=C.MINIO_BUCKET,
        )
        minio_live = s3.health()
        active = sorted(_active_jobs)
        return self._send_json(
            200,
            {
                "service": "zkcex-backup",
                "version": "1.0",
                "uptime_s": int(time.time() - _runtime_state.get("start_ts", time.time())),
                "minio_live": minio_live,
                "wal_streamer": bool(_runtime_state.get("wal_streamer")),
                "active_jobs": active,
                "schedules": C.DEFAULT_SCHEDULES,
                "now_utc": C.utc_iso(),
            },
        )

    def h_jobs(self, q) -> None:
        limit = max(1, min(500, int(q.get("limit", ["50"])[0])))
        conn = C.open_db()
        rows = conn.execute(
            "SELECT * FROM backup_jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return self._send_json(200, {"jobs": [dict(r) for r in rows]})

    def h_inventory(self, q) -> None:
        source = (q.get("source", [""])[0]).strip()
        conn = C.open_db()
        if source:
            rows = conn.execute(
                "SELECT * FROM backup_inventory WHERE source=? "
                "ORDER BY created_at DESC LIMIT 500",
                (source,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM backup_inventory ORDER BY created_at DESC LIMIT 500"
            ).fetchall()
        return self._send_json(200, {"inventory": [dict(r) for r in rows]})

    def h_storage(self) -> None:
        s3 = S3Client(
            C.MINIO_ENDPOINT,
            C.MINIO_ACCESS_KEY,
            C.MINIO_SECRET_KEY,
            region=C.MINIO_REGION,
            bucket=C.MINIO_BUCKET,
        )
        try:
            stats = s3.bucket_stats()
        except Exception as e:
            return self._send_json(503, {"error": "minio_unreachable", "message": str(e)})
        conn = C.open_db()
        n_inv = conn.execute("SELECT COUNT(*) AS n FROM backup_inventory").fetchone()["n"]
        by_source = [
            dict(r)
            for r in conn.execute(
                "SELECT source, COUNT(*) AS n, SUM(size_bytes) AS bytes "
                "FROM backup_inventory GROUP BY source ORDER BY source"
            ).fetchall()
        ]
        return self._send_json(
            200,
            {
                "bucket": C.MINIO_BUCKET,
                "endpoint": C.MINIO_ENDPOINT,
                "object_count": stats["object_count"],
                "total_bytes": stats["total_bytes"],
                "oldest_object": stats["oldest"],
                "inventory_rows": n_inv,
                "by_source": by_source,
            },
        )

    def h_run(self, source: str) -> None:
        if source not in SOURCE_DISPATCH:
            return self._send_json(
                400,
                {
                    "error": "unknown_source",
                    "source": source,
                    "available": sorted(SOURCE_DISPATCH.keys()),
                },
            )
        try:
            out = _run_source(source)
        except Exception as e:
            return self._send_json(
                500, {"error": "backup_failed", "source": source, "message": repr(e)}
            )
        return self._send_json(200, out)

    def h_restore(self) -> None:
        body = self._read_json_body()
        source = body.get("source")
        if not source:
            return self._send_json(400, {"error": "source required"})
        tgt = body.get("target_time_unix")
        if tgt is None and body.get("target_time"):
            tgt = C.parse_iso(body["target_time"])
        elif tgt is not None:
            tgt = int(tgt)
        dest = body.get("dest_path") or body.get("dest")
        try:
            if source.startswith("sqlite-"):
                dest = dest or os.path.join(tempfile.gettempdir(), f"{source}-restored.db")
                out = R.restore_sqlite(source, dest, target_time=tgt)
            elif source == "postgres-auth":
                mode = body.get("mode", "logical")
                if mode == "physical":
                    out = R.restore_postgres_physical(
                        dest or os.path.join(tempfile.gettempdir(), f"zkcex-pg-{int(time.time())}"),
                        target_time=tgt,
                    )
                else:
                    out = R.restore_postgres_logical(
                        target_time=tgt, dest_db=dest or "zkcex_auth_restored"
                    )
            elif source.startswith("mariadb-"):
                out = R.restore_mariadb(dest_db=dest or "zk_pol_restored", target_time=tgt)
            else:
                return self._send_json(400, {"error": "unknown_source", "source": source})
            return self._send_json(200, out)
        except Exception as e:
            return self._send_json(500, {"error": "restore_failed", "message": repr(e)})


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5670
    _runtime_state["start_ts"] = time.time()
    # initialise DB + key
    C.open_db()
    K.load_or_create_master_key(C.MASTER_KEY_PATH)
    get_admin_token()  # surface the token in logs on first start

    sched = Scheduler()
    sched.start()
    _runtime_state["scheduler"] = sched

    log(f"backup daemon listening on {LISTEN_HOST}:{port}")
    httpd = _ThreadedHTTPServer((LISTEN_HOST, port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sched.stop()


if __name__ == "__main__":
    main()
