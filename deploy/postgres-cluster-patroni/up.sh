#!/usr/bin/env bash
# Bring up a three-node Patroni Postgres cluster with etcd + HAProxy.
# Parallel to ../postgres-cluster/up.sh — does not touch that stack.
set -euo pipefail
cd "$(dirname "$0")"

echo "=== Bringing up etcd ==="
docker compose up -d etcd
echo "Waiting for etcd..."
for i in $(seq 1 30); do
  if docker exec zkcex-pg-etcd etcdctl endpoint health >/dev/null 2>&1; then
    echo "etcd ready on :2379"
    break
  fi
  sleep 1
done

echo "=== Bringing up first Patroni node (node-a) ==="
docker compose up -d patroni-node-a
echo "Waiting for node-a to win first leader election..."
ready=""
for i in $(seq 1 90); do
  if curl -sf http://localhost:8008/leader > /dev/null 2>&1; then
    echo "node-a is leader"
    ready=1
    break
  fi
  sleep 2
done
if [ -z "$ready" ]; then
  echo "node-a did not become leader within 180s. Last 40 log lines:"
  docker logs --tail 40 zkcex-pg-patroni-a || true
  exit 1
fi

# Bootstrap the auth schema. Spilo runs Postgres as user `postgres` and the
# super-user password we set is `superpw`; the `app` role is created as a
# normal login role by the spilo entry. We pipe the schema through `psql`
# inside the container so we don't need libpq on the host.
echo "=== Creating zkcex_auth database (owner=app) ==="
docker exec zkcex-pg-patroni-a su postgres -c \
  "psql -c \"CREATE DATABASE zkcex_auth OWNER app\" 2>/dev/null" || true

echo "=== Importing auth schema ==="
cat ../../tools/auth_schema/postgres.sql | \
  docker exec -i zkcex-pg-patroni-a su postgres -c \
  "psql -d zkcex_auth"

echo "=== Bringing up node-b, node-c, haproxy ==="
docker compose up -d patroni-node-b patroni-node-c haproxy

echo "Waiting for replicas to catch up..."
for i in $(seq 1 60); do
  # Both b and c should report /replica = 200 once they are streaming
  b_ok=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8009/replica || echo 000)
  c_ok=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8010/replica || echo 000)
  if [ "$b_ok" = "200" ] && [ "$c_ok" = "200" ]; then
    echo "both replicas streaming"
    break
  fi
  sleep 2
done

echo ""
echo "=== Cluster topology (patronictl list) ==="
docker exec zkcex-pg-patroni-a patronictl -c /home/postgres/postgres.yml list || true

echo ""
echo "=== Connect strings ==="
echo "  RW: postgresql://app:app-password@127.0.0.1:5443/zkcex_auth"
echo "  RO: postgresql://app:app-password@127.0.0.1:5444/zkcex_auth"
echo "  Stats: http://127.0.0.1:7777/"
echo ""
echo "Direct node access (debugging only — apps should use HAProxy):"
echo "  node-a: 127.0.0.1:5440  REST: http://127.0.0.1:8008/"
echo "  node-b: 127.0.0.1:5441  REST: http://127.0.0.1:8009/"
echo "  node-c: 127.0.0.1:5442  REST: http://127.0.0.1:8010/"
