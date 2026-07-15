-- Expand durable run lineage for scheduled and on-demand news collection.
-- The replacement is repeatable: the currently active constraint protects writes
-- while the new constraint is installed and validated.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'cycle_runs'::regclass
          AND conname = 'cycle_runs_run_kind_check_news'
    ) THEN
        ALTER TABLE cycle_runs
            ADD CONSTRAINT cycle_runs_run_kind_check_news
            CHECK (run_kind IN ('cycle', 'collector', 'processor', 'news'))
            NOT VALID;
    END IF;
END $$;

ALTER TABLE cycle_runs
    VALIDATE CONSTRAINT cycle_runs_run_kind_check_news;

ALTER TABLE cycle_runs
    DROP CONSTRAINT IF EXISTS cycle_runs_run_kind_check;

ALTER TABLE cycle_runs
    RENAME CONSTRAINT cycle_runs_run_kind_check_news
    TO cycle_runs_run_kind_check;
