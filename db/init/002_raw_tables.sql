CREATE TABLE macro_series (
    series_id TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    value DOUBLE PRECISION,
    source TEXT NOT NULL,
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (series_id, observed_at)
);

SELECT create_hypertable('macro_series', 'observed_at', migrate_data => true);

CREATE TRIGGER macro_series_updated_at
    BEFORE UPDATE ON macro_series
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();


CREATE TABLE macro_series_metadata (
    series_id TEXT PRIMARY KEY,
    title TEXT,
    units TEXT,
    seasonal_adjustment TEXT,
    frequency TEXT,
    fetched_at TIMESTAMPTZ NOT NULL
);


CREATE TABLE source_payload_cache (
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

CREATE INDEX idx_source_payload_cache_source_week
    ON source_payload_cache (source, target_week);

CREATE TRIGGER source_payload_cache_updated_at
    BEFORE UPDATE ON source_payload_cache
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();


CREATE TABLE econ_events (
    event_id TEXT PRIMARY KEY,
    event_name TEXT NOT NULL,
    country TEXT NOT NULL,
    scheduled_at TIMESTAMPTZ NOT NULL,
    impact_level TEXT,
    consensus TEXT,
    previous TEXT,
    actual TEXT,
    source TEXT NOT NULL,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_econ_events_scheduled_at ON econ_events (scheduled_at);

CREATE TRIGGER econ_events_updated_at
    BEFORE UPDATE ON econ_events
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();


CREATE TABLE market_data (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    open DOUBLE PRECISION,
    high DOUBLE PRECISION,
    low DOUBLE PRECISION,
    close DOUBLE PRECISION,
    volume DOUBLE PRECISION,
    source TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (symbol, timeframe, timestamp)
);

SELECT create_hypertable('market_data', 'timestamp', migrate_data => true);

CREATE TRIGGER market_data_updated_at
    BEFORE UPDATE ON market_data
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
