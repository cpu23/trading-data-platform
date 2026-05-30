CREATE TABLE structured_opinions (
    opinion_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    opinion_type TEXT NOT NULL,
    scope TEXT NOT NULL,
    direction TEXT,
    confidence TEXT,
    timeframe TEXT,
    summary TEXT,
    key_factors JSONB,
    reasoning TEXT,
    data_inputs JSONB,
    model_used TEXT,
    prompt_version TEXT,
    tokens_used INTEGER,
    cost_usd DOUBLE PRECISION,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_structured_opinions_type_scope_created
    ON structured_opinions (opinion_type, scope, created_at DESC);

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
    briefing_date DATE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    content TEXT,
    sections JSONB,
    opinion_ids UUID[],
    model_used TEXT,
    prompt_version TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (briefing_date)
);

CREATE TRIGGER daily_briefings_updated_at
    BEFORE UPDATE ON daily_briefings
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
