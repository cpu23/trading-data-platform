-- Phase 9 normalized long-horizon research objects and deterministic filing
-- deltas.  Additive/idempotent; rollback drops the new tables in reverse
-- dependency order after stopping research consumers.

CREATE TABLE IF NOT EXISTS investment_themes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    definition TEXT NOT NULL,
    horizon TEXT NOT NULL DEFAULT 'multi_year',
    macro_drivers TEXT[] NOT NULL DEFAULT '{}',
    key_indicators TEXT[] NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    review_at TIMESTAMPTZ,
    invalidation_conditions JSONB NOT NULL DEFAULT '[]',
    confidence DOUBLE PRECISION,
    confidence_components JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_themes_status_check
        CHECK (status IN ('active', 'paused', 'retired')),
    CONSTRAINT investment_themes_confidence_check
        CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

CREATE TABLE IF NOT EXISTS investment_theme_entities (
    theme_id UUID NOT NULL REFERENCES investment_themes (id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    display_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (theme_id, entity_type, entity_id),
    CONSTRAINT investment_theme_entities_type_check
        CHECK (entity_type IN ('industry', 'company', 'symbol', 'macro_series'))
);
CREATE INDEX IF NOT EXISTS idx_investment_theme_entities_lookup
    ON investment_theme_entities (entity_type, entity_id);

CREATE TABLE IF NOT EXISTS investment_theses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    theme_id UUID NOT NULL REFERENCES investment_themes (id) ON DELETE CASCADE,
    company TEXT,
    symbol TEXT,
    claim TEXT NOT NULL,
    variant_perception TEXT,
    status TEXT NOT NULL DEFAULT 'candidate',
    horizon TEXT,
    review_at TIMESTAMPTZ,
    confidence DOUBLE PRECISION,
    invalidation_conditions JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_theses_status_check
        CHECK (status IN ('candidate', 'active', 'paused', 'closed')),
    CONSTRAINT investment_theses_confidence_check
        CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);
CREATE INDEX IF NOT EXISTS idx_investment_theses_theme_status
    ON investment_theses (theme_id, status);

CREATE TABLE IF NOT EXISTS investment_thesis_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thesis_id UUID NOT NULL REFERENCES investment_theses (id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    claim TEXT NOT NULL,
    variant_perception TEXT,
    confidence DOUBLE PRECISION,
    rationale TEXT,
    changed_by TEXT NOT NULL DEFAULT 'operator',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_thesis_versions_unique UNIQUE (thesis_id, version)
);

CREATE TABLE IF NOT EXISTS investment_thesis_evidence (
    thesis_id UUID NOT NULL REFERENCES investment_theses (id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    relationship TEXT NOT NULL,
    excerpt TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (thesis_id, evidence_type, evidence_id, relationship),
    CONSTRAINT investment_thesis_evidence_relationship_check
        CHECK (relationship IN ('supports', 'contradicts', 'context'))
);

CREATE TABLE IF NOT EXISTS investment_catalysts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thesis_id UUID NOT NULL REFERENCES investment_theses (id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    expected_at TIMESTAMPTZ,
    state TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_catalysts_state_check
        CHECK (state IN ('pending', 'confirmed', 'missed', 'expired'))
);

CREATE TABLE IF NOT EXISTS investment_risks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thesis_id UUID NOT NULL REFERENCES investment_theses (id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'counter_thesis',
    severity TEXT NOT NULL DEFAULT 'moderate',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_risks_kind_check
        CHECK (kind IN ('counter_thesis', 'execution', 'external')),
    CONSTRAINT investment_risks_severity_check
        CHECK (severity IN ('low', 'moderate', 'high'))
);

CREATE TABLE IF NOT EXISTS investment_watch_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thesis_id UUID NOT NULL REFERENCES investment_theses (id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    source_kind TEXT,
    source_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS company_research_profiles (
    company TEXT PRIMARY KEY,
    symbol TEXT,
    business_overview TEXT,
    segments JSONB NOT NULL DEFAULT '[]',
    key_operating_drivers TEXT[] NOT NULL DEFAULT '{}',
    capital_allocation TEXT,
    valuation_assumptions JSONB NOT NULL DEFAULT '{}',
    guidance JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS investment_filing_deltas (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES investment_documents (document_id) ON DELETE CASCADE,
    previous_document_id UUID REFERENCES investment_documents (document_id) ON DELETE SET NULL,
    category TEXT NOT NULL,
    change_kind TEXT NOT NULL,
    section_hash TEXT,
    previous_section_hash TEXT,
    excerpt TEXT,
    previous_excerpt TEXT,
    metrics JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_filing_deltas_category_check
        CHECK (category IN (
            'guidance', 'risk_language', 'segments', 'margins_cashflow',
            'capex', 'balance_sheet', 'capital_allocation', 'commitments',
            'management_language'
        )),
    CONSTRAINT investment_filing_deltas_change_kind_check
        CHECK (change_kind IN ('new', 'changed', 'removed', 'unchanged')),
    CONSTRAINT investment_filing_deltas_unique UNIQUE (document_id, category)
);
CREATE INDEX IF NOT EXISTS idx_investment_filing_deltas_previous
    ON investment_filing_deltas (previous_document_id);

CREATE TABLE IF NOT EXISTS portfolio_holdings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol TEXT NOT NULL UNIQUE,
    company TEXT,
    sector TEXT,
    country TEXT,
    currency TEXT,
    weight DOUBLE PRECISION NOT NULL DEFAULT 0,
    theme_tags TEXT[] NOT NULL DEFAULT '{}',
    rate_sensitivity TEXT,
    commodity_sensitivity TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT portfolio_holdings_weight_check CHECK (weight BETWEEN 0 AND 1)
);
