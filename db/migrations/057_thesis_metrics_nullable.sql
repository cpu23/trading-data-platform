-- 057: Unknown thesis metrics stay unknown; zero-cost reservations admitted.
--
-- The 049 desk scoring columns were born NOT NULL DEFAULT 0 so legacy
-- manual rows stayed valid.  That made "never evaluated" and "evaluated as
-- zero" indistinguishable, and the desk's own scoring treats an absent
-- input (no directional evidence, no catalyst set, no attention/crowding)
-- as *unknown*, never as a favorable zero.  This migration:
--
--   * drops NOT NULL and the DEFAULT 0 from the eight thesis metric columns
--     on investment_theses (evidence_strength, contradiction_strength,
--     neglect_score, catalyst_score, confidence_score, expected_value,
--     expected_shortfall, opportunity_score) and from the seven sub-metric
--     columns on investment_opportunity_snapshots.  The snapshot
--     opportunity_score stays NOT NULL: every evaluation run produces a
--     numeric gated score, so a frozen snapshot always carries one.
--   * backfills ONLY rows that have never been evaluated
--     (last_evaluated_at IS NULL) to NULL.  Every evaluated row
--     (last_evaluated_at IS NOT NULL) keeps its stored values exactly —
--     a legitimate evaluated zero is preserved byte-for-byte.
--   * relaxes the 045 budget_reservations estimate CHECK from strictly
--     positive to non-negative so a known-free model can reserve zero cost
--     under the daily cap while still carrying an auditable reservation row
--     that settles/releases like any other.
--
-- Fully idempotent: DROP NOT NULL / DROP DEFAULT and the guarded constraint
-- swap are no-ops on re-application, and the backfill only touches rows
-- whose metrics are still stored.  The SQL CHECKs on the score columns
-- (BETWEEN 0 AND 1) pass NULL under standard SQL semantics, so no
-- constraint needs replacing.
--
-- Rollback: restore the zero defaults and NOT NULL (first deciding how the
-- now-NULL unevaluated rows should be represented), and re-add the
-- positive-estimate CHECK on budget_reservations.

-- ---------------------------------------------------------------------------
-- 1. Thesis metric columns: unknown is representable, never defaulted.
-- ---------------------------------------------------------------------------

ALTER TABLE investment_theses
    ALTER COLUMN evidence_strength DROP NOT NULL,
    ALTER COLUMN contradiction_strength DROP NOT NULL,
    ALTER COLUMN neglect_score DROP NOT NULL,
    ALTER COLUMN catalyst_score DROP NOT NULL,
    ALTER COLUMN confidence_score DROP NOT NULL,
    ALTER COLUMN expected_value DROP NOT NULL,
    ALTER COLUMN expected_shortfall DROP NOT NULL,
    ALTER COLUMN opportunity_score DROP NOT NULL;

ALTER TABLE investment_theses
    ALTER COLUMN evidence_strength DROP DEFAULT,
    ALTER COLUMN contradiction_strength DROP DEFAULT,
    ALTER COLUMN neglect_score DROP DEFAULT,
    ALTER COLUMN catalyst_score DROP DEFAULT,
    ALTER COLUMN confidence_score DROP DEFAULT,
    ALTER COLUMN expected_value DROP DEFAULT,
    ALTER COLUMN expected_shortfall DROP DEFAULT,
    ALTER COLUMN opportunity_score DROP DEFAULT;

-- Never-evaluated rows carry only the old neutral defaults, not measured
-- scores: represent them as unknown.  Rows with a last_evaluated_at are
-- measured and are never rewritten, so evaluated zeros survive intact.
UPDATE investment_theses
   SET evidence_strength = NULL,
       contradiction_strength = NULL,
       neglect_score = NULL,
       catalyst_score = NULL,
       confidence_score = NULL,
       expected_value = NULL,
       expected_shortfall = NULL,
       opportunity_score = NULL
 WHERE last_evaluated_at IS NULL
   AND (evidence_strength IS NOT NULL
        OR contradiction_strength IS NOT NULL
        OR neglect_score IS NOT NULL
        OR catalyst_score IS NOT NULL
        OR confidence_score IS NOT NULL
        OR expected_value IS NOT NULL
        OR expected_shortfall IS NOT NULL
        OR opportunity_score IS NOT NULL);

-- ---------------------------------------------------------------------------
-- 2. Frozen opportunity snapshots: carry the same unknowns as NULL instead
--    of coercing them to favorable zeros.  opportunity_score stays NOT
--    NULL because every evaluation produces a numeric gated score.
-- ---------------------------------------------------------------------------

ALTER TABLE investment_opportunity_snapshots
    ALTER COLUMN expected_value DROP NOT NULL,
    ALTER COLUMN expected_shortfall DROP NOT NULL,
    ALTER COLUMN confidence_score DROP NOT NULL,
    ALTER COLUMN neglect_score DROP NOT NULL,
    ALTER COLUMN catalyst_score DROP NOT NULL,
    ALTER COLUMN evidence_strength DROP NOT NULL,
    ALTER COLUMN contradiction_strength DROP NOT NULL;

ALTER TABLE investment_opportunity_snapshots
    ALTER COLUMN expected_value DROP DEFAULT,
    ALTER COLUMN expected_shortfall DROP DEFAULT,
    ALTER COLUMN confidence_score DROP DEFAULT,
    ALTER COLUMN neglect_score DROP DEFAULT,
    ALTER COLUMN catalyst_score DROP DEFAULT,
    ALTER COLUMN evidence_strength DROP DEFAULT,
    ALTER COLUMN contradiction_strength DROP DEFAULT;

-- ---------------------------------------------------------------------------
-- 3. Budget reservations: a known-free model reserves zero cost.
-- ---------------------------------------------------------------------------

ALTER TABLE budget_reservations
    DROP CONSTRAINT IF EXISTS budget_reservations_estimate_positive;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'budget_reservations'::regclass
          AND conname = 'budget_reservations_estimate_nonnegative'
    ) THEN
        ALTER TABLE budget_reservations
            ADD CONSTRAINT budget_reservations_estimate_nonnegative
            CHECK (estimated_usd >= 0);
    END IF;
END $$;
