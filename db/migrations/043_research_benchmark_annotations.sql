-- Immutable human review history for deterministic benchmark scorecards.
-- Additive and idempotent. Rollback: drop the index and table.

CREATE TABLE IF NOT EXISTS research_benchmark_annotations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scorecard_id UUID NOT NULL
        REFERENCES research_benchmark_scorecards(id) ON DELETE CASCADE,
    annotation_version INTEGER NOT NULL,
    annotations JSONB NOT NULL,
    annotated_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_benchmark_annotations_version_check CHECK (
        annotation_version >= 1
    ),
    CONSTRAINT research_benchmark_annotations_payload_check CHECK (
        JSONB_TYPEOF(annotations) = 'object'
    ),
    CONSTRAINT research_benchmark_annotations_identity_unique UNIQUE (
        scorecard_id, annotation_version
    )
);

CREATE INDEX IF NOT EXISTS idx_research_benchmark_annotations_scorecard
    ON research_benchmark_annotations (scorecard_id, annotation_version DESC);

INSERT INTO research_benchmark_annotations (
    scorecard_id, annotation_version, annotations, annotated_by, created_at
)
SELECT id, annotation_version, human_annotations,
       COALESCE(NULLIF(annotated_by, ''), 'legacy_import'),
       COALESCE(annotated_at, updated_at, created_at)
FROM research_benchmark_scorecards
WHERE annotation_version > 0
ON CONFLICT (scorecard_id, annotation_version) DO NOTHING;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_proc WHERE proname = 'reject_research_immutable_mutation'
    ) THEN
        DROP TRIGGER IF EXISTS research_benchmark_annotations_immutable
            ON research_benchmark_annotations;
        CREATE TRIGGER research_benchmark_annotations_immutable
            BEFORE UPDATE OR DELETE ON research_benchmark_annotations
            FOR EACH ROW EXECUTE FUNCTION reject_research_immutable_mutation();
    END IF;
END $$;
