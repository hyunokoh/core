# zkCEX Backup + PITR Runbook

This document covers operating the backup pipeline:
**MinIO + backup_daemon (port 5670) + Postgres/MariaDB/SQLite drivers**.

It is the **on-call runbook** for the backup subsystem. Everything here
should be executable by someone who has read access to this repo, the
MinIO admin credentials, and an admin Bearer token for the backup
daemon.

## Targets

| Concern | Target |
| ------- | ------ |
| RPO -- Postgres auth | 5 min (continuous WAL streaming) |
| RPO -- MariaDB zkPoL | 1 hour (hourly binlog snapshot) |
| RPO -- SQLite | 24 hours (nightly VACUUM INTO) |
| RPO -- custody/key state | 24 hours / 7 days |
| RTO -- single DB | 30 min |
| RTO -- full cluster | 4 hours |
| DR drill cadence | quarterly + automated nightly |
| Encryption | client-side AES-256 + HMAC-SHA256 (envelope) |
| Bucket retention | 30d full / 7d WAL / 90d custody / 1y signing key |

## What is being backed up

* **Postgres `zkcex-postgres-auth`** -- full base backup nightly +
  continuous WAL streaming via `pg_receivewal --slot=zkcex_backup_slot`.
  Also a daily `pg_dump --format=custom` for the PITR-lite fallback path.
* **MariaDB `zkpol-mariadb`** -- `mariadb-dump --single-transaction
  --master-data=2` nightly + hourly binlog snapshot.
* **SQLite under `tools/.local/`** -- `auth.db`, `chain.db`, `perp.db`,
  `order_engine.db`, `mm_bot.db`, `safu.db`, `push.db`, `mcp_calls.db`,
  `custody_audit.db`, `api_keys.db`, `ops.db`, `travel_rule.db`,
  `zk_orderbook.db`, `zkpol_bridge.db`, `pol-snapshot.db`, `pol.db`.
  Method: `VACUUM INTO` -> gzip -> AES envelope -> upload. No writer
  lock taken.
* **Chain state** -- custody shamir shares (`custody/shard_*.bin`),
  `pol_signing_key`, `vapid.json`, `pol_snapshot_feed_secret`.

## Standard operations

### Bring up the stack (one-time, then idempotent)

```bash
# 1. MinIO object store
bash deploy/backup/init-minio.sh

# 2. Backup daemon (port 5670)
python3 tools/backup/backup_daemon.py 5670 &

# 3. Health check
curl -s http://localhost:5670/backup/health | jq .
```

The daemon prints its admin token on first start (`BACKUP_ADMIN_TOKEN=...`).
Set `BACKUP_ADMIN_TOKEN` in env to make it persistent across restarts.

### Trigger an ad-hoc backup

```bash
TOKEN="$BACKUP_ADMIN_TOKEN"
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  http://localhost:5670/backup/run/sqlite-auth.db | jq .
```

Valid sources: anything in `SOURCE_DISPATCH` (see `GET /backup/sources`).

### List recent jobs

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:5670/backup/jobs?limit=20" | jq '.jobs[] | {ts, source, type, status, bytes_uploaded, duration_ms}'
```

### Inspect storage

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:5670/backup/storage | jq .
```

### Restore a single SQLite DB

```bash
python3 tools/backup/restore.py \
  --source sqlite-auth.db \
  --dest /tmp/auth-restored.db
sqlite3 /tmp/auth-restored.db "SELECT count(*) FROM users"
```

The restore tool picks the most-recent backup whose `created_at` is
<= the optional `--target-time` (ISO-8601). Default = latest.

### Restore a single user's data

```bash
# 1) restore auth.db to a tmpfile
python3 tools/backup/restore.py --source sqlite-auth.db --dest /tmp/auth-pit.db
# 2) extract that user's row(s)
sqlite3 /tmp/auth-pit.db "SELECT * FROM users WHERE email='alice@example.com'"
# 3) re-insert into live via the auth_server admin API (do NOT touch
#    /tmp/auth-pit.db -> auth.db directly; the live DB has WAL state
#    that a file copy would corrupt).
```

### Postgres PITR (logical path -- simple)

```bash
python3 tools/backup/restore.py \
  --source postgres-auth \
  --target-time 2026-05-12T03:14:00Z \
  --mode logical \
  --dest zkcex_auth_pit
```

The daemon downloads the most-recent encrypted `pg_dump` <= the target
time, decrypts in-memory, and pipes it into `pg_restore` against a new
database (`zkcex_auth_pit`) inside the live container. The original
`zkcex_auth` is untouched.

