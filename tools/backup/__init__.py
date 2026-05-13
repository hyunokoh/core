"""zkCEX backup + PITR pipeline.

Modules:
  s3_client     -- stdlib AWS Signature V4 S3/MinIO client
  crypto        -- AES-256-GCM client-side envelope encryption
  sqlite_backup -- VACUUM INTO + compress + encrypt + upload for SQLite
  postgres_backup -- pg_basebackup + pg_receivewal driver
  mariadb_backup  -- mysqldump + binlog snapshot
  restore       -- generic restore CLI
  dr_drill      -- automated DR drill (backup -> mutate -> restore -> verify)
  backup_daemon -- HTTP service (port 5670) coordinating it all

All modules are stdlib-only.
"""
