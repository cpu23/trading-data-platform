-- Add durable lifecycle metadata to cycle jobs without dropping historical data.
ALTER TABLE cycle_runs
    ADD COLUMN IF NOT EXISTS accepted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS worker_id TEXT,
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

-- CURRENT_TIMESTAMP is transaction-stable and is only a defensive fallback for
-- schemas where started_at was already made nullable before this migration.
UPDATE cycle_runs
SET accepted_at = COALESCE(started_at, CURRENT_TIMESTAMP)
WHERE accepted_at IS NULL;

UPDATE cycle_runs
SET heartbeat_at = started_at
WHERE status = 'running'
  AND heartbeat_at IS NULL;

ALTER TABLE cycle_runs
    ALTER COLUMN accepted_at SET NOT NULL,
    ALTER COLUMN started_at DROP NOT NULL;

-- Validate the expanded lifecycle while the bootstrap constraint still protects
-- writes. The known bootstrap name is cycle_runs_status_check.
ALTER TABLE cycle_runs
    ADD CONSTRAINT cycle_runs_status_check_expanded
    CHECK (status IN ('accepted', 'running', 'completed', 'failed', 'abandoned'))
    NOT VALID;

ALTER TABLE cycle_runs
    VALIDATE CONSTRAINT cycle_runs_status_check_expanded;

ALTER TABLE cycle_runs
    DROP CONSTRAINT cycle_runs_status_check;

ALTER TABLE cycle_runs
    RENAME CONSTRAINT cycle_runs_status_check_expanded TO cycle_runs_status_check;

CREATE UNIQUE INDEX IF NOT EXISTS idx_cycle_runs_idempotency_key
    ON cycle_runs (idempotency_key)
    WHERE idempotency_key IS NOT NULL;