### Postgres PITR (physical path -- full WAL replay)

```bash
python3 tools/backup/restore.py \
  --source postgres-auth \
  --mode physical \
  --target-time 2026-05-12T03:14:00Z \
  --dest /tmp/zkcex-pg-pit
# Then bring up a new postgres pointed at /tmp/zkcex-pg-pit
docker run --rm -d --name zkcex-pg-pit \
  -v /tmp/zkcex-pg-pit:/var/lib/postgresql/data \
  -e POSTGRES_USER=app -e POSTGRES_PASSWORD=app-password \
  -p 5434:5432 postgres:16
# It will refuse to start with archive recovery enabled by default;
# `touch /tmp/zkcex-pg-pit/recovery.signal` is already there. Postgres
# will replay WAL until `recovery_target_time` then transition to a
# normal DB.
```

### Restore the entire stack after a total cluster loss

This is the **disaster** path. Assumes MinIO is intact (off-region
replica, see "What still isn't here" below).

1. Bring up a fresh host with the zkCEX repo checked out.
2. Bring up the dependency containers:
   ```bash
   bash tools/auth_server_pg_bootstrap.sh        # postgres-auth
   docker compose -f core/docker-compose.yml up -d zkpol-mariadb
   ```
3. Bring up MinIO (point at the existing data volume / off-region copy).
4. Copy the master encryption key from key escrow to
   `tools/.local/backup_encryption.key` (chmod 0600).
5. Start the backup daemon.
6. Restore each source:
   ```bash
   # SQLite
   for src in sqlite-auth.db sqlite-chain.db sqlite-perp.db sqlite-order_engine.db \
              sqlite-safu.db sqlite-push.db sqlite-ops.db sqlite-mm_bot.db \
              sqlite-api_keys.db sqlite-custody_audit.db sqlite-zk_orderbook.db \
              sqlite-zkpol_bridge.db sqlite-travel_rule.db sqlite-mcp_calls.db; do
     python3 tools/backup/restore.py --source $src --dest tools/.local/$(echo $src | sed 's/^sqlite-//')
   done

   # Postgres
   python3 tools/backup/restore.py --source postgres-auth --mode logical --dest zkcex_auth

   # MariaDB
   python3 tools/backup/restore.py --source mariadb-zkpol --dest zk_pol

   # Chain state
   for s in chain-custody_shard_0.bin chain-custody_shard_1.bin chain-custody_shard_2.bin \
            chain-custody_shard_3.bin chain-custody_shard_4.bin chain-custody_public.json \
            chain-pol_signing_key chain-vapid.json; do
     dest=$(echo $s | sed 's/^chain-//' | sed 's/_/\//')
     python3 tools/backup/restore.py --source $s --dest tools/.local/$dest
   done
   ```
7. Restart the application servers (`auth_server.py`, `chain_server.py`, etc.).
8. Run the DR drill to confirm the restored state is sane:
   ```bash
   python3 tools/backup/dr_drill.py
   ```
9. **RTO target: 4 hours.** If you're past that, escalate.

## Testing

### Manual DR drill

```bash
python3 tools/backup/dr_drill.py
# Exit code 0 = PASS, 1 = FAIL. Logs include per-step timings.
```

The drill clones the live `auth.db` to a sandbox, runs the same backup
path used in production, mutates the clone, restores, and asserts:

  * the probe user added AFTER backup is NOT in the restored DB
  * a known user FROM BEFORE the backup IS in the restored DB
  * row counts match the pre-mutation state

### Quarterly auto-drill

Schedule the daemon's `POST /backup/drill` endpoint via cron:

```cron
0 4 1 */3 * curl -s -X POST -H "Authorization: Bearer $BACKUP_ADMIN_TOKEN" \
  http://localhost:5670/backup/drill | tee /var/log/zkcex/dr-drills/$(date +\%F).json
```

Alert on `result != "PASS"`. The drill is short and idempotent.

## Key rotation

The master key at `tools/.local/backup_encryption.key` is 32 random
bytes. To rotate:

1. Generate a new key: `head -c 32 /dev/urandom > /tmp/new.key && chmod 0600 /tmp/new.key`.
2. For each object in MinIO under `zkcex-backups/`:
   - download
   - `decrypt(old_key, blob)`
   - `encrypt(new_key, plaintext)`
   - upload with a `*.k2.enc` suffix
3. Verify a sample restore from a `*.k2.enc` object.
4. Replace `tools/.local/backup_encryption.key` with the new key on
   all backup nodes.
