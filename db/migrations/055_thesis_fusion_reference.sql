-- Thesis fusion accepted-reference guard.
--
-- investment_theses.fusion_reference_at is the monotonic accepted-reference
-- guard for autonomous fusion content: an autonomous candidate merge claims
-- a thesis only when the incoming cycle reference is at least the stored
-- guard, so accepted-reference order -- never completion order -- decides
-- which cycle's claim, version, scenario, catalyst, evidence, evaluation,
-- and challenge state is current.  A stale cycle (incoming reference older
-- than the stored guard, or equal to it with a different -- or unprovable
-- -- candidate fingerprint) is a complete no-op: it writes no claim,
-- version, evidence attachment, catalyst, scenario, playbook, position
-- link, forecast, evaluation, or challenge child state for that candidate.
--
-- investment_theses.fusion_candidate_fingerprint pairs the guard with the
-- accepted candidate fingerprint: the content-addressed fingerprint
-- (identity + inputs) of the autonomous candidate that first provenly
-- claimed the thesis at fusion_reference_at.  At an equal reference only
-- the candidate that can prove the identical fingerprint may resume
-- (idempotent rerun); a different fingerprint is a different model output
-- and stays stale, so lock/completion order can never choose between
-- distinct outputs for the same reference.  A strictly newer reference
-- claims and stores both fields together, so a newer cycle always wins and
-- an older cycle can never un-claim a newer one.
--
-- Both columns are nullable on purpose:
--   * Rows created by manual/non-autonomy paths carry no reference and no
--     fingerprint and stay claimable by any autonomous cycle (including
--     legacy rows, which are backfilled conservatively below so a replay
--     of an old cycle can never claim a thesis whose accepted state is
--     provably newer).
--   * The guard pair is only ever advanced by autonomous claims, so a
--     newer cycle can always claim a thesis an older cycle claimed, and an
--     older cycle can never un-claim a newer one.
--   * The fingerprint has NO legacy backfill: the candidate that produced
--     pre-migration content is unknowable, and the only honest value is
--     NULL.  That is fail-closed -- an equal-reference claim against a
--     NULL fingerprint cannot prove it is the same output and is refused
--     as stale; only a strictly newer reference may claim the thesis.
--
-- Backfill: the most conservative accepted/current timestamp is the
-- greatest of every known lifecycle/evaluation timestamp, so a legacy
-- thesis is claimable only by cycles at or after the newest of them.  A
-- content update or evaluation bumps updated_at/last_evaluated_at; using
-- anything less (e.g. preferring an older last_evaluated_at over a newer
-- updated_at) would admit a replay between the two to overwrite current
-- legacy content.  created_at and updated_at are NOT NULL with NOW()
-- defaults (migration 038) and COALESCE covers the one nullable column
-- (last_evaluated_at), so every pre-existing row receives a conservative
-- stamp equal to the latest timestamp it carries.
--
-- Fully idempotent and additive: both columns are ADD COLUMN IF NOT
-- EXISTS, the backfill is self-guarding (WHERE fusion_reference_at IS
-- NULL, so re-applying is a no-op and fresh installs backfill nothing),
-- and no rows, columns, constraints, or indexes are dropped.  No
-- additional index is created: the guard pair is only read through the
-- primary-key thesis lookup inside the merge claim (point reads), so an
-- index would add write amplification without serving a query.
-- Rollback: ALTER TABLE investment_theses DROP COLUMN fusion_reference_at,
-- ALTER TABLE investment_theses DROP COLUMN fusion_candidate_fingerprint.

ALTER TABLE investment_theses
    ADD COLUMN IF NOT EXISTS fusion_reference_at TIMESTAMPTZ;

ALTER TABLE investment_theses
    ADD COLUMN IF NOT EXISTS fusion_candidate_fingerprint TEXT;

UPDATE investment_theses
   SET fusion_reference_at = GREATEST(
           created_at, updated_at, COALESCE(last_evaluated_at, created_at)
       )
 WHERE fusion_reference_at IS NULL;
