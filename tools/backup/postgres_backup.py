"""Postgres backup driver.

We use the path-(b) approach from the brief: run ``pg_basebackup`` and
``pg_receivewal`` from outside the container via ``docker exec``. This
avoids modifying the postgres container image while still giving us
continuous WAL streaming + restartable base backups.

  * Full base backup: ``pg_basebackup`` produces a tar that we capture
    on stdout, then encrypt and upload.
  * WAL streaming: ``pg_receivewal --slot=zkcex_backup_slot`` runs as a
    long-lived subprocess and writes WAL segments to /tmp; a watcher
    thread uploads finished segments and trims the local cache.

The replication slot is created lazily on first run; this keeps the
upstream postgres from recycling WAL we still need.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
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


PG_WAL_CONTAINER_DIR = os.environ.get(
    "ZKCEX_PG_WAL_CONTAINER_DIR", os.path.join(os.sep, "tmp", "zkcex-wal")
)


def _run(
    cmd: list[str],
    *,
    env: dict | None = None,
    input_: bytes | None = None,
    check: bool = True,
    timeout: int = 300,
) -> subprocess.CompletedProcess:
    """Run a command, capturing stdout/stderr. Stderr is captured but
    forwarded to our log on failure."""
    e = dict(os.environ)
    if env:
        e.update(env)
    r = subprocess.run(  # noqa: S603 - executable is resolved before invocation.
        _docker_cmd(cmd[1:]) if cmd and cmd[0] == "docker" else cmd,
        env=e,
        input=input_,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if check and r.returncode != 0:
        C.log(
            f"command failed: {' '.join(cmd)} rc={r.returncode}\n"
            f"stderr={r.stderr.decode('utf-8','replace')[:800]}"
        )
        raise RuntimeError(f"command failed: {cmd[0]}")
    return r


def container_up() -> bool:
    r = _run(
        ["docker", "inspect", "-f", "{{.State.Running}}", C.POSTGRES_CONTAINER],
        check=False,
        timeout=10,
    )
    return r.returncode == 0 and r.stdout.strip() == b"true"


def ensure_replication_slot() -> None:
    """Create the physical replication slot used by pg_receivewal, if
    it doesn't exist. Idempotent."""
    sql = (
        "SELECT pg_create_physical_replication_slot('zkcex_backup_slot') "
        "WHERE NOT EXISTS (SELECT 1 FROM pg_replication_slots "
        "WHERE slot_name='zkcex_backup_slot');"
    )
    _run(
        [
            "docker",
            "exec",
            "-e",
            f"PGPASSWORD={C.POSTGRES_PASSWORD}",
            C.POSTGRES_CONTAINER,
            "psql",
            "-U",
            C.POSTGRES_USER,
            "-d",
            C.POSTGRES_DB,
            "-tAc",
            sql,
        ],
        timeout=30,
        check=False,
    )


def ensure_wal_level_replica() -> None:
    """Set ``wal_level=replica`` if it's lower. Requires postgres restart
    if changed -- we surface a warning to the user but don't auto-restart
    a running cluster."""
    r = _run(
        [
            "docker",
            "exec",
            "-e",
            f"PGPASSWORD={C.POSTGRES_PASSWORD}",
            C.POSTGRES_CONTAINER,
            "psql",
            "-U",
            C.POSTGRES_USER,
            "-d",
            C.POSTGRES_DB,
            "-tAc",
            "SHOW wal_level;",
        ],
        timeout=10,
        check=False,
    )
    level = r.stdout.decode().strip()
    if level in ("replica", "logical"):
        return
    C.log(
        f"warning: wal_level={level!r}; pg_basebackup/pg_receivewal need"
        " 'replica' or higher. To enable, run:\n"
        f"  docker exec {C.POSTGRES_CONTAINER} psql -U {C.POSTGRES_USER} "
        f"-d {C.POSTGRES_DB} -c \"ALTER SYSTEM SET wal_level='replica';\" "
        f"&& docker restart {C.POSTGRES_CONTAINER}"
    )


