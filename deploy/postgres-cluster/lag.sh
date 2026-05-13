#!/usr/bin/env bash
# Print replication lag in bytes and wall-clock time.
# Healthy idle cluster shows send_lag_bytes=0, flush_lag_bytes=0, replay_lag_bytes=0.
docker exec zkcex-pg-primary psql -U app -d zkcex_auth -c \
  "SELECT application_name, client_addr, state, sync_state,
          pg_wal_lsn_diff(pg_current_wal_lsn(), sent_lsn)   AS send_lag_bytes,
          pg_wal_lsn_diff(pg_current_wal_lsn(), flush_lsn)  AS flush_lag_bytes,
          pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS replay_lag_bytes,
          write_lag, flush_lag, replay_lag
   FROM pg_stat_replication;"
