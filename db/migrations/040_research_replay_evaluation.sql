-- Point-in-time research replay, analytical evaluation, exploratory requests,
-- and factor-first macro context. Additive and idempotent.

CREATE TABLE IF NOT EXISTS research_replay_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    benchmark_id TEXT,
    replay_as_of TIMESTAMPTZ NOT NULL,
    evidence_source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    evidence_fingerprint TEXT NOT NULL,
    deterministic_input_fingerprint TEXT NOT NULL,
    execution_fingerprint TEXT NOT NULL,
    comparison_group TEXT,
    model_overrides JSONB NOT NULL DEFAULT '{}'::JSONB,
    prompt_overrides JSONB NOT NULL DEFAULT '{}'::JSONB,
    audit JSONB NOT NULL DEFAULT '{}'::JSONB,
    deterministic_metrics JSONB NOT NULL DEFAULT '{}'::JSONB,
    stage_metrics JSONB NOT NULL DEFAULT '[]'::JSONB,
    result_summary JSONB NOT NULL DEFAULT '{}'::JSONB,
    cost_usd NUMERIC(14, 8) NOT NULL DEFAULT 0,
    correlation_id UUID REFERENCES cycle_runs(correlation_id) ON DELETE SET NULL,
    cache_parent_run_id UUID REFERENCES research_replay_runs(id) ON DELETE SET NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_replay_runs_source_check CHECK (
        evidence_source IN ('database', 'synthetic_benchmark')
    ),
    CONSTRAINT research_replay_runs_status_check CHECK (
        status IN ('running', 'completed', 'completed_with_errors', 'failed', 'leakage_failed')
    ),
    CONSTRAINT research_replay_runs_json_check CHECK (
        JSONB_TYPEOF(model_overrides) = 'object' AND
        JSONB_TYPEOF(prompt_overrides) = 'object' AND
        JSONB_TYPEOF(audit) = 'object' AND
        JSONB_TYPEOF(deterministic_metrics) = 'object' AND
        JSONB_TYPEOF(stage_metrics) = 'array' AND
        JSONB_TYPEOF(result_summary) = 'object'
    ),
    CONSTRAINT research_replay_runs_cost_check CHECK (cost_usd >= 0)
);
CREATE INDEX IF NOT EXISTS idx_research_replay_runs_benchmark
    ON research_replay_runs (benchmark_id, replay_as_of, created_at DESC)
    WHERE benchmark_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_research_replay_runs_input
    ON research_replay_runs (
        deterministic_input_fingerprint, execution_fingerprint, created_at DESC
    );
CREATE INDEX IF NOT EXISTS idx_research_replay_runs_status
    ON research_replay_runs (status, created_at DESC);

CREATE TABLE IF NOT EXISTS research_replay_cases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    replay_run_id UUID NOT NULL REFERENCES research_replay_runs(id) ON DELETE CASCADE,
    semantic_fingerprint TEXT NOT NULL,
    title TEXT NOT NULL,
    definition TEXT NOT NULL,
    case_is_economic_proposition BOOLEAN NOT NULL,
    proposition_rationale TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL,
    first_qualifying_evidence_at TIMESTAMPTZ,
    first_detection_at TIMESTAMPTZ NOT NULL,
    evidence_count INTEGER NOT NULL,
    source_diversity INTEGER NOT NULL,
    maximum_graph_depth INTEGER NOT NULL DEFAULT 0,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_replay_cases_unique UNIQUE (replay_run_id, semantic_fingerprint),
    CONSTRAINT research_replay_cases_lifecycle_check CHECK (
        lifecycle_state IN (
            'candidate', 'forming', 'corroborated', 'research_ready',
            'mature', 'weakening', 'archived'
        )
    ),
    CONSTRAINT research_replay_cases_counts_check CHECK (
        evidence_count >= 0 AND source_diversity >= 0 AND maximum_graph_depth BETWEEN 0 AND 8
    ),
    CONSTRAINT research_replay_cases_payload_check CHECK (JSONB_TYPEOF(payload) = 'object')
);
CREATE INDEX IF NOT EXISTS idx_research_replay_cases_run
    ON research_replay_cases (replay_run_id, lifecycle_state, first_detection_at);
CREATE INDEX IF NOT EXISTS idx_research_replay_cases_semantic
    ON research_replay_cases (semantic_fingerprint, first_detection_at);

