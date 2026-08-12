-- Phase 5.1 calendar-aware reaction windows with timeframe-scoped identity.
--
-- Adds additive, nullable columns for persisted timestamp offsets and
-- calendar/volatility provenance, then performs a deterministic dedupe on the
-- NEW full identity BEFORE tightening the unique constraint.
--
-- The legacy unique identity (event_id, instrument_symbol, horizon) was
-- stricter than the new one, so legacy rows cannot collide across timeframes;
-- only rows sharing the full (event_id, instrument_symbol, timeframe, horizon)
-- identity can be duplicates (rows inserted before the constraint existed).
-- The deterministic winner per full identity is the most recently updated row
-- (ties broken by the greatest id, i.e. the most recently inserted row); every
-- other duplicate is deleted. Rows with distinct timeframes are preserved.
--
-- Rollback (dependency-safe order): drop the new indexes and constraints,
-- restore the legacy UNIQUE (event_id, instrument_symbol, horizon), then
-- drop the additive columns.

ALTER TABLE event_reaction_windows
    ADD COLUMN IF NOT EXISTS baseline_offset_seconds BIGINT,
    ADD COLUMN IF NOT EXISTS target_offset_seconds BIGINT,
    ADD COLUMN IF NOT EXISTS calendar_name TEXT,
    ADD COLUMN IF NOT EXISTS calendar_version TEXT,
    ADD COLUMN IF NOT EXISTS volatility_version INTEGER;

-- Deterministic dedupe on the new full identity: keep the most recently
-- updated row; ties broken by the greatest id.
DELETE FROM event_reaction_windows legacy
USING event_reaction_windows keep
WHERE keep.event_id = legacy.event_id
  AND keep.instrument_symbol = legacy.instrument_symbol
  AND keep.timeframe = legacy.timeframe
  AND keep.horizon = legacy.horizon
  AND (keep.updated_at, keep.id) > (legacy.updated_at, legacy.id);

-- Backfill timestamp offsets for pre-044 rows from persisted timestamps.
-- baseline_offset_seconds is measured from event_at and is strictly negative
-- (baseline rows are strictly pre-event); legacy rows whose baseline sat on
-- the event timestamp keep a NULL offset rather than violating that rule.
-- target_offset_seconds is measured from target_at (operationally the delay
-- between the planned target and the observed sample) and may be negative,
-- zero, or positive, so it carries no sign constraint.
UPDATE event_reaction_windows SET
    baseline_offset_seconds = CASE
        WHEN baseline_at IS NOT NULL AND event_at IS NOT NULL
             AND baseline_at < event_at
            THEN FLOOR(EXTRACT(EPOCH FROM (baseline_at - event_at)))::BIGINT
        ELSE NULL
    END,
    target_offset_seconds = CASE
        WHEN observed_at IS NOT NULL AND target_at IS NOT NULL
            THEN FLOOR(EXTRACT(EPOCH FROM (observed_at - target_at)))::BIGINT
        ELSE NULL
    END
WHERE baseline_offset_seconds IS NULL OR target_offset_seconds IS NULL;

-- Legacy volatility labeling: rows already resolved under the pre-044 per-bar
-- volatility carry volatility_version = 1 wherever the adjusted metric exists
-- (NULL stays reserved for rows with no volatility-based metric, so old
-- tick-vol semantics are never ambiguous or mixable with v2). An explicit
-- legacy marker is added to provenance without overwriting existing keys;
-- the explicit recompute path later relabels them to the current version.
UPDATE event_reaction_windows
SET volatility_version = 1
WHERE volatility_version IS NULL
  AND volatility_adjusted_move IS NOT NULL;

UPDATE event_reaction_windows
SET provenance = provenance
    || jsonb_build_object(
        'volatility',
        jsonb_build_object('version', 1, 'method', 'legacy_per_bar')
    )
WHERE volatility_version = 1
  AND NOT provenance ? 'volatility';

-- Tighten persisted identity to include timeframe so one event can hold
-- distinct reaction windows per instrument timeframe.
ALTER TABLE event_reaction_windows
    DROP CONSTRAINT IF EXISTS event_reaction_windows_identity_unique;
ALTER TABLE event_reaction_windows
    ADD CONSTRAINT event_reaction_windows_identity_unique
    UNIQUE (event_id, instrument_symbol, timeframe, horizon);

-- Constraint documentation for the new additive columns. Baseline offsets are
-- strictly negative when present; target offsets are intentionally unconstrained.
-- Guard each addition so the checksum-verified migration chain remains rerunnable.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'event_reaction_windows'::regclass
          AND conname = 'event_reaction_windows_baseline_offset_sign_check'
    ) THEN
        ALTER TABLE event_reaction_windows
            ADD CONSTRAINT event_reaction_windows_baseline_offset_sign_check
            CHECK (baseline_offset_seconds IS NULL OR baseline_offset_seconds < 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'event_reaction_windows'::regclass
          AND conname = 'event_reaction_windows_calendar_name_nonblank_check'
    ) THEN
        ALTER TABLE event_reaction_windows
            ADD CONSTRAINT event_reaction_windows_calendar_name_nonblank_check
            CHECK (calendar_name IS NULL OR BTRIM(calendar_name) <> '');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'event_reaction_windows'::regclass
          AND conname = 'event_reaction_windows_volatility_version_check'
    ) THEN
        ALTER TABLE event_reaction_windows
            ADD CONSTRAINT event_reaction_windows_volatility_version_check
            CHECK (volatility_version IS NULL OR volatility_version >= 1);
    END IF;
END $$;

-- Extend the missing-data-reason vocabulary with the post-selection baseline
-- freshness outcome (stale_baseline). Replaces the 031-era constraint; guarded
-- so the chain remains rerunnable.
ALTER TABLE event_reaction_windows
    DROP CONSTRAINT IF EXISTS event_reaction_windows_missing_reason_check;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'event_reaction_windows'::regclass
          AND conname = 'event_reaction_windows_missing_reason_check'
    ) THEN
        ALTER TABLE event_reaction_windows
            ADD CONSTRAINT event_reaction_windows_missing_reason_check
            CHECK (missing_data_reason IS NULL OR missing_data_reason IN (
                'future_window', 'missing_baseline', 'missing_target',
                'zero_baseline', 'zero_target', 'stale_baseline'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_event_reaction_windows_event_identity
    ON event_reaction_windows (event_id, timeframe, instrument_symbol, horizon);