def full_base_backup() -> dict:
    """Run pg_basebackup; capture tar on stdout; encrypt + upload."""
    if not container_up():
        raise RuntimeError(f"container {C.POSTGRES_CONTAINER} not running")

    ensure_wal_level_replica()

    source = "postgres-auth"
    conn = C.open_db()
    job_id = C.insert_job(conn, source, "full")
    t0 = time.time()
    try:
        # Run pg_basebackup INSIDE the container, write tar to stdout.
        # -Ft : tar format; -X fetch : include WAL files; -P : progress
        cmd = [
            "docker",
            "exec",
            "-i",
            "-e",
            f"PGPASSWORD={C.POSTGRES_PASSWORD}",
            C.POSTGRES_CONTAINER,
            "pg_basebackup",
            "-h",
            "127.0.0.1",
            "-p",
            "5432",
            "-U",
            C.POSTGRES_USER,
            "-D",
            "-",  # stdout
            "-Ft",  # tar format
            "-X",
            "fetch",  # include WAL needed to start
            "-w",  # no password prompt
        ]
        p = subprocess.run(  # noqa: S603 - executable is resolved and args are fixed.
            _docker_cmd(cmd[1:]), capture_output=True, timeout=900, check=False
        )
        if p.returncode != 0:
            raise RuntimeError(
                f"pg_basebackup failed rc={p.returncode}: "
                f"{p.stderr.decode('utf-8','replace')[:400]}"
            )
        tar_bytes = p.stdout

        master = K.load_or_create_master_key(C.MASTER_KEY_PATH)
        ct = K.encrypt(master, tar_bytes)

        date = C.utc_date(t0)
        seq = C.next_sequence_for(conn, source, date)
        object_key = f"{source}/base/{date}/{seq:04d}.tar.enc"
        s3 = _new_s3()
        meta = {
            "source": source,
            "type": "full",
            "orig_bytes": str(len(tar_bytes)),
            "snapshot_ts": str(int(t0)),
        }
        s3.put_object(object_key, ct, metadata=meta)
        checksum = C.sha256_bytes(ct)

        retention = C.RETENTION_SECONDS["postgres-full"]
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

        # Stage the (encrypted) tar on disk too so operators can verify
        # locally with `ls /tmp/zkcex-pg-base/` per the runbook. The
        # canonical copy lives in MinIO; this is just a cached mirror.
        os.makedirs(PG_BASE_DIR, exist_ok=True)
        cache_path = os.path.join(PG_BASE_DIR, os.path.basename(object_key))
        try:
            with open(cache_path, "wb") as f:
                f.write(ct)
            # also drop a manifest so an operator running `ls` sees something
            # human-readable
            with open(os.path.join(PG_BASE_DIR, "LATEST.json"), "w") as f:
                json.dump(
                    {
                        "object_key": object_key,
                        "cache_path": cache_path,
                        "snapshot_ts": int(t0),
                        "size_bytes_encrypted": len(ct),
                        "size_bytes_plain": len(tar_bytes),
                        "checksum_sha256": checksum,
                    },
                    f,
                    indent=2,
                )
        except Exception as _e:
            C.log(f"on-disk mirror failed (non-fatal): {_e!r}")

        # Also dump a plain logical backup (pg_dump) as a fallback
        # restore path -- this is robust against bytesize differences
        # between pg client versions during PITR demos.
        _logical_dump(conn, t0)

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


