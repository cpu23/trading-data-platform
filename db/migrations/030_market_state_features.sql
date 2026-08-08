-- Phase 5 deterministic market-state primitives.
--
-- Raw market_data is retained by the existing market_data policy. These
-- continuous aggregates are retained for shorter periods: 1m keeps 90 days,
-- 5m keeps 180 days, 15m keeps 1 year, 1h keeps 2 years, and 1d keeps 5
-- years. View retention drops only materialized aggregate chunks; it never
-- deletes rows from market_data.
--
-- Every statement is additive/idempotent. Rollback is dependency-safe:
-- remove feature consumers, drop market_feature_snapshots, then remove the
-- continuous-aggregate policies, indexes, and views in reverse order.

CREATE MATERIALIZED VIEW IF NOT EXISTS market_data_1m
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '1 minute', timestamp) AS bucket, symbol, source,
       first(open, timestamp) AS open, max(high) AS high, min(low) AS low,
       last(close, timestamp) AS close, sum(volume) AS volume, count(*) AS samples
FROM market_data
WHERE timeframe = 'PRICE'
GROUP BY bucket, symbol, source
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS market_data_5m
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '5 minutes', timestamp) AS bucket, symbol, source,
       first(open, timestamp) AS open, max(high) AS high, min(low) AS low,
       last(close, timestamp) AS close, sum(volume) AS volume, count(*) AS samples
FROM market_data
WHERE timeframe = 'PRICE'
GROUP BY bucket, symbol, source
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS market_data_15m
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '15 minutes', timestamp) AS bucket, symbol, source,
       first(open, timestamp) AS open, max(high) AS high, min(low) AS low,
       last(close, timestamp) AS close, sum(volume) AS volume, count(*) AS samples
FROM market_data
WHERE timeframe = 'PRICE'
GROUP BY bucket, symbol, source
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS market_data_1h
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '1 hour', timestamp) AS bucket, symbol, source,
       first(open, timestamp) AS open, max(high) AS high, min(low) AS low,
       last(close, timestamp) AS close, sum(volume) AS volume, count(*) AS samples
FROM market_data
WHERE timeframe = 'PRICE'
GROUP BY bucket, symbol, source
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS market_data_1d
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '1 day', timestamp) AS bucket, symbol, source,
       first(open, timestamp) AS open, max(high) AS high, min(low) AS low,
       last(close, timestamp) AS close, sum(volume) AS volume, count(*) AS samples
FROM market_data
WHERE timeframe = 'PRICE'
GROUP BY bucket, symbol, source
WITH NO DATA;

CREATE INDEX IF NOT EXISTS idx_market_data_1m_symbol_bucket
    ON market_data_1m (symbol, bucket DESC);
CREATE INDEX IF NOT EXISTS idx_market_data_5m_symbol_bucket
    ON market_data_5m (symbol, bucket DESC);
CREATE INDEX IF NOT EXISTS idx_market_data_15m_symbol_bucket
    ON market_data_15m (symbol, bucket DESC);
CREATE INDEX IF NOT EXISTS idx_market_data_1h_symbol_bucket
    ON market_data_1h (symbol, bucket DESC);
CREATE INDEX IF NOT EXISTS idx_market_data_1d_symbol_bucket
    ON market_data_1d (symbol, bucket DESC);

SELECT add_continuous_aggregate_policy('market_data_1m', start_offset => INTERVAL '90 days', end_offset => INTERVAL '1 minute', schedule_interval => INTERVAL '1 minute', if_not_exists => TRUE);
SELECT add_continuous_aggregate_policy('market_data_5m', start_offset => INTERVAL '180 days', end_offset => INTERVAL '5 minutes', schedule_interval => INTERVAL '5 minutes', if_not_exists => TRUE);
SELECT add_continuous_aggregate_policy('market_data_15m', start_offset => INTERVAL '1 year', end_offset => INTERVAL '15 minutes', schedule_interval => INTERVAL '15 minutes', if_not_exists => TRUE);
SELECT add_continuous_aggregate_policy('market_data_1h', start_offset => INTERVAL '2 years', end_offset => INTERVAL '1 hour', schedule_interval => INTERVAL '1 hour', if_not_exists => TRUE);
SELECT add_continuous_aggregate_policy('market_data_1d', start_offset => INTERVAL '5 years', end_offset => INTERVAL '1 day', schedule_interval => INTERVAL '1 day', if_not_exists => TRUE);

SELECT add_retention_policy('market_data_1m', INTERVAL '90 days', if_not_exists => TRUE);
SELECT add_retention_policy('market_data_5m', INTERVAL '180 days', if_not_exists => TRUE);
SELECT add_retention_policy('market_data_15m', INTERVAL '1 year', if_not_exists => TRUE);
SELECT add_retention_policy('market_data_1h', INTERVAL '2 years', if_not_exists => TRUE);
SELECT add_retention_policy('market_data_1d', INTERVAL '5 years', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS market_feature_snapshots (
    symbol TEXT NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    source_event_id UUID NOT NULL REFERENCES market_events(id),
    features JSONB NOT NULL,
    unavailable JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, as_of, source_event_id),
    CONSTRAINT market_feature_snapshots_symbol_nonblank_check CHECK (BTRIM(symbol) <> ''),
    CONSTRAINT market_feature_snapshots_features_object_check CHECK (JSONB_TYPEOF(features) = 'object'),
    CONSTRAINT market_feature_snapshots_unavailable_object_check CHECK (JSONB_TYPEOF(unavailable) = 'object')
);

CREATE INDEX IF NOT EXISTS idx_market_feature_snapshots_symbol_asof
    ON market_feature_snapshots (symbol, as_of DESC);
CREATE INDEX IF NOT EXISTS idx_market_feature_snapshots_source_event
    ON market_feature_snapshots (source_event_id);
