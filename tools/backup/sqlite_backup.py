"""SQLite backup driver.

Strategy:
  1. VACUUM INTO '/tmp/<name>-<ts>.db'  -- consistent online snapshot
     with no exclusive lock on the source.
  2. gzip the snapshot
  3. AES-CTR encrypt with the master key
  4. Upload to MinIO under sqlite/<basename>/<date>/<seq>.db.gz.enc
  5. Verify by streaming the object back, decrypting, gunzipping, and
     opening it with sqlite3 to make sure ``PRAGMA integrity_check``
     returns "ok".

The snapshot is single-file: sqlite3's ``VACUUM INTO`` writes a fresh
DB without WAL/SHM sidecars, so the upload is one blob.

For non-sqlite chain-state blobs (custody shares, vapid.json, etc.) we
fall back to a simple read-encrypt-upload (also in this module).
"""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
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


def _read_master() -> bytes:
    return K.load_or_create_master_key(C.MASTER_KEY_PATH)


def _verify_sqlite_roundtrip(blob: bytes, master: bytes) -> bool:
    """Decrypt, gunzip, write to a tmp file, run integrity_check."""
    pt = K.decrypt(master, blob)
    raw = gzip.decompress(pt)
    fd, tmp = tempfile.mkstemp(prefix="zkcex-verify-", suffix=".db")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(raw)
        with sqlite3.connect(tmp) as c:
            r = c.execute("PRAGMA integrity_check").fetchone()
            return bool(r and r[0] == "ok")
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def backup_one_sqlite(db_filename: str) -> dict:
    """Backup a single SQLite DB.

    db_filename: just the basename, e.g. "auth.db"
    Returns a dict describing the result.
    """
    src_path = os.path.join(C.LOCAL_DIR, db_filename)
    if not os.path.exists(src_path):
        raise FileNotFoundError(f"source DB not found: {src_path}")

    source = f"sqlite-{db_filename}"
    conn = C.open_db()
    job_id = C.insert_job(conn, source, "full")
    t0 = time.time()

    try:
        # 1. snapshot via VACUUM INTO
        safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in db_filename)
        fd, snap_path = tempfile.mkstemp(prefix=f"zkcex-snap-{safe_name}-", suffix=".db")
        os.close(fd)
        try:
            os.unlink(snap_path)
        except FileNotFoundError:
            pass
        src_conn = sqlite3.connect(src_path, timeout=10.0)
        try:
            src_conn.execute(f"VACUUM INTO '{snap_path}'")
        finally:
            src_conn.close()

        # 2. gzip
        with open(snap_path, "rb") as f:
            raw = f.read()
        gz = gzip.compress(raw, compresslevel=6)

        # 3. encrypt
        master = _read_master()
        ct = K.encrypt(master, gz)

        # 4. upload
        date = C.utc_date(t0)
        seq = C.next_sequence_for(conn, source, date)
        object_key = f"{source}/{date}/{seq:04d}.db.gz.enc"
        s3 = _new_s3()
        meta = {
            "source": source,
            "type": "full",
            "orig_bytes": str(len(raw)),
            "gz_bytes": str(len(gz)),
            "snapshot_ts": str(int(t0)),
        }
        res = s3.put_object(object_key, ct, metadata=meta)
        checksum = C.sha256_bytes(ct)

        # 5. verify (download+decrypt+integrity_check)
        verify_blob = s3.get_object(object_key)
        if C.sha256_bytes(verify_blob) != checksum:
            raise RuntimeError("uploaded object checksum mismatch")
        if not _verify_sqlite_roundtrip(verify_blob, master):
            raise RuntimeError("integrity_check failed after round-trip")

        duration_ms = int((time.time() - t0) * 1000)
        retention = C.RETENTION_SECONDS["sqlite"]
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

        try:
            os.unlink(snap_path)
        except FileNotFoundError:
            pass

        return {
            "source": source,
            "object_key": object_key,
            "bytes_uploaded": len(ct),
            "duration_ms": duration_ms,
            "checksum": checksum,
            "version_id": res.get("version_id"),
            "status": "completed",
        }

    except Exception as e:
        C.update_job(
            conn, job_id, status="failed", error=repr(e), duration_ms=int((time.time() - t0) * 1000)
        )
        raise


def backup_all_sqlite() -> list[dict]:
    """Backup every well-known SQLite DB. Continues on individual errors."""
    out: list[dict] = []
    for name in C.SQLITE_SOURCES:
        path = os.path.join(C.LOCAL_DIR, name)
        if not os.path.exists(path):
            continue
        try:
            out.append(backup_one_sqlite(name))
        except Exception as e:
            out.append({"source": f"sqlite-{name}", "status": "failed", "error": repr(e)})
    return out


def backup_chain_state_file(rel_path: str, retention_key: str = "custody") -> dict:
    """Backup a binary chain-state file (custody shares, signing keys, etc.)."""
    src = os.path.join(C.LOCAL_DIR, rel_path)
    if not os.path.exists(src):
        raise FileNotFoundError(src)

    source = f"chain-{rel_path.replace('/', '_')}"
    conn = C.open_db()
    job_id = C.insert_job(conn, source, "full")
    t0 = time.time()
    try:
        with open(src, "rb") as f:
            raw = f.read()
        gz = gzip.compress(raw, compresslevel=6)
        master = _read_master()
        ct = K.encrypt(master, gz)

        date = C.utc_date(t0)
        seq = C.next_sequence_for(conn, source, date)
        ext = "bin.gz.enc"
        object_key = f"{source}/{date}/{seq:04d}.{ext}"
        s3 = _new_s3()
        meta = {"source": source, "orig_bytes": str(len(raw))}
        s3.put_object(object_key, ct, metadata=meta)
        checksum = C.sha256_bytes(ct)

        retention = C.RETENTION_SECONDS.get(retention_key, 30 * 86400)
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
        }
    except Exception as e:
        C.update_job(
            conn, job_id, status="failed", error=repr(e), duration_ms=int((time.time() - t0) * 1000)
        )
        raise


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        name = sys.argv[1]
        print(json.dumps(backup_one_sqlite(name), indent=2))
    else:
        for r in backup_all_sqlite():
            print(json.dumps(r))
