# zkCEX Postgres cluster — operator runbook

This is the procedure for the two-region Postgres cluster that backs
`auth_server` (and, by extension, KYC + session storage). The compose stack here
is a faithful stand-in for the Helm chart's
`region-a primary + region-b warm standby` topology — same WAL streaming,
same physical replication slot, same promotion flow, just on a laptop.

```
   region-a                              region-b
+----------------+   stream WAL       +-------------------+
| zkcex-pg-      |  ---------------> | zkcex-pg-replica  |
| primary :5434  |  slot=replica_a_  | :5435 (standby)   |
| (writable)     |  slot             | hot_standby=on    |
+----------------+                    +-------------------+
        |                                       ^
        | application traffic                   | reads (read-only)
        v                                       |
   auth_server, ...                       (post-failover writes)
```

## Bring the cluster up

```bash
bash deploy/postgres-cluster/up.sh
```

The script (a) starts the primary, (b) imports `tools/auth_schema/postgres.sql`,
(c) starts the replica which bootstraps itself with `pg_basebackup` against
slot `replica_a_slot`, then (d) prints `pg_stat_replication`. If the replica
status row is missing the cluster is not actually replicating — check
`docker logs zkcex-pg-replica`.

## Monitor replication lag

```bash
bash deploy/postgres-cluster/lag.sh
```

Healthy idle output has `send_lag_bytes = flush_lag_bytes = replay_lag_bytes = 0`.
Under sustained write load the three columns grow in order (send → flush →
replay); the difference between `flush_lag_bytes` and `replay_lag_bytes` is
useful — it tells you whether the replica's disk is keeping up with the
network or its CPU/IO is.

Alert thresholds (suggested):

| Metric             | Warn      | Page       |
|--------------------|-----------|------------|
| `replay_lag`       | > 5 s     | > 30 s     |
| `flush_lag_bytes`  | > 16 MiB  | > 256 MiB  |
| slot `wal_status`  | reserved  | lost       |

## Emergency failover

```bash
bash deploy/postgres-cluster/failover.sh
```

This stops the primary, calls `pg_promote(true, 60)` on the replica, and waits
for it to leave recovery. Once the replica is writable, swap the application
DSN:

```
POSTGRES_DSN=postgresql://app:app-password@127.0.0.1:5435/zkcex_auth
```

…and restart `auth_server`. In Kubernetes the operator instead flips the
`leader` label and the `auth-db` Service follows.

## Rebuild the (former) primary as a new replica

After a failover the old primary's WAL has diverged from the new primary's
timeline. The fast way back is `pg_rewind` — it copies only the blocks that
changed since the divergence point rather than re-doing a full `pg_basebackup`.

```bash
# from inside the old-primary container, with the cluster shut down cleanly:
pg_rewind \
  --target-pgdata=/var/lib/postgresql/data \
  --source-server="host=zkcex-pg-replica port=5432 user=replicator dbname=zkcex_auth password=replicator-password" \
  --progress

# then point it at the new primary as a standby:
echo "primary_conninfo = 'host=zkcex-pg-replica port=5432 user=replicator password=replicator-password'" \
  >> /var/lib/postgresql/data/postgresql.auto.conf
echo "primary_slot_name = 'replica_a_slot'" \
  >> /var/lib/postgresql/data/postgresql.auto.conf
touch /var/lib/postgresql/data/standby.signal
```

If `pg_rewind` refuses ("source server must not be in recovery") it usually
means the new primary hasn't done a checkpoint since promotion — run
`CHECKPOINT;` on it and retry.

Prerequisite: `wal_log_hints = on` (or `data_checksums`) must have been set
on the data directory from initdb time. Our compose stack runs without it for
brevity; production must enable it or `pg_rewind` will refuse.

## Upgrade Postgres

The supported path is replica-first switchover:

1. Stop the standby. `docker compose stop postgres-replica`.
2. Run `pg_upgrade` against the standby's data directory using the new binaries,
   or wipe and re-`pg_basebackup` from the (still-old) primary on the new major.
3. Start the upgraded standby and verify it streams.
4. Run `failover.sh` to promote it. Application now talks to the new-version node.
5. Repeat steps 1–3 against the old primary so it becomes the standby.
6. Optionally switch back so region-a is primary again.

