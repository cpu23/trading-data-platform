CREATE TABLE IF NOT EXISTS macro_series_metadata (
    series_id TEXT PRIMARY KEY,
    title TEXT,
    units TEXT,
    seasonal_adjustment TEXT,
    frequency TEXT,
    fetched_at TIMESTAMPTZ NOT NULL
);
