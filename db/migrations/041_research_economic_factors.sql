-- Complete factor-first macro state introduced by migration 040.
-- Additive/idempotent. Rollback: drop the two added columns after reverting consumers.

ALTER TABLE research_economic_factors
    ADD COLUMN IF NOT EXISTS invalidation_conditions JSONB NOT NULL DEFAULT '[]'::JSONB,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'research_economic_factors_invalidation_check'
    ) THEN
        ALTER TABLE research_economic_factors
            ADD CONSTRAINT research_economic_factors_invalidation_check
            CHECK (JSONB_TYPEOF(invalidation_conditions) = 'array');
    END IF;
END $$;

-- A factor can revisit an earlier semantic state; only one current version is unique.
ALTER TABLE research_economic_factors
    DROP CONSTRAINT IF EXISTS research_economic_factors_identity_unique;

DO $$
BEGIN
    CREATE TRIGGER research_economic_factors_updated_at
        BEFORE UPDATE ON research_economic_factors
        FOR EACH ROW EXECUTE FUNCTION update_updated_at();
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;
