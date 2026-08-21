-- Autonomous thesis-fusion desk: additive foundation.
--
-- Extends the canonical investment_theses record with autonomy/scoring
-- columns and investment_thesis_evidence with provenance/weight columns,
-- then creates the shared desk tables: thesis groups, versioned group
-- membership, versioned scenarios, versioned forecasts with stable
-- point-in-time identity, forecast outcomes, opportunity snapshots,
-- falsification runs, and position-thesis links.
--
-- Invalidation is a first-class evidence relationship: the canonical
-- relationship CHECK is swapped (under its original name, with the original
-- composite primary key untouched) to admit 'invalidation'.  Scenario
-- probability is nullable so unknown legs stay distinct from conviction
-- (they are never defaulted); each scenario stores a bounded, finite
-- expected_return with the domain's +/-100 magnitude cap.
--
-- Existing manual rows stay valid under neutral defaults: origin 'manual',
-- direction 'neutral', zero scores, and full effective_weight for legacy
-- manual evidence (desk evidence weights are computed by the scoring
-- module). Evidence is deduped by independence_key; NULL keys are exempt
-- so manual rows and pre-desk inserts are unaffected.  Forecast rows are
-- immutable except the one-time NULL -> non-NULL superseded_at transition
-- used to version a forecast; afterwards the row is frozen.
--
-- Fully idempotent: every statement is guarded (IF NOT EXISTS, DROP
-- TRIGGER IF EXISTS, CREATE OR REPLACE, EXCEPTION duplicate_object) so the
-- file can be re-applied on fresh or upgraded databases.
-- Rollback: drop the new tables in reverse dependency order, then drop the
-- added columns and the trigger functions.

-- ---------------------------------------------------------------------------
-- 1. Thesis groups (created first so investment_theses.group_id can
--    reference them).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS investment_thesis_groups (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_thesis_groups_status_check
        CHECK (status IN ('active', 'archived'))
);

DO $$
BEGIN
    CREATE TRIGGER investment_thesis_groups_updated_at
        BEFORE UPDATE ON investment_thesis_groups
        FOR EACH ROW EXECUTE FUNCTION update_updated_at();
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- ---------------------------------------------------------------------------
-- 2. Additive autonomy/scoring columns on the canonical thesis record.
--    All defaults are neutral so existing manual theses remain valid.
-- ---------------------------------------------------------------------------

