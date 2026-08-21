-- 050: Free public market sources: corporate actions and options chain
-- snapshots.
--
-- Adds two append-only fact tables consumed by the keyless public-market
-- collectors (equity daily prices/corporate actions, options chain
-- snapshots) plus a nullable metadata column on the canonical market_data
-- hypertable so price rows can distinguish provider (source) time from
-- acquisition/availability time.
--
-- Point-in-time identity: corporate action rows are identified by a
-- deterministic digest over (source, symbol, action_type, effective_date,
-- amount or ratio).  Re-collecting the same action is an idempotent no-op;
-- a provider amendment (new amount/ratio/date) produces a NEW action_id
-- row instead of mutating history, so the table is a faithful append-only
-- record of what the source served at each point in time.  Options chain
-- rows carry the same identity inside (source, contract_symbol,
-- captured_at): each fetch is one immutable snapshot.
--
-- Immutable snapshot semantics: rows in both tables are frozen.  Writes
-- arrive exclusively through ON CONFLICT DO NOTHING (identical snapshots
-- are no-ops); the guard triggers below make accidental UPDATE/DELETE
-- fail loudly instead of silently rewriting provider history.
--
-- Finite/range/type checks: every numeric column rejects NaN/Infinity
-- (NaN is excluded by `x = x`, infinities by the explicit bounds) and
-- enforces its domain range: non-negative prices, strictly positive
-- strikes and split ratios, bounded implied volatility, and per-type
-- field sets for split vs dividend rows.
--
-- Fully idempotent: every statement is guarded (IF NOT EXISTS / CREATE
-- OR REPLACE / EXCEPTION duplicate_object) so the file can be re-applied
-- on fresh or upgraded databases.
-- Rollback: drop the guard triggers, drop the two tables, then drop the
-- market_data metadata column.

-- ---------------------------------------------------------------------------
-- 1. Corporate actions (append-only; corrections are new action_id rows).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS corporate_actions (
    action_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    action_type TEXT NOT NULL,
    effective_date DATE NOT NULL,
    source TEXT NOT NULL,
    -- Provider-reported event time (e.g. ex-date announcement timestamp);
    -- distinct from available_at, the acquisition time recorded below.
    source_timestamp TIMESTAMPTZ NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    -- Dividend cash amount per share (dividend rows only).
    amount DOUBLE PRECISION,
    -- Split ratio (split rows only): numerator/denominator, e.g. 4/1.
    ratio_numerator DOUBLE PRECISION,
    ratio_denominator DOUBLE PRECISION,
    description TEXT,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT corporate_actions_type_check
        CHECK (action_type IN ('split', 'dividend')),
    CONSTRAINT corporate_actions_symbol_length_check
        CHECK (length(symbol) BETWEEN 1 AND 32),
    CONSTRAINT corporate_actions_source_length_check
        CHECK (length(source) BETWEEN 1 AND 64),
    -- amount: finite, non-negative (NaN rejected by amount = amount).
    CONSTRAINT corporate_actions_amount_finite_range_check
        CHECK (
            amount IS NULL
            OR (
                amount = amount
                AND amount >= 0
                AND amount < 'Infinity'::double precision
            )
        ),
    -- ratio: both sides present together, finite, strictly positive.
    CONSTRAINT corporate_actions_ratio_finite_range_check
        CHECK (
            (ratio_numerator IS NULL AND ratio_denominator IS NULL)
            OR (
                ratio_numerator IS NOT NULL
                AND ratio_denominator IS NOT NULL
                AND ratio_numerator = ratio_numerator
                AND ratio_denominator = ratio_denominator
                AND ratio_numerator > 0
                AND ratio_denominator > 0
                AND ratio_numerator < 'Infinity'::double precision
                AND ratio_denominator < 'Infinity'::double precision
            )
        ),
    -- Per-type field sets: dividends carry amount, splits carry the ratio.
    CONSTRAINT corporate_actions_type_fields_check
        CHECK (
            (action_type = 'dividend' AND amount IS NOT NULL
                AND ratio_numerator IS NULL AND ratio_denominator IS NULL)
            OR (action_type = 'split' AND amount IS NULL
                AND ratio_numerator IS NOT NULL AND ratio_denominator IS NOT NULL)
        )
);

CREATE INDEX IF NOT EXISTS idx_corporate_actions_symbol_effective
    ON corporate_actions (symbol, effective_date DESC);
