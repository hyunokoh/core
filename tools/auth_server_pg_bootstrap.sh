#!/usr/bin/env bash
# Bootstrap a sibling Postgres for the zkCEX auth + KYC backend.
#
# We deliberately don't reuse `core-postgres-auth-1` from the main
# docker-compose stack: that container exposes no host port (only 5432/tcp
# inside the docker network) and is owned by Keycloak. Standing up a
# dedicated `zkcex-postgres-auth` on host port 5433 keeps the
# AUTH_DB_BACKEND=postgres path self-contained and easy to tear down.
#
# Idempotent: if the container already exists we just start it; if it's
# already running we skip and print the connect string.
set -euo pipefail

NAME="zkcex-postgres-auth"
HOST_PORT="${HOST_PORT:-5433}"
DB="${POSTGRES_DB:-zkcex_auth}"
USER="${POSTGRES_USER:-app}"
PASSWORD="${POSTGRES_PASSWORD:-app-password}"
IMAGE="${POSTGRES_IMAGE:-postgres:16}"

if docker ps --format '{{.Names}}' | grep -q "^${NAME}$"; then
  echo "[pg] ${NAME} is already running"
elif docker ps -a --format '{{.Names}}' | grep -q "^${NAME}$"; then
  echo "[pg] starting existing ${NAME}"
  docker start "${NAME}" >/dev/null
else
  echo "[pg] creating ${NAME} on host :${HOST_PORT} (image=${IMAGE})"
  docker run -d --name "${NAME}" \
    -p "${HOST_PORT}:5432" \
    -e POSTGRES_DB="${DB}" \
    -e POSTGRES_USER="${USER}" \
    -e POSTGRES_PASSWORD="${PASSWORD}" \
    "${IMAGE}" >/dev/null
fi

echo "[pg] waiting for ${NAME} to accept connections..."
for i in $(seq 1 30); do
  if docker exec "${NAME}" pg_isready -U "${USER}" -d "${DB}" >/dev/null 2>&1; then
    echo "[pg] ready"
    break
  fi
  sleep 1
done

DSN="postgresql://${USER}:${PASSWORD}@127.0.0.1:${HOST_PORT}/${DB}"
echo
echo "[pg] connection details:"
echo "  AUTH_DB_BACKEND=postgres"
echo "  POSTGRES_DSN=${DSN}"
echo
echo "[pg] next steps:"
echo "  1) python3 tools/migrate_auth_db.py \\"
echo "       --from sqlite --from-path tools/.local/auth.db \\"
echo "       --to postgres --to-dsn \"${DSN}\""
echo "  2) AUTH_DB_BACKEND=postgres POSTGRES_DSN=\"${DSN}\" \\"
echo "       python3 tools/auth_server.py 5501"
