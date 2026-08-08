-- Phase 7 reusable, evidence-linked analytical claims.
-- Rollback: stop atom producers/readers, then drop these tables.
CREATE TABLE IF NOT EXISTS analysis_atoms (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    claim_type TEXT NOT NULL,
    claim TEXT NOT NULL,
    observation_text TEXT,
    interpretation_text TEXT,
    scenario_text TEXT,
    unknowns TEXT[] NOT NULL DEFAULT '{}',
    affected_assets JSONB NOT NULL DEFAULT '[]',
    time_horizon TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    confidence_components JSONB NOT NULL DEFAULT '{}',
    valid_from TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    carry_forward BOOLEAN NOT NULL DEFAULT FALSE,
    invalidation_conditions JSONB NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    supersedes_atom_id UUID REFERENCES analysis_atoms (id),
    source_event_id UUID REFERENCES market_events (id),
    prompt_version TEXT,
    model_slug TEXT,
    generation_attempt_id UUID,
    input_fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT analysis_atoms_confidence_bounds
        CHECK (confidence >= 0 AND confidence <= 1),
    CONSTRAINT analysis_atoms_status_allowed CHECK (
        status IN ('draft', 'validated', 'published', 'superseded', 'expired', 'retracted')
    )
);

CREATE INDEX IF NOT EXISTS idx_analysis_atoms_subject
    ON analysis_atoms (subject_type, subject_id, status);
CREATE INDEX IF NOT EXISTS idx_analysis_atoms_current
    ON analysis_atoms (status, valid_from DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_analysis_atoms_fingerprint
    ON analysis_atoms (input_fingerprint)
    WHERE status IN ('draft', 'validated', 'published');

CREATE TABLE IF NOT EXISTS analysis_atom_evidence (
    atom_id UUID NOT NULL REFERENCES analysis_atoms (id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    relationship TEXT NOT NULL,
    excerpt TEXT,
    source_timestamp TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (atom_id, evidence_type, evidence_id, relationship),
    CONSTRAINT analysis_atom_evidence_relationship_allowed CHECK (
        relationship IN ('supports', 'contradicts', 'context', 'invalidation')
    )
);

CREATE INDEX IF NOT EXISTS idx_analysis_atom_evidence_lookup
    ON analysis_atom_evidence (evidence_type, evidence_id);
