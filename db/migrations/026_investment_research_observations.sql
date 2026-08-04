CREATE TABLE IF NOT EXISTS investment_research_observations (
    observation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    industry TEXT NOT NULL,
    company TEXT,
    symbol TEXT,
    region TEXT,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    narrative JSONB NOT NULL DEFAULT '{}'::jsonb,
    themes TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    score NUMERIC(10, 4),
    state TEXT,
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_research_observations_source_check
        CHECK (source_kind IN ('report', 'news')),
    CONSTRAINT investment_research_observations_industry_check
        CHECK (industry IN (
            'Semiconductors & Compute',
            'Software, Cloud & Communications',
            'Energy & Utilities',
            'Industrials & Materials',
            'Financials & Real Estate',
            'Healthcare',
            'Consumer',
            'Aerospace & Defence',
            'Unclassified'
        )),
    CONSTRAINT investment_research_observations_source_unique
        UNIQUE (source_kind, source_id, industry)
);

CREATE INDEX IF NOT EXISTS idx_investment_research_observations_history
    ON investment_research_observations (industry, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_investment_research_observations_company
    ON investment_research_observations (symbol, company, observed_at DESC)
    WHERE source_kind = 'report';
CREATE INDEX IF NOT EXISTS idx_investment_research_observations_themes
    ON investment_research_observations USING GIN (themes)
    WHERE source_kind = 'news';

INSERT INTO investment_research_observations (
    source_kind, source_id, observed_at, industry, company, symbol, region,
    metrics, narrative, score, state, provenance
)
SELECT
    'report',
    d.document_id::TEXT,
    COALESCE(d.report_date::TIMESTAMPTZ, a.created_at),
    d.industry,
    d.company,
    d.symbol,
    d.region,
    COALESCE(a.facts->'metrics', '{}'::jsonb),
    jsonb_strip_nulls(jsonb_build_object(
        'summary', a.analysis->'summary',
        'thesis', a.analysis->'thesis',
        'qualitative', a.facts->'qualitative',
        'drivers', a.analysis->'drivers',
        'catalysts', a.analysis->'catalysts',
        'risks', a.analysis->'risks',
        'watch_items', a.analysis->'watch_items',
        'news_context', a.analysis->'news_context'
    )),
    CASE
        WHEN jsonb_typeof(a.analysis->'score') = 'number'
        THEN (a.analysis->>'score')::NUMERIC
        ELSE NULL
    END,
    a.analysis->>'state',
    jsonb_strip_nulls(jsonb_build_object(
        'document_id', d.document_id,
        'document_type', d.document_type,
        'filing_source', d.filing_source,
        'model', a.model,
        'extraction', a.analysis->'extraction'
    ))
FROM investment_documents AS d
JOIN investment_analyses AS a ON a.document_id = d.document_id
WHERE d.document_type = 'annual_report'
ON CONFLICT (source_kind, source_id, industry) DO UPDATE SET
    observed_at = EXCLUDED.observed_at,
    company = EXCLUDED.company,
    symbol = EXCLUDED.symbol,
    region = EXCLUDED.region,
    metrics = EXCLUDED.metrics,
    narrative = EXCLUDED.narrative,
    score = EXCLUDED.score,
    state = EXCLUDED.state,
    provenance = EXCLUDED.provenance,
    updated_at = NOW();
