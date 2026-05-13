#!/usr/bin/env bash
# Bootstrap a local MinIO instance for the zkCEX backup pipeline.
#
# This is the demo's object-store target. Single-node, no replication,
# no off-region copy. Production needs *much* more: see RUNBOOK.md.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="${HERE}/minio-compose.yml"

echo "[minio] bringing up zkcex-minio container..."
docker compose -f "${COMPOSE_FILE}" up -d

echo "[minio] waiting for MinIO to accept connections..."
for i in $(seq 1 30); do
  if curl -fs http://localhost:9000/minio/health/live >/dev/null 2>&1; then
    echo "[minio] live"
    break
  fi
  sleep 1
done

# Use the mc client baked into the image to bootstrap the bucket.
docker exec zkcex-minio /bin/sh -c '
  mc alias set local http://localhost:9000 zkcex zkcex-backup-demo >/dev/null 2>&1
  if ! mc ls local/zkcex-backups >/dev/null 2>&1; then
    mc mb local/zkcex-backups
    mc anonymous set none local/zkcex-backups
    mc version enable local/zkcex-backups
  fi
  mc ls local/
' || echo "[minio] (bucket bootstrap had warnings; this is fine on re-runs)"

cat <<EOF

[minio] ready
   Console : http://localhost:9001  (user: zkcex / pass: zkcex-backup-demo)
   S3 API  : http://localhost:9000
   Bucket  : zkcex-backups          (versioning: enabled)

Next:
   python3 tools/backup/backup_daemon.py 5670 &
   curl -s http://localhost:5670/backup/health
EOF
