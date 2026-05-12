#!/usr/bin/env bash
# Promote the warm standby to primary. This is the manual failover path —
# in production, Patroni / pg_auto_failover / repmgr would do this on quorum.
set -euo pipefail
cd "$(dirname "$0")"

echo "=== STAGE 1: stopping primary ==="
docker stop zkcex-pg-primary || true

echo "=== STAGE 2: promoting replica ==="
docker exec zkcex-pg-replica psql -U app -d zkcex_auth -c "SELECT pg_promote(true, 60);"

# Wait for promotion to complete (replica leaves recovery mode).
for i in $(seq 1 30); do
  if docker exec zkcex-pg-replica psql -U app -d zkcex_auth -tAc "SELECT pg_is_in_recovery()" | grep -q '^f$'; then
    echo "Replica promoted to primary."
    break
  fi
  sleep 1
done

echo ""
echo "=== STAGE 3: switch auth_server DSN ==="
echo "Operator action: set"
echo "    POSTGRES_DSN=postgresql://app:app-password@127.0.0.1:5435/zkcex_auth"
echo "and restart auth_server. In Kubernetes the Service follows the leader label,"
echo "so the application reconnects automatically once the pod's readiness flips."