Total downtime is the time it takes to do one `pg_promote` plus an application
DSN flip — typically the same RTO that `test_failover.py` measures.

## Slot accumulates WAL ("replication lag is permanent")

If `pg_replication_slots.wal_status` shows `reserved` or `extended` for hours,
something is wrong with the replica — either it's offline or it's too slow
ever to catch up. The slot will keep growing forever until the data
directory fills.

Recovery:

```bash
# On the primary:
docker exec zkcex-pg-primary psql -U app -d zkcex_auth \
  -c "SELECT pg_drop_replication_slot('replica_a_slot');"
# Then rebuild the standby from scratch:
docker compose down -v postgres-replica
docker compose up -d postgres-replica
```

Wiping the slot drops the safety belt — any open standby will fall off
immediately. Only do this once the standby is confirmed unrecoverable.

## Sync vs async replication

| Mode                          | RPO       | Commit latency cost      | Failure model |
|-------------------------------|-----------|--------------------------|---------------|
| `synchronous_commit = off`    | seconds   | ~0                       | replica down does nothing |
| `synchronous_commit = local`  | seconds   | ~0                       | replica down does nothing |
| `synchronous_commit = on`     | 0 bytes   | + RTT to slowest sync replica | with only one sync replica, replica down blocks all writes |
| `synchronous_commit = remote_apply` | 0 bytes, read-after-write safe | + RTT + apply | same blocking caveat |

The compose stack here is async by default, which matches what
`test_failover.py` reports (non-zero RPO is possible under load; with our
write rate and a local network it landed at 0).

For the financial-critical paths in production we want `synchronous_commit = on`
**plus** at least two sync standbys (`synchronous_standby_names = 'ANY 1 (a, b)'`)
so a single standby outage doesn't pause writes. With one sync standby and no
witness, a standby reboot blocks writes for the duration of the reboot —
which is worse than the async failure mode it was supposed to fix.

## Cross-region latency reality

Seoul (ICN) to Singapore (SIN) is around 70 ms one way on commodity transit
(50 ms on a paid private backbone). That is the floor cost of synchronous
commit between the two regions — every transaction commit adds at least
2 × RTT (write + ack), so ~280 ms minimum if both regions are involved.

The choices that matter:

- **`remote_write` instead of `remote_apply`**: ack on fsync, not on apply.
  Saves the apply latency on the standby (often 1–10 ms) and is the right
  setting unless you need read-after-write against the standby.
- **At-least-one sync, prefer-local quorum**: `synchronous_standby_names = 'ANY 1 (region_a_replica, region_b_replica)'`
  with a same-region replica plus the cross-region one means the same-region
  ack usually wins and the cross-region one trails (still RPO=0 if region-a
  loses both nodes, because the region-b one would be the one ack'ing).
- **Witness node**: a tiny third Postgres or a Consul/Etcd quorum prevents
  split-brain when the WAN link goes flaky. Without a witness, both regions
  can promote themselves and you reconcile by hand.

## Automatic failover in production

This compose stack uses a manual `failover.sh`. Production uses one of:

- **Patroni** + Etcd/Consul. The de facto choice; the rolling-release k8s
  operator ("Postgres Operator" / "Spilo") is built on it.
- **pg_auto_failover**. Simpler model, monitor process arbitrates promotion.
  Fits two-node + witness deployments well.
- **repmgr**. Older but battle-tested; less batteries-included, more
  configuration in your own hands.

The Helm chart's "region-a primary + region-b warm standby" maps to a
Patroni cluster where the warm standby has `nofailover: false` but a higher
`priority` value in region-a, so a clean shutdown of region-a hands over to
region-b but a transient network blip does not.

## Integration with auth_server

Point at the primary (read-write):

```bash
AUTH_DB_BACKEND=postgres \
  POSTGRES_DSN="postgresql://app:app-password@127.0.0.1:5434/zkcex_auth" \
  python3 tools/auth_server.py 5501
```

Read-only traffic (e.g. session lookups for a metrics dashboard) can go to
the replica at `:5435`. Anything attempting INSERT/UPDATE on the replica
will hit `cannot execute INSERT in a read-only transaction` — wrap such
queries with retry-on-readonly that re-resolves the leader DSN.
