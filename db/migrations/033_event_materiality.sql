-- Phase 5 deterministic, auditable materiality decisions.
--
-- One row is retained for every event/job routing evaluation, including
-- suppressed decisions.  The unique event/job key makes evaluation retries
-- idempotent without requiring the caller to commit.

CREATE TABLE IF NOT EXISTS event_materiality (
    id BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL REFERENCES market_events(id) ON DELETE CASCADE,
    job_type TEXT NOT NULL,
    importance DOUBLE PRECISION NOT NULL,
    relevance DOUBLE PRECISION NOT NULL,
    novelty DOUBLE PRECISION NOT NULL,
    source_confidence DOUBLE PRECISION NOT NULL,
    time_sensitivity DOUBLE PRECISION NOT NULL,
    score DOUBLE PRECISION NOT NULL,
    routing_threshold DOUBLE PRECISION NOT NULL,
    decision TEXT NOT NULL,
    suppression_reason TEXT,
    component_rationale JSONB NOT NULL DEFAULT '{}'::JSONB,
    component_provenance JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT event_materiality_job_type_nonblank_check
        CHECK (BTRIM(job_type) <> ''),
    CONSTRAINT event_materiality_importance_check
        CHECK (importance BETWEEN 0.0 AND 1.0),
    CONSTRAINT event_materiality_relevance_check
        CHECK (relevance BETWEEN 0.0 AND 1.0),
    CONSTRAINT event_materiality_novelty_check
        CHECK (novelty BETWEEN 0.0 AND 1.0),
    CONSTRAINT event_materiality_source_confidence_check
        CHECK (source_confidence BETWEEN 0.0 AND 1.0),
    CONSTRAINT event_materiality_time_sensitivity_check
        CHECK (time_sensitivity BETWEEN 0.0 AND 1.0),
    CONSTRAINT event_materiality_score_check
        CHECK (score BETWEEN 0.0 AND 1.0),
    CONSTRAINT event_materiality_threshold_check
        CHECK (routing_threshold BETWEEN 0.0 AND 1.0),
    CONSTRAINT event_materiality_decision_check
        CHECK (decision IN ('route', 'suppress')),
    CONSTRAINT event_materiality_rationale_object_check
        CHECK (JSONB_TYPEOF(component_rationale) = 'object'),
    CONSTRAINT event_materiality_provenance_object_check
        CHECK (JSONB_TYPEOF(component_provenance) = 'object'),
    CONSTRAINT event_materiality_event_job_unique
        UNIQUE (event_id, job_type)
);

CREATE INDEX IF NOT EXISTS idx_event_materiality_event_created_at
    ON event_materiality (event_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_event_materiality_decision_created_at
    ON event_materiality (decision, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_event_materiality_job_score
    ON event_materiality (job_type, score DESC);