ALTER TABLE investment_theses
    ADD COLUMN IF NOT EXISTS group_id UUID
        REFERENCES investment_thesis_groups (id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'manual'
        CHECK (origin IN ('manual', 'generated', 'fusion')),
    ADD COLUMN IF NOT EXISTS canonical_key TEXT,
    ADD COLUMN IF NOT EXISTS mechanism TEXT,
    ADD COLUMN IF NOT EXISTS direction TEXT NOT NULL DEFAULT 'neutral'
        CHECK (direction IN ('long', 'short', 'neutral')),
    ADD COLUMN IF NOT EXISTS catalyst_summary TEXT,
    ADD COLUMN IF NOT EXISTS evidence_strength DOUBLE PRECISION
        NOT NULL DEFAULT 0 CHECK (evidence_strength BETWEEN 0 AND 1),
    ADD COLUMN IF NOT EXISTS contradiction_strength DOUBLE PRECISION
        NOT NULL DEFAULT 0 CHECK (contradiction_strength BETWEEN 0 AND 1),
    ADD COLUMN IF NOT EXISTS neglect_score DOUBLE PRECISION
        NOT NULL DEFAULT 0 CHECK (neglect_score BETWEEN 0 AND 1),
    ADD COLUMN IF NOT EXISTS catalyst_score DOUBLE PRECISION
        NOT NULL DEFAULT 0 CHECK (catalyst_score BETWEEN 0 AND 1),
    ADD COLUMN IF NOT EXISTS confidence_score DOUBLE PRECISION
        NOT NULL DEFAULT 0 CHECK (confidence_score BETWEEN 0 AND 1),
    ADD COLUMN IF NOT EXISTS expected_value DOUBLE PRECISION
        NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS expected_shortfall DOUBLE PRECISION
        NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS opportunity_score DOUBLE PRECISION
        NOT NULL DEFAULT 0 CHECK (opportunity_score BETWEEN 0 AND 1),
    ADD COLUMN IF NOT EXISTS last_evaluated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_evidence_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS input_fingerprint TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_investment_theses_canonical_key
    ON investment_theses (canonical_key)
    WHERE canonical_key IS NOT NULL;
-- Evaluation dedup: the fingerprint is content-addressed over the thesis
-- identity and its evaluation inputs, so it is globally unique; identical
-- inputs re-evaluated are no-ops.
CREATE UNIQUE INDEX IF NOT EXISTS idx_investment_theses_input_fingerprint
    ON investment_theses (input_fingerprint)
    WHERE input_fingerprint IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_investment_theses_group
    ON investment_theses (group_id)
    WHERE group_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_investment_theses_last_evaluated
    ON investment_theses (last_evaluated_at DESC)
    WHERE last_evaluated_at IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 3. Provenance/weight columns on evidence. The existing primary key
--    (thesis_id, evidence_type, evidence_id, relationship) is preserved.
-- ---------------------------------------------------------------------------

ALTER TABLE investment_thesis_evidence
    ADD COLUMN IF NOT EXISTS source_family TEXT NOT NULL DEFAULT 'manual',
    ADD COLUMN IF NOT EXISTS origin_key TEXT,
    ADD COLUMN IF NOT EXISTS independence_key TEXT,
    ADD COLUMN IF NOT EXISTS evidence_fingerprint TEXT,
    ADD COLUMN IF NOT EXISTS source_timestamp TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS available_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS quality_score DOUBLE PRECISION
        NOT NULL DEFAULT 0 CHECK (quality_score BETWEEN 0 AND 1),
    ADD COLUMN IF NOT EXISTS entailment_score DOUBLE PRECISION
        NOT NULL DEFAULT 0 CHECK (entailment_score BETWEEN 0 AND 1),
    ADD COLUMN IF NOT EXISTS freshness_score DOUBLE PRECISION
        NOT NULL DEFAULT 0 CHECK (freshness_score BETWEEN 0 AND 1),
    ADD COLUMN IF NOT EXISTS effective_weight DOUBLE PRECISION
        NOT NULL DEFAULT 1 CHECK (effective_weight BETWEEN 0 AND 1);

-- Invalidation is a first-class evidence relationship (desk evidence and
-- falsification both record it).  The canonical 038 check is swapped for an
-- expanded one under the same name, 011-style: the new constraint is added
-- NOT VALID and validated while the original still guards writes, then the
-- original is dropped and the new one renamed into the canonical name.  The
-- whole swap is guarded so re-applying the file is a no-op.  The composite
-- primary key (thesis_id, evidence_type, evidence_id, relationship) is
-- preserved.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'investment_thesis_evidence'::regclass
          AND conname = 'investment_thesis_evidence_relationship_check'
          AND pg_get_constraintdef(oid) LIKE '%invalidation%'
    ) THEN
        ALTER TABLE investment_thesis_evidence
            ADD CONSTRAINT investment_thesis_evidence_relationship_check_v2
            CHECK (relationship IN
                ('supports', 'contradicts', 'context', 'invalidation'))
            NOT VALID;
        ALTER TABLE investment_thesis_evidence
            VALIDATE CONSTRAINT investment_thesis_evidence_relationship_check_v2;
        ALTER TABLE investment_thesis_evidence
            DROP CONSTRAINT IF EXISTS
                investment_thesis_evidence_relationship_check;
        ALTER TABLE investment_thesis_evidence
            RENAME CONSTRAINT investment_thesis_evidence_relationship_check_v2
            TO investment_thesis_evidence_relationship_check;
    END IF;
END $$;

-- Evidence is deduped/capped by independence_key: at most one row per
-- independent source per thesis. Manual rows (NULL key) are exempt.
CREATE UNIQUE INDEX IF NOT EXISTS idx_investment_thesis_evidence_independence
    ON investment_thesis_evidence (thesis_id, independence_key)
    WHERE independence_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_investment_thesis_evidence_origin
    ON investment_thesis_evidence (origin_key)
    WHERE origin_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_investment_thesis_evidence_fingerprint
    ON investment_thesis_evidence (evidence_fingerprint)
    WHERE evidence_fingerprint IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 4. Versioned group membership. Rows are immutable; membership ends by
