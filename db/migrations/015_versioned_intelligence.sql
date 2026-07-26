ALTER TABLE structured_opinions
    ADD COLUMN IF NOT EXISTS correlation_id UUID REFERENCES cycle_runs(correlation_id),
    ADD COLUMN IF NOT EXISTS schema_version TEXT NOT NULL DEFAULT '1',
    ADD COLUMN IF NOT EXISTS lifecycle_status TEXT NOT NULL DEFAULT 'published',
    ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS baseline_opinion_id UUID REFERENCES structured_opinions(opinion_id),
    ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}';

UPDATE structured_opinions
SET published_at = COALESCE(published_at, created_at)
WHERE lifecycle_status = 'published';

ALTER TABLE structured_opinions
    DROP CONSTRAINT IF EXISTS structured_opinions_lifecycle_status_check;
ALTER TABLE structured_opinions
    ADD CONSTRAINT structured_opinions_lifecycle_status_check
    CHECK (lifecycle_status IN ('draft', 'validated', 'published', 'quarantined'));

CREATE INDEX IF NOT EXISTS idx_structured_opinions_published
    ON structured_opinions (opinion_type, scope, published_at DESC)
    WHERE lifecycle_status = 'published';

CREATE UNIQUE INDEX IF NOT EXISTS uq_opinion_per_cycle_type_scope
    ON structured_opinions (correlation_id, opinion_type, scope)
    WHERE correlation_id IS NOT NULL;

ALTER TABLE processing_log
    ADD COLUMN IF NOT EXISTS output_ids UUID[],
    ADD COLUMN IF NOT EXISTS request_metadata JSONB;

ALTER TABLE daily_briefings
    ADD COLUMN IF NOT EXISTS correlation_id UUID REFERENCES cycle_runs(correlation_id),
    ADD COLUMN IF NOT EXISTS lifecycle_status TEXT NOT NULL DEFAULT 'published',
    ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;

UPDATE daily_briefings
SET published_at = COALESCE(published_at, created_at)
WHERE lifecycle_status = 'published';

ALTER TABLE daily_briefings
    DROP CONSTRAINT IF EXISTS daily_briefings_briefing_date_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_daily_briefing_cycle
    ON daily_briefings (briefing_date, correlation_id);

ALTER TABLE cycle_runs
    ADD COLUMN IF NOT EXISTS publication_status TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS baseline_correlation_id UUID REFERENCES cycle_runs(correlation_id),
    ADD COLUMN IF NOT EXISTS config_fingerprint TEXT;

ALTER TABLE cycle_runs
    DROP CONSTRAINT IF EXISTS cycle_runs_publication_status_check;
ALTER TABLE cycle_runs
    ADD CONSTRAINT cycle_runs_publication_status_check
    CHECK (publication_status IN ('pending', 'published', 'failed'));

UPDATE cycle_runs
SET publication_status = CASE
    WHEN result_status IN ('success', 'partial') THEN 'published'
    WHEN status = 'failed' THEN 'failed'
    ELSE publication_status
END,
published_at = CASE
    WHEN result_status IN ('success', 'partial') THEN COALESCE(completed_at, started_at)
    ELSE published_at
END;

CREATE OR REPLACE FUNCTION prune_old_llm_payloads(retention_days INTEGER DEFAULT 90)
RETURNS INTEGER AS $$
DECLARE
    affected INTEGER;
BEGIN
    UPDATE processing_log
    SET prompt_text = NULL, raw_response = NULL
    WHERE completed_at < NOW() - make_interval(days => retention_days)
      AND (prompt_text IS NOT NULL OR raw_response IS NOT NULL);
    GET DIAGNOSTICS affected = ROW_COUNT;
    RETURN affected;
END;
$$ LANGUAGE plpgsql;
