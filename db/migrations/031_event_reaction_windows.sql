-- Phase 5 deterministic market-event reaction windows.
-- One row is retained for every material event, mapped instrument, and horizon.
-- The orchestrator owns the transaction; this migration is additive/idempotent.
CREATE TABLE IF NOT EXISTS event_reaction_windows (
    id BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL REFERENCES market_events(id) ON DELETE CASCADE,
    instrument_symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL DEFAULT 'PRICE',
    horizon TEXT NOT NULL,
    event_at TIMESTAMPTZ NOT NULL,
    baseline_at TIMESTAMPTZ,
    target_at TIMESTAMPTZ NOT NULL,
    baseline_price DOUBLE PRECISION,
    target_price DOUBLE PRECISION,
    observed_at TIMESTAMPTZ,
    observed_price DOUBLE PRECISION,
    absolute_move DOUBLE PRECISION,
    percentage_move DOUBLE PRECISION,
    volatility_adjusted_move DOUBLE PRECISION,
    expected_direction TEXT NOT NULL DEFAULT 'neutral',
    sensitivity TEXT NOT NULL DEFAULT 'neutral',
    direction_vs_expected TEXT NOT NULL DEFAULT 'unknown',
    reaction_state TEXT NOT NULL DEFAULT 'pending',
    missing_data_reason TEXT,
    source_payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    provenance JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT event_reaction_windows_identity_unique
        UNIQUE (event_id, instrument_symbol, horizon),
    CONSTRAINT event_reaction_windows_symbol_nonblank_check
        CHECK (BTRIM(instrument_symbol) <> ''),
    CONSTRAINT event_reaction_windows_timeframe_nonblank_check
        CHECK (BTRIM(timeframe) <> ''),
    CONSTRAINT event_reaction_windows_horizon_check
        CHECK (horizon IN ('1m', '5m', '15m', '30m', '60m', 'end_of_session')),
    CONSTRAINT event_reaction_windows_direction_check
        CHECK (expected_direction IN ('up', 'down', 'neutral')),
    CONSTRAINT event_reaction_windows_sensitivity_check
        CHECK (sensitivity IN ('positive', 'negative', 'neutral', 'high', 'moderate', 'low')),
    CONSTRAINT event_reaction_windows_direction_result_check
        CHECK (direction_vs_expected IN ('aligned', 'opposed', 'neutral', 'unknown')),
    CONSTRAINT event_reaction_windows_state_check
        CHECK (reaction_state IN ('pending', 'persistence', 'reversal', 'mixed')),
    CONSTRAINT event_reaction_windows_missing_reason_check
        CHECK (missing_data_reason IS NULL OR missing_data_reason IN (
            'future_window', 'missing_baseline', 'missing_target',
            'zero_baseline', 'zero_target')),
    CONSTRAINT event_reaction_windows_source_payload_object_check
        CHECK (JSONB_TYPEOF(source_payload) = 'object'),
    CONSTRAINT event_reaction_windows_provenance_object_check
        CHECK (JSONB_TYPEOF(provenance) = 'object'),
    CONSTRAINT event_reaction_windows_baseline_price_finite_check
        CHECK (baseline_price IS NULL OR
            baseline_price NOT IN ('NaN'::DOUBLE PRECISION, 'Infinity'::DOUBLE PRECISION,
                                   '-Infinity'::DOUBLE PRECISION)),
    CONSTRAINT event_reaction_windows_target_price_finite_check
        CHECK (target_price IS NULL OR
            target_price NOT IN ('NaN'::DOUBLE PRECISION, 'Infinity'::DOUBLE PRECISION,
                                 '-Infinity'::DOUBLE PRECISION)),
    CONSTRAINT event_reaction_windows_absolute_move_finite_check
        CHECK (absolute_move IS NULL OR
            absolute_move NOT IN ('NaN'::DOUBLE PRECISION, 'Infinity'::DOUBLE PRECISION,
                                  '-Infinity'::DOUBLE PRECISION)),
    CONSTRAINT event_reaction_windows_percentage_move_finite_check
        CHECK (percentage_move IS NULL OR
            percentage_move NOT IN ('NaN'::DOUBLE PRECISION, 'Infinity'::DOUBLE PRECISION,
                                    '-Infinity'::DOUBLE PRECISION)),
    CONSTRAINT event_reaction_windows_volatility_move_finite_check
        CHECK (volatility_adjusted_move IS NULL OR
            volatility_adjusted_move NOT IN ('NaN'::DOUBLE PRECISION, 'Infinity'::DOUBLE PRECISION,
                                             '-Infinity'::DOUBLE PRECISION))
);

CREATE INDEX IF NOT EXISTS idx_event_reaction_windows_pending_target
    ON event_reaction_windows (target_at, id)
    WHERE reaction_state = 'pending' OR missing_data_reason IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_event_reaction_windows_event_horizon
    ON event_reaction_windows (event_id, horizon, instrument_symbol);

CREATE INDEX IF NOT EXISTS idx_event_reaction_windows_instrument_observed
    ON event_reaction_windows (instrument_symbol, observed_at DESC)
    WHERE observed_at IS NOT NULL;
