-- Phase 1b migration: add metadata column to econ_events
-- Stores extra per-event info (e.g. "all_day": true, "tentative": true)
ALTER TABLE econ_events ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}';