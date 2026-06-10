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
