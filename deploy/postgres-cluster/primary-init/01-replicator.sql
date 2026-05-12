-- Create replication user and a physical replication slot for the warm standby
-- in region-b. The slot name `replica_a_slot` lines up with the Helm chart's
-- region-a primary configuration.
CREATE USER replicator WITH REPLICATION ENCRYPTED PASSWORD 'replicator-password';
SELECT pg_create_physical_replication_slot('replica_a_slot');