5. Update the inventory: `UPDATE backup_inventory SET object_key=REPLACE(object_key,'.enc','.k2.enc')`.
6. Garbage-collect old objects after the retention window.

In production: this key lives in HSM or KMS, **not** on disk. The
backup daemon would call the KMS to wrap/unwrap a per-object data
encryption key (envelope encryption pattern). The current implementation
keeps the key on disk for demo convenience and documents this gap.

## If MinIO is unreachable

The daemon will:
* Mark in-flight jobs `failed` with the underlying error.
* `GET /backup/health` will return `minio_live=false`.
* The scheduler keeps trying every minute -- no manual reset needed.

Operator steps:
1. `docker ps | grep zkcex-minio` -- if the container is gone:
   `bash deploy/backup/init-minio.sh`
2. If the volume is gone: this is the "lose the bucket" disaster.
   Restore MinIO from the off-region replica (production) or accept
   data loss back to the most recent off-site copy.
3. Until MinIO recovers, **freeze migrations and schema changes** so
   the application state stays in a recoverable shape.

## What still isn't here (honest gaps)

The demo deliberately stops short of full production-grade. In
production the following is required and absent here:

* **Off-region S3 + cross-region replication.** Single-host MinIO is a
  single point of failure. Real deployments push to two geographically
  separated buckets with versioning + replication enabled.
* **Object Lock for ransomware resilience.** Bucket-level Object Lock
  in compliance mode prevents an attacker (or insider) from deleting
  backups during the retention window even with full IAM rights.
* **KMS-backed envelope encryption.** Key on disk is not a real
  control. Move to AWS KMS / GCP KMS / a real HSM; the daemon should
  wrap a per-object DEK with the KMS-managed CMK.
* **Regulator-compliant retention.** Different jurisdictions impose
  7-year (MiFID II), 5-year (BSA), or 10-year (some EU member states)
  retention floors. Configure per-source.
* **Game days.** Quarterly DR drill is the floor; pen-test-style
  game days where the SRE team is given a synthetic disaster (zone
  outage, KMS unavailable, encryption key lost) and timed to recover
  are also required.
* **Multipart upload + streaming compression.** Right now we hold the
  whole tar/dump in memory. For >5 GB Postgres clusters this needs
  streaming via S3 multipart and chunked encryption.
* **Backup of accountant / eventlog / wallet / market / api / bc-gateway
  Postgres databases** -- the docker-compose stack has six more
  Postgres instances; we cover only `postgres-auth`. Same pipeline,
  add entries to `SOURCE_DISPATCH`.
* **WAL streamer durability**. The current streamer uploads on a 5s
  poll. Production needs `archive_command` direct from postgres so a
  segment is uploaded *before* it is recycled.
* **Restore time validation.** RTO numbers in this runbook are
  estimates from manual testing; production needs continuous restore
  drills that time the full path.

## Incident playbook -- backup failures

A failed backup means **RPO is growing**. Steps:

1. Find the failing source: `GET /backup/jobs?limit=20`, filter by
   `status="failed"`.
2. Read the `error` column.
3. Common causes:
   * MinIO unreachable -- see above.
   * `wal_level != replica` -- run
     `docker exec zkcex-postgres-auth psql -U app -c "ALTER SYSTEM SET wal_level='replica';"`
     and restart the container.
   * Replication slot full -- `psql -c "SELECT * FROM pg_replication_slots;"`,
     and drop+recreate the slot if WAL has accumulated on the primary.
   * `VACUUM INTO` fails -- usually a stale lock. Restart the app
     server that holds an open writer connection.
4. Trigger an immediate retry: `POST /backup/run/<source>`.
5. If retry fails, escalate to the on-call SRE.

## Schedule reference

| Source | Frequency | Retention |
| ------ | --------- | --------- |
| postgres-auth (base) | daily 02:00 UTC | 30 days |
| postgres-auth (WAL) | continuous | 7 days |
| postgres-auth (logical) | daily 02:00 UTC | 30 days |
| mariadb-zkpol (full) | daily 02:30 UTC | 30 days |
| mariadb-zkpol (binlog) | hourly | 7 days |
| sqlite-* | daily 03:00 UTC | 30 days |
| custody-shares | daily 03:30 UTC | 90 days |
| pol-signing-key | weekly Sun 04:00 UTC | 365 days |

Override any schedule with `BACKUP_SCHEDULE_<SOURCE>=HH:MM` in the
daemon environment (use upper-snake-case; e.g.
`BACKUP_SCHEDULE_POSTGRES_FULL=01:30`).
