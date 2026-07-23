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