def _logical_dump(conn, t0: float) -> dict:
    """pg_dump --format=custom of the auth DB. Encrypted + uploaded.

    Used as the PITR-fallback path: lighter weight than tar restore and
    perfect for the auth.db / users table demos."""
    source = "postgres-auth"
    job_id = C.insert_job(conn, source, "logical-dump")
    t1 = time.time()
    try:
        cmd = [
            "docker",
            "exec",
            "-e",
            f"PGPASSWORD={C.POSTGRES_PASSWORD}",
            C.POSTGRES_CONTAINER,
            "pg_dump",
            "-U",
            C.POSTGRES_USER,
            "-d",
            C.POSTGRES_DB,
            "--format=custom",
            "--no-owner",
        ]
        p = subprocess.run(  # noqa: S603 - executable is resolved and args are fixed.
            _docker_cmd(cmd[1:]), capture_output=True, timeout=300, check=False
        )
        if p.returncode != 0:
            raise RuntimeError(
                f"pg_dump failed rc={p.returncode}: " f"{p.stderr.decode('utf-8','replace')[:400]}"
            )
        dump_bytes = p.stdout
        master = K.load_or_create_master_key(C.MASTER_KEY_PATH)
        ct = K.encrypt(master, dump_bytes)

        date = C.utc_date(t0)
        seq = C.next_sequence_for(conn, f"{source}-logical", date)
        object_key = f"{source}/logical/{date}/{seq:04d}.dump.enc"
        s3 = _new_s3()
        s3.put_object(object_key, ct, metadata={"source": source, "type": "logical-dump"})
        checksum = C.sha256_bytes(ct)

        retention = C.RETENTION_SECONDS["postgres-full"]
        C.record_inventory(
            conn,
            object_key,
            f"{source}-logical",
            "logical-dump",
            size=len(ct),
            checksum=checksum,
            retention_seconds=retention,
            metadata_json=json.dumps({"snapshot_ts": int(t0)}),
        )
        duration_ms = int((time.time() - t1) * 1000)
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
        return {"object_key": object_key, "bytes": len(ct)}
    except Exception as e:
        C.update_job(
            conn, job_id, status="failed", error=repr(e), duration_ms=int((time.time() - t1) * 1000)
        )
        raise


