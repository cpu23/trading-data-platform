CREATE TABLE IF NOT EXISTS investment_documents (
    document_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company TEXT NOT NULL,
    symbol TEXT,
    region TEXT NOT NULL,
    industry TEXT NOT NULL,
    document_type TEXT NOT NULL,
    report_date DATE,
    source_url TEXT,
    filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    extracted_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ingested',
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_documents_region_check
        CHECK (region IN ('US', 'EU', 'ASIA')),
    CONSTRAINT investment_documents_type_check
        CHECK (document_type IN (
            'annual_report', 'investor_report', 'earnings_release',
            'investor_presentation', 'regulatory_filing', 'other'
        )),
    CONSTRAINT investment_documents_status_check
        CHECK (status IN ('ingested', 'analyzing', 'analyzed', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_investment_documents_company_date
    ON investment_documents (symbol, company, report_date DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_investment_documents_industry_region
    ON investment_documents (industry, region, report_date DESC);

CREATE TABLE IF NOT EXISTS investment_analyses (
    analysis_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL UNIQUE
        REFERENCES investment_documents(document_id) ON DELETE CASCADE,
    previous_document_id UUID
        REFERENCES investment_documents(document_id) ON DELETE SET NULL,
    facts JSONB NOT NULL,
    analysis JSONB NOT NULL,
    model TEXT NOT NULL,
    tokens_input INTEGER NOT NULL DEFAULT 0,
    tokens_output INTEGER NOT NULL DEFAULT 0,
    cost_usd NUMERIC(14, 8) NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_investment_analyses_created
    ON investment_analyses (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_investment_analyses_previous_document
    ON investment_analyses (previous_document_id);
