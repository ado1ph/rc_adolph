ALTER TABLE delivery_origins
    ADD COLUMN cooldown_hold boolean NOT NULL DEFAULT false,
    ADD COLUMN cooldown_reason text;

ALTER TABLE delivery_jobs
    DROP CONSTRAINT delivery_jobs_status_check;

ALTER TABLE delivery_jobs
    ADD CONSTRAINT delivery_jobs_status_check
    CHECK (status IN (
        'pending','in_flight','succeeded','failed','paused','reconciliation_required'
    ));

UPDATE delivery_origins o
SET cooldown_hold=true,
    cooldown_reason='legacy_non_finite_retry_after',
    next_allowed=CASE
        WHEN o.next_allowed IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8) THEN 0
        ELSE o.next_allowed
    END
WHERE o.next_allowed IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8)
   OR EXISTS (
       SELECT 1 FROM delivery_jobs j
       WHERE j.origin=o.origin
         AND j.next_attempt_at IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8)
   );

UPDATE delivery_origins
SET last_claimed=CASE
        WHEN last_claimed IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8) THEN 0
        ELSE last_claimed
    END,
    open_until=CASE
        WHEN open_until IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8) THEN 0
        ELSE open_until
    END;

WITH repaired AS (
    UPDATE delivery_jobs
    SET status=CASE
            WHEN status='in_flight' OR attempt_count>0 THEN 'reconciliation_required'
            ELSE 'failed'
        END,
        deadline=CASE
            WHEN deadline IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8)
                THEN extract(epoch FROM clock_timestamp())::float8
            ELSE deadline
        END,
        key_expires_at=CASE
            WHEN key_expires_at IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8)
                THEN extract(epoch FROM clock_timestamp())::float8
            ELSE key_expires_at
        END,
        next_attempt_at=CASE
            WHEN next_attempt_at IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8)
                THEN extract(epoch FROM clock_timestamp())::float8
            ELSE next_attempt_at
        END,
        created_at=CASE
            WHEN created_at IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8)
                THEN extract(epoch FROM clock_timestamp())::float8
            ELSE created_at
        END,
        lease_token=NULL,
        lease_until=NULL,
        last_error='legacy_non_finite_schedule',
        updated_at=extract(epoch FROM clock_timestamp())::float8
    WHERE deadline IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8)
       OR key_expires_at IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8)
    RETURNING id,tenant,origin,status
)
INSERT INTO delivery_audit(tenant,actor,action,notification_id,reason,created_at)
SELECT tenant,'schema-migration-v4','non_finite_schedule_repaired',id,
       'legacy_non_finite_schedule',extract(epoch FROM clock_timestamp())::float8
FROM repaired;

UPDATE delivery_jobs
SET lease_until=0
WHERE lease_until IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8);

WITH repaired AS (
    UPDATE delivery_jobs
    SET status=CASE
            WHEN status='in_flight' THEN 'reconciliation_required'
            WHEN status='pending' THEN 'paused'
            ELSE status
        END,
        next_attempt_at=extract(epoch FROM clock_timestamp())::float8,
        lease_token=CASE WHEN status='in_flight' THEN NULL ELSE lease_token END,
        lease_until=CASE WHEN status='in_flight' THEN NULL ELSE lease_until END,
        created_at=CASE
            WHEN created_at IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8)
                THEN extract(epoch FROM clock_timestamp())::float8
            ELSE created_at
        END,
        last_error=CASE
            WHEN status IN ('pending','in_flight') THEN 'legacy_retry_after_hold'
            ELSE last_error
        END,
        updated_at=extract(epoch FROM clock_timestamp())::float8
    WHERE next_attempt_at IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8)
       OR created_at IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8)
       OR updated_at IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8)
    RETURNING id,tenant,status,last_error
)
INSERT INTO delivery_audit(tenant,actor,action,notification_id,reason,created_at)
SELECT tenant,'schema-migration-v4','non_finite_schedule_repaired',id,
       'legacy_retry_after_hold',extract(epoch FROM clock_timestamp())::float8
FROM repaired;

