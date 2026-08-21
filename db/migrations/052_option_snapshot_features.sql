-- 052: Immutable per-snapshot option analytics features + operational
-- storage for raw option chains.
--
-- Two concerns:
--
-- 1. Raw contract rows (option_chain_snapshots, migration 050) become a
--    Timescale hypertable partitioned on captured_at with a 90-day
--    retention policy: each fetch writes thousands of contracts and raw
--    history must not grow unbounded.  The composite primary key already
--    contains captured_at and the column is NOT NULL, so the conversion is
--    safe with migrate_data.  The immutability guard from migration 050 is
--    preserved across the conversion (triggers survive).
--
-- 2. One feature row per (source, symbol, captured_at) snapshot: the
--    deterministic analytics computed by options_analytics.analyze_chain
--    over the bounded, validated contracts of that snapshot at collection
--    time (ATM IV, implied move, put/call skew, volume/open-interest
--    totals, term structure, and gated unusualness).  Feature rows are
--    long-lived aggregates on a plain table (no retention policy): they
--    survive raw-chunk expiry so snapshot-level analytics history remains
--    queryable.
--
-- Feature rows are persisted insert-only in the same transaction as the
-- chain rows, so re-collecting the identical snapshot is an idempotent
-- no-op and the analytics always match exactly the contracts that were
-- persisted.  Explicit state semantics: analytics never backfills missing
-- IV/open interest and never claims historical unusualness without local
-- history; the per-metric insufficient_data / insufficient_history states
-- produced by the analyzer are preserved verbatim in the analytics JSONB.
--
-- Time semantics: captured_at is the snapshot acquisition time (identity),
-- source_timestamp_min/max bound the provider quote times of the analyzed
-- contracts (NULL when the provider sent none), available_at is when the
-- feature row became available (same acquisition time as the snapshot),
-- and created_at is the row persistence time.  Replay cutoffs consult the
-- feature available/created times.
--
-- Immutable: writes arrive exclusively through ON CONFLICT DO NOTHING; the
-- guard trigger below refuses UPDATE/DELETE, mirroring migration 050.
--
-- Fully idempotent: every statement is guarded (IF NOT EXISTS / hypertable
-- existence check / EXCEPTION duplicate_object) so the file can be
-- re-applied on fresh or upgraded databases.
-- Rollback: drop the retention policy, drop the guard trigger, then drop
-- the feature table (dehypertable conversion of option_chain_snapshots is
-- out of scope for an additive migration).

-- ---------------------------------------------------------------------------
-- 1. Raw option chains: hypertable on captured_at + 90-day retention.
--    Conversion is guarded by an existence check so re-applying the file
--    (or upgrading a database where the table is already chunked) is a
--    no-op; migrate_data moves any pre-conversion rows into chunks.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM timescaledb_information.hypertables
        WHERE hypertable_schema = 'public'
          AND hypertable_name = 'option_chain_snapshots'
    ) THEN
        PERFORM create_hypertable(
            'option_chain_snapshots',
            'captured_at',
            migrate_data => true
        );
    END IF;
END $$;

SELECT add_retention_policy('option_chain_snapshots', INTERVAL '90 days',
    if_not_exists => TRUE);

-- ---------------------------------------------------------------------------
-- 2. Per-snapshot analytics features (long-lived plain table, no retention).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS option_snapshot_features (
    source TEXT NOT NULL,
    symbol TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    -- Analytics contract version; bump when the analyzer output shape
    -- changes so consumers can distinguish feature generations.
    feature_version TEXT NOT NULL,
    -- Provider quote-time bounds over the analyzed contracts (UTC).
    source_timestamp_min TIMESTAMPTZ,
    source_timestamp_max TIMESTAMPTZ,
    -- Availability time of this feature row (same acquisition time as the
    -- snapshot); distinct from source times and from created_at below.
    available_at TIMESTAMPTZ NOT NULL,
    -- Number of analyzed contracts (the bounded validated subset of the
    -- snapshot; rejected or bounded-out contracts are never analyzed).
    contract_count INTEGER NOT NULL,
    analytics JSONB NOT NULL DEFAULT '{}',
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, symbol, captured_at),
    CONSTRAINT option_snapshot_features_symbol_length_check
        CHECK (length(symbol) BETWEEN 1 AND 32),
    CONSTRAINT option_snapshot_features_source_length_check
        CHECK (length(source) BETWEEN 1 AND 64),
    CONSTRAINT option_snapshot_features_version_check
        CHECK (feature_version <> ''),
    CONSTRAINT option_snapshot_features_contract_count_check
        CHECK (contract_count >= 0),
    CONSTRAINT option_snapshot_features_analytics_object_check
        CHECK (jsonb_typeof(analytics) = 'object'),
    CONSTRAINT option_snapshot_features_metadata_object_check
        CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE INDEX IF NOT EXISTS idx_option_snapshot_features_symbol_captured
    ON option_snapshot_features (symbol, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_option_snapshot_features_captured
    ON option_snapshot_features (captured_at DESC);

-- Immutability guard: UPDATE/DELETE on the feature table is refused; new
-- facts arrive as new rows (DO NOTHING upserts).  Reuses the migration 050
-- guard function, which reports the offending table name.
DO $$
BEGIN
    CREATE TRIGGER option_snapshot_features_immutable_guard
        BEFORE UPDATE OR DELETE ON option_snapshot_features
        FOR EACH ROW EXECUTE FUNCTION prevent_market_source_mutation();
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;
