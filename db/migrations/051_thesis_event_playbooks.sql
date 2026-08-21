-- 051: Catalyst event playbooks: immutable, evidence-linked monitored event
-- scenarios for thesis catalysts.
--
-- Turns a promotion-eligible thesis candidate's catalyst into monitored
-- event content that the autonomy cycle can match against the normalized
-- market-event ledger (market_events, migration 027).  Playbooks are pure
-- monitoring content: they carry the catalyst, the monitored horizon, the
-- bounded MarketEventType vocabulary to watch, verbatim trigger /
-- confirmation / invalidation conditions, the three scenario legs, and the
-- exact cited evidence refs.  No recommendation, entry/exit, stop/target,
-- sizing, allocation, or execution field exists in either table, so no row
-- can become a trading instruction.
--
-- investment_thesis_event_playbooks is immutable versioned content.  Each
-- playbook_key (deterministic identity of thesis + catalyst + horizon) has
-- exactly one active version; changing the derived content supersedes the
-- active row through the one-time NULL -> non-NULL superseded_at transition
-- and inserts the next version, preserving point-in-time history.  The
-- lifecycle guard refuses DELETE and any UPDATE except that single
-- transition; content (including input_fingerprint, which covers all
-- content) is frozen.  event_types draws only from the
-- events.contracts.MarketEventType vocabulary, enforced by a CHECK; all
-- arrays/JSONB are bounded; scenario legs stay unknown (NULL) rather than
-- fabricated.
--
-- investment_thesis_event_matches is the append-only match ledger: one row
-- per (playbook, market_event, match_kind), with the playbook FK and the
-- market_event FK (market_events is itself an append-only ledger).  A
-- duplicate recording is an idempotent no-op; UPDATE/DELETE on ledger rows
-- is refused by a guard trigger.
--
-- Fully idempotent: every statement is guarded (IF NOT EXISTS / CREATE
-- OR REPLACE / DROP TRIGGER IF EXISTS / EXCEPTION duplicate_object) so the
-- file can be re-applied on fresh or upgraded databases.
-- Rollback: drop the guard triggers, then the tables in dependency order
-- (investment_thesis_event_matches first, as it references the playbooks
-- table), then the trigger functions.

