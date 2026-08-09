-- Isolate longitudinal replay lifecycle history by resolved model/prompt variant.
-- Additive and idempotent. Rollback: drop the index, constraints, and columns.

ALTER TABLE research_replay_runs
    ADD COLUMN IF NOT EXISTS variant_fingerprint TEXT;
ALTER TABLE research_replay_runs
    ADD COLUMN IF NOT EXISTS variant_identity JSONB NOT NULL DEFAULT '{}'::JSONB;


UPDATE research_replay_runs
SET variant_fingerprint = execution_fingerprint
WHERE variant_fingerprint IS NULL;

ALTER TABLE research_replay_runs
    ALTER COLUMN variant_fingerprint SET NOT NULL;

DO $$
BEGIN
    ALTER TABLE research_replay_runs
        ADD CONSTRAINT research_replay_runs_variant_fingerprint_check
        CHECK (variant_fingerprint ~ '^[a-f0-9]{64}$');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE research_replay_runs
        ADD CONSTRAINT research_replay_runs_variant_identity_object_check
        CHECK (jsonb_typeof(variant_identity) = 'object');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_research_replay_runs_variant_timeline
    ON research_replay_runs (
        benchmark_id, variant_fingerprint, comparison_group,
        replay_as_of DESC, created_at DESC
    )
    WHERE benchmark_id IS NOT NULL;
