CREATE TABLE IF NOT EXISTS cycle_runs (
  correlation_id UUID PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
  started_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  error_message TEXT,
  triggered_by TEXT NOT NULL DEFAULT 'manual'
);

CREATE INDEX IF NOT EXISTS idx_cycle_runs_started_at ON cycle_runs (started_at DESC);