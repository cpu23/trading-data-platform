-- Phase 5 deterministic, revision-aware macro release cards.
--
-- macro_release_cards is append-only history. A revised release is a new
-- immutable row linked to the row it supersedes; the current table is the only
-- mutable pointer and makes current reads cheap without rewriting history.
-- Rollback: stop card producers/readers, drop macro_release_cards_current,
-- then macro_release_cards and its indexes.

CREATE TABLE IF NOT EXISTS macro_release_cards (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    release_identity TEXT NOT NULL,
    series_id TEXT,
    revision_number INTEGER NOT NULL DEFAULT 0,
    source_event_id UUID NOT NULL REFERENCES market_events(id),
    revision_of_event_id UUID REFERENCES market_events(id),
    supersedes_card_id UUID REFERENCES macro_release_cards(id),
    event_name TEXT NOT NULL,
    actual DOUBLE PRECISION,
    consensus DOUBLE PRECISION,
    previous DOUBLE PRECISION,
    revised_previous DOUBLE PRECISION,
    absolute_surprise DOUBLE PRECISION,
    standardized_surprise DOUBLE PRECISION,
    impact TEXT NOT NULL DEFAULT 'unknown',
    source TEXT NOT NULL,
    observed_at TIMESTAMPTZ,
    released_at TIMESTAMPTZ,
    revision_at TIMESTAMPTZ,
    quality_flags JSONB NOT NULL DEFAULT '[]'::JSONB,
    stage TEXT NOT NULL DEFAULT 't0',
    reaction_summary JSONB NOT NULL DEFAULT '{}'::JSONB,
    source_event_provenance JSONB NOT NULL DEFAULT '{}'::JSONB,
    source_payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT macro_release_cards_identity_nonblank_check
        CHECK (BTRIM(release_identity) <> ''),
    CONSTRAINT macro_release_cards_event_name_nonblank_check
        CHECK (BTRIM(event_name) <> ''),
    CONSTRAINT macro_release_cards_source_nonblank_check
        CHECK (BTRIM(source) <> ''),
    CONSTRAINT macro_release_cards_revision_nonnegative_check
        CHECK (revision_number >= 0),
    CONSTRAINT macro_release_cards_stage_check
        CHECK (stage IN ('t0', 'developing', 'reaction', 'final')),
    CONSTRAINT macro_release_cards_quality_flags_array_check
        CHECK (JSONB_TYPEOF(quality_flags) = 'array'),
    CONSTRAINT macro_release_cards_reaction_summary_object_check
        CHECK (JSONB_TYPEOF(reaction_summary) = 'object'),
    CONSTRAINT macro_release_cards_provenance_object_check
        CHECK (JSONB_TYPEOF(source_event_provenance) = 'object'),
    CONSTRAINT macro_release_cards_source_payload_object_check
        CHECK (JSONB_TYPEOF(source_payload) = 'object'),
    CONSTRAINT macro_release_cards_revision_event_check
        CHECK (revision_number = 0 OR revision_of_event_id IS NOT NULL),
    CONSTRAINT macro_release_cards_event_identity_unique
        UNIQUE (release_identity, source_event_id)
);

CREATE INDEX IF NOT EXISTS idx_macro_release_cards_identity_history
    ON macro_release_cards (release_identity, revision_number DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_macro_release_cards_series_history
    ON macro_release_cards (series_id, observed_at DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_macro_release_cards_source_event
    ON macro_release_cards (source_event_id);
CREATE INDEX IF NOT EXISTS idx_macro_release_cards_observed_at
    ON macro_release_cards (observed_at DESC);

CREATE TABLE IF NOT EXISTS macro_release_cards_current (
    release_identity TEXT PRIMARY KEY,
    card_id UUID NOT NULL REFERENCES macro_release_cards(id),
    stage TEXT NOT NULL DEFAULT 't0',
    reaction_summary JSONB NOT NULL DEFAULT '{}'::JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT macro_release_cards_current_stage_check
        CHECK (stage IN ('t0', 'developing', 'reaction', 'final')),
    CONSTRAINT macro_release_cards_current_reaction_summary_check
        CHECK (JSONB_TYPEOF(reaction_summary) = 'object')
);

CREATE INDEX IF NOT EXISTS idx_macro_release_cards_current_card
    ON macro_release_cards_current (card_id);
CREATE INDEX IF NOT EXISTS idx_macro_release_cards_current_updated_at
    ON macro_release_cards_current (updated_at DESC);
