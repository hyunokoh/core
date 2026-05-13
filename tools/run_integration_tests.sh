#!/usr/bin/env bash
# Run integration tests for zkCEX core services.
#
# Requirements (provided by .github/workflows/test.yml service container):
#   - A reachable Postgres on 127.0.0.1:5433 with user app / pw app-password / db zkcex_auth.
#
# Locally, you can replicate via:
#   docker run --rm -e POSTGRES_USER=app -e POSTGRES_PASSWORD=app-password \
#     -e POSTGRES_DB=zkcex_auth -p 5433:5432 postgres:16

set -euo pipefail

# Resolve script directory so the script works no matter where CI invokes it from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

export AUTH_DB_BACKEND=postgres
export POSTGRES_DSN=postgresql://app:app-password@127.0.0.1:5433/zkcex_auth
export PYTHONPATH="${SCRIPT_DIR}:${PYTHONPATH:-}"

# Start a few services for integration smoke.
AUTH_PORT=5501
python3 tools/auth_server.py "$AUTH_PORT" > /tmp/it-auth.log 2>&1 &
AUTH_PID=$!

cleanup() {
  kill "$AUTH_PID" 2>/dev/null || true
  wait "$AUTH_PID" 2>/dev/null || true
}
trap cleanup EXIT

# Wait for auth_server to come up (max 30s). In this CI harness, auth_server is
# required; a skipped pytest suite would be a false green.
AUTH_READY=0
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${AUTH_PORT}/health" > /dev/null 2>&1; then
    AUTH_READY=1
    break
  fi
  sleep 1
done

if [[ "$AUTH_READY" != "1" ]]; then
  echo "auth_server did not become healthy on port ${AUTH_PORT}" >&2
  cat /tmp/it-auth.log >&2 || true
  exit 1
fi

# Run integration scenarios.
python3 -m pytest tools/tests/integration/ -v
