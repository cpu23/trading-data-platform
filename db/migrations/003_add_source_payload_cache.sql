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
