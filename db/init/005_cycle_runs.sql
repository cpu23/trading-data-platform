CREATE TABLE IF NOT EXISTS cycle_runs (
  correlation_id UUID PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
  started_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  error_message TEXT,
  triggered_by TEXT NOT NULL DEFAULT 'manual',
  run_kind TEXT NOT NULL DEFAULT 'cycle',
  requested_component TEXT,
  result_status TEXT,
  summary JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_cycle_runs_started_at ON cycle_runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_collection_log_correlation ON collection_log (correlation_id, started_at);
CREATE INDEX IF NOT EXISTS idx_processing_log_correlation ON processing_log (correlation_id, started_at);
