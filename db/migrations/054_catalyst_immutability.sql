-- Catalyst replay safety: every catalyst replay input is immutable after
-- insert.
--
-- investment_catalysts has no update API in the current product (rows are
-- inserted once by research/autonomy and only ever read), but nothing in
-- schema 038 prevented a later UPDATE.  A historical replay that scores a
-- thesis at a cutoff must never see a catalyst whose scoring content or
-- visibility changed after the cutoff, so this migration:
--
--   1. Stamps every pre-migration row with the migration time.  Whether a
--      legacy row was ever mutated is unknowable (nothing maintained
--      updated_at before this migration), so fail closed: a legacy row is
--      only valid for cutoffs at or after the migration ran.  Rows that
--      already carry an updated_at after created_at are stamped too, since
--      that timestamp may itself record a pre-migration mutation; the
--      stamp never moves a timestamp backward (GREATEST keeps an
--      already-later updated_at).  The stamp runs exactly once, guarded by
--      the trigger's existence, so re-applying the file is a no-op and
--      rows inserted after the migration keep updated_at = created_at.
--   2. Installs a BEFORE UPDATE OR DELETE trigger that rejects changes to
--      every replay input: the scoring/identity fields (thesis_id,
--      description, expected_at, state, created_at; id is immutable by
--      definition) and updated_at, which gates replay visibility.  A row's
--      visibility can therefore never be widened (moved backward toward an
--      earlier cutoff) or narrowed (moved forward) after insert.  An exact
--      no-op UPDATE (every column written back to its own value) passes
--      the IS DISTINCT FROM guard and changes nothing; it stays permitted
--      only because it cannot affect replay.
--
-- The evaluator (thesis_fusion.evaluate_thesis) filters catalysts by
-- created_at and updated_at both at or before the as-of cutoff; since
-- updated_at is frozen at insert (post-migration) or at the conservative
-- migration stamp (legacy rows), no post-insert change can move a catalyst
-- across a cutoff.
--
-- Fully idempotent and additive: no columns, constraints, or tables are
-- dropped; existing rows are preserved (only their replay validity is
-- narrowed).  Rollback: DROP TRIGGER investment_catalysts_immutable and
-- DROP FUNCTION enforce_investment_catalyst_immutability; nothing else
-- changed.

-- ---------------------------------------------------------------------------
-- 1. Conservative legacy stamp (once, before the trigger exists).
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'investment_catalysts_immutable'
          AND tgrelid = 'investment_catalysts'::regclass
    ) THEN
        -- Every legacy row is stamped, with no row filter: mutation
        -- history is unknowable for all of them.  GREATEST/COALESCE make
        -- the stamp monotonic — it never moves a timestamp backward.
        UPDATE investment_catalysts
           SET updated_at = GREATEST(COALESCE(updated_at, created_at), NOW());
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. Immutability guard for every replay input.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION enforce_investment_catalyst_immutability()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'catalysts are append-only';
    END IF;
    -- Every replay input is immutable after insert: the scoring/identity
    -- fields and the updated_at visibility gate alike.
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.thesis_id IS DISTINCT FROM OLD.thesis_id
       OR NEW.description IS DISTINCT FROM OLD.description
       OR NEW.expected_at IS DISTINCT FROM OLD.expected_at
       OR NEW.state IS DISTINCT FROM OLD.state
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.updated_at IS DISTINCT FROM OLD.updated_at THEN
        RAISE EXCEPTION 'catalyst replay inputs are immutable after insert';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS investment_catalysts_immutable ON investment_catalysts;
CREATE TRIGGER investment_catalysts_immutable
    BEFORE UPDATE OR DELETE ON investment_catalysts
    FOR EACH ROW EXECUTE FUNCTION enforce_investment_catalyst_immutability();
