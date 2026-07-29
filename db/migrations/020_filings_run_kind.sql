-- 020: Add 'filings' run_kind for investment filing collection jobs.
ALTER TABLE cycle_runs DROP CONSTRAINT IF EXISTS cycle_runs_run_kind_check;
ALTER TABLE cycle_runs ADD CONSTRAINT cycle_runs_run_kind_check
  CHECK (run_kind = ANY (ARRAY[
    'cycle'::text,
    'collector'::text,
    'processor'::text,
    'news'::text,
    'filings'::text
  ]));
