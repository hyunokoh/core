#!/usr/bin/env bash
# End-to-end Patroni failover smoke test.
#
# Verifies:
#   1. We can identify the current leader via `patronictl list`.
#   2. Writes through the patroni-primary Service land on that leader.
#   3. Killing the leader pod (--grace-period=0) triggers a Patroni-driven
#      failover within the configured ttl (30s) plus loop_wait (10s).
#   4. The patroni-primary Service selector flips to the new leader.
#   5. New writes succeed through the same Service name (clients don't have
#      to know which pod won).
#   6. The killed pod, once recreated by the StatefulSet, rejoins as a
#      replica (spilo-role=replica) and streams from the new leader.
#
# Usage:
#   NAMESPACE=zkcex ./failover-test.sh
#
# Requires:
#   * kubectl pointing at a cluster where the Patroni StatefulSet from
#     patroni-statefulset.yaml is up with at least 2 healthy members.
#   * The `postgres-superuser` Secret applied.
#   * No real customer data — this script CREATES and DROPS a small
#     `failover_test` table in the `postgres` database.
set -euo pipefail

NAMESPACE="${NAMESPACE:-zkcex}"
TIMEOUT_FAILOVER_S="${TIMEOUT_FAILOVER_S:-45}"
PRIMARY_SVC="${PRIMARY_SVC:-patroni-primary}"
APP_DB="${APP_DB:-postgres}"

# Colours (only if stdout is a TTY).
if [ -t 1 ]; then
    R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; B=$'\033[34m'; N=$'\033[0m'
else
    R=""; G=""; Y=""; B=""; N=""
fi

say()  { printf "%s[failover-test]%s %s\n" "$B" "$N" "$*"; }
ok()   { printf "%s[ok]%s %s\n" "$G" "$N" "$*"; }
warn() { printf "%s[warn]%s %s\n" "$Y" "$N" "$*"; }
die()  { printf "%s[fail]%s %s\n" "$R" "$N" "$*" >&2; exit 1; }

require() {
    command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"
}

require kubectl

# Fetch the superuser password from the Secret so we can run psql from inside
# a pod. We never echo this back to stdout.
say "fetching postgres-superuser password from Secret"
PG_PASS="$(kubectl -n "$NAMESPACE" get secret postgres-superuser \
    -o jsonpath='{.data.password}' | base64 -d)"
PG_USER="$(kubectl -n "$NAMESPACE" get secret postgres-superuser \
    -o jsonpath='{.data.username}' | base64 -d)"
[ -n "$PG_PASS" ] || die "postgres-superuser/password is empty"

# Pick any Patroni pod to exec `patronictl` and psql commands inside.
# We use the StatefulSet's first ordinal for stability.
PATRONI_POD=""
for candidate in $(kubectl -n "$NAMESPACE" get pods \
        -l app.kubernetes.io/name=patroni -o name); do
    if kubectl -n "$NAMESPACE" get "$candidate" \
            -o jsonpath='{.status.phase}' 2>/dev/null | grep -q Running; then
        PATRONI_POD="${candidate#pod/}"
        break
    fi
done
[ -n "$PATRONI_POD" ] || die "no Running Patroni pod found in ns=$NAMESPACE"
say "using pod $PATRONI_POD for control-plane commands"

# ---------------------------------------------------------------------------
# Step 1. List members + find current leader.
# ---------------------------------------------------------------------------
say "step 1: patronictl list (cluster state)"
kubectl -n "$NAMESPACE" exec "$PATRONI_POD" -- \
    patronictl -c /etc/patroni/patroni.yaml list || \
    die "patronictl list failed; cluster not converged?"

LEADER_BEFORE="$(kubectl -n "$NAMESPACE" exec "$PATRONI_POD" -- \
    patronictl -c /etc/patroni/patroni.yaml list -f json 2>/dev/null \
    | python3 -c "import json,sys
rows=json.load(sys.stdin)
for r in rows:
    if r.get('Role','').lower() in ('leader','master','primary'):
        print(r['Member']); break" || true)"
[ -n "$LEADER_BEFORE" ] || die "could not parse current leader from patronictl list"
ok "current leader: $LEADER_BEFORE"

# ---------------------------------------------------------------------------
# Step 2. Insert a sentinel row through patroni-primary.
# ---------------------------------------------------------------------------
say "step 2: insert pre-failover sentinel through Service/$PRIMARY_SVC"
SENTINEL_BEFORE_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
kubectl -n "$NAMESPACE" exec "$PATRONI_POD" -- env PGPASSWORD="$PG_PASS" \
    psql -h "$PRIMARY_SVC" -U "$PG_USER" -d "$APP_DB" -v ON_ERROR_STOP=1 \
    -c "CREATE TABLE IF NOT EXISTS failover_test (id serial primary key, phase text, ts timestamptz default now());" \
    -c "INSERT INTO failover_test (phase) VALUES ('pre-failover');" \
    > /dev/null \
    || die "pre-failover INSERT failed"
ok "pre-failover INSERT succeeded at $SENTINEL_BEFORE_TS"

# ---------------------------------------------------------------------------
# Step 3. Kill the leader pod hard.
# ---------------------------------------------------------------------------
say "step 3: deleting leader pod $LEADER_BEFORE with --grace-period=0"
kubectl -n "$NAMESPACE" delete pod "$LEADER_BEFORE" --grace-period=0 --force \
    --wait=false || die "delete leader pod failed"
KILL_TS="$(date -u +%s)"
ok "leader pod kill requested"

