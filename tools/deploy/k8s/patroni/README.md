# Patroni for zkCEX Postgres

This directory holds the Kubernetes manifests that turn the existing
`deploy/postgres-cluster/` primary + replica into a 3-node Patroni cluster
with **automatic** leader election and failover.

```
                    +----------------------+
                    |  K8s API (DCS)       |
                    |  endpoints/zkcex     |   <-- holds leader lease
                    +----------+-----------+
                               |
              watch + patch    |    spilo-role transitions
              ---------------- + -----------------
             /                 |                  \
+----------------+   +-----------------+   +-----------------+
| patroni-0      |   | patroni-1       |   | patroni-2       |
| spilo-role=    |   | spilo-role=     |   | spilo-role=     |
|   master       |   |   replica       |   |   replica       |
| PG :5432       |   | PG :5432        |   | PG :5432        |
| Patroni :8008  |   | Patroni :8008   |   | Patroni :8008   |
+--------+-------+   +--------+--------+   +--------+--------+
         ^                    ^                     ^
         |                    |                     |
         | selector picks     | selector picks      |
         |  spilo-role=master |  spilo-role=replica |
         |                    |                     |
+--------+--------+   +-------+--------------------++
| patroni-primary |   |     patroni-replica         |
|    Service      |   |        Service              |
| (RW, 1 endpoint)|   |   (RO fanout, N endpoints)  |
+-----------------+   +-----------------------------+
```

## Why Patroni (vs. pg_auto_failover / repmgr)

| Property | Patroni                       | pg_auto_failover     | repmgr                 |
|----------|-------------------------------|----------------------|------------------------|
| DCS      | K8s API, etcd, consul, ZK     | Dedicated monitor PG | None (manual + cron)   |
| Quorum   | Raft via the DCS              | Single monitor SPOF* | None                   |
| K8s mode | First-class (Spilo image)     | Helm chart, less mature | Container only      |
| Watchdog | Kernel softdog + REST fencing | No                   | No                     |
| Switchover | `patronictl switchover` (controlled) | `pg_autoctl perform switchover` | manual scripts |
| Maturity | Powers Zalando, RedHat, AWS RDS-OS forks | Production | Production but smaller surface |
| Operator burden in K8s | low (Spilo bundles everything) | medium | high |

\* pg_auto_failover's monitor is single-instance unless you build HA for the
monitor itself; that pushes the failure boundary, it doesn't remove it.

We picked Patroni because:

1. **K8s-native DCS.** No separate etcd cluster to operate; the API server
   we already trust is the source of truth. One less stateful system.
2. **Spilo packages it.** The `ghcr.io/zalando/spilo-15:3.0-p1` image
   bundles PG 15 + Patroni + WAL-G + sane defaults. No bespoke Dockerfile.
3. **Split-brain protection.** Leader lease in DCS + soft watchdog +
   `synchronous_commit=on` means a fenced ex-leader cannot acknowledge a
   write that the new leader doesn't have.
