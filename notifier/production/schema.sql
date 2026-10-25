-- Applied once by `python -m notifier.production migrate`, never at API startup.
CREATE TABLE IF NOT EXISTS delivery_schema (version integer PRIMARY KEY);
CREATE TABLE IF NOT EXISTS delivery_capacity (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    snapshot_bytes bigint NOT NULL CHECK(snapshot_bytes >= 0),
    retained_jobs bigint NOT NULL CHECK(retained_jobs >= 0)
);
CREATE TABLE IF NOT EXISTS delivery_origins (
    origin text PRIMARY KEY,
    in_flight integer NOT NULL DEFAULT 0 CHECK (in_flight >= 0),
    last_claimed double precision NOT NULL DEFAULT 0,
    next_allowed double precision NOT NULL DEFAULT 0,
    failures integer NOT NULL DEFAULT 0,
    open_until double precision NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS delivery_jobs (
    id uuid PRIMARY KEY,
    tenant text NOT NULL,
    idempotency_key text NOT NULL,
    request_hash text NOT NULL,
    snapshot text,
    origin text NOT NULL REFERENCES delivery_origins(origin),
    retry_mode text NOT NULL CHECK(retry_mode IN ('idempotent','duplicate_tolerant','manual_on_ambiguous')),
    status text NOT NULL CHECK (status IN ('pending','in_flight','succeeded','failed')),
    attempt_count integer NOT NULL DEFAULT 0,
    cycle_attempts integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL,
    replay_count integer NOT NULL DEFAULT 0,
    next_attempt_at double precision NOT NULL,
    deadline double precision NOT NULL,
    key_expires_at double precision,
    lease_token text,
    lease_until double precision,
    last_error text,
    created_at double precision NOT NULL,
    updated_at double precision NOT NULL,
    UNIQUE (tenant,idempotency_key)
);
CREATE INDEX IF NOT EXISTS delivery_due ON delivery_jobs(origin,next_attempt_at,created_at,id)
    WHERE status='pending';
CREATE INDEX IF NOT EXISTS delivery_expired ON delivery_jobs(origin,lease_until)
    WHERE status='in_flight';
CREATE INDEX IF NOT EXISTS delivery_tenant ON delivery_jobs(tenant,created_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS delivery_active ON delivery_jobs(tenant,status)
    WHERE status IN ('pending','in_flight');
CREATE INDEX IF NOT EXISTS delivery_retention ON delivery_jobs(updated_at)
    WHERE status IN ('succeeded','failed');
CREATE TABLE IF NOT EXISTS delivery_attempts (
    notification_id uuid NOT NULL REFERENCES delivery_jobs(id) ON DELETE CASCADE,
    number integer NOT NULL,
    outcome text NOT NULL,
    started_at double precision NOT NULL,
    finished_at double precision,
    http_status integer,
    duration_ms integer,
    error text,
    PRIMARY KEY(notification_id,number)
);
CREATE TABLE IF NOT EXISTS delivery_audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant text NOT NULL,
    actor text NOT NULL,
    action text NOT NULL,
    notification_id uuid,
    reason text,
    created_at double precision NOT NULL
);
CREATE INDEX IF NOT EXISTS delivery_audit_tenant ON delivery_audit(tenant,id DESC);
CREATE TABLE IF NOT EXISTS delivery_workers (
    id text PRIMARY KEY,
    last_seen double precision NOT NULL
);
INSERT INTO delivery_schema(version) VALUES (1) ON CONFLICT DO NOTHING;
INSERT INTO delivery_capacity(singleton,snapshot_bytes,retained_jobs)
    SELECT true,coalesce(sum(octet_length(snapshot)),0),count(*) FROM delivery_jobs
    ON CONFLICT DO NOTHING;
