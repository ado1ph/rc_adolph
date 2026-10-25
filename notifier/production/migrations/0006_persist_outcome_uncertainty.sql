ALTER TABLE delivery_jobs
    ADD COLUMN outcome_uncertain boolean NOT NULL DEFAULT false;

ALTER TABLE delivery_reconciliations
    ADD COLUMN retry_requested boolean,
    ADD COLUMN retry_scheduled boolean;

UPDATE delivery_reconciliations r
SET retry_requested=(r.outcome <> 'delivered'),
    retry_scheduled=(
        r.outcome='not_delivered'
        AND (j.status IN ('pending','in_flight') OR j.replay_count>0)
    )
FROM delivery_jobs j
WHERE j.id=r.notification_id;

ALTER TABLE delivery_reconciliations
    ALTER COLUMN retry_requested SET NOT NULL,
    ALTER COLUMN retry_scheduled SET NOT NULL;

WITH uncertain AS (
    UPDATE delivery_jobs j
    SET outcome_uncertain=true,
        status=CASE WHEN j.status='failed' THEN 'reconciliation_required' ELSE j.status END,
        updated_at=CASE WHEN j.status='failed'
                        THEN extract(epoch FROM clock_timestamp())::float8 ELSE j.updated_at END
    WHERE j.status='reconciliation_required'
       OR EXISTS (
            SELECT 1 FROM delivery_attempts a
            WHERE a.notification_id=j.id
              AND (a.outcome IN ('abandoned','reconciliation_required')
                   OR a.error='manual_reconciliation_required'
                   OR left(coalesce(a.error,''),10)='ambiguous_'
                   OR a.http_status IN (408,425,429)
                   OR a.http_status BETWEEN 500 AND 599)
       )
    RETURNING j.id,j.tenant,j.status,j.last_error
)
INSERT INTO delivery_audit(tenant,actor,action,notification_id,reason,created_at)
SELECT tenant,'schema-migration-v6','historical_outcome_uncertainty_preserved',id,
       'status=' || status || '; last_error=' || coalesce(last_error,''),
       extract(epoch FROM clock_timestamp())::float8
FROM uncertain;

DELETE FROM delivery_schema WHERE version=5;
INSERT INTO delivery_schema(version) VALUES (6);
