#!/usr/bin/env python3
"""Auto-failover test for the Patroni-managed cluster.

The shape of this test is intentionally different from the manual
test_failover.py in the sibling postgres-cluster/ stack: instead of
"pre-failover N rows; cut over; post-failover M rows", we run a continuous
writer through the HAProxy RW endpoint at :5443 and kill the current leader
mid-stream. The interesting numbers are:

  * write_unavailability_ms — wall time between the last successful commit
    before the kill and the first successful commit after the new leader is
    routed.
  * writes_failed — INSERTs that hit a connection error or a "read-only
    transaction" error during the failover window. We retry them once a new
    leader appears; they will count toward the unavailability window but
    will eventually succeed.
  * rto_observed_ms — same thing measured as the time between sending the
    SIGKILL to the leader and the first new successful commit. This is the
    user-observed RTO with auto-failover.
  * data_loss_rows — rows we got a commit-ack on before the kill but that
    are NOT visible on the new leader. With async streaming this can be
    non-zero; with `synchronous_mode: true` in Patroni it is guaranteed 0.

Run continuously: writes a row every 100ms via the HAProxy RW endpoint.
Mid-test we identify the current leader via the Patroni REST API and
`docker kill -s KILL` it. We then watch for the next successful write.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request

import pg8000.dbapi as pg

HERE = os.path.dirname(os.path.abspath(__file__))

# HAProxy entry points (stable; survive failover)
RW_DSN = dict(host="127.0.0.1", port=5443, database="zkcex_auth",
              user="app", password="app-password")
RO_DSN = dict(host="127.0.0.1", port=5444, database="zkcex_auth",
              user="app", password="app-password")

# Direct Patroni REST endpoints — we use these only to *observe* the cluster,
# not to drive the failover. The whole point of this test is that we don't
# need to drive promotion ourselves.
NODE_REST = {
    "node-a": ("zkcex-pg-patroni-a", "http://127.0.0.1:8008"),
    "node-b": ("zkcex-pg-patroni-b", "http://127.0.0.1:8009"),
    "node-c": ("zkcex-pg-patroni-c", "http://127.0.0.1:8010"),
}

TEST_TABLE = "auto_failover_writes"


def now_ms() -> float:
    return time.monotonic() * 1000.0


def http_get(url: str, timeout: float = 2.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.getcode(), r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:
        return None, b""


def find_leader() -> tuple[str, str] | None:
    """Return (node_name, container_name) of the current Patroni leader."""
    for name, (container, base) in NODE_REST.items():
        code, _ = http_get(f"{base}/leader")
        if code == 200:
            return name, container
    return None


def cluster_members() -> list[dict]:
    """Best-effort dump of /cluster from whichever node responds."""
    for _name, (_c, base) in NODE_REST.items():
        code, body = http_get(f"{base}/cluster")
        if code == 200:
            try:
                return json.loads(body).get("members", [])
            except Exception:
                pass
    return []


def connect_rw(retries: int = 1, delay: float = 0.05):
    """Connect to HAProxy RW endpoint. We deliberately don't retry forever —
    the caller controls the retry behaviour so we can measure unavailability."""
    last = None
    for _ in range(max(1, retries)):
        try:
            return pg.connect(**RW_DSN)
        except Exception as e:
            last = e
            time.sleep(delay)
    raise RuntimeError(f"could not connect to RW endpoint: {last}")


def main() -> int:
    print("==== zkCEX Patroni auto-failover test ====")
    print("")

    # Wait for HAProxy RW endpoint to be alive (the cluster might still be
    # bootstrapping when this script is run).
    print("[setup] waiting for HAProxy RW endpoint at :5443 ...")
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            c = pg.connect(**RW_DSN)
            c.close()
            break
        except Exception:
            time.sleep(0.5)
    else:
        print("[setup] HAProxy RW endpoint did not come up in 60s")
        return 2
    print("[setup] HAProxy RW endpoint is up")

    initial_leader = find_leader()
    if initial_leader is None:
        print("[setup] could not find a leader via Patroni REST API")
        return 2
    print(f"[setup] initial leader: {initial_leader[0]} (container {initial_leader[1]})")

    members_before = cluster_members()
    print(f"[setup] cluster has {len(members_before)} members")
    for m in members_before:
        print(f"        - {m.get('name')}: role={m.get('role')} state={m.get('state')}")

    # ---- Setup table ----
    conn = connect_rw()
    cur = conn.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {TEST_TABLE}")
    cur.execute(f"""
        CREATE TABLE {TEST_TABLE} (
            seq BIGINT PRIMARY KEY,
            written_at_ms DOUBLE PRECISION NOT NULL,
            written_by TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    print(f"[setup] created table {TEST_TABLE}")

    # ---- Continuous writer ----
    # We will write seq=0,1,2,... at 100 ms cadence for up to 40 seconds.
    # After 5 seconds we kill the leader. Then we watch for the next commit.
    acked_seqs: list[int] = []
    failed_seqs: list[int] = []
    last_ack_ms_before_kill: float | None = None
    first_ack_ms_after_kill: float | None = None
    kill_sent_ms: float | None = None
    new_leader_seen_ms: float | None = None
    new_leader_name: str | None = None
    killed_container: str | None = None

    total_writes_target = 400  # 400 * 100ms = 40s
    kill_after_writes = 50     # 5s into the run
    seq = 0
    writer_conn = None

    t_start = now_ms()
    while seq < total_writes_target:
        if seq == kill_after_writes and kill_sent_ms is None:
            # Time to kill the current leader
            leader = find_leader()
            if leader is None:
                print("[kill] no leader currently; aborting")
                break
            new_leader_name = None  # reset for the post-kill detection
            killed_container = leader[1]
            print(f"")
            print(f"[kill] sending SIGKILL to {leader[0]} (container {killed_container})")
            kill_sent_ms = now_ms()
            subprocess.run(
                ["docker", "kill", "-s", "KILL", killed_container],
                capture_output=True, text=True,
            )

        loop_start = now_ms()
        committed = False
        try:
            if writer_conn is None:
                writer_conn = connect_rw(retries=1)
            wcur = writer_conn.cursor()
            wcur.execute(
                f"INSERT INTO {TEST_TABLE} (seq, written_at_ms, written_by) "
                f"VALUES (%s, %s, %s)",
                (seq, now_ms(), "writer"),
            )
            writer_conn.commit()
            acked_seqs.append(seq)
            if kill_sent_ms is None:
                last_ack_ms_before_kill = now_ms()
            else:
                if first_ack_ms_after_kill is None:
                    first_ack_ms_after_kill = now_ms()
                    # Also record which node is now leader
                    nl = find_leader()
                    if nl is not None:
                        new_leader_name = nl[0]
                        new_leader_seen_ms = now_ms()
                        print(f"[recovery] first ack after kill at seq={seq}; "
                              f"new leader = {new_leader_name}")
            committed = True
        except Exception as e:
            failed_seqs.append(seq)
            # Tear down the connection — it's almost certainly dead now
            try:
                if writer_conn is not None:
                    writer_conn.close()
            except Exception:
                pass
            writer_conn = None
            if kill_sent_ms is not None and seq % 5 == 0:
                # Surface what's happening during the failover window
                print(f"[failing] seq={seq} err={type(e).__name__}: {str(e)[:80]}")

        seq += 1

        # Sleep until the next 100ms tick
        elapsed = now_ms() - loop_start
        if elapsed < 100:
            time.sleep((100 - elapsed) / 1000.0)

        # Stop once we've had at least 20 acks after the kill — gives us a
        # stable post-failover sample but bounds the test length.
        if (kill_sent_ms is not None
                and first_ack_ms_after_kill is not None
                and len([s for s in acked_seqs if s > kill_after_writes]) >= 20):
            break

    t_end = now_ms()
    try:
        if writer_conn is not None:
            writer_conn.close()
    except Exception:
        pass

    # ---- Compute metrics ----
    print("")
    print("==== RESULTS ====")
    print(f"Total writes attempted:    {seq}")
    print(f"Writes acked:              {len(acked_seqs)}")
    print(f"Writes failed (in window): {len(failed_seqs)}")

    if kill_sent_ms is None:
        print("Kill never happened — test inconclusive.")
        return 3

    if first_ack_ms_after_kill is None:
        print("No successful write after kill — failover did NOT complete.")
        print(f"Writes failed for {(t_end - kill_sent_ms)/1000:.1f}s straight.")
        return 4

    rto_ms = first_ack_ms_after_kill - kill_sent_ms
    if last_ack_ms_before_kill is not None:
        write_unavailability_ms = first_ack_ms_after_kill - last_ack_ms_before_kill
    else:
        write_unavailability_ms = rto_ms

    print(f"Killed container:          {killed_container}")
    print(f"New leader:                {new_leader_name}")
    print(f"RTO (kill -> first ack):   {rto_ms/1000:.2f} s")
    print(f"Write unavailability:      {write_unavailability_ms/1000:.2f} s")

    # ---- Data-loss check: every acked seq must be visible on the new leader ----
    try:
        verify = connect_rw(retries=10, delay=0.5)
        vcur = verify.cursor()
        vcur.execute(f"SELECT COUNT(*) FROM {TEST_TABLE}")
        visible = vcur.fetchone()[0]
        vcur.execute(f"SELECT seq FROM {TEST_TABLE} ORDER BY seq")
        visible_seqs = set(r[0] for r in vcur.fetchall())
        lost = [s for s in acked_seqs if s not in visible_seqs]
        verify.close()
    except Exception as e:
        print(f"[verify] could not verify on new leader: {e}")
        return 5

    print(f"Rows visible on new leader: {visible}")
    print(f"Acked rows missing (data loss): {len(lost)}")

    print("")
    print("==== PASS/FAIL ====")
    rto_pass = rto_ms <= 30000        # 30s budget (sibling manual was 0.71s)
    no_loss = len(lost) == 0          # async mode: best-effort; expect 0 in idle
    progress = len([s for s in acked_seqs if s > kill_after_writes]) > 0
    print(f"RTO under 30s:             {'PASS' if rto_pass else 'FAIL'}")
    print(f"No data loss (async):      {'PASS' if no_loss else 'FAIL'} "
          f"({len(lost)} rows)")
    print(f"Writes resumed:            {'PASS' if progress else 'FAIL'}")

    print("")
    print("Note: the manual failover.sh sibling clocks 0.71s RTO because it")
    print("knows exactly who to promote and skips election. Patroni's etcd")
    print("TTL (default 30s loop_wait=10s, retry_timeout=10s, ttl=30s) is")
    print("the floor here — tune it down in production if you can afford the")
    print("more aggressive leader-loss false-positive rate.")

    return 0 if (rto_pass and no_loss and progress) else 6


def cleanup() -> None:
    """Drop the test table whichever node is now leader."""
    try:
        c = pg.connect(**RW_DSN)
        cur = c.cursor()
        cur.execute(f"DROP TABLE IF EXISTS {TEST_TABLE}")
        c.commit()
        c.close()
    except Exception:
        pass


if __name__ == "__main__":
    rc = 99
    try:
        rc = main()
    except Exception:
        traceback.print_exc()
        rc = 1
    finally:
        cleanup()
    sys.exit(rc)
