ALTER TABLE delivery_reconciliations
    ADD COLUMN retry_decision_source text NOT NULL DEFAULT 'legacy_unverified';

COMMENT ON COLUMN delivery_reconciliations.retry_decision_source IS
    'legacy_unverified means retry_requested/retry_scheduled provenance cannot be established; '
    'runtime_recorded means the reconciler decision was captured by the application.';

CREATE TEMPORARY TABLE v7_outcome_uncertainty_decisions
ON COMMIT DROP AS
WITH uncertain_jobs AS MATERIALIZED (
    SELECT id,tenant,status
    FROM delivery_jobs
    WHERE outcome_uncertain
), attempt_summary AS MATERIALIZED (
    SELECT j.id,
           max(a.finished_at) AS max_finished_at,
           bool_or(a.started_at IS NULL OR a.finished_at IS NULL) AS has_missing_time
    FROM uncertain_jobs j
    LEFT JOIN delivery_attempts a ON a.notification_id=j.id
    GROUP BY j.id
), last_ambiguity AS MATERIALIZED (
    SELECT DISTINCT ON (a.notification_id)
           a.notification_id,a.number,a.started_at,a.finished_at
    FROM delivery_attempts a
    JOIN uncertain_jobs j ON j.id=a.notification_id
    WHERE a.outcome IN ('abandoned','reconciliation_required')
       OR a.error='manual_reconciliation_required'
       OR left(coalesce(a.error,''),10)='ambiguous_'
       OR a.http_status IN (408,425,429)
       OR a.http_status BETWEEN 500 AND 599
    ORDER BY a.notification_id,a.number DESC
), last_success AS MATERIALIZED (
    SELECT DISTINCT ON (a.notification_id)
           a.notification_id,a.number,a.started_at,a.finished_at
    FROM delivery_attempts a
    JOIN last_ambiguity u ON u.notification_id=a.notification_id
    WHERE a.outcome='succeeded'
      AND a.number>u.number
    ORDER BY a.notification_id,a.number DESC
), reconciliation_summary AS MATERIALIZED (
    SELECT r.notification_id,
           count(DISTINCT CASE
               WHEN r.outcome LIKE 'not_delivered%' THEN 'not_delivered'
               ELSE r.outcome
           END)>1 AS has_conflicting_outcomes
    FROM delivery_reconciliations r
    JOIN uncertain_jobs j ON j.id=r.notification_id
    GROUP BY r.notification_id
), last_reconciliation AS MATERIALIZED (
    SELECT DISTINCT ON (r.notification_id)
           r.notification_id,r.id,r.outcome,r.created_at,
           r.retry_requested,r.retry_scheduled,r.retry_decision_source
    FROM delivery_reconciliations r
    JOIN uncertain_jobs j ON j.id=r.notification_id
    ORDER BY r.notification_id,r.created_at DESC,r.id DESC
), evidence AS MATERIALIZED (
    SELECT j.id,j.tenant,j.status,
           u.number AS ambiguous_attempt_number,
           u.started_at AS ambiguous_started_at,
           u.finished_at AS ambiguous_finished_at,
           s.number AS success_attempt_number,
           s.started_at AS success_started_at,
           s.finished_at AS success_finished_at,
           a.max_finished_at,a.has_missing_time,
           r.id AS reconciliation_id,r.outcome AS reconciliation_outcome,
           r.created_at AS reconciliation_at,
           r.retry_requested,r.retry_scheduled,r.retry_decision_source,
           coalesce(rs.has_conflicting_outcomes,false) AS has_conflicting_outcomes,
           (
               j.status='succeeded'
               AND u.number IS NOT NULL
               AND s.number>u.number
               AND NOT a.has_missing_time
               AND u.finished_at IS NOT NULL
               AND s.started_at>u.finished_at
               AND s.finished_at>s.started_at
               AND NOT coalesce(rs.has_conflicting_outcomes,false)
               AND NOT EXISTS (
                   SELECT 1
                   FROM delivery_reconciliations x
                   WHERE x.notification_id=j.id
                     AND x.outcome LIKE 'not_delivered%'
                     AND x.created_at>=s.finished_at
               )
           ) AS resolved_by_later_success,
           (
               j.status='succeeded'
               AND u.number IS NOT NULL
               AND r.outcome='delivered'
               AND NOT a.has_missing_time
               AND a.max_finished_at IS NOT NULL
               AND r.created_at>a.max_finished_at
               AND NOT EXISTS (
                   SELECT 1
                   FROM delivery_attempts x
                   WHERE x.notification_id=j.id
                     AND x.started_at>=r.created_at
               )
               AND NOT coalesce(rs.has_conflicting_outcomes,false)
           ) AS resolved_by_delivered_reconciliation,
           (
               j.status='pending'
               AND u.number IS NOT NULL
               AND r.outcome LIKE 'not_delivered%'
               AND NOT a.has_missing_time
               AND a.max_finished_at IS NOT NULL
               AND r.created_at>a.max_finished_at
               AND NOT EXISTS (
                   SELECT 1
                   FROM delivery_attempts x
                   WHERE x.notification_id=j.id
                     AND x.started_at>=r.created_at
               )
               AND NOT coalesce(rs.has_conflicting_outcomes,false)
           ) AS resolved_for_existing_pending_schedule,
           (
               j.status='failed'
               AND u.number IS NOT NULL
               AND r.outcome LIKE 'not_delivered%'
               AND NOT a.has_missing_time
               AND a.max_finished_at IS NOT NULL
               AND r.created_at>a.max_finished_at
               AND NOT EXISTS (
                   SELECT 1
                   FROM delivery_attempts x
                   WHERE x.notification_id=j.id
                     AND x.started_at>=r.created_at
               )
               AND NOT coalesce(rs.has_conflicting_outcomes,false)
           ) AS resolved_for_existing_failed_terminal
    FROM uncertain_jobs j
    LEFT JOIN attempt_summary a ON a.id=j.id
    LEFT JOIN last_ambiguity u ON u.notification_id=j.id
    LEFT JOIN last_success s ON s.notification_id=j.id
    LEFT JOIN last_reconciliation r ON r.notification_id=j.id
    LEFT JOIN reconciliation_summary rs ON rs.notification_id=j.id
)
SELECT *,
       CASE
           WHEN resolved_by_later_success THEN 'later_success_attempt'
           WHEN resolved_by_delivered_reconciliation THEN 'delivered_reconciliation'
           WHEN resolved_for_existing_pending_schedule THEN 'not_delivered_pending_schedule'
           WHEN resolved_for_existing_failed_terminal THEN 'not_delivered_failed_terminal'
           WHEN status IN ('failed','succeeded') THEN 'manual_reconciliation'
           ELSE 'preserve_current_state'
       END AS action
