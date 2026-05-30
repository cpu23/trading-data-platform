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
    config_snapshot JSONB
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
    error_message TEXT
);
