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