--    setting removed_at instead of deleting. At most one active row per
--    (group_id, thesis_id); history accumulates as removed rows.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS investment_thesis_group_members (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    group_id UUID NOT NULL REFERENCES investment_thesis_groups (id) ON DELETE CASCADE,
    thesis_id UUID NOT NULL REFERENCES investment_theses (id) ON DELETE CASCADE,
    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    removed_at TIMESTAMPTZ,
    note TEXT,
    CONSTRAINT investment_thesis_group_members_removed_after_added
        CHECK (removed_at IS NULL OR removed_at >= added_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_investment_thesis_group_members_active
    ON investment_thesis_group_members (group_id, thesis_id)
    WHERE removed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_investment_thesis_group_members_thesis
    ON investment_thesis_group_members (thesis_id)
    WHERE removed_at IS NULL;

-- ---------------------------------------------------------------------------
-- 5. Versioned scenarios. Each (thesis_id, name) has an active version and
--    a superseded history; probability revisions insert a new version.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS investment_thesis_scenarios (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thesis_id UUID NOT NULL REFERENCES investment_theses (id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    probability DOUBLE PRECISION
        CHECK (probability BETWEEN 0 AND 1),
    expected_return DOUBLE PRECISION NOT NULL DEFAULT 0
        CHECK (expected_return BETWEEN -100 AND 100),
    is_base_case BOOLEAN NOT NULL DEFAULT FALSE,
    version INTEGER NOT NULL DEFAULT 1,
    superseded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_thesis_scenarios_version_check
        CHECK (version >= 1),
    CONSTRAINT investment_thesis_scenarios_superseded_after_created
        CHECK (superseded_at IS NULL OR superseded_at >= created_at),
    CONSTRAINT investment_thesis_scenarios_identity_unique
        UNIQUE (thesis_id, name, version)
);

-- Upgrade path for schemas that already ran an earlier draft of this
-- migration: unknown probability is representable (NULL) and the bounded
-- finite expected_return column is stored.  The CHECK passes NULL under SQL
-- semantics, so relaxing NOT NULL is the only probability change needed;
-- BETWEEN -100 AND 100 rejects NaN/Infinity as well as out-of-range
-- returns, matching the domain's MAX_ABS_RETURN = 100 cap.  Both statements
-- are no-ops on the fresh schema above, so re-applying stays idempotent.
ALTER TABLE investment_thesis_scenarios
    ALTER COLUMN probability DROP NOT NULL;
ALTER TABLE investment_thesis_scenarios
    ADD COLUMN IF NOT EXISTS expected_return DOUBLE PRECISION
        NOT NULL DEFAULT 0
        CHECK (expected_return BETWEEN -100 AND 100);

CREATE UNIQUE INDEX IF NOT EXISTS idx_investment_thesis_scenarios_active
    ON investment_thesis_scenarios (thesis_id, name)
    WHERE superseded_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_investment_thesis_scenarios_base_case
    ON investment_thesis_scenarios (thesis_id)
    WHERE is_base_case AND superseded_at IS NULL;

-- ---------------------------------------------------------------------------
-- 6. Versioned forecasts with stable point-in-time identity. Each
--    forecast_key has one active version; superseding marks the old
--    version superseded_at and inserts the new version.  The ONLY allowed
--    UPDATE is the one-time NULL -> non-NULL superseded_at transition;
--    identity and content stay frozen, and superseded rows are immutable.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS investment_thesis_forecasts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thesis_id UUID NOT NULL REFERENCES investment_theses (id) ON DELETE CASCADE,
    scenario_id UUID REFERENCES investment_thesis_scenarios (id) ON DELETE SET NULL,
    forecast_key TEXT NOT NULL,
    forecast_type TEXT NOT NULL DEFAULT 'price',
    direction TEXT NOT NULL DEFAULT 'up',
    target_value DOUBLE PRECISION,
    target_date DATE,
    as_of TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    version INTEGER NOT NULL DEFAULT 1,
    superseded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_thesis_forecasts_version_check
        CHECK (version >= 1),
    CONSTRAINT investment_thesis_forecasts_direction_check
        CHECK (direction IN ('up', 'down', 'flat')),
    CONSTRAINT investment_thesis_forecasts_type_check
        CHECK (forecast_type IN ('price', 'earnings', 'revenue', 'relative', 'other')),
    CONSTRAINT investment_thesis_forecasts_superseded_after_created
        CHECK (superseded_at IS NULL OR superseded_at >= created_at),
    CONSTRAINT investment_thesis_forecasts_identity_unique
        UNIQUE (forecast_key, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_investment_thesis_forecasts_active
    ON investment_thesis_forecasts (forecast_key)
    WHERE superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_investment_thesis_forecasts_thesis
    ON investment_thesis_forecasts (thesis_id, as_of DESC);

-- ---------------------------------------------------------------------------
-- 7. Forecast outcomes: one terminal outcome per forecast version,
--    recorded at measurement time. Append-only.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS investment_forecast_outcomes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    forecast_id UUID NOT NULL REFERENCES investment_thesis_forecasts (id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    actual_value DOUBLE PRECISION,
    measured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_forecast_outcomes_status_check
        CHECK (status IN ('hit', 'miss', 'inconclusive')),
    CONSTRAINT investment_forecast_outcomes_forecast_unique
        UNIQUE (forecast_id)
);

-- ---------------------------------------------------------------------------
-- 8. Opportunity snapshots: frozen scoring state per evaluation run,
--    keyed by (thesis_id, snapshot_key). Append-only.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS investment_opportunity_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thesis_id UUID NOT NULL REFERENCES investment_theses (id) ON DELETE CASCADE,
    snapshot_key TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    input_fingerprint TEXT,
    opportunity_score DOUBLE PRECISION NOT NULL
        CHECK (opportunity_score BETWEEN 0 AND 1),
    expected_value DOUBLE PRECISION NOT NULL DEFAULT 0,
    expected_shortfall DOUBLE PRECISION NOT NULL DEFAULT 0,
    confidence_score DOUBLE PRECISION NOT NULL DEFAULT 0
        CHECK (confidence_score BETWEEN 0 AND 1),
    neglect_score DOUBLE PRECISION NOT NULL DEFAULT 0
        CHECK (neglect_score BETWEEN 0 AND 1),
    catalyst_score DOUBLE PRECISION NOT NULL DEFAULT 0
        CHECK (catalyst_score BETWEEN 0 AND 1),
    evidence_strength DOUBLE PRECISION NOT NULL DEFAULT 0
        CHECK (evidence_strength BETWEEN 0 AND 1),
    contradiction_strength DOUBLE PRECISION NOT NULL DEFAULT 0
        CHECK (contradiction_strength BETWEEN 0 AND 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_opportunity_snapshots_identity_unique
        UNIQUE (thesis_id, snapshot_key)
);

CREATE INDEX IF NOT EXISTS idx_investment_opportunity_snapshots_thesis
    ON investment_opportunity_snapshots (thesis_id, captured_at DESC);

-- ---------------------------------------------------------------------------
-- 9. Falsification runs: one run per (thesis_id, run_key); status moves
--    pending/in_progress -> terminal and is then frozen.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS investment_thesis_falsification_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thesis_id UUID NOT NULL REFERENCES investment_theses (id) ON DELETE CASCADE,
    run_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    findings JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_thesis_falsification_runs_status_check
        CHECK (status IN (
            'pending', 'in_progress', 'not_falsified', 'falsified', 'inconclusive'
        )),
    CONSTRAINT investment_thesis_falsification_runs_completed_after_started
        CHECK (completed_at IS NULL OR completed_at >= started_at),
    CONSTRAINT investment_thesis_falsification_runs_identity_unique
        UNIQUE (thesis_id, run_key)
);

CREATE INDEX IF NOT EXISTS idx_investment_thesis_falsification_runs_thesis
    ON investment_thesis_falsification_runs (thesis_id, started_at DESC);

-- ---------------------------------------------------------------------------
-- 10. Position-thesis links (positions are portfolio_holdings rows).
--     Versioned audit trail: linking inserts a row; unlinking sets
--     removed_at. At most one active link per (position, thesis, type).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS position_thesis_links (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    position_id UUID NOT NULL REFERENCES portfolio_holdings (id) ON DELETE CASCADE,
    thesis_id UUID NOT NULL REFERENCES investment_theses (id) ON DELETE CASCADE,
    link_type TEXT NOT NULL DEFAULT 'primary',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    removed_at TIMESTAMPTZ,
    CONSTRAINT position_thesis_links_link_type_check
        CHECK (link_type IN ('primary', 'secondary', 'hedge', 'watch')),
    CONSTRAINT position_thesis_links_removed_after_created
        CHECK (removed_at IS NULL OR removed_at >= created_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_position_thesis_links_active
    ON position_thesis_links (position_id, thesis_id, link_type)
    WHERE removed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_position_thesis_links_thesis
    ON position_thesis_links (thesis_id)
    WHERE removed_at IS NULL;

-- ---------------------------------------------------------------------------
-- 11. Append-only / lifecycle triggers.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION reject_thesis_immutable_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION '% is immutable', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS investment_thesis_forecasts_immutable ON investment_thesis_forecasts;

CREATE OR REPLACE FUNCTION enforce_thesis_forecast_lifecycle()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'forecasts are append-only';
    END IF;
    -- Identity and content are immutable: the only permitted UPDATE is the
    -- one-time supersede transition below, which touches nothing else.
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.thesis_id IS DISTINCT FROM OLD.thesis_id
       OR NEW.scenario_id IS DISTINCT FROM OLD.scenario_id
       OR NEW.forecast_key IS DISTINCT FROM OLD.forecast_key
       OR NEW.forecast_type IS DISTINCT FROM OLD.forecast_type
       OR NEW.direction IS DISTINCT FROM OLD.direction
       OR NEW.target_value IS DISTINCT FROM OLD.target_value
       OR NEW.target_date IS DISTINCT FROM OLD.target_date
       OR NEW.as_of IS DISTINCT FROM OLD.as_of
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'forecast content is immutable; supersede to revise';
    END IF;
    -- An UPDATE that does not supersede is a revision in place: reject.
    IF NEW.superseded_at IS NULL THEN
        RAISE EXCEPTION 'forecast rows are immutable; supersede to revise';
    END IF;
    -- The transition is one-time: superseded rows are frozen.
    IF OLD.superseded_at IS NOT NULL THEN
        RAISE EXCEPTION 'superseded forecasts are immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS investment_thesis_forecasts_lifecycle ON investment_thesis_forecasts;
CREATE TRIGGER investment_thesis_forecasts_lifecycle
    BEFORE UPDATE OR DELETE ON investment_thesis_forecasts
    FOR EACH ROW EXECUTE FUNCTION enforce_thesis_forecast_lifecycle();

DROP TRIGGER IF EXISTS investment_forecast_outcomes_immutable ON investment_forecast_outcomes;
CREATE TRIGGER investment_forecast_outcomes_immutable
    BEFORE UPDATE OR DELETE ON investment_forecast_outcomes
    FOR EACH ROW EXECUTE FUNCTION reject_thesis_immutable_mutation();

DROP TRIGGER IF EXISTS investment_opportunity_snapshots_immutable ON investment_opportunity_snapshots;
CREATE TRIGGER investment_opportunity_snapshots_immutable
    BEFORE UPDATE OR DELETE ON investment_opportunity_snapshots
    FOR EACH ROW EXECUTE FUNCTION reject_thesis_immutable_mutation();

DROP TRIGGER IF EXISTS position_thesis_links_immutable ON position_thesis_links;

CREATE OR REPLACE FUNCTION enforce_thesis_position_link_append_only()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'position links are append-only; set removed_at to unlink';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.position_id IS DISTINCT FROM OLD.position_id
       OR NEW.thesis_id IS DISTINCT FROM OLD.thesis_id
       OR NEW.link_type IS DISTINCT FROM OLD.link_type
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'position link identity columns are immutable';
    END IF;
    IF OLD.removed_at IS NOT NULL
       AND NEW.removed_at IS DISTINCT FROM OLD.removed_at THEN
        RAISE EXCEPTION 'unlinked position links are immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS position_thesis_links_append_only ON position_thesis_links;
CREATE TRIGGER position_thesis_links_append_only
    BEFORE UPDATE OR DELETE ON position_thesis_links
    FOR EACH ROW EXECUTE FUNCTION enforce_thesis_position_link_append_only();

CREATE OR REPLACE FUNCTION enforce_thesis_group_membership_append_only()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'group memberships are append-only; set removed_at to end membership';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.group_id IS DISTINCT FROM OLD.group_id
       OR NEW.thesis_id IS DISTINCT FROM OLD.thesis_id
       OR NEW.added_at IS DISTINCT FROM OLD.added_at THEN
        RAISE EXCEPTION 'group membership identity columns are immutable';
    END IF;
    IF OLD.removed_at IS NOT NULL
       AND NEW.removed_at IS DISTINCT FROM OLD.removed_at THEN
        RAISE EXCEPTION 'removed memberships are immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS investment_thesis_group_members_append_only ON investment_thesis_group_members;
CREATE TRIGGER investment_thesis_group_members_append_only
    BEFORE UPDATE OR DELETE ON investment_thesis_group_members
    FOR EACH ROW EXECUTE FUNCTION enforce_thesis_group_membership_append_only();

CREATE OR REPLACE FUNCTION enforce_thesis_falsification_run_lifecycle()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'falsification runs are append-only';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.thesis_id IS DISTINCT FROM OLD.thesis_id
       OR NEW.run_key IS DISTINCT FROM OLD.run_key
       OR NEW.started_at IS DISTINCT FROM OLD.started_at THEN
        RAISE EXCEPTION 'falsification run identity columns are immutable';
    END IF;
    IF OLD.status NOT IN ('pending', 'in_progress') THEN
        RAISE EXCEPTION 'falsification run status is final';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS investment_thesis_falsification_runs_lifecycle ON investment_thesis_falsification_runs;
CREATE TRIGGER investment_thesis_falsification_runs_lifecycle
    BEFORE UPDATE OR DELETE ON investment_thesis_falsification_runs
    FOR EACH ROW EXECUTE FUNCTION enforce_thesis_falsification_run_lifecycle();