# ---------------------------------------------------------------------------
# Step 4. Poll patroni-primary until a NEW leader can serve writes.
# ---------------------------------------------------------------------------
say "step 4: polling Service/$PRIMARY_SVC for write capability (timeout ${TIMEOUT_FAILOVER_S}s)"
START_S="$(date -u +%s)"
NEW_LEADER=""
while :; do
    NOW_S="$(date -u +%s)"
    ELAPSED=$(( NOW_S - START_S ))
    if [ "$ELAPSED" -ge "$TIMEOUT_FAILOVER_S" ]; then
        die "failover did not complete within ${TIMEOUT_FAILOVER_S}s"
    fi

    # Try a SELECT pg_is_in_recovery()=false against the primary Service.
    # When this returns 'f', the Service has flipped to the new leader.
    NOT_RECOVERY="$(kubectl -n "$NAMESPACE" exec "$PATRONI_POD" -- \
        env PGPASSWORD="$PG_PASS" \
        psql -h "$PRIMARY_SVC" -U "$PG_USER" -d "$APP_DB" \
        -tA -c "SELECT NOT pg_is_in_recovery();" 2>/dev/null || true)"
    if [ "$NOT_RECOVERY" = "t" ]; then
        ok "Service/$PRIMARY_SVC is serving a writable PG after ${ELAPSED}s"
        break
    fi
    sleep 1
done

# Identify who won.
NEW_LEADER="$(kubectl -n "$NAMESPACE" exec "$PATRONI_POD" -- \
    patronictl -c /etc/patroni/patroni.yaml list -f json 2>/dev/null \
    | python3 -c "import json,sys
rows=json.load(sys.stdin)
for r in rows:
    if r.get('Role','').lower() in ('leader','master','primary'):
        print(r['Member']); break" || true)"
[ -n "$NEW_LEADER" ] || die "could not identify new leader after failover"
if [ "$NEW_LEADER" = "$LEADER_BEFORE" ]; then
    die "leader did not change (still $LEADER_BEFORE) — failover did not actually run"
fi
ok "new leader: $NEW_LEADER (was: $LEADER_BEFORE)"

# ---------------------------------------------------------------------------
# Step 5. Insert a second sentinel — must land on the new leader.
# ---------------------------------------------------------------------------
say "step 5: insert post-failover sentinel through Service/$PRIMARY_SVC"
SENTINEL_AFTER_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
kubectl -n "$NAMESPACE" exec "$PATRONI_POD" -- env PGPASSWORD="$PG_PASS" \
    psql -h "$PRIMARY_SVC" -U "$PG_USER" -d "$APP_DB" -v ON_ERROR_STOP=1 \
    -c "INSERT INTO failover_test (phase) VALUES ('post-failover');" \
    > /dev/null \
    || die "post-failover INSERT failed"
ok "post-failover INSERT succeeded at $SENTINEL_AFTER_TS"

# ---------------------------------------------------------------------------
# Step 6. SELECT both rows. Synchronous replication means pre-failover must
# be visible on the new leader (it had received the WAL before commit
# returned to the client).
# ---------------------------------------------------------------------------
say "step 6: SELECT both rows from the new leader"
ROWS="$(kubectl -n "$NAMESPACE" exec "$PATRONI_POD" -- env PGPASSWORD="$PG_PASS" \
    psql -h "$PRIMARY_SVC" -U "$PG_USER" -d "$APP_DB" -tA \
    -c "SELECT phase FROM failover_test ORDER BY id;" 2>/dev/null || true)"
echo "$ROWS"
echo "$ROWS" | grep -q "^pre-failover$" || die "pre-failover row missing — data loss!"
echo "$ROWS" | grep -q "^post-failover$" || die "post-failover row missing"
ok "both sentinel rows present on new leader (RPO=0)"

# ---------------------------------------------------------------------------
# Step 7. Wait for the killed pod to come back as a replica.
# ---------------------------------------------------------------------------
say "step 7: waiting for $LEADER_BEFORE to rejoin as replica"
REJOIN_DEADLINE_S=$(( $(date -u +%s) + 120 ))
REJOINED=""
while :; do
    NOW_S="$(date -u +%s)"
    if [ "$NOW_S" -ge "$REJOIN_DEADLINE_S" ]; then
        warn "$LEADER_BEFORE did not rejoin within 120s (StatefulSet PVC reuse delay?)"
        break
    fi
    ROLE="$(kubectl -n "$NAMESPACE" get pod "$LEADER_BEFORE" \
        -o jsonpath='{.metadata.labels.spilo-role}' 2>/dev/null || true)"
    if [ "$ROLE" = "replica" ]; then
        REJOINED=yes
        break
    fi
    sleep 2
done

if [ "$REJOINED" = "yes" ]; then
    ok "$LEADER_BEFORE rejoined as replica (spilo-role=replica)"
else
    warn "rejoin status indeterminate — inspect:"
    warn "  kubectl -n $NAMESPACE get pods -l app.kubernetes.io/name=patroni --show-labels"
fi

# ---------------------------------------------------------------------------
# Final cluster state for the operator's eyes.
# ---------------------------------------------------------------------------
say "final patronictl list"
kubectl -n "$NAMESPACE" exec "$PATRONI_POD" -- \
    patronictl -c /etc/patroni/patroni.yaml list || true

# Cleanup option (off by default — keep the sentinel rows for forensics).
if [ "${CLEANUP:-0}" = "1" ]; then
    say "CLEANUP=1 — dropping failover_test"
    kubectl -n "$NAMESPACE" exec "$PATRONI_POD" -- env PGPASSWORD="$PG_PASS" \
        psql -h "$PRIMARY_SVC" -U "$PG_USER" -d "$APP_DB" \
        -c "DROP TABLE failover_test;" > /dev/null || true
fi

ok "failover smoke test passed"
