# zkCEX Kubernetes Runbook

Operational procedures for the zkCEX production stack. Assumes the chart in
`deploy/k8s/charts/zkcex/` is the source of truth and is reconciled by a
GitOps controller (ArgoCD or Flux).

---

## 1. Scaling a service

```bash
# Imperative (will be reverted by GitOps on next sync — fine for emergencies)
kubectl -n zkcex scale deployment auth --replicas=5

# Permanent: bump replicas in values.yaml under services.<name>.replicas,
# commit, push, let GitOps sync.
```

For autoscaled services, edit `services.<name>.hpa.{minReplicas,maxReplicas}`
in values.yaml. Imperative override:

```bash
kubectl -n zkcex patch hpa auth-hpa --type=merge \
  -p '{"spec":{"minReplicas":5,"maxReplicas":20}}'
```

## 2. Draining a node

```bash
kubectl drain node-1 --ignore-daemonsets --delete-emptydir-data
# Pods will reschedule onto other nodes. PDBs ensure minAvailable
# (replicas - 1) survives the drain.

# Done?
kubectl uncordon node-1
```

## 3. Rotating Postgres credentials

We do not store credentials in Git. The `zkcex-postgres` Secret is expected
to be reconciled by External Secrets Operator from Vault / AWS Secrets Manager.

```bash
# 1) Create new role in Postgres
psql -h zkcex-postgresql -U postgres -c \
  "CREATE USER zkcex_new WITH PASSWORD 'NEW_PASSWORD'; \
   GRANT ALL PRIVILEGES ON DATABASE zkcex TO zkcex_new;"

# 2) Update the upstream secret in Vault (or k8s secret if not using ESO)
kubectl -n zkcex create secret generic zkcex-postgres \
  --from-literal=dsn='postgresql://zkcex_new:NEW_PASSWORD@zkcex-postgresql:5432/zkcex?sslmode=require' \
  --dry-run=client -o yaml | kubectl apply -f -

# 3) Rolling restart of services that hold the DSN
for d in auth chain api-key zk-orderbook order-engine perp; do
  kubectl -n zkcex rollout restart deployment/$d
done

# 4) Verify
kubectl -n zkcex rollout status deployment/auth
# 5) Drop old user
psql -h zkcex-postgresql -U postgres -c "DROP USER zkcex_old;"
```

## 4. Promoting region-B to primary

See `MULTI_REGION_FAILOVER.md` for the full procedure. TL;DR:

```bash
# On region-B cluster:
helm upgrade zkcex deploy/k8s/charts/zkcex \
  -f deploy/k8s/charts/zkcex/values-region-b.yaml \
  --set isPrimary=true \
  --set postgresql.replicaUrl="" \
  -n zkcex
```

## 5. Rolling back a bad deploy

```bash
# Helm history shows revisions:
helm -n zkcex history zkcex

# Rollback to revision 1:
helm -n zkcex rollback zkcex 1

# If GitOps managed: revert the commit in the chart repo and push.
```

## 6. Reading logs

```bash
# Live logs for all auth pods
kubectl -n zkcex logs -l app.kubernetes.io/name=auth --tail=100 -f

# Previous container (if it crashed)
kubectl -n zkcex logs <pod> -c auth --previous

# Aggregate via Loki (preferred for production):
logcli query '{namespace="zkcex", app_kubernetes_io_name="auth"} |~ "ERROR"'
```

## 7. Common alerts and their playbooks

### `ZkcexAuthDown` — auth service has <2 healthy replicas
1. `kubectl -n zkcex get pods -l app.kubernetes.io/name=auth -o wide`
2. Check events: `kubectl -n zkcex describe pod <pending-pod>`
3. If image pull failure -> verify registry pull secret.
4. If `CrashLoopBackOff` -> read logs, check Postgres reachability.

### `ZkcexHighLatencyP99` — proxy p99 latency > 1s for 5min
1. Check upstream health: `kubectl -n zkcex get hpa`
2. Inspect `kubectl top pods -n zkcex --sort-by=cpu`
3. If a hot pod is found, the HPA should be scaling — verify metrics-server is up.

### `ZkcexLeaderLost` — singleton service Lease unheld for >60s
1. `kubectl -n zkcex get lease zkcex-mm-bot-leader -o yaml`
2. Verify the active region's mm-bot pod is healthy.
3. If region-A is down, follow MULTI_REGION_FAILOVER.md.

### `ZkcexPDBBlocked` — PDB prevents drain for >30min
1. Service has too few replicas to meet `minAvailable`. Scale up first:
   `kubectl -n zkcex scale deployment <svc> --replicas=<current+1>`
2. Then retry drain.

## 8. Disaster recovery (full cluster lost)

1. Confirm via Route53 health check that region-A is unreachable.
2. Flip DNS to region-B (failover record).
3. On region-B cluster: promote Postgres standby
   (`pg_ctl promote -D /bitnami/postgresql/data`) and MariaDB replica.
4. Re-deploy chart with `isPrimary=true` (see step 4 above).
5. Verify singleton Leases are acquired by region-B pods:
   `kubectl -n zkcex get lease -o wide`
6. Run smoke tests: `tools/e2e/smoke.sh` against the public URL.
7. Page the on-call to monitor for the next 60 minutes.
8. When region-A returns: rebuild it as the warm standby, reverse-replicate
   from region-B. Do NOT auto-fail-back; schedule a controlled cutover.

## 9. Maintenance: helm chart smoke test before deploy

```bash
helm template deploy/k8s/charts/zkcex --debug > /tmp/render.yaml
kubectl --dry-run=client apply -f /tmp/render.yaml
# OR use a validator:
kubeconform -strict -summary /tmp/render.yaml
```