CREATE TABLE IF NOT EXISTS research_replay_timeline_events (
    id BIGSERIAL PRIMARY KEY,
    replay_run_id UUID NOT NULL REFERENCES research_replay_runs(id) ON DELETE CASCADE,
    benchmark_id TEXT,
    semantic_fingerprint TEXT,
    event_type TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    detail JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_replay_timeline_event_check CHECK (
        event_type IN (
            'evidence_started', 'candidate_generated', 'case_formed',
            'hypothesis_generated', 'case_corroborated', 'research_ready',
            'case_mature', 'case_weakened', 'case_archived', 'theme_promoted'
        )
    ),
    CONSTRAINT research_replay_timeline_detail_check CHECK (JSONB_TYPEOF(detail) = 'object'),
    CONSTRAINT research_replay_timeline_unique UNIQUE (
        replay_run_id, semantic_fingerprint, event_type, occurred_at
    )
);
CREATE INDEX IF NOT EXISTS idx_research_replay_timeline_benchmark
    ON research_replay_timeline_events (benchmark_id, occurred_at, id);

CREATE TABLE IF NOT EXISTS research_quality_metrics (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    replay_run_id UUID REFERENCES research_replay_runs(id) ON DELETE CASCADE,
    benchmark_id TEXT,
    metric_scope TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    metric_version TEXT NOT NULL,
    metrics JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_quality_metrics_scope_check CHECK (
        metric_scope IN ('replay', 'case', 'benchmark', 'cohort', 'comparison')
    ),
    CONSTRAINT research_quality_metrics_payload_check CHECK (JSONB_TYPEOF(metrics) = 'object'),
    CONSTRAINT research_quality_metrics_unique UNIQUE (
        replay_run_id, metric_scope, subject_id, metric_version
    )
);
CREATE INDEX IF NOT EXISTS idx_research_quality_metrics_subject
    ON research_quality_metrics (metric_scope, subject_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_research_quality_metrics_live_unique
    ON research_quality_metrics (metric_scope, subject_id, metric_version)
    WHERE replay_run_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_research_quality_metrics_benchmark
    ON research_quality_metrics (benchmark_id, created_at DESC)
    WHERE benchmark_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS research_benchmark_scorecards (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    replay_run_id UUID NOT NULL UNIQUE REFERENCES research_replay_runs(id) ON DELETE CASCADE,
    benchmark_id TEXT NOT NULL,
    scorecard_version TEXT NOT NULL,
    dimensions JSONB NOT NULL,
    human_annotations JSONB NOT NULL DEFAULT '{}'::JSONB,
    annotation_version INTEGER NOT NULL DEFAULT 0,
    annotated_by TEXT,
    annotated_at TIMESTAMPTZ,
    evaluator_model TEXT,
    evaluator_prompt_version TEXT,
    evaluator_judgment JSONB,
    evaluator_attempt_id UUID REFERENCES generation_attempts(attempt_id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_benchmark_scorecards_json_check CHECK (
        JSONB_TYPEOF(dimensions) = 'object' AND
        JSONB_TYPEOF(human_annotations) = 'object' AND
        (evaluator_judgment IS NULL OR JSONB_TYPEOF(evaluator_judgment) = 'object')
    ),
    CONSTRAINT research_benchmark_scorecards_annotation_version_check CHECK (
        annotation_version >= 0
    )
);
CREATE INDEX IF NOT EXISTS idx_research_benchmark_scorecards_benchmark
    ON research_benchmark_scorecards (benchmark_id, created_at DESC);

ALTER TABLE research_data_requests
    ADD COLUMN IF NOT EXISTS causal_edge_id UUID REFERENCES research_causal_edges(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS support_criteria TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS weakening_criteria TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS minimum_independent_sources INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS last_reconsidered_at TIMESTAMPTZ;
ALTER TABLE research_data_requests
    DROP CONSTRAINT IF EXISTS research_data_requests_status_check;
ALTER TABLE research_data_requests
    ADD CONSTRAINT research_data_requests_status_check CHECK (
        status IN (
            'unresolved', 'in_progress', 'partially_satisfied', 'satisfied',
            'unavailable', 'cancelled', 'obsolete'
        )
    );
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'research_data_requests_minimum_sources_check'
    ) THEN
        ALTER TABLE research_data_requests
            ADD CONSTRAINT research_data_requests_minimum_sources_check
            CHECK (minimum_independent_sources BETWEEN 1 AND 5);
    END IF;
END $$;
DROP INDEX IF EXISTS idx_research_data_requests_open;
CREATE INDEX idx_research_data_requests_open
    ON research_data_requests (priority DESC, created_at, id)
    WHERE status IN ('unresolved', 'in_progress', 'partially_satisfied');
CREATE INDEX IF NOT EXISTS idx_research_data_requests_edge
    ON research_data_requests (causal_edge_id, status)
    WHERE causal_edge_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS research_data_request_evidence (
    request_id UUID NOT NULL REFERENCES research_data_requests(id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    matched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    match_reason TEXT NOT NULL,
    PRIMARY KEY (request_id, evidence_type, evidence_id)
);
CREATE INDEX IF NOT EXISTS idx_research_data_request_evidence_lookup
    ON research_data_request_evidence (evidence_type, evidence_id, request_id);

CREATE TABLE IF NOT EXISTS research_economic_factors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    factor_key TEXT NOT NULL,
    factor_label TEXT NOT NULL,
    state TEXT NOT NULL,
    strength TEXT NOT NULL,
    horizon TEXT NOT NULL,
    mechanism TEXT NOT NULL,
    confidence DOUBLE PRECISION,
    confidence_rationale TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    model_slug TEXT,
    prompt_version TEXT NOT NULL,
    generation_attempt_id UUID REFERENCES generation_attempts(attempt_id) ON DELETE SET NULL,
    valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    superseded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_economic_factors_identity_unique UNIQUE (
        factor_key, input_fingerprint
    ),
    CONSTRAINT research_economic_factors_state_check CHECK (
        state IN ('rising', 'falling', 'stable', 'mixed', 'unknown')
    ),
    CONSTRAINT research_economic_factors_strength_check CHECK (
        strength IN ('low', 'moderate', 'high', 'unknown')
    ),
    CONSTRAINT research_economic_factors_horizon_check CHECK (
        horizon IN ('intraday', 'days', 'weeks', 'months', 'multi_year', 'unknown')
    ),
    CONSTRAINT research_economic_factors_confidence_check CHECK (
        confidence IS NULL OR confidence BETWEEN 0 AND 1
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_research_economic_factors_one_current
    ON research_economic_factors (factor_key) WHERE superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_research_economic_factors_current
    ON research_economic_factors (valid_from DESC, factor_key)
    WHERE superseded_at IS NULL;

CREATE TABLE IF NOT EXISTS research_economic_factor_evidence (
    factor_id UUID NOT NULL REFERENCES research_economic_factors(id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    relationship TEXT NOT NULL DEFAULT 'supports',
    excerpt TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (factor_id, evidence_type, evidence_id, relationship),
    CONSTRAINT research_economic_factor_evidence_relationship_check CHECK (
        relationship IN ('supports', 'contradicts', 'context', 'invalidation')
    )
);
CREATE INDEX IF NOT EXISTS idx_research_economic_factor_evidence_lookup
    ON research_economic_factor_evidence (evidence_type, evidence_id, factor_id);

CREATE TABLE IF NOT EXISTS research_factor_transmissions (
    factor_id UUID NOT NULL REFERENCES research_economic_factors(id) ON DELETE CASCADE,
    target TEXT NOT NULL,
    direction TEXT NOT NULL,
    mechanism TEXT NOT NULL,
    invalidation_conditions TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (factor_id, target),
    CONSTRAINT research_factor_transmissions_direction_check CHECK (
        direction IN ('supportive', 'headwind', 'mixed', 'neutral', 'unknown')
    )
);
CREATE INDEX IF NOT EXISTS idx_research_factor_transmissions_target
    ON research_factor_transmissions (target, factor_id);

ALTER TABLE research_market_drivers
    ADD COLUMN IF NOT EXISTS factor_id UUID
        REFERENCES research_economic_factors(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_research_market_drivers_factor
    ON research_market_drivers (factor_id) WHERE factor_id IS NOT NULL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_proc WHERE proname = 'reject_research_immutable_mutation'
    ) THEN
        DROP TRIGGER IF EXISTS research_replay_cases_immutable ON research_replay_cases;
        CREATE TRIGGER research_replay_cases_immutable
            BEFORE UPDATE OR DELETE ON research_replay_cases
            FOR EACH ROW EXECUTE FUNCTION reject_research_immutable_mutation();
        DROP TRIGGER IF EXISTS research_replay_timeline_immutable ON research_replay_timeline_events;
        CREATE TRIGGER research_replay_timeline_immutable
            BEFORE UPDATE OR DELETE ON research_replay_timeline_events
            FOR EACH ROW EXECUTE FUNCTION reject_research_immutable_mutation();
        DROP TRIGGER IF EXISTS research_quality_metrics_immutable ON research_quality_metrics;
        CREATE TRIGGER research_quality_metrics_immutable
            BEFORE UPDATE OR DELETE ON research_quality_metrics
            FOR EACH ROW EXECUTE FUNCTION reject_research_immutable_mutation();
    END IF;
END $$;

DO $$
BEGIN
    CREATE TRIGGER research_benchmark_scorecards_updated_at
        BEFORE UPDATE ON research_benchmark_scorecards
        FOR EACH ROW EXECUTE FUNCTION update_updated_at();
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;
