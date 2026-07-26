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