UPDATE delivery_attempts
SET started_at=CASE
        WHEN started_at IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8)
            THEN extract(epoch FROM clock_timestamp())::float8
        ELSE started_at
    END,
    finished_at=CASE
        WHEN finished_at IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8)
            THEN extract(epoch FROM clock_timestamp())::float8
        ELSE finished_at
    END;

UPDATE delivery_attempts a
SET outcome='reconciliation_required',
    finished_at=coalesce(a.finished_at,extract(epoch FROM clock_timestamp())::float8),
    error='legacy_non_finite_schedule'
FROM delivery_jobs j
WHERE j.id=a.notification_id AND j.status='reconciliation_required'
  AND a.outcome='in_flight';

UPDATE delivery_audit
SET created_at=extract(epoch FROM clock_timestamp())::float8
WHERE created_at IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8);

UPDATE delivery_reconciliations
SET created_at=extract(epoch FROM clock_timestamp())::float8
WHERE created_at IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8);

UPDATE delivery_workers
SET last_seen=0
WHERE last_seen IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8);

UPDATE delivery_origins o
SET in_flight=(
    SELECT count(*) FROM delivery_jobs j WHERE j.origin=o.origin AND j.status='in_flight'
);

DROP INDEX IF EXISTS delivery_active;
CREATE INDEX delivery_active ON delivery_jobs(tenant,status)
    WHERE status IN ('pending','in_flight','paused');

ALTER TABLE delivery_origins
    ADD CONSTRAINT delivery_origins_last_claimed_finite
        CHECK (last_claimed > '-Infinity'::float8 AND last_claimed < 'Infinity'::float8),
    ADD CONSTRAINT delivery_origins_next_allowed_finite
        CHECK (next_allowed > '-Infinity'::float8 AND next_allowed < 'Infinity'::float8),
    ADD CONSTRAINT delivery_origins_open_until_finite
        CHECK (open_until > '-Infinity'::float8 AND open_until < 'Infinity'::float8);

ALTER TABLE delivery_jobs
    ADD CONSTRAINT delivery_jobs_next_attempt_finite
        CHECK (next_attempt_at > '-Infinity'::float8 AND next_attempt_at < 'Infinity'::float8),
    ADD CONSTRAINT delivery_jobs_deadline_finite
        CHECK (deadline > '-Infinity'::float8 AND deadline < 'Infinity'::float8),
    ADD CONSTRAINT delivery_jobs_key_expiry_finite
        CHECK (key_expires_at IS NULL OR
               (key_expires_at > '-Infinity'::float8 AND key_expires_at < 'Infinity'::float8)),
    ADD CONSTRAINT delivery_jobs_lease_finite
        CHECK (lease_until IS NULL OR
               (lease_until > '-Infinity'::float8 AND lease_until < 'Infinity'::float8)),
    ADD CONSTRAINT delivery_jobs_created_finite
        CHECK (created_at > '-Infinity'::float8 AND created_at < 'Infinity'::float8),
    ADD CONSTRAINT delivery_jobs_updated_finite
        CHECK (updated_at > '-Infinity'::float8 AND updated_at < 'Infinity'::float8);

ALTER TABLE delivery_attempts
    ADD CONSTRAINT delivery_attempts_started_finite
        CHECK (started_at > '-Infinity'::float8 AND started_at < 'Infinity'::float8),
    ADD CONSTRAINT delivery_attempts_finished_finite
        CHECK (finished_at IS NULL OR
               (finished_at > '-Infinity'::float8 AND finished_at < 'Infinity'::float8));

ALTER TABLE delivery_audit
    ADD CONSTRAINT delivery_audit_created_finite
        CHECK (created_at > '-Infinity'::float8 AND created_at < 'Infinity'::float8);

ALTER TABLE delivery_reconciliations
    ADD CONSTRAINT delivery_reconciliations_created_finite
        CHECK (created_at > '-Infinity'::float8 AND created_at < 'Infinity'::float8);

ALTER TABLE delivery_workers
    ADD CONSTRAINT delivery_workers_last_seen_finite
        CHECK (last_seen > '-Infinity'::float8 AND last_seen < 'Infinity'::float8);

DELETE FROM delivery_schema WHERE version = 3;
INSERT INTO delivery_schema(version) VALUES (4);
