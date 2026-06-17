-- TimescaleDB data retention policies.
-- These drop chunks older than the specified interval.
-- Applied via: python cli.py migrate

-- Keep macro series data for 5 years
SELECT add_retention_policy('macro_series', INTERVAL '5 years',
    if_not_exists => TRUE);

-- Keep market data for 2 years
SELECT add_retention_policy('market_data', INTERVAL '2 years',
    if_not_exists => TRUE);
