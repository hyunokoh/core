"""Generic restore CLI + library.

Usage::

  python3 -m tools.backup.restore \\
      --source sqlite-auth.db \\
      --dest /tmp/auth-restored.db

  python3 -m tools.backup.restore \\
      --source postgres-auth \\
      --target-time 2026-05-12T03:14:00Z \\
      --dest /tmp/zkcex-restore-pg

For SQLite sources we pick the most-recent backup at-or-before
``--target-time`` (default: latest).

For Postgres we restore the latest logical dump produced by the
backup pipeline -- this is the simple PITR path. (Full WAL replay
from base + segments is supported by the same pipeline but is more
involved; we expose it via the ``--mode=physical`` flag.)
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time

if __package__ in (None, ""):
    import os as _os
    import sys as _sys

    _HERE = _os.path.dirname(_os.path.abspath(__file__))
    _sys.path.insert(0, _os.path.dirname(_HERE))
    from backup import common as C
    from backup import crypto as K
    from backup.s3_client import S3Client
else:
    from . import common as C
    from . import crypto as K
    from .s3_client import S3Client


def _new_s3() -> S3Client:
    return S3Client(
        C.MINIO_ENDPOINT,
        C.MINIO_ACCESS_KEY,
        C.MINIO_SECRET_KEY,
        region=C.MINIO_REGION,
        bucket=C.MINIO_BUCKET,
    )


_DB_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _safe_db_identifier(name: str) -> str:
    if not _DB_IDENTIFIER_RE.fullmatch(name):
        raise ValueError(f"unsafe database identifier: {name!r}")
    return name


def _require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"required restore tool not found on PATH: {name}")
    return path


def _restore_temp_path(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    return os.path.join(tempfile.gettempdir(), safe)


def list_backups(source: str, as_of: int | None = None) -> list[dict]:
    """Return matching inventory rows newest-first; if as_of is set,
    filter to rows with created_at <= as_of."""
    conn = C.open_db()
    if as_of is None:
        rows = conn.execute(
            "SELECT * FROM backup_inventory WHERE source=? " "ORDER BY created_at DESC", (source,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM backup_inventory WHERE source=? AND created_at<=? "
            "ORDER BY created_at DESC",
            (source, as_of),
        ).fetchall()
    return [dict(r) for r in rows]


def restore_sqlite(source: str, dest: str, target_time: int | None = None) -> dict:
    """Restore a SQLite database to ``dest``.

    Picks the most recent backup whose ``created_at`` <= target_time.
    """
    rows = list_backups(source, as_of=target_time)
    if not rows:
        raise RuntimeError(f"no backup found for {source} as_of={target_time}")
    row = rows[0]
    s3 = _new_s3()
    blob = s3.get_object(row["object_key"])
    if C.sha256_bytes(blob) != row["checksum_sha256"]:
        raise RuntimeError(f"checksum mismatch for {row['object_key']}")
    master = K.load_or_create_master_key(C.MASTER_KEY_PATH)
    pt = K.decrypt(master, blob)
    raw = gzip.decompress(pt)
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    with open(dest, "wb") as f:
        f.write(raw)
    # quick integrity check
    with sqlite3.connect(dest) as c:
        r = c.execute("PRAGMA integrity_check").fetchone()
        ok = bool(r and r[0] == "ok")
    return {
        "source": source,
        "object_key": row["object_key"],
        "created_at": row["created_at"],
        "dest": dest,
        "bytes": len(raw),
        "integrity_ok": ok,
    }


def restore_postgres_logical(
    target_time: int | None = None, dest_db: str = "zkcex_auth_restored"
) -> dict:
    """Restore the most-recent logical dump <= target_time into a
    fresh DB on the live Postgres cluster.

    The new DB is named ``dest_db``; we DROP+CREATE it first so the
    restore is idempotent. We don't touch the live ``zkcex_auth`` DB.
    """
    dest_db = _safe_db_identifier(dest_db)
    rows = list_backups("postgres-auth-logical", as_of=target_time)
    if not rows:
        raise RuntimeError(f"no logical dump found as_of={target_time}")
    row = rows[0]
    s3 = _new_s3()
    blob = s3.get_object(row["object_key"])
    if C.sha256_bytes(blob) != row["checksum_sha256"]:
        raise RuntimeError(f"checksum mismatch for {row['object_key']}")
    master = K.load_or_create_master_key(C.MASTER_KEY_PATH)
    dump = K.decrypt(master, blob)
    docker = _require_tool("docker")
    # Drop + create target DB
    for sql in (f"DROP DATABASE IF EXISTS {dest_db};", f"CREATE DATABASE {dest_db};"):
        r = subprocess.run(  # noqa: S603 - executable is resolved; SQL identifier is validated above.
            [
                docker,
                "exec",
                "-e",
                f"PGPASSWORD={C.POSTGRES_PASSWORD}",
                C.POSTGRES_CONTAINER,
                "psql",
                "-U",
                C.POSTGRES_USER,
                "-d",
                "postgres",
                "-tAc",
                sql,
            ],
            capture_output=True,
            timeout=30,
        )
        if r.returncode != 0:
            raise RuntimeError(f"{sql} failed: {r.stderr.decode('utf-8','replace')[:200]}")
    # pg_restore on stdin
    r = subprocess.run(  # noqa: S603 - executable is resolved; args are fixed restore flags.
        [
            docker,
            "exec",
            "-i",
            "-e",
            f"PGPASSWORD={C.POSTGRES_PASSWORD}",
            C.POSTGRES_CONTAINER,
            "pg_restore",
            "-U",
            C.POSTGRES_USER,
            "-d",
            dest_db,
            "--no-owner",
            "--clean",
            "--if-exists",
            "--exit-on-error",
        ],
        input=dump,
        capture_output=True,
        timeout=300,
    )
    # pg_restore may exit non-zero with warnings; we surface but don't fail
    # if data made it in.
    warnings = r.stderr.decode("utf-8", "replace")[:400]
    return {
        "source": "postgres-auth-logical",
        "object_key": row["object_key"],
        "created_at": row["created_at"],
        "dest_db": dest_db,
        "pg_restore_rc": r.returncode,
        "pg_restore_warnings": warnings,
    }


def restore_postgres_physical(dest_dir: str, target_time: int | None = None) -> dict:
    """Download the base tar + every WAL segment <= target_time,
    extract to dest_dir and apply WALs to recover to the target time.

    This is the full PITR path. We extract the base to dest_dir/, drop
    a recovery signal + ``recovery.conf``-style postgresql.auto.conf so
    a fresh postgres started against that directory replays WAL up to
    the target time, then stops.

    Note: starting a postgres against this datadir is a separate step
    documented in the runbook -- we just stage the directory here.
    """
    base_rows = list_backups("postgres-auth", as_of=target_time)
    base_rows = [r for r in base_rows if r["type"] == "full"]
    if not base_rows:
        raise RuntimeError("no postgres base backup found")
    base = base_rows[0]
    s3 = _new_s3()
    blob = s3.get_object(base["object_key"])
    if C.sha256_bytes(blob) != base["checksum_sha256"]:
        raise RuntimeError("base backup checksum mismatch")
    master = K.load_or_create_master_key(C.MASTER_KEY_PATH)
    tar_bytes = K.decrypt(master, blob)
    tar = _require_tool("tar")

    os.makedirs(dest_dir, exist_ok=True)
    tar_path = os.path.join(dest_dir, "base.tar")
    with open(tar_path, "wb") as f:
        f.write(tar_bytes)
    # Extract
    r = subprocess.run(  # noqa: S603 - executable is resolved; archive path is created above.
        [tar, "-xf", tar_path, "-C", dest_dir],
        capture_output=True,
        timeout=300,
    )
    if r.returncode != 0:
        raise RuntimeError(f"tar -xf failed: {r.stderr.decode('utf-8','replace')[:200]}")

    # Download WAL segments
    wals = list_backups("postgres-auth-wal", as_of=target_time)
    wals.sort(key=lambda r: r["object_key"])
    wal_dir = os.path.join(dest_dir, "pg_wal")
    os.makedirs(wal_dir, exist_ok=True)
    for w in wals:
        ct = s3.get_object(w["object_key"])
        if C.sha256_bytes(ct) != w["checksum_sha256"]:
            C.log(f"warning: wal segment checksum mismatch: {w['object_key']}")
            continue
        seg = K.decrypt(master, ct)
        # segment name is the last path component minus .enc
        name = w["object_key"].rsplit("/", 1)[-1]
        if name.endswith(".enc"):
            name = name[:-4]
        with open(os.path.join(wal_dir, name), "wb") as f:
            f.write(seg)

    # Write recovery signal + recovery target
    if target_time is not None:
        ts = C.utc_iso(target_time)
        with open(os.path.join(dest_dir, "recovery.signal"), "w") as f:
            pass
        with open(os.path.join(dest_dir, "postgresql.auto.conf"), "a") as f:
            f.write(f"\n# zkCEX PITR\nrecovery_target_time = '{ts}'\n")

    return {
        "dest_dir": dest_dir,
        "base_object_key": base["object_key"],
        "wal_segments": len(wals),
        "target_time": C.utc_iso(target_time) if target_time else "latest",
    }


def restore_mariadb(dest_db: str = "zk_pol_restored", target_time: int | None = None) -> dict:
    dest_db = _safe_db_identifier(dest_db)
    rows = list_backups("mariadb-zkpol", as_of=target_time)
    rows = [r for r in rows if r["type"] == "full"]
    if not rows:
        raise RuntimeError("no mariadb full backup found")
    row = rows[0]
    s3 = _new_s3()
    blob = s3.get_object(row["object_key"])
    if C.sha256_bytes(blob) != row["checksum_sha256"]:
        raise RuntimeError("checksum mismatch")
    master = K.load_or_create_master_key(C.MASTER_KEY_PATH)
    sql = K.decrypt(master, blob).decode("utf-8", "replace")
    # Replace database name in CREATE/USE statements so we don't
    # clobber the live database.
    sql = sql.replace(
        f"CREATE DATABASE /*!32312 IF NOT EXISTS*/ `{C.MARIADB_DB}`",
        f"CREATE DATABASE /*!32312 IF NOT EXISTS*/ `{dest_db}`",
    )
    sql = sql.replace(f"USE `{C.MARIADB_DB}`", f"USE `{dest_db}`")

    docker = _require_tool("docker")
    r = subprocess.run(  # noqa: S603 - executable is resolved; destination DB name is validated above.
        [
            docker,
            "exec",
            "-i",
            "-e",
            f"MYSQL_PWD={C.MARIADB_PASSWORD}",
            C.MARIADB_CONTAINER,
            "mariadb",
            "-h",
            "127.0.0.1",
            "-u",
            C.MARIADB_USER,
        ],
        input=sql.encode("utf-8"),
        capture_output=True,
        timeout=300,
    )
    return {
        "source": "mariadb-zkpol",
        "object_key": row["object_key"],
        "created_at": row["created_at"],
        "dest_db": dest_db,
        "rc": r.returncode,
        "stderr": r.stderr.decode("utf-8", "replace")[:400],
    }


def main() -> None:
    p = argparse.ArgumentParser(prog="restore", description=__doc__)
    p.add_argument(
        "--source",
        required=True,
        help="e.g. sqlite-auth.db, postgres-auth, mariadb-zkpol, chain-...",
    )
    p.add_argument("--dest", help="destination path / db name")
    p.add_argument(
        "--target-time", "--as-of", dest="target_time", help="ISO-8601 timestamp; default = latest"
    )
    p.add_argument(
        "--mode",
        default="logical",
        choices=["logical", "physical"],
        help="postgres-only: logical (pg_restore) or physical (PITR)",
    )
    args = p.parse_args()

    tgt = C.parse_iso(args.target_time) if args.target_time else None

    if args.source.startswith("sqlite-"):
        dest = args.dest or _restore_temp_path(f"{args.source}-restored.db")
        out = restore_sqlite(args.source, dest, target_time=tgt)
    elif args.source.startswith("chain-"):
        # download to dest path, decrypt + decompress
        rows = list_backups(args.source, as_of=tgt)
        if not rows:
            raise SystemExit(f"no backups for {args.source}")
        row = rows[0]
        s3 = _new_s3()
        blob = s3.get_object(row["object_key"])
        master = K.load_or_create_master_key(C.MASTER_KEY_PATH)
        raw = gzip.decompress(K.decrypt(master, blob))
        dest = args.dest or _restore_temp_path(f"{args.source}.bin")
        os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
        with open(dest, "wb") as f:
            f.write(raw)
        out = {"source": args.source, "dest": dest, "bytes": len(raw)}
    elif args.source == "postgres-auth" or args.source == "postgres-auth-logical":
        if args.mode == "physical":
            dest = args.dest or _restore_temp_path(f"zkcex-restore-pg-{int(time.time())}")
            out = restore_postgres_physical(dest, target_time=tgt)
        else:
            dest = args.dest or "zkcex_auth_restored"
            out = restore_postgres_logical(target_time=tgt, dest_db=dest)
    elif args.source.startswith("mariadb-"):
        dest = args.dest or "zk_pol_restored"
        out = restore_mariadb(dest_db=dest, target_time=tgt)
    else:
        raise SystemExit(f"unknown source: {args.source}")

    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