4. **Battle-tested** by Zalando (5k+ instances), RDS clones, Crunchy Data,
   Cloud Native PG (which lifts Patroni's design wholesale).

## Files

| File | Purpose |
|------|---------|
| `patroni-statefulset.yaml` | The 3-replica StatefulSet (Spilo image), headless Service, podAntiAffinity. |
| `service-primary.yaml`      | RW Service. Selector pins to `spilo-role=master`. |
| `service-replica.yaml`      | RO Service. Selector pins to `spilo-role=replica`. |
| `configmap-patroni.yaml`    | Patroni YAML config: ttl=30, sync replication, WAL params. |
| `rbac.yaml`                 | ServiceAccount + Role + RoleBinding for K8s DCS access. |
| `secret.yaml.template`      | Template for `postgres-superuser` + `postgres-replication-user`. |
| `failover-test.sh`          | End-to-end automatic failover smoke test. |

## Bringing the cluster up

```bash
# 1. Copy the template, fill in real passwords, apply, delete the local copy.
cp secret.yaml.template /tmp/patroni-secret.yaml
# edit /tmp/patroni-secret.yaml — generate with: openssl rand -base64 32
kubectl -n zkcex apply -f /tmp/patroni-secret.yaml
shred -u /tmp/patroni-secret.yaml

# 2. RBAC, config, services, StatefulSet — order matters because the
# StatefulSet's first pod needs the headless Service for peer discovery and
# the ConfigMap for its patroni.yaml mount.
kubectl -n zkcex apply -f rbac.yaml
kubectl -n zkcex apply -f configmap-patroni.yaml
kubectl -n zkcex apply -f service-primary.yaml
kubectl -n zkcex apply -f service-replica.yaml
kubectl -n zkcex apply -f patroni-statefulset.yaml

# 3. Wait for all 3 members to converge.
kubectl -n zkcex rollout status statefulset/patroni --timeout=10m

# 4. Confirm exactly one leader.
kubectl -n zkcex exec patroni-0 -- \
    patronictl -c /etc/patroni/patroni.yaml list
```

## Operational runbook

### Planned switchover (zero downtime)

Use this for node maintenance, k8s upgrades, scaling.

```bash
kubectl -n zkcex exec patroni-0 -- \
    patronictl -c /etc/patroni/patroni.yaml switchover \
    --master patroni-0 \
    --candidate patroni-1 \
    --force
```

`switchover` blocks until the candidate is caught up to within
`maximum_lag_on_failover` (1 MiB), executes the role swap atomically, and
returns. Application connections to `patroni-primary` see one TCP-level
disconnect; new connects land on the new leader immediately.

### Unplanned failover (leader has crashed or partitioned)

Patroni handles this autonomously. The clock you should know:

* `ttl` = 30s: the leader lease expires after this if the leader can't
  renew it in the K8s API.
* `loop_wait` = 10s: each follower polls every loop_wait.
* `maximum_lag_on_failover` = 1 MiB: only sufficiently caught-up followers
  are eligible to promote.

**Worst-case detection + promotion** is `ttl + loop_wait + retry_timeout`
=~ 50 seconds. With sync replication, the new leader is guaranteed to have
every committed write — RPO = 0.

If you need to force a specific candidate manually:

```bash
patronictl -c /etc/patroni/patroni.yaml failover --candidate patroni-2
```

### Adding a replica

`kubectl scale statefulset patroni --replicas=4`. The new pod runs
`pg_basebackup` from the current leader during boot, then joins as a
replica. Watch for the basebackup to complete — large databases can take
hours, during which the new pod will be Pending or Initializing.

### Point-in-time recovery (PITR) with WAL-G

WAL-G is bundled in Spilo but disabled in this baseline (USE_WALG_BACKUP=false).
To turn it on:

1. Provision an S3-compatible bucket. Note the credentials.
2. Add envs to the StatefulSet:
   ```yaml
   - name: USE_WALG_BACKUP
     value: "true"
   - name: USE_WALG_RESTORE
     value: "true"
   - name: WALG_S3_PREFIX
     value: s3://your-bucket/zkcex
   - name: AWS_ACCESS_KEY_ID
     valueFrom: {secretKeyRef: {name: walg-s3, key: access_key}}
   - name: AWS_SECRET_ACCESS_KEY
     valueFrom: {secretKeyRef: {name: walg-s3, key: secret_key}}
   ```
3. Roll the StatefulSet. Spilo's `postgres-appliance` cron schedules base
   backups (default: daily) and continuous WAL push.
4. To restore to a point in time, scale to 0, override
   `WALG_RESTORE_TARGET_TIME` on a new StatefulSet, scale to 1, let it
   replay, then scale to 3.

### Putting pgBouncer in front

The Patroni pods themselves do **not** run pgBouncer. The recommended
topology is:

```
app --> pgBouncer Deployment (replicas=3) --> patroni-primary Service
                                          --> patroni-replica Service
```

* Pool mode `transaction` for most services.
* `auth_type=scram-sha-256`, with `auth_query` against the Patroni
  cluster's `pg_shadow`.
* Set `server_check_query = 'SELECT 1'` and `server_check_delay = 30` so
  pgBouncer notices when a backend connection has been failed over off
  underneath it.
* When the primary moves, pgBouncer's existing connections to the old leader
  break and it reconnects through DNS — so pgBouncer effectively gets
  failover for free as long as it resolves `patroni-primary` each connect.

## Failure modes

### Split-brain prevention

Three layers stack:

1. **DCS quorum.** Leader lease lives in the K8s Endpoints object. Only
   one pod can hold a lease whose `ttl` has not expired. The K8s API
   itself is HA (etcd quorum behind it).
2. **Sync replication.** `synchronous_commit=on` + `synchronous_standby_names='*'`
   means a write isn't acknowledged until at least one standby has
   flushed it. If the leader is partitioned from all standbys, COMMIT
   blocks — the partitioned leader cannot acknowledge writes that no one
   else has seen.
3. **Watchdog (optional).** The Spilo image ships with softdog support;
   we keep it off in-cluster because K8s liveness eviction is fast enough
   and softdog can complicate node maintenance. Turn it on for bare-metal
   deployments where node-fencing isn't otherwise solved.

### Network partition behavior

Scenario: pod-0 (leader) loses connectivity to the K8s API.

* t=0:    Leader can't renew its lease. Replicas keep streaming until
          their own TCP to the leader breaks.
* t<30s:  Leader still serves writes — but since
          `synchronous_commit=on`, every COMMIT waits for a standby
          ACK. If replicas can still reach the leader on Postgres-port
          5432 (different network path), writes keep flowing.
* t=30s:  Lease expires in DCS. Followers race to acquire the lease;
          the most-caught-up one wins, runs `promote`, and the K8s
          Endpoints behind `patroni-primary` flip to point at it.
* t>30s:  Old leader sees its lease was lost, runs `demote` →
          becomes a replica, then reconciles its data against the new
          leader via `pg_rewind` (`use_pg_rewind: true` in config).

### fsync settings

`fsync=on`, `synchronous_commit=on` are non-negotiable for financial
state. They are the defaults in the bundled Spilo config and we do not
override them. Do NOT change them to chase write throughput — buy bigger
disks instead.

Specifically:

* `wal_log_hints=on` is required for `pg_rewind` to work after failover.
  Without it, a former leader that briefly diverged cannot rejoin without
  a full `pg_basebackup`.
* `wal_keep_size=256MB` is a compromise: large enough for a brief replica
  outage to catch up on stream rather than basebackup, small enough not
  to fill the data dir if WAL archiving stalls. Tune up if you see
  "requested WAL segment ... has already been removed" in standby logs.

## Migration path from primary/replica

You have two options to migrate from the current
`deploy/postgres-cluster/` (primary + streaming replica) to Patroni.

### Option A — Logical dump + restore (preferred for small-to-medium DBs)

Lower risk; cleaner cutover.

```bash
# 1. Bring up the Patroni cluster but DON'T point any traffic at it.
kubectl -n zkcex apply -f .

# 2. From the existing primary, dump the schema + data.
pg_dump --clean --if-exists --no-owner --no-privileges \
        -h <old-primary> -U app -d zkcex_auth \
        > /tmp/zkcex_auth.sql

# 3. Restore into the Patroni leader.
PASS=$(kubectl -n zkcex get secret postgres-superuser \
       -o jsonpath='{.data.password}' | base64 -d)
kubectl -n zkcex exec -i patroni-0 -- env PGPASSWORD="$PASS" \
    psql -h patroni-primary -U postgres -d postgres < /tmp/zkcex_auth.sql

# 4. Cut traffic.
#    - Update POSTGRES_DSN in the auth-config Secret to point at patroni-primary.
#    - kubectl rollout restart deployment/auth.
#    - Verify writes land on the Patroni leader (`patronictl list` shows lsn moving).
#    - Decommission the old primary/replica after a soak period.
```

Cutover RTO is bounded by step 4 — typically ~30s of write rejection while
the auth Deployment rolls. If that is unacceptable, use Option B.

### Option B — pg_basebackup from the existing primary

Cluster-replace via Patroni's `replica` bootstrap method against an
external WAL source. Tricky; only do this if downtime in Option A is
unacceptable AND your DB is too large to dump in a maintenance window.

The procedure (high-level):

1. Configure the Patroni StatefulSet to bootstrap via
   `pg_basebackup --host=<old-primary>`. This is a Spilo env override:
   `CLONE_WITH_BASEBACKUP=true`, `CLONE_HOST`, `CLONE_PORT`, `CLONE_USER`.
2. Start the StatefulSet with 1 replica. It clones from the old primary
   via streaming replication, becoming a "standby of the old primary".
3. Once caught up, run `pg_promote` on the Patroni leader (via
   `patronictl reinit --force` after switching off the standby_signal).
   At this point both clusters are independent.
4. Scale Patroni to 3. The new pods clone from the Patroni leader.
5. Flip application DSN. Decommission.

The risk is that step 3 has a race: writes that landed on the old primary
AFTER the basebackup snapshotted but BEFORE the promote can be lost. Use
Option A unless you've measured this and accepted it.

### Verifying the migration

```bash
bash failover-test.sh                # smoke test the new cluster
psql -h patroni-primary -c "SELECT count(*) FROM users;"   # row counts match old
patronictl list                      # 1 leader + 2 replicas
```

## Honest gaps + caveats

* **K8s API is now a dependency for write availability.** If the K8s
  API server is unreachable for longer than `ttl + retry_timeout` (=40s),
  the current leader will demote itself (Patroni's safety stance) and
  writes will pause until DCS is reachable again. This is a deliberate
  trade — split-brain is worse than a brief outage — but it does mean
  the K8s control plane needs to be HA. The managed K8s control planes
  (EKS, GKE, AKS) are; a single-node kubeadm cluster is not.
* **DNS TTL.** `patroni-primary` is a Service ClusterIP, so DNS doesn't
  change on failover — only the Endpoints behind it. But `pg8000` (our
  Python driver) caches the TCP connection; on failover, in-flight
  queries get an `OperationalError` and the next connect picks up the
  new endpoint. The application must retry. `auth_db.PostgresAuthDB`
  already uses short-lived connections, so this is fine for that
  service; longer-lived service mesh connections (custody) need an
  explicit reconnect policy.
* **WAL-G is off by default.** Until you stand up the S3 bucket + IAM,
  there is no off-cluster backup. The replicas give you HA against pod
  loss but NOT against a region-wide event or a bad migration that
  corrupts state. Wire WAL-G before declaring this production-ready.
* **sync_node_count=1, not strict.** `synchronous_mode_strict: false`
  means: if all replicas are unhealthy, the leader will fall back to
  async and keep serving writes (RPO can become non-zero). The strict
  alternative is to set `synchronous_mode_strict: true` and accept that
  losing all replicas at once stops writes. We default to "available"
  here; flip to strict for the highest-value workloads (custody, settlement).
* **Two-region failover is out of scope here.** This config gives you
  HA within one K8s cluster. Cross-region warm-standby is the
  `MULTI_REGION_FAILOVER.md` problem and is handled by Patroni's
  `standby_cluster` config in a second cluster — not covered here.

## Pointing the application at Patroni

In the zkCEX Helm chart, set:

```yaml
patroni:
  enabled: true
```

That switches the chart's templates over to render this directory's
manifests with values-driven sizing. The `zkcex-postgres` Secret used by
`auth_server` / `chain_server` / etc. is repointed at `patroni-primary`
for writes; read-only callers should target `patroni-replica` explicitly
via `POSTGRES_READ_HOST`. See `auth_db.py` for the read/write split.
