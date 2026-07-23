ALTER TABLE macro_series
    ADD COLUMN IF NOT EXISTS released_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS revision_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS positioning_reports (
    source TEXT NOT NULL,
    market_id TEXT NOT NULL,
    report_date DATE NOT NULL,
    category TEXT NOT NULL,
    long_positions BIGINT,
    short_positions BIGINT,
    net_position BIGINT,
    open_interest BIGINT,
    net_pct_open_interest DOUBLE PRECISION,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (source, market_id, report_date, category)
);

CREATE TABLE IF NOT EXISTS source_documents (
    document_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    institution TEXT,
    document_type TEXT NOT NULL,
    title TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    url TEXT,
    content TEXT,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_source_documents_institution_published
    ON source_documents (institution, published_at DESC);
