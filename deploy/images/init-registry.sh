#!/usr/bin/env bash
# Bring up a local Docker registry on http://127.0.0.1:5050 so the kind cluster
# (and bare-metal docker run) can pull zkcex images without internet access.
set -euo pipefail
cd "$(dirname "$0")"
docker compose -f registry-compose.yml up -d
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1:5050/v2/ >/dev/null; then
    echo "Registry ready at http://127.0.0.1:5050"
    exit 0
  fi
  sleep 1
done
echo "Registry did NOT come up in 30s" >&2
exit 1
