import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Conflict(ValueError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','in_flight','succeeded','failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    cycle_attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    replay_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    lease_token TEXT,
    lease_until REAL,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS due_notifications ON notifications(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS expired_notifications ON notifications(status, lease_until);
CREATE TABLE IF NOT EXISTS attempts (
    notification_id TEXT NOT NULL REFERENCES notifications(id),
    number INTEGER NOT NULL,
    lease_token TEXT NOT NULL,
    outcome TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    http_status INTEGER,
    duration_ms INTEGER,
    error TEXT,
    PRIMARY KEY(notification_id, number)
);
PRAGMA user_version = 1;
"""


class Store:
    def __init__(self, path, clock=time.time):
        self.path = str(path)
        self.clock = clock
        parent = Path(path).parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        with self.connection() as conn:
            # Avoid unpatched WAL-reset races in older system SQLite runtimes.
            mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if mode.lower() != "delete":
                raise RuntimeError("Could not enable rollback journaling")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError("Unsupported database schema version")
            conn.executescript(SCHEMA)

    @contextmanager
    def connection(self, write=False):
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=EXTRA")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except BaseException:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    def submit(self, key, request, digest, max_attempts):
        now = self.clock()
        with self.connection(write=True) as conn:
            existing = conn.execute(
                "SELECT id, request_hash FROM notifications WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing:
                if existing["request_hash"] != digest:
                    raise Conflict("Idempotency-Key already belongs to a different request")
                return existing["id"], False
            identifier = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO notifications
                (id,idempotency_key,request_hash,request_json,status,max_attempts,
                 next_attempt_at,created_at,updated_at) VALUES (?,?,?,?,'pending',?,?,?,?)""",
                (identifier, key, digest, json.dumps(request), max_attempts, now, now, now),
            )
        return identifier, True

    def claim(self, lease_seconds):
        now = self.clock()
        with self.connection(write=True) as conn:
            expired = conn.execute(
                """SELECT * FROM notifications WHERE status='in_flight' AND lease_until<=?
                ORDER BY lease_until LIMIT 100""",
                (now,),
            ).fetchall()
            for row in expired:
                conn.execute(
                    """UPDATE attempts SET outcome='abandoned',finished_at=?,error=?
                    WHERE notification_id=? AND lease_token=? AND outcome='in_flight'""",
                    (now, "worker_lease_expired", row["id"], row["lease_token"]),
                )
                state = "failed" if row["cycle_attempts"] >= row["max_attempts"] else "pending"
                conn.execute(
                    """UPDATE notifications SET status=?,lease_token=NULL,lease_until=NULL,
                    next_attempt_at=?,last_error='worker_lease_expired',updated_at=? WHERE id=?""",
                    (state, now, now, row["id"]),
                )
            row = conn.execute(
                """SELECT * FROM notifications WHERE status='pending' AND next_attempt_at<=?
                ORDER BY next_attempt_at,created_at,id LIMIT 1""", (now,),
            ).fetchone()
            if not row:
                return None
            job = dict(row)
            job["lease_token"] = uuid.uuid4().hex
            job["attempt_count"] += 1
            job["cycle_attempts"] += 1
            conn.execute(
                """UPDATE notifications SET status='in_flight',lease_token=?,lease_until=?,
                attempt_count=?,cycle_attempts=?,updated_at=? WHERE id=?""",
                (job["lease_token"], now + lease_seconds, job["attempt_count"],
                 job["cycle_attempts"], now, job["id"]),
            )
            conn.execute(
                """INSERT INTO attempts
                (notification_id,number,lease_token,outcome,started_at)
                VALUES (?,?,?,'in_flight',?)""",
                (job["id"], job["attempt_count"], job["lease_token"], now),
            )
            job["request"] = json.loads(job.pop("request_json"))
            return job

    def heartbeat(self, job, lease_seconds):
        now = self.clock()
        with self.connection(write=True) as conn:
            result = conn.execute(
                """UPDATE notifications SET lease_until=?,updated_at=?
                WHERE id=? AND status='in_flight' AND lease_token=? AND lease_until>?""",
                (now + lease_seconds, now, job["id"], job["lease_token"], now),
            )
            return result.rowcount == 1

    def finish(self, job, result, delay):
        now = self.clock()
        if result.success:
            state, outcome = "succeeded", "succeeded"
        elif result.retryable and job["cycle_attempts"] < job["max_attempts"]:
            state, outcome = "pending", "retryable_failure"
        else:
            state = "failed"
            outcome = "retry_exhausted" if result.retryable else "permanent_failure"
        with self.connection(write=True) as conn:
            # The token AND live lease fence stale workers after crash recovery.
            updated = conn.execute(
                """UPDATE notifications SET status=?,next_attempt_at=?,lease_token=NULL,
                lease_until=NULL,last_error=?,updated_at=?
                WHERE id=? AND status='in_flight' AND lease_token=? AND lease_until>?""",
                (state, now + delay, result.error, now, job["id"], job["lease_token"], now),
            )
            if updated.rowcount != 1:
                return False
            conn.execute(
                """UPDATE attempts SET outcome=?,finished_at=?,http_status=?,duration_ms=?,error=?
                WHERE notification_id=? AND number=? AND lease_token=?""",
                (outcome, now, result.http_status, result.duration_ms, result.error,
                 job["id"], job["attempt_count"], job["lease_token"]),
            )
            return True

    def replay(self, identifier):
        now = self.clock()
        with self.connection(write=True) as conn:
            row = conn.execute(
                "SELECT status FROM notifications WHERE id=?", (identifier,)
            ).fetchone()
            if not row:
                return False
            if row["status"] != "failed":
                raise Conflict("Only failed notifications can be replayed")
            conn.execute(
                """UPDATE notifications SET status='pending',cycle_attempts=0,
                replay_count=replay_count+1,next_attempt_at=?,last_error=NULL,updated_at=?
                WHERE id=?""", (now, now, identifier),
            )
        return True

    @staticmethod
    def public(row):
        if row is None:
            return None
        # Request snapshots contain credentials; never expose them from read APIs.
        return {key: row[key] for key in (
            "id", "status", "attempt_count", "cycle_attempts", "max_attempts",
            "replay_count", "next_attempt_at", "last_error", "created_at", "updated_at",
        )}

    def get(self, identifier):
        with self.connection() as conn:
            conn.execute("BEGIN")
            result = self.public(conn.execute(
                "SELECT * FROM notifications WHERE id=?", (identifier,)
            ).fetchone())
            if result is not None:
                result["attempts"] = [dict(row) for row in conn.execute(
                    """SELECT number,outcome,started_at,finished_at,http_status,duration_ms,error
                    FROM attempts WHERE notification_id=? ORDER BY number""", (identifier,),
                )]
            return result

    def list(self, status=None, limit=50):
        with self.connection() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM notifications WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                )
            else:
                rows = conn.execute(
                    "SELECT * FROM notifications ORDER BY created_at DESC LIMIT ?", (limit,)
                )
            return [self.public(row) for row in rows]

    def stats(self):
        with self.connection() as conn:
            counts = dict(conn.execute(
                "SELECT status,COUNT(*) FROM notifications GROUP BY status"
            ).fetchall())
            oldest = conn.execute(
                "SELECT MIN(created_at) FROM notifications WHERE status='pending'"
            ).fetchone()[0]
            return {
                "notifications": {state: counts.get(state, 0) for state in (
                    "pending", "in_flight", "succeeded", "failed",
                )},
                "oldest_pending_age_seconds": (
                    max(0, self.clock() - oldest) if oldest is not None else 0
                ),
            }
