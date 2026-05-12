# Postgres production hardening notes

The `AUTH_DB_BACKEND=postgres` path in `tools/auth_db.py` runs against a
single `zkcex-postgres-auth` container by default. Real exchanges run a
multi-replica, backed-up cluster behind a connection pooler. This file
sketches the recipe so the demo can be promoted without re-architecting
the application layer; nothing here is implemented in the demo build.

## 1. Streaming replication (HA reads, basic DR)

* 1 primary + N read replicas using PostgreSQL physical replication.
* Primary `postgresql.conf`:
  * `wal_level = replica` (or `logical` if you want to feed downstream
    consumers — not needed for HA alone).
  * `max_wal_senders = 10` (room for replicas + pgBackRest).
  * `max_replication_slots = 10` (one slot per replica + one per backup
    tool to prevent the primary from recycling WAL too eagerly).
  * `wal_keep_size = 1GB` as a safety margin in addition to the slots.
* Replica `postgresql.conf`:
  * `hot_standby = on` so reads are served while replay continues.
  * `primary_conninfo` pointing at the primary; use a replication-only
    role (`CREATE ROLE replicator REPLICATION LOGIN ...`).
* `pg_hba.conf` on the primary: allow `replication` from the replica
  subnet only, with `scram-sha-256` and TLS required.

The auth_server today opens short-lived connections per call. In the
multi-replica world the application would split read-only traffic
(`/auth/me`, `/auth/health`, `lookup_session`) onto a separate DSN
pointing at the read pool, and keep writes on the primary. The facade
methods are already partitioned by intent (every `_exec()` is a write,
every `_fetchone/_fetchall()` is a read), so this split is a small
refactor.

## 2. Connection pooling

* PgBouncer in **transaction** pooling mode in front of the primary and
  in front of the read pool.
* Set `pool_mode = transaction`, `max_client_conn = 2000`,
  `default_pool_size = 25`. With short-lived `psycopg/pg8000`
  connections from the app, transaction-mode pooling is a 50x reduction
  in actual Postgres backends.
* Stick PgBouncer next to the application (sidecar pattern) so the TCP
  hop is local.

## 3. Backups + point-in-time recovery (PITR)

* Use **pgBackRest** (preferred for richer ops tooling) or **WAL-G**.
* Schedule:
  * Daily **full** backup, retention 7 days.
  * Hourly **differential** backup, retention 24 h.
  * Continuous **WAL streaming** to the same archive.
* Target: an S3-compatible object store (MinIO is fine on-prem). Both
  tools encrypt at rest with a customer-managed key.
* Validate restorability weekly with `pgbackrest --stanza=auth --type=time
  '... ' restore` against a scratch box and run a checksum on the users
  table. A backup you have not tested is a wish, not a backup.

## 4. Failover automation

Two viable choices; pick one and stick with it:

* **Patroni** + etcd/Consul as the DCS. Mature, opinionated, good
  documentation. Switchover is a single `patronictl switchover` call.
* **pg_auto_failover** from Citus. Lighter weight, no external DCS
  required (uses a separate "monitor" Postgres). Simpler to bootstrap
  on K8s.

In either case the application config should point at a virtual hostname
(VIP, k8s Service, or a `multi-host` pgbouncer DSN) so a leader change
does not require a redeploy.

## 5. TLS + auth

* Server certificate signed by the cluster CA, validity 90 days,
  rotated by cert-manager.
* Force `ssl = on`, `password_encryption = scram-sha-256`,
  `ssl_min_protocol_version = TLSv1.2`.
* For the auth_server connection, prefer **client-cert auth** (`cert`
  in `pg_hba.conf`) over a long-lived password. The cert is issued by
  the same internal CA and rotated alongside the workload.
* For DB-to-DB replication links, require client certs as well.

## 6. Schema-migration tooling

The current `init_schema()` is idempotent CREATE-IF-NOT-EXISTS, which is
fine for the v1 demo but does not handle column drops, type changes, or
data backfills. Adopt one of:

* **Sqitch** (declarative SQL change scripts, lightweight).
* **Flyway** (well-known, but JVM and proprietary tier for some
  features).
* **Alembic** (Python; pairs nicely with the rest of the stack).

The expected migration here is to replace the file-loaded
`auth_schema/postgres.sql` with a numbered changelog
(`migrations/0001_init.sql`, `0002_add_kyc_audit.sql`, ...) and run the
chosen tool from CI before each deploy. The application keeps a
read-only contract on the schema; it does not run DDL itself in
production.

## 7. Observability

Out of scope for this file but worth pinning:

* `pg_stat_statements` enabled, scraped by Prometheus
  (postgres_exporter) at 30 s.
* Replication lag alert at > 10 s WAL bytes behind primary.
* Slow-query log at `log_min_duration_statement = 250ms`.
* Connection-saturation alert: `numbackends / max_connections > 0.7`
  for 5 min.

## What this file is *not*

It is documentation only. There is no Helm chart, no Terraform, no
Ansible play in the demo. The point is to show the next reviewer that
the abstraction in `auth_db.py` is *compatible* with the production
shape above — no application code changes are required to bolt on
replication, PgBouncer, backups, or failover.
