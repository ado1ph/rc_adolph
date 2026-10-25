WITH candidates AS MATERIALIZED (
    SELECT id,tenant,status,outcome_uncertain,last_error,lease_token,lease_until
    FROM delivery_jobs
    WHERE outcome_uncertain OR status='reconciliation_required'
), evidence AS MATERIALIZED (
    SELECT j.*,
           ambiguous.number AS latest_ambiguous_number,
           ambiguous.outcome AS latest_ambiguous_outcome,
           ambiguous.http_status AS latest_ambiguous_http_status,
           ambiguous.error AS latest_ambiguous_error,
           ambiguous.started_at AS latest_ambiguous_started_at,
           ambiguous.finished_at AS latest_ambiguous_finished_at,
           successful.number AS later_success_number,
           successful.finished_at AS later_success_finished_at,
           resolution.outcome AS latest_reconciliation_outcome,
           resolution.created_at AS latest_reconciliation_at,
           resolution.retry_requested AS latest_reconciliation_retry_requested,
           resolution.retry_scheduled AS latest_reconciliation_retry_scheduled,
           resolution.retry_decision_source AS latest_retry_decision_source,
           attempts.all_attempts,
           attempts.max_finished_at,
           attempts.has_missing_finish_time,
           attempts.has_missing_start_time,
           reconciliations.all_reconciliations,
           reconciliations.have_conflicting_outcomes,
           audits.v6_audits,
           (
               successful.number IS NOT NULL
               AND NOT attempts.has_missing_finish_time
               AND NOT attempts.has_missing_start_time
               AND successful.started_at>ambiguous.finished_at
               AND successful.finished_at>ambiguous.finished_at
               AND successful.finished_at>successful.started_at
               AND NOT reconciliations.have_conflicting_outcomes
               AND NOT EXISTS (
                   SELECT 1
                   FROM delivery_reconciliations r
                   WHERE r.notification_id=j.id
                     AND r.outcome LIKE 'not_delivered%'
                     AND r.created_at>=successful.finished_at
               )
           ) AS success_proves_resolution,
           (
               ambiguous.finished_at IS NOT NULL
               AND resolution.created_at IS NOT NULL
               AND resolution.outcome='delivered'
               AND resolution.created_at>attempts.max_finished_at
               AND NOT attempts.has_missing_finish_time
               AND NOT attempts.has_missing_start_time
               AND NOT EXISTS (
                   SELECT 1
                   FROM delivery_attempts a
                   WHERE a.notification_id=j.id
                     AND a.started_at>=resolution.created_at
               )
               AND NOT reconciliations.have_conflicting_outcomes
           ) AS reconciliation_proves_resolution,
           (
               ambiguous.finished_at IS NOT NULL
               AND resolution.created_at IS NOT NULL
               AND resolution.outcome LIKE 'not_delivered%'
               AND j.status='pending'
               AND resolution.created_at>attempts.max_finished_at
               AND NOT attempts.has_missing_finish_time
               AND NOT attempts.has_missing_start_time
               AND NOT EXISTS (
                   SELECT 1
                   FROM delivery_attempts a
                   WHERE a.notification_id=j.id
                     AND a.started_at>=resolution.created_at
               )
               AND NOT reconciliations.have_conflicting_outcomes
           ) AS pending_not_delivered_proves_resolution,
           (
               ambiguous.finished_at IS NOT NULL
               AND resolution.created_at IS NOT NULL
               AND resolution.outcome LIKE 'not_delivered%'
               AND j.status='failed'
               AND resolution.created_at>attempts.max_finished_at
               AND NOT attempts.has_missing_finish_time
               AND NOT attempts.has_missing_start_time
               AND NOT EXISTS (
                   SELECT 1
                   FROM delivery_attempts a
                   WHERE a.notification_id=j.id
                     AND a.started_at>=resolution.created_at
               )
               AND NOT reconciliations.have_conflicting_outcomes
           ) AS failed_not_delivered_proves_resolution
    FROM candidates j
    LEFT JOIN LATERAL (
        SELECT a.number,a.outcome,a.http_status,a.error,a.started_at,a.finished_at
        FROM delivery_attempts a
        WHERE a.notification_id=j.id
          AND (a.outcome IN ('abandoned','reconciliation_required')
               OR a.error='manual_reconciliation_required'
               OR left(coalesce(a.error,''),10)='ambiguous_'
               OR a.http_status IN (408,425,429)
               OR a.http_status BETWEEN 500 AND 599)
        ORDER BY a.number DESC LIMIT 1
    ) ambiguous ON true
    LEFT JOIN LATERAL (
        SELECT a.number,a.started_at,a.finished_at
        FROM delivery_attempts a
        WHERE a.notification_id=j.id AND a.outcome='succeeded'
          AND a.number>ambiguous.number AND a.finished_at IS NOT NULL
        ORDER BY a.number DESC LIMIT 1
    ) successful ON true
    LEFT JOIN LATERAL (
        SELECT r.outcome,r.created_at,r.retry_requested,r.retry_scheduled,
               coalesce(to_jsonb(r)->>'retry_decision_source','legacy_unverified')
                   AS retry_decision_source
        FROM delivery_reconciliations r
        WHERE r.notification_id=j.id
        ORDER BY r.created_at DESC,r.id DESC LIMIT 1
    ) resolution ON true
    LEFT JOIN LATERAL (
        SELECT coalesce(jsonb_agg(jsonb_build_object(
                   'number',a.number,'outcome',a.outcome,'http_status',a.http_status,
                   'error',a.error,'started_at',a.started_at,'finished_at',a.finished_at
               ) ORDER BY a.number),'[]'::jsonb) AS all_attempts,
               max(a.finished_at) AS max_finished_at,
               coalesce(bool_or(a.finished_at IS NULL),false) AS has_missing_finish_time,
               coalesce(bool_or(a.started_at IS NULL),false) AS has_missing_start_time
        FROM delivery_attempts a WHERE a.notification_id=j.id
    ) attempts ON true
    LEFT JOIN LATERAL (
        SELECT coalesce(jsonb_agg(jsonb_build_object(
                   'id',r.id,'outcome',r.outcome,'created_at',r.created_at,
                   'retry_requested',r.retry_requested,'retry_scheduled',r.retry_scheduled,
                   'retry_decision_source',
                       coalesce(to_jsonb(r)->>'retry_decision_source','legacy_unverified')
               ) ORDER BY r.created_at,r.id),'[]'::jsonb) AS all_reconciliations,
               count(DISTINCT r.outcome)>1 AS have_conflicting_outcomes
        FROM delivery_reconciliations r WHERE r.notification_id=j.id
    ) reconciliations ON true
    LEFT JOIN LATERAL (
        SELECT coalesce(jsonb_agg(jsonb_build_object(
                   'action',a.action,'reason',a.reason,'created_at',a.created_at
               ) ORDER BY a.id),'[]'::jsonb) AS v6_audits
        FROM delivery_audit a
        WHERE a.notification_id=j.id
          AND a.action='historical_outcome_uncertainty_preserved'
    ) audits ON true
)
SELECT id,tenant,status,outcome_uncertain,last_error,
       jsonb_build_object(
           'number',latest_ambiguous_number,'outcome',latest_ambiguous_outcome,
           'http_status',latest_ambiguous_http_status,'error',latest_ambiguous_error,
           'started_at',latest_ambiguous_started_at,'finished_at',latest_ambiguous_finished_at
       ) AS latest_ambiguity,
       later_success_number,later_success_finished_at,
       latest_reconciliation_outcome,latest_reconciliation_at,
       latest_reconciliation_retry_scheduled,
       all_attempts,all_reconciliations,v6_audits,
       success_proves_resolution,reconciliation_proves_resolution,
       CASE
           WHEN pending_not_delivered_proves_resolution
               THEN 'clear_uncertainty_keep_pending'
           WHEN failed_not_delivered_proves_resolution
               THEN 'clear_uncertainty_keep_failed'
           WHEN status IN ('pending','in_flight')
                AND latest_reconciliation_outcome LIKE 'not_delivered%'
                AND latest_reconciliation_retry_scheduled
                AND latest_retry_decision_source='runtime_recorded'
               THEN CASE WHEN status='pending'
                         THEN 'preserve_pending_retry_authorization'
                         ELSE 'preserve_inflight_lease_and_attempt' END
           WHEN status IN ('pending','in_flight')
                AND latest_reconciliation_outcome LIKE 'not_delivered%'
                AND latest_reconciliation_retry_scheduled
               THEN CASE WHEN status='pending'
                         THEN 'preserve_pending_unverified_retry_intent'
                         ELSE 'preserve_inflight_lease_unverified_retry_intent' END
           WHEN status='paused'
               THEN 'preserve_paused_state_and_uncertainty'
           WHEN status='succeeded' AND success_proves_resolution
               THEN 'clear_uncertainty_keep_succeeded'
           WHEN reconciliation_proves_resolution
                AND status='succeeded'
               THEN 'clear_uncertainty_keep_succeeded'
           WHEN latest_ambiguous_number IS NULL
               THEN 'retain_uncertainty_no_orderable_ambiguity_evidence'
           WHEN latest_ambiguous_finished_at IS NULL OR has_missing_finish_time
               THEN 'retain_uncertainty_missing_attempt_time'
           WHEN latest_reconciliation_at IS NULL
               THEN CASE WHEN status IN ('failed','succeeded')
                         THEN 'route_terminal_state_to_reconciliation'
                         ELSE 'retain_uncertainty_without_resolution' END
           WHEN latest_reconciliation_at=latest_ambiguous_finished_at
                OR latest_reconciliation_at=latest_ambiguous_started_at
               THEN 'retain_uncertainty_equal_time'
           WHEN latest_reconciliation_at<latest_ambiguous_finished_at
               THEN 'retain_uncertainty_new_ambiguity_after_reconciliation'
           WHEN have_conflicting_outcomes
               THEN 'retain_uncertainty_conflicting_reconciliations'
           WHEN latest_reconciliation_outcome LIKE 'not_delivered%'
                AND latest_retry_decision_source<>'runtime_recorded'
               THEN CASE
                    WHEN status IN ('failed','succeeded')
                        THEN 'route_terminal_state_to_reconciliation_preserve_unverified_retry_intent'
                    WHEN status='reconciliation_required'
                        THEN 'retain_reconciliation_required_unverified_retry_intent'
                    ELSE 'retain_uncertainty_retry_decision_unverified'
                    END
           WHEN status IN ('failed','succeeded')
               THEN 'route_terminal_state_to_reconciliation'
           ELSE 'retain_uncertainty_no_strict_resolution_order'
       END AS proposed_action,
       'Attempt number orders attempts only; reconciliation requires strict timestamp order; v6 audit is context only.'
           AS ordering_basis,
       latest_reconciliation_retry_requested,
       coalesce(latest_retry_decision_source,'legacy_unverified')
           AS retry_decision_source,
       CASE WHEN latest_retry_decision_source='runtime_recorded'
            THEN 'runtime_recorded'
            ELSE 'legacy_value_unverified_v6_backfill_or_unknown' END
           AS retry_decision_reliability,
       has_missing_start_time,has_missing_finish_time,
       (lease_token IS NOT NULL AND lease_until IS NOT NULL) AS current_lease_present,
       CASE WHEN success_proves_resolution
            THEN 'later successful attempt with strict finish-time order'
            WHEN reconciliation_proves_resolution
            THEN 'strict later delivered reconciliation; no conflicting outcome'
           WHEN pending_not_delivered_proves_resolution
            THEN 'strict not_delivered result; current pending schedule is preserved'
           WHEN failed_not_delivered_proves_resolution
            THEN 'strict not_delivered result; existing failed status is preserved'
            ELSE 'no sufficient resolution evidence' END
           AS evidence_reliability
       ,pending_not_delivered_proves_resolution,
       failed_not_delivered_proves_resolution
FROM evidence
ORDER BY tenant,id;
