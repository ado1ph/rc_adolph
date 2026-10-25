-- Run as the schema owner AFTER migration, in a dedicated database.
-- Create the login role and password through your secret-management/provisioning system first.
-- The runtime role must not own the database, schema, or tables.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE notifier TO notifier_runtime;
GRANT USAGE ON SCHEMA public TO notifier_runtime;
GRANT SELECT ON delivery_schema TO notifier_runtime;
GRANT SELECT, INSERT, UPDATE ON delivery_origins TO notifier_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON delivery_jobs, delivery_attempts,
    delivery_workers TO notifier_runtime;
GRANT SELECT, UPDATE ON delivery_capacity TO notifier_runtime;
GRANT SELECT, INSERT, DELETE ON delivery_audit TO notifier_runtime;
GRANT SELECT, INSERT, DELETE ON delivery_reconciliations TO notifier_runtime;
GRANT USAGE, SELECT ON SEQUENCE delivery_audit_id_seq TO notifier_runtime;