-- ---------------------------------------------------------------------------
-- 1. Versioned event playbooks (immutable content; supersede to revise).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS investment_thesis_event_playbooks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thesis_id UUID NOT NULL REFERENCES investment_theses (id) ON DELETE CASCADE,
    -- Stable identity of thesis + catalyst + horizon; one active version.
    playbook_key TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    -- Thesis version at build time (point-in-time provenance, never 0).
    thesis_version INTEGER NOT NULL DEFAULT 1,
    catalyst TEXT NOT NULL,
    horizon TEXT NOT NULL,
    expected_at TIMESTAMPTZ,
    event_types TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    trigger_conditions JSONB NOT NULL DEFAULT '[]'::JSONB,
    confirmation_conditions JSONB NOT NULL DEFAULT '[]'::JSONB,
    invalidation_conditions JSONB NOT NULL DEFAULT '[]'::JSONB,
    bull_scenario JSONB,
    base_scenario JSONB,
    bear_scenario JSONB,
    cited_evidence_refs TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    input_fingerprint TEXT NOT NULL,
    superseded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_thesis_event_playbooks_version_check
        CHECK (version >= 1),
    CONSTRAINT investment_thesis_event_playbooks_thesis_version_check
        CHECK (thesis_version >= 1),
    CONSTRAINT investment_thesis_event_playbooks_catalyst_length_check
        CHECK (length(catalyst) BETWEEN 1 AND 2000),
    -- Bounded horizon vocabulary: the research-intelligence horizons used
    -- by tournament candidates plus the market-event horizon set.
    CONSTRAINT investment_thesis_event_playbooks_horizon_check
        CHECK (horizon IN (
            'intraday', 'days', 'weeks', 'months', 'multi_year', 'unknown',
            'swing', 'medium', 'long_term'
        )),
    -- Bounded, vocabulary-constrained event types: values must come from
    -- events.contracts.MarketEventType (18 values) and never exceed the
    -- full vocabulary size.
    CONSTRAINT investment_thesis_event_playbooks_event_types_bounded_check
        CHECK (cardinality(event_types) <= 18),
    CONSTRAINT investment_thesis_event_playbooks_event_types_vocabulary_check
        CHECK (event_types <@ ARRAY[
            'price_tick', 'price_bar_closed', 'option_chain_published',
            'corporate_action_published', 'volatility_state_changed',
            'correlation_state_changed', 'macro_release', 'macro_revision',
            'calendar_event_changed', 'headline_published', 'story_updated',
            'regulatory_filing_published', 'transcript_published',
            'filing_ingested', 'central_bank_communication',
            'positioning_report_published', 'source_freshness_changed',
            'manual_research_event'
        ]::TEXT[]),
    -- Condition arrays are bounded JSONB arrays of strings.
    CONSTRAINT investment_thesis_event_playbooks_trigger_conditions_check
        CHECK (
            JSONB_TYPEOF(trigger_conditions) = 'array'
            AND JSONB_ARRAY_LENGTH(trigger_conditions) <= 20
        ),
    CONSTRAINT investment_thesis_event_playbooks_confirmation_conditions_check
        CHECK (
            JSONB_TYPEOF(confirmation_conditions) = 'array'
            AND JSONB_ARRAY_LENGTH(confirmation_conditions) <= 20
        ),
    CONSTRAINT investment_thesis_event_playbooks_invalidation_conditions_check
        CHECK (
            JSONB_TYPEOF(invalidation_conditions) = 'array'
            AND JSONB_ARRAY_LENGTH(invalidation_conditions) <= 20
        ),
    -- Scenario legs are objects when present; unknown legs stay NULL.
    CONSTRAINT investment_thesis_event_playbooks_bull_scenario_check
        CHECK (bull_scenario IS NULL OR JSONB_TYPEOF(bull_scenario) = 'object'),
    CONSTRAINT investment_thesis_event_playbooks_base_scenario_check
        CHECK (base_scenario IS NULL OR JSONB_TYPEOF(base_scenario) = 'object'),
    CONSTRAINT investment_thesis_event_playbooks_bear_scenario_check
        CHECK (bear_scenario IS NULL OR JSONB_TYPEOF(bear_scenario) = 'object'),
    CONSTRAINT investment_thesis_event_playbooks_cited_evidence_bounded_check
        CHECK (cardinality(cited_evidence_refs) <= 30),
    -- Content-addressed fingerprint (SHA-256 hex of canonical content).
    CONSTRAINT investment_thesis_event_playbooks_input_fingerprint_check
        CHECK (input_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT investment_thesis_event_playbooks_superseded_after_created
        CHECK (superseded_at IS NULL OR superseded_at >= created_at),
    CONSTRAINT investment_thesis_event_playbooks_identity_unique
        UNIQUE (playbook_key, version)
);

-- Exactly one active row per playbook key.
CREATE UNIQUE INDEX IF NOT EXISTS idx_investment_thesis_event_playbooks_active
    ON investment_thesis_event_playbooks (playbook_key)
    WHERE superseded_at IS NULL;
