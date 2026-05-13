"""Shared paths, config, and helpers for the backup pipeline."""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import sqlite3
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.dirname(HERE)
LOCAL_DIR = os.path.join(TOOLS_DIR, ".local")
os.makedirs(LOCAL_DIR, exist_ok=True)

BACKUP_DB_PATH = os.path.join(LOCAL_DIR, "backup.db")
MASTER_KEY_PATH = os.path.join(LOCAL_DIR, "backup_encryption.key")

MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "http://localhost:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "zkcex")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "zkcex-backup-demo")
MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "zkcex-backups")
MINIO_REGION = os.environ.get("MINIO_REGION", "us-east-1")

# Postgres source config
POSTGRES_CONTAINER = os.environ.get("POSTGRES_CONTAINER", "zkcex-postgres-auth")
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "127.0.0.1")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5433"))
POSTGRES_USER = os.environ.get("POSTGRES_USER", "app")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "app-password")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "zkcex_auth")
POSTGRES_REPL_SLOT = os.environ.get("POSTGRES_REPL_SLOT", "zkcex_backup_slot")
PG_WAL_DIR = os.path.join(tempfile.gettempdir(), "zkcex-pg-wal")
PG_BASE_DIR = os.path.join(tempfile.gettempdir(), "zkcex-pg-base")

# MariaDB source config
MARIADB_CONTAINER = os.environ.get("MARIADB_CONTAINER", "zkpol-mariadb")
MARIADB_PORT = int(os.environ.get("MARIADB_PORT", "21002"))
MARIADB_USER = os.environ.get("MARIADB_USER", "app")
MARIADB_PASSWORD = os.environ.get("MARIADB_PASSWORD", "app-password")
MARIADB_DB = os.environ.get("MARIADB_DB", "zk_pol")

# Retention (in seconds)
RETENTION_SECONDS = {
    "sqlite": 30 * 86400,
    "postgres-full": 30 * 86400,
    "postgres-wal": 7 * 86400,
    "mariadb-full": 30 * 86400,
    "mariadb-binlog": 7 * 86400,
    "custody": 90 * 86400,
    "pol-signing-key": 365 * 86400,
}

# Default schedules (cron-like dispatch times in UTC HH:MM). Keys MUST
# match entries in backup_daemon.SOURCE_DISPATCH.
DEFAULT_SCHEDULES = {
    "postgres-auth": "02:00",  # full base backup + logical dump
    "mariadb-zkpol": "02:30",
    "sqlite-all": "03:00",
    "custody-shares": "03:30",
    "pol-signing-key": "weekly:Sun:04:00",
}

# SQLite databases under .local that we back up nightly. Note: the
# *-shm and *-wal sidecar files are *not* listed here -- VACUUM INTO
# produces a single self-contained file from the running database.
SQLITE_SOURCES = [
    "auth.db",
    "chain.db",
    "perp.db",
    "order_engine.db",
    "mm_bot.db",
    "safu.db",
    "push.db",
    "mcp_calls.db",
    "custody_audit.db",
    "api_keys.db",
    "ops.db",
    "travel_rule.db",
    "zk_orderbook.db",
    "zkpol_bridge.db",
    "pol-snapshot.db",
    "pol.db",
]

# Extra binary state outside sqlite
CHAIN_STATE_FILES = [
    "custody/shard_0.bin",
    "custody/shard_1.bin",
    "custody/shard_2.bin",
    "custody/shard_3.bin",
    "custody/shard_4.bin",
    "custody/public.json",
    "pol_signing_key",
    "pol_snapshot_feed_secret",
    "vapid.json",
]


_db_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS backup_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  source TEXT NOT NULL,
  type TEXT NOT NULL,
  status TEXT NOT NULL,
  bytes_uploaded INTEGER,
  duration_ms INTEGER,
  object_key TEXT,
  checksum_sha256 TEXT,
  error TEXT,
  retention_until INTEGER
);
CREATE INDEX IF NOT EXISTS idx_backup_jobs_ts ON backup_jobs(ts);
CREATE INDEX IF NOT EXISTS idx_backup_jobs_source ON backup_jobs(source);

CREATE TABLE IF NOT EXISTS backup_inventory (
  object_key TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  type TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  checksum_sha256 TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  retention_until INTEGER NOT NULL,
  metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_inv_source ON backup_inventory(source);
CREATE INDEX IF NOT EXISTS idx_inv_created_at ON backup_inventory(created_at);
"""


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(BACKUP_DB_PATH, isolation_level=None, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def log(msg: str) -> None:
    sys.stderr.write(f"[backup] {msg}\n")
    sys.stderr.flush()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def utc_date(ts: int | float | None = None) -> str:
    ts = int(ts if ts is not None else time.time())
    return _dt.datetime.fromtimestamp(ts, _dt.UTC).strftime("%Y-%m-%d")


def utc_iso(ts: int | float | None = None) -> str:
    ts = int(ts if ts is not None else time.time())
    return _dt.datetime.fromtimestamp(ts, _dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> int:
    """Parse an ISO-8601 timestamp into an epoch int. Accepts Z, +00:00,
    or naive timestamps (treated as UTC)."""
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = _dt.datetime.fromisoformat(s)
    except ValueError:
        # try without seconds
        dt = _dt.datetime.strptime(s, "%Y-%m-%dT%H:%M%z")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.UTC)
    return int(dt.timestamp())


def next_sequence_for(conn: sqlite3.Connection, source: str, date: str) -> int:
    """Returns the next free sequence number for (source, date). Sequences
    start at 0. This guarantees idempotent keys for backups on the same day.
    """
    prefix = f"{source}/{date}/"
    row = conn.execute(
        "SELECT object_key FROM backup_inventory WHERE object_key LIKE ? "
        "ORDER BY object_key DESC LIMIT 1",
        (prefix + "%",),
    ).fetchone()
    if not row:
        return 0
    last_key = row["object_key"]
    tail = last_key[len(prefix) :]
    seq_str = tail.split(".", 1)[0]
    try:
        return int(seq_str) + 1
    except ValueError:
        return 0


def insert_job(conn: sqlite3.Connection, source: str, type_: str) -> int:
    cur = conn.execute(
        "INSERT INTO backup_jobs(ts, source, type, status) VALUES(?,?,?,?)",
        (int(time.time()), source, type_, "running"),
    )
    return cur.lastrowid


def update_job(conn: sqlite3.Connection, job_id: int, **fields) -> None:
    keys = list(fields.keys())
    allowed = {
        "status",
        "bytes_uploaded",
        "duration_ms",
        "object_key",
        "checksum_sha256",
        "error",
        "retention_until",
    }
    unknown = sorted(set(keys) - allowed)
    if unknown:
        raise ValueError(f"unsupported backup_jobs columns: {unknown}")
    vals = [fields[k] for k in keys]
    set_clause = ", ".join(f"{k}=?" for k in keys)
    vals.append(job_id)
    conn.execute(f"UPDATE backup_jobs SET {set_clause} WHERE id=?", vals)  # noqa: S608


def record_inventory(
    conn: sqlite3.Connection,
    object_key: str,
    source: str,
    type_: str,
    size: int,
    checksum: str,
    retention_seconds: int,
    metadata_json: str = "{}",
) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO backup_inventory("
        " object_key, source, type, size_bytes, checksum_sha256, created_at,"
        " retention_until, metadata_json) VALUES(?,?,?,?,?,?,?,?)",
        (object_key, source, type_, size, checksum, now, now + retention_seconds, metadata_json),
    )
