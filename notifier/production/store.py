import math
import re
import time
import uuid
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ..store import Conflict


class CapacityExceeded(Exception):
    pass


class PayloadExpired(Exception):
    pass


PUBLIC_FIELDS = (
    "id", "status", "attempt_count", "cycle_attempts", "max_attempts", "replay_count",
    "outcome_uncertain", "next_attempt_at", "deadline", "key_expires_at", "last_error",
    "created_at", "updated_at",
    "retry_mode", "origin",
)
PUBLIC_SQL = (
    ", ".join(PUBLIC_FIELDS)
    + ", snapshot IS NOT NULL AS has_snapshot, "
      "(key_expires_at IS NULL OR key_expires_at>extract(epoch FROM clock_timestamp())+%s) "
      "AS supplier_key_valid, "
      "NOT EXISTS (SELECT 1 FROM delivery_reconciliations r "
      "WHERE r.notification_id=delivery_jobs.id AND r.retry_scheduled=false) "
      "AS replay_allowed_by_reconciliation"
)


def finite_values(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: finite_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite_values(item) for item in value]
    return value


def public(row):
    if row is None:
        return None
    result = finite_values({key: row[key] for key in PUBLIC_FIELDS})
    result["id"] = str(result["id"])
    result["replayable"] = (
        row["has_snapshot"] and not row["outcome_uncertain"]
        and row["status"] == "failed" and row["supplier_key_valid"]
        and row["replay_allowed_by_reconciliation"]
    )
    return result


