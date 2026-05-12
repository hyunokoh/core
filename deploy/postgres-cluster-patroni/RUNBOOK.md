# zkCEX Patroni Postgres cluster — operator runbook

This is the auto-failover successor to `../postgres-cluster/`. Same topology
shape (Postgres replicas streaming WAL), but now Patroni — running inside each
node via the Spilo image — owns the leader-election problem. Apps connect to
HAProxy, which always points at whichever node Patroni currently considers the
leader, so a node death no longer needs an operator to flip a DSN.

```
                       +--------------------+
                       |       etcd         |   distributed consensus store
                       |  zkcex-pg-etcd     |   (the source of truth for who
                       |       :2379        |    holds the leader lock)
                       +---------+----------+
                                 ^
                                 | Patroni heartbeats / leader-lock TTL
                                 |
       +-------------------------+-------------------------+
       |                         |                         |
+------v-------+         +-------v-----+           +-------v-----+
|  node-a      |  WAL    |   node-b    |   WAL     |   node-c    |
|  Patroni +   |<--------|  Patroni +  |<----------| Patroni +   |
|  Postgres 16 |         | Postgres 16 |           | Postgres 16 |
|  :5440 / :8008         | :5441 / :8009           | :5442 / :8010
+------+-------+         +-------+-----+           +-------+-----+
       \_____________________________________________________/
                                 |
                       +---------v----------+
                       |      HAProxy       |   :5443 RW (-> /leader)
                       |  zkcex-pg-haproxy  |   :5444 RO (-> /replica)
                       +---------+----------+   :7000 stats
                                 |
                       auth_server, etc.
```

## Bring the cluster up

```bash
bash deploy/postgres-cluster-patroni/up.sh
```

The script:

