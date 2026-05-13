"""Disaster-recovery drill.

End-to-end proof that the backup + restore loop is healthy. Steps:

  1. Snapshot the current ``auth.db`` row count and a known user.
  2. Trigger a full backup of ``auth.db``.
  3. Mutate the live ``auth.db`` (add a probe user).
  4. Restore the backup to ``/tmp/auth-restored.db``.
  5. Assert the probe user is NOT in the restored DB.
  6. Assert a pre-existing user IS in the restored DB.
  7. Print PASS / FAIL with timing per step.

Runs in <60 seconds against a typical demo dataset.

It deliberately operates on a *copy* of the live auth.db (a tmpfs
clone), so this drill is safe to run continuously without polluting
the real DB. The clone is named ``auth.db`` so that the rest of the
backup pipeline -- which assumes that filename -- finds it.

We do this by:
  - copying ``tools/.local/auth.db`` -> ``/tmp/dr-drill-<ts>/auth.db``
  - pointing our SQLite-backup path at ``/tmp/dr-drill-<ts>``
    via the ``DR_DRILL_LOCAL_DIR`` env override
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback

if __package__ in (None, ""):
    import os
    import sys

    HERE = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.dirname(HERE))
    from backup import common as C
    from backup import restore as R
    from backup import sqlite_backup as SB
else:
    from . import common as C
    from . import restore as R
    from . import sqlite_backup as SB


PROBE_EMAIL_PREFIX = "dr-drill-probe-"


def _step(name: str, fn, results: list[dict]) -> object:
    t0 = time.time()
    try:
        v = fn()
        elapsed = (time.time() - t0) * 1000
        results.append({"step": name, "status": "ok", "ms": round(elapsed, 1)})
        return v
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        results.append(
            {
                "step": name,
                "status": "fail",
                "ms": round(elapsed, 1),
                "error": repr(e),
                "traceback": traceback.format_exc()[-800:],
            }
        )
        raise


def run() -> dict:
    results: list[dict] = []
    drill_id = int(time.time())
    workdir = tempfile.mkdtemp(prefix=f"zkcex-dr-{drill_id}-")
    overall_t0 = time.time()
    try:
        # ---- 1) snapshot live state ----------------------------------
        def snapshot_live():
            live = os.path.join(C.LOCAL_DIR, "auth.db")
            if not os.path.exists(live):
                raise RuntimeError("live auth.db not found; nothing to drill")
            with sqlite3.connect(live, timeout=5) as c:
                # Use safe online-copy semantics: open with deferred TX
                count = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                row = c.execute(
                    "SELECT id, email FROM users WHERE email NOT LIKE ? " "ORDER BY id ASC LIMIT 1",
                    (f"{PROBE_EMAIL_PREFIX}%",),
                ).fetchone()
            known_user = {"id": row[0], "email": row[1]} if row else None
            return {"row_count": count, "known_user": known_user, "live_path": live}

        live_snapshot = _step("snapshot_live", snapshot_live, results)

        # ---- 2) clone -> sandbox + take a backup of the clone -------
        def clone_and_backup():
            sandbox = os.path.join(workdir, ".local")
            os.makedirs(sandbox, exist_ok=True)
            shutil.copyfile(live_snapshot["live_path"], os.path.join(sandbox, "auth.db"))
            # also copy wal/shm so it's a real clone
            for suf in ("-wal", "-shm"):
                src = live_snapshot["live_path"] + suf
                if os.path.exists(src):
                    shutil.copyfile(src, os.path.join(sandbox, "auth.db" + suf))
            # Redirect our LOCAL_DIR to the sandbox for the duration of
            # the backup. We import the module locally so we can mutate
            # its module-level constants.
            C._original_LOCAL_DIR = C.LOCAL_DIR  # type: ignore[attr-defined]
            C.LOCAL_DIR = sandbox  # type: ignore[misc]
            res = SB.backup_one_sqlite("auth.db")
            return res

        backup_res = _step("clone_and_backup", clone_and_backup, results)

        # ---- 3) mutate the clone (add a probe user) -----------------
        probe_email = f"{PROBE_EMAIL_PREFIX}{drill_id}@example.invalid"

        def mutate_clone():
            sandbox_auth = os.path.join(workdir, ".local", "auth.db")
            with sqlite3.connect(sandbox_auth, timeout=5) as c:
                # Schema lookup -- insert a probe row that fills every
                # NOT NULL column (other than the autoincrement PK) with
                # a placeholder. We don't care about referential or
                # semantic correctness; the drill only checks presence.
                info = c.execute("PRAGMA table_info(users)").fetchall()
                # info row: (cid, name, type, notnull, dflt_value, pk)
                vals = {}
                for _cid, name, typ, notnull, dflt, pk in info:
                    if pk:
                        continue  # let PK autoinc
                    if name == "email":
                        vals[name] = probe_email
                    elif notnull and dflt is None:
                        t = (typ or "").upper()
                        if "INT" in t:
                            vals[name] = 0
                        elif "BLOB" in t:
                            vals[name] = b"\x00" * 32
                        elif "REAL" in t:
                            vals[name] = 0.0
                        else:
                            vals[name] = "dr-drill-placeholder"
                # Make sure email is always set
                vals.setdefault("email", probe_email)
                cols = list(vals.keys())
                placeholders = ",".join("?" for _ in cols)
                c.execute(
                    f"INSERT INTO users({','.join(cols)}) VALUES({placeholders})",
                    [vals[k] for k in cols],
                )
                c.commit()
                count_after = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            return {"probe_email": probe_email, "count_after": count_after}

        _step("mutate_clone", mutate_clone, results)

        # ---- 4) restore the backup to a fresh location --------------
        def restore_backup():
            dest = os.path.join(workdir, "auth-restored.db")
            return R.restore_sqlite("sqlite-auth.db", dest)

        restored = _step("restore_backup", restore_backup, results)

        # ---- 5/6) verify the probe is absent + known user present ----
        def verify():
            with sqlite3.connect(restored["dest"], timeout=5) as c:
                count_restored = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                probe_in_restored = (
                    c.execute("SELECT 1 FROM users WHERE email=?", (probe_email,)).fetchone()
                    is not None
                )
                known_in_restored = True
                if live_snapshot.get("known_user"):
                    known_in_restored = (
                        c.execute(
                            "SELECT 1 FROM users WHERE email=?",
                            (live_snapshot["known_user"]["email"],),
                        ).fetchone()
                        is not None
                    )
            return {
                "count_restored": count_restored,
                "probe_in_restored": probe_in_restored,
                "known_in_restored": known_in_restored,
                "count_live_at_start": live_snapshot["row_count"],
            }

        verify_out = _step("verify", verify, results)

        # ---- assertions ---------------------------------------------
        assertions = []
        # Probe was added AFTER backup -- must not be in the restored DB
        a1_ok = not verify_out["probe_in_restored"]
        assertions.append({"name": "probe_user_not_in_restored", "ok": a1_ok})
        # Known user must be in the restored DB
        a2_ok = verify_out["known_in_restored"]
        assertions.append({"name": "known_user_in_restored", "ok": a2_ok})
        # Restored count should equal the original count (we copied
        # before any mutation in the clone path).
        a3_ok = verify_out["count_restored"] == verify_out["count_live_at_start"]
        assertions.append(
            {
                "name": "restored_count_matches_pre_mutation",
                "ok": a3_ok,
                "expected": verify_out["count_live_at_start"],
                "actual": verify_out["count_restored"],
            }
        )
        all_ok = all(a["ok"] for a in assertions)

        overall_ms = round((time.time() - overall_t0) * 1000, 1)
        return {
            "result": "PASS" if all_ok else "FAIL",
            "total_ms": overall_ms,
            "steps": results,
            "backup": backup_res,
            "restored": restored,
            "verify": verify_out,
            "assertions": assertions,
        }
    except Exception as e:
        overall_ms = round((time.time() - overall_t0) * 1000, 1)
        return {
            "result": "FAIL",
            "total_ms": overall_ms,
            "steps": results,
            "error": repr(e),
            "traceback": traceback.format_exc(),
        }
    finally:
        # Restore LOCAL_DIR
        if hasattr(C, "_original_LOCAL_DIR"):
            C.LOCAL_DIR = C._original_LOCAL_DIR  # type: ignore[misc]
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            C.log(f"drill workdir cleanup failed: {exc!r}")


if __name__ == "__main__":
    out = run()
    print(json.dumps(out, indent=2, default=str))
    sys.exit(0 if out.get("result") == "PASS" else 1)
