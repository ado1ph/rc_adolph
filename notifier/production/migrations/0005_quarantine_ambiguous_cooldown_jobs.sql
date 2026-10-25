WITH quarantined AS (
    UPDATE delivery_jobs
    SET status='reconciliation_required',
        last_error='legacy_paused_manual_outcome_unknown',
        updated_at=extract(epoch FROM clock_timestamp())::float8
    WHERE status='paused' AND retry_mode='manual_on_ambiguous'
    RETURNING id,tenant,attempt_count
)
UPDATE delivery_attempts a
SET outcome='reconciliation_required',
    error='legacy_paused_manual_outcome_unknown'
FROM quarantined q
WHERE a.notification_id=q.id AND a.number=q.attempt_count;

INSERT INTO delivery_audit(tenant,actor,action,notification_id,reason,created_at)
SELECT tenant,'schema-migration-v5',
       'legacy_paused_manual_outcome_quarantined',id,
       'Legacy paused status cannot prove supplier non-delivery',
       extract(epoch FROM clock_timestamp())::float8
FROM delivery_jobs
WHERE status='reconciliation_required'
  AND last_error='legacy_paused_manual_outcome_unknown';

DELETE FROM delivery_schema WHERE version=4;
INSERT INTO delivery_schema(version) VALUES (5);
