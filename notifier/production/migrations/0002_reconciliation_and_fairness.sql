ALTER TABLE delivery_origins
    ADD COLUMN last_tenant text NOT NULL DEFAULT '';

ALTER TABLE delivery_jobs
    ADD COLUMN key_window_seconds bigint;

ALTER TABLE delivery_jobs
    DROP CONSTRAINT delivery_jobs_status_check;

ALTER TABLE delivery_jobs
    ADD CONSTRAINT delivery_jobs_status_check
    CHECK (status IN ('pending','in_flight','succeeded','failed','reconciliation_required'));

UPDATE delivery_jobs
SET status='reconciliation_required',updated_at=extract(epoch FROM clock_timestamp())
WHERE status='failed' AND last_error='manual_reconciliation_required';

UPDATE delivery_attempts a
SET outcome='reconciliation_required'
FROM delivery_jobs j
WHERE j.id=a.notification_id AND a.number=j.attempt_count
  AND j.status='reconciliation_required'
  AND j.last_error='manual_reconciliation_required';

INSERT INTO delivery_audit(tenant,actor,action,notification_id,reason,created_at)
SELECT tenant,'schema-migration-v2','migration_reconciliation_required',id,
       'legacy_ambiguous_supplier_result',extract(epoch FROM clock_timestamp())
FROM delivery_jobs
WHERE status='reconciliation_required'
  AND last_error='manual_reconciliation_required';

CREATE TABLE delivery_reconciliations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    notification_id uuid NOT NULL REFERENCES delivery_jobs(id) ON DELETE CASCADE,
    tenant text NOT NULL,
    actor text NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('delivered','not_delivered','not_delivered_new_key_required')),
    evidence_ref text NOT NULL,
    reason text NOT NULL,
    created_at double precision NOT NULL
);

CREATE INDEX delivery_reconciliations_job
    ON delivery_reconciliations(notification_id,id);

DELETE FROM delivery_schema WHERE version = 1;
INSERT INTO delivery_schema(version) VALUES (2);