class PostgresStore:
    def __init__(self, settings, security):
        self.settings = settings
        self.security = security
        options = conninfo_to_dict(settings.dsn).get("options", "")
        self.pool = ConnectionPool(
            settings.dsn, min_size=1, max_size=settings.pool_size, timeout=3,
            max_waiting=64, open=False, check=ConnectionPool.check_connection,
            kwargs={
                "row_factory": dict_row, "connect_timeout": 5,
                "options": options + " -c statement_timeout=5000 -c lock_timeout=2000 "
                           "-c idle_in_transaction_session_timeout=10000",
                "application_name": "reliable-notifier",
            },
        )

    def open(self):
        self.pool.open(wait=True, timeout=10)

    def close(self):
        self.pool.close()

    @staticmethod
    def migrate(dsn):
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(73918200)")
            exists = conn.execute("SELECT to_regclass('delivery_schema')").fetchone()[0]
            if not exists:
                conn.execute(Path(__file__).with_name("schema.sql").read_text())
                current = 1
            else:
                versions = conn.execute("SELECT version FROM delivery_schema ORDER BY version").fetchall()
                if versions not in (
                    [(1,)], [(2,)], [(3,)], [(4,)], [(5,)], [(6,)], [(7,)],
                ):
                    raise RuntimeError("Unsupported schema; use a reviewed versioned migration")
                current = versions[-1][0]
            migrations = Path(__file__).with_name("migrations")
            for path in sorted(migrations.glob("*.sql")):
                target = int(path.name[:4])
                if target <= current:
                    continue
                if target != current + 1:
                    raise RuntimeError("A schema migration is missing")
                conn.execute(path.read_text())
                current = target

    @staticmethod
    def now(conn):
        return conn.execute("SELECT extract(epoch FROM clock_timestamp())::float8 AS now").fetchone()["now"]

    def ready(self):
        with self.pool.connection() as conn:
            return [row["version"] for row in conn.execute(
                "SELECT version FROM delivery_schema"
            )] == [7]

    def _admission(self, conn):
        # A short shared lock makes quotas and idempotency atomic across API replicas.
        # This intentionally serializes admissions, not delivery; benchmark before increasing scale.
        conn.execute("SELECT pg_advisory_xact_lock(73918201)")

    def _capacity(self, conn, tenant, snapshot_size=0, new_job=False):
        row = conn.execute(
            """SELECT count(*) AS total, count(*) FILTER (WHERE tenant=%s) AS tenant
            FROM delivery_jobs WHERE status IN ('pending','in_flight','paused')""", (tenant,),
        ).fetchone()
        if row["total"] >= self.settings.max_pending or row["tenant"] >= self.settings.max_tenant_pending:
            raise CapacityExceeded()
        capacity = conn.execute("SELECT * FROM delivery_capacity WHERE singleton").fetchone()
        if (capacity["snapshot_bytes"] + snapshot_size > self.settings.max_snapshot_bytes
                or (new_job and capacity["retained_jobs"] >= self.settings.max_retained)):
            raise CapacityExceeded()

    def _audit(self, conn, principal, action, identifier, reason=None):
        conn.execute(
            """INSERT INTO delivery_audit(tenant,actor,action,notification_id,reason,created_at)
            VALUES (%s,%s,%s,%s,%s,%s)""",
            (principal.tenant, principal.name, action, identifier, reason, self.now(conn)),
        )

    def submit(self, principal, key, request, origin, digest):
        snapshot = self.security.encode(request)
        with self.pool.connection() as conn:
            self._admission(conn)
            old = conn.execute(
                "SELECT id,request_hash FROM delivery_jobs WHERE tenant=%s AND idempotency_key=%s",
                (principal.tenant, key),
            ).fetchone()
            if old:
                if old["request_hash"] != digest:
                    raise Conflict("Idempotency key belongs to another request")
                return str(old["id"]), False
            snapshot_size = len(snapshot.encode())
            self._capacity(conn, principal.tenant, snapshot_size, new_job=True)
            now = self.now(conn)
            key_window = request.get("_key_window_seconds")
            deadline = now + self.settings.delivery_window
            identifier = uuid.uuid4()
            conn.execute(
                "INSERT INTO delivery_origins(origin) VALUES (%s) ON CONFLICT DO NOTHING",
                (origin,),
            )
            conn.execute(
                """INSERT INTO delivery_jobs
                (id,tenant,idempotency_key,request_hash,snapshot,origin,retry_mode,status,max_attempts,
                 next_attempt_at,deadline,key_expires_at,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s,%s,%s,%s)""",
                (identifier, principal.tenant, key, digest, snapshot, origin,
                 request.get("_retry_mode", "manual_on_ambiguous"),
                 self.settings.max_attempts, now, deadline, None, now, now),
            )
            conn.execute(
                "UPDATE delivery_jobs SET key_window_seconds=%s WHERE id=%s",
                (key_window, identifier),
            )
            self._audit(conn, principal, "submit", identifier)
            conn.execute(
                """UPDATE delivery_capacity SET snapshot_bytes=snapshot_bytes+%s,
                retained_jobs=retained_jobs+1 WHERE singleton""", (snapshot_size,),
            )
        return str(identifier), True

    def lookup_submission(self, principal, key, digest):
        with self.pool.connection() as conn:
            self._admission(conn)
            old = conn.execute(
                """SELECT id,request_hash FROM delivery_jobs
                WHERE tenant=%s AND idempotency_key=%s""",
                (principal.tenant, key),
            ).fetchone()
            if old is None:
                return None
            if old["request_hash"] != digest:
                raise Conflict("Idempotency key belongs to another request")
            return str(old["id"])

    def _recover(self, conn, origin, now):
        execution_budget = self.settings.timeout_seconds + 3
        rows = conn.execute(
            """SELECT * FROM delivery_jobs WHERE origin=%s AND status='in_flight'
            AND lease_until<=%s ORDER BY lease_until LIMIT 100 FOR UPDATE SKIP LOCKED""",
            (origin, now),
        ).fetchall()
        for row in rows:
            unsafe_window = (
                row["retry_mode"] == "idempotent"
                and row["key_expires_at"] is not None
                and row["key_expires_at"] <= now + execution_budget
            )
            exhausted = (
                row["cycle_attempts"] >= row["max_attempts"] or row["deadline"] <= now
            )
            needs_reconciliation = (
                row["retry_mode"] == "manual_on_ambiguous" or unsafe_window or exhausted
            )
            error = (
                "manual_reconciliation_required" if row["retry_mode"] == "manual_on_ambiguous"
                else "supplier_deduplication_window_insufficient" if unsafe_window
                else "worker_lease_expired"
            )
            state = "reconciliation_required" if needs_reconciliation else "pending"
            if not row["outcome_uncertain"]:
                conn.execute(
                    """INSERT INTO delivery_audit
                    (tenant,actor,action,notification_id,reason,created_at)
                    VALUES (%s,'worker','supplier_outcome_uncertain',%s,%s,%s)""",
                    (row["tenant"], row["id"], "worker_lease_expired", now),
                )
            conn.execute(
                """UPDATE delivery_attempts SET outcome='abandoned',finished_at=%s,
                error=%s WHERE notification_id=%s AND number=%s""",
                (now, error, row["id"], row["attempt_count"]),
            )
            conn.execute(
                """UPDATE delivery_jobs SET status=%s,outcome_uncertain=true,
                lease_token=NULL,lease_until=NULL,
                next_attempt_at=%s,last_error=%s,updated_at=%s WHERE id=%s""",
                (state, now, error, now, row["id"]),
            )
        if rows:
            conn.execute(
                "UPDATE delivery_origins SET in_flight=in_flight-%s WHERE origin=%s",
                (len(rows), origin),
            )
        # A deadline is enforced even if rate limiting/circuit opening prevents attempts.
        conn.execute(
            """UPDATE delivery_jobs SET
            status=CASE WHEN outcome_uncertain
                        THEN 'reconciliation_required' ELSE 'failed' END,
            last_error=CASE WHEN outcome_uncertain
                            THEN 'historical_supplier_outcome_unknown'
                            ELSE 'delivery_deadline_exceeded' END,
            updated_at=%s
            WHERE origin=%s AND status='pending'
            AND (deadline<=%s+%s OR (retry_mode='idempotent' AND attempt_count>0
                                  AND key_expires_at<=%s+%s))""",
            (now, origin, now, execution_budget, now, execution_budget),
        )

    def claim(self, lease_seconds):
        with self.pool.connection() as conn:
            now = self.now(conn)
            claim_started = time.monotonic()
            execution_budget = self.settings.timeout_seconds + 3
            # Every mutation takes locks in origin -> job order. SKIP LOCKED permits
            # other replicas to make progress on independent origins.
            origin = conn.execute(
                """SELECT o.* FROM delivery_origins o WHERE EXISTS (
                    SELECT 1 FROM delivery_jobs j WHERE j.origin=o.origin AND (
                        (j.status='in_flight' AND j.lease_until<=%s) OR
                        (j.status='pending' AND (j.deadline<=%s+%s OR
                            (j.retry_mode='idempotent' AND j.attempt_count>0
                             AND j.key_expires_at<=%s+%s) OR
                            (NOT o.cooldown_hold AND j.next_attempt_at<=%s
                             AND o.next_allowed<=%s AND o.open_until<=%s
                             AND o.in_flight<%s AND (o.failures<%s OR o.in_flight=0)))))
                ) ORDER BY o.last_claimed,o.origin LIMIT 1 FOR UPDATE OF o SKIP LOCKED""",
                (now, now, execution_budget, now, execution_budget, now, now, now,
                 self.settings.per_origin, self.settings.circuit_threshold),
            ).fetchone()
            if origin is None:
                return None
            now = self.now(conn)
            name = origin["origin"]
            self._recover(conn, name, now)
            origin = conn.execute("SELECT * FROM delivery_origins WHERE origin=%s", (name,)).fetchone()
            conn.execute("UPDATE delivery_origins SET last_claimed=%s WHERE origin=%s", (now, name))
            if (origin["cooldown_hold"] or origin["in_flight"] >= self.settings.per_origin
                    or origin["open_until"] > now
                    or origin["next_allowed"] > now
                    or (origin["failures"] >= self.settings.circuit_threshold and origin["in_flight"])):
                return None
            tenant_row = conn.execute(
                """SELECT tenant FROM delivery_jobs WHERE origin=%s AND status='pending'
                AND next_attempt_at<=%s AND deadline>%s+%s
                AND (retry_mode<>'idempotent' OR key_expires_at IS NULL
                     OR key_expires_at>%s+%s)
                GROUP BY tenant
                ORDER BY (tenant>%s) DESC,tenant LIMIT 1""",
                (name, now, now, execution_budget, now, execution_budget, origin["last_tenant"]),
            ).fetchone()
            if tenant_row is None:
                return None
            tenant = tenant_row["tenant"]
            job = conn.execute(
                """SELECT * FROM delivery_jobs WHERE origin=%s AND tenant=%s AND status='pending'
                AND next_attempt_at<=%s AND deadline>%s+%s
                AND (retry_mode<>'idempotent' OR key_expires_at IS NULL
                     OR key_expires_at>%s+%s)
                ORDER BY next_attempt_at,created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED""",
                (name, tenant, now, now, execution_budget, now, execution_budget),
            ).fetchone()
            if job is None:
                return None
            if job["retry_mode"] == "idempotent" and job["key_expires_at"] is None:
                job["key_expires_at"] = now + job["key_window_seconds"]
                job["deadline"] = min(
                    job["deadline"], job["key_expires_at"] - execution_budget,
                )
            if job["deadline"] <= now:
                return None
            job["lease_token"] = uuid.uuid4().hex
            job["lease_until"] = now + lease_seconds
            job["attempt_count"] += 1
            job["cycle_attempts"] += 1
            conn.execute(
                """UPDATE delivery_jobs SET status='in_flight',lease_token=%s,lease_until=%s,
                attempt_count=%s,cycle_attempts=%s,key_expires_at=%s,deadline=%s,
                updated_at=%s WHERE id=%s""",
                (job["lease_token"], job["lease_until"], job["attempt_count"],
                 job["cycle_attempts"], job["key_expires_at"], job["deadline"], now, job["id"]),
            )
            conn.execute(
                """INSERT INTO delivery_attempts(notification_id,number,outcome,started_at)
                VALUES (%s,%s,'in_flight',%s)""", (job["id"], job["attempt_count"], now),
            )
            conn.execute(
                """UPDATE delivery_origins SET in_flight=in_flight+1,next_allowed=%s,last_tenant=%s
                WHERE origin=%s""", (now + 1 / self.settings.rate_per_second, tenant, name),
            )
            # Decrypt after commit: corruption/key loss leaves a recoverable durable lease.
        job["id"] = str(job["id"])
        execution_budget = min(
            self.settings.timeout_seconds + 3,
            job["deadline"] - now,
            job["lease_until"] - now,
        )
        job["execution_deadline_monotonic"] = claim_started + execution_budget
        job["request"] = {"snapshot": self.security.decode(job.pop("snapshot")), "tenant": job["tenant"]}
        return job

    def heartbeat(self, job, lease_seconds):
        with self.pool.connection() as conn:
            conn.execute("SELECT origin FROM delivery_origins WHERE origin=%s FOR UPDATE", (job["origin"],))
            now = self.now(conn)
            return conn.execute(
                """UPDATE delivery_jobs SET lease_until=%s WHERE id=%s AND status='in_flight'
                AND lease_token=%s AND lease_until>%s""",
                (now + lease_seconds, job["id"], job["lease_token"], now),
            ).rowcount == 1

    def finish(self, job, result, delay):
        with self.pool.connection() as conn:
            origin = conn.execute(
                "SELECT * FROM delivery_origins WHERE origin=%s FOR UPDATE", (job["origin"],),
            ).fetchone()
            now = self.now(conn)
            current = conn.execute(
                """SELECT * FROM delivery_jobs WHERE id=%s AND status='in_flight'
                AND lease_token=%s FOR UPDATE""",
                (job["id"], job["lease_token"]),
            ).fetchone()
            if current is None or current["lease_until"] <= now:
                return False
            # The persisted row is authoritative for retry windows and attempt budgets.
            job = {**job, **current}
            was_uncertain = job["outcome_uncertain"]
            current_uncertain = (
                result.ambiguous
                or result.request_state == "possibly_sent"
                or result.error == "manual_reconciliation_required"
                or (result.retryable and result.http_status is not None)
            )
            remains_uncertain = was_uncertain or current_uncertain
            scheduled_at = now + delay
            cooldown_until = now + result.retry_after
            cooldown_hold = not result.success and (
                result.cooldown_hold
                or not math.isfinite(delay)
                or delay < 0
                or not math.isfinite(result.retry_after)
                or result.retry_after < 0
                or not math.isfinite(scheduled_at)
                or not math.isfinite(cooldown_until)
            )
            hold_reason = (
                result.cooldown_reason or result.error or "retry_after_requires_operator_release"
                if cooldown_hold else None
            )
            state, outcome = "failed", "permanent_failure"
            if result.success:
                state, outcome = "succeeded", "succeeded"
                remains_uncertain = False
            elif result.error == "manual_reconciliation_required":
                state, outcome = "reconciliation_required", "reconciliation_required"
            elif result.ambiguous and job["retry_mode"] == "manual_on_ambiguous":
                state, outcome = "reconciliation_required", "reconciliation_required"
            elif cooldown_hold:
                state, outcome = "paused", "retry_after_hold"
            elif result.ambiguous and job["retry_mode"] == "idempotent":
                retry_budget = self.settings.timeout_seconds + 3
                key_window_safe = (
                    job["key_expires_at"] is not None
                    and now + delay + retry_budget < job["key_expires_at"]
                )
                if (result.retryable and key_window_safe
                        and job["cycle_attempts"] < job["max_attempts"]
                        and now + delay + retry_budget < job["deadline"]):
                    state, outcome = "pending", "retryable_failure"
                else:
                    state, outcome = "reconciliation_required", "reconciliation_required"
            elif result.retryable:
                state, outcome = "failed", "retry_exhausted"
                retry_budget = self.settings.timeout_seconds + 3
                key_window_safe = (
                    job["retry_mode"] != "idempotent"
                    or (job["key_expires_at"] is not None
                        and now + delay + retry_budget < job["key_expires_at"])
                )
                retry_is_safe = key_window_safe
                if (retry_is_safe and job["cycle_attempts"] < job["max_attempts"]
                        and now + delay + self.settings.timeout_seconds + 3 < job["deadline"]):
                    state, outcome = "pending", "retryable_failure"
                elif job["retry_mode"] == "idempotent" and job["key_expires_at"] is not None:
                    state, outcome = "reconciliation_required", "reconciliation_required"
            if remains_uncertain and state == "failed":
                state, outcome = "reconciliation_required", "reconciliation_required"
            updated = conn.execute(
                """UPDATE delivery_jobs SET status=%s,outcome_uncertain=%s,
                next_attempt_at=%s,lease_token=NULL,
                lease_until=NULL,last_error=%s,updated_at=%s WHERE id=%s AND status='in_flight'
                AND lease_token=%s AND lease_until>%s""",
                (state, remains_uncertain, now if cooldown_hold else scheduled_at,
                 result.error or hold_reason, now, job["id"], job["lease_token"], now),
            )
            if updated.rowcount != 1:
                return False
            if current_uncertain and not was_uncertain:
                conn.execute(
                    """INSERT INTO delivery_audit
                    (tenant,actor,action,notification_id,reason,created_at)
                    VALUES (%s,'worker','supplier_outcome_uncertain',%s,%s,%s)""",
                    (job["tenant"], job["id"], result.error or "possibly_sent", now),
                )
            elif result.success and was_uncertain:
                conn.execute(
                    """INSERT INTO delivery_audit
                    (tenant,actor,action,notification_id,reason,created_at)
                    VALUES (%s,'worker','supplier_outcome_uncertainty_resolved',%s,%s,%s)""",
                    (job["tenant"], job["id"], "successful_supplier_response", now),
                )
            failure_class = result.failure_class or (
                "supplier_network" if result.ambiguous or result.error == "network_error"
                else "supplier_service" if result.http_status in {408, 425, 429}
                or (result.http_status is not None and result.http_status >= 500)
                else "business_rejection"
            )
            availability_failure = failure_class in {"supplier_network", "supplier_service"}
            if failure_class in {"local_configuration", "local_authorization", "local_policy"}:
                failures = origin["failures"]
                open_until = origin["open_until"]
            else:
                failures = origin["failures"] + 1 if availability_failure else 0
                open_until = 0
            if availability_failure and failures >= self.settings.circuit_threshold:
                open_until = max(origin["open_until"], now + self.settings.circuit_seconds)
            conn.execute(
                """UPDATE delivery_origins SET in_flight=in_flight-1,failures=%s,
                open_until=%s,next_allowed=greatest(next_allowed,%s),
                cooldown_hold=cooldown_hold OR %s,
                cooldown_reason=CASE WHEN %s THEN %s ELSE cooldown_reason END
                WHERE origin=%s""",
                (failures, open_until, now if cooldown_hold else cooldown_until,
                 cooldown_hold, cooldown_hold, hold_reason, job["origin"]),
            )
            conn.execute(
                """UPDATE delivery_attempts SET outcome=%s,finished_at=%s,http_status=%s,
                duration_ms=%s,error=%s WHERE notification_id=%s AND number=%s""",
                (outcome, now, result.http_status, result.duration_ms, result.error,
                 job["id"], job["attempt_count"]),
            )
            if cooldown_hold:
                conn.execute(
                    """INSERT INTO delivery_audit(tenant,actor,action,notification_id,reason,created_at)
                    VALUES (%s,'worker','retry_after_hold',%s,%s,%s)""",
                    (job["tenant"], job["id"], hold_reason, now),
                )
            return True

    def get(self, tenant, identifier, after_attempt=0, limit=50):
        with self.pool.connection() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            row = conn.execute(
                f"SELECT {PUBLIC_SQL} FROM delivery_jobs WHERE tenant=%s AND id=%s",
                (self.settings.timeout_seconds + 3, tenant, identifier),
            ).fetchone()
            result = public(row)
            if result:
                attempts = conn.execute(
                    """SELECT number,outcome,started_at,finished_at,http_status,duration_ms,error
                    FROM delivery_attempts WHERE notification_id=%s AND number>%s
                    ORDER BY number LIMIT %s""", (identifier, after_attempt, limit + 1),
                ).fetchall()
                result["attempts"] = [finite_values(attempt) for attempt in attempts[:limit]]
                result["next_attempt_cursor"] = attempts[limit - 1]["number"] if len(attempts) > limit else None
                result["reconciliations"] = [finite_values(item) for item in conn.execute(
                    """SELECT actor,outcome,evidence_ref,reason,created_at,
                    retry_requested,retry_scheduled,retry_decision_source
                    FROM delivery_reconciliations WHERE notification_id=%s ORDER BY id""",
                    (identifier,),
                ).fetchall()]
            return result

    def list(self, tenant, before=None, limit=50, status=None):
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"""SELECT {PUBLIC_SQL} FROM delivery_jobs WHERE tenant=%s
                AND (%s::text IS NULL OR status=%s)
                AND (%s::float8 IS NULL OR (created_at,id)<(%s,%s::uuid))
                ORDER BY created_at DESC,id DESC LIMIT %s""",
                (self.settings.timeout_seconds + 3, tenant, status, status, before[0] if before else None,
                 before[0] if before else None, before[1] if before else None, limit + 1),
            ).fetchall()
            items = [public(row) for row in rows[:limit]]
            cursor = [rows[limit - 1]["created_at"], str(rows[limit - 1]["id"])] if len(rows) > limit else None
            return {"items": items, "next_cursor": cursor}

    def replay(self, principal, identifier, reason):
        with self.pool.connection() as conn:
            self._admission(conn)
            row = conn.execute(
                "SELECT origin FROM delivery_jobs WHERE id=%s AND tenant=%s", (identifier, principal.tenant),
            ).fetchone()
            if not row:
                return False
            conn.execute("SELECT origin FROM delivery_origins WHERE origin=%s FOR UPDATE", (row["origin"],))
            row = conn.execute(
                "SELECT * FROM delivery_jobs WHERE id=%s AND tenant=%s FOR UPDATE",
                (identifier, principal.tenant),
            ).fetchone()
            if not row:
                return False
            reconciliation_closed = conn.execute(
                """SELECT EXISTS(SELECT 1 FROM delivery_reconciliations
                WHERE notification_id=%s AND retry_scheduled=false) AS closed""",
                (identifier,),
            ).fetchone()["closed"]
            if (row["status"] != "failed" or row["outcome_uncertain"]
                    or row["replay_count"] >= 10 or reconciliation_closed):
                raise Conflict("Only failed jobs with fewer than 10 replays can be replayed")
            if row["snapshot"] is None:
                raise PayloadExpired()
            self._capacity(conn, principal.tenant)
            now = self.now(conn)
            execution_budget = self.settings.timeout_seconds + 3
            if (row["key_expires_at"] is not None
                    and row["key_expires_at"] <= now + execution_budget):
                conn.execute(
                    """UPDATE delivery_jobs SET status='reconciliation_required',
                    last_error='supplier_deduplication_window_insufficient',updated_at=%s WHERE id=%s""",
                    (now, identifier),
                )
                self._audit(conn, principal, "reconciliation_required", identifier,
                            "supplier_deduplication_window_insufficient")
                return "reconciliation_required"
            deadline = min(
                now + self.settings.delivery_window,
                (row["key_expires_at"] - execution_budget) if row["key_expires_at"] else float("inf"),
            )
            conn.execute(
                """UPDATE delivery_jobs SET status='pending',cycle_attempts=0,
                replay_count=replay_count+1,next_attempt_at=%s,deadline=%s,
                last_error=NULL,updated_at=%s WHERE id=%s""",
                (now, deadline, now, identifier),
            )
            self._audit(conn, principal, "replay", identifier, reason)
            return True

    def reconcile(
        self, principal, identifier, outcome, evidence_ref, reason,
        retry_after_reconciliation=None,
    ):
        if outcome not in {"delivered", "not_delivered"}:
            raise ValueError("Unsupported reconciliation outcome")
        if (retry_after_reconciliation is not None
                and type(retry_after_reconciliation) is not bool):
            raise ValueError("Retry decision must be boolean")
        if outcome == "delivered" and retry_after_reconciliation is True:
            raise ValueError("Delivered outcomes cannot request another delivery")
        if not isinstance(evidence_ref, str) or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", evidence_ref):
            raise ValueError("A non-secret evidence reference is required")
        if (not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 256
                or any(ord(char) < 32 for char in reason)):
            raise ValueError("A bounded reconciliation reason is required")

        with self.pool.connection() as conn:
            self._admission(conn)
            row = conn.execute(
                """SELECT origin FROM delivery_jobs WHERE id=%s AND tenant=%s""",
                (identifier, principal.tenant),
            ).fetchone()
            if row is None:
                return None
            conn.execute("SELECT origin FROM delivery_origins WHERE origin=%s FOR UPDATE", (row["origin"],))
            row = conn.execute(
                """SELECT * FROM delivery_jobs WHERE id=%s AND tenant=%s FOR UPDATE""",
                (identifier, principal.tenant),
            ).fetchone()
            if row["status"] != "reconciliation_required":
                raise Conflict("Only unresolved outcomes can be reconciled")
            now = self.now(conn)
            if outcome == "delivered":
                status = "succeeded"
                error = None
                event_outcome = outcome
                action = "reconcile_delivered"
                retry_requested = False
                retry_scheduled = False
            else:
                retry_requested = (
                    True if retry_after_reconciliation is None
                    else retry_after_reconciliation
                )
                expired_supplier_key = (
                    row["key_expires_at"] is not None
                    and row["key_expires_at"] <= now + self.settings.timeout_seconds + 3
                )
                if not retry_requested:
                    status = "failed"
                    error = "confirmed_not_delivered_retry_declined"
                    event_outcome = "not_delivered"
                    action = "reconcile_not_delivered"
                elif row["snapshot"] is None:
                    status = "failed"
                    error = "confirmed_not_delivered_snapshot_expired"
                    event_outcome = "not_delivered"
                    action = "reconcile_not_delivered"
                elif expired_supplier_key:
                    status = "failed"
                    error = "confirmed_not_delivered_new_supplier_key_required"
                    event_outcome = "not_delivered"
                    action = "reconcile_not_delivered"
                elif row["replay_count"] >= 10:
                    status = "failed"
                    error = "confirmed_not_delivered_retry_limit_reached"
                    event_outcome = "not_delivered"
                    action = "reconcile_not_delivered"
                else:
                    self._capacity(conn, principal.tenant)
                    status = "pending"
                    error = None
                    event_outcome = outcome
                    action = "reconcile_not_delivered"
                retry_scheduled = status == "pending"
            deadline = min(
                now + self.settings.delivery_window,
                (row["key_expires_at"] - self.settings.timeout_seconds - 3)
                if status == "pending" and row["key_expires_at"] else float("inf"),
            )
            conn.execute(
                """INSERT INTO delivery_reconciliations
                (notification_id,tenant,actor,outcome,evidence_ref,reason,created_at,
                 retry_requested,retry_scheduled,retry_decision_source)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'runtime_recorded')""",
                (identifier, principal.tenant, principal.name, event_outcome,
                 evidence_ref, reason, now, retry_requested, retry_scheduled),
            )
            conn.execute(
                """UPDATE delivery_jobs SET status=%s,outcome_uncertain=false,cycle_attempts=0,
                replay_count=replay_count+CASE WHEN %s='pending' THEN 1 ELSE 0 END,
                next_attempt_at=%s,deadline=%s,last_error=%s,updated_at=%s WHERE id=%s""",
                (status, status, now, deadline, error, now, identifier),
            )
            self._audit(conn, principal, action, identifier, reason)
            next_action = None
            if outcome == "not_delivered" and not retry_scheduled:
                next_action = (
                    "submit_new_operation_with_fresh_supplier_key"
                    if row["key_expires_at"] is not None
                    and row["key_expires_at"] <= now + self.settings.timeout_seconds + 3
                    else "submit_new_notification_with_fresh_supplier_key"
                )
            return {
                "id": str(identifier), "status": status, "next_action": next_action,
                "retry_requested": retry_requested, "retry_scheduled": retry_scheduled,
            }

    def audit(self, tenant, before=None, limit=50):
        with self.pool.connection() as conn:
            rows = conn.execute(
                """SELECT id,actor,action,notification_id,reason,created_at FROM delivery_audit
                WHERE tenant=%s AND (%s::bigint IS NULL OR id<%s) ORDER BY id DESC LIMIT %s""",
                (tenant, before, before, limit + 1),
            ).fetchall()
            return {
                "items": [{**row, "notification_id": str(row["notification_id"])} for row in rows[:limit]],
                "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            }

    def worker_seen(self, identifier):
        with self.pool.connection() as conn:
            conn.execute(
                """INSERT INTO delivery_workers(id,last_seen) VALUES (%s,%s)
                ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen""",
                (identifier, self.now(conn)),
            )

    def stats(self, tenant=None):
        with self.pool.connection() as conn:
            now = self.now(conn)
            rows = conn.execute(
                """SELECT status,count(*) AS count FROM delivery_jobs
                WHERE (%s::text IS NULL OR tenant=%s) GROUP BY status""", (tenant, tenant),
            ).fetchall()
            oldest = conn.execute(
                """SELECT min(created_at) AS oldest FROM delivery_jobs WHERE status='pending'
                AND (%s::text IS NULL OR tenant=%s)""", (tenant, tenant),
            ).fetchone()["oldest"]
            result = {
                "notifications": {
                    state: 0 for state in (
                        "pending", "in_flight", "succeeded", "failed", "paused",
                        "reconciliation_required",
                    )
                },
                "oldest_pending_age_seconds": max(0, now - oldest) if oldest else 0,
            }
            result["notifications"].update({row["status"]: row["count"] for row in rows})
            if tenant is None:
                result["healthy_workers"] = conn.execute(
                    "SELECT count(*) AS n FROM delivery_workers WHERE last_seen>%s", (now - 30,),
                ).fetchone()["n"]
                result["open_circuits"] = conn.execute(
                    "SELECT count(*) AS n FROM delivery_origins WHERE failures>=%s",
                    (self.settings.circuit_threshold,),
                ).fetchone()["n"]
                result["retry_after_holds"] = conn.execute(
                    "SELECT count(*) AS n FROM delivery_origins WHERE cooldown_hold",
                ).fetchone()["n"]
                capacity = conn.execute("SELECT * FROM delivery_capacity WHERE singleton").fetchone()
                result["snapshot_bytes"] = capacity["snapshot_bytes"]
                result["retained_jobs"] = capacity["retained_jobs"]
            return result

    def maintain(self, batch=500):
        if type(batch) is not int or not 1 <= batch <= 5000:
            raise ValueError("Maintenance batch must be between 1 and 5000")
        with self.pool.connection() as conn:
            # Serialize maintenance so it never competes with itself across replicas.
            if not conn.execute("SELECT pg_try_advisory_xact_lock(73918202) AS ok").fetchone()["ok"]:
                return 0
            self._admission(conn)
            now = self.now(conn)
            cutoff = now - self.settings.payload_retention_days * 86400
            old = conn.execute(
                """WITH candidates AS MATERIALIZED (
                    SELECT id
                    FROM delivery_jobs
                    WHERE status IN ('succeeded','failed') AND outcome_uncertain=false
                      AND updated_at<%s
                    ORDER BY updated_at,id LIMIT %s FOR UPDATE SKIP LOCKED
                ), deleted AS (
                    DELETE FROM delivery_jobs j USING candidates c
                    WHERE j.id=c.id RETURNING coalesce(octet_length(j.snapshot),0) AS bytes
                )
                SELECT count(*) AS count,coalesce(sum(bytes),0) AS bytes FROM deleted""",
                (now - self.settings.metadata_retention_days * 86400, batch),
            ).fetchone()
            snapshots = conn.execute(
                """WITH candidates AS MATERIALIZED (
                    SELECT id,octet_length(snapshot) AS bytes
                    FROM delivery_jobs
                    WHERE status IN ('succeeded','failed') AND outcome_uncertain=false
                      AND snapshot IS NOT NULL
                      AND updated_at<%s
                    ORDER BY updated_at,id LIMIT %s FOR UPDATE SKIP LOCKED
                ), cleared AS (
                    UPDATE delivery_jobs j SET snapshot=NULL
                    FROM candidates c WHERE j.id=c.id RETURNING j.id
                ), removed_attempts AS (
                    DELETE FROM delivery_attempts a USING cleared c
                    WHERE a.notification_id=c.id RETURNING 1
                )
                SELECT count(*) AS count,coalesce(sum(c.bytes),0) AS bytes,
                       (SELECT count(*) FROM removed_attempts) AS attempts
                FROM cleared x JOIN candidates c USING (id)""",
                (cutoff, batch),
            ).fetchone()
            conn.execute(
                """UPDATE delivery_capacity SET snapshot_bytes=snapshot_bytes-%s,
                retained_jobs=retained_jobs-%s WHERE singleton""",
                (snapshots["bytes"] + old["bytes"], old["count"]),
            )
            conn.execute(
                """DELETE FROM delivery_audit WHERE id IN (SELECT id FROM delivery_audit
                WHERE created_at<%s
                AND NOT EXISTS (
                    SELECT 1 FROM delivery_jobs j
                    WHERE j.id=delivery_audit.notification_id AND j.outcome_uncertain
                )
                LIMIT %s)""",
                (now - self.settings.metadata_retention_days * 86400, batch),
            )
            conn.execute("DELETE FROM delivery_workers WHERE last_seen<%s", (now - 86400,))
            # Origin rows intentionally remain: deleting them races with concurrent admission.
            return snapshots["count"] + old["count"]

    def rotate_snapshots(self):
        cursor = uuid.UUID(int=0)
        changed = 0
        while True:
            with self.pool.connection() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(73918202)")
                rows = conn.execute(
                    """SELECT id,snapshot FROM delivery_jobs WHERE id>%s AND snapshot IS NOT NULL
                    ORDER BY id LIMIT 100 FOR UPDATE""", (cursor,),
                ).fetchall()
                if not rows:
                    return changed
                cipher = self.security.cipher()
                for row in rows:
                    conn.execute(
                        "UPDATE delivery_jobs SET snapshot=%s WHERE id=%s",
                        (cipher.rotate(row["snapshot"].encode()).decode(), row["id"]),
                    )
                cursor = rows[-1]["id"]
                changed += len(rows)
