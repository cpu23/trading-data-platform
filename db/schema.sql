-- Authoritative fresh-deployment schema. Existing databases must be rebuilt.

-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
CREATE TABLE macro_series (
    series_id TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    value DOUBLE PRECISION,
    source TEXT NOT NULL,
    released_at TIMESTAMPTZ,
    revision_at TIMESTAMPTZ,
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (series_id, observed_at)
);

SELECT create_hypertable('macro_series', 'observed_at', migrate_data => true);

CREATE TRIGGER macro_series_updated_at
    BEFORE UPDATE ON macro_series
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();


CREATE TABLE macro_series_metadata (
    series_id TEXT PRIMARY KEY,
    title TEXT,
    units TEXT,
    seasonal_adjustment TEXT,
    frequency TEXT,
    fetched_at TIMESTAMPTZ NOT NULL
);


CREATE TABLE source_payload_cache (
    cache_key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    target_week TEXT NOT NULL,
    raw_payload JSONB NOT NULL,
    payload_hash TEXT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_source_payload_cache_source_week
    ON source_payload_cache (source, target_week);

CREATE TRIGGER source_payload_cache_updated_at
    BEFORE UPDATE ON source_payload_cache
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();


CREATE TABLE econ_events (
    event_id TEXT PRIMARY KEY,
    event_name TEXT NOT NULL,
    country TEXT NOT NULL,
    scheduled_at TIMESTAMPTZ NOT NULL,
    impact_level TEXT,
    consensus TEXT,
    previous TEXT,
    actual TEXT,
    source TEXT NOT NULL,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_econ_events_scheduled_at ON econ_events (scheduled_at);

CREATE TRIGGER econ_events_updated_at
    BEFORE UPDATE ON econ_events
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();


CREATE TABLE market_data (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    open DOUBLE PRECISION,
    high DOUBLE PRECISION,
    low DOUBLE PRECISION,
    close DOUBLE PRECISION,
    volume DOUBLE PRECISION,
    source TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (symbol, timeframe, timestamp)
);

SELECT create_hypertable('market_data', 'timestamp', migrate_data => true);

CREATE TRIGGER market_data_updated_at
    BEFORE UPDATE ON market_data
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TABLE positioning_reports (
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

CREATE TABLE source_documents (
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

CREATE INDEX idx_source_documents_institution_published
    ON source_documents (institution, published_at DESC);

-- ---------------------------------------------------------------------------
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

-- ---------------------------------------------------------------------------
CREATE TABLE collection_log (
    log_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    collector TEXT NOT NULL,
    status TEXT NOT NULL,
    records_fetched INTEGER,
    records_written INTEGER,
    error_message TEXT,
    error_traceback TEXT,
    duration_ms INTEGER,
    api_calls_made INTEGER,
    config_snapshot JSONB,
    correlation_id UUID
);


CREATE TABLE processing_log (
    log_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    processor TEXT NOT NULL,
    status TEXT NOT NULL,
    input_summary JSONB,
    output_id UUID,
    prompt_text TEXT,
    raw_response TEXT,
    model_used TEXT,
    tokens_input INTEGER,
    tokens_output INTEGER,
    cost_usd DOUBLE PRECISION,
    duration_ms INTEGER,
    error_message TEXT,
    input_fingerprint TEXT,
    skip_reason TEXT,
    forced BOOLEAN NOT NULL DEFAULT FALSE,
    correlation_id UUID
);

CREATE INDEX IF NOT EXISTS idx_processing_log_reusable_fingerprint
    ON processing_log (processor, completed_at DESC)
    INCLUDE (input_fingerprint, output_id)
    WHERE status = 'success' AND input_fingerprint IS NOT NULL;

-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cycle_runs (
  correlation_id UUID PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('accepted', 'running', 'completed', 'failed', 'abandoned')),
  accepted_at TIMESTAMPTZ NOT NULL,
  started_at TIMESTAMPTZ,
  heartbeat_at TIMESTAMPTZ,
  worker_id TEXT,
  idempotency_key TEXT,
  completed_at TIMESTAMPTZ,
  error_message TEXT,
  triggered_by TEXT NOT NULL DEFAULT 'manual',
  run_kind TEXT NOT NULL DEFAULT 'cycle'
    CONSTRAINT cycle_runs_run_kind_check
    CHECK (run_kind IN ('cycle', 'collector', 'processor', 'news')),
  requested_component TEXT,
  result_status TEXT,
  summary JSONB DEFAULT '{}',
  publication_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (publication_status IN ('pending', 'published', 'failed')),
  published_at TIMESTAMPTZ,
  baseline_correlation_id UUID REFERENCES cycle_runs(correlation_id),
  config_fingerprint TEXT
);

CREATE INDEX IF NOT EXISTS idx_cycle_runs_started_at ON cycle_runs (started_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cycle_runs_idempotency_key
  ON cycle_runs (idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_collection_log_correlation ON collection_log (correlation_id, started_at);
CREATE INDEX IF NOT EXISTS idx_processing_log_correlation ON processing_log (correlation_id, started_at);

-- ---------------------------------------------------------------------------
-- Phase 1b migration: add metadata column to econ_events
-- Stores extra per-event info (e.g. "all_day": true, "tentative": true)
ALTER TABLE econ_events ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}';

-- ---------------------------------------------------------------------------
-- Phase 2: Dashboard query performance indices

-- Latest record per series (for indicators section)
CREATE INDEX IF NOT EXISTS idx_macro_series_latest
  ON macro_series (series_id, observed_at DESC);

-- Recent collection log entries (for /logs page and staleness checks)
CREATE INDEX IF NOT EXISTS idx_collection_log_started
  ON collection_log (started_at DESC);

-- Recent processing log entries (for /logs page and health endpoint)
CREATE INDEX IF NOT EXISTS idx_processing_log_started
  ON processing_log (started_at DESC);

-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_payload_cache (
    cache_key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    target_week TEXT NOT NULL,
    raw_payload JSONB NOT NULL,
    payload_hash TEXT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_source_payload_cache_source_week
    ON source_payload_cache (source, target_week);

DO $$
BEGIN
    CREATE TRIGGER source_payload_cache_updated_at
        BEFORE UPDATE ON source_payload_cache
        FOR EACH ROW EXECUTE FUNCTION update_updated_at();
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- ---------------------------------------------------------------------------
ALTER TABLE collection_log
    ADD COLUMN IF NOT EXISTS correlation_id UUID REFERENCES cycle_runs(correlation_id);

ALTER TABLE processing_log
    ADD COLUMN IF NOT EXISTS correlation_id UUID REFERENCES cycle_runs(correlation_id);

ALTER TABLE cycle_runs
    ADD COLUMN IF NOT EXISTS run_kind TEXT NOT NULL DEFAULT 'cycle',
    ADD COLUMN IF NOT EXISTS requested_component TEXT,
    ADD COLUMN IF NOT EXISTS result_status TEXT,
    ADD COLUMN IF NOT EXISTS summary JSONB DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_collection_log_correlation
    ON collection_log (correlation_id, started_at);

CREATE INDEX IF NOT EXISTS idx_processing_log_correlation
    ON processing_log (correlation_id, started_at);

CREATE INDEX IF NOT EXISTS idx_cycle_runs_result_started
    ON cycle_runs (result_status, started_at DESC);

-- ---------------------------------------------------------------------------
-- TimescaleDB data retention policies.
-- These drop chunks older than the specified interval.

-- Keep macro series data for 5 years
SELECT add_retention_policy('macro_series', INTERVAL '5 years',
    if_not_exists => TRUE);

-- Keep market data for 2 years
SELECT add_retention_policy('market_data', INTERVAL '2 years',
    if_not_exists => TRUE);


-- ---------------------------------------------------------------------------
-- Add durable lifecycle metadata to cycle jobs without dropping historical data.
ALTER TABLE cycle_runs
    ADD COLUMN IF NOT EXISTS accepted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS worker_id TEXT,
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

-- CURRENT_TIMESTAMP is transaction-stable and is only a defensive fallback for
-- schemas where started_at was already made nullable before this migration.
UPDATE cycle_runs
SET accepted_at = COALESCE(started_at, CURRENT_TIMESTAMP)
WHERE accepted_at IS NULL;

UPDATE cycle_runs
SET heartbeat_at = started_at
WHERE status = 'running'
  AND heartbeat_at IS NULL;

ALTER TABLE cycle_runs
    ALTER COLUMN accepted_at SET NOT NULL,
    ALTER COLUMN started_at DROP NOT NULL;

-- Validate the expanded lifecycle while the bootstrap constraint still protects
-- writes. The known bootstrap name is cycle_runs_status_check.
ALTER TABLE cycle_runs
    ADD CONSTRAINT cycle_runs_status_check_expanded
    CHECK (status IN ('accepted', 'running', 'completed', 'failed', 'abandoned'))
    NOT VALID;

ALTER TABLE cycle_runs
    VALIDATE CONSTRAINT cycle_runs_status_check_expanded;

ALTER TABLE cycle_runs
    DROP CONSTRAINT cycle_runs_status_check;

ALTER TABLE cycle_runs
    RENAME CONSTRAINT cycle_runs_status_check_expanded TO cycle_runs_status_check;

CREATE UNIQUE INDEX IF NOT EXISTS idx_cycle_runs_idempotency_key
    ON cycle_runs (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS macro_series_metadata (
    series_id TEXT PRIMARY KEY,
    title TEXT,
    units TEXT,
    seasonal_adjustment TEXT,
    frequency TEXT,
    fetched_at TIMESTAMPTZ NOT NULL
);

-- ---------------------------------------------------------------------------
ALTER TABLE processing_log
    ADD COLUMN IF NOT EXISTS input_fingerprint TEXT,
    ADD COLUMN IF NOT EXISTS skip_reason TEXT,
    ADD COLUMN IF NOT EXISTS forced BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_processing_log_reusable_fingerprint
    ON processing_log (processor, completed_at DESC)
    INCLUDE (input_fingerprint, output_id)
    WHERE status = 'success' AND input_fingerprint IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Expand durable run lineage for scheduled and on-demand news collection.
-- The replacement is repeatable: the currently active constraint protects writes
-- while the new constraint is installed and validated.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'cycle_runs'::regclass
          AND conname = 'cycle_runs_run_kind_check_news'
    ) THEN
        ALTER TABLE cycle_runs
            ADD CONSTRAINT cycle_runs_run_kind_check_news
            CHECK (run_kind IN ('cycle', 'collector', 'processor', 'news'))
            NOT VALID;
    END IF;
END $$;

ALTER TABLE cycle_runs
    VALIDATE CONSTRAINT cycle_runs_run_kind_check_news;

ALTER TABLE cycle_runs
    DROP CONSTRAINT IF EXISTS cycle_runs_run_kind_check;

ALTER TABLE cycle_runs
    RENAME CONSTRAINT cycle_runs_run_kind_check_news
    TO cycle_runs_run_kind_check;

-- ---------------------------------------------------------------------------
ALTER TABLE structured_opinions
    ADD COLUMN IF NOT EXISTS correlation_id UUID REFERENCES cycle_runs(correlation_id),
    ADD COLUMN IF NOT EXISTS schema_version TEXT NOT NULL DEFAULT '1',
    ADD COLUMN IF NOT EXISTS lifecycle_status TEXT NOT NULL DEFAULT 'published',
    ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS baseline_opinion_id UUID REFERENCES structured_opinions(opinion_id),
    ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}';

UPDATE structured_opinions
SET published_at = COALESCE(published_at, created_at)
WHERE lifecycle_status = 'published';

ALTER TABLE structured_opinions
    DROP CONSTRAINT IF EXISTS structured_opinions_lifecycle_status_check;
ALTER TABLE structured_opinions
    ADD CONSTRAINT structured_opinions_lifecycle_status_check
    CHECK (lifecycle_status IN ('draft', 'validated', 'published', 'quarantined'));

CREATE INDEX IF NOT EXISTS idx_structured_opinions_published
    ON structured_opinions (opinion_type, scope, published_at DESC)
    WHERE lifecycle_status = 'published';

CREATE UNIQUE INDEX IF NOT EXISTS uq_opinion_per_cycle_type_scope
    ON structured_opinions (correlation_id, opinion_type, scope)
    WHERE correlation_id IS NOT NULL;

ALTER TABLE processing_log
    ADD COLUMN IF NOT EXISTS output_ids UUID[],
    ADD COLUMN IF NOT EXISTS request_metadata JSONB;

ALTER TABLE daily_briefings
    ADD COLUMN IF NOT EXISTS correlation_id UUID REFERENCES cycle_runs(correlation_id),
    ADD COLUMN IF NOT EXISTS lifecycle_status TEXT NOT NULL DEFAULT 'published',
    ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;

UPDATE daily_briefings
SET published_at = COALESCE(published_at, created_at)
WHERE lifecycle_status = 'published';

ALTER TABLE daily_briefings
    DROP CONSTRAINT IF EXISTS daily_briefings_briefing_date_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_daily_briefing_cycle
    ON daily_briefings (briefing_date, correlation_id);

ALTER TABLE cycle_runs
    ADD COLUMN IF NOT EXISTS publication_status TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS baseline_correlation_id UUID REFERENCES cycle_runs(correlation_id),
    ADD COLUMN IF NOT EXISTS config_fingerprint TEXT;

ALTER TABLE cycle_runs
    DROP CONSTRAINT IF EXISTS cycle_runs_publication_status_check;
ALTER TABLE cycle_runs
    ADD CONSTRAINT cycle_runs_publication_status_check
    CHECK (publication_status IN ('pending', 'published', 'failed'));

UPDATE cycle_runs
SET publication_status = CASE
    WHEN result_status IN ('success', 'partial') THEN 'published'
    WHEN status = 'failed' THEN 'failed'
    ELSE publication_status
END,
published_at = CASE
    WHEN result_status IN ('success', 'partial') THEN COALESCE(completed_at, started_at)
    ELSE published_at
END;

CREATE OR REPLACE FUNCTION prune_old_llm_payloads(retention_days INTEGER DEFAULT 90)
RETURNS INTEGER AS $$
DECLARE
    affected INTEGER;
BEGIN
    UPDATE processing_log
    SET prompt_text = NULL, raw_response = NULL
    WHERE completed_at < NOW() - make_interval(days => retention_days)
      AND (prompt_text IS NOT NULL OR raw_response IS NOT NULL);
    GET DIAGNOSTICS affected = ROW_COUNT;
    RETURN affected;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
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

-- ---------------------------------------------------------------------------
ALTER TABLE macro_series
    ADD COLUMN IF NOT EXISTS acquired_at TIMESTAMPTZ;

ALTER TABLE econ_events
    ADD COLUMN IF NOT EXISTS acquired_at TIMESTAMPTZ;

ALTER TABLE positioning_reports
    ADD COLUMN IF NOT EXISTS acquired_at TIMESTAMPTZ;

ALTER TABLE source_documents
    ADD COLUMN IF NOT EXISTS acquired_at TIMESTAMPTZ;

UPDATE macro_series
SET acquired_at = COALESCE(acquired_at, created_at)
WHERE acquired_at IS NULL;

UPDATE econ_events
SET acquired_at = COALESCE(acquired_at, created_at)
WHERE acquired_at IS NULL;

UPDATE positioning_reports
SET acquired_at = COALESCE(acquired_at, created_at)
WHERE acquired_at IS NULL;

UPDATE source_documents
SET acquired_at = COALESCE(acquired_at, created_at)
WHERE acquired_at IS NULL;

ALTER TABLE macro_series
    ALTER COLUMN acquired_at SET DEFAULT NOW();

ALTER TABLE econ_events
    ALTER COLUMN acquired_at SET DEFAULT NOW();

ALTER TABLE positioning_reports
    ALTER COLUMN acquired_at SET DEFAULT NOW();

ALTER TABLE source_documents
    ALTER COLUMN acquired_at SET DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_macro_series_source_acquired
    ON macro_series (source, acquired_at DESC);

CREATE INDEX IF NOT EXISTS idx_econ_events_source_acquired
    ON econ_events (source, acquired_at DESC);

CREATE INDEX IF NOT EXISTS idx_positioning_source_acquired
    ON positioning_reports (source, acquired_at DESC);

CREATE INDEX IF NOT EXISTS idx_documents_source_acquired
    ON source_documents (source, acquired_at DESC);

-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS generation_attempts (
    attempt_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    correlation_id UUID REFERENCES cycle_runs(correlation_id),
    processor TEXT NOT NULL,
    stage TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    status TEXT NOT NULL CHECK (status IN ('validated', 'validation_failed')),
    prompt_text TEXT,
    raw_response TEXT,
    validation_issues JSONB NOT NULL DEFAULT '[]',
    model_used TEXT,
    tokens_input INTEGER NOT NULL DEFAULT 0,
    tokens_output INTEGER NOT NULL DEFAULT 0,
    cost_usd DOUBLE PRECISION,
    duration_ms INTEGER,
    request_metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_generation_attempts_correlation
    ON generation_attempts (correlation_id, created_at);

CREATE INDEX IF NOT EXISTS idx_generation_attempts_failed
    ON generation_attempts (created_at DESC)
    WHERE status = 'validation_failed';

CREATE OR REPLACE FUNCTION prune_old_generation_attempts(
    retention_days INTEGER DEFAULT 90
)
RETURNS INTEGER AS $$
DECLARE
    affected INTEGER;
BEGIN
    DELETE FROM generation_attempts
    WHERE created_at < NOW() - make_interval(days => retention_days);
    GET DIAGNOSTICS affected = ROW_COUNT;
    RETURN affected;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
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

-- ---------------------------------------------------------------------------
-- 020: Add 'filings' run_kind for investment filing collection jobs.
ALTER TABLE cycle_runs DROP CONSTRAINT IF EXISTS cycle_runs_run_kind_check;
ALTER TABLE cycle_runs ADD CONSTRAINT cycle_runs_run_kind_check
  CHECK (run_kind = ANY (ARRAY[
    'cycle'::text,
    'collector'::text,
    'processor'::text,
    'news'::text,
    'filings'::text
  ]));

-- ---------------------------------------------------------------------------
ALTER TABLE investment_documents
    ADD COLUMN IF NOT EXISTS filing_source TEXT,
    ADD COLUMN IF NOT EXISTS filing_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_investment_documents_filing_identity
    ON investment_documents (filing_source, filing_id)
    WHERE filing_source IS NOT NULL AND filing_id IS NOT NULL;

-- ---------------------------------------------------------------------------
ALTER TABLE investment_documents
    DROP CONSTRAINT IF EXISTS investment_documents_type_check;

ALTER TABLE investment_documents
    ADD CONSTRAINT investment_documents_type_check
    CHECK (document_type IN (
        'annual_report', 'quarterly_report', 'investor_report',
        'earnings_release', 'investor_presentation',
        'regulatory_filing', 'other'
    ));

-- ---------------------------------------------------------------------------
ALTER TABLE investment_documents
    ADD COLUMN IF NOT EXISTS raw_content BYTEA;

-- ---------------------------------------------------------------------------
ALTER TABLE investment_analyses
    ADD COLUMN IF NOT EXISTS duration_ms INTEGER NOT NULL DEFAULT 0;

-- ---------------------------------------------------------------------------
UPDATE investment_documents
SET industry = CASE
    WHEN LOWER(COALESCE(industry, '')) ~ '(aerospace|defence|defense|aircraft|aviation|military)' THEN 'Aerospace & Defence'
    WHEN LOWER(COALESCE(industry, '')) ~ '(semiconductor|memory|dram|nand|foundry|chip|processor|compute hardware|asic|silicon|electronic|connector|data storage)' THEN 'Semiconductors & Compute'
    WHEN LOWER(COALESCE(industry, '')) ~ '(energy|oil|petroleum|gas|lng|utility|utilities|power|electric|renewable|solar|wind|nuclear|drilling|subsea)' THEN 'Energy & Utilities'
    WHEN LOWER(COALESCE(industry, '')) ~ '(bank|insurance|financial|capital market|asset manag|fintech|private equity|credit|real estate|reit|payment|broker|broking|savings institution|investment|wealth)' THEN 'Financials & Real Estate'
    WHEN LOWER(COALESCE(industry, '')) ~ '(consumer|retail|e-commerce|ecommerce|food|beverage|apparel|automotive|automobile|travel|leisure|hospitality|restaurant|tobacco|cigarette|education|hotel|entertainment|household|discount store|warehouse club)' THEN 'Consumer'
    WHEN LOWER(COALESCE(industry, '')) ~ '(healthcare|health care|biotech|pharma|drug|medical|life science|biological|hospital|surgical|orthopedic|therapeutic)' THEN 'Healthcare'
    WHEN LOWER(COALESCE(industry, '')) ~ '(software|cloud|data cent(er|re)|datacenter|communication|telecom|information technology|technology|internet|digital|media|programming|computer|network|cybersecurity|audio streaming|ai infrastructure)' THEN 'Software, Cloud & Communications'
    WHEN LOWER(COALESCE(industry, '')) ~ '(industrial|automation|robot|machinery|material|chemical|mining|metal|construction|transport|logistics|manufactur|railroad|equipment|building product|hardware|steel|copper)' THEN 'Industrials & Materials'
    ELSE 'Unclassified'
END,
updated_at = NOW();

UPDATE investment_analyses AS analysis
SET facts = CASE
        WHEN jsonb_typeof(analysis.facts->'classification') = 'object'
        THEN jsonb_set(analysis.facts, '{classification,industry}', to_jsonb(document.industry), false)
        ELSE analysis.facts
    END,
    analysis = CASE
        WHEN jsonb_typeof(analysis.analysis->'classification') = 'object'
        THEN jsonb_set(analysis.analysis, '{classification,industry}', to_jsonb(document.industry), false)
        ELSE analysis.analysis
    END,
    updated_at = NOW()
FROM investment_documents AS document
WHERE document.document_id = analysis.document_id;

ALTER TABLE investment_documents
    DROP CONSTRAINT IF EXISTS investment_documents_industry_check;

ALTER TABLE investment_documents
    ADD CONSTRAINT investment_documents_industry_check
    CHECK (
        industry IS NULL OR industry IN (
            'Semiconductors & Compute',
            'Software, Cloud & Communications',
            'Energy & Utilities',
            'Industrials & Materials',
            'Financials & Real Estate',
            'Healthcare',
            'Consumer',
            'Aerospace & Defence',
            'Unclassified'
        )
    );

-- ---------------------------------------------------------------------------
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

-- ---------------------------------------------------------------------------
-- Phase 2 event ledger, transactional outbox, and source freshness state.
--
-- market_events is an append-only ledger: deliveries are immutable, and a
-- revised observation is represented by a new row linked through
-- revision_of_event_id. Do not update or delete ledger rows in application
-- code; duplicate deliveries use the identity constraint below.
--
-- event_outbox is the durable handoff from ingestion to processing. A worker
-- claims a row with claimed_at/claimed_by, increments attempt_count, and
-- clears the claim for retry after a retryable failure. completed_at is the
-- successful terminal acknowledgement; failed_at marks a terminal failure
-- that remains available to operators for investigation but is not leased.
--
-- Rollback notes (dependency-safe order): drop event_outbox indexes, then
-- event_outbox (it references market_events), then source_freshness_state,
-- then market_events. Dropping a table also drops its owned indexes and
-- constraints; never drop market_events before event_outbox.

CREATE TABLE IF NOT EXISTS market_events (
    id UUID PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    source_event_id TEXT,
    source_payload_id UUID,
    observed_at TIMESTAMPTZ NOT NULL,
    effective_at TIMESTAMPTZ,
    published_at TIMESTAMPTZ,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revision_of_event_id UUID REFERENCES market_events(id),
    content_hash TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    entities JSONB NOT NULL DEFAULT '[]'::JSONB,
    markets JSONB NOT NULL DEFAULT '[]'::JSONB,
    horizons TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    importance_hint DOUBLE PRECISION,
    payload JSONB NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    correlation_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT market_events_schema_version_check
        CHECK (schema_version = 1),
    CONSTRAINT market_events_event_type_nonblank_check
        CHECK (BTRIM(event_type) <> ''),
    CONSTRAINT market_events_source_nonblank_check
        CHECK (BTRIM(source) <> ''),
    CONSTRAINT market_events_content_hash_nonblank_check
        CHECK (BTRIM(content_hash) <> ''),
    CONSTRAINT market_events_dedupe_key_nonblank_check
        CHECK (BTRIM(dedupe_key) <> ''),
    CONSTRAINT market_events_entities_array_check
        CHECK (JSONB_TYPEOF(entities) = 'array'),
    CONSTRAINT market_events_markets_array_check
        CHECK (JSONB_TYPEOF(markets) = 'array'),
    CONSTRAINT market_events_payload_object_check
        CHECK (JSONB_TYPEOF(payload) = 'object'),
    CONSTRAINT market_events_metadata_object_check
        CHECK (JSONB_TYPEOF(metadata) = 'object'),
    CONSTRAINT market_events_importance_hint_check
        CHECK (importance_hint IS NULL OR importance_hint BETWEEN 0.0 AND 1.0),
    CONSTRAINT market_events_identity_unique
        UNIQUE (source, dedupe_key, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_market_events_event_type_effective_at
    ON market_events (event_type, effective_at DESC);
CREATE INDEX IF NOT EXISTS idx_market_events_source_ingested_at
    ON market_events (source, ingested_at DESC);
CREATE INDEX IF NOT EXISTS idx_market_events_correlation_id
    ON market_events (correlation_id);

CREATE TABLE IF NOT EXISTS event_outbox (
    id BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL REFERENCES market_events(id) ON DELETE CASCADE,
    topic TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claimed_at TIMESTAMPTZ,
    claimed_by TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    completed_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    CONSTRAINT event_outbox_topic_nonblank_check
        CHECK (BTRIM(topic) <> ''),
    CONSTRAINT event_outbox_attempt_count_check
        CHECK (attempt_count >= 0),
    CONSTRAINT event_outbox_event_topic_unique
        UNIQUE (event_id, topic)
);

CREATE INDEX IF NOT EXISTS idx_event_outbox_available_uncompleted
    ON event_outbox (available_at, id)
    WHERE completed_at IS NULL
      AND failed_at IS NULL;

CREATE TABLE IF NOT EXISTS source_freshness_state (
    source TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'never_run',
    expected_next_at TIMESTAMPTZ,
    last_attempt_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    last_observation_at TIMESTAMPTZ,
    last_material_change_at TIMESTAMPTZ,
    lag_seconds DOUBLE PRECISION,
    reason_code TEXT,
    detail JSONB NOT NULL DEFAULT '{}'::JSONB,
    cache_mode TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT source_freshness_state_source_nonblank_check
        CHECK (BTRIM(source) <> ''),
    CONSTRAINT source_freshness_state_state_check
        CHECK (state IN (
            'current',
            'expected_idle',
            'outside_schedule',
            'stale',
            'cached_fallback',
            'rate_limited',
            'delayed',
            'failed',
            'never_run',
            'disabled'
        )),
    CONSTRAINT source_freshness_state_lag_seconds_check
        CHECK (lag_seconds IS NULL OR lag_seconds >= 0),
    CONSTRAINT source_freshness_state_detail_object_check
        CHECK (JSONB_TYPEOF(detail) = 'object'),
    CONSTRAINT source_freshness_state_consecutive_failures_check
        CHECK (consecutive_failures >= 0)
);

CREATE INDEX IF NOT EXISTS idx_source_freshness_state_state_updated_at
    ON source_freshness_state (state, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_freshness_state_expected_next_at
    ON source_freshness_state (expected_next_at, source)
    WHERE expected_next_at IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Canonical durable jobs and versioned section snapshots.
--
-- One table carries both analysis jobs and accepted operation runs. Workers
-- isolate the two kinds with the mutually exclusive job_type/run_kind columns.

CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type TEXT,
    run_kind TEXT,
    requested_component TEXT,
    source_event_id UUID REFERENCES market_events(id) ON DELETE SET NULL,
    correlation_id UUID NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL DEFAULT 100,
    dedupe_key TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    not_before TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_expires_at TIMESTAMPTZ,
    claimed_by TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    result_ref JSONB,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    CONSTRAINT jobs_kind_check CHECK (
        (job_type IS NOT NULL AND run_kind IS NULL)
        OR (job_type IS NULL AND run_kind IS NOT NULL)
    ),
    CONSTRAINT jobs_job_type_nonblank_check
        CHECK (job_type IS NULL OR BTRIM(job_type) <> ''),
    CONSTRAINT jobs_run_kind_check
        CHECK (run_kind IS NULL OR run_kind IN ('cycle', 'collector', 'processor', 'news', 'filings')),
    CONSTRAINT jobs_state_check CHECK (state IN (
        'queued', 'leased', 'running', 'succeeded', 'failed_retryable',
        'failed_terminal', 'suppressed_duplicate', 'suppressed_immaterial',
        'suppressed_budget', 'cancelled'
    )),
    CONSTRAINT jobs_dedupe_key_nonblank_check CHECK (BTRIM(dedupe_key) <> ''),
    CONSTRAINT jobs_input_fingerprint_nonblank_check CHECK (BTRIM(input_fingerprint) <> ''),
    CONSTRAINT jobs_claimed_by_nonblank_check
        CHECK (claimed_by IS NULL OR BTRIM(claimed_by) <> ''),
    CONSTRAINT jobs_attempt_count_check CHECK (attempt_count >= 0),
    CONSTRAINT jobs_max_attempts_check CHECK (max_attempts > 0),
    CONSTRAINT jobs_attempts_within_limit_check CHECK (attempt_count <= max_attempts),
    CONSTRAINT jobs_payload_object_check CHECK (JSONB_TYPEOF(payload) = 'object'),
    CONSTRAINT jobs_result_ref_object_check
        CHECK (result_ref IS NULL OR JSONB_TYPEOF(result_ref) = 'object'),
    CONSTRAINT jobs_lease_consistency_check CHECK (
        state NOT IN ('leased', 'running')
        OR (claimed_by IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CONSTRAINT jobs_running_started_check
        CHECK (state <> 'running' OR started_at IS NOT NULL),
    CONSTRAINT jobs_terminal_timestamp_check CHECK (
        state NOT IN (
            'succeeded', 'failed_terminal', 'suppressed_duplicate',
            'suppressed_immaterial', 'suppressed_budget'
        )
        OR completed_at IS NOT NULL
    ),
    CONSTRAINT jobs_nonterminal_completed_check CHECK (
        state IN (
            'succeeded', 'failed_terminal', 'suppressed_duplicate',
            'suppressed_immaterial', 'suppressed_budget'
        )
        OR completed_at IS NULL
    ),
    CONSTRAINT jobs_cancelled_timestamp_check
        CHECK (state <> 'cancelled' OR cancelled_at IS NOT NULL),
    CONSTRAINT jobs_noncancelled_timestamp_check
        CHECK (state = 'cancelled' OR cancelled_at IS NULL),
    CONSTRAINT jobs_timestamp_order_check CHECK (
        (started_at IS NULL OR started_at >= created_at)
        AND (completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at)
        AND (cancelled_at IS NULL OR cancelled_at >= created_at)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_analysis_identity
    ON jobs (job_type, dedupe_key, input_fingerprint)
    WHERE job_type IS NOT NULL
      AND state IN ('queued', 'leased', 'running', 'failed_retryable');

CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_operation_identity
    ON jobs (run_kind, dedupe_key, input_fingerprint)
    WHERE run_kind IS NOT NULL
      AND state IN ('queued', 'leased', 'running', 'failed_retryable');

CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_operation_correlation
    ON jobs (correlation_id)
    WHERE run_kind IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_jobs_analysis_queue
    ON jobs (priority DESC, not_before, created_at, id)
    WHERE job_type IS NOT NULL AND state IN ('queued', 'failed_retryable');

CREATE INDEX IF NOT EXISTS idx_jobs_operation_queue
    ON jobs (priority DESC, not_before, created_at, id)
    WHERE run_kind IS NOT NULL AND state IN ('queued', 'failed_retryable');

CREATE INDEX IF NOT EXISTS idx_jobs_lease_recovery
    ON jobs (lease_expires_at, priority DESC, created_at, id)
    WHERE state IN ('leased', 'running');

CREATE INDEX IF NOT EXISTS idx_jobs_source_event
    ON jobs (source_event_id, created_at DESC)
    WHERE source_event_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_jobs_state_created_at
    ON jobs (state, created_at DESC, id);

CREATE INDEX IF NOT EXISTS idx_jobs_correlation_id
    ON jobs (correlation_id);
CREATE TABLE IF NOT EXISTS section_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    section_key TEXT NOT NULL,
    scope_key TEXT NOT NULL DEFAULT 'global',
    version BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    payload JSONB,
    render_context JSONB,
    content_hash TEXT NOT NULL,
    data_freshness_at TIMESTAMPTZ,
    analysis_freshness_at TIMESTAMPTZ,
    source_event_ids UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at TIMESTAMPTZ,
    supersedes_snapshot_id UUID REFERENCES section_snapshots(id) ON DELETE SET NULL,
    CONSTRAINT section_snapshots_section_key_nonblank_check
        CHECK (BTRIM(section_key) <> ''),
    CONSTRAINT section_snapshots_scope_key_nonblank_check
        CHECK (BTRIM(scope_key) <> ''),
    CONSTRAINT section_snapshots_version_positive_check
        CHECK (version > 0),
    CONSTRAINT section_snapshots_status_check
        CHECK (status IN ('draft', 'published', 'superseded', 'failed')),
    CONSTRAINT section_snapshots_payload_json_check
        CHECK (
            payload IS NULL
            OR JSONB_TYPEOF(payload) IN ('object', 'array')
        ),
    CONSTRAINT section_snapshots_render_context_object_check
        CHECK (
            render_context IS NULL
            OR JSONB_TYPEOF(render_context) = 'object'
        ),
    CONSTRAINT section_snapshots_content_hash_nonblank_check
        CHECK (BTRIM(content_hash) <> ''),
    CONSTRAINT section_snapshots_source_event_ids_array_check
        CHECK (source_event_ids IS NOT NULL),
    CONSTRAINT section_snapshots_publication_timestamp_check
        CHECK (
            (status IN ('published', 'superseded') AND published_at IS NOT NULL)
            OR (status IN ('draft', 'failed') AND published_at IS NULL)
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_section_snapshots_section_scope_version
    ON section_snapshots (section_key, scope_key, version);

CREATE UNIQUE INDEX IF NOT EXISTS idx_section_snapshots_current_published
    ON section_snapshots (section_key, scope_key)
    WHERE status = 'published';

CREATE INDEX IF NOT EXISTS idx_section_snapshots_history
    ON section_snapshots (section_key, scope_key, version DESC);

CREATE INDEX IF NOT EXISTS idx_section_snapshots_current_lookup
    ON section_snapshots (section_key, scope_key, status, version DESC);

-- ---------------------------------------------------------------------------
-- Phase 5 deterministic market-state primitives.
--
-- Raw market_data is retained by the existing market_data policy. These
-- continuous aggregates are retained for shorter periods: 1m keeps 90 days,
-- 5m keeps 180 days, 15m keeps 1 year, 1h keeps 2 years, and 1d keeps 5
-- years. View retention drops only materialized aggregate chunks; it never
-- deletes rows from market_data.
--
-- Every statement is additive/idempotent. Rollback is dependency-safe:
-- remove feature consumers, drop market_feature_snapshots, then remove the
-- continuous-aggregate policies, indexes, and views in reverse order.

CREATE MATERIALIZED VIEW IF NOT EXISTS market_data_1m
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '1 minute', timestamp) AS bucket, symbol, source,
       first(open, timestamp) AS open, max(high) AS high, min(low) AS low,
       last(close, timestamp) AS close, sum(volume) AS volume, count(*) AS samples
FROM market_data
WHERE timeframe = 'PRICE'
GROUP BY bucket, symbol, source
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS market_data_5m
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '5 minutes', timestamp) AS bucket, symbol, source,
       first(open, timestamp) AS open, max(high) AS high, min(low) AS low,
       last(close, timestamp) AS close, sum(volume) AS volume, count(*) AS samples
FROM market_data
WHERE timeframe = 'PRICE'
GROUP BY bucket, symbol, source
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS market_data_15m
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '15 minutes', timestamp) AS bucket, symbol, source,
       first(open, timestamp) AS open, max(high) AS high, min(low) AS low,
       last(close, timestamp) AS close, sum(volume) AS volume, count(*) AS samples
FROM market_data
WHERE timeframe = 'PRICE'
GROUP BY bucket, symbol, source
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS market_data_1h
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '1 hour', timestamp) AS bucket, symbol, source,
       first(open, timestamp) AS open, max(high) AS high, min(low) AS low,
       last(close, timestamp) AS close, sum(volume) AS volume, count(*) AS samples
FROM market_data
WHERE timeframe = 'PRICE'
GROUP BY bucket, symbol, source
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS market_data_1d
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '1 day', timestamp) AS bucket, symbol, source,
       first(open, timestamp) AS open, max(high) AS high, min(low) AS low,
       last(close, timestamp) AS close, sum(volume) AS volume, count(*) AS samples
FROM market_data
WHERE timeframe = 'PRICE'
GROUP BY bucket, symbol, source
WITH NO DATA;

CREATE INDEX IF NOT EXISTS idx_market_data_1m_symbol_bucket
    ON market_data_1m (symbol, bucket DESC);
CREATE INDEX IF NOT EXISTS idx_market_data_5m_symbol_bucket
    ON market_data_5m (symbol, bucket DESC);
CREATE INDEX IF NOT EXISTS idx_market_data_15m_symbol_bucket
    ON market_data_15m (symbol, bucket DESC);
CREATE INDEX IF NOT EXISTS idx_market_data_1h_symbol_bucket
    ON market_data_1h (symbol, bucket DESC);
CREATE INDEX IF NOT EXISTS idx_market_data_1d_symbol_bucket
    ON market_data_1d (symbol, bucket DESC);

SELECT add_continuous_aggregate_policy('market_data_1m', start_offset => INTERVAL '90 days', end_offset => INTERVAL '1 minute', schedule_interval => INTERVAL '1 minute', if_not_exists => TRUE);
SELECT add_continuous_aggregate_policy('market_data_5m', start_offset => INTERVAL '180 days', end_offset => INTERVAL '5 minutes', schedule_interval => INTERVAL '5 minutes', if_not_exists => TRUE);
SELECT add_continuous_aggregate_policy('market_data_15m', start_offset => INTERVAL '1 year', end_offset => INTERVAL '15 minutes', schedule_interval => INTERVAL '15 minutes', if_not_exists => TRUE);
SELECT add_continuous_aggregate_policy('market_data_1h', start_offset => INTERVAL '2 years', end_offset => INTERVAL '1 hour', schedule_interval => INTERVAL '1 hour', if_not_exists => TRUE);
SELECT add_continuous_aggregate_policy('market_data_1d', start_offset => INTERVAL '5 years', end_offset => INTERVAL '1 day', schedule_interval => INTERVAL '1 day', if_not_exists => TRUE);

SELECT add_retention_policy('market_data_1m', INTERVAL '90 days', if_not_exists => TRUE);
SELECT add_retention_policy('market_data_5m', INTERVAL '180 days', if_not_exists => TRUE);
SELECT add_retention_policy('market_data_15m', INTERVAL '1 year', if_not_exists => TRUE);
SELECT add_retention_policy('market_data_1h', INTERVAL '2 years', if_not_exists => TRUE);
SELECT add_retention_policy('market_data_1d', INTERVAL '5 years', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS market_feature_snapshots (
    symbol TEXT NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    source_event_id UUID NOT NULL REFERENCES market_events(id),
    features JSONB NOT NULL,
    unavailable JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, as_of, source_event_id),
    CONSTRAINT market_feature_snapshots_symbol_nonblank_check CHECK (BTRIM(symbol) <> ''),
    CONSTRAINT market_feature_snapshots_features_object_check CHECK (JSONB_TYPEOF(features) = 'object'),
    CONSTRAINT market_feature_snapshots_unavailable_object_check CHECK (JSONB_TYPEOF(unavailable) = 'object')
);

CREATE INDEX IF NOT EXISTS idx_market_feature_snapshots_symbol_asof
    ON market_feature_snapshots (symbol, as_of DESC);
CREATE INDEX IF NOT EXISTS idx_market_feature_snapshots_source_event
    ON market_feature_snapshots (source_event_id);

-- ---------------------------------------------------------------------------
-- Phase 5 deterministic market-event reaction windows.
-- One row is retained for every material event, mapped instrument, and horizon.
-- The orchestrator owns the transaction; this migration is additive/idempotent.
CREATE TABLE IF NOT EXISTS event_reaction_windows (
    id BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL REFERENCES market_events(id) ON DELETE CASCADE,
    instrument_symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL DEFAULT 'PRICE',
    horizon TEXT NOT NULL,
    event_at TIMESTAMPTZ NOT NULL,
    baseline_at TIMESTAMPTZ,
    target_at TIMESTAMPTZ NOT NULL,
    baseline_price DOUBLE PRECISION,
    target_price DOUBLE PRECISION,
    observed_at TIMESTAMPTZ,
    observed_price DOUBLE PRECISION,
    absolute_move DOUBLE PRECISION,
    percentage_move DOUBLE PRECISION,
    volatility_adjusted_move DOUBLE PRECISION,
    expected_direction TEXT NOT NULL DEFAULT 'neutral',
    sensitivity TEXT NOT NULL DEFAULT 'neutral',
    direction_vs_expected TEXT NOT NULL DEFAULT 'unknown',
    reaction_state TEXT NOT NULL DEFAULT 'pending',
    missing_data_reason TEXT,
    source_payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    provenance JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT event_reaction_windows_identity_unique
        UNIQUE (event_id, instrument_symbol, horizon),
    CONSTRAINT event_reaction_windows_symbol_nonblank_check
        CHECK (BTRIM(instrument_symbol) <> ''),
    CONSTRAINT event_reaction_windows_timeframe_nonblank_check
        CHECK (BTRIM(timeframe) <> ''),
    CONSTRAINT event_reaction_windows_horizon_check
        CHECK (horizon IN ('1m', '5m', '15m', '30m', '60m', 'end_of_session')),
    CONSTRAINT event_reaction_windows_direction_check
        CHECK (expected_direction IN ('up', 'down', 'neutral')),
    CONSTRAINT event_reaction_windows_sensitivity_check
        CHECK (sensitivity IN ('positive', 'negative', 'neutral', 'high', 'moderate', 'low')),
    CONSTRAINT event_reaction_windows_direction_result_check
        CHECK (direction_vs_expected IN ('aligned', 'opposed', 'neutral', 'unknown')),
    CONSTRAINT event_reaction_windows_state_check
        CHECK (reaction_state IN ('pending', 'persistence', 'reversal', 'mixed')),
    CONSTRAINT event_reaction_windows_missing_reason_check
        CHECK (missing_data_reason IS NULL OR missing_data_reason IN (
            'future_window', 'missing_baseline', 'missing_target',
            'zero_baseline', 'zero_target')),
    CONSTRAINT event_reaction_windows_source_payload_object_check
        CHECK (JSONB_TYPEOF(source_payload) = 'object'),
    CONSTRAINT event_reaction_windows_provenance_object_check
        CHECK (JSONB_TYPEOF(provenance) = 'object'),
    CONSTRAINT event_reaction_windows_baseline_price_finite_check
        CHECK (baseline_price IS NULL OR
            baseline_price NOT IN ('NaN'::DOUBLE PRECISION, 'Infinity'::DOUBLE PRECISION,
                                   '-Infinity'::DOUBLE PRECISION)),
    CONSTRAINT event_reaction_windows_target_price_finite_check
        CHECK (target_price IS NULL OR
            target_price NOT IN ('NaN'::DOUBLE PRECISION, 'Infinity'::DOUBLE PRECISION,
                                 '-Infinity'::DOUBLE PRECISION)),
    CONSTRAINT event_reaction_windows_absolute_move_finite_check
        CHECK (absolute_move IS NULL OR
            absolute_move NOT IN ('NaN'::DOUBLE PRECISION, 'Infinity'::DOUBLE PRECISION,
                                  '-Infinity'::DOUBLE PRECISION)),
    CONSTRAINT event_reaction_windows_percentage_move_finite_check
        CHECK (percentage_move IS NULL OR
            percentage_move NOT IN ('NaN'::DOUBLE PRECISION, 'Infinity'::DOUBLE PRECISION,
                                    '-Infinity'::DOUBLE PRECISION)),
    CONSTRAINT event_reaction_windows_volatility_move_finite_check
        CHECK (volatility_adjusted_move IS NULL OR
            volatility_adjusted_move NOT IN ('NaN'::DOUBLE PRECISION, 'Infinity'::DOUBLE PRECISION,
                                             '-Infinity'::DOUBLE PRECISION))
);

CREATE INDEX IF NOT EXISTS idx_event_reaction_windows_pending_target
    ON event_reaction_windows (target_at, id)
    WHERE reaction_state = 'pending' OR missing_data_reason IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_event_reaction_windows_event_horizon
    ON event_reaction_windows (event_id, horizon, instrument_symbol);

CREATE INDEX IF NOT EXISTS idx_event_reaction_windows_instrument_observed
    ON event_reaction_windows (instrument_symbol, observed_at DESC)
    WHERE observed_at IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Phase 5 deterministic, revision-aware macro release cards.
--
-- macro_release_cards is append-only history. A revised release is a new
-- immutable row linked to the row it supersedes; the current table is the only
-- mutable pointer and makes current reads cheap without rewriting history.
-- Rollback: stop card producers/readers, drop macro_release_cards_current,
-- then macro_release_cards and its indexes.

CREATE TABLE IF NOT EXISTS macro_release_cards (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    release_identity TEXT NOT NULL,
    series_id TEXT,
    revision_number INTEGER NOT NULL DEFAULT 0,
    source_event_id UUID NOT NULL REFERENCES market_events(id),
    revision_of_event_id UUID REFERENCES market_events(id),
    supersedes_card_id UUID REFERENCES macro_release_cards(id),
    event_name TEXT NOT NULL,
    actual DOUBLE PRECISION,
    consensus DOUBLE PRECISION,
    previous DOUBLE PRECISION,
    revised_previous DOUBLE PRECISION,
    absolute_surprise DOUBLE PRECISION,
    standardized_surprise DOUBLE PRECISION,
    impact TEXT NOT NULL DEFAULT 'unknown',
    source TEXT NOT NULL,
    observed_at TIMESTAMPTZ,
    released_at TIMESTAMPTZ,
    revision_at TIMESTAMPTZ,
    quality_flags JSONB NOT NULL DEFAULT '[]'::JSONB,
    stage TEXT NOT NULL DEFAULT 't0',
    reaction_summary JSONB NOT NULL DEFAULT '{}'::JSONB,
    source_event_provenance JSONB NOT NULL DEFAULT '{}'::JSONB,
    source_payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT macro_release_cards_identity_nonblank_check
        CHECK (BTRIM(release_identity) <> ''),
    CONSTRAINT macro_release_cards_event_name_nonblank_check
        CHECK (BTRIM(event_name) <> ''),
    CONSTRAINT macro_release_cards_source_nonblank_check
        CHECK (BTRIM(source) <> ''),
    CONSTRAINT macro_release_cards_revision_nonnegative_check
        CHECK (revision_number >= 0),
    CONSTRAINT macro_release_cards_stage_check
        CHECK (stage IN ('t0', 'developing', 'reaction', 'final')),
    CONSTRAINT macro_release_cards_quality_flags_array_check
        CHECK (JSONB_TYPEOF(quality_flags) = 'array'),
    CONSTRAINT macro_release_cards_reaction_summary_object_check
        CHECK (JSONB_TYPEOF(reaction_summary) = 'object'),
    CONSTRAINT macro_release_cards_provenance_object_check
        CHECK (JSONB_TYPEOF(source_event_provenance) = 'object'),
    CONSTRAINT macro_release_cards_source_payload_object_check
        CHECK (JSONB_TYPEOF(source_payload) = 'object'),
    CONSTRAINT macro_release_cards_revision_event_check
        CHECK (revision_number = 0 OR revision_of_event_id IS NOT NULL),
    CONSTRAINT macro_release_cards_event_identity_unique
        UNIQUE (release_identity, source_event_id)
);

CREATE INDEX IF NOT EXISTS idx_macro_release_cards_identity_history
    ON macro_release_cards (release_identity, revision_number DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_macro_release_cards_series_history
    ON macro_release_cards (series_id, observed_at DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_macro_release_cards_source_event
    ON macro_release_cards (source_event_id);
CREATE INDEX IF NOT EXISTS idx_macro_release_cards_observed_at
    ON macro_release_cards (observed_at DESC);

CREATE TABLE IF NOT EXISTS macro_release_cards_current (
    release_identity TEXT PRIMARY KEY,
    card_id UUID NOT NULL REFERENCES macro_release_cards(id),
    stage TEXT NOT NULL DEFAULT 't0',
    reaction_summary JSONB NOT NULL DEFAULT '{}'::JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT macro_release_cards_current_stage_check
        CHECK (stage IN ('t0', 'developing', 'reaction', 'final')),
    CONSTRAINT macro_release_cards_current_reaction_summary_check
        CHECK (JSONB_TYPEOF(reaction_summary) = 'object')
);

CREATE INDEX IF NOT EXISTS idx_macro_release_cards_current_card
    ON macro_release_cards_current (card_id);
CREATE INDEX IF NOT EXISTS idx_macro_release_cards_current_updated_at
    ON macro_release_cards_current (updated_at DESC);

-- ---------------------------------------------------------------------------
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

-- ---------------------------------------------------------------------------
-- Phase 6 deterministic canonical news-story state and immutable audit history.
-- Additive/idempotent. Rollback: stop story producers/readers, then drop
-- story_cluster_versions, story_cluster_members, and story_clusters.

CREATE TABLE IF NOT EXISTS story_clusters (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    canonical_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    summary TEXT,
    state TEXT NOT NULL,
    lane TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    last_material_change_at TIMESTAMPTZ NOT NULL,
    importance DOUBLE PRECISION NOT NULL,
    novelty DOUBLE PRECISION NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    entities JSONB NOT NULL DEFAULT '[]'::JSONB,
    markets JSONB NOT NULL DEFAULT '[]'::JSONB,
    source_count INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1,
    change_summary TEXT,
    clustering_reason JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT story_clusters_state_check CHECK (
        state IN ('developing', 'confirmed', 'contradicted', 'stale', 'closed')
    ),
    CONSTRAINT story_clusters_lane_check CHECK (
        lane IN (
            'market_moving', 'watchlist_related', 'macro_central_banks',
            'filings_regulators', 'developing', 'low_confidence'
        )
    ),
    CONSTRAINT story_clusters_importance_check CHECK (importance BETWEEN 0 AND 1),
    CONSTRAINT story_clusters_novelty_check CHECK (novelty BETWEEN 0 AND 1),
    CONSTRAINT story_clusters_confidence_check CHECK (confidence BETWEEN 0 AND 1),
    CONSTRAINT story_clusters_source_count_check CHECK (source_count >= 1),
    CONSTRAINT story_clusters_version_check CHECK (version >= 1)
);

CREATE INDEX IF NOT EXISTS idx_story_clusters_lane_last_seen
    ON story_clusters (lane, last_seen_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_story_clusters_state_last_seen
    ON story_clusters (state, last_seen_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_story_clusters_entities_gin
    ON story_clusters USING GIN (entities);
CREATE INDEX IF NOT EXISTS idx_story_clusters_markets_gin
    ON story_clusters USING GIN (markets);

CREATE TABLE IF NOT EXISTS story_cluster_members (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    cluster_id UUID NOT NULL REFERENCES story_clusters(id) ON DELETE CASCADE,
    market_event_id UUID REFERENCES market_events(id) ON DELETE SET NULL,
    source TEXT NOT NULL,
    source_item_id TEXT NOT NULL,
    source_label TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    url TEXT,
    published_at TIMESTAMPTZ NOT NULL,
    similarity_score DOUBLE PRECISION NOT NULL,
    contribution_type TEXT NOT NULL,
    materially_changed BOOLEAN NOT NULL DEFAULT FALSE,
    clustering_reason JSONB NOT NULL DEFAULT '{}'::JSONB,
    entities JSONB NOT NULL DEFAULT '[]'::JSONB,
    markets JSONB NOT NULL DEFAULT '[]'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT story_cluster_members_source_item_unique UNIQUE (source, source_item_id),
    CONSTRAINT story_cluster_members_similarity_check CHECK (similarity_score BETWEEN 0 AND 1),
    CONSTRAINT story_cluster_members_contribution_check CHECK (
        contribution_type IN (
            'origin', 'repeated_coverage', 'material_update',
            'cross_source_confirmation', 'contradiction'
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_story_cluster_members_cluster_time
    ON story_cluster_members (cluster_id, published_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_story_cluster_members_event
    ON story_cluster_members (market_event_id)
    WHERE market_event_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS story_cluster_versions (
    id BIGSERIAL PRIMARY KEY,
    cluster_id UUID NOT NULL REFERENCES story_clusters(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    prior_state TEXT,
    state TEXT NOT NULL,
    contribution_type TEXT NOT NULL,
    change_summary TEXT NOT NULL,
    member_id UUID REFERENCES story_cluster_members(id) ON DELETE SET NULL,
    snapshot JSONB NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT story_cluster_versions_unique UNIQUE (cluster_id, version)
);

CREATE INDEX IF NOT EXISTS idx_story_cluster_versions_cluster
    ON story_cluster_versions (cluster_id, version DESC);

-- ---------------------------------------------------------------------------
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

-- ---------------------------------------------------------------------------
-- Phase 7 reusable, evidence-linked analytical claims.
-- Rollback: stop atom producers/readers, then drop these tables.
CREATE TABLE IF NOT EXISTS analysis_atoms (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    claim_type TEXT NOT NULL,
    claim TEXT NOT NULL,
    observation_text TEXT,
    interpretation_text TEXT,
    scenario_text TEXT,
    unknowns TEXT[] NOT NULL DEFAULT '{}',
    affected_assets JSONB NOT NULL DEFAULT '[]',
    time_horizon TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    confidence_components JSONB NOT NULL DEFAULT '{}',
    valid_from TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    carry_forward BOOLEAN NOT NULL DEFAULT FALSE,
    invalidation_conditions JSONB NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    supersedes_atom_id UUID REFERENCES analysis_atoms (id),
    source_event_id UUID REFERENCES market_events (id),
    prompt_version TEXT,
    model_slug TEXT,
    generation_attempt_id UUID,
    input_fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT analysis_atoms_confidence_bounds
        CHECK (confidence >= 0 AND confidence <= 1),
    CONSTRAINT analysis_atoms_status_allowed CHECK (
        status IN ('draft', 'validated', 'published', 'superseded', 'expired', 'retracted')
    )
);

CREATE INDEX IF NOT EXISTS idx_analysis_atoms_subject
    ON analysis_atoms (subject_type, subject_id, status);
CREATE INDEX IF NOT EXISTS idx_analysis_atoms_current
    ON analysis_atoms (status, valid_from DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_analysis_atoms_fingerprint
    ON analysis_atoms (input_fingerprint)
    WHERE status IN ('draft', 'validated', 'published');

CREATE TABLE IF NOT EXISTS analysis_atom_evidence (
    atom_id UUID NOT NULL REFERENCES analysis_atoms (id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    relationship TEXT NOT NULL,
    excerpt TEXT,
    source_timestamp TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (atom_id, evidence_type, evidence_id, relationship),
    CONSTRAINT analysis_atom_evidence_relationship_allowed CHECK (
        relationship IN ('supports', 'contradicts', 'context', 'invalidation')
    )
);

CREATE INDEX IF NOT EXISTS idx_analysis_atom_evidence_lookup
    ON analysis_atom_evidence (evidence_type, evidence_id);

-- ---------------------------------------------------------------------------
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

-- ---------------------------------------------------------------------------
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

-- ---------------------------------------------------------------------------
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

-- ---------------------------------------------------------------------------
-- Complete factor-first macro state introduced by migration 040.
-- Additive/idempotent. Rollback: drop the two added columns after reverting consumers.

ALTER TABLE research_economic_factors
    ADD COLUMN IF NOT EXISTS invalidation_conditions JSONB NOT NULL DEFAULT '[]'::JSONB,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'research_economic_factors_invalidation_check'
    ) THEN
        ALTER TABLE research_economic_factors
            ADD CONSTRAINT research_economic_factors_invalidation_check
            CHECK (JSONB_TYPEOF(invalidation_conditions) = 'array');
    END IF;
END $$;

-- A factor can revisit an earlier semantic state; only one current version is unique.
ALTER TABLE research_economic_factors
    DROP CONSTRAINT IF EXISTS research_economic_factors_identity_unique;

DO $$
BEGIN
    CREATE TRIGGER research_economic_factors_updated_at
        BEFORE UPDATE ON research_economic_factors
        FOR EACH ROW EXECUTE FUNCTION update_updated_at();
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- ---------------------------------------------------------------------------
-- Isolate longitudinal replay lifecycle history by resolved model/prompt variant.
-- Additive and idempotent. Rollback: drop the index, constraints, and columns.

ALTER TABLE research_replay_runs
    ADD COLUMN IF NOT EXISTS variant_fingerprint TEXT;
ALTER TABLE research_replay_runs
    ADD COLUMN IF NOT EXISTS variant_identity JSONB NOT NULL DEFAULT '{}'::JSONB;


UPDATE research_replay_runs
SET variant_fingerprint = execution_fingerprint
WHERE variant_fingerprint IS NULL;

ALTER TABLE research_replay_runs
    ALTER COLUMN variant_fingerprint SET NOT NULL;

DO $$
BEGIN
    ALTER TABLE research_replay_runs
        ADD CONSTRAINT research_replay_runs_variant_fingerprint_check
        CHECK (variant_fingerprint ~ '^[a-f0-9]{64}$');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE research_replay_runs
        ADD CONSTRAINT research_replay_runs_variant_identity_object_check
        CHECK (jsonb_typeof(variant_identity) = 'object');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_research_replay_runs_variant_timeline
    ON research_replay_runs (
        benchmark_id, variant_fingerprint, comparison_group,
        replay_as_of DESC, created_at DESC
    )
    WHERE benchmark_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Immutable human review history for deterministic benchmark scorecards.
-- Additive and idempotent. Rollback: drop the index and table.

CREATE TABLE IF NOT EXISTS research_benchmark_annotations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scorecard_id UUID NOT NULL
        REFERENCES research_benchmark_scorecards(id) ON DELETE CASCADE,
    annotation_version INTEGER NOT NULL,
    annotations JSONB NOT NULL,
    annotated_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_benchmark_annotations_version_check CHECK (
        annotation_version >= 1
    ),
    CONSTRAINT research_benchmark_annotations_payload_check CHECK (
        JSONB_TYPEOF(annotations) = 'object'
    ),
    CONSTRAINT research_benchmark_annotations_identity_unique UNIQUE (
        scorecard_id, annotation_version
    )
);

CREATE INDEX IF NOT EXISTS idx_research_benchmark_annotations_scorecard
    ON research_benchmark_annotations (scorecard_id, annotation_version DESC);

INSERT INTO research_benchmark_annotations (
    scorecard_id, annotation_version, annotations, annotated_by, created_at
)
SELECT id, annotation_version, human_annotations,
       COALESCE(NULLIF(annotated_by, ''), 'legacy_import'),
       COALESCE(annotated_at, updated_at, created_at)
FROM research_benchmark_scorecards
WHERE annotation_version > 0
ON CONFLICT (scorecard_id, annotation_version) DO NOTHING;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_proc WHERE proname = 'reject_research_immutable_mutation'
    ) THEN
        DROP TRIGGER IF EXISTS research_benchmark_annotations_immutable
            ON research_benchmark_annotations;
        CREATE TRIGGER research_benchmark_annotations_immutable
            BEFORE UPDATE OR DELETE ON research_benchmark_annotations
            FOR EACH ROW EXECUTE FUNCTION reject_research_immutable_mutation();
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Phase 5.1 calendar-aware reaction windows with timeframe-scoped identity.
--
-- Adds additive, nullable columns for persisted timestamp offsets and
-- calendar/volatility provenance, then performs a deterministic dedupe on the
-- NEW full identity BEFORE tightening the unique constraint.
--
-- The legacy unique identity (event_id, instrument_symbol, horizon) was
-- stricter than the new one, so legacy rows cannot collide across timeframes;
-- only rows sharing the full (event_id, instrument_symbol, timeframe, horizon)
-- identity can be duplicates (rows inserted before the constraint existed).
-- The deterministic winner per full identity is the most recently updated row
-- (ties broken by the greatest id, i.e. the most recently inserted row); every
-- other duplicate is deleted. Rows with distinct timeframes are preserved.
--
-- Rollback (dependency-safe order): drop the new indexes and constraints,
-- restore the legacy UNIQUE (event_id, instrument_symbol, horizon), then
-- drop the additive columns.

ALTER TABLE event_reaction_windows
    ADD COLUMN IF NOT EXISTS baseline_offset_seconds BIGINT,
    ADD COLUMN IF NOT EXISTS target_offset_seconds BIGINT,
    ADD COLUMN IF NOT EXISTS calendar_name TEXT,
    ADD COLUMN IF NOT EXISTS calendar_version TEXT,
    ADD COLUMN IF NOT EXISTS volatility_version INTEGER;

-- Deterministic dedupe on the new full identity: keep the most recently
-- updated row; ties broken by the greatest id.
DELETE FROM event_reaction_windows legacy
USING event_reaction_windows keep
WHERE keep.event_id = legacy.event_id
  AND keep.instrument_symbol = legacy.instrument_symbol
  AND keep.timeframe = legacy.timeframe
  AND keep.horizon = legacy.horizon
  AND (keep.updated_at, keep.id) > (legacy.updated_at, legacy.id);

-- Backfill timestamp offsets for pre-044 rows from persisted timestamps.
-- baseline_offset_seconds is measured from event_at and is strictly negative
-- (baseline rows are strictly pre-event); legacy rows whose baseline sat on
-- the event timestamp keep a NULL offset rather than violating that rule.
-- target_offset_seconds is measured from target_at (operationally the delay
-- between the planned target and the observed sample) and may be negative,
-- zero, or positive, so it carries no sign constraint.
UPDATE event_reaction_windows SET
    baseline_offset_seconds = CASE
        WHEN baseline_at IS NOT NULL AND event_at IS NOT NULL
             AND baseline_at < event_at
            THEN FLOOR(EXTRACT(EPOCH FROM (baseline_at - event_at)))::BIGINT
        ELSE NULL
    END,
    target_offset_seconds = CASE
        WHEN observed_at IS NOT NULL AND target_at IS NOT NULL
            THEN FLOOR(EXTRACT(EPOCH FROM (observed_at - target_at)))::BIGINT
        ELSE NULL
    END
WHERE baseline_offset_seconds IS NULL OR target_offset_seconds IS NULL;

-- Legacy volatility labeling: rows already resolved under the pre-044 per-bar
-- volatility carry volatility_version = 1 wherever the adjusted metric exists
-- (NULL stays reserved for rows with no volatility-based metric, so old
-- tick-vol semantics are never ambiguous or mixable with v2). An explicit
-- legacy marker is added to provenance without overwriting existing keys;
-- the explicit recompute path later relabels them to the current version.
UPDATE event_reaction_windows
SET volatility_version = 1
WHERE volatility_version IS NULL
  AND volatility_adjusted_move IS NOT NULL;

UPDATE event_reaction_windows
SET provenance = provenance
    || jsonb_build_object(
        'volatility',
        jsonb_build_object('version', 1, 'method', 'legacy_per_bar')
    )
WHERE volatility_version = 1
  AND NOT provenance ? 'volatility';

-- Tighten persisted identity to include timeframe so one event can hold
-- distinct reaction windows per instrument timeframe.
ALTER TABLE event_reaction_windows
    DROP CONSTRAINT IF EXISTS event_reaction_windows_identity_unique;
ALTER TABLE event_reaction_windows
    ADD CONSTRAINT event_reaction_windows_identity_unique
    UNIQUE (event_id, instrument_symbol, timeframe, horizon);

-- Constraint documentation for the new additive columns. Baseline offsets are
-- strictly negative when present; target offsets are intentionally unconstrained.
-- Guard each addition so the checksum-verified migration chain remains rerunnable.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'event_reaction_windows'::regclass
          AND conname = 'event_reaction_windows_baseline_offset_sign_check'
    ) THEN
        ALTER TABLE event_reaction_windows
            ADD CONSTRAINT event_reaction_windows_baseline_offset_sign_check
            CHECK (baseline_offset_seconds IS NULL OR baseline_offset_seconds < 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'event_reaction_windows'::regclass
          AND conname = 'event_reaction_windows_calendar_name_nonblank_check'
    ) THEN
        ALTER TABLE event_reaction_windows
            ADD CONSTRAINT event_reaction_windows_calendar_name_nonblank_check
            CHECK (calendar_name IS NULL OR BTRIM(calendar_name) <> '');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'event_reaction_windows'::regclass
          AND conname = 'event_reaction_windows_volatility_version_check'
    ) THEN
        ALTER TABLE event_reaction_windows
            ADD CONSTRAINT event_reaction_windows_volatility_version_check
            CHECK (volatility_version IS NULL OR volatility_version >= 1);
    END IF;
END $$;

-- Extend the missing-data-reason vocabulary with the post-selection baseline
-- freshness outcome (stale_baseline). Replaces the 031-era constraint; guarded
-- so the chain remains rerunnable.
ALTER TABLE event_reaction_windows
    DROP CONSTRAINT IF EXISTS event_reaction_windows_missing_reason_check;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'event_reaction_windows'::regclass
          AND conname = 'event_reaction_windows_missing_reason_check'
    ) THEN
        ALTER TABLE event_reaction_windows
            ADD CONSTRAINT event_reaction_windows_missing_reason_check
            CHECK (missing_data_reason IS NULL OR missing_data_reason IN (
                'future_window', 'missing_baseline', 'missing_target',
                'zero_baseline', 'zero_target', 'stale_baseline'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_event_reaction_windows_event_identity
    ON event_reaction_windows (event_id, timeframe, instrument_symbol, horizon);

-- ---------------------------------------------------------------------------
-- Durable admission reservations for the UTC-day LLM budget.
-- A paid call reserves an estimated cost before dispatch; the sum of
-- unreserved recorded spend plus active reservation estimates plus settled
-- reservation actuals (anchored to their reservation day) must fit under the
-- daily cap. Reservations settle with actual cost, or expire after their TTL
-- and release their estimate. Provenance (correlation, run kind, component,
-- requestor) is retained for audit, and lifecycle invariants are enforced by
-- CHECK constraints. Additive and idempotent.
-- Rollback: drop the table.

CREATE TABLE IF NOT EXISTS budget_reservations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    budget_day DATE NOT NULL,
    correlation_id UUID,
    run_kind TEXT,
    component TEXT,
    processor TEXT NOT NULL,
    requested_by TEXT,
    reason TEXT,
    estimated_usd NUMERIC(12, 6) NOT NULL,
    settled_usd NUMERIC(12, 6),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'settled', 'expired', 'released')),
    reserved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    settled_at TIMESTAMPTZ,
    CONSTRAINT budget_reservations_estimate_positive CHECK (estimated_usd > 0),
    CONSTRAINT budget_reservations_settle_nonnegative CHECK (
        settled_usd IS NULL OR settled_usd >= 0
    ),
    CONSTRAINT budget_reservations_expiry_after_reservation CHECK (
        expires_at > reserved_at
    ),
    CONSTRAINT budget_reservations_processor_nonblank CHECK (
        length(trim(processor)) > 0
    ),
    CONSTRAINT budget_reservations_component_nonblank CHECK (
        component IS NULL OR length(trim(component)) > 0
    ),
    CONSTRAINT budget_reservations_run_kind_nonblank CHECK (
        run_kind IS NULL OR length(trim(run_kind)) > 0
    ),
    CONSTRAINT budget_reservations_active_unsettled CHECK (
        status <> 'active' OR (settled_usd IS NULL AND settled_at IS NULL)
    ),
    CONSTRAINT budget_reservations_settled_complete CHECK (
        status <> 'settled' OR (settled_usd IS NOT NULL AND settled_at IS NOT NULL)
    ),
    CONSTRAINT budget_reservations_released_complete CHECK (
        status <> 'released' OR (settled_usd = 0 AND settled_at IS NOT NULL)
    ),
    CONSTRAINT budget_reservations_expired_unsettled CHECK (
        status <> 'expired' OR (settled_usd IS NULL AND settled_at IS NULL)
    ),
    CONSTRAINT budget_reservations_settle_after_reserve CHECK (
        settled_at IS NULL OR settled_at >= reserved_at
    ),
    CONSTRAINT budget_reservations_day_matches_reservation CHECK (
        budget_day = (reserved_at AT TIME ZONE 'UTC')::date
    )
);

CREATE INDEX IF NOT EXISTS idx_budget_reservations_day_status
    ON budget_reservations (budget_day, status, expires_at);

CREATE INDEX IF NOT EXISTS idx_budget_reservations_correlation
    ON budget_reservations (correlation_id);

-- ---------------------------------------------------------------------------
-- Worker liveness and persisted quote state.
--
-- role_heartbeats is written by the web and combined worker runtimes.
-- quote_state persists the latest observed quote for polling HTTP readers.


CREATE TABLE IF NOT EXISTS role_heartbeats (
    role TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    status TEXT NOT NULL,
    last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    detail JSONB NOT NULL DEFAULT '{}'::JSONB,
    PRIMARY KEY (role, instance_id),
    CONSTRAINT role_heartbeats_role_nonblank_check
        CHECK (BTRIM(role) <> ''),
    CONSTRAINT role_heartbeats_instance_nonblank_check
        CHECK (BTRIM(instance_id) <> ''),
    CONSTRAINT role_heartbeats_status_nonblank_check
        CHECK (BTRIM(status) <> ''),
    CONSTRAINT role_heartbeats_detail_object_check
        CHECK (JSONB_TYPEOF(detail) = 'object')
);

-- Freshness lookup per role; each process owns exactly one row keyed by
-- (role, instance_id) so replicas never overwrite each other's liveness.
CREATE INDEX IF NOT EXISTS idx_role_heartbeats_role_heartbeat
    ON role_heartbeats (role, last_heartbeat_at DESC);

CREATE TABLE IF NOT EXISTS quote_state (
    symbol TEXT PRIMARY KEY,
    price DOUBLE PRECISION NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT quote_state_symbol_nonblank_check
        CHECK (BTRIM(symbol) <> ''),
    CONSTRAINT quote_state_price_finite_check
        CHECK (price <> 'NaN' AND price <> 'Infinity' AND price <> '-Infinity')
);

CREATE INDEX IF NOT EXISTS idx_quote_state_updated
    ON quote_state (updated_at DESC);

-- ---------------------------------------------------------------------------
-- Durable content-addressed file storage for investment document uploads.
--
-- Async HTTP ingests persist the bounded upload on the shared news data
-- volume (content-addressed path, atomic write) instead of binding BYTEA;
-- the durable analysis worker extracts from the path later. Legacy rows keep
-- raw_content BYTEA; content_path is NULL for them. Additive and idempotent.
-- Rollback: ALTER TABLE investment_documents DROP COLUMN content_path;

ALTER TABLE investment_documents
    ADD COLUMN IF NOT EXISTS content_path TEXT;

CREATE INDEX IF NOT EXISTS idx_investment_documents_content_path
    ON investment_documents (content_path)
    WHERE content_path IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Autonomous thesis-fusion desk: additive foundation.
--
-- Extends the canonical investment_theses record with autonomy/scoring
-- columns and investment_thesis_evidence with provenance/weight columns,
-- then creates the shared desk tables: thesis groups, versioned group
-- membership, versioned scenarios, versioned forecasts with stable
-- point-in-time identity, forecast outcomes, opportunity snapshots,
-- falsification runs, and position-thesis links.
--
-- Invalidation is a first-class evidence relationship: the canonical
-- relationship CHECK is swapped (under its original name, with the original
-- composite primary key untouched) to admit 'invalidation'.  Scenario
-- probability is nullable so unknown legs stay distinct from conviction
-- (they are never defaulted); each scenario stores a bounded, finite
-- expected_return with the domain's +/-100 magnitude cap.
--
-- Existing manual rows stay valid under neutral defaults: origin 'manual',
-- direction 'neutral', zero scores, and full effective_weight for legacy
-- manual evidence (desk evidence weights are computed by the scoring
-- module). Evidence is deduped by independence_key; NULL keys are exempt
-- so manual rows and pre-desk inserts are unaffected.  Forecast rows are
-- immutable except the one-time NULL -> non-NULL superseded_at transition
-- used to version a forecast; afterwards the row is frozen.
--
-- Fully idempotent: every statement is guarded (IF NOT EXISTS, DROP
-- TRIGGER IF EXISTS, CREATE OR REPLACE, EXCEPTION duplicate_object) so the
-- file can be re-applied on fresh or upgraded databases.
-- Rollback: drop the new tables in reverse dependency order, then drop the
-- added columns and the trigger functions.

-- ---------------------------------------------------------------------------
-- 1. Thesis groups (created first so investment_theses.group_id can
--    reference them).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS investment_thesis_groups (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_thesis_groups_status_check
        CHECK (status IN ('active', 'archived'))
);

DO $$
BEGIN
    CREATE TRIGGER investment_thesis_groups_updated_at
        BEFORE UPDATE ON investment_thesis_groups
        FOR EACH ROW EXECUTE FUNCTION update_updated_at();
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- ---------------------------------------------------------------------------
-- 2. Additive autonomy/scoring columns on the canonical thesis record.
--    All defaults are neutral so existing manual theses remain valid.
-- ---------------------------------------------------------------------------

ALTER TABLE investment_theses
    ADD COLUMN IF NOT EXISTS group_id UUID
        REFERENCES investment_thesis_groups (id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'manual'
        CHECK (origin IN ('manual', 'generated', 'fusion')),
    ADD COLUMN IF NOT EXISTS canonical_key TEXT,
    ADD COLUMN IF NOT EXISTS mechanism TEXT,
    ADD COLUMN IF NOT EXISTS direction TEXT NOT NULL DEFAULT 'neutral'
        CHECK (direction IN ('long', 'short', 'neutral')),
    ADD COLUMN IF NOT EXISTS catalyst_summary TEXT,
    ADD COLUMN IF NOT EXISTS evidence_strength DOUBLE PRECISION
        NOT NULL DEFAULT 0 CHECK (evidence_strength BETWEEN 0 AND 1),
    ADD COLUMN IF NOT EXISTS contradiction_strength DOUBLE PRECISION
        NOT NULL DEFAULT 0 CHECK (contradiction_strength BETWEEN 0 AND 1),
    ADD COLUMN IF NOT EXISTS neglect_score DOUBLE PRECISION
        NOT NULL DEFAULT 0 CHECK (neglect_score BETWEEN 0 AND 1),
    ADD COLUMN IF NOT EXISTS catalyst_score DOUBLE PRECISION
        NOT NULL DEFAULT 0 CHECK (catalyst_score BETWEEN 0 AND 1),
    ADD COLUMN IF NOT EXISTS confidence_score DOUBLE PRECISION
        NOT NULL DEFAULT 0 CHECK (confidence_score BETWEEN 0 AND 1),
    ADD COLUMN IF NOT EXISTS expected_value DOUBLE PRECISION
        NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS expected_shortfall DOUBLE PRECISION
        NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS opportunity_score DOUBLE PRECISION
        NOT NULL DEFAULT 0 CHECK (opportunity_score BETWEEN 0 AND 1),
    ADD COLUMN IF NOT EXISTS last_evaluated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_evidence_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS input_fingerprint TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_investment_theses_canonical_key
    ON investment_theses (canonical_key)
    WHERE canonical_key IS NOT NULL;
-- Evaluation dedup: the fingerprint is content-addressed over the thesis
-- identity and its evaluation inputs, so it is globally unique; identical
-- inputs re-evaluated are no-ops.
CREATE UNIQUE INDEX IF NOT EXISTS idx_investment_theses_input_fingerprint
    ON investment_theses (input_fingerprint)
    WHERE input_fingerprint IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_investment_theses_group
    ON investment_theses (group_id)
    WHERE group_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_investment_theses_last_evaluated
    ON investment_theses (last_evaluated_at DESC)
    WHERE last_evaluated_at IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 3. Provenance/weight columns on evidence. The existing primary key
--    (thesis_id, evidence_type, evidence_id, relationship) is preserved.
-- ---------------------------------------------------------------------------

ALTER TABLE investment_thesis_evidence
    ADD COLUMN IF NOT EXISTS source_family TEXT NOT NULL DEFAULT 'manual',
    ADD COLUMN IF NOT EXISTS origin_key TEXT,
    ADD COLUMN IF NOT EXISTS independence_key TEXT,
    ADD COLUMN IF NOT EXISTS evidence_fingerprint TEXT,
    ADD COLUMN IF NOT EXISTS source_timestamp TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS available_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS quality_score DOUBLE PRECISION
        NOT NULL DEFAULT 0 CHECK (quality_score BETWEEN 0 AND 1),
    ADD COLUMN IF NOT EXISTS entailment_score DOUBLE PRECISION
        NOT NULL DEFAULT 0 CHECK (entailment_score BETWEEN 0 AND 1),
    ADD COLUMN IF NOT EXISTS freshness_score DOUBLE PRECISION
        NOT NULL DEFAULT 0 CHECK (freshness_score BETWEEN 0 AND 1),
    ADD COLUMN IF NOT EXISTS effective_weight DOUBLE PRECISION
        NOT NULL DEFAULT 1 CHECK (effective_weight BETWEEN 0 AND 1);

-- Invalidation is a first-class evidence relationship (desk evidence and
-- falsification both record it).  The canonical 038 check is swapped for an
-- expanded one under the same name, 011-style: the new constraint is added
-- NOT VALID and validated while the original still guards writes, then the
-- original is dropped and the new one renamed into the canonical name.  The
-- whole swap is guarded so re-applying the file is a no-op.  The composite
-- primary key (thesis_id, evidence_type, evidence_id, relationship) is
-- preserved.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'investment_thesis_evidence'::regclass
          AND conname = 'investment_thesis_evidence_relationship_check'
          AND pg_get_constraintdef(oid) LIKE '%invalidation%'
    ) THEN
        ALTER TABLE investment_thesis_evidence
            ADD CONSTRAINT investment_thesis_evidence_relationship_check_v2
            CHECK (relationship IN
                ('supports', 'contradicts', 'context', 'invalidation'))
            NOT VALID;
        ALTER TABLE investment_thesis_evidence
            VALIDATE CONSTRAINT investment_thesis_evidence_relationship_check_v2;
        ALTER TABLE investment_thesis_evidence
            DROP CONSTRAINT IF EXISTS
                investment_thesis_evidence_relationship_check;
        ALTER TABLE investment_thesis_evidence
            RENAME CONSTRAINT investment_thesis_evidence_relationship_check_v2
            TO investment_thesis_evidence_relationship_check;
    END IF;
END $$;

-- Evidence is deduped/capped by independence_key: at most one row per
-- independent source per thesis. Manual rows (NULL key) are exempt.
CREATE UNIQUE INDEX IF NOT EXISTS idx_investment_thesis_evidence_independence
    ON investment_thesis_evidence (thesis_id, independence_key)
    WHERE independence_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_investment_thesis_evidence_origin
    ON investment_thesis_evidence (origin_key)
    WHERE origin_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_investment_thesis_evidence_fingerprint
    ON investment_thesis_evidence (evidence_fingerprint)
    WHERE evidence_fingerprint IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 4. Versioned group membership. Rows are immutable; membership ends by
--    setting removed_at instead of deleting. At most one active row per
--    (group_id, thesis_id); history accumulates as removed rows.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS investment_thesis_group_members (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    group_id UUID NOT NULL REFERENCES investment_thesis_groups (id) ON DELETE CASCADE,
    thesis_id UUID NOT NULL REFERENCES investment_theses (id) ON DELETE CASCADE,
    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    removed_at TIMESTAMPTZ,
    note TEXT,
    CONSTRAINT investment_thesis_group_members_removed_after_added
        CHECK (removed_at IS NULL OR removed_at >= added_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_investment_thesis_group_members_active
    ON investment_thesis_group_members (group_id, thesis_id)
    WHERE removed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_investment_thesis_group_members_thesis
    ON investment_thesis_group_members (thesis_id)
    WHERE removed_at IS NULL;

-- ---------------------------------------------------------------------------
-- 5. Versioned scenarios. Each (thesis_id, name) has an active version and
--    a superseded history; probability revisions insert a new version.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS investment_thesis_scenarios (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thesis_id UUID NOT NULL REFERENCES investment_theses (id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    probability DOUBLE PRECISION
        CHECK (probability BETWEEN 0 AND 1),
    expected_return DOUBLE PRECISION NOT NULL DEFAULT 0
        CHECK (expected_return BETWEEN -100 AND 100),
    is_base_case BOOLEAN NOT NULL DEFAULT FALSE,
    version INTEGER NOT NULL DEFAULT 1,
    superseded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_thesis_scenarios_version_check
        CHECK (version >= 1),
    CONSTRAINT investment_thesis_scenarios_superseded_after_created
        CHECK (superseded_at IS NULL OR superseded_at >= created_at),
    CONSTRAINT investment_thesis_scenarios_identity_unique
        UNIQUE (thesis_id, name, version)
);

-- Upgrade path for schemas that already ran an earlier draft of this
-- migration: unknown probability is representable (NULL) and the bounded
-- finite expected_return column is stored.  The CHECK passes NULL under SQL
-- semantics, so relaxing NOT NULL is the only probability change needed;
-- BETWEEN -100 AND 100 rejects NaN/Infinity as well as out-of-range
-- returns, matching the domain's MAX_ABS_RETURN = 100 cap.  Both statements
-- are no-ops on the fresh schema above, so re-applying stays idempotent.
ALTER TABLE investment_thesis_scenarios
    ALTER COLUMN probability DROP NOT NULL;
ALTER TABLE investment_thesis_scenarios
    ADD COLUMN IF NOT EXISTS expected_return DOUBLE PRECISION
        NOT NULL DEFAULT 0
        CHECK (expected_return BETWEEN -100 AND 100);

CREATE UNIQUE INDEX IF NOT EXISTS idx_investment_thesis_scenarios_active
    ON investment_thesis_scenarios (thesis_id, name)
    WHERE superseded_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_investment_thesis_scenarios_base_case
    ON investment_thesis_scenarios (thesis_id)
    WHERE is_base_case AND superseded_at IS NULL;

-- ---------------------------------------------------------------------------
-- 6. Versioned forecasts with stable point-in-time identity. Each
--    forecast_key has one active version; superseding marks the old
--    version superseded_at and inserts the new version.  The ONLY allowed
--    UPDATE is the one-time NULL -> non-NULL superseded_at transition;
--    identity and content stay frozen, and superseded rows are immutable.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS investment_thesis_forecasts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thesis_id UUID NOT NULL REFERENCES investment_theses (id) ON DELETE CASCADE,
    scenario_id UUID REFERENCES investment_thesis_scenarios (id) ON DELETE SET NULL,
    forecast_key TEXT NOT NULL,
    forecast_type TEXT NOT NULL DEFAULT 'price',
    direction TEXT NOT NULL DEFAULT 'up',
    target_value DOUBLE PRECISION,
    target_date DATE,
    as_of TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    version INTEGER NOT NULL DEFAULT 1,
    superseded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_thesis_forecasts_version_check
        CHECK (version >= 1),
    CONSTRAINT investment_thesis_forecasts_direction_check
        CHECK (direction IN ('up', 'down', 'flat')),
    CONSTRAINT investment_thesis_forecasts_type_check
        CHECK (forecast_type IN ('price', 'earnings', 'revenue', 'relative', 'other')),
    CONSTRAINT investment_thesis_forecasts_superseded_after_created
        CHECK (superseded_at IS NULL OR superseded_at >= created_at),
    CONSTRAINT investment_thesis_forecasts_identity_unique
        UNIQUE (forecast_key, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_investment_thesis_forecasts_active
    ON investment_thesis_forecasts (forecast_key)
    WHERE superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_investment_thesis_forecasts_thesis
    ON investment_thesis_forecasts (thesis_id, as_of DESC);

-- ---------------------------------------------------------------------------
-- 7. Forecast outcomes: one terminal outcome per forecast version,
--    recorded at measurement time. Append-only.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS investment_forecast_outcomes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    forecast_id UUID NOT NULL REFERENCES investment_thesis_forecasts (id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    actual_value DOUBLE PRECISION,
    measured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_forecast_outcomes_status_check
        CHECK (status IN ('hit', 'miss', 'inconclusive')),
    CONSTRAINT investment_forecast_outcomes_forecast_unique
        UNIQUE (forecast_id)
);

-- ---------------------------------------------------------------------------
-- 8. Opportunity snapshots: frozen scoring state per evaluation run,
--    keyed by (thesis_id, snapshot_key). Append-only.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS investment_opportunity_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thesis_id UUID NOT NULL REFERENCES investment_theses (id) ON DELETE CASCADE,
    snapshot_key TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    input_fingerprint TEXT,
    opportunity_score DOUBLE PRECISION NOT NULL
        CHECK (opportunity_score BETWEEN 0 AND 1),
    expected_value DOUBLE PRECISION NOT NULL DEFAULT 0,
    expected_shortfall DOUBLE PRECISION NOT NULL DEFAULT 0,
    confidence_score DOUBLE PRECISION NOT NULL DEFAULT 0
        CHECK (confidence_score BETWEEN 0 AND 1),
    neglect_score DOUBLE PRECISION NOT NULL DEFAULT 0
        CHECK (neglect_score BETWEEN 0 AND 1),
    catalyst_score DOUBLE PRECISION NOT NULL DEFAULT 0
        CHECK (catalyst_score BETWEEN 0 AND 1),
    evidence_strength DOUBLE PRECISION NOT NULL DEFAULT 0
        CHECK (evidence_strength BETWEEN 0 AND 1),
    contradiction_strength DOUBLE PRECISION NOT NULL DEFAULT 0
        CHECK (contradiction_strength BETWEEN 0 AND 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_opportunity_snapshots_identity_unique
        UNIQUE (thesis_id, snapshot_key)
);

CREATE INDEX IF NOT EXISTS idx_investment_opportunity_snapshots_thesis
    ON investment_opportunity_snapshots (thesis_id, captured_at DESC);

-- ---------------------------------------------------------------------------
-- 9. Falsification runs: one run per (thesis_id, run_key); status moves
--    pending/in_progress -> terminal and is then frozen.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS investment_thesis_falsification_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thesis_id UUID NOT NULL REFERENCES investment_theses (id) ON DELETE CASCADE,
    run_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    findings JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_thesis_falsification_runs_status_check
        CHECK (status IN (
            'pending', 'in_progress', 'not_falsified', 'falsified', 'inconclusive'
        )),
    CONSTRAINT investment_thesis_falsification_runs_completed_after_started
        CHECK (completed_at IS NULL OR completed_at >= started_at),
    CONSTRAINT investment_thesis_falsification_runs_identity_unique
        UNIQUE (thesis_id, run_key)
);

CREATE INDEX IF NOT EXISTS idx_investment_thesis_falsification_runs_thesis
    ON investment_thesis_falsification_runs (thesis_id, started_at DESC);

-- ---------------------------------------------------------------------------
-- 10. Position-thesis links (positions are portfolio_holdings rows).
--     Versioned audit trail: linking inserts a row; unlinking sets
--     removed_at. At most one active link per (position, thesis, type).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS position_thesis_links (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    position_id UUID NOT NULL REFERENCES portfolio_holdings (id) ON DELETE CASCADE,
    thesis_id UUID NOT NULL REFERENCES investment_theses (id) ON DELETE CASCADE,
    link_type TEXT NOT NULL DEFAULT 'primary',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    removed_at TIMESTAMPTZ,
    CONSTRAINT position_thesis_links_link_type_check
        CHECK (link_type IN ('primary', 'secondary', 'hedge', 'watch')),
    CONSTRAINT position_thesis_links_removed_after_created
        CHECK (removed_at IS NULL OR removed_at >= created_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_position_thesis_links_active
    ON position_thesis_links (position_id, thesis_id, link_type)
    WHERE removed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_position_thesis_links_thesis
    ON position_thesis_links (thesis_id)
    WHERE removed_at IS NULL;

-- ---------------------------------------------------------------------------
-- 11. Append-only / lifecycle triggers.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION reject_thesis_immutable_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION '% is immutable', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS investment_thesis_forecasts_immutable ON investment_thesis_forecasts;

CREATE OR REPLACE FUNCTION enforce_thesis_forecast_lifecycle()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'forecasts are append-only';
    END IF;
    -- Identity and content are immutable: the only permitted UPDATE is the
    -- one-time supersede transition below, which touches nothing else.
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.thesis_id IS DISTINCT FROM OLD.thesis_id
       OR NEW.scenario_id IS DISTINCT FROM OLD.scenario_id
       OR NEW.forecast_key IS DISTINCT FROM OLD.forecast_key
       OR NEW.forecast_type IS DISTINCT FROM OLD.forecast_type
       OR NEW.direction IS DISTINCT FROM OLD.direction
       OR NEW.target_value IS DISTINCT FROM OLD.target_value
       OR NEW.target_date IS DISTINCT FROM OLD.target_date
       OR NEW.as_of IS DISTINCT FROM OLD.as_of
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'forecast content is immutable; supersede to revise';
    END IF;
    -- An UPDATE that does not supersede is a revision in place: reject.
    IF NEW.superseded_at IS NULL THEN
        RAISE EXCEPTION 'forecast rows are immutable; supersede to revise';
    END IF;
    -- The transition is one-time: superseded rows are frozen.
    IF OLD.superseded_at IS NOT NULL THEN
        RAISE EXCEPTION 'superseded forecasts are immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS investment_thesis_forecasts_lifecycle ON investment_thesis_forecasts;
CREATE TRIGGER investment_thesis_forecasts_lifecycle
    BEFORE UPDATE OR DELETE ON investment_thesis_forecasts
    FOR EACH ROW EXECUTE FUNCTION enforce_thesis_forecast_lifecycle();

DROP TRIGGER IF EXISTS investment_forecast_outcomes_immutable ON investment_forecast_outcomes;
CREATE TRIGGER investment_forecast_outcomes_immutable
    BEFORE UPDATE OR DELETE ON investment_forecast_outcomes
    FOR EACH ROW EXECUTE FUNCTION reject_thesis_immutable_mutation();

DROP TRIGGER IF EXISTS investment_opportunity_snapshots_immutable ON investment_opportunity_snapshots;
CREATE TRIGGER investment_opportunity_snapshots_immutable
    BEFORE UPDATE OR DELETE ON investment_opportunity_snapshots
    FOR EACH ROW EXECUTE FUNCTION reject_thesis_immutable_mutation();

DROP TRIGGER IF EXISTS position_thesis_links_immutable ON position_thesis_links;

CREATE OR REPLACE FUNCTION enforce_thesis_position_link_append_only()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'position links are append-only; set removed_at to unlink';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.position_id IS DISTINCT FROM OLD.position_id
       OR NEW.thesis_id IS DISTINCT FROM OLD.thesis_id
       OR NEW.link_type IS DISTINCT FROM OLD.link_type
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'position link identity columns are immutable';
    END IF;
    IF OLD.removed_at IS NOT NULL
       AND NEW.removed_at IS DISTINCT FROM OLD.removed_at THEN
        RAISE EXCEPTION 'unlinked position links are immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS position_thesis_links_append_only ON position_thesis_links;
CREATE TRIGGER position_thesis_links_append_only
    BEFORE UPDATE OR DELETE ON position_thesis_links
    FOR EACH ROW EXECUTE FUNCTION enforce_thesis_position_link_append_only();

CREATE OR REPLACE FUNCTION enforce_thesis_group_membership_append_only()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'group memberships are append-only; set removed_at to end membership';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.group_id IS DISTINCT FROM OLD.group_id
       OR NEW.thesis_id IS DISTINCT FROM OLD.thesis_id
       OR NEW.added_at IS DISTINCT FROM OLD.added_at THEN
        RAISE EXCEPTION 'group membership identity columns are immutable';
    END IF;
    IF OLD.removed_at IS NOT NULL
       AND NEW.removed_at IS DISTINCT FROM OLD.removed_at THEN
        RAISE EXCEPTION 'removed memberships are immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS investment_thesis_group_members_append_only ON investment_thesis_group_members;
CREATE TRIGGER investment_thesis_group_members_append_only
    BEFORE UPDATE OR DELETE ON investment_thesis_group_members
    FOR EACH ROW EXECUTE FUNCTION enforce_thesis_group_membership_append_only();

CREATE OR REPLACE FUNCTION enforce_thesis_falsification_run_lifecycle()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'falsification runs are append-only';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.thesis_id IS DISTINCT FROM OLD.thesis_id
       OR NEW.run_key IS DISTINCT FROM OLD.run_key
       OR NEW.started_at IS DISTINCT FROM OLD.started_at THEN
        RAISE EXCEPTION 'falsification run identity columns are immutable';
    END IF;
    IF OLD.status NOT IN ('pending', 'in_progress') THEN
        RAISE EXCEPTION 'falsification run status is final';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS investment_thesis_falsification_runs_lifecycle ON investment_thesis_falsification_runs;
CREATE TRIGGER investment_thesis_falsification_runs_lifecycle
    BEFORE UPDATE OR DELETE ON investment_thesis_falsification_runs
    FOR EACH ROW EXECUTE FUNCTION enforce_thesis_falsification_run_lifecycle();

-- ---------------------------------------------------------------------------
-- 050: Free public market sources: corporate actions and options chain
-- snapshots.
--
-- Adds two append-only fact tables consumed by the keyless public-market
-- collectors (equity daily prices/corporate actions, options chain
-- snapshots) plus a nullable metadata column on the canonical market_data
-- hypertable so price rows can distinguish provider (source) time from
-- acquisition/availability time.
--
-- Point-in-time identity: corporate action rows are identified by a
-- deterministic digest over (source, symbol, action_type, effective_date,
-- amount or ratio).  Re-collecting the same action is an idempotent no-op;
-- a provider amendment (new amount/ratio/date) produces a NEW action_id
-- row instead of mutating history, so the table is a faithful append-only
-- record of what the source served at each point in time.  Options chain
-- rows carry the same identity inside (source, contract_symbol,
-- captured_at): each fetch is one immutable snapshot.
--
-- Immutable snapshot semantics: rows in both tables are frozen.  Writes
-- arrive exclusively through ON CONFLICT DO NOTHING (identical snapshots
-- are no-ops); the guard triggers below make accidental UPDATE/DELETE
-- fail loudly instead of silently rewriting provider history.
--
-- Finite/range/type checks: every numeric column rejects NaN/Infinity
-- (NaN is excluded by `x = x`, infinities by the explicit bounds) and
-- enforces its domain range: non-negative prices, strictly positive
-- strikes and split ratios, bounded implied volatility, and per-type
-- field sets for split vs dividend rows.
--
-- Fully idempotent: every statement is guarded (IF NOT EXISTS / CREATE
-- OR REPLACE / EXCEPTION duplicate_object) so the file can be re-applied
-- on fresh or upgraded databases.
-- Rollback: drop the guard triggers, drop the two tables, then drop the
-- market_data metadata column.

-- ---------------------------------------------------------------------------
-- 1. Corporate actions (append-only; corrections are new action_id rows).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS corporate_actions (
    action_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    action_type TEXT NOT NULL,
    effective_date DATE NOT NULL,
    source TEXT NOT NULL,
    -- Provider-reported event time (e.g. ex-date announcement timestamp);
    -- distinct from available_at, the acquisition time recorded below.
    source_timestamp TIMESTAMPTZ NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    -- Dividend cash amount per share (dividend rows only).
    amount DOUBLE PRECISION,
    -- Split ratio (split rows only): numerator/denominator, e.g. 4/1.
    ratio_numerator DOUBLE PRECISION,
    ratio_denominator DOUBLE PRECISION,
    description TEXT,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT corporate_actions_type_check
        CHECK (action_type IN ('split', 'dividend')),
    CONSTRAINT corporate_actions_symbol_length_check
        CHECK (length(symbol) BETWEEN 1 AND 32),
    CONSTRAINT corporate_actions_source_length_check
        CHECK (length(source) BETWEEN 1 AND 64),
    -- amount: finite, non-negative (NaN rejected by amount = amount).
    CONSTRAINT corporate_actions_amount_finite_range_check
        CHECK (
            amount IS NULL
            OR (
                amount = amount
                AND amount >= 0
                AND amount < 'Infinity'::double precision
            )
        ),
    -- ratio: both sides present together, finite, strictly positive.
    CONSTRAINT corporate_actions_ratio_finite_range_check
        CHECK (
            (ratio_numerator IS NULL AND ratio_denominator IS NULL)
            OR (
                ratio_numerator IS NOT NULL
                AND ratio_denominator IS NOT NULL
                AND ratio_numerator = ratio_numerator
                AND ratio_denominator = ratio_denominator
                AND ratio_numerator > 0
                AND ratio_denominator > 0
                AND ratio_numerator < 'Infinity'::double precision
                AND ratio_denominator < 'Infinity'::double precision
            )
        ),
    -- Per-type field sets: dividends carry amount, splits carry the ratio.
    CONSTRAINT corporate_actions_type_fields_check
        CHECK (
            (action_type = 'dividend' AND amount IS NOT NULL
                AND ratio_numerator IS NULL AND ratio_denominator IS NULL)
            OR (action_type = 'split' AND amount IS NULL
                AND ratio_numerator IS NOT NULL AND ratio_denominator IS NOT NULL)
        )
);

CREATE INDEX IF NOT EXISTS idx_corporate_actions_symbol_effective
    ON corporate_actions (symbol, effective_date DESC);
CREATE INDEX IF NOT EXISTS idx_corporate_actions_source_effective
    ON corporate_actions (source, effective_date DESC);

-- ---------------------------------------------------------------------------
-- 2. Options chain snapshots (immutable per-fetch rows).
--
-- Identity: PRIMARY KEY (source, contract_symbol, captured_at).  One chain
-- fetch for a symbol produces one row per contract sharing captured_at
-- (acquisition time); source_timestamp is the provider quote time and may
-- be absent.  Re-collecting the identical snapshot is an idempotent
-- no-op; a later fetch with a different captured_at is a new snapshot.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS option_chain_snapshots (
    source TEXT NOT NULL,
    symbol TEXT NOT NULL,
    contract_symbol TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    source_timestamp TIMESTAMPTZ,
    expiration DATE NOT NULL,
    strike DOUBLE PRECISION NOT NULL,
    option_type TEXT NOT NULL,
    bid DOUBLE PRECISION,
    ask DOUBLE PRECISION,
    last DOUBLE PRECISION,
    volume DOUBLE PRECISION,
    open_interest DOUBLE PRECISION,
    implied_volatility DOUBLE PRECISION,
    underlying_price DOUBLE PRECISION,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, contract_symbol, captured_at),
    CONSTRAINT option_chain_snapshots_option_type_check
        CHECK (option_type IN ('call', 'put')),
    CONSTRAINT option_chain_snapshots_symbol_length_check
        CHECK (length(symbol) BETWEEN 1 AND 32),
    CONSTRAINT option_chain_snapshots_strike_finite_positive_check
        CHECK (
            strike = strike
            AND strike > 0
            AND strike < 'Infinity'::double precision
        ),
    -- Quotes and underlier: finite, non-negative when present.
    CONSTRAINT option_chain_snapshots_prices_finite_nonneg_check
        CHECK (
            (bid IS NULL OR (bid = bid AND bid >= 0
                AND bid < 'Infinity'::double precision))
            AND (ask IS NULL OR (ask = ask AND ask >= 0
                AND ask < 'Infinity'::double precision))
            AND (last IS NULL OR (last = last AND last >= 0
                AND last < 'Infinity'::double precision))
            AND (underlying_price IS NULL
                OR (underlying_price = underlying_price
                    AND underlying_price >= 0
                    AND underlying_price < 'Infinity'::double precision))
        ),
    -- Activity: finite, non-negative when present.
    CONSTRAINT option_chain_snapshots_activity_finite_nonneg_check
        CHECK (
            (volume IS NULL OR (volume = volume AND volume >= 0
                AND volume < 'Infinity'::double precision))
            AND (open_interest IS NULL
                OR (open_interest = open_interest AND open_interest >= 0
                    AND open_interest < 'Infinity'::double precision))
        ),
    -- Implied volatility: bounded to the plausible 0..1000% band.
    CONSTRAINT option_chain_snapshots_iv_finite_range_check
        CHECK (
            implied_volatility IS NULL
            OR (
                implied_volatility = implied_volatility
                AND implied_volatility >= 0
                AND implied_volatility <= 10
            )
        )
);

CREATE INDEX IF NOT EXISTS idx_option_chain_snapshots_symbol_captured
    ON option_chain_snapshots (symbol, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_option_chain_snapshots_expiration
    ON option_chain_snapshots (expiration);

-- ---------------------------------------------------------------------------
-- 3. Price rows distinguish source time from acquisition time in metadata.
--    Nullable column, additive on the existing hypertable; existing rows
--    carry the default empty object.
-- ---------------------------------------------------------------------------

ALTER TABLE market_data
    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}';

-- ---------------------------------------------------------------------------
-- 4. Immutability guards: UPDATE/DELETE on either fact table is refused;
--    new facts arrive as new rows (DO NOTHING upserts).
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION prevent_market_source_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION '% rows are immutable snapshots; insert a new row instead of updating or deleting', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    CREATE TRIGGER corporate_actions_immutable_guard
        BEFORE UPDATE OR DELETE ON corporate_actions
        FOR EACH ROW EXECUTE FUNCTION prevent_market_source_mutation();
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE TRIGGER option_chain_snapshots_immutable_guard
        BEFORE UPDATE OR DELETE ON option_chain_snapshots
        FOR EACH ROW EXECUTE FUNCTION prevent_market_source_mutation();
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- ---------------------------------------------------------------------------
-- 051: Catalyst event playbooks: immutable, evidence-linked monitored event
-- scenarios for thesis catalysts.
--
-- Turns a promotion-eligible thesis candidate's catalyst into monitored
-- event content that the autonomy cycle can match against the normalized
-- market-event ledger (market_events, migration 027).  Playbooks are pure
-- monitoring content: they carry the catalyst, the monitored horizon, the
-- bounded MarketEventType vocabulary to watch, verbatim trigger /
-- confirmation / invalidation conditions, the three scenario legs, and the
-- exact cited evidence refs.  No recommendation, entry/exit, stop/target,
-- sizing, allocation, or execution field exists in either table, so no row
-- can become a trading instruction.
--
-- investment_thesis_event_playbooks is immutable versioned content.  Each
-- playbook_key (deterministic identity of thesis + catalyst + horizon) has
-- exactly one active version; changing the derived content supersedes the
-- active row through the one-time NULL -> non-NULL superseded_at transition
-- and inserts the next version, preserving point-in-time history.  The
-- lifecycle guard refuses DELETE and any UPDATE except that single
-- transition; content (including input_fingerprint, which covers all
-- content) is frozen.  event_types draws only from the
-- events.contracts.MarketEventType vocabulary, enforced by a CHECK; all
-- arrays/JSONB are bounded; scenario legs stay unknown (NULL) rather than
-- fabricated.
--
-- investment_thesis_event_matches is the append-only match ledger: one row
-- per (playbook, market_event, match_kind), with the playbook FK and the
-- market_event FK (market_events is itself an append-only ledger).  A
-- duplicate recording is an idempotent no-op; UPDATE/DELETE on ledger rows
-- is refused by a guard trigger.
--
-- Fully idempotent: every statement is guarded (IF NOT EXISTS / CREATE
-- OR REPLACE / DROP TRIGGER IF EXISTS / EXCEPTION duplicate_object) so the
-- file can be re-applied on fresh or upgraded databases.
-- Rollback: drop the guard triggers, then the tables in dependency order
-- (investment_thesis_event_matches first, as it references the playbooks
-- table), then the trigger functions.

-- ---------------------------------------------------------------------------
-- 1. Versioned event playbooks (immutable content; supersede to revise).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS investment_thesis_event_playbooks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thesis_id UUID NOT NULL REFERENCES investment_theses (id) ON DELETE CASCADE,
    -- Stable identity of thesis + catalyst + horizon; one active version.
    playbook_key TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    -- Thesis version at build time (point-in-time provenance, never 0).
    thesis_version INTEGER NOT NULL DEFAULT 1,
    catalyst TEXT NOT NULL,
    horizon TEXT NOT NULL,
    expected_at TIMESTAMPTZ,
    event_types TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    trigger_conditions JSONB NOT NULL DEFAULT '[]'::JSONB,
    confirmation_conditions JSONB NOT NULL DEFAULT '[]'::JSONB,
    invalidation_conditions JSONB NOT NULL DEFAULT '[]'::JSONB,
    bull_scenario JSONB,
    base_scenario JSONB,
    bear_scenario JSONB,
    cited_evidence_refs TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    input_fingerprint TEXT NOT NULL,
    superseded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_thesis_event_playbooks_version_check
        CHECK (version >= 1),
    CONSTRAINT investment_thesis_event_playbooks_thesis_version_check
        CHECK (thesis_version >= 1),
    CONSTRAINT investment_thesis_event_playbooks_catalyst_length_check
        CHECK (length(catalyst) BETWEEN 1 AND 2000),
    -- Bounded horizon vocabulary: the research-intelligence horizons used
    -- by tournament candidates plus the market-event horizon set.
    CONSTRAINT investment_thesis_event_playbooks_horizon_check
        CHECK (horizon IN (
            'intraday', 'days', 'weeks', 'months', 'multi_year', 'unknown',
            'swing', 'medium', 'long_term'
        )),
    -- Bounded, vocabulary-constrained event types: values must come from
    -- events.contracts.MarketEventType (18 values) and never exceed the
    -- full vocabulary size.
    CONSTRAINT investment_thesis_event_playbooks_event_types_bounded_check
        CHECK (cardinality(event_types) <= 18),
    CONSTRAINT investment_thesis_event_playbooks_event_types_vocabulary_check
        CHECK (event_types <@ ARRAY[
            'price_tick', 'price_bar_closed', 'option_chain_published',
            'corporate_action_published', 'volatility_state_changed',
            'correlation_state_changed', 'macro_release', 'macro_revision',
            'calendar_event_changed', 'headline_published', 'story_updated',
            'regulatory_filing_published', 'transcript_published',
            'filing_ingested', 'central_bank_communication',
            'positioning_report_published', 'source_freshness_changed',
            'manual_research_event'
        ]::TEXT[]),
    -- Condition arrays are bounded JSONB arrays of strings.
    CONSTRAINT investment_thesis_event_playbooks_trigger_conditions_check
        CHECK (
            JSONB_TYPEOF(trigger_conditions) = 'array'
            AND JSONB_ARRAY_LENGTH(trigger_conditions) <= 20
        ),
    CONSTRAINT investment_thesis_event_playbooks_confirmation_conditions_check
        CHECK (
            JSONB_TYPEOF(confirmation_conditions) = 'array'
            AND JSONB_ARRAY_LENGTH(confirmation_conditions) <= 20
        ),
    CONSTRAINT investment_thesis_event_playbooks_invalidation_conditions_check
        CHECK (
            JSONB_TYPEOF(invalidation_conditions) = 'array'
            AND JSONB_ARRAY_LENGTH(invalidation_conditions) <= 20
        ),
    -- Scenario legs are objects when present; unknown legs stay NULL.
    CONSTRAINT investment_thesis_event_playbooks_bull_scenario_check
        CHECK (bull_scenario IS NULL OR JSONB_TYPEOF(bull_scenario) = 'object'),
    CONSTRAINT investment_thesis_event_playbooks_base_scenario_check
        CHECK (base_scenario IS NULL OR JSONB_TYPEOF(base_scenario) = 'object'),
    CONSTRAINT investment_thesis_event_playbooks_bear_scenario_check
        CHECK (bear_scenario IS NULL OR JSONB_TYPEOF(bear_scenario) = 'object'),
    CONSTRAINT investment_thesis_event_playbooks_cited_evidence_bounded_check
        CHECK (cardinality(cited_evidence_refs) <= 30),
    -- Content-addressed fingerprint (SHA-256 hex of canonical content).
    CONSTRAINT investment_thesis_event_playbooks_input_fingerprint_check
        CHECK (input_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT investment_thesis_event_playbooks_superseded_after_created
        CHECK (superseded_at IS NULL OR superseded_at >= created_at),
    CONSTRAINT investment_thesis_event_playbooks_identity_unique
        UNIQUE (playbook_key, version)
);

-- Exactly one active row per playbook key.
CREATE UNIQUE INDEX IF NOT EXISTS idx_investment_thesis_event_playbooks_active
    ON investment_thesis_event_playbooks (playbook_key)
    WHERE superseded_at IS NULL;
-- Lookups by thesis (history) and by due date (scheduler polling).
CREATE INDEX IF NOT EXISTS idx_investment_thesis_event_playbooks_thesis
    ON investment_thesis_event_playbooks (thesis_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_investment_thesis_event_playbooks_due
    ON investment_thesis_event_playbooks (expected_at, created_at)
    WHERE superseded_at IS NULL AND expected_at IS NOT NULL;
-- Event-type matching over the bounded vocabulary.
CREATE INDEX IF NOT EXISTS idx_investment_thesis_event_playbooks_event_types
    ON investment_thesis_event_playbooks USING GIN (event_types)
    WHERE superseded_at IS NULL;

-- ---------------------------------------------------------------------------
-- 2. Append-only match ledger.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS investment_thesis_event_matches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    playbook_id UUID NOT NULL
        REFERENCES investment_thesis_event_playbooks (id) ON DELETE CASCADE,
    market_event_id UUID NOT NULL REFERENCES market_events (id) ON DELETE CASCADE,
    match_kind TEXT NOT NULL,
    evidence_refs TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    observed_at TIMESTAMPTZ NOT NULL,
    assessment JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_thesis_event_matches_match_kind_check
        CHECK (match_kind IN ('trigger', 'confirmation', 'invalidation', 'context')),
    CONSTRAINT investment_thesis_event_matches_evidence_refs_bounded_check
        CHECK (cardinality(evidence_refs) <= 30),
    CONSTRAINT investment_thesis_event_matches_assessment_object_check
        CHECK (JSONB_TYPEOF(assessment) = 'object'),
    CONSTRAINT investment_thesis_event_matches_identity_unique
        UNIQUE (playbook_id, market_event_id, match_kind)
);

CREATE INDEX IF NOT EXISTS idx_investment_thesis_event_matches_playbook
    ON investment_thesis_event_matches (playbook_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_investment_thesis_event_matches_event
    ON investment_thesis_event_matches (market_event_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- 3. Immutability / lifecycle guards.
-- ---------------------------------------------------------------------------

-- Match ledger rows are strictly append-only: a new match is a new row.
CREATE OR REPLACE FUNCTION reject_event_match_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION '% rows are immutable; a new match is a new row', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    CREATE TRIGGER investment_thesis_event_matches_immutable
        BEFORE UPDATE OR DELETE ON investment_thesis_event_matches
        FOR EACH ROW EXECUTE FUNCTION reject_event_match_mutation();
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

CREATE OR REPLACE FUNCTION enforce_thesis_event_playbook_lifecycle()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'event playbooks are append-only';
    END IF;
    -- Identity and content are immutable: the only permitted UPDATE is the
    -- one-time supersede transition below, which touches nothing else.
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.thesis_id IS DISTINCT FROM OLD.thesis_id
       OR NEW.playbook_key IS DISTINCT FROM OLD.playbook_key
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.thesis_version IS DISTINCT FROM OLD.thesis_version
       OR NEW.catalyst IS DISTINCT FROM OLD.catalyst
       OR NEW.horizon IS DISTINCT FROM OLD.horizon
       OR NEW.expected_at IS DISTINCT FROM OLD.expected_at
       OR NEW.event_types IS DISTINCT FROM OLD.event_types
       OR NEW.trigger_conditions IS DISTINCT FROM OLD.trigger_conditions
       OR NEW.confirmation_conditions IS DISTINCT FROM OLD.confirmation_conditions
       OR NEW.invalidation_conditions IS DISTINCT FROM OLD.invalidation_conditions
       OR NEW.bull_scenario IS DISTINCT FROM OLD.bull_scenario
       OR NEW.base_scenario IS DISTINCT FROM OLD.base_scenario
       OR NEW.bear_scenario IS DISTINCT FROM OLD.bear_scenario
       OR NEW.cited_evidence_refs IS DISTINCT FROM OLD.cited_evidence_refs
       OR NEW.input_fingerprint IS DISTINCT FROM OLD.input_fingerprint
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'playbook content is immutable; supersede to revise';
    END IF;
    -- An UPDATE that does not supersede is a revision in place: reject.
    IF NEW.superseded_at IS NULL THEN
        RAISE EXCEPTION 'playbook rows are immutable; supersede to revise';
    END IF;
    -- The transition is one-time: superseded rows are frozen.
    IF OLD.superseded_at IS NOT NULL THEN
        RAISE EXCEPTION 'superseded playbooks are immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS investment_thesis_event_playbooks_lifecycle
    ON investment_thesis_event_playbooks;
CREATE TRIGGER investment_thesis_event_playbooks_lifecycle
    BEFORE UPDATE OR DELETE ON investment_thesis_event_playbooks
    FOR EACH ROW EXECUTE FUNCTION enforce_thesis_event_playbook_lifecycle();

-- ---------------------------------------------------------------------------
-- 052: Immutable per-snapshot option analytics features + operational
-- storage for raw option chains.
--
-- Two concerns:
--
-- 1. Raw contract rows (option_chain_snapshots, migration 050) become a
--    Timescale hypertable partitioned on captured_at with a 90-day
--    retention policy: each fetch writes thousands of contracts and raw
--    history must not grow unbounded.  The composite primary key already
--    contains captured_at and the column is NOT NULL, so the conversion is
--    safe with migrate_data.  The immutability guard from migration 050 is
--    preserved across the conversion (triggers survive).
--
-- 2. One feature row per (source, symbol, captured_at) snapshot: the
--    deterministic analytics computed by options_analytics.analyze_chain
--    over the bounded, validated contracts of that snapshot at collection
--    time (ATM IV, implied move, put/call skew, volume/open-interest
--    totals, term structure, and gated unusualness).  Feature rows are
--    long-lived aggregates on a plain table (no retention policy): they
--    survive raw-chunk expiry so snapshot-level analytics history remains
--    queryable.
--
-- Feature rows are persisted insert-only in the same transaction as the
-- chain rows, so re-collecting the identical snapshot is an idempotent
-- no-op and the analytics always match exactly the contracts that were
-- persisted.  Explicit state semantics: analytics never backfills missing
-- IV/open interest and never claims historical unusualness without local
-- history; the per-metric insufficient_data / insufficient_history states
-- produced by the analyzer are preserved verbatim in the analytics JSONB.
--
-- Time semantics: captured_at is the snapshot acquisition time (identity),
-- source_timestamp_min/max bound the provider quote times of the analyzed
-- contracts (NULL when the provider sent none), available_at is when the
-- feature row became available (same acquisition time as the snapshot),
-- and created_at is the row persistence time.  Replay cutoffs consult the
-- feature available/created times.
--
-- Immutable: writes arrive exclusively through ON CONFLICT DO NOTHING; the
-- guard trigger below refuses UPDATE/DELETE, mirroring migration 050.
--
-- Fully idempotent: every statement is guarded (IF NOT EXISTS / hypertable
-- existence check / EXCEPTION duplicate_object) so the file can be
-- re-applied on fresh or upgraded databases.
-- Rollback: drop the retention policy, drop the guard trigger, then drop
-- the feature table (dehypertable conversion of option_chain_snapshots is
-- out of scope for an additive migration).

-- ---------------------------------------------------------------------------
-- 1. Raw option chains: hypertable on captured_at + 90-day retention.
--    Conversion is guarded by an existence check so re-applying the file
--    (or upgrading a database where the table is already chunked) is a
--    no-op; migrate_data moves any pre-conversion rows into chunks.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM timescaledb_information.hypertables
        WHERE hypertable_schema = 'public'
          AND hypertable_name = 'option_chain_snapshots'
    ) THEN
        PERFORM create_hypertable(
            'option_chain_snapshots',
            'captured_at',
            migrate_data => true
        );
    END IF;
END $$;

SELECT add_retention_policy('option_chain_snapshots', INTERVAL '90 days',
    if_not_exists => TRUE);

-- ---------------------------------------------------------------------------
-- 2. Per-snapshot analytics features (long-lived plain table, no retention).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS option_snapshot_features (
    source TEXT NOT NULL,
    symbol TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    -- Analytics contract version; bump when the analyzer output shape
    -- changes so consumers can distinguish feature generations.
    feature_version TEXT NOT NULL,
    -- Provider quote-time bounds over the analyzed contracts (UTC).
    source_timestamp_min TIMESTAMPTZ,
    source_timestamp_max TIMESTAMPTZ,
    -- Availability time of this feature row (same acquisition time as the
    -- snapshot); distinct from source times and from created_at below.
    available_at TIMESTAMPTZ NOT NULL,
    -- Number of analyzed contracts (the bounded validated subset of the
    -- snapshot; rejected or bounded-out contracts are never analyzed).
    contract_count INTEGER NOT NULL,
    analytics JSONB NOT NULL DEFAULT '{}',
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, symbol, captured_at),
    CONSTRAINT option_snapshot_features_symbol_length_check
        CHECK (length(symbol) BETWEEN 1 AND 32),
    CONSTRAINT option_snapshot_features_source_length_check
        CHECK (length(source) BETWEEN 1 AND 64),
    CONSTRAINT option_snapshot_features_version_check
        CHECK (feature_version <> ''),
    CONSTRAINT option_snapshot_features_contract_count_check
        CHECK (contract_count >= 0),
    CONSTRAINT option_snapshot_features_analytics_object_check
        CHECK (jsonb_typeof(analytics) = 'object'),
    CONSTRAINT option_snapshot_features_metadata_object_check
        CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE INDEX IF NOT EXISTS idx_option_snapshot_features_symbol_captured
    ON option_snapshot_features (symbol, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_option_snapshot_features_captured
    ON option_snapshot_features (captured_at DESC);

-- Immutability guard: UPDATE/DELETE on the feature table is refused; new
-- facts arrive as new rows (DO NOTHING upserts).  Reuses the migration 050
-- guard function, which reports the offending table name.
DO $$
BEGIN
    CREATE TRIGGER option_snapshot_features_immutable_guard
        BEFORE UPDATE OR DELETE ON option_snapshot_features
        FOR EACH ROW EXECUTE FUNCTION prevent_market_source_mutation();
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- ---------------------------------------------------------------------------
-- One active forecast per scenario: deterministic legacy dedupe plus a
-- partial unique index.
--
-- The autonomous desk contract guarantees at most one unsuperseded
-- forecast per non-null scenario: the first frozen as_of/reference
-- close/target/target date wins until explicitly superseded.  Databases
-- that ran 049 before this guard can contain legacy duplicates created by
-- reruns whose forecast_key changed (target date or fingerprint drift).
-- This migration deterministically keeps the earliest frozen row per
-- scenario (created_at, then id) and supersedes every other active
-- duplicate at its own created_at — the one-time NULL -> non-NULL
-- transition the forecast lifecycle trigger permits, satisfying the
-- superseded_after_created CHECK — leaving immutable history intact.  It
-- then installs the partial unique index so the invariant is enforced on
-- every future write; the bounded precheck in the freeze path keeps
-- ordinary reruns from reaching it.
--
-- Scenario-less forecasts (scenario_id IS NULL) stay valid and are outside
-- the index.  Fully idempotent: the dedupe UPDATE is a no-op once no
-- active duplicates remain, and the index creation is guarded, so the file
-- re-applies cleanly on fresh and upgraded databases.
-- Rollback: drop the index.  Superseded duplicates remain as frozen
-- history and are never deleted.

WITH active_duplicates AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY scenario_id
               ORDER BY created_at, id
           ) AS kept_rank
    FROM investment_thesis_forecasts
    WHERE scenario_id IS NOT NULL
      AND superseded_at IS NULL
)
UPDATE investment_thesis_forecasts f
SET superseded_at = f.created_at
FROM active_duplicates d
WHERE d.id = f.id
  AND d.kept_rank > 1;

CREATE UNIQUE INDEX IF NOT EXISTS idx_investment_thesis_forecasts_active_scenario
    ON investment_thesis_forecasts (scenario_id)
    WHERE scenario_id IS NOT NULL AND superseded_at IS NULL;

-- ---------------------------------------------------------------------------
-- Catalyst replay safety: every catalyst replay input is immutable after
-- insert.
--
-- investment_catalysts has no update API in the current product (rows are
-- inserted once by research/autonomy and only ever read), but nothing in
-- schema 038 prevented a later UPDATE.  A historical replay that scores a
-- thesis at a cutoff must never see a catalyst whose scoring content or
-- visibility changed after the cutoff, so this migration:
--
--   1. Stamps every pre-migration row with the migration time.  Whether a
--      legacy row was ever mutated is unknowable (nothing maintained
--      updated_at before this migration), so fail closed: a legacy row is
--      only valid for cutoffs at or after the migration ran.  Rows that
--      already carry an updated_at after created_at are stamped too, since
--      that timestamp may itself record a pre-migration mutation; the
--      stamp never moves a timestamp backward (GREATEST keeps an
--      already-later updated_at).  The stamp runs exactly once, guarded by
--      the trigger's existence, so re-applying the file is a no-op and
--      rows inserted after the migration keep updated_at = created_at.
--   2. Installs a BEFORE UPDATE OR DELETE trigger that rejects changes to
--      every replay input: the scoring/identity fields (thesis_id,
--      description, expected_at, state, created_at; id is immutable by
--      definition) and updated_at, which gates replay visibility.  A row's
--      visibility can therefore never be widened (moved backward toward an
--      earlier cutoff) or narrowed (moved forward) after insert.  An exact
--      no-op UPDATE (every column written back to its own value) passes
--      the IS DISTINCT FROM guard and changes nothing; it stays permitted
--      only because it cannot affect replay.
--
-- The evaluator (thesis_fusion.evaluate_thesis) filters catalysts by
-- created_at and updated_at both at or before the as-of cutoff; since
-- updated_at is frozen at insert (post-migration) or at the conservative
-- migration stamp (legacy rows), no post-insert change can move a catalyst
-- across a cutoff.
--
-- Fully idempotent and additive: no columns, constraints, or tables are
-- dropped; existing rows are preserved (only their replay validity is
-- narrowed).  Rollback: DROP TRIGGER investment_catalysts_immutable and
-- DROP FUNCTION enforce_investment_catalyst_immutability; nothing else
-- changed.

-- ---------------------------------------------------------------------------
-- 1. Conservative legacy stamp (once, before the trigger exists).
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'investment_catalysts_immutable'
          AND tgrelid = 'investment_catalysts'::regclass
    ) THEN
        -- Every legacy row is stamped, with no row filter: mutation
        -- history is unknowable for all of them.  GREATEST/COALESCE make
        -- the stamp monotonic — it never moves a timestamp backward.
        UPDATE investment_catalysts
           SET updated_at = GREATEST(COALESCE(updated_at, created_at), NOW());
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. Immutability guard for every replay input.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION enforce_investment_catalyst_immutability()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'catalysts are append-only';
    END IF;
    -- Every replay input is immutable after insert: the scoring/identity
    -- fields and the updated_at visibility gate alike.
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.thesis_id IS DISTINCT FROM OLD.thesis_id
       OR NEW.description IS DISTINCT FROM OLD.description
       OR NEW.expected_at IS DISTINCT FROM OLD.expected_at
       OR NEW.state IS DISTINCT FROM OLD.state
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.updated_at IS DISTINCT FROM OLD.updated_at THEN
        RAISE EXCEPTION 'catalyst replay inputs are immutable after insert';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS investment_catalysts_immutable ON investment_catalysts;
CREATE TRIGGER investment_catalysts_immutable
    BEFORE UPDATE OR DELETE ON investment_catalysts
    FOR EACH ROW EXECUTE FUNCTION enforce_investment_catalyst_immutability();

-- ---------------------------------------------------------------------------
-- Thesis fusion accepted-reference guard.
--
-- investment_theses.fusion_reference_at is the monotonic accepted-reference
-- guard for autonomous fusion content: an autonomous candidate merge claims
-- a thesis only when the incoming cycle reference is at least the stored
-- guard, so accepted-reference order -- never completion order -- decides
-- which cycle's claim, version, scenario, catalyst, evidence, evaluation,
-- and challenge state is current.  A stale cycle (incoming reference older
-- than the stored guard, or equal to it with a different -- or unprovable
-- -- candidate fingerprint) is a complete no-op: it writes no claim,
-- version, evidence attachment, catalyst, scenario, playbook, position
-- link, forecast, evaluation, or challenge child state for that candidate.
--
-- investment_theses.fusion_candidate_fingerprint pairs the guard with the
-- accepted candidate fingerprint: the content-addressed fingerprint
-- (identity + inputs) of the autonomous candidate that first provenly
-- claimed the thesis at fusion_reference_at.  At an equal reference only
-- the candidate that can prove the identical fingerprint may resume
-- (idempotent rerun); a different fingerprint is a different model output
-- and stays stale, so lock/completion order can never choose between
-- distinct outputs for the same reference.  A strictly newer reference
-- claims and stores both fields together, so a newer cycle always wins and
-- an older cycle can never un-claim a newer one.
--
-- Both columns are nullable on purpose:
--   * Rows created by manual/non-autonomy paths carry no reference and no
--     fingerprint and stay claimable by any autonomous cycle (including
--     legacy rows, which are backfilled conservatively below so a replay
--     of an old cycle can never claim a thesis whose accepted state is
--     provably newer).
--   * The guard pair is only ever advanced by autonomous claims, so a
--     newer cycle can always claim a thesis an older cycle claimed, and an
--     older cycle can never un-claim a newer one.
--   * The fingerprint has NO legacy backfill: the candidate that produced
--     pre-migration content is unknowable, and the only honest value is
--     NULL.  That is fail-closed -- an equal-reference claim against a
--     NULL fingerprint cannot prove it is the same output and is refused
--     as stale; only a strictly newer reference may claim the thesis.
--
-- Backfill: the most conservative accepted/current timestamp is the
-- greatest of every known lifecycle/evaluation timestamp, so a legacy
-- thesis is claimable only by cycles at or after the newest of them.  A
-- content update or evaluation bumps updated_at/last_evaluated_at; using
-- anything less (e.g. preferring an older last_evaluated_at over a newer
-- updated_at) would admit a replay between the two to overwrite current
-- legacy content.  created_at and updated_at are NOT NULL with NOW()
-- defaults (migration 038) and COALESCE covers the one nullable column
-- (last_evaluated_at), so every pre-existing row receives a conservative
-- stamp equal to the latest timestamp it carries.
--
-- Fully idempotent and additive: both columns are ADD COLUMN IF NOT
-- EXISTS, the backfill is self-guarding (WHERE fusion_reference_at IS
-- NULL, so re-applying is a no-op and fresh installs backfill nothing),
-- and no rows, columns, constraints, or indexes are dropped.  No
-- additional index is created: the guard pair is only read through the
-- primary-key thesis lookup inside the merge claim (point reads), so an
-- index would add write amplification without serving a query.
-- Rollback: ALTER TABLE investment_theses DROP COLUMN fusion_reference_at,
-- ALTER TABLE investment_theses DROP COLUMN fusion_candidate_fingerprint.

ALTER TABLE investment_theses
    ADD COLUMN IF NOT EXISTS fusion_reference_at TIMESTAMPTZ;

ALTER TABLE investment_theses
    ADD COLUMN IF NOT EXISTS fusion_candidate_fingerprint TEXT;

UPDATE investment_theses
   SET fusion_reference_at = GREATEST(
           created_at, updated_at, COALESCE(last_evaluated_at, created_at)
       )
 WHERE fusion_reference_at IS NULL;

-- ---------------------------------------------------------------------------
-- 056: Persist actionable thesis context and field-level citation maps.
--
-- Autonomous candidates now retain their quantified trend, valuation, and
-- measured-sentiment context plus the exact evidence refs supporting each
-- factual field.  The same fields are stored on immutable thesis versions so
-- history remains auditable when a candidate changes. Existing manual rows
-- remain valid with empty nullable context and an empty citation object.
--
-- Rollback: drop the four columns from investment_thesis_versions, then from
-- investment_theses (after removing the two citation-map constraints).

ALTER TABLE investment_theses
    ADD COLUMN IF NOT EXISTS trend_context TEXT,
    ADD COLUMN IF NOT EXISTS valuation_context TEXT,
    ADD COLUMN IF NOT EXISTS sentiment_context TEXT,
    ADD COLUMN IF NOT EXISTS citation_map JSONB NOT NULL DEFAULT '{}';

ALTER TABLE investment_thesis_versions
    ADD COLUMN IF NOT EXISTS trend_context TEXT,
    ADD COLUMN IF NOT EXISTS valuation_context TEXT,
    ADD COLUMN IF NOT EXISTS sentiment_context TEXT,
    ADD COLUMN IF NOT EXISTS citation_map JSONB NOT NULL DEFAULT '{}';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'investment_theses'::regclass
          AND conname = 'investment_theses_citation_map_object_check'
    ) THEN
        ALTER TABLE investment_theses
            ADD CONSTRAINT investment_theses_citation_map_object_check
            CHECK (jsonb_typeof(citation_map) = 'object');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'investment_thesis_versions'::regclass
          AND conname = 'investment_thesis_versions_citation_map_object_check'
    ) THEN
        ALTER TABLE investment_thesis_versions
            ADD CONSTRAINT investment_thesis_versions_citation_map_object_check
            CHECK (jsonb_typeof(citation_map) = 'object');
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 057: Unknown thesis metrics stay unknown; zero-cost reservations admitted.
--
-- The 049 desk scoring columns were born NOT NULL DEFAULT 0 so legacy
-- manual rows stayed valid.  That made "never evaluated" and "evaluated as
-- zero" indistinguishable, and the desk's own scoring treats an absent
-- input (no directional evidence, no catalyst set, no attention/crowding)
-- as *unknown*, never as a favorable zero.  This migration:
--
--   * drops NOT NULL and the DEFAULT 0 from the eight thesis metric columns
--     on investment_theses (evidence_strength, contradiction_strength,
--     neglect_score, catalyst_score, confidence_score, expected_value,
--     expected_shortfall, opportunity_score) and from the seven sub-metric
--     columns on investment_opportunity_snapshots.  The snapshot
--     opportunity_score stays NOT NULL: every evaluation run produces a
--     numeric gated score, so a frozen snapshot always carries one.
--   * backfills ONLY rows that have never been evaluated
--     (last_evaluated_at IS NULL) to NULL.  Every evaluated row
--     (last_evaluated_at IS NOT NULL) keeps its stored values exactly —
--     a legitimate evaluated zero is preserved byte-for-byte.
--   * relaxes the 045 budget_reservations estimate CHECK from strictly
--     positive to non-negative so a known-free model can reserve zero cost
--     under the daily cap while still carrying an auditable reservation row
--     that settles/releases like any other.
--
-- Fully idempotent: DROP NOT NULL / DROP DEFAULT and the guarded constraint
-- swap are no-ops on re-application, and the backfill only touches rows
-- whose metrics are still stored.  The SQL CHECKs on the score columns
-- (BETWEEN 0 AND 1) pass NULL under standard SQL semantics, so no
-- constraint needs replacing.
--
-- Rollback: restore the zero defaults and NOT NULL (first deciding how the
-- now-NULL unevaluated rows should be represented), and re-add the
-- positive-estimate CHECK on budget_reservations.

-- ---------------------------------------------------------------------------
-- 1. Thesis metric columns: unknown is representable, never defaulted.
-- ---------------------------------------------------------------------------

ALTER TABLE investment_theses
    ALTER COLUMN evidence_strength DROP NOT NULL,
    ALTER COLUMN contradiction_strength DROP NOT NULL,
    ALTER COLUMN neglect_score DROP NOT NULL,
    ALTER COLUMN catalyst_score DROP NOT NULL,
    ALTER COLUMN confidence_score DROP NOT NULL,
    ALTER COLUMN expected_value DROP NOT NULL,
    ALTER COLUMN expected_shortfall DROP NOT NULL,
    ALTER COLUMN opportunity_score DROP NOT NULL;

ALTER TABLE investment_theses
    ALTER COLUMN evidence_strength DROP DEFAULT,
    ALTER COLUMN contradiction_strength DROP DEFAULT,
    ALTER COLUMN neglect_score DROP DEFAULT,
    ALTER COLUMN catalyst_score DROP DEFAULT,
    ALTER COLUMN confidence_score DROP DEFAULT,
    ALTER COLUMN expected_value DROP DEFAULT,
    ALTER COLUMN expected_shortfall DROP DEFAULT,
    ALTER COLUMN opportunity_score DROP DEFAULT;

-- Never-evaluated rows carry only the old neutral defaults, not measured
-- scores: represent them as unknown.  Rows with a last_evaluated_at are
-- measured and are never rewritten, so evaluated zeros survive intact.
UPDATE investment_theses
   SET evidence_strength = NULL,
       contradiction_strength = NULL,
       neglect_score = NULL,
       catalyst_score = NULL,
       confidence_score = NULL,
       expected_value = NULL,
       expected_shortfall = NULL,
       opportunity_score = NULL
 WHERE last_evaluated_at IS NULL
   AND (evidence_strength IS NOT NULL
        OR contradiction_strength IS NOT NULL
        OR neglect_score IS NOT NULL
        OR catalyst_score IS NOT NULL
        OR confidence_score IS NOT NULL
        OR expected_value IS NOT NULL
        OR expected_shortfall IS NOT NULL
        OR opportunity_score IS NOT NULL);

-- ---------------------------------------------------------------------------
-- 2. Frozen opportunity snapshots: carry the same unknowns as NULL instead
--    of coercing them to favorable zeros.  opportunity_score stays NOT
--    NULL because every evaluation produces a numeric gated score.
-- ---------------------------------------------------------------------------

ALTER TABLE investment_opportunity_snapshots
    ALTER COLUMN expected_value DROP NOT NULL,
    ALTER COLUMN expected_shortfall DROP NOT NULL,
    ALTER COLUMN confidence_score DROP NOT NULL,
    ALTER COLUMN neglect_score DROP NOT NULL,
    ALTER COLUMN catalyst_score DROP NOT NULL,
    ALTER COLUMN evidence_strength DROP NOT NULL,
    ALTER COLUMN contradiction_strength DROP NOT NULL;

ALTER TABLE investment_opportunity_snapshots
    ALTER COLUMN expected_value DROP DEFAULT,
    ALTER COLUMN expected_shortfall DROP DEFAULT,
    ALTER COLUMN confidence_score DROP DEFAULT,
    ALTER COLUMN neglect_score DROP DEFAULT,
    ALTER COLUMN catalyst_score DROP DEFAULT,
    ALTER COLUMN evidence_strength DROP DEFAULT,
    ALTER COLUMN contradiction_strength DROP DEFAULT;

-- ---------------------------------------------------------------------------
-- 3. Budget reservations: a known-free model reserves zero cost.
-- ---------------------------------------------------------------------------

ALTER TABLE budget_reservations
    DROP CONSTRAINT IF EXISTS budget_reservations_estimate_positive;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'budget_reservations'::regclass
          AND conname = 'budget_reservations_estimate_nonnegative'
    ) THEN
        ALTER TABLE budget_reservations
            ADD CONSTRAINT budget_reservations_estimate_nonnegative
            CHECK (estimated_usd >= 0);
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 059: Persistent investment thesis review queue.
--
-- Autonomous thesis candidate results are staged as immutable proposals
-- for mandatory human review rather than being directly materialized into
-- canonical thesis records.  Only explicit human approval transitions a
-- proposal to 'approved' and materializes canonical records.
--
-- Fully idempotent: all table, index, trigger, and constraint statements
-- are guarded so the migration can be re-run safely.
--
-- Rollback: drop the trigger and function, then drop investment_thesis_proposals.

CREATE TABLE IF NOT EXISTS investment_thesis_proposals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proposal_key TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    theme_id UUID REFERENCES investment_themes (id) ON DELETE SET NULL,
    company TEXT,
    symbol TEXT,
    subject TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT 'neutral',
    horizon TEXT NOT NULL DEFAULT 'months',
    mechanism TEXT,
    status TEXT NOT NULL DEFAULT 'pending_review',
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    evidence JSONB NOT NULL DEFAULT '[]'::JSONB,
    scenarios JSONB NOT NULL DEFAULT '[]'::JSONB,
    scoring JSONB NOT NULL DEFAULT '{}'::JSONB,
    challenge JSONB NOT NULL DEFAULT '{}'::JSONB,
    diff JSONB NOT NULL DEFAULT '{}'::JSONB,
    matching_thesis_id UUID REFERENCES investment_theses (id) ON DELETE SET NULL,
    materialized_thesis_id UUID REFERENCES investment_theses (id) ON DELETE SET NULL,
    reviewer_id TEXT,
    review_note TEXT,
    reviewed_at TIMESTAMPTZ,
    parent_proposal_id UUID REFERENCES investment_thesis_proposals (id) ON DELETE SET NULL,
    revision_instructions TEXT,
    accepted_reference TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_thesis_proposals_status_check
        CHECK (status IN ('pending_review', 'approved', 'rejected', 'revision_requested')),
    CONSTRAINT investment_thesis_proposals_direction_check
        CHECK (direction IN ('long', 'short', 'neutral')),
    CONSTRAINT investment_thesis_proposals_payload_object_check
        CHECK (JSONB_TYPEOF(payload) = 'object'),
    CONSTRAINT investment_thesis_proposals_evidence_array_check
        CHECK (JSONB_TYPEOF(evidence) = 'array'),
    CONSTRAINT investment_thesis_proposals_scenarios_array_check
        CHECK (JSONB_TYPEOF(scenarios) = 'array'),
    CONSTRAINT investment_thesis_proposals_scoring_object_check
        CHECK (JSONB_TYPEOF(scoring) = 'object'),
    CONSTRAINT investment_thesis_proposals_challenge_object_check
        CHECK (JSONB_TYPEOF(challenge) = 'object'),
    CONSTRAINT investment_thesis_proposals_diff_object_check
        CHECK (JSONB_TYPEOF(diff) = 'object'),
    CONSTRAINT investment_thesis_proposals_identity_unique
        UNIQUE (proposal_key)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_investment_thesis_proposals_key
    ON investment_thesis_proposals (proposal_key);

CREATE INDEX IF NOT EXISTS idx_investment_thesis_proposals_status
    ON investment_thesis_proposals (status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_investment_thesis_proposals_canonical
    ON investment_thesis_proposals (canonical_key, status);

CREATE INDEX IF NOT EXISTS idx_investment_thesis_proposals_symbol
    ON investment_thesis_proposals (symbol, created_at DESC)
    WHERE symbol IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_investment_thesis_proposals_matching
    ON investment_thesis_proposals (matching_thesis_id)
    WHERE matching_thesis_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_investment_thesis_proposals_materialized
    ON investment_thesis_proposals (materialized_thesis_id)
    WHERE materialized_thesis_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_investment_thesis_proposals_parent
    ON investment_thesis_proposals (parent_proposal_id)
    WHERE parent_proposal_id IS NOT NULL;

CREATE OR REPLACE FUNCTION guard_thesis_proposal_lifecycle()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'thesis proposals are immutable review records and cannot be deleted';
    END IF;

    -- Identity and staged payload are immutable once created.
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.proposal_key IS DISTINCT FROM OLD.proposal_key
       OR NEW.canonical_key IS DISTINCT FROM OLD.canonical_key
       OR NEW.theme_id IS DISTINCT FROM OLD.theme_id
       OR NEW.company IS DISTINCT FROM OLD.company
       OR NEW.symbol IS DISTINCT FROM OLD.symbol
       OR NEW.subject IS DISTINCT FROM OLD.subject
       OR NEW.direction IS DISTINCT FROM OLD.direction
       OR NEW.horizon IS DISTINCT FROM OLD.horizon
       OR NEW.mechanism IS DISTINCT FROM OLD.mechanism
       OR NEW.payload IS DISTINCT FROM OLD.payload
       OR NEW.evidence IS DISTINCT FROM OLD.evidence
       OR NEW.scenarios IS DISTINCT FROM OLD.scenarios
       OR NEW.scoring IS DISTINCT FROM OLD.scoring
       OR NEW.challenge IS DISTINCT FROM OLD.challenge
       OR NEW.diff IS DISTINCT FROM OLD.diff
       OR NEW.matching_thesis_id IS DISTINCT FROM OLD.matching_thesis_id
       OR NEW.parent_proposal_id IS DISTINCT FROM OLD.parent_proposal_id
       OR NEW.accepted_reference IS DISTINCT FROM OLD.accepted_reference
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'thesis proposal identity and staged payload are immutable';
    END IF;

    -- Only pending_review proposals can transition.
    IF OLD.status IN ('approved', 'rejected', 'revision_requested')
       AND NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION 'terminal thesis proposal (%) cannot transition to %', OLD.status, NEW.status;
    END IF;

    IF OLD.status = 'pending_review' AND NEW.status NOT IN ('pending_review', 'approved', 'rejected', 'revision_requested') THEN
        RAISE EXCEPTION 'invalid thesis proposal status transition: % -> %', OLD.status, NEW.status;
    END IF;

    -- Approval requires a materialized thesis id and reviewer.
    IF NEW.status = 'approved' AND NEW.status IS DISTINCT FROM OLD.status THEN
        IF NEW.materialized_thesis_id IS NULL THEN
            RAISE EXCEPTION 'approved thesis proposal requires materialized_thesis_id';
        END IF;
        IF NEW.reviewer_id IS NULL OR BTRIM(NEW.reviewer_id) = '' THEN
            RAISE EXCEPTION 'approved thesis proposal requires reviewer_id';
        END IF;
        NEW.reviewed_at := COALESCE(NEW.reviewed_at, NOW());
    END IF;

    -- Rejection requires reviewer.
    IF NEW.status = 'rejected' AND NEW.status IS DISTINCT FROM OLD.status THEN
        IF NEW.reviewer_id IS NULL OR BTRIM(NEW.reviewer_id) = '' THEN
            RAISE EXCEPTION 'rejected thesis proposal requires reviewer_id';
        END IF;
        NEW.reviewed_at := COALESCE(NEW.reviewed_at, NOW());
    END IF;

    -- Revision request requires reviewer and revision instructions.
    IF NEW.status = 'revision_requested' AND NEW.status IS DISTINCT FROM OLD.status THEN
        IF NEW.reviewer_id IS NULL OR BTRIM(NEW.reviewer_id) = '' THEN
            RAISE EXCEPTION 'revision-requested thesis proposal requires reviewer_id';
        END IF;
        IF NEW.revision_instructions IS NULL OR BTRIM(NEW.revision_instructions) = '' THEN
            RAISE EXCEPTION 'revision-requested thesis proposal requires revision_instructions';
        END IF;
        NEW.reviewed_at := COALESCE(NEW.reviewed_at, NOW());
    END IF;

    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS thesis_proposal_lifecycle ON investment_thesis_proposals;
CREATE TRIGGER thesis_proposal_lifecycle
BEFORE UPDATE OR DELETE ON investment_thesis_proposals
FOR EACH ROW EXECUTE FUNCTION guard_thesis_proposal_lifecycle();
