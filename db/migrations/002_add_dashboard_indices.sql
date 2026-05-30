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