-- Phase 6 deterministic headline-market confirmation observations.
-- Observations are descriptive only and must never be interpreted as trade signals.
-- Rollback: stop confirmation producers/readers, then drop this table.

CREATE TABLE IF NOT EXISTS story_market_confirmations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    cluster_id UUID NOT NULL REFERENCES story_clusters(id) ON DELETE CASCADE,
    source_event_id UUID NOT NULL REFERENCES market_events(id) ON DELETE CASCADE,
    market_symbol TEXT NOT NULL,
    headline_at TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    pre_headline_move DOUBLE PRECISION,
    move_5m DOUBLE PRECISION,
    move_30m DOUBLE PRECISION,
    move_session DOUBLE PRECISION,
    flags JSONB NOT NULL DEFAULT '[]'::JSONB,
    missing_reasons JSONB NOT NULL DEFAULT '{}'::JSONB,
    provenance JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT story_market_confirmations_identity_unique
        UNIQUE (cluster_id, source_event_id, market_symbol)
);

CREATE INDEX IF NOT EXISTS idx_story_market_confirmations_cluster
    ON story_market_confirmations (cluster_id, observed_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_story_market_confirmations_event
    ON story_market_confirmations (source_event_id);
