-- Phase 3 durable analysis jobs and versioned section snapshots.
--
-- analysis_jobs is the durable hand-off for bounded analysis/publication work.
-- Workers lease rows and transition state only while owning the current lease;
-- active dedupe identity is enforced by the partial unique index below.
-- section_snapshots stores immutable publication history.  A section/scope has
-- at most one published row, and publication replaces that row atomically in
-- the caller's transaction.
--
-- Rollback notes (dependency-safe order): drop section snapshot lookup and
-- uniqueness indexes, then section_snapshots (including its self-reference);
-- drop analysis job queue, lease, source, state, correlation, and uniqueness
-- indexes, then analysis_jobs.  analysis_jobs references market_events, so it
-- must be removed before market_events.  Dropping a table also drops its owned
-- indexes and constraints; never drop market_events before analysis_jobs.
-- This migration depends on 027_market_events_outbox_freshness.sql for
-- market_events, which supplies analysis_jobs.source_event_id.

CREATE TABLE IF NOT EXISTS analysis_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL DEFAULT 100,
    source_event_id UUID REFERENCES market_events(id) ON DELETE SET NULL,
    dedupe_key TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    not_before TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_expires_at TIMESTAMPTZ,
    claimed_by TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    payload JSONB NOT NULL,
    result_ref JSONB,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    correlation_id UUID NOT NULL,
    CONSTRAINT analysis_jobs_job_type_nonblank_check
        CHECK (BTRIM(job_type) <> ''),
    CONSTRAINT analysis_jobs_state_check
        CHECK (state IN (
            'queued',
            'leased',
            'running',
            'succeeded',
            'failed_retryable',
            'failed_terminal',
            'suppressed_duplicate',
            'suppressed_immaterial',
            'suppressed_budget',
            'cancelled'
        )),
    CONSTRAINT analysis_jobs_dedupe_key_nonblank_check
        CHECK (BTRIM(dedupe_key) <> ''),
    CONSTRAINT analysis_jobs_input_fingerprint_nonblank_check
        CHECK (BTRIM(input_fingerprint) <> ''),
    CONSTRAINT analysis_jobs_claimed_by_nonblank_check
        CHECK (claimed_by IS NULL OR BTRIM(claimed_by) <> ''),
    CONSTRAINT analysis_jobs_attempt_count_check
        CHECK (attempt_count >= 0),
    CONSTRAINT analysis_jobs_max_attempts_check
        CHECK (max_attempts > 0),
    CONSTRAINT analysis_jobs_attempts_within_limit_check
        CHECK (attempt_count <= max_attempts),
    CONSTRAINT analysis_jobs_payload_object_check
        CHECK (JSONB_TYPEOF(payload) = 'object'),
    CONSTRAINT analysis_jobs_result_ref_object_check
        CHECK (result_ref IS NULL OR JSONB_TYPEOF(result_ref) = 'object'),
    CONSTRAINT analysis_jobs_lease_consistency_check
        CHECK (
            state NOT IN ('leased', 'running')
            OR (claimed_by IS NOT NULL AND lease_expires_at IS NOT NULL)
        ),
    CONSTRAINT analysis_jobs_running_started_check
        CHECK (state <> 'running' OR started_at IS NOT NULL),
    CONSTRAINT analysis_jobs_terminal_timestamp_check
        CHECK (
            state NOT IN (
                'succeeded',
                'failed_terminal',
                'suppressed_duplicate',
                'suppressed_immaterial',
                'suppressed_budget'
            )
            OR completed_at IS NOT NULL
        ),
    CONSTRAINT analysis_jobs_nonterminal_completed_check
        CHECK (
            state IN (
                'succeeded',
                'failed_terminal',
                'suppressed_duplicate',
                'suppressed_immaterial',
                'suppressed_budget'
            )
            OR completed_at IS NULL
        ),
    CONSTRAINT analysis_jobs_cancelled_timestamp_check
        CHECK (state <> 'cancelled' OR cancelled_at IS NOT NULL),
    CONSTRAINT analysis_jobs_noncancelled_timestamp_check
        CHECK (state = 'cancelled' OR cancelled_at IS NULL),
    CONSTRAINT analysis_jobs_timestamp_order_check
        CHECK (
            (started_at IS NULL OR started_at >= created_at)
            AND (completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at)
            AND (cancelled_at IS NULL OR cancelled_at >= created_at)
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_analysis_jobs_active_identity
    ON analysis_jobs (job_type, dedupe_key, input_fingerprint)
    WHERE state IN ('queued', 'leased', 'running', 'failed_retryable');

CREATE INDEX IF NOT EXISTS idx_analysis_jobs_queue
    ON analysis_jobs (priority DESC, not_before, created_at, id)
    WHERE state IN ('queued', 'failed_retryable');

CREATE INDEX IF NOT EXISTS idx_analysis_jobs_lease_recovery
    ON analysis_jobs (lease_expires_at, priority DESC, created_at, id)
    WHERE state = 'leased';

CREATE INDEX IF NOT EXISTS idx_analysis_jobs_source_event
    ON analysis_jobs (source_event_id, created_at DESC)
    WHERE source_event_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_analysis_jobs_state_created_at
    ON analysis_jobs (state, created_at DESC, id);

CREATE INDEX IF NOT EXISTS idx_analysis_jobs_correlation_id
    ON analysis_jobs (correlation_id);

CREATE TABLE IF NOT EXISTS section_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    section_key TEXT NOT NULL,
    scope_key TEXT NOT NULL DEFAULT 'global',
    version BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    payload JSONB,
    render_context JSONB,
    content_hash TEXT NOT NULL,
    data_freshness_at TIMESTAMPTZ,
    analysis_freshness_at TIMESTAMPTZ,
    source_event_ids UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at TIMESTAMPTZ,
    supersedes_snapshot_id UUID REFERENCES section_snapshots(id) ON DELETE SET NULL,
    CONSTRAINT section_snapshots_section_key_nonblank_check
        CHECK (BTRIM(section_key) <> ''),
    CONSTRAINT section_snapshots_scope_key_nonblank_check
        CHECK (BTRIM(scope_key) <> ''),
    CONSTRAINT section_snapshots_version_positive_check
        CHECK (version > 0),
    CONSTRAINT section_snapshots_status_check
        CHECK (status IN ('draft', 'published', 'superseded', 'failed')),
    CONSTRAINT section_snapshots_payload_json_check
        CHECK (
            payload IS NULL
            OR JSONB_TYPEOF(payload) IN ('object', 'array')
        ),
    CONSTRAINT section_snapshots_render_context_object_check
        CHECK (
            render_context IS NULL
            OR JSONB_TYPEOF(render_context) = 'object'
        ),
    CONSTRAINT section_snapshots_content_hash_nonblank_check
        CHECK (BTRIM(content_hash) <> ''),
    CONSTRAINT section_snapshots_source_event_ids_array_check
        CHECK (source_event_ids IS NOT NULL),
    CONSTRAINT section_snapshots_publication_timestamp_check
        CHECK (
            (status IN ('published', 'superseded') AND published_at IS NOT NULL)
            OR (status IN ('draft', 'failed') AND published_at IS NULL)
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_section_snapshots_section_scope_version
    ON section_snapshots (section_key, scope_key, version);

CREATE UNIQUE INDEX IF NOT EXISTS idx_section_snapshots_current_published
    ON section_snapshots (section_key, scope_key)
    WHERE status = 'published';

CREATE INDEX IF NOT EXISTS idx_section_snapshots_history
    ON section_snapshots (section_key, scope_key, version DESC);

CREATE INDEX IF NOT EXISTS idx_section_snapshots_current_lookup
    ON section_snapshots (section_key, scope_key, status, version DESC);