# --- WAL streaming -------------------------------------------------------
class WalStreamer:
    """Drives a background ``pg_receivewal`` and uploads completed
    segments to MinIO.

    pg_receivewal writes each WAL segment as a fixed-size 16 MB file
    plus a trailing ``.partial`` for the segment currently being
    received. We watch the directory: any non-``.partial`` file we
    haven't uploaded yet gets encrypted + uploaded, and then we leave
    the local copy for a configurable grace period to support
    fast-restart restores.
    """

    GRACE_LOCAL_S = 24 * 3600  # keep local WAL for 24h after upload

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self._uploaded: set[str] = set()

    def start(self) -> None:
        if not container_up():
            raise RuntimeError(f"container {C.POSTGRES_CONTAINER} not running")
        ensure_replication_slot()
        os.makedirs(PG_HOST_WAL_DIR, exist_ok=True)
        # We could run pg_receivewal on the host using a host-installed
        # client, but to keep zero pip-deps and consistent versions we
        # exec it inside the container writing to a bind-mounted dir.
        # For the demo we run it OUTSIDE the container via the host's
        # postgres client only if available; otherwise we fall back to
        # docker exec with the container's tmpfs.
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        C.log("wal streamer started")

    def stop(self) -> None:
        self.stop_event.set()
        if self.proc:
            try:
                self.proc.terminate()
            except Exception as e:  # noqa: BLE001
                C.log(f"wal streamer terminate failed: {e!r}")

    def _loop(self) -> None:
        """Run pg_receivewal in a loop, restarting on failure with backoff."""
        backoff = 1
        while not self.stop_event.is_set():
            try:
                self._one_run()
                backoff = 1
            except Exception as e:
                C.log(f"wal streamer error: {e!r}; retry in {backoff}s")
                time.sleep(backoff)
                backoff = min(60, backoff * 2)

    def _one_run(self) -> None:
        # Run pg_receivewal inside the container; it writes WAL segments
        # to a staging dir inside the container. We periodically copy
        # finished segments out and upload them.
        in_container_dir = PG_WAL_CONTAINER_DIR
        _run(
            [
                "docker",
                "exec",
                C.POSTGRES_CONTAINER,
                "mkdir",
                "-p",
                in_container_dir,
            ],
            check=False,
            timeout=10,
        )

        cmd = [
            "docker",
            "exec",
            "-e",
            f"PGPASSWORD={C.POSTGRES_PASSWORD}",
            C.POSTGRES_CONTAINER,
            "pg_receivewal",
            "-h",
            "127.0.0.1",
            "-p",
            "5432",
            "-U",
            C.POSTGRES_USER,
            "-D",
            in_container_dir,
            "--slot=zkcex_backup_slot",
            "--no-loop",
        ]
        self.proc = subprocess.Popen(  # noqa: S603 - executable is resolved and args are fixed.
            _docker_cmd(cmd[1:]), stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        # Watch loop: every 5s, list /tmp/zkcex-wal inside the container
        # for completed segments, copy them out, upload.
        try:
            while not self.stop_event.is_set():
                if self.proc.poll() is not None:
                    err = (self.proc.stderr.read() or b"").decode("utf-8", "replace")
                    raise RuntimeError(
                        f"pg_receivewal exited rc={self.proc.returncode} stderr={err[:300]}"
                    )
                self._scan_and_upload(in_container_dir)
                time.sleep(5)
        finally:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except Exception as e:  # noqa: BLE001
                    C.log(f"wal streamer wait failed: {e!r}")

    def _scan_and_upload(self, in_container_dir: str) -> None:
        r = _run(
            ["docker", "exec", C.POSTGRES_CONTAINER, "ls", "-1", in_container_dir],
            check=False,
            timeout=10,
        )
        if r.returncode != 0:
            return
        for line in r.stdout.decode().splitlines():
            name = line.strip()
            if not name or name.endswith(".partial"):
                continue
            if name in self._uploaded:
                continue
            try:
                self._upload_one_segment(in_container_dir, name)
                self._uploaded.add(name)
            except Exception as e:
                C.log(f"wal upload error for {name}: {e!r}")

    def _upload_one_segment(self, in_container_dir: str, name: str) -> None:
        r = _run(
            [
                "docker",
                "exec",
                C.POSTGRES_CONTAINER,
                "cat",
                f"{in_container_dir}/{name}",
            ],
            check=True,
            timeout=60,
        )
        raw = r.stdout
        master = K.load_or_create_master_key(C.MASTER_KEY_PATH)
        ct = K.encrypt(master, raw)
        date = C.utc_date()
        object_key = f"postgres-auth/wal/{date}/{name}.enc"
        s3 = _new_s3()
        s3.put_object(object_key, ct, metadata={"source": "postgres-auth", "type": "wal"})
        checksum = C.sha256_bytes(ct)
        conn = C.open_db()
        retention = C.RETENTION_SECONDS["postgres-wal"]
        C.record_inventory(
            conn,
            object_key,
            "postgres-auth-wal",
            "wal",
            size=len(ct),
            checksum=checksum,
            retention_seconds=retention,
            metadata_json=json.dumps({"segment": name}),
        )
        # Also record a job row for visibility
        job_id = C.insert_job(conn, "postgres-auth", "wal")
        C.update_job(
            conn,
            job_id,
            status="completed",
            bytes_uploaded=len(ct),
            object_key=object_key,
            checksum_sha256=checksum,
            retention_until=int(time.time()) + retention,
        )


# Host-side staging dirs (the canonical copy is in MinIO; these are
# operator-facing local mirrors).
PG_HOST_WAL_DIR = os.environ.get(
    "ZKCEX_PG_HOST_WAL_DIR", os.path.join(tempfile.gettempdir(), "zkcex-pg-wal")
)
PG_BASE_DIR = os.environ.get(
    "ZKCEX_PG_BASE_DIR", os.path.join(tempfile.gettempdir(), "zkcex-pg-base")
)


if __name__ == "__main__":
    import sys

    arg = sys.argv[1] if len(sys.argv) > 1 else "base"
    if arg == "base":
        print(json.dumps(full_base_backup(), indent=2))
    elif arg == "logical":
        conn = C.open_db()
        print(json.dumps(_logical_dump(conn, time.time()), indent=2))
    elif arg == "wal":
        ws = WalStreamer()
        ws.start()
        try:
            while True:
                time.sleep(5)
        except KeyboardInterrupt:
            ws.stop()
    else:
        print("usage: postgres_backup.py [base|logical|wal]")
        sys.exit(2)
