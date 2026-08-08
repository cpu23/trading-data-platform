-- Phase 2 event ledger, transactional outbox, and source freshness state.
--
-- market_events is an append-only ledger: deliveries are immutable, and a
-- revised observation is represented by a new row linked through
-- revision_of_event_id. Do not update or delete ledger rows in application
-- code; duplicate deliveries use the identity constraint below.
--
-- event_outbox is the durable handoff from ingestion to processing. A worker
-- claims a row with claimed_at/claimed_by, increments attempt_count, and
-- clears the claim for retry after a retryable failure. completed_at is the
-- successful terminal acknowledgement; failed_at marks a terminal failure
-- that remains available to operators for investigation but is not leased.
--
-- Rollback notes (dependency-safe order): drop event_outbox indexes, then
-- event_outbox (it references market_events), then source_freshness_state,
-- then market_events. Dropping a table also drops its owned indexes and
-- constraints; never drop market_events before event_outbox.

CREATE TABLE IF NOT EXISTS market_events (
    id UUID PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    source_event_id TEXT,
    source_payload_id UUID,
    observed_at TIMESTAMPTZ NOT NULL,
    effective_at TIMESTAMPTZ,
    published_at TIMESTAMPTZ,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revision_of_event_id UUID REFERENCES market_events(id),
    content_hash TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    entities JSONB NOT NULL DEFAULT '[]'::JSONB,
    markets JSONB NOT NULL DEFAULT '[]'::JSONB,
    horizons TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    importance_hint DOUBLE PRECISION,
    payload JSONB NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    correlation_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT market_events_schema_version_check
        CHECK (schema_version = 1),
    CONSTRAINT market_events_event_type_nonblank_check
        CHECK (BTRIM(event_type) <> ''),
    CONSTRAINT market_events_source_nonblank_check
        CHECK (BTRIM(source) <> ''),
    CONSTRAINT market_events_content_hash_nonblank_check
        CHECK (BTRIM(content_hash) <> ''),
    CONSTRAINT market_events_dedupe_key_nonblank_check
        CHECK (BTRIM(dedupe_key) <> ''),
    CONSTRAINT market_events_entities_array_check
        CHECK (JSONB_TYPEOF(entities) = 'array'),
    CONSTRAINT market_events_markets_array_check
        CHECK (JSONB_TYPEOF(markets) = 'array'),
    CONSTRAINT market_events_payload_object_check
        CHECK (JSONB_TYPEOF(payload) = 'object'),
    CONSTRAINT market_events_metadata_object_check
        CHECK (JSONB_TYPEOF(metadata) = 'object'),
    CONSTRAINT market_events_importance_hint_check
        CHECK (importance_hint IS NULL OR importance_hint BETWEEN 0.0 AND 1.0),
    CONSTRAINT market_events_identity_unique
        UNIQUE (source, dedupe_key, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_market_events_event_type_effective_at
    ON market_events (event_type, effective_at DESC);
CREATE INDEX IF NOT EXISTS idx_market_events_source_ingested_at
    ON market_events (source, ingested_at DESC);
CREATE INDEX IF NOT EXISTS idx_market_events_correlation_id
    ON market_events (correlation_id);

CREATE TABLE IF NOT EXISTS event_outbox (
    id BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL REFERENCES market_events(id) ON DELETE CASCADE,
    topic TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claimed_at TIMESTAMPTZ,
    claimed_by TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    completed_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    CONSTRAINT event_outbox_topic_nonblank_check
        CHECK (BTRIM(topic) <> ''),
    CONSTRAINT event_outbox_attempt_count_check
        CHECK (attempt_count >= 0),
    CONSTRAINT event_outbox_event_topic_unique
        UNIQUE (event_id, topic)
);

CREATE INDEX IF NOT EXISTS idx_event_outbox_available_uncompleted
    ON event_outbox (available_at, id)
    WHERE completed_at IS NULL
      AND failed_at IS NULL;

CREATE TABLE IF NOT EXISTS source_freshness_state (
    source TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'never_run',
    expected_next_at TIMESTAMPTZ,
    last_attempt_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    last_observation_at TIMESTAMPTZ,
    last_material_change_at TIMESTAMPTZ,
    lag_seconds DOUBLE PRECISION,
    reason_code TEXT,
    detail JSONB NOT NULL DEFAULT '{}'::JSONB,
    cache_mode TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT source_freshness_state_source_nonblank_check
        CHECK (BTRIM(source) <> ''),
    CONSTRAINT source_freshness_state_state_check
        CHECK (state IN (
            'current',
            'expected_idle',
            'outside_schedule',
            'stale',
            'cached_fallback',
            'rate_limited',
            'delayed',
            'failed',
            'never_run',
            'disabled'
        )),
    CONSTRAINT source_freshness_state_lag_seconds_check
        CHECK (lag_seconds IS NULL OR lag_seconds >= 0),
    CONSTRAINT source_freshness_state_detail_object_check
        CHECK (JSONB_TYPEOF(detail) = 'object'),
    CONSTRAINT source_freshness_state_consecutive_failures_check
        CHECK (consecutive_failures >= 0)
);

CREATE INDEX IF NOT EXISTS idx_source_freshness_state_state_updated_at
    ON source_freshness_state (state, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_freshness_state_expected_next_at
    ON source_freshness_state (expected_next_at, source)
    WHERE expected_next_at IS NOT NULL;
