# Multi-Region Failover — zkCEX

Topology: **region-a (Seoul, ap-northeast-2) — primary**, **region-b (Singapore,
ap-southeast-1) — warm standby**. Every service is replicated in both regions.
Stateful stores (Postgres, MariaDB) stream from A -> B. Kafka mirrors topics
with MirrorMaker 2. DNS for `app.zkcex.io` is fronted by Route53 with health
checks.

Singleton services (`mm-bot`, `pol-feed`, `custody-coordinator`, `perp`,
`order-engine`) run as 1-replica Deployments in both regions, but only the
holder of a `coordination.k8s.io/v1` Lease is allowed to perform work.
Region-A pods aggressively renew the lease; region-B pods only become active
once the lease has lapsed for `crossRegion.leaseDurationSeconds` (default 30s).

---

## Planned failover (controlled cutover, e.g. maintenance window)

**Goal:** flip primary from A to B with zero data loss.

Time budget: ~10 minutes.

1. **Quiesce writes in region-A.** Set the proxy's read-only mode flag:
   ```bash
   kubectl --context kind-a -n zkcex patch configmap zkcex-app-config \
     --type merge -p '{"data":{"READ_ONLY":"true"}}'
   for d in proxy auth zk-orderbook order-engine perp; do
     kubectl --context kind-a -n zkcex rollout restart deployment/$d
   done
   ```
2. **Wait for Postgres replication lag = 0.**
   ```bash
   kubectl --context kind-a -n zkcex exec -it zkcex-postgresql-0 -- \
     psql -U postgres -c \
     "SELECT client_addr, write_lag, flush_lag, replay_lag FROM pg_stat_replication;"
   ```
3. **Wait for Kafka MM2 lag = 0.**
   ```bash
   kubectl --context kind-a -n zkcex exec deploy/kafka-mm2 -- \
     /opt/bitnami/kafka/bin/kafka-consumer-groups.sh \
       --bootstrap-server kafka:9092 --describe --all-groups | grep -v "LAG=0"
   ```
4. **Release Leases in region-A.** Delete each singleton's holderIdentity:
   ```bash
   for s in mm-bot pol-feed custody-coordinator perp order-engine; do
     kubectl --context kind-a -n zkcex patch lease zkcex-$s-leader \
       --type=merge -p '{"spec":{"holderIdentity":null}}'
   done
   ```
5. **Promote region-B Postgres.**
   ```bash
   kubectl --context kind-b -n zkcex exec -it zkcex-postgresql-0 -- \
     pg_ctl promote -D /bitnami/postgresql/data
   ```
   And MariaDB:
   ```bash
   kubectl --context kind-b -n zkcex exec -it zkcex-mariadb-0 -- \
     mariadb -uroot -p"$PW" -e "STOP SLAVE; RESET SLAVE ALL;"
   ```
6. **Re-render region-B with `isPrimary=true`.**
   ```bash
   helm --kube-context kind-b upgrade zkcex deploy/k8s/charts/zkcex \
     -f deploy/k8s/charts/zkcex/values-region-b.yaml \
     --set isPrimary=true \
     --set postgresql.replicaUrl="" \
     --set mariadb.replicaUrl="" \
     -n zkcex
   ```
7. **DNS flip via Route53.** Update the failover record (`PRIMARY` ->
   `SECONDARY`) for `app.zkcex.io`:
   ```bash
   aws route53 change-resource-record-sets --hosted-zone-id ZONE \
     --change-batch file://r53-failover-to-b.json
   ```
   Wait for TTL (typically 60s).
8. **Confirm B is taking traffic.**
   ```bash
   curl -sS https://app.zkcex.io/healthz
   kubectl --context kind-b -n zkcex get lease -o wide
   # Each singleton lease should show region-b holder
   ```
9. **Re-enable writes.**
   ```bash
   kubectl --context kind-b -n zkcex patch configmap zkcex-app-config \
     --type merge -p '{"data":{"READ_ONLY":"false"}}'
   for d in proxy auth zk-orderbook order-engine perp; do
     kubectl --context kind-b -n zkcex rollout restart deployment/$d
   done
   ```
10. **Reverse-replicate A from B** once region-A is healthy again. Treat A as
    the new standby. Do NOT auto-fail-back; schedule another controlled cutover.

## Unplanned failover (region-A completely unreachable)

Some data loss is possible — we accept whatever has not yet replicated.

1. Confirm region-A is hard-down (Route53 health checks failing for >5 minutes,
   pingdom alerts, multiple operators agree).
2. Skip steps 1–4 above (cannot reach A). Start at step 5 (promote B).
3. In step 5, force-promote Postgres if it has not finished applying the last
   WAL: `pg_ctl promote -D /bitnami/postgresql/data` accepts whatever it has.
4. Log the data loss window in the incident channel.
5. Continue with steps 6–9.
6. **Do not** allow region-A to come back online and replicate "backward"
   without manual reconciliation — there may be split-brain writes. The DBAs
   must diff and merge before re-attaching A as a replica.

## Per-store details

| Store | Replication mode | Promote command |
|-------|------------------|-----------------|
| Postgres | streaming + replication slot `region_b_slot` | `pg_ctl promote` |
| MariaDB  | async row-based binlog replication            | `STOP SLAVE; RESET SLAVE ALL;` |
| Redis    | per-region (not replicated; cache, ephemeral) | n/a — empty cache OK |
| Kafka    | MirrorMaker 2 (active-active topic mirror)    | flip consumer-group offsets per CG migration plan |

## Lease renewal sequence diagram

```
region-A mm-bot pod                            Lease(zkcex-mm-bot-leader)
      |                                                |
      |--- spec.holderIdentity = "region-a/mm-bot-0" ->|
      |                                                |
      |--- (renew every 10s, lease=30s) -------------->|
      |                                                |
      X  (region-A goes down)                          |
      |                                                |
                              (no renewal for 30s)     |
                                                       |
region-B mm-bot pod                                    |
      |<-- watch: holder unchanged for >30s -----------|
      |--- spec.holderIdentity = "region-b/mm-bot-0" ->|
      |    ACTIVE_ROLE flips true                      |
```