CREATE INDEX IF NOT EXISTS idx_corporate_actions_source_effective
    ON corporate_actions (source, effective_date DESC);

-- ---------------------------------------------------------------------------
-- 2. Options chain snapshots (immutable per-fetch rows).
--
-- Identity: PRIMARY KEY (source, contract_symbol, captured_at).  One chain
-- fetch for a symbol produces one row per contract sharing captured_at
-- (acquisition time); source_timestamp is the provider quote time and may
-- be absent.  Re-collecting the identical snapshot is an idempotent
-- no-op; a later fetch with a different captured_at is a new snapshot.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS option_chain_snapshots (
    source TEXT NOT NULL,
    symbol TEXT NOT NULL,
    contract_symbol TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    source_timestamp TIMESTAMPTZ,
    expiration DATE NOT NULL,
    strike DOUBLE PRECISION NOT NULL,
    option_type TEXT NOT NULL,
    bid DOUBLE PRECISION,
    ask DOUBLE PRECISION,
    last DOUBLE PRECISION,
    volume DOUBLE PRECISION,
    open_interest DOUBLE PRECISION,
    implied_volatility DOUBLE PRECISION,
    underlying_price DOUBLE PRECISION,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, contract_symbol, captured_at),
    CONSTRAINT option_chain_snapshots_option_type_check
        CHECK (option_type IN ('call', 'put')),
    CONSTRAINT option_chain_snapshots_symbol_length_check
        CHECK (length(symbol) BETWEEN 1 AND 32),
    CONSTRAINT option_chain_snapshots_strike_finite_positive_check
        CHECK (
            strike = strike
            AND strike > 0
            AND strike < 'Infinity'::double precision
        ),
    -- Quotes and underlier: finite, non-negative when present.
    CONSTRAINT option_chain_snapshots_prices_finite_nonneg_check
        CHECK (
            (bid IS NULL OR (bid = bid AND bid >= 0
                AND bid < 'Infinity'::double precision))
            AND (ask IS NULL OR (ask = ask AND ask >= 0
                AND ask < 'Infinity'::double precision))
            AND (last IS NULL OR (last = last AND last >= 0
                AND last < 'Infinity'::double precision))
            AND (underlying_price IS NULL
                OR (underlying_price = underlying_price
                    AND underlying_price >= 0
                    AND underlying_price < 'Infinity'::double precision))
        ),
    -- Activity: finite, non-negative when present.
    CONSTRAINT option_chain_snapshots_activity_finite_nonneg_check
        CHECK (
            (volume IS NULL OR (volume = volume AND volume >= 0
                AND volume < 'Infinity'::double precision))
            AND (open_interest IS NULL
                OR (open_interest = open_interest AND open_interest >= 0
                    AND open_interest < 'Infinity'::double precision))
        ),
    -- Implied volatility: bounded to the plausible 0..1000% band.
    CONSTRAINT option_chain_snapshots_iv_finite_range_check
        CHECK (
            implied_volatility IS NULL
            OR (
                implied_volatility = implied_volatility
                AND implied_volatility >= 0
                AND implied_volatility <= 10
            )
        )
);

CREATE INDEX IF NOT EXISTS idx_option_chain_snapshots_symbol_captured
    ON option_chain_snapshots (symbol, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_option_chain_snapshots_expiration
    ON option_chain_snapshots (expiration);

-- ---------------------------------------------------------------------------
-- 3. Price rows distinguish source time from acquisition time in metadata.
--    Nullable column, additive on the existing hypertable; existing rows
--    carry the default empty object.
-- ---------------------------------------------------------------------------

ALTER TABLE market_data
    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}';

-- ---------------------------------------------------------------------------
-- 4. Immutability guards: UPDATE/DELETE on either fact table is refused;
--    new facts arrive as new rows (DO NOTHING upserts).
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION prevent_market_source_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION '% rows are immutable snapshots; insert a new row instead of updating or deleting', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    CREATE TRIGGER corporate_actions_immutable_guard
        BEFORE UPDATE OR DELETE ON corporate_actions
        FOR EACH ROW EXECUTE FUNCTION prevent_market_source_mutation();
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE TRIGGER option_chain_snapshots_immutable_guard
        BEFORE UPDATE OR DELETE ON option_chain_snapshots
        FOR EACH ROW EXECUTE FUNCTION prevent_market_source_mutation();
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;