FROM evidence;

UPDATE delivery_jobs j
SET outcome_uncertain=false
FROM v7_outcome_uncertainty_decisions d
WHERE d.id=j.id
  AND j.outcome_uncertain
  AND d.action IN (
      'later_success_attempt',
      'delivered_reconciliation',
      'not_delivered_pending_schedule',
      'not_delivered_failed_terminal'
  );

INSERT INTO delivery_audit(tenant,actor,action,notification_id,reason,created_at)
SELECT tenant,'schema-migration-v7','historical_outcome_uncertainty_cleared',id,
       action
       || '; attempt=' || coalesce(success_attempt_number::text,'')
       || '; reconciliation=' || coalesce(reconciliation_id::text,'')
       || '; retry_decision_source=' || coalesce(retry_decision_source,'legacy_unverified'),
       extract(epoch FROM clock_timestamp())::float8
FROM v7_outcome_uncertainty_decisions
WHERE action IN (
    'later_success_attempt',
    'delivered_reconciliation',
    'not_delivered_pending_schedule',
    'not_delivered_failed_terminal'
);

UPDATE delivery_jobs j
SET status='reconciliation_required',
    updated_at=extract(epoch FROM clock_timestamp())::float8
FROM v7_outcome_uncertainty_decisions d
WHERE d.id=j.id
  AND j.outcome_uncertain
  AND j.status IN ('failed','succeeded')
  AND d.action='manual_reconciliation';

INSERT INTO delivery_audit(tenant,actor,action,notification_id,reason,created_at)
SELECT tenant,'schema-migration-v7','historical_outcome_requires_reconciliation',id,
       'prior_status=' || status
       || '; ambiguous_attempt=' || coalesce(ambiguous_attempt_number::text,'')
       || '; latest_reconciliation=' || coalesce(reconciliation_id::text,'')
       || '; retry_decision_source=' || coalesce(retry_decision_source,'legacy_unverified'),
       extract(epoch FROM clock_timestamp())::float8
FROM v7_outcome_uncertainty_decisions
WHERE action='manual_reconciliation';

DELETE FROM delivery_schema WHERE version=6;
INSERT INTO delivery_schema(version) VALUES (7);
