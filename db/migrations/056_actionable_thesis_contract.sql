-- 056: Persist actionable thesis context and field-level citation maps.
--
-- Autonomous candidates now retain their quantified trend, valuation, and
-- measured-sentiment context plus the exact evidence refs supporting each
-- factual field.  The same fields are stored on immutable thesis versions so
-- history remains auditable when a candidate changes. Existing manual rows
-- remain valid with empty nullable context and an empty citation object.
--
-- Rollback: drop the four columns from investment_thesis_versions, then from
-- investment_theses (after removing the two citation-map constraints).

ALTER TABLE investment_theses
    ADD COLUMN IF NOT EXISTS trend_context TEXT,
    ADD COLUMN IF NOT EXISTS valuation_context TEXT,
    ADD COLUMN IF NOT EXISTS sentiment_context TEXT,
    ADD COLUMN IF NOT EXISTS citation_map JSONB NOT NULL DEFAULT '{}';

ALTER TABLE investment_thesis_versions
    ADD COLUMN IF NOT EXISTS trend_context TEXT,
    ADD COLUMN IF NOT EXISTS valuation_context TEXT,
    ADD COLUMN IF NOT EXISTS sentiment_context TEXT,
    ADD COLUMN IF NOT EXISTS citation_map JSONB NOT NULL DEFAULT '{}';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'investment_theses'::regclass
          AND conname = 'investment_theses_citation_map_object_check'
    ) THEN
        ALTER TABLE investment_theses
            ADD CONSTRAINT investment_theses_citation_map_object_check
            CHECK (jsonb_typeof(citation_map) = 'object');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'investment_thesis_versions'::regclass
          AND conname = 'investment_thesis_versions_citation_map_object_check'
    ) THEN
        ALTER TABLE investment_thesis_versions
            ADD CONSTRAINT investment_thesis_versions_citation_map_object_check
            CHECK (jsonb_typeof(citation_map) = 'object');
    END IF;
END $$;