1. starts etcd, waits for `endpoint health`,
2. starts `patroni-node-a` and waits for it to win the first leader election
   (Patroni's REST `GET /leader` returns 200 on the leader, 503 elsewhere),
3. creates the `zkcex_auth` database and imports `tools/auth_schema/postgres.sql`
   on the (current) leader,
4. starts `node-b`, `node-c`, and HAProxy,
5. waits for both new nodes to start streaming (their `/replica` endpoints
   return 200), then prints `patronictl list`.

## View cluster state

```bash
docker exec zkcex-pg-patroni-a patronictl list
```

Healthy output is three rows, exactly one tagged `Leader`, two `Replica`, all
showing `running`. The `Lag in MB` column should be `0` for the leader and
small (single-digit MB on this idle stack) for the replicas.

You can also peek at the REST API directly:

```bash
curl -s http://127.0.0.1:8008/cluster | jq .
curl -s http://127.0.0.1:8008/leader   # 200 only on the leader
curl -s http://127.0.0.1:8008/replica  # 200 only on a streaming replica
curl -s http://127.0.0.1:8008/patroni  # full local state + config
```

## Manual failover (planned switchover)

A *planned* switchover is the safe operation: it picks the candidate, waits
for it to be in sync, blocks new writes on the old leader, fences the old
leader, and promotes the candidate. There is a brief write-blocking window
but no data loss.

```bash
docker exec zkcex-pg-patroni-a patronictl failover zkcex-pg --candidate node-b
```

For an *immediate* failover (e.g. the leader's host is dying and you want to
race ahead of the TTL):

```bash
docker exec zkcex-pg-patroni-a patronictl failover zkcex-pg --force
```

Use `switchover` instead of `failover` when the old leader is still healthy
and you just want to rotate roles (e.g. before maintenance):

```bash
docker exec zkcex-pg-patroni-a patronictl switchover zkcex-pg --candidate node-c
```

## Auto-failover (no operator)

This is the headline scenario. Kill the leader (any way: `docker kill`, network
isolation, full host loss), and Patroni's surviving nodes notice the leader-lock
TTL has expired in etcd, race to acquire it, and the winner promotes itself.
HAProxy's `httpchk OPTIONS /leader` health check sees the new leader respond
200 and starts routing writes there. No DSN change needed on the application
side.

```bash
python3 deploy/postgres-cluster-patroni/failover_test.py
```

The test writes one row every 100 ms to `:5443`, kills the current leader
mid-stream, and reports:

- Time from `docker kill` to first successful new commit (the user-observed RTO).
- Number of writes that hit a failure during the window.
- Number of acked rows missing on the new leader (data loss; 0 in async with
  no in-flight work, guaranteed 0 with `synchronous_mode: true`).

## Tune the failover speed

Patroni's defaults are conservative — the leader lock has a 30 s TTL, the
control loop runs every 10 s, and HTTP retries get 10 s. That means a
worst-case observed RTO is in the 20–30 s range on this stack, vs. the
sibling manual failover at 0.71 s (the manual script knows who to promote
and skips election entirely).

To shrink the window, after the cluster is up:

```bash
docker exec -it zkcex-pg-patroni-a patronictl edit-config zkcex-pg
```

The relevant knobs:

```yaml
ttl: 15            # leader lock TTL; must be > loop_wait + retry_timeout
loop_wait: 3       # control-loop interval
retry_timeout: 5   # HTTP retry budget per loop
```

With `ttl=15, loop_wait=3, retry_timeout=5` you can land observed RTO around
8–12 s in the lab; production tradeoff is more false-positive promotions if
the network briefly stutters.

## Rolling minor-version upgrade

Patroni handles this for you. Update the image tag in `docker-compose.yml`,
then:

```bash
# 1) replicas first (no leader change yet)
docker compose up -d patroni-node-b
docker compose up -d patroni-node-c
# 2) switchover off the old binary
docker exec zkcex-pg-patroni-a patronictl switchover zkcex-pg --candidate node-b
# 3) upgrade the (now) replica node-a
docker compose up -d patroni-node-a
```

Major-version upgrades (16 → 17) need a separate procedure — see "Major
upgrades" below — because the data directory format changes and Patroni
will refuse to start a 16 replica against a 17 leader.

## Add a node (scale out)

Append a fourth service `patroni-node-d` to `docker-compose.yml` with
`PATRONI_NAME=node-d` and `CONNECT_ADDRESS=patroni-node-d`, plus the same
etcd/scope/namespace env. Bring it up: `docker compose up -d patroni-node-d`.
Patroni detects the existing scope in etcd, runs `pg_basebackup` from the
leader, and joins as a replica. Add a `server node-d patroni-node-d:5432`
line in both `listen` blocks of `haproxy.cfg` and `docker compose up -d
haproxy` to refresh.

## Remove a node

```bash
docker exec zkcex-pg-patroni-a patronictl remove zkcex-pg
# Patroni will prompt for the cluster name and the member to remove.
docker compose stop patroni-node-c
docker compose rm -f patroni-node-c
```

Remove the corresponding `server` line from `haproxy.cfg`.

## Replication slot management

Patroni manages slots automatically: one physical slot per replica on the
leader, named after the replica. You can see them via:

```bash
docker exec zkcex-pg-patroni-a su postgres -c \
  "psql -c 'SELECT slot_name, slot_type, active, wal_status FROM pg_replication_slots'"
```

If `wal_status` is `lost` for a slot, the replica has fallen too far behind
to ever catch up and Patroni will reinit it from a fresh `pg_basebackup`.
If you see slots that don't correspond to any current member, drop them by
hand — they're leftover from a removed node.

## WAL archiving

This demo runs with `archive_mode=on, archive_command=/bin/true` — i.e.
archiving is wired up but the command throws WAL into the void. That is
fine for a failover demo (synchronous in-memory replication is the actual
durability backstop here) but it is **not** acceptable in production
because:

- a full cluster wipe is unrecoverable,
- point-in-time recovery is impossible,
- a fresh replica can only be bootstrapped while the leader still has the
  needed WAL on its slot.

The supported production path is `wal-g` to S3 / GCS. Spilo has first-class
support for it: set `USE_WALG_BACKUP=true` and supply the bucket/credentials
env (`WAL_S3_BUCKET`, `AWS_ACCESS_KEY_ID`, etc.). Patroni will then run
`wal-g backup-push` for periodic base backups and Postgres' `archive_command`
becomes `wal-g wal-push %p`. Restore: `WALE_S3_PREFIX=... wal-g backup-fetch`
into the data directory before letting Patroni bootstrap.

## Synchronous mode (zero RPO)

Async streaming is the default. To make the failover demo show
**guaranteed zero data loss** even under load:

```bash
docker exec -it zkcex-pg-patroni-a patronictl edit-config zkcex-pg
```

then set:

```yaml
synchronous_mode: true
synchronous_node_count: 1            # one sync standby is enough for 3 nodes
postgresql:
  parameters:
    synchronous_standby_names: '*'   # let Patroni manage the list
```

After this, a commit blocks on the leader until at least one standby has
fsynced the WAL. If the only sync standby dies, Patroni elevates one of the
async ones to sync automatically — that's why `synchronous_mode: true` is
safer than naked `synchronous_standby_names`.

The tradeoff: commit latency goes up by one local-network RTT (sub-ms here,
50–70 ms across regions; see "Cross-region latency" in the manual sibling's
runbook).

## Recovery from total cluster loss

If all three Postgres nodes are gone but you still have a backup:

1. Wipe etcd: `docker compose down etcd && docker volume rm postgres-cluster-patroni_etcd-data` (this clears any stale leader lock).
2. Configure node-a to restore from the backup: in `docker-compose.yml`,
   set `USE_WALG_RESTORE=true` and supply `WAL_S3_BUCKET` /
   `WAL_S3_RESTORE_PREFIX` env vars.
3. `docker compose up -d etcd patroni-node-a` — Patroni sees an empty etcd
   and an empty data dir, runs the configured restore, and once Postgres
   is up promotes itself as the new leader.
4. Bring up node-b, node-c — they bootstrap fresh from the new leader.

If you have no backup but **one** Postgres data directory survives, you can
force-bootstrap from it: keep that volume, wipe etcd, set the env var
`SPILO_CONFIGURATION` to skip the restore step, start that single node. It
will refuse to start while it thinks it's a replica with no leader — flip
`/home/postgres/pgdata/pgroot/data/standby.signal` off (rm the file) before
starting, and Patroni will register the existing data as the new leader.

## Major-version upgrade

Spilo bundles `pg_upgrade` and Patroni has a `patronictl reload` flow but
major upgrades still need an outage. The supported path:

1. Stop all writes (drain through HAProxy: take RW backend down).
2. `patronictl pause zkcex-pg` — stop Patroni from reacting.
3. Run `pg_upgrade` on the leader's data dir against the new binaries.
4. Update the image tag in `docker-compose.yml` and `docker compose up -d`
   the leader.
5. Wipe the replicas' data volumes and let them re-bootstrap from the
   upgraded leader.
6. `patronictl resume zkcex-pg`.

Blue/green is the lower-risk alternative: bring up a second cluster on the
new version, replicate from the old one with logical replication, switch
the application endpoint, decommission the old cluster.

## Backup integration

Two layered backups are recommended in production:

- **pgBackRest** for periodic base backups + incremental backups + WAL
  archiving. It's the most feature-rich Postgres backup tool and supports
  parallel restore, encryption, retention policies, and S3-compatible
  object stores. Run pgBackRest as a sidecar on each node and have it
  orchestrate from the Patroni leader.
- **wal-g** for continuous WAL streaming to S3. Lighter than pgBackRest;
  Spilo has it built-in. Use this for the WAL stream even if you choose
  pgBackRest for base backups.

This demo uses neither — replicas bootstrap with `pg_basebackup` direct
from the leader, which is fine for a failover demo but not for disaster
recovery.

## Alternative DCS

Patroni supports three distributed consensus stores. We deploy **etcd**
here because:

- It's the smallest dependency (a single Go binary).
- It's what the Kubernetes ecosystem standardised on, so a production
  cluster usually already has one running.

The alternatives:

- **Consul** — heavier (includes service discovery + KV + ACL), nice if
  you want service discovery across more than just Postgres. Patroni env:
  `PATRONI_CONSUL_HOST=consul:8500`.
- **ZooKeeper** — battle-tested, more operationally annoying (no idle
  TTLs, more aggressive memory profile, JVM in the loop). Use if your
  org already runs a ZK cluster for Kafka.
- **Kubernetes API** — Patroni can use the k8s API itself as the DCS via
  ConfigMap or Endpoints. This is what the production Helm chart uses:
  no extra dependency, leader election rides on k8s' own etcd. The
  tradeoff is that the kube-apiserver becomes a hard runtime dependency
  for the Postgres cluster.

## Integration with auth_server

After the cluster is up, point auth_server at the HAProxy RW endpoint:

```bash
AUTH_DB_BACKEND=postgres \
  POSTGRES_DSN="postgresql://app:app-password@127.0.0.1:5443/zkcex_auth" \
  python3 tools/auth_server.py 5501
```

After auto-failover the DSN does not change. HAProxy detects the new leader
within `inter * fall = 9s` (three failed 3s health checks) and starts
routing to it. In-flight transactions that committed on the dying primary
are at risk of being lost under async; transactions still open get a
connection-reset error and must be retried by the application.

A small `retry-with-backoff` helper around the `auth_db.py` connection pool
is enough to make `auth_server` ride out the failover window transparently:
on `pg8000.dbapi.InterfaceError` or `OperationalError`, close the
connection, sleep `min(2^n * 50ms, 5s)`, reconnect. After the new leader is
elected the very next attempt succeeds. (Don't modify `auth_db.py` from
this task — the change belongs to the auth-server squad. Just document the
contract: it must be reconnect-safe across DSN-level outages of up to 30 s.)

Read-only traffic (session lookups for dashboards) can use the RO endpoint
at `:5444`. HAProxy round-robins it across all healthy replicas:

```bash
POSTGRES_DSN_RO="postgresql://app:app-password@127.0.0.1:5444/zkcex_auth"
```

## Monitoring

In production deploy `patroni-exporter` (a Prometheus exporter that scrapes
each node's `/metrics` endpoint) plus the standard `postgres_exporter`.
Key alerts:

| Alert                                | Threshold                |
|--------------------------------------|--------------------------|
| `patroni_cluster_unlocked` = 1       | for > 30 s               |
| `patroni_replica_replication_lag`    | > 30 s                   |
| `pg_replication_slots.wal_status`    | = `lost`                 |
| HAProxy `postgres-rw` backends UP    | < 1                      |
| `patroni_member_state` != `running`  | for any member, > 60 s   |

A Grafana dashboard panel that overlays the leader-name (from
`patroni_cluster_member` with `role="primary"`) against the application's
read-only DSN's round-trip time tells you immediately whether HAProxy is
actually following the elections.
