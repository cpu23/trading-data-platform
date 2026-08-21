-- One active forecast per scenario: deterministic legacy dedupe plus a
-- partial unique index.
--
-- The autonomous desk contract guarantees at most one unsuperseded
-- forecast per non-null scenario: the first frozen as_of/reference
-- close/target/target date wins until explicitly superseded.  Databases
-- that ran 049 before this guard can contain legacy duplicates created by
-- reruns whose forecast_key changed (target date or fingerprint drift).
-- This migration deterministically keeps the earliest frozen row per
-- scenario (created_at, then id) and supersedes every other active
-- duplicate at its own created_at — the one-time NULL -> non-NULL
-- transition the forecast lifecycle trigger permits, satisfying the
-- superseded_after_created CHECK — leaving immutable history intact.  It
-- then installs the partial unique index so the invariant is enforced on
-- every future write; the bounded precheck in the freeze path keeps
-- ordinary reruns from reaching it.
--
-- Scenario-less forecasts (scenario_id IS NULL) stay valid and are outside
-- the index.  Fully idempotent: the dedupe UPDATE is a no-op once no
-- active duplicates remain, and the index creation is guarded, so the file
-- re-applies cleanly on fresh and upgraded databases.
-- Rollback: drop the index.  Superseded duplicates remain as frozen
-- history and are never deleted.

WITH active_duplicates AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY scenario_id
               ORDER BY created_at, id
           ) AS kept_rank
    FROM investment_thesis_forecasts
    WHERE scenario_id IS NOT NULL
      AND superseded_at IS NULL
)
UPDATE investment_thesis_forecasts f
SET superseded_at = f.created_at
FROM active_duplicates d
WHERE d.id = f.id
  AND d.kept_rank > 1;

CREATE UNIQUE INDEX IF NOT EXISTS idx_investment_thesis_forecasts_active_scenario
    ON investment_thesis_forecasts (scenario_id)
    WHERE scenario_id IS NOT NULL AND superseded_at IS NULL;