-- Lookups by thesis (history) and by due date (scheduler polling).
CREATE INDEX IF NOT EXISTS idx_investment_thesis_event_playbooks_thesis
    ON investment_thesis_event_playbooks (thesis_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_investment_thesis_event_playbooks_due
    ON investment_thesis_event_playbooks (expected_at, created_at)
    WHERE superseded_at IS NULL AND expected_at IS NOT NULL;
-- Event-type matching over the bounded vocabulary.
CREATE INDEX IF NOT EXISTS idx_investment_thesis_event_playbooks_event_types
    ON investment_thesis_event_playbooks USING GIN (event_types)
    WHERE superseded_at IS NULL;

-- ---------------------------------------------------------------------------
-- 2. Append-only match ledger.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS investment_thesis_event_matches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    playbook_id UUID NOT NULL
        REFERENCES investment_thesis_event_playbooks (id) ON DELETE CASCADE,
    market_event_id UUID NOT NULL REFERENCES market_events (id) ON DELETE CASCADE,
    match_kind TEXT NOT NULL,
    evidence_refs TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    observed_at TIMESTAMPTZ NOT NULL,
    assessment JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT investment_thesis_event_matches_match_kind_check
        CHECK (match_kind IN ('trigger', 'confirmation', 'invalidation', 'context')),
    CONSTRAINT investment_thesis_event_matches_evidence_refs_bounded_check
        CHECK (cardinality(evidence_refs) <= 30),
    CONSTRAINT investment_thesis_event_matches_assessment_object_check
        CHECK (JSONB_TYPEOF(assessment) = 'object'),
    CONSTRAINT investment_thesis_event_matches_identity_unique
        UNIQUE (playbook_id, market_event_id, match_kind)
);

CREATE INDEX IF NOT EXISTS idx_investment_thesis_event_matches_playbook
    ON investment_thesis_event_matches (playbook_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_investment_thesis_event_matches_event
    ON investment_thesis_event_matches (market_event_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- 3. Immutability / lifecycle guards.
-- ---------------------------------------------------------------------------

-- Match ledger rows are strictly append-only: a new match is a new row.
CREATE OR REPLACE FUNCTION reject_event_match_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION '% rows are immutable; a new match is a new row', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    CREATE TRIGGER investment_thesis_event_matches_immutable
        BEFORE UPDATE OR DELETE ON investment_thesis_event_matches
        FOR EACH ROW EXECUTE FUNCTION reject_event_match_mutation();
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

CREATE OR REPLACE FUNCTION enforce_thesis_event_playbook_lifecycle()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'event playbooks are append-only';
    END IF;
    -- Identity and content are immutable: the only permitted UPDATE is the
    -- one-time supersede transition below, which touches nothing else.
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.thesis_id IS DISTINCT FROM OLD.thesis_id
       OR NEW.playbook_key IS DISTINCT FROM OLD.playbook_key
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.thesis_version IS DISTINCT FROM OLD.thesis_version
       OR NEW.catalyst IS DISTINCT FROM OLD.catalyst
       OR NEW.horizon IS DISTINCT FROM OLD.horizon
       OR NEW.expected_at IS DISTINCT FROM OLD.expected_at
       OR NEW.event_types IS DISTINCT FROM OLD.event_types
       OR NEW.trigger_conditions IS DISTINCT FROM OLD.trigger_conditions
       OR NEW.confirmation_conditions IS DISTINCT FROM OLD.confirmation_conditions
       OR NEW.invalidation_conditions IS DISTINCT FROM OLD.invalidation_conditions
       OR NEW.bull_scenario IS DISTINCT FROM OLD.bull_scenario
       OR NEW.base_scenario IS DISTINCT FROM OLD.base_scenario
       OR NEW.bear_scenario IS DISTINCT FROM OLD.bear_scenario
       OR NEW.cited_evidence_refs IS DISTINCT FROM OLD.cited_evidence_refs
       OR NEW.input_fingerprint IS DISTINCT FROM OLD.input_fingerprint
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'playbook content is immutable; supersede to revise';
    END IF;
    -- An UPDATE that does not supersede is a revision in place: reject.
    IF NEW.superseded_at IS NULL THEN
        RAISE EXCEPTION 'playbook rows are immutable; supersede to revise';
    END IF;
    -- The transition is one-time: superseded rows are frozen.
    IF OLD.superseded_at IS NOT NULL THEN
        RAISE EXCEPTION 'superseded playbooks are immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS investment_thesis_event_playbooks_lifecycle
    ON investment_thesis_event_playbooks;
CREATE TRIGGER investment_thesis_event_playbooks_lifecycle
    BEFORE UPDATE OR DELETE ON investment_thesis_event_playbooks
    FOR EACH ROW EXECUTE FUNCTION enforce_thesis_event_playbook_lifecycle();
