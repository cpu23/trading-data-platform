-- Phase 4 UI invalidation events for authenticated live section refresh.
--
-- ui_events contains wakeups only.  Payloads are intentionally limited to the
-- section identity and published version; clients fetch authoritative markup
-- from partial endpoints.  Rows are retained for 48 hours by their caller's
-- expires_at value and are removed by bounded cleanup batches.
--
-- Rollback notes (dependency-safe order): stop event producers and consumers,
-- drop idx_ui_events_expires_at, then idx_ui_events_section_scope_created_at,
-- then drop ui_events.  This migration has no foreign-key dependencies and
-- must be rolled back after any code that reads or writes ui_events is retired.

CREATE TABLE IF NOT EXISTS ui_events (
    id BIGSERIAL PRIMARY KEY,
    event_name TEXT NOT NULL,
    section_key TEXT NOT NULL,
    scope_key TEXT NOT NULL DEFAULT 'global',
    section_version BIGINT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ui_events_event_name_nonblank_check
        CHECK (BTRIM(event_name) <> ''),
    CONSTRAINT ui_events_section_key_nonblank_check
        CHECK (BTRIM(section_key) <> ''),
    CONSTRAINT ui_events_scope_key_nonblank_check
        CHECK (BTRIM(scope_key) <> ''),
    CONSTRAINT ui_events_section_version_positive_check
        CHECK (section_version > 0),
    CONSTRAINT ui_events_payload_object_check
        CHECK (JSONB_TYPEOF(payload) = 'object'),
    CONSTRAINT ui_events_expiry_after_creation_check
        CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS idx_ui_events_section_scope_created_at
    ON ui_events (section_key, scope_key, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ui_events_expires_at
    ON ui_events (expires_at);
