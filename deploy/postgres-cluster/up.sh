#!/usr/bin/env bash
# Bring up a two-region Postgres cluster (primary on :5434, warm standby on :5435).
# Stand-in for the Helm chart's "region-a primary + region-b warm standby" design.
set -euo pipefail
cd "$(dirname "$0")"

echo "=== Bringing up primary ==="
docker compose up -d postgres-primary

echo "Waiting for primary..."
until docker exec zkcex-pg-primary pg_isready -U app -d zkcex_auth >/dev/null 2>&1; do
  sleep 1
done
echo "Primary ready on :5434"

# Import the auth schema (idempotent — CREATE TABLE IF NOT EXISTS).
echo "=== Importing auth schema ==="
docker exec -i zkcex-pg-primary psql -U app -d zkcex_auth < ../../tools/auth_schema/postgres.sql

# Start the replica. The replica's entrypoint runs pg_basebackup on first boot
# from the slot `replica_a_slot` that the primary's init SQL created.
echo "=== Bringing up replica ==="
docker compose up -d postgres-replica

echo "Waiting for replica..."
ready=""
for i in $(seq 1 60); do
  if docker exec zkcex-pg-replica pg_isready -U app -d zkcex_auth >/dev/null 2>&1; then
    echo "Replica ready on :5435"
    ready=1
    break
  fi
  sleep 2
done

if [ -z "$ready" ]; then
  echo "Replica did not become ready in 120s. Last 40 log lines:"
  docker logs --tail 40 zkcex-pg-replica || true
  exit 1
fi

# Verify replication.
echo ""
echo "=== Replication status (from primary's pg_stat_replication) ==="
docker exec zkcex-pg-primary psql -U app -d zkcex_auth -c \
  "SELECT client_addr, state, sync_state, write_lag, flush_lag, replay_lag FROM pg_stat_replication;"

echo ""
echo "=== Recovery status (from replica) ==="
docker exec zkcex-pg-replica psql -U app -d zkcex_auth -c \
  "SELECT pg_is_in_recovery() AS in_recovery, pg_last_wal_receive_lsn() AS receive_lsn, pg_last_wal_replay_lsn() AS replay_lsn;"
