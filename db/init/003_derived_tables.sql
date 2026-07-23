CREATE TABLE structured_opinions (
    opinion_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    correlation_id UUID,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    opinion_type TEXT NOT NULL,
    scope TEXT NOT NULL,
    schema_version TEXT NOT NULL DEFAULT '1',
    lifecycle_status TEXT NOT NULL DEFAULT 'published'
        CHECK (lifecycle_status IN ('draft', 'validated', 'published', 'quarantined')),
    published_at TIMESTAMPTZ,
    baseline_opinion_id UUID REFERENCES structured_opinions(opinion_id),
    direction TEXT,
    confidence TEXT,
    timeframe TEXT,
    summary TEXT,
    key_factors JSONB,
    reasoning TEXT,
    payload JSONB NOT NULL DEFAULT '{}',
    data_inputs JSONB,
    model_used TEXT,
    prompt_version TEXT,
    tokens_used INTEGER,
    cost_usd DOUBLE PRECISION,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_structured_opinions_type_scope_created
    ON structured_opinions (opinion_type, scope, created_at DESC);

CREATE INDEX idx_structured_opinions_published
    ON structured_opinions (opinion_type, scope, published_at DESC)
    WHERE lifecycle_status = 'published';

CREATE TRIGGER structured_opinions_updated_at
    BEFORE UPDATE ON structured_opinions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();


CREATE TABLE regime_classifications (
    classification_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    scope TEXT NOT NULL,
    regime TEXT NOT NULL,
    sub_regime TEXT,
    confidence TEXT,
    supporting_data JSONB,
    opinion_id UUID REFERENCES structured_opinions(opinion_id),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_regime_classifications_scope_created
    ON regime_classifications (scope, created_at DESC);

CREATE TRIGGER regime_classifications_updated_at
    BEFORE UPDATE ON regime_classifications
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();


CREATE TABLE daily_briefings (
    briefing_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    correlation_id UUID,
    briefing_date DATE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    content TEXT,
    sections JSONB,
    opinion_ids UUID[],
    model_used TEXT,
    prompt_version TEXT,
    lifecycle_status TEXT NOT NULL DEFAULT 'published'
        CHECK (lifecycle_status IN ('validated', 'published', 'quarantined')),
    published_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (briefing_date, correlation_id)
);

CREATE TRIGGER daily_briefings_updated_at
    BEFORE UPDATE ON daily_briefings
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
