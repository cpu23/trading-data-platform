-- Durable operation queue, role liveness, and persisted quote state.
--
-- operation_jobs is the durable hand-off between run acceptance and worker
-- execution.  An API or scheduler process inserts the cycle_runs acceptance
-- row and its operation_jobs entry in ONE transaction: acceptance and enqueue
-- are atomic, and a worker later claims the job with a lease, keeps the
-- cycle_runs heartbeat while executing, and finalizes both in the worker's
-- own transaction.  Lease expiry/reclaim, retry/backoff, poison terminal
-- state, and duplicate suppression all follow the analysis_jobs contract.
--
-- The partial unique index below is the DB identity that prevents duplicate
-- logical runs: a scheduler firing twice for the same window has one active
-- row; the second enqueue is suppressed inside the acceptance transaction.
-- pg_advisory_xact_lock serializes same-window fires in application code.
--
-- role_heartbeats is the durable replacement for process-global worker
-- status.  Each role process (api, scheduler, worker, outbox, quotes) writes
-- a heartbeat while alive; health endpoints and `roles check <ROLE>` read it.
--
-- quote_state persists the latest observed quote so the API /quotes endpoint
-- and the SSE poller can serve data while the quote-stream process owns the
-- live connection.  The stream process upserts on every tick.
--
-- Rollback notes (dependency-safe order): drop operation_jobs indexes, then
-- operation_jobs (it references cycle_runs); drop quote_state; drop
-- role_heartbeats.  Dropping a table also drops its owned indexes and
-- constraints.

CREATE TABLE IF NOT EXISTS operation_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_kind TEXT NOT NULL,
    requested_component TEXT,
    correlation_id UUID NOT NULL UNIQUE
        REFERENCES cycle_runs(correlation_id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL DEFAULT 100,
    dedupe_key TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    not_before TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_expires_at TIMESTAMPTZ,
    claimed_by TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    result_ref JSONB,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    CONSTRAINT operation_jobs_run_kind_check
        CHECK (run_kind IN ('cycle', 'collector', 'processor', 'news', 'filings')),
    CONSTRAINT operation_jobs_state_check
        CHECK (state IN (
            'queued',
            'leased',
            'running',
            'succeeded',
            'failed_retryable',
            'failed_terminal',
            'suppressed_duplicate',
            'cancelled'
        )),
    CONSTRAINT operation_jobs_dedupe_key_nonblank_check
        CHECK (BTRIM(dedupe_key) <> ''),
    CONSTRAINT operation_jobs_input_fingerprint_nonblank_check
        CHECK (BTRIM(input_fingerprint) <> ''),
    CONSTRAINT operation_jobs_claimed_by_nonblank_check
        CHECK (claimed_by IS NULL OR BTRIM(claimed_by) <> ''),
    CONSTRAINT operation_jobs_attempt_count_check
        CHECK (attempt_count >= 0),
    CONSTRAINT operation_jobs_max_attempts_check
        CHECK (max_attempts > 0),
    CONSTRAINT operation_jobs_attempts_within_limit_check
        CHECK (attempt_count <= max_attempts),
    CONSTRAINT operation_jobs_payload_object_check
        CHECK (JSONB_TYPEOF(payload) = 'object'),
    CONSTRAINT operation_jobs_result_ref_object_check
        CHECK (result_ref IS NULL OR JSONB_TYPEOF(result_ref) = 'object'),
    CONSTRAINT operation_jobs_lease_consistency_check
        CHECK (
            state NOT IN ('leased', 'running')
            OR (claimed_by IS NOT NULL AND lease_expires_at IS NOT NULL)
        ),
    CONSTRAINT operation_jobs_running_started_check
        CHECK (state <> 'running' OR started_at IS NOT NULL),
    CONSTRAINT operation_jobs_terminal_timestamp_check
        CHECK (
            state NOT IN ('succeeded', 'failed_terminal', 'suppressed_duplicate')
            OR completed_at IS NOT NULL
        ),
    CONSTRAINT operation_jobs_nonterminal_completed_check
        CHECK (
            state IN ('succeeded', 'failed_terminal', 'suppressed_duplicate')
            OR completed_at IS NULL
        ),
    CONSTRAINT operation_jobs_cancelled_timestamp_check
        CHECK (state <> 'cancelled' OR cancelled_at IS NOT NULL),
    CONSTRAINT operation_jobs_noncancelled_timestamp_check
        CHECK (state = 'cancelled' OR cancelled_at IS NULL),
    CONSTRAINT operation_jobs_timestamp_order_check
        CHECK (
            (started_at IS NULL OR started_at >= created_at)
            AND (completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at)
            AND (cancelled_at IS NULL OR cancelled_at >= created_at)
        )
);

-- Active-run identity: at most one live logical run per (run_kind, dedupe_key,
-- input_fingerprint).  Terminal rows are ignored so a later explicit retry or
-- the next schedule window can enqueue again.
CREATE UNIQUE INDEX IF NOT EXISTS idx_operation_jobs_active_identity
    ON operation_jobs (run_kind, dedupe_key, input_fingerprint)
    WHERE state IN ('queued', 'leased', 'running', 'failed_retryable');

CREATE INDEX IF NOT EXISTS idx_operation_jobs_queue
    ON operation_jobs (priority DESC, not_before, created_at, id)
    WHERE state IN ('queued', 'failed_retryable');

CREATE INDEX IF NOT EXISTS idx_operation_jobs_lease_recovery
    ON operation_jobs (lease_expires_at, id)
    WHERE state IN ('leased', 'running');

CREATE INDEX IF NOT EXISTS idx_operation_jobs_correlation
    ON operation_jobs (correlation_id);

CREATE TABLE IF NOT EXISTS role_heartbeats (
    role TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    status TEXT NOT NULL,
    last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    detail JSONB NOT NULL DEFAULT '{}'::JSONB,
    PRIMARY KEY (role, instance_id),
    CONSTRAINT role_heartbeats_role_nonblank_check
        CHECK (BTRIM(role) <> ''),
    CONSTRAINT role_heartbeats_instance_nonblank_check
        CHECK (BTRIM(instance_id) <> ''),
    CONSTRAINT role_heartbeats_status_nonblank_check
        CHECK (BTRIM(status) <> ''),
    CONSTRAINT role_heartbeats_detail_object_check
        CHECK (JSONB_TYPEOF(detail) = 'object')
);

-- Freshness lookup per role; each process owns exactly one row keyed by
-- (role, instance_id) so replicas never overwrite each other's liveness.
CREATE INDEX IF NOT EXISTS idx_role_heartbeats_role_heartbeat
    ON role_heartbeats (role, last_heartbeat_at DESC);

CREATE TABLE IF NOT EXISTS quote_state (
    symbol TEXT PRIMARY KEY,
    price DOUBLE PRECISION NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT quote_state_symbol_nonblank_check
        CHECK (BTRIM(symbol) <> ''),
    CONSTRAINT quote_state_price_finite_check
        CHECK (price <> 'NaN' AND price <> 'Infinity' AND price <> '-Infinity')
);

CREATE INDEX IF NOT EXISTS idx_quote_state_updated
    ON quote_state (updated_at DESC);
