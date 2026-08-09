-- Evidence-bounded macro and dynamic investment research intelligence.
-- Additive and idempotent. Core source tables remain source-owned; normalized
-- evidence is an in-memory adapter contract rather than a duplicate data lake.

CREATE TABLE IF NOT EXISTS research_source_claims (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    evidence_type TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    claim_fingerprint TEXT NOT NULL UNIQUE,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object_value TEXT,
    unit TEXT,
    period TEXT,
    geography TEXT,
    direction TEXT,
    claim_kind TEXT NOT NULL,
    source_span TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    entities JSONB NOT NULL DEFAULT '[]'::JSONB,
    model_slug TEXT,
    prompt_version TEXT NOT NULL,
    generation_attempt_id UUID REFERENCES generation_attempts(attempt_id) ON DELETE SET NULL,
    input_fingerprint TEXT NOT NULL,
    provenance JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_source_claims_kind_check CHECK (
        claim_kind IN ('reported_fact', 'company_guidance', 'estimate', 'opinion')
    ),
    CONSTRAINT research_source_claims_confidence_check CHECK (confidence BETWEEN 0 AND 1),
    CONSTRAINT research_source_claims_entities_check CHECK (JSONB_TYPEOF(entities) = 'array'),
    CONSTRAINT research_source_claims_provenance_check CHECK (JSONB_TYPEOF(provenance) = 'object'),
    CONSTRAINT research_source_claims_source_unique UNIQUE (
        evidence_type, evidence_id, claim_fingerprint
    )
);
CREATE INDEX IF NOT EXISTS idx_research_source_claims_source
    ON research_source_claims (evidence_type, evidence_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_research_source_claims_observed
    ON research_source_claims (observed_at DESC, id);
CREATE INDEX IF NOT EXISTS idx_research_source_claims_entities
    ON research_source_claims USING GIN (entities);

CREATE TABLE IF NOT EXISTS research_cases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    semantic_fingerprint TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    definition TEXT NOT NULL,
    horizon TEXT NOT NULL DEFAULT 'unknown',
    case_type TEXT NOT NULL DEFAULT 'unclear',
    lifecycle_state TEXT NOT NULL DEFAULT 'candidate',
    origin TEXT NOT NULL DEFAULT 'discovered',
    economic_significance TEXT,
    market_sensitivity TEXT,
    persistence TEXT,
    breadth TEXT,
    investability TEXT,
    evidence_strength TEXT,
    time_sensitivity TEXT,
    importance_rationale JSONB NOT NULL DEFAULT '{}'::JSONB,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_evidence_at TIMESTAMPTZ NOT NULL,
    last_changed_at TIMESTAMPTZ NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 0,
    input_fingerprint TEXT NOT NULL,
    model_slug TEXT,
    prompt_version TEXT,
    generation_attempt_id UUID REFERENCES generation_attempts(attempt_id) ON DELETE SET NULL,
    correlation_id UUID REFERENCES cycle_runs(correlation_id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_cases_horizon_check CHECK (
        horizon IN ('intraday', 'days', 'weeks', 'months', 'multi_year', 'unknown')
    ),
    CONSTRAINT research_cases_type_check CHECK (
        case_type IN ('cyclical', 'structural', 'event_driven', 'unclear')
    ),
    CONSTRAINT research_cases_lifecycle_check CHECK (
        lifecycle_state IN (
            'candidate', 'forming', 'corroborated', 'research_ready',
            'mature', 'weakening', 'archived'
        )
    ),
    CONSTRAINT research_cases_origin_check CHECK (origin IN ('manual', 'discovered')),
    CONSTRAINT research_cases_importance_check CHECK (
        (economic_significance IS NULL OR economic_significance IN ('low', 'moderate', 'high')) AND
        (market_sensitivity IS NULL OR market_sensitivity IN ('low', 'moderate', 'high')) AND
        (persistence IS NULL OR persistence IN ('low', 'moderate', 'high')) AND
        (breadth IS NULL OR breadth IN ('low', 'moderate', 'high')) AND
        (investability IS NULL OR investability IN ('low', 'moderate', 'high')) AND
        (evidence_strength IS NULL OR evidence_strength IN ('low', 'moderate', 'high')) AND
        (time_sensitivity IS NULL OR time_sensitivity IN ('low', 'moderate', 'high'))
    ),
    CONSTRAINT research_cases_importance_rationale_check
        CHECK (JSONB_TYPEOF(importance_rationale) = 'object'),
    CONSTRAINT research_cases_version_check CHECK (current_version >= 0)
);
CREATE INDEX IF NOT EXISTS idx_research_cases_current
    ON research_cases (lifecycle_state, last_changed_at DESC, id)
    WHERE lifecycle_state <> 'archived';
CREATE INDEX IF NOT EXISTS idx_research_cases_evidence_time
    ON research_cases (last_evidence_at DESC, id);

CREATE TABLE IF NOT EXISTS research_case_aliases (
    case_id UUID NOT NULL REFERENCES research_cases(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (case_id, normalized_alias)
);
CREATE INDEX IF NOT EXISTS idx_research_case_aliases_lookup
    ON research_case_aliases (normalized_alias, case_id);

CREATE TABLE IF NOT EXISTS research_case_entities (
    case_id UUID NOT NULL REFERENCES research_cases(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,
    normalized_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (case_id, entity_type, normalized_key)
);
CREATE INDEX IF NOT EXISTS idx_research_case_entities_lookup
    ON research_case_entities (entity_type, normalized_key, case_id);

CREATE TABLE IF NOT EXISTS research_case_evidence (
    case_id UUID NOT NULL REFERENCES research_cases(id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    title TEXT NOT NULL,
    source_reference TEXT,
    relationship TEXT NOT NULL,
    evidence_fingerprint TEXT NOT NULL,
    source_timestamp TIMESTAMPTZ NOT NULL,
    excerpt TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (case_id, evidence_type, evidence_id, relationship),
    CONSTRAINT research_case_evidence_relationship_check CHECK (
        relationship IN ('supports', 'contradicts', 'context', 'invalidation')
    )
);
CREATE INDEX IF NOT EXISTS idx_research_case_evidence_lookup
    ON research_case_evidence (evidence_type, evidence_id, case_id);
CREATE INDEX IF NOT EXISTS idx_research_case_evidence_time
    ON research_case_evidence (case_id, source_timestamp DESC);

CREATE TABLE IF NOT EXISTS research_case_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES research_cases(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    input_fingerprint TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL,
    change_summary TEXT NOT NULL,
    payload JSONB NOT NULL,
    model_slug TEXT,
    prompt_version TEXT,
    generation_attempt_id UUID REFERENCES generation_attempts(attempt_id) ON DELETE SET NULL,
    correlation_id UUID REFERENCES cycle_runs(correlation_id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_case_snapshots_version_unique UNIQUE (case_id, version),
    CONSTRAINT research_case_snapshots_input_unique UNIQUE (case_id, input_fingerprint),
    CONSTRAINT research_case_snapshots_version_check CHECK (version > 0),
    CONSTRAINT research_case_snapshots_lifecycle_check CHECK (
        lifecycle_state IN (
            'candidate', 'forming', 'corroborated', 'research_ready',
            'mature', 'weakening', 'archived'
        )
    ),
    CONSTRAINT research_case_snapshots_payload_check CHECK (JSONB_TYPEOF(payload) = 'object')
);
CREATE INDEX IF NOT EXISTS idx_research_case_snapshots_history
    ON research_case_snapshots (case_id, version DESC);

CREATE TABLE IF NOT EXISTS research_causal_edges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES research_cases(id) ON DELETE CASCADE,
    edge_fingerprint TEXT NOT NULL,
    from_type TEXT NOT NULL,
    from_key TEXT NOT NULL,
    from_name TEXT NOT NULL,
    relationship TEXT NOT NULL,
    to_type TEXT NOT NULL,
    to_key TEXT NOT NULL,
    to_name TEXT NOT NULL,
    mechanism TEXT NOT NULL,
    epistemic_state TEXT NOT NULL,
    confidence DOUBLE PRECISION,
    missing_evidence TEXT[] NOT NULL DEFAULT '{}',
    break_conditions TEXT[] NOT NULL DEFAULT '{}',
    depth INTEGER NOT NULL,
    valid_from TIMESTAMPTZ,
    valid_to TIMESTAMPTZ,
    input_fingerprint TEXT NOT NULL,
    model_slug TEXT,
    prompt_version TEXT,
    generation_attempt_id UUID REFERENCES generation_attempts(attempt_id) ON DELETE SET NULL,
    superseded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_causal_edges_current_unique
        UNIQUE (case_id, edge_fingerprint, input_fingerprint),
    CONSTRAINT research_causal_edges_relationship_check CHECK (
        relationship IN (
            'supplies', 'purchases_from', 'consumes', 'depends_on',
            'raises_demand_for', 'reduces_demand_for', 'raises_supply_of',
            'reduces_supply_of', 'raises_cost_for', 'passes_cost_to',
            'constrains', 'substitutes_for', 'complements',
            'increases_capex_for', 'derives_revenue_from', 'exposed_to',
            'regulates', 'finances'
        )
    ),
    CONSTRAINT research_causal_edges_epistemic_check CHECK (
        epistemic_state IN ('observed', 'supported', 'hypothesis', 'rejected')
    ),
    CONSTRAINT research_causal_edges_confidence_check
        CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    CONSTRAINT research_causal_edges_depth_check CHECK (depth BETWEEN 1 AND 8),
    CONSTRAINT research_causal_edges_self_check CHECK (
        from_type <> to_type OR from_key <> to_key
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_research_causal_edges_one_current
    ON research_causal_edges (case_id, edge_fingerprint)
    WHERE superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_research_causal_edges_from
    ON research_causal_edges (case_id, from_type, from_key)
    WHERE superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_research_causal_edges_to
    ON research_causal_edges (case_id, to_type, to_key)
    WHERE superseded_at IS NULL;

CREATE TABLE IF NOT EXISTS research_causal_edge_evidence (
    edge_id UUID NOT NULL REFERENCES research_causal_edges(id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    relationship TEXT NOT NULL DEFAULT 'supports',
    excerpt TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (edge_id, evidence_type, evidence_id, relationship),
    CONSTRAINT research_causal_edge_evidence_relationship_check CHECK (
        relationship IN ('supports', 'contradicts', 'context', 'invalidation')
    )
);
CREATE INDEX IF NOT EXISTS idx_research_causal_edge_evidence_lookup
    ON research_causal_edge_evidence (evidence_type, evidence_id, edge_id);

CREATE TABLE IF NOT EXISTS research_value_capture_assessments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES research_cases(id) ON DELETE CASCADE,
    node_type TEXT NOT NULL,
    node_key TEXT NOT NULL,
    node_name TEXT NOT NULL,
    demand_impulse TEXT,
    revenue_exposure TEXT,
    volume_sensitivity TEXT,
    supply_responsiveness TEXT,
    scarcity TEXT,
    pricing_power TEXT,
    cost_pass_through TEXT,
    margin_sensitivity TEXT,
    capital_intensity TEXT,
    competitive_intensity TEXT,
    barriers_to_entry TEXT,
    capacity_lead_time TEXT,
    substitution_risk TEXT,
    balance_sheet_capacity TEXT,
    capital_allocation TEXT,
    public_market_investability TEXT,
    valuation TEXT,
    crowding TEXT,
    evidence_strength TEXT,
    assessment_rationale JSONB NOT NULL DEFAULT '{}'::JSONB,
    unknowns TEXT[] NOT NULL DEFAULT '{}',
    input_fingerprint TEXT NOT NULL,
    model_slug TEXT,
    prompt_version TEXT,
    generation_attempt_id UUID REFERENCES generation_attempts(attempt_id) ON DELETE SET NULL,
    superseded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_value_capture_identity_unique
        UNIQUE (case_id, node_type, node_key, input_fingerprint),
    CONSTRAINT research_value_capture_dimensions_check CHECK (
        (demand_impulse IS NULL OR demand_impulse IN ('low', 'moderate', 'high')) AND
        (revenue_exposure IS NULL OR revenue_exposure IN ('low', 'moderate', 'high')) AND
        (volume_sensitivity IS NULL OR volume_sensitivity IN ('low', 'moderate', 'high')) AND
        (supply_responsiveness IS NULL OR supply_responsiveness IN ('low', 'moderate', 'high')) AND
        (scarcity IS NULL OR scarcity IN ('low', 'moderate', 'high')) AND
        (pricing_power IS NULL OR pricing_power IN ('low', 'moderate', 'high')) AND
        (cost_pass_through IS NULL OR cost_pass_through IN ('low', 'moderate', 'high')) AND
        (margin_sensitivity IS NULL OR margin_sensitivity IN ('low', 'moderate', 'high')) AND
        (capital_intensity IS NULL OR capital_intensity IN ('low', 'moderate', 'high')) AND
        (competitive_intensity IS NULL OR competitive_intensity IN ('low', 'moderate', 'high')) AND
        (barriers_to_entry IS NULL OR barriers_to_entry IN ('low', 'moderate', 'high')) AND
        (capacity_lead_time IS NULL OR capacity_lead_time IN ('low', 'moderate', 'high')) AND
        (substitution_risk IS NULL OR substitution_risk IN ('low', 'moderate', 'high')) AND
        (balance_sheet_capacity IS NULL OR balance_sheet_capacity IN ('low', 'moderate', 'high')) AND
        (capital_allocation IS NULL OR capital_allocation IN ('low', 'moderate', 'high')) AND
        (public_market_investability IS NULL OR public_market_investability IN ('low', 'moderate', 'high')) AND
        (valuation IS NULL OR valuation IN ('low', 'moderate', 'high')) AND
        (crowding IS NULL OR crowding IN ('low', 'moderate', 'high')) AND
        (evidence_strength IS NULL OR evidence_strength IN ('low', 'moderate', 'high'))
    ),
    CONSTRAINT research_value_capture_rationale_check
        CHECK (JSONB_TYPEOF(assessment_rationale) = 'object')
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_research_value_capture_one_current
    ON research_value_capture_assessments (case_id, node_type, node_key)
    WHERE superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_research_value_capture_case
    ON research_value_capture_assessments (case_id, created_at DESC);

CREATE TABLE IF NOT EXISTS research_value_capture_evidence (
    assessment_id UUID NOT NULL REFERENCES research_value_capture_assessments(id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    excerpt TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (assessment_id, evidence_type, evidence_id)
);
CREATE INDEX IF NOT EXISTS idx_research_value_capture_evidence_lookup
    ON research_value_capture_evidence (evidence_type, evidence_id, assessment_id);

CREATE TABLE IF NOT EXISTS research_counterevidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES research_cases(id) ON DELETE CASCADE,
    counter_fingerprint TEXT NOT NULL,
    kind TEXT NOT NULL,
    statement TEXT NOT NULL,
    epistemic_state TEXT NOT NULL,
    evidence_type TEXT,
    evidence_id TEXT,
    edge_id UUID REFERENCES research_causal_edges(id) ON DELETE SET NULL,
    rationale TEXT,
    input_fingerprint TEXT NOT NULL,
    model_slug TEXT,
    prompt_version TEXT,
    generation_attempt_id UUID REFERENCES generation_attempts(attempt_id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_counterevidence_unique UNIQUE (case_id, counter_fingerprint),
    CONSTRAINT research_counterevidence_kind_check CHECK (
        kind IN (
            'alternative_explanation', 'contradicting_evidence', 'weak_edge',
            'assumption', 'invalidation'
        )
    ),
    CONSTRAINT research_counterevidence_epistemic_check CHECK (
        epistemic_state IN ('supported', 'hypothesis', 'rejected')
    ),
    CONSTRAINT research_counterevidence_reference_check CHECK (
        (evidence_type IS NULL AND evidence_id IS NULL) OR
        (evidence_type IS NOT NULL AND evidence_id IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_research_counterevidence_case
    ON research_counterevidence (case_id, created_at DESC, id);
CREATE INDEX IF NOT EXISTS idx_research_counterevidence_lookup
    ON research_counterevidence (evidence_type, evidence_id)
    WHERE evidence_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS research_counterevidence_evidence (
    counterevidence_id UUID NOT NULL REFERENCES research_counterevidence(id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    excerpt TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (counterevidence_id, evidence_type, evidence_id)
);
CREATE INDEX IF NOT EXISTS idx_research_counterevidence_evidence_lookup
    ON research_counterevidence_evidence (evidence_type, evidence_id, counterevidence_id);

CREATE TABLE IF NOT EXISTS research_data_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES research_cases(id) ON DELETE CASCADE,
    request_fingerprint TEXT NOT NULL,
    subject TEXT NOT NULL,
    requested_evidence_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    desired_frequency TEXT,
    priority TEXT NOT NULL DEFAULT 'moderate',
    status TEXT NOT NULL DEFAULT 'unresolved',
    candidate_source_class TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    model_slug TEXT,
    prompt_version TEXT,
    generation_attempt_id UUID REFERENCES generation_attempts(attempt_id) ON DELETE SET NULL,
    linked_evidence_type TEXT,
    linked_evidence_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    CONSTRAINT research_data_requests_unique UNIQUE (case_id, request_fingerprint),
    CONSTRAINT research_data_requests_priority_check
        CHECK (priority IN ('low', 'moderate', 'high')),
    CONSTRAINT research_data_requests_status_check CHECK (
        status IN ('unresolved', 'in_progress', 'satisfied', 'unavailable', 'cancelled')
    ),
    CONSTRAINT research_data_requests_source_class_check CHECK (
        candidate_source_class IN ('official', 'industry', 'company', 'market', 'academic', 'other')
    ),
    CONSTRAINT research_data_requests_link_check CHECK (
        (linked_evidence_type IS NULL AND linked_evidence_id IS NULL) OR
        (linked_evidence_type IS NOT NULL AND linked_evidence_id IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_research_data_requests_open
    ON research_data_requests (priority DESC, created_at, id)
    WHERE status IN ('unresolved', 'in_progress');
CREATE INDEX IF NOT EXISTS idx_research_data_requests_case
    ON research_data_requests (case_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS research_market_drivers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target TEXT NOT NULL,
    driver_key TEXT NOT NULL,
    driver_label TEXT NOT NULL,
    direction TEXT NOT NULL,
    strength TEXT NOT NULL,
    horizon TEXT NOT NULL,
    mechanism TEXT NOT NULL,
    changed_since_prior BOOLEAN NOT NULL DEFAULT FALSE,
    invalidation_conditions TEXT[] NOT NULL DEFAULT '{}',
    confidence DOUBLE PRECISION,
    confidence_rationale TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    model_slug TEXT,
    prompt_version TEXT NOT NULL,
    generation_attempt_id UUID REFERENCES generation_attempts(attempt_id) ON DELETE SET NULL,
    valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    superseded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_market_drivers_identity_unique
        UNIQUE (target, driver_key, input_fingerprint),
    CONSTRAINT research_market_drivers_direction_check CHECK (
        direction IN ('supportive', 'headwind', 'mixed', 'neutral', 'unknown')
    ),
    CONSTRAINT research_market_drivers_strength_check CHECK (
        strength IN ('low', 'moderate', 'high', 'unknown')
    ),
    CONSTRAINT research_market_drivers_horizon_check CHECK (
        horizon IN ('intraday', 'days', 'weeks', 'months', 'multi_year', 'unknown')
    ),
    CONSTRAINT research_market_drivers_confidence_check
        CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_research_market_drivers_one_current
    ON research_market_drivers (target, driver_key)
    WHERE superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_research_market_drivers_current
    ON research_market_drivers (changed_since_prior DESC, target, valid_from DESC)
    WHERE superseded_at IS NULL;

CREATE TABLE IF NOT EXISTS research_market_driver_evidence (
    driver_id UUID NOT NULL REFERENCES research_market_drivers(id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    relationship TEXT NOT NULL DEFAULT 'supports',
    excerpt TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (driver_id, evidence_type, evidence_id, relationship),
    CONSTRAINT research_market_driver_evidence_relationship_check CHECK (
        relationship IN ('supports', 'contradicts', 'context', 'invalidation')
    )
);
CREATE INDEX IF NOT EXISTS idx_research_market_driver_evidence_lookup
    ON research_market_driver_evidence (evidence_type, evidence_id, driver_id);

ALTER TABLE investment_themes
    ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'manual',
    ADD COLUMN IF NOT EXISTS source_case_id UUID,
    ADD COLUMN IF NOT EXISTS discovery_provenance JSONB NOT NULL DEFAULT '{}'::JSONB;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'investment_themes_origin_check'
    ) THEN
        ALTER TABLE investment_themes ADD CONSTRAINT investment_themes_origin_check
            CHECK (origin IN ('manual', 'discovered'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'investment_themes_source_case_fk'
    ) THEN
        ALTER TABLE investment_themes ADD CONSTRAINT investment_themes_source_case_fk
            FOREIGN KEY (source_case_id) REFERENCES research_cases(id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'investment_themes_discovery_provenance_check'
    ) THEN
        ALTER TABLE investment_themes ADD CONSTRAINT investment_themes_discovery_provenance_check
            CHECK (JSONB_TYPEOF(discovery_provenance) = 'object');
    END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS idx_investment_themes_source_case
    ON investment_themes (source_case_id)
    WHERE source_case_id IS NOT NULL;

ALTER TABLE cycle_runs DROP CONSTRAINT IF EXISTS cycle_runs_run_kind_check;
ALTER TABLE cycle_runs ADD CONSTRAINT cycle_runs_run_kind_check CHECK (
    run_kind IN ('cycle', 'collector', 'processor', 'news', 'filings', 'research')
);

CREATE INDEX IF NOT EXISTS idx_generation_attempts_research_input
    ON generation_attempts (
        stage,
        (request_metadata->>'input_fingerprint'),
        created_at DESC
    )
    WHERE status = 'validated' AND processor LIKE 'research_%';

CREATE OR REPLACE FUNCTION reject_research_immutable_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION '% is immutable', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS research_source_claims_immutable ON research_source_claims;
CREATE TRIGGER research_source_claims_immutable
    BEFORE UPDATE OR DELETE ON research_source_claims
    FOR EACH ROW EXECUTE FUNCTION reject_research_immutable_mutation();

DROP TRIGGER IF EXISTS research_case_snapshots_immutable ON research_case_snapshots;
CREATE TRIGGER research_case_snapshots_immutable
    BEFORE UPDATE OR DELETE ON research_case_snapshots
    FOR EACH ROW EXECUTE FUNCTION reject_research_immutable_mutation();
