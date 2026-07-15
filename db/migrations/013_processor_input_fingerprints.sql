ALTER TABLE processing_log
    ADD COLUMN IF NOT EXISTS input_fingerprint TEXT,
    ADD COLUMN IF NOT EXISTS skip_reason TEXT,
    ADD COLUMN IF NOT EXISTS forced BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_processing_log_reusable_fingerprint
    ON processing_log (processor, completed_at DESC)
    INCLUDE (input_fingerprint, output_id)
    WHERE status = 'success' AND input_fingerprint IS NOT NULL;
