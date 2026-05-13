"""MariaDB backup driver.

Demo uses ``mysqldump --single-transaction --master-data=2`` for the
full nightly path -- it's contained in the official mariadb image so we
can call it via ``docker exec`` with zero extra setup. In production
this should be ``mariabackup`` for low-overhead physical backups, with
binlog streaming via ``mysqlbinlog --raw --read-from-remote-server``.

Binlog snapshot (hourly):
  list binary logs, find any since the last one we have in MinIO, and
  upload deltas. The first run captures *all* available binlogs.

The mariadb image we run is ``mariadb:11`` (from docker ps), default
auth uses native_password; we read credentials from env (defaults to
the well-known dev creds for zkPoL).
"""

from __future__ import annotations

import json
import shutil
import subprocess
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


def _require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"required backup tool not found on PATH: {name}")
    return path


def _docker_cmd(args: list[str]) -> list[str]:
    return [_require_tool("docker"), *args]


def _mariadb_running() -> bool:
    r = subprocess.run(  # noqa: S603 - executable is resolved before invocation.
        _docker_cmd(["inspect", "-f", "{{.State.Running}}", C.MARIADB_CONTAINER]),
        capture_output=True,
        timeout=10,
    )
    return r.returncode == 0 and r.stdout.strip() == b"true"


def _mysql(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a mysql/mariadb client command inside the container."""
    full = [
        "docker",
        "exec",
        "-e",
        f"MYSQL_PWD={C.MARIADB_PASSWORD}",
        C.MARIADB_CONTAINER,
    ] + args
    return subprocess.run(  # noqa: S603 - executable is resolved before invocation.
        _docker_cmd(full[1:]), capture_output=True, timeout=timeout, check=False
    )


def _binlog_enabled() -> bool:
    """Detect whether binary logging is enabled on the server.

    SHOW VARIABLES LIKE 'log_bin' returns ON / OFF.
    """
    r = _mysql(
        [
            "mariadb",
            "-h",
            "127.0.0.1",
            "-u",
            C.MARIADB_USER,
            "-N",
            "-e",
            "SHOW VARIABLES LIKE 'log_bin'",
        ],
        timeout=10,
    )
    if r.returncode != 0:
        return False
    return b"ON" in r.stdout.upper()


def full_dump() -> dict:
    """mysqldump of zk_pol; encrypt and upload.

    We drop ``--master-data=2`` automatically if binlogs are disabled
    on the server (otherwise mariadb-dump errors out, since master-data
    needs a coordinate from the binlog). The PITR fallback for
    binlog-less servers is just the next full dump.
    """
    if not _mariadb_running():
        raise RuntimeError(f"container {C.MARIADB_CONTAINER} not running")
    source = "mariadb-zkpol"
    conn = C.open_db()
    job_id = C.insert_job(conn, source, "full")
    t0 = time.time()
    try:
        binlog_on = _binlog_enabled()
        cmd = [
            "mariadb-dump",
            "-h",
            "127.0.0.1",
            "-u",
            C.MARIADB_USER,
            "--single-transaction",
            "--routines",
            "--events",
            "--triggers",
            "--databases",
            C.MARIADB_DB,
        ]
        if binlog_on:
            cmd.insert(4, "--master-data=2")
        r = _mysql(cmd, timeout=600)
        if r.returncode != 0:
            # try mysqldump fallback (older images)
            cmd[0] = "mysqldump"
            r = _mysql(cmd, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(
                f"mariadb-dump failed rc={r.returncode}: "
                f"{r.stderr.decode('utf-8','replace')[:400]}"
            )
        dump_bytes = r.stdout

        master = K.load_or_create_master_key(C.MASTER_KEY_PATH)
        ct = K.encrypt(master, dump_bytes)

        date = C.utc_date(t0)
        seq = C.next_sequence_for(conn, source, date)
        object_key = f"{source}/full/{date}/{seq:04d}.sql.enc"
        s3 = _new_s3()
        meta = {
            "source": source,
            "type": "full",
            "orig_bytes": str(len(dump_bytes)),
            "snapshot_ts": str(int(t0)),
        }
        s3.put_object(object_key, ct, metadata=meta)
        checksum = C.sha256_bytes(ct)

        retention = C.RETENTION_SECONDS["mariadb-full"]
        C.record_inventory(
            conn,
            object_key,
            source,
            "full",
            size=len(ct),
            checksum=checksum,
            retention_seconds=retention,
            metadata_json=json.dumps(meta),
        )
        duration_ms = int((time.time() - t0) * 1000)
        C.update_job(
            conn,
            job_id,
            status="completed",
            bytes_uploaded=len(ct),
            duration_ms=duration_ms,
            object_key=object_key,
            checksum_sha256=checksum,
            retention_until=int(time.time()) + retention,
        )
        return {
            "source": source,
            "object_key": object_key,
            "bytes_uploaded": len(ct),
            "duration_ms": duration_ms,
            "status": "completed",
            "checksum": checksum,
        }
    except Exception as e:
        C.update_job(
            conn, job_id, status="failed", error=repr(e), duration_ms=int((time.time() - t0) * 1000)
        )
        raise


def binlog_snapshot() -> list[dict]:
    """List binlogs in the container and upload any we don't already have.
    Returns a list of {file, status} entries."""
    if not _mariadb_running():
        raise RuntimeError(f"container {C.MARIADB_CONTAINER} not running")

    # SHOW BINARY LOGS -> two-column output: Log_name | File_size
    r = _mysql(
        [
            "mariadb",
            "-h",
            "127.0.0.1",
            "-u",
            C.MARIADB_USER,
            "-N",
            "-e",
            "SHOW BINARY LOGS",
        ],
        timeout=30,
    )
    if r.returncode != 0:
        # binlog might be disabled
        return [{"status": "skipped", "reason": r.stderr.decode("utf-8", "replace")[:200]}]
    out: list[dict] = []
    conn = C.open_db()
    s3 = _new_s3()
    master = K.load_or_create_master_key(C.MASTER_KEY_PATH)
    for line in r.stdout.decode().splitlines():
        parts = line.split()
        if not parts:
            continue
        log_name = parts[0]
        # Check inventory: have we uploaded a non-empty copy of this log?
        prev = conn.execute(
            "SELECT object_key, size_bytes FROM backup_inventory "
            "WHERE source='mariadb-zkpol-binlog' AND metadata_json LIKE ?",
            (f'%"log_name": "{log_name}"%',),
        ).fetchone()
        # Pull the binlog file from the container's datadir
        rfile = _mysql(
            [
                "cat",
                f"/var/lib/mysql/{log_name}",
            ],
            timeout=120,
        )
        if rfile.returncode != 0:
            out.append(
                {
                    "file": log_name,
                    "status": "skipped",
                    "reason": rfile.stderr.decode("utf-8", "replace")[:200],
                }
            )
            continue
        raw = rfile.stdout
        if prev and prev["size_bytes"] >= len(K.encrypt(master, b"")) + len(raw):
            # Already have an at-least-as-large copy; skip.
            out.append({"file": log_name, "status": "unchanged"})
            continue
        ct = K.encrypt(master, raw)
        job_id = C.insert_job(conn, "mariadb-zkpol", "binlog")
        t0 = time.time()
        try:
            date = C.utc_date(t0)
            seq = C.next_sequence_for(conn, "mariadb-zkpol-binlog", date)
            object_key = f"mariadb-zkpol/binlog/{date}/{seq:04d}.{log_name}.enc"
            s3.put_object(object_key, ct, metadata={"source": "mariadb-zkpol", "type": "binlog"})
            checksum = C.sha256_bytes(ct)
            retention = C.RETENTION_SECONDS["mariadb-binlog"]
            C.record_inventory(
                conn,
                object_key,
                "mariadb-zkpol-binlog",
                "binlog",
                size=len(ct),
                checksum=checksum,
                retention_seconds=retention,
                metadata_json=json.dumps({"log_name": log_name, "orig_bytes": len(raw)}),
            )
            duration_ms = int((time.time() - t0) * 1000)
            C.update_job(
                conn,
                job_id,
                status="completed",
                bytes_uploaded=len(ct),
                duration_ms=duration_ms,
                object_key=object_key,
                checksum_sha256=checksum,
                retention_until=int(time.time()) + retention,
            )
            out.append(
                {"file": log_name, "status": "uploaded", "object_key": object_key, "bytes": len(ct)}
            )
        except Exception as e:
            C.update_job(conn, job_id, status="failed", error=repr(e))
            out.append({"file": log_name, "status": "failed", "error": repr(e)})
    return out


if __name__ == "__main__":
    import sys

    arg = sys.argv[1] if len(sys.argv) > 1 else "full"
    if arg == "full":
        print(json.dumps(full_dump(), indent=2))
    elif arg == "binlog":
        print(json.dumps(binlog_snapshot(), indent=2))
    else:
        print("usage: mariadb_backup.py [full|binlog]")
        sys.exit(2)
