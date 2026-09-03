"""Tests for autonomous thesis cycle execution, identity, race conditions, and dispatch."""

import copy
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ORCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH_ROOT))

from thesis_autonomy_support import (
    CANDIDATE,
    EXISTING_ID,
    JOB_TYPE,
    NOW,
    THEME_ID,
    MemorySession,
    OpposingRunner,
    RecordingChallenger,
    RecordingSession,
    ScriptedAuditor,
    ScriptedChallenger,
    ScriptedRunner,
    _count_unversioned_second_pass_candidates,
    _cycle_key,
    _id,
    _load_second_pass_snapshot,
    _persist_candidate_risks,
    _second_pass_candidates,
    canonical_fingerprint,
    cycle_config,
    enqueue_thesis_autonomy_job,
    evidence_item,
    evidence_items,
    run_autonomous_thesis_cycle,
    run_cycle,
    thesis_autonomy_identity,
)

from research_intelligence.evidence import EvidenceCollection, EvidenceRegistry


class AutonomousCycleTests(unittest.TestCase):
    def test_credentialless_cycle_persists_ranks_scores_and_falsifies(self):
        session = MemorySession()
        runner = ScriptedRunner(CANDIDATE)
        challenger = ScriptedChallenger()
        auditor = ScriptedAuditor()
        result = run_cycle(
            session, runner=runner, challenger=challenger, auditor=auditor
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(result["evidence_collected"], 6)
        self.assertEqual(result["raw_candidate_count"], 8)
        self.assertEqual(result["promoted_count"], 1)
        self.assertEqual(result["thesis_count"], 1)
        self.assertEqual(result["scenario_upserts"], 3)
        self.assertEqual(result["risk_upserts"], 1)
        self.assertEqual(result["catalyst_upserts"], 1)
        self.assertEqual(result["group_count"], 1)
        self.assertEqual(result["falsification_runs"], 1)
        self.assertEqual(result["paused_count"], 0)
        self.assertEqual(result["second_pass_candidates"], 0)
        self.assertEqual(result["role_failures"], 0)
        self.assertEqual(result["challenger_failures"], 0)
        self.assertEqual(result["semantic_audit_rejections"], 0)
        self.assertEqual(result["cost_usd"], 0.08)
        self.assertEqual(result["theme_id"], THEME_ID)
        self.assertTrue(result["theme_created"])
        self.assertEqual(result["cycle_key"], "20260815T093000.000000")

        self.assertEqual(len(session.theses), 1)
        thesis = next(iter(session.theses.values()))
        self.assertEqual(thesis["status"], "candidate")
        self.assertEqual(thesis["company"], "acme-corporation")
        self.assertEqual(thesis["symbol"], "ACME")
        active_scenarios = [
            row for row in session.scenarios if row["superseded_at"] is None
        ]
        self.assertEqual(
            {row["name"] for row in active_scenarios}, {"bull", "base", "bear"}
        )
        base = next(row for row in active_scenarios if row["name"] == "base")
        self.assertTrue(base["is_base_case"])
        self.assertEqual(
            {row["expected_return"] for row in active_scenarios}, {0.1, 0.0, -0.2}
        )
        self.assertEqual(
            {row["name"]: row["description"] for row in active_scenarios},
            {
                "bull": CANDIDATE["scenarios"]["bull"]["description"],
                "base": CANDIDATE["scenarios"]["base"]["description"],
                "bear": CANDIDATE["scenarios"]["bear"]["description"],
            },
        )
        # Each supplied invalidator materializes as one structured risk row
        # with conservative deterministic kind/severity — never invented.
        self.assertEqual(len(session.risks), 1)
        risk = session.risks[0]
        self.assertEqual(risk["thesis_id"], thesis["id"])
        self.assertEqual(risk["description"], CANDIDATE["invalidators"][0])
        self.assertEqual(risk["kind"], "counter_thesis")
        self.assertEqual(risk["severity"], "moderate")
        self.assertEqual(len(session.groups), 1)
        self.assertEqual(len(session.members), 1)
        self.assertEqual(len(session.catalysts), 1)
        self.assertEqual(session.catalysts[0]["state"], "pending")
        self.assertEqual(len(session.evidence), 3)
        self.assertTrue(
            all(row["relationship"] == "supports" for row in session.evidence)
        )
        self.assertTrue(all(row["excerpt"] for row in session.evidence))
        self.assertTrue(
            all(
                row["quality_score"] is not None
                and row["entailment_score"] is not None
                and row["freshness_score"] is not None
                and row["effective_weight"] is not None
                for row in session.evidence
            )
        )
        self.assertTrue(all(row["entailment_score"] == 1.0 for row in session.evidence))
        self.assertEqual(len(session.snapshots), 1)
        self.assertEqual(len(session.falsification_runs), 1)
        run = next(iter(session.falsification_runs.values()))
        self.assertEqual(run["status"], "not_falsified")
        self.assertEqual(result["opportunity_snapshots"], 1)
        self.assertEqual(runner.calls, 8)
        self.assertEqual(auditor.calls, 1)

    def test_nullable_scenario_probabilities_persist_as_unknown(self):
        candidate = copy.deepcopy(CANDIDATE)
        candidate["scenarios"]["base"]["probability"] = None
        session = MemorySession()
        result = run_cycle(session, runner=ScriptedRunner(candidate))
        self.assertEqual(result["status"], "completed")
        active_scenarios = [
            row for row in session.scenarios if row["superseded_at"] is None
        ]
        base = next(row for row in active_scenarios if row["name"] == "base")
        self.assertIsNone(base["probability"])
        run = next(iter(session.falsification_runs.values()))
        # Missing probability threatens (never invented): inconclusive.
        self.assertEqual(run["status"], "inconclusive")

    def test_rerun_with_identical_inputs_creates_no_duplicate_identities(self):
        session = MemorySession()
        runner = ScriptedRunner(CANDIDATE)
        first = run_cycle(session, runner=runner, challenger=ScriptedChallenger())
        second = run_cycle(session, runner=runner, challenger=ScriptedChallenger())

        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["error_count"], 0)
        self.assertEqual(second["thesis_count"], 1)
        self.assertEqual(second["scenario_upserts"], 0)
        self.assertEqual(second["risk_upserts"], 0)
        self.assertEqual(second["falsification_runs"], 0)
        self.assertEqual(second["playbook_upserts"], 0)
        self.assertEqual(second["catalyst_upserts"], 0)
        self.assertFalse(second["theme_created"])

        self.assertEqual(len(session.theses), 1)
        self.assertEqual(len(session.evidence), 3)
        self.assertEqual(
            len([row for row in session.scenarios if row["superseded_at"] is None]),
            3,
        )
        # Reruns never duplicate structured risks: the single invalidator
        # stays one row with the same identity.
        self.assertEqual(len(session.risks), 1)
        self.assertEqual(len(session.snapshots), 1)
        self.assertEqual(len(session.falsification_runs), 1)
        self.assertEqual(len(session.groups), 1)
        self.assertEqual(len(session.members), 1)
        self.assertEqual(len(session.playbooks), 1)
        self.assertEqual(len(session.catalysts), 1)
        self.assertEqual(first["cycle_key"], second["cycle_key"])

    def test_current_cycle_scores_its_own_persisted_artifacts_at_cutoff(self):
        # The cycle reference precedes the run's own writes: scenarios and
        # the catalyst are persisted DURING the run, so their rows postdate
        # the cutoff and would be excluded by the replay-safe persisted
        # queries.  They enter scoring as explicit current-cycle inputs
        # (derived from cutoff-bounded evidence), so the fresh candidate
        # still scores its own legs, catalyst, and opportunity snapshot.
        session = MemorySession()
        session.market_data["ACME"] = [(NOW - timedelta(hours=1), 100.0)]
        result = run_cycle(
            session,
            as_of=NOW - timedelta(seconds=1),
            runner=ScriptedRunner(CANDIDATE),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(result["opportunity_snapshots"], 1)
        thesis = next(iter(session.theses.values()))
        # 0.3*0.1 + 0.5*0.0 + 0.2*(-0.2) - 0.0 cost from the candidate legs;
        # a DB-only reconstruction at the cutoff would find no scenarios
        # (rows were created after the reference) and value the thesis at 0.
        self.assertAlmostEqual(thesis["expected_value"], -0.01)
        self.assertAlmostEqual(thesis["expected_shortfall"], 0.04)
        # One pending catalyst without a date contributes the neutral 0.5.
        self.assertEqual(thesis["catalyst_score"], 0.5)
        self.assertGreater(thesis["evidence_strength"], 0.0)
        self.assertGreater(thesis["opportunity_score"], 0.0)

    def test_stale_replay_evaluation_does_not_regress_current_scores(self):
        # A replay cycle whose reference predates a newer evaluation of the
        # same thesis computes its results but must not overwrite the newer
        # current ranking columns or last_evaluated_at.  The legacy catalyst
        # backfill evaluation is the cycle path that touches an
        # already-evaluated thesis (its catalyst row postdates the
        # reference, so it scores via the explicit current-cycle input).
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            origin="fusion",
            status="active",
            catalyst_summary="Quarterly disclosure confirms the operating change",
            opportunity_score=0.9,
            last_evaluated_at=NOW,
        )
        result = run_cycle(
            session,
            as_of=NOW - timedelta(days=1),
            runner=ScriptedRunner(CANDIDATE),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["legacy_catalyst_backfills"], 1)
        thesis = session.theses[EXISTING_ID]
        # The newer evaluation stays the current ranking state; the stale
        # replay result (computed and returned) is never applied.
        self.assertEqual(thesis["last_evaluated_at"], NOW)
        self.assertEqual(thesis["opportunity_score"], 0.9)

    def test_replay_never_backfills_or_scores_post_reference_catalyst(self):
        # A replay at R performs no maintenance write and supplies no
        # explicit score input for a thesis whose existence/current/fusion
        # state is not provable at R: no catalyst row is materialized for
        # it and it is never re-evaluated from the older cycle (its newer
        # ranking state stays untouched).
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            origin="fusion",
            status="active",
            catalyst_summary="Quarterly disclosure confirms the operating change",
            created_at=NOW,
            updated_at=NOW,
            fusion_reference_at=NOW,
            last_evaluated_at=NOW,
            opportunity_score=0.9,
        )
        result = run_cycle(
            session,
            as_of=NOW - timedelta(days=1),
            runner=ScriptedRunner(CANDIDATE),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["legacy_catalyst_backfills"], 0)
        thesis = session.theses[EXISTING_ID]
        # No score backdating: the thesis was never evaluated by the replay.
        self.assertEqual(thesis["last_evaluated_at"], NOW)
        self.assertEqual(thesis["opportunity_score"], 0.9)
        self.assertFalse(
            any(row["thesis_id"] == EXISTING_ID for row in session.catalysts)
        )

    def test_role_failure_is_isolated_and_other_roles_still_promote(self):
        session = MemorySession()
        runner = ScriptedRunner(CANDIDATE, fail_roles={"contrarian"})
        result = run_cycle(session, runner=runner)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(result["role_failures"], 1)
        self.assertEqual(result["promoted_count"], 1)
        self.assertEqual(len(session.theses), 1)

    def test_all_roles_failing_still_returns_bounded_result(self):
        class FailingRunner:
            cost_usd = 0.0
            calls = 0

            def run(self, *, role, prompt, schema):
                raise RuntimeError("simulated outage")

        session = MemorySession()
        result = run_cycle(session, runner=FailingRunner())
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["role_failures"], 8)
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["thesis_count"], 0)
        self.assertEqual(result["error_count"], 0)

    def test_challenger_failure_fails_closed_before_promotion(self):
        session = MemorySession()
        result = run_cycle(
            session,
            runner=ScriptedRunner(CANDIDATE),
            challenger=ScriptedChallenger(fail=True),
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["challenger_failures"], 1)
        self.assertEqual(result["falsification_runs"], 0)
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(session.falsification_runs, {})

    def test_source_quality_gate_rejects_single_family_support(self):
        same_family = tuple(
            evidence_item(
                index,
                source_name="One Wire",
                provenance={
                    "adapter": "source_claims",
                    "source_family": "one-wire",
                },
            )
            for index in range(6)
        )
        with patch.object(
            EvidenceRegistry,
            "collect",
            return_value=EvidenceCollection(items=same_family, failures={}),
        ):
            result = run_autonomous_thesis_cycle(
                MemorySession(),
                cycle_config(
                    minimum_supporting_source_families=2,
                    require_cited_excerpts=True,
                ),
                as_of=NOW,
                runner=ScriptedRunner(CANDIDATE),
                challenger=ScriptedChallenger(),
                auditor=ScriptedAuditor(),
            )
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["source_gate_rejections"], 1)

    def test_opposition_gate_rejects_one_sided_competition(self):
        with patch.object(
            EvidenceRegistry,
            "collect",
            return_value=EvidenceCollection(items=tuple(evidence_items()), failures={}),
        ):
            result = run_autonomous_thesis_cycle(
                MemorySession(),
                cycle_config(require_opposing_variants=True),
                as_of=NOW,
                runner=ScriptedRunner(CANDIDATE),
                challenger=ScriptedChallenger(),
                auditor=ScriptedAuditor(),
            )
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["opposition_gate_rejections"], 1)

    def test_rejected_opponent_cannot_enable_surviving_direction(self):
        class RejectShortChallenger(ScriptedChallenger):
            def challenge(self, snapshot, evidence):
                self.calls += 1
                if snapshot.direction == "short":
                    raise RuntimeError("short challenger unavailable")
                return None

        session = MemorySession()
        challenger = RejectShortChallenger()
        with patch.object(
            EvidenceRegistry,
            "collect",
            return_value=EvidenceCollection(items=tuple(evidence_items()), failures={}),
        ):
            result = run_autonomous_thesis_cycle(
                session,
                cycle_config(require_opposing_variants=True),
                as_of=NOW,
                runner=OpposingRunner(CANDIDATE),
                challenger=challenger,
                auditor=ScriptedAuditor(),
            )
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["challenger_failures"], 1)
        self.assertEqual(result["opposition_gate_rejections"], 1)
        self.assertEqual(result["challenge_attempts"], 2)
        self.assertEqual(session.theses, {})

    def test_opposing_variants_share_one_canonical_competition_group(self):
        session = MemorySession()
        with (
            patch.object(
                EvidenceRegistry,
                "collect",
                return_value=EvidenceCollection(
                    items=tuple(evidence_items()), failures={}
                ),
            ),
            patch(
                "thesis_autonomy._existing_fusion_mechanism",
                side_effect=lambda *args, **kwargs: kwargs["fallback"],
            ),
        ):
            result = run_autonomous_thesis_cycle(
                session,
                cycle_config(
                    minimum_supporting_source_families=2,
                    require_cited_excerpts=True,
                    require_opposing_variants=True,
                ),
                as_of=NOW,
                runner=OpposingRunner(CANDIDATE),
                challenger=ScriptedChallenger(),
                auditor=ScriptedAuditor(),
            )
        self.assertEqual(result["promoted_count"], 2)
        self.assertEqual(result["group_count"], 1)
        self.assertEqual(
            {row["direction"] for row in session.theses.values()}, {"long", "short"}
        )

    def test_auditor_gates_promotion_and_failures_reject_the_batch(self):
        session = MemorySession()
        result = run_cycle(
            session,
            runner=ScriptedRunner(CANDIDATE),
            auditor=ScriptedAuditor(verdict="unsupported"),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["semantic_audit_rejections"], 1)
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["thesis_count"], 0)

        session2 = MemorySession()
        result2 = run_cycle(
            session2,
            runner=ScriptedRunner(CANDIDATE),
            auditor=ScriptedAuditor(fail=True),
        )
        self.assertEqual(result2["status"], "completed")
        self.assertEqual(result2["promoted_count"], 0)
        self.assertEqual(result2["error_count"], 0)

    def test_challenger_citations_attach_as_contradicts_and_score_recomputes(self):
        session = MemorySession()
        proposal = {
            "kind": "counter_evidence",
            "statement": "The cited evidence supports the opposite view",
            "citations": ["source_claim:claim:0005"],
        }
        result = run_cycle(
            session,
            runner=ScriptedRunner(CANDIDATE),
            challenger=ScriptedChallenger(proposal=proposal),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["contradictions_attached"], 1)
        self.assertEqual(result["challenger_failures"], 0)
        contradicts = [
            row for row in session.evidence if row["relationship"] == "contradicts"
        ]
        self.assertEqual(len(contradicts), 1)
        self.assertEqual(contradicts[0]["evidence_id"], "claim:0005")
        run = next(iter(session.falsification_runs.values()))
        self.assertEqual(run["status"], "inconclusive")

    def test_breached_existing_thesis_pauses_and_never_closes(self):
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            status="active",
            opportunity_score=0.9,
            last_evaluated_at=NOW - timedelta(days=1),
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_id="claim:invalidate",
            relationship="invalidation",
            evidence_fingerprint="d" * 64,
        )
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["second_pass_candidates"], 1)
        self.assertEqual(result["second_pass_challenged"], 1)
        self.assertEqual(result["paused_count"], 1)
        self.assertEqual(session.theses[EXISTING_ID]["status"], "paused")
        run = session.falsification_runs[
            (EXISTING_ID, "autonomy:20260815T093000.000000")
        ]
        self.assertEqual(run["status"], "falsified")
        # The promoted thesis stays candidate: intact theses never auto-promote.
        promoted = [row for row in session.theses.values() if row["id"] != EXISTING_ID]
        self.assertEqual(promoted[0]["status"], "candidate")

    def test_second_pass_skips_theses_already_challenged_this_cycle(self):
        session = MemorySession()
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        # The only candidate thesis is the promoted one; it is excluded from
        # the second pass, so nothing is challenged twice in one run.
        self.assertEqual(result["second_pass_candidates"], 0)
        self.assertEqual(result["second_pass_challenged"], 0)
        self.assertEqual(len(session.falsification_runs), 1)

    def test_second_pass_recompute_keeps_backfilled_catalyst_at_cutoff(self):
        # A legacy thesis whose catalyst is backfilled this cycle is scored
        # with the catalyst as an explicit current-cycle input; when its
        # contradiction recompute runs in the second pass, the same explicit
        # input must survive the replay cutoff (the catalyst row postdates
        # the reference) instead of being dropped and erasing the score.
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            origin="fusion",
            status="active",
            opportunity_score=0.9,
            catalyst_summary="Quarterly disclosure confirms the operating change",
            last_evaluated_at=NOW - timedelta(days=1),
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_id="claim:seed",
            evidence_fingerprint="c" * 64,
        )
        proposal = {
            "kind": "counter_evidence",
            "statement": "The cited evidence supports the opposite view",
            "citations": ["source_claim:claim:0000"],
        }
        result = run_cycle(
            session,
            runner=ScriptedRunner(CANDIDATE),
            challenger=ScriptedChallenger(proposal=proposal),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(result["legacy_catalyst_backfills"], 1)
        self.assertEqual(result["second_pass_candidates"], 1)
        self.assertEqual(result["second_pass_challenged"], 1)
        # The promoted thesis already cites claim:0000 as support, so the
        # contradiction is deduplicated there and only the second-pass
        # thesis gains a contradicts row (which triggers its recompute).
        self.assertEqual(result["contradictions_attached"], 1)
        self.assertEqual(
            [
                row["thesis_id"]
                for row in session.evidence
                if row["relationship"] == "contradicts"
            ],
            [EXISTING_ID],
        )
        # The recompute kept the backfilled catalyst: pending scores 0.5.
        self.assertEqual(session.theses[EXISTING_ID]["catalyst_score"], 0.5)


class CandidateRiskPersistenceTests(unittest.TestCase):
    """Promoted candidates persist described scenarios and structured risks."""

    def test_blank_scenario_description_rejects_promotion(self):
        candidate = copy.deepcopy(CANDIDATE)
        candidate["scenarios"]["bull"]["description"] = "   "
        session = MemorySession()
        result = run_cycle(session, runner=ScriptedRunner(candidate))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["error_count"], 0)
        # The blank description rejects the candidate before promotion: no
        # investable row, no scenarios, no risks.
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["thesis_count"], 0)
        self.assertEqual(session.theses, {})
        self.assertEqual(session.scenarios, [])
        self.assertEqual(session.risks, [])

    def test_empty_invalidators_reject_promotion(self):
        candidate = copy.deepcopy(CANDIDATE)
        candidate["invalidators"] = []
        session = MemorySession()
        result = run_cycle(session, runner=ScriptedRunner(candidate))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["error_count"], 0)
        # No supplied invalidator means no structured risk could exist, so
        # promotion is rejected rather than creating an investable row.
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["thesis_count"], 0)
        self.assertEqual(session.theses, {})
        self.assertEqual(session.risks, [])

    def test_risks_are_idempotent_and_never_invented(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID)

        class Candidate:
            def __init__(self, invalidators):
                self.invalidators = tuple(invalidators)

        first = _persist_candidate_risks(
            session,
            EXISTING_ID,
            Candidate(["cost trend reverses", "guidance withdrawn"]),
        )
        second = _persist_candidate_risks(
            session,
            EXISTING_ID,
            Candidate(["cost trend reverses", "guidance withdrawn"]),
        )
        # One row per supplied invalidator, exactly; reruns add nothing.
        self.assertEqual(first, 2)
        self.assertEqual(second, 0)
        self.assertEqual(len(session.risks), 2)
        self.assertEqual(
            {row["description"] for row in session.risks},
            {"cost trend reverses", "guidance withdrawn"},
        )
        # Conservative deterministic kind/severity, never inferred.
        self.assertTrue(all(row["kind"] == "counter_thesis" for row in session.risks))
        self.assertTrue(all(row["severity"] == "moderate" for row in session.risks))
        # Every insert serialized on the exact-identity advisory lock.
        self.assertEqual(len(session.risk_lock_keys), 4)
        self.assertTrue(
            all(
                key.startswith(f"risk_identity:{EXISTING_ID}:")
                for key in session.risk_lock_keys
            )
        )


class CycleIdentityTests(unittest.TestCase):
    """The cycle identity is the full accepted reference (seconds plus
    fractional microseconds, UTC): distinct accepted references never
    collide on snapshots, falsification runs, or challenge claims, while an
    exact-reference rerun coalesces on the same keys."""

    def test_cycle_key_renders_fixed_full_precision_utc(self):
        # Seconds and fractional microseconds are part of the identity, so
        # 09:30:10, 09:30:50, and 09:30:10.000001 never share a key.
        self.assertEqual(
            _cycle_key(datetime(2026, 8, 15, 9, 30, 10, tzinfo=UTC)),
            "20260815T093010.000000",
        )
        self.assertEqual(
            _cycle_key(datetime(2026, 8, 15, 9, 30, 50, tzinfo=UTC)),
            "20260815T093050.000000",
        )
        self.assertEqual(
            _cycle_key(datetime(2026, 8, 15, 9, 30, 10, 1, tzinfo=UTC)),
            "20260815T093010.000001",
        )
        self.assertEqual(
            _cycle_key(datetime(2026, 8, 15, 9, 30, 10, 123456, tzinfo=UTC)),
            "20260815T093010.123456",
        )
        # Aware non-UTC references normalize to UTC first.
        self.assertEqual(
            _cycle_key(datetime.fromisoformat("2026-08-15T11:30:10+02:00")),
            "20260815T093010.000000",
        )
        # Naive references are treated as UTC, exactly like _as_utc.
        self.assertEqual(
            _cycle_key(datetime(2026, 8, 15, 9, 30, 10)),
            "20260815T093010.000000",
        )
        # Deterministic, fixed width, and collision-free across seconds and
        # fractional microseconds.
        key = _cycle_key(datetime(2026, 8, 15, 9, 30, 10, 123456, tzinfo=UTC))
        self.assertEqual(len(key), 22)
        self.assertEqual(
            key,
            _cycle_key(datetime(2026, 8, 15, 9, 30, 10, 123456, tzinfo=UTC)),
        )
        self.assertNotEqual(
            _cycle_key(datetime(2026, 8, 15, 9, 30, 10, tzinfo=UTC)),
            _cycle_key(datetime(2026, 8, 15, 9, 30, 50, tzinfo=UTC)),
        )
        self.assertNotEqual(
            _cycle_key(datetime(2026, 8, 15, 9, 30, 10, tzinfo=UTC)),
            _cycle_key(datetime(2026, 8, 15, 9, 30, 10, 1, tzinfo=UTC)),
        )

    def test_seconds_apart_references_never_share_audit_identities(self):
        # 09:30:00 and 09:30:40 are distinct accepted references: the
        # second cycle must append its own immutable snapshot, falsification
        # run, and challenge claim instead of colliding with (or
        # overwriting) the first cycle's rows.
        session = MemorySession()
        challenger = RecordingChallenger()
        first = run_cycle(
            session,
            runner=ScriptedRunner(CANDIDATE),
            challenger=challenger,
        )
        second = run_cycle(
            session,
            as_of=NOW + timedelta(seconds=40),
            runner=ScriptedRunner(CANDIDATE),
            challenger=challenger,
        )

        self.assertEqual(first["cycle_key"], "20260815T093000.000000")
        self.assertEqual(second["cycle_key"], "20260815T093040.000000")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["error_count"], 0)
        (thesis_id,) = session.theses.keys()
        # Both accepted references left their own immutable audit rows.
        self.assertEqual(
            session.snapshots,
            {
                (thesis_id, "autonomy:20260815T093000.000000"),
                (thesis_id, "autonomy:20260815T093040.000000"),
            },
        )
        self.assertEqual(
            set(session.falsification_runs),
            {
                (thesis_id, "autonomy:20260815T093000.000000"),
                (thesis_id, "autonomy:20260815T093040.000000"),
            },
        )
        self.assertEqual(len(session.falsification_runs), 2)
        # Challenge claim identities embed the full accepted reference too.
        self.assertEqual(len(challenger.captured), 2)
        claims = [snapshot.claims[0].claim_id for snapshot, _ in challenger.captured]
        candidate_id = claims[0].split(":autonomy:", 1)[0]
        self.assertEqual(
            set(claims),
            {
                f"{candidate_id}:autonomy:20260815T093000.000000",
                f"{candidate_id}:autonomy:20260815T093040.000000",
            },
        )

    def test_fractional_microsecond_references_stay_distinct(self):
        # 09:30:10.000000 vs 09:30:10.000001: even a one-microsecond
        # difference must never collide or overwrite by timing.
        session = MemorySession()
        first = run_cycle(
            session,
            as_of=NOW + timedelta(seconds=10),
            runner=ScriptedRunner(CANDIDATE),
        )
        second = run_cycle(
            session,
            as_of=NOW + timedelta(seconds=10, microseconds=1),
            runner=ScriptedRunner(CANDIDATE),
        )

        self.assertEqual(first["cycle_key"], "20260815T093010.000000")
        self.assertEqual(second["cycle_key"], "20260815T093010.000001")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["error_count"], 0)
        (thesis_id,) = session.theses.keys()
        self.assertEqual(
            session.snapshots,
            {
                (thesis_id, "autonomy:20260815T093010.000000"),
                (thesis_id, "autonomy:20260815T093010.000001"),
            },
        )
        self.assertEqual(
            set(session.falsification_runs),
            {
                (thesis_id, "autonomy:20260815T093010.000000"),
                (thesis_id, "autonomy:20260815T093010.000001"),
            },
        )

    def test_exact_reference_rerun_coalesces_on_the_same_audit_keys(self):
        # An exact rerun at one accepted reference reproduces the identical
        # cycle identity and idempotently reuses the snapshot and
        # falsification rows instead of appending duplicates.
        session = MemorySession()
        first = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        second = run_cycle(session, runner=ScriptedRunner(CANDIDATE))

        self.assertEqual(first["cycle_key"], "20260815T093000.000000")
        self.assertEqual(first["cycle_key"], second["cycle_key"])
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["error_count"], 0)
        self.assertEqual(second["falsification_runs"], 0)
        (thesis_id,) = session.theses.keys()
        self.assertEqual(
            session.snapshots,
            {(thesis_id, "autonomy:20260815T093000.000000")},
        )
        self.assertEqual(
            set(session.falsification_runs),
            {(thesis_id, "autonomy:20260815T093000.000000")},
        )


class FusionReferenceRaceTests(unittest.TestCase):
    """Accepted reference, not completion order, decides current state."""

    OLDER = NOW - timedelta(days=1)

    def _older_candidate(self) -> dict:
        candidate = copy.deepcopy(CANDIDATE)
        candidate["claim"] = "An older cycle's stale claim for the subject"
        candidate["variant_perception"] = "Superseded variant perception"
        # Every leg differs from the current candidate (including base,
        # whose 0.5/0.0 would otherwise be an intentional no-op), so the
        # newer cycle's upserts supersede ALL older versions; probabilities
        # stay valid and sum to one across bull/base/bear.
        candidate["scenarios"] = {
            "bull": {
                "probability": 0.2,
                "expected_return": 0.05,
                "description": "older bull path",
            },
            "base": {
                "probability": 0.6,
                "expected_return": 0.02,
                "description": "older base path",
            },
            "bear": {
                "probability": 0.2,
                "expected_return": -0.15,
                "description": "older bear path",
            },
        }
        return candidate

    def test_older_first_newer_second_yields_newer_current_state(self):
        session = MemorySession()
        first = run_cycle(
            session,
            as_of=self.OLDER,
            runner=ScriptedRunner(self._older_candidate()),
        )
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["thesis_count"], 1)
        self.assertEqual(first["stale_candidates"], 0)

        second = run_cycle(session, as_of=NOW, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["error_count"], 0)
        self.assertEqual(second["thesis_count"], 1)
        self.assertEqual(second["stale_candidates"], 0)
        # Every autonomous merge routes the canonical-key advisory lock
        # that closes the both-see-no-thesis create race.
        self.assertTrue(
            any("pg_advisory_xact_lock" in sql for sql, _params in session.calls)
        )

        (thesis_id,) = session.theses.keys()
        thesis = session.theses[thesis_id]
        # The newer cycle's claim is the current claim and the guard
        # advanced to its accepted reference.
        self.assertEqual(thesis["claim"], CANDIDATE["claim"])
        self.assertEqual(thesis["fusion_reference_at"], NOW)
        # The newer claim appended a version (2) and superseded the older
        # cycle's scenario legs (active legs are version 2).
        self.assertEqual(session.versions[thesis_id], 2)
        active_scenarios = [
            row for row in session.scenarios if row["superseded_at"] is None
        ]
        self.assertEqual(
            {row["name"] for row in active_scenarios}, {"bull", "base", "bear"}
        )
        self.assertTrue(
            all(row["version"] == 2 for row in active_scenarios),
            active_scenarios,
        )
        self.assertEqual(
            {row["expected_return"] for row in active_scenarios}, {0.1, 0.0, -0.2}
        )

    def test_newer_first_older_second_makes_the_older_job_a_complete_noop(self):
        session = MemorySession()
        newer = run_cycle(session, as_of=NOW, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(newer["status"], "completed")
        self.assertEqual(newer["thesis_count"], 1)

        older = run_cycle(
            session,
            as_of=self.OLDER,
            runner=ScriptedRunner(self._older_candidate()),
        )
        self.assertEqual(older["status"], "completed")
        self.assertEqual(older["error_count"], 0)
        # The stale cycle still serialized on the canonical-key advisory
        # lock before its lookup, then was rejected by the accepted-
        # reference guard: every child/current-state write was skipped.
        self.assertTrue(
            any("pg_advisory_xact_lock" in sql for sql, _params in session.calls)
        )
        self.assertEqual(older["stale_candidates"], 1)
        self.assertEqual(older["thesis_count"], 0)
        self.assertEqual(older["scenario_upserts"], 0)
        self.assertEqual(older["catalyst_upserts"], 0)
        self.assertEqual(older["playbook_upserts"], 0)
        self.assertEqual(older["watch_links"], 0)
        self.assertEqual(older["forecasts_frozen"], 0)
        self.assertEqual(older["opportunity_snapshots"], 0)
        self.assertEqual(older["falsification_runs"], 0)
        self.assertEqual(older["contradictions_attached"], 0)
        self.assertEqual(older["paused_count"], 0)
        self.assertEqual(older["group_count"], 0)

        (thesis_id,) = session.theses.keys()
        thesis = session.theses[thesis_id]
        # The newer cycle's claim/version/scenario state is untouched.
        self.assertEqual(thesis["claim"], CANDIDATE["claim"])
        self.assertEqual(thesis["fusion_reference_at"], NOW)
        self.assertEqual(session.versions[thesis_id], 1)
        active_scenarios = [
            row for row in session.scenarios if row["superseded_at"] is None
        ]
        self.assertEqual(len(active_scenarios), 3)
        self.assertTrue(all(row["version"] == 1 for row in active_scenarios))
        # No child state was appended: evidence links, catalysts, playbooks,
        # snapshots, groups, members, and falsification runs all stay at the
        # newer cycle's single set.
        self.assertEqual(len(session.evidence), 3)
        self.assertEqual(len(session.catalysts), 1)
        self.assertEqual(len(session.playbooks), 1)
        self.assertEqual(len(session.snapshots), 1)
        self.assertEqual(len(session.groups), 1)
        self.assertEqual(len(session.members), 1)
        self.assertEqual(len(session.falsification_runs), 1)
        self.assertEqual(len(session.position_links), 0)


class ContextAffectedPriorityTests(unittest.TestCase):
    LINKED_ID = "35353535-3535-4535-8535-353535353535"
    AFFECTED_ID = "36363636-3636-4636-8636-363636363636"
    PLAIN_ID = "37373737-3737-4737-8737-373737373737"

    def _seed(self):
        session = MemorySession()
        # Plain thesis with the highest opportunity score.
        session.seed_thesis(
            self.PLAIN_ID,
            status="active",
            opportunity_score=0.9,
            last_evaluated_at=NOW - timedelta(days=1),
        )
        session.seed_evidence(
            self.PLAIN_ID,
            evidence_id="claim:plain-support",
            evidence_fingerprint="b" * 64,
        )
        # Affected thesis: recent context match, no position link.
        session.seed_thesis(
            self.AFFECTED_ID,
            status="active",
            opportunity_score=0.2,
            last_evaluated_at=NOW - timedelta(days=1),
        )
        session.seed_evidence(
            self.AFFECTED_ID,
            evidence_id="claim:affected-inv",
            relationship="invalidation",
            evidence_fingerprint="f" * 64,
        )
        playbook = session.seed_playbook(self.AFFECTED_ID)
        session.seed_context_match(
            playbook["id"], "99999999-9999-4999-8999-999999999999"
        )
        # Linked thesis (watch) with a low opportunity score.
        session.seed_thesis(
            self.LINKED_ID,
            status="active",
            opportunity_score=0.1,
            last_evaluated_at=NOW - timedelta(days=1),
        )
        session.seed_evidence(
            self.LINKED_ID,
            evidence_id="claim:linked-inv",
            relationship="invalidation",
            evidence_fingerprint="a" * 64,
        )
        session.position_links.append(
            {
                "position_id": "67676767-6767-4767-8767-676767676767",
                "thesis_id": self.LINKED_ID,
                "link_type": "watch",
                "created_at": NOW - timedelta(days=1),
                "removed_at": None,
            }
        )
        return session

    def test_affected_theses_rank_ahead_of_generic_high_opportunity(self):
        session = self._seed()
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["context_affected"], 1)
        self.assertEqual(result["second_pass_challenged"], 3)
        # Affected thesis is falsified/paused; the plain high-opportunity
        # thesis is challenged only after it and stays intact.
        self.assertEqual(session.theses[self.AFFECTED_ID]["status"], "paused")
        self.assertEqual(session.theses[self.PLAIN_ID]["status"], "active")
        affected_run = session.falsification_runs[
            (self.AFFECTED_ID, "autonomy:20260815T093000.000000")
        ]
        self.assertEqual(affected_run["status"], "falsified")
        plain_run = session.falsification_runs[
            (self.PLAIN_ID, "autonomy:20260815T093000.000000")
        ]
        self.assertEqual(plain_run["status"], "not_falsified")

    def test_linked_positions_still_sort_first_within_affected(self):
        session = self._seed()
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(result["second_pass_challenged"], 3)
        keys = [row[0] for row in session.falsification_runs]
        # The promoted thesis is challenged first; the second pass then runs
        # linked -> affected -> plain.
        promoted = [
            thesis_id
            for thesis_id in keys
            if thesis_id not in {self.LINKED_ID, self.AFFECTED_ID, self.PLAIN_ID}
        ]
        self.assertEqual(len(promoted), 1)
        self.assertEqual(keys[0], promoted[0])
        self.assertEqual(keys[1], self.LINKED_ID)
        self.assertEqual(keys[2], self.AFFECTED_ID)
        self.assertEqual(keys[3], self.PLAIN_ID)
        self.assertEqual(session.theses[self.LINKED_ID]["status"], "paused")
        self.assertEqual(session.theses[self.AFFECTED_ID]["status"], "paused")
        self.assertEqual(session.theses[self.PLAIN_ID]["status"], "active")

    def test_challenge_cap_is_shared_by_promotion_and_second_pass(self):
        session = self._seed()
        challenger = ScriptedChallenger()
        with patch.object(
            EvidenceRegistry,
            "collect",
            return_value=EvidenceCollection(items=tuple(evidence_items()), failures={}),
        ):
            result = run_autonomous_thesis_cycle(
                session,
                cycle_config(maximum_challenges_per_run=1),
                as_of=NOW,
                runner=ScriptedRunner(CANDIDATE),
                challenger=challenger,
                auditor=ScriptedAuditor(),
            )
        self.assertEqual(result["promoted_count"], 1)
        self.assertEqual(result["challenge_limit"], 1)
        self.assertEqual(result["challenge_attempts"], 1)
        self.assertEqual(result["falsification_runs"], 1)
        self.assertEqual(result["second_pass_candidates"], 0)
        self.assertEqual(result["second_pass_challenged"], 0)
        self.assertEqual(challenger.calls, 1)


class PositionLinkTests(unittest.TestCase):
    def test_exact_normalized_symbol_match_links_watch(self):
        session = MemorySession()
        session.seed_holding("67676767-6767-4767-8767-676767676767", " ACME ")
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(result["watch_links"], 1)
        self.assertEqual(len(session.position_links), 1)
        link = session.position_links[0]
        self.assertEqual(link["link_type"], "watch")
        self.assertEqual(link["position_id"], "67676767-6767-4767-8767-676767676767")

    def test_exact_only_match_and_no_symbol_skip_linking(self):
        session = MemorySession()
        session.seed_holding("67676767-6767-4767-8767-676767676767", "ACMEX")
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(result["watch_links"], 0)
        self.assertEqual(session.position_links, [])

        candidate = copy.deepcopy(CANDIDATE)
        candidate["instrument"] = ""
        session2 = MemorySession()
        session2.seed_holding("67676767-6767-4767-8767-676767676767", "ACME")
        result2 = run_cycle(session2, runner=ScriptedRunner(candidate))
        self.assertEqual(result2["watch_links"], 0)
        self.assertEqual(session2.position_links, [])

    def test_rerun_does_not_duplicate_watch_links(self):
        session = MemorySession()
        session.seed_holding("67676767-6767-4767-8767-676767676767", "ACME")
        first = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        second = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(first["watch_links"], 1)
        self.assertEqual(second["watch_links"], 0)
        self.assertEqual(len(session.position_links), 1)

    def test_watch_linked_thesis_is_prioritized_in_second_pass(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID, status="active", opportunity_score=0.1)
        session.seed_evidence(
            EXISTING_ID,
            evidence_id="claim:invalidate",
            relationship="invalidation",
            evidence_fingerprint="e" * 64,
        )
        session.position_links.append(
            {
                "position_id": "67676767-6767-4767-8767-676767676767",
                "thesis_id": EXISTING_ID,
                "link_type": "watch",
                "created_at": NOW - timedelta(days=1),
                "removed_at": None,
            }
        )
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(result["second_pass_candidates"], 1)
        self.assertEqual(result["second_pass_challenged"], 1)
        self.assertEqual(result["paused_count"], 1)
        self.assertEqual(session.theses[EXISTING_ID]["status"], "paused")


class SecondPassReferenceSafetyTests(unittest.TestCase):
    """Second-pass reconstruction consumes only reference-visible state.

    The second falsification pass fails closed: theses whose current state
    is not provable at the reference (missing or post-reference
    created/updated timestamps) are never selected, challenged, or paused,
    and snapshot scenarios/evidence attachments are bounded by the same
    cutoff.  Durable live cycles (reference = acceptance time) retain the
    full falsification path.
    """

    PAST_ID = "38383838-3838-4838-8838-383838383838"
    FUTURE_ID = "39393939-3939-4939-8939-393939393939"
    MUTATED_ID = "3a3a3a3a-3a3a-4a3a-8a3a-3a3a3a3a3a"
    SCORED_ID = "3b3b3b3b-3b3b-4b3b-8b3b-3b3b3b3b3b"
    FUSED_ID = "3c3c3c3c-3c3c-4c3c-8c3c-3c3c3c3c3c"
    REPLAY_AT = NOW - timedelta(days=10)

    def _visible_thesis(self, thesis_id: str, opportunity: float) -> None:
        """A thesis whose whole state predates the replay reference."""
        session = self.session
        session.seed_thesis(
            thesis_id,
            status="active",
            opportunity_score=opportunity,
            last_evaluated_at=self.REPLAY_AT - timedelta(days=1),
            created_at=NOW - timedelta(days=30),
            updated_at=NOW - timedelta(days=30),
        )
        session.seed_evidence(
            thesis_id,
            evidence_id=f"claim:{thesis_id}-inv",
            relationship="invalidation",
            evidence_fingerprint="d" * 64,
            created_at=NOW - timedelta(days=30),
            source_timestamp=NOW - timedelta(days=30),
            available_at=NOW - timedelta(days=30),
        )

    def test_historical_replay_never_challenges_post_reference_theses(self):
        session = self.session = MemorySession()
        self._visible_thesis(self.PAST_ID, opportunity=0.9)
        # Created after the replay reference: its current state is future.
        session.seed_thesis(
            self.FUTURE_ID,
            status="active",
            opportunity_score=0.95,
            last_evaluated_at=NOW - timedelta(days=1),
            created_at=NOW - timedelta(days=5),
            updated_at=NOW - timedelta(days=5),
        )
        session.seed_evidence(
            self.FUTURE_ID,
            evidence_id="claim:future-inv",
            relationship="invalidation",
            evidence_fingerprint="e" * 64,
        )
        # Created before the reference but mutated after it: fail closed.
        session.seed_thesis(
            self.MUTATED_ID,
            status="active",
            opportunity_score=0.8,
            last_evaluated_at=NOW - timedelta(days=1),
            created_at=NOW - timedelta(days=30),
            updated_at=NOW - timedelta(days=2),
        )
        result = run_cycle(
            session,
            as_of=self.REPLAY_AT,
            runner=ScriptedRunner(CANDIDATE),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["second_pass_candidates"], 1)
        self.assertEqual(result["second_pass_challenged"], 1)
        self.assertEqual(result["context_affected"], 0)
        # The promoted thesis is created during this run (post-reference) and
        # the two seeded post-reference theses are excluded: 3 total.
        self.assertEqual(result["second_pass_unversioned_excluded"], 3)
        # Only the reference-visible thesis is challenged, falsified, paused.
        self.assertEqual(result["paused_count"], 1)
        self.assertEqual(session.theses[self.PAST_ID]["status"], "paused")
        self.assertEqual(session.theses[self.FUTURE_ID]["status"], "active")
        self.assertEqual(session.theses[self.MUTATED_ID]["status"], "active")
        run_key = f"autonomy:{result['cycle_key']}"
        self.assertIn((self.PAST_ID, run_key), session.falsification_runs)
        self.assertNotIn((self.FUTURE_ID, run_key), session.falsification_runs)
        self.assertNotIn((self.MUTATED_ID, run_key), session.falsification_runs)

    def test_historical_replay_never_selects_newer_score_or_fusion_state(self):
        # Newer current scoring/fusion state cannot enter an older bounded
        # selection: a thesis whose opportunity score was written by an
        # evaluation after the reference (post-reference last_evaluated_at)
        # or which was claimed by a newer fusion cycle (post-reference
        # fusion_reference_at) is never challenged or paused from an older
        # verdict, no matter how high its current score ranks.
        session = self.session = MemorySession()
        self._visible_thesis(self.PAST_ID, opportunity=0.5)
        # Evaluated after the reference: its current score is future state.
        session.seed_thesis(
            self.SCORED_ID,
            status="active",
            opportunity_score=0.99,
            last_evaluated_at=NOW - timedelta(days=1),
            created_at=NOW - timedelta(days=30),
            updated_at=NOW - timedelta(days=30),
        )
        # Claimed by an autonomous fusion cycle after the reference: its
        # current content may reflect the newer claim.
        session.seed_thesis(
            self.FUSED_ID,
            status="active",
            opportunity_score=0.98,
            last_evaluated_at=self.REPLAY_AT - timedelta(days=1),
            fusion_reference_at=NOW - timedelta(days=2),
            created_at=NOW - timedelta(days=30),
            updated_at=NOW - timedelta(days=30),
        )
        selected = _second_pass_candidates(
            session,
            limit=25,
            excluded_ids=[],
            reference=self.REPLAY_AT,
            context_since=self.REPLAY_AT - timedelta(days=7),
        )
        # Only the fully reference-visible thesis is selected, and each
        # selected row carries the optimistic pause tokens.
        self.assertEqual([str(row["id"]) for row in selected], [self.PAST_ID])
        self.assertEqual(selected[0]["status"], "active")
        self.assertIsNotNone(selected[0]["updated_at"])
        self.assertIsNotNone(selected[0]["last_evaluated_at"])
        self.assertIsNone(selected[0]["fusion_reference_at"])
        result = run_cycle(
            session,
            as_of=self.REPLAY_AT,
            runner=ScriptedRunner(CANDIDATE),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["second_pass_candidates"], 1)
        self.assertEqual(result["second_pass_challenged"], 1)
        self.assertEqual(result["paused_count"], 1)
        self.assertEqual(result["second_pass_stale_skipped"], 0)
        # The promoted thesis plus both post-reference-state theses are
        # excluded: 3 total.
        self.assertEqual(result["second_pass_unversioned_excluded"], 3)
        self.assertEqual(session.theses[self.PAST_ID]["status"], "paused")
        self.assertEqual(session.theses[self.SCORED_ID]["status"], "active")
        self.assertEqual(session.theses[self.FUSED_ID]["status"], "active")
        run_key = f"autonomy:{result['cycle_key']}"
        self.assertIn((self.PAST_ID, run_key), session.falsification_runs)
        self.assertNotIn((self.SCORED_ID, run_key), session.falsification_runs)
        self.assertNotIn((self.FUSED_ID, run_key), session.falsification_runs)

    def test_historical_replay_bounds_matches_scenarios_and_attachments(self):
        session = self.session = MemorySession()
        self._visible_thesis(self.PAST_ID, opportunity=0.2)
        playbook = session.seed_playbook(
            self.PAST_ID, created_at=NOW - timedelta(days=30)
        )
        # Context match observed within the recent window and created well
        # before the reference: reference-visible and context-affecting.
        session.seed_context_match(
            playbook["id"],
            "99999999-9999-4999-8999-999999999999",
            observed_at=self.REPLAY_AT - timedelta(days=2),
            created_at=NOW - timedelta(days=30),
        )
        # Same thesis, second match whose row was created after the
        # reference: must not count as context for the replay.
        session.seed_context_match(
            playbook["id"],
            "99999999-9999-4999-8999-888888888888",
            observed_at=self.REPLAY_AT - timedelta(days=1),
            created_at=NOW - timedelta(days=5),
        )
        # Snapshot attachments: one leg and one evidence row visible at the
        # reference, one of each created after it.
        session.scenarios.append(
            {
                "id": _id(f"scenario:{self.PAST_ID}:visible"),
                "thesis_id": self.PAST_ID,
                "name": "visible-leg",
                "probability": 0.5,
                "expected_return": 0.0,
                "is_base_case": True,
                "version": 1,
                "superseded_at": None,
                "created_at": NOW - timedelta(days=30),
            }
        )
        session.scenarios.append(
            {
                "id": _id(f"scenario:{self.PAST_ID}:future"),
                "thesis_id": self.PAST_ID,
                "name": "future-leg",
                "probability": 0.5,
                "expected_return": 0.1,
                "is_base_case": False,
                "version": 1,
                "superseded_at": None,
                "created_at": NOW - timedelta(days=5),
            }
        )
        session.seed_evidence(
            self.PAST_ID,
            evidence_id="claim:past-visible",
            relationship="supports",
            evidence_fingerprint="a" * 64,
            created_at=NOW - timedelta(days=30),
            source_timestamp=NOW - timedelta(days=30),
            available_at=NOW - timedelta(days=30),
        )
        session.seed_evidence(
            self.PAST_ID,
            evidence_id="claim:past-future",
            relationship="supports",
            evidence_fingerprint="b" * 64,
            created_at=NOW - timedelta(days=5),
            source_timestamp=NOW - timedelta(days=5),
            available_at=NOW - timedelta(days=5),
        )
        challenger = RecordingChallenger()
        result = run_cycle(
            session,
            as_of=self.REPLAY_AT,
            runner=ScriptedRunner(CANDIDATE),
            challenger=challenger,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["second_pass_candidates"], 1)
        # Only the reference-visible context match counts.
        self.assertEqual(result["context_affected"], 1)
        snapshot, evidence = next(
            pair for pair in challenger.captured if pair[0].thesis_id == self.PAST_ID
        )
        # Only the reference-visible scenario leg enters the snapshot.
        self.assertEqual(
            [scenario.label for scenario in snapshot.scenarios],
            ["visible-leg"],
        )
        # The post-reference attachment never reaches the challenger; the
        # reference-visible attachment does.
        evidence_ids = {signal.evidence_id for signal in evidence if signal.evidence_id}
        self.assertIn("claim:past-visible", evidence_ids)
        self.assertNotIn("claim:past-future", evidence_ids)

    def test_live_cycle_second_pass_keeps_full_falsification(self):
        # A durable live job (reference = acceptance time) still challenges
        # and pauses reference-visible high-opportunity theses exactly as
        # before: prioritization, challenge isolation, and bounds unchanged.
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            status="active",
            opportunity_score=0.9,
            last_evaluated_at=NOW - timedelta(days=1),
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_id="claim:invalidate",
            relationship="invalidation",
            evidence_fingerprint="d" * 64,
        )
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["second_pass_candidates"], 1)
        self.assertEqual(result["second_pass_challenged"], 1)
        self.assertEqual(result["second_pass_unversioned_excluded"], 0)
        self.assertEqual(result["paused_count"], 1)
        self.assertEqual(session.theses[EXISTING_ID]["status"], "paused")
        run = session.falsification_runs[
            (EXISTING_ID, "autonomy:20260815T093000.000000")
        ]
        self.assertEqual(run["status"], "falsified")

    def test_live_cycle_fails_closed_on_missing_or_future_timestamps(self):
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            status="active",
            opportunity_score=0.9,
            created_at=None,
            updated_at=None,
        )
        session.seed_thesis(
            self.MUTATED_ID,
            status="active",
            opportunity_score=0.8,
            created_at=NOW - timedelta(days=5),
            updated_at=NOW + timedelta(hours=1),
        )
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(result["status"], "completed")
        # Missing lifecycle timestamps cannot prove visibility: excluded.
        self.assertEqual(result["second_pass_candidates"], 0)
        self.assertEqual(result["second_pass_challenged"], 0)
        self.assertEqual(result["second_pass_unversioned_excluded"], 2)
        self.assertEqual(result["paused_count"], 0)
        self.assertEqual(session.theses[EXISTING_ID]["status"], "active")
        self.assertEqual(session.theses[self.MUTATED_ID]["status"], "active")
        run_key = f"autonomy:{result['cycle_key']}"
        self.assertNotIn((EXISTING_ID, run_key), session.falsification_runs)
        self.assertNotIn((self.MUTATED_ID, run_key), session.falsification_runs)

    def test_second_pass_helpers_require_an_explicit_reference(self):
        # A historical path can never invoke the second-pass helpers without
        # making the reference-safety decision: the cutoff is a required
        # keyword argument, so an accidental unversioned call fails closed.
        session = MemorySession()
        with self.assertRaises(TypeError):
            _second_pass_candidates(
                session,
                limit=5,
                excluded_ids=[],
                context_since=NOW - timedelta(days=7),
            )
        with self.assertRaises(TypeError):
            _load_second_pass_snapshot(
                session,
                {
                    "id": EXISTING_ID,
                    "claim": "Existing claim",
                    "direction": "long",
                    "status": "active",
                    "invalidation_conditions": [],
                    "opportunity_score": 0.0,
                    "last_evaluated_at": None,
                },
                cost=0.0,
                cycle_key="20260815T093000.000000",
            )
        with self.assertRaises(TypeError):
            _count_unversioned_second_pass_candidates(session)
        # With the explicit decision both helpers behave deterministically.
        self.assertEqual(
            _second_pass_candidates(
                session,
                limit=5,
                excluded_ids=[],
                reference=NOW,
                context_since=NOW - timedelta(days=7),
            ),
            [],
        )
        self.assertEqual(
            _count_unversioned_second_pass_candidates(session, reference=NOW),
            0,
        )


class RacingSession(MemorySession):
    """MemorySession that mutates one thesis exactly between second-pass
    selection and the conditional pause UPDATE, simulating a concurrent
    or newer cycle landing during model latency.

    ``mutate`` is called with the thesis row immediately before the pause
    executes (identified by the conditional UPDATE's ``updated_at`` token
    parameter); the first-pass unconditional pause never carries that
    parameter and is not intercepted.
    """

    def __init__(self, mutate=None):
        super().__init__()
        self._mutate = mutate

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        if (
            "SET status = 'paused'" in sql
            and params is not None
            and "updated_at" in params
        ):
            row = self.theses.get(str(params["id"]))
            if row is not None and self._mutate is not None:
                self._mutate(row)
        return super().execute(statement, params)


class SecondPassPauseRaceTests(unittest.TestCase):
    """The second-pass pause is an optimistic conditional write.

    The challenge verdict is computed against state visible at the cycle
    reference, but the pause executes after model latency.  A thesis
    re-evaluated, re-fused, or re-stated by a concurrent/newer cycle in
    that window must never be paused from the older verdict (the
    reference-bounded falsification audit still persists); unchanged
    breached theses still pause exactly once.
    """

    def _breached_seed(self, session) -> None:
        session.seed_thesis(
            EXISTING_ID,
            status="active",
            opportunity_score=0.9,
            last_evaluated_at=NOW - timedelta(days=1),
        )
        session.seed_evidence(
            EXISTING_ID,
            evidence_id="claim:invalidate",
            relationship="invalidation",
            evidence_fingerprint="d" * 64,
        )

    def _run_with_racer(self, mutate):
        session = RacingSession(mutate=mutate)
        self._breached_seed(session)
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(result["status"], "completed")
        return session, result

    def test_concurrent_evaluation_between_selection_and_pause_is_a_noop(self):
        # A newer cycle re-evaluates the thesis after selection: the older
        # verdict must not pause the newer score state.
        session, result = self._run_with_racer(
            lambda row: row.update(last_evaluated_at=NOW + timedelta(seconds=5))
        )
        self.assertEqual(result["second_pass_candidates"], 1)
        self.assertEqual(result["second_pass_challenged"], 1)
        self.assertEqual(result["paused_count"], 0)
        self.assertEqual(result["second_pass_stale_skipped"], 1)
        self.assertEqual(session.theses[EXISTING_ID]["status"], "active")
        # The reference-bounded challenge/falsification audit still persists.
        run = session.falsification_runs[
            (EXISTING_ID, "autonomy:20260815T093000.000000")
        ]
        self.assertEqual(run["status"], "falsified")

    def test_concurrent_fusion_claim_between_selection_and_pause_is_a_noop(self):
        # A newer cycle claims/fuses the thesis after the reference:
        # fusion_reference_at moves past it, so the older verdict cannot
        # pause the newer content.
        session, result = self._run_with_racer(
            lambda row: row.update(fusion_reference_at=NOW + timedelta(minutes=1))
        )
        self.assertEqual(result["paused_count"], 0)
        self.assertEqual(result["second_pass_stale_skipped"], 1)
        self.assertEqual(session.theses[EXISTING_ID]["status"], "active")
        self.assertIn(
            (EXISTING_ID, "autonomy:20260815T093000.000000"), session.falsification_runs
        )

    def test_concurrent_status_change_between_selection_and_pause_is_a_noop(self):
        # A newer cycle already moved the thesis (here: paused it): the
        # status token no longer matches, so nothing is paused twice.
        session, result = self._run_with_racer(lambda row: row.update(status="paused"))
        self.assertEqual(result["paused_count"], 0)
        self.assertEqual(result["second_pass_stale_skipped"], 1)
        self.assertEqual(session.theses[EXISTING_ID]["status"], "paused")

    def test_concurrent_row_mutation_between_selection_and_pause_is_a_noop(self):
        # Any other row mutation bumps updated_at past the selected token.
        session, result = self._run_with_racer(
            lambda row: row.update(updated_at=NOW + timedelta(seconds=1))
        )
        self.assertEqual(result["paused_count"], 0)
        self.assertEqual(result["second_pass_stale_skipped"], 1)
        self.assertEqual(session.theses[EXISTING_ID]["status"], "active")

    def test_unchanged_breached_thesis_still_pauses_exactly_once(self):
        # No concurrent change: the optimistic UPDATE applies, stamps
        # updated_at, and the run reports exactly one paused thesis.
        session, result = self._run_with_racer(mutate=None)
        self.assertEqual(result["paused_count"], 1)
        self.assertEqual(result["second_pass_stale_skipped"], 0)
        self.assertEqual(session.theses[EXISTING_ID]["status"], "paused")
        self.assertEqual(session.theses[EXISTING_ID]["updated_at"], NOW)

    def test_same_cycle_contradiction_recompute_still_pauses(self):
        # The second pass itself recomputes scores at the reference when
        # contradictions attach, moving last_evaluated_at exactly to the
        # reference; that same-cycle write stays safe for the pause.
        session = MemorySession()
        self._breached_seed(session)
        proposal = {
            "kind": "counter_evidence",
            "statement": "The cited evidence supports the opposite view",
            "citations": ["source_claim:claim:0005"],
        }
        result = run_cycle(
            session,
            runner=ScriptedRunner(CANDIDATE),
            challenger=ScriptedChallenger(proposal=proposal),
        )
        self.assertEqual(result["status"], "completed")
        # Promoted thesis and second-pass thesis each attach the citation.
        self.assertEqual(result["contradictions_attached"], 2)
        self.assertEqual(result["second_pass_challenged"], 1)
        self.assertEqual(result["paused_count"], 1)
        self.assertEqual(result["second_pass_stale_skipped"], 0)
        self.assertEqual(session.theses[EXISTING_ID]["status"], "paused")
        self.assertEqual(session.theses[EXISTING_ID]["last_evaluated_at"], NOW)


class PlaybookTests(unittest.TestCase):
    def test_playbook_is_built_and_persisted_idempotently(self):
        session = MemorySession()
        first = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(first["playbook_upserts"], 1)
        self.assertEqual(len(session.playbooks), 1)
        active = [row for row in session.playbooks if row["superseded_at"] is None]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["version"], 1)

        second = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(second["playbook_upserts"], 0)
        self.assertEqual(len(session.playbooks), 1)


class EnqueueTests(unittest.TestCase):
    def test_enqueue_uses_shared_job_identity_and_coalesces(self):
        session = RecordingSession()

        class SessionContext:
            def __enter__(self):
                return session

            def __exit__(self, *args):
                return False

        with (
            patch("thesis_autonomy.accept_run", return_value=NOW) as accept,
            patch("thesis_autonomy.start_run", return_value=True),
            patch("thesis_autonomy.finalize_run_safely") as finalize,
            patch("thesis_autonomy.get_session", return_value=SessionContext()),
            patch(
                "thesis_autonomy.enqueue_job",
                return_value=SimpleNamespace(
                    inserted=True,
                    job=SimpleNamespace(id=7, correlation_id="corr-x"),
                ),
            ) as enqueue,
        ):
            result = enqueue_thesis_autonomy_job(
                cycle_config(), triggered_by="scheduler"
            )
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["job_id"], "7")
        self.assertTrue(result["inserted"])
        call = enqueue.call_args
        self.assertEqual(call.kwargs["job_type"], JOB_TYPE)
        self.assertEqual(call.kwargs["dedupe_key"], "thesis-autonomy:global")
        self.assertEqual(
            call.kwargs["payload"],
            {"force": False, "as_of": NOW.isoformat()},
        )
        expected = canonical_fingerprint(
            {
                "job_type": JOB_TYPE,
                "config": thesis_autonomy_identity(cycle_config()),
                "request_date": NOW.date().isoformat(),
                "request_nonce": None,
            }
        )
        self.assertEqual(call.kwargs["input_fingerprint"], expected)
        accept.assert_called_once()
        self.assertEqual(
            accept.call_args.args[2:],
            ("scheduler", "research", "thesis_autonomy"),
        )
        finalize.assert_called_once()
        self.assertEqual(finalize.call_args.kwargs["run_kind"], "research")
        self.assertEqual(finalize.call_args.kwargs["component"], "thesis_autonomy")

    def test_forced_runs_receive_a_unique_identity(self):
        session = RecordingSession()

        class SessionContext:
            def __enter__(self):
                return session

            def __exit__(self, *args):
                return False

        fingerprints = []

        def capture_enqueue(_session, **kwargs):
            fingerprints.append(kwargs["input_fingerprint"])
            return SimpleNamespace(
                inserted=True,
                job=SimpleNamespace(id=1, correlation_id="c"),
            )

        with (
            patch("thesis_autonomy.accept_run", return_value=NOW),
            patch("thesis_autonomy.start_run", return_value=True),
            patch("thesis_autonomy.finalize_run_safely"),
            patch("thesis_autonomy.get_session", return_value=SessionContext()),
            patch("thesis_autonomy.enqueue_job", side_effect=capture_enqueue),
        ):
            enqueue_thesis_autonomy_job(cycle_config(), triggered_by="api", force=False)
            enqueue_thesis_autonomy_job(cycle_config(), triggered_by="api", force=True)
        self.assertEqual(len(fingerprints), 2)
        self.assertNotEqual(fingerprints[0], fingerprints[1])


class HandlerDispatchTests(unittest.TestCase):
    def test_route_job_dispatches_thesis_autonomy_run(self):
        from analysis_job_handlers import route_job

        job = SimpleNamespace(
            job_type=JOB_TYPE,
            correlation_id="corr-1",
            payload={"as_of": NOW.isoformat()},
        )
        with (
            patch("analysis_job_handlers._config", return_value=cycle_config()),
            patch(
                "thesis_autonomy.run_autonomous_thesis_cycle",
                return_value={
                    "status": "completed",
                    "error_count": 0,
                    "cost_usd": 0.25,
                    "promoted_count": 2,
                    "falsification_runs": 1,
                    "source_gate_rejections": 3,
                    "opposition_gate_rejections": 4,
                    "semantic_audit_rejections": 5,
                },
            ) as cycle,
        ):
            result = route_job(RecordingSession(), job)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(result["cost_usd"], 0.25)
        self.assertEqual(result["promoted_count"], 2)
        self.assertEqual(result["source_gate_rejections"], 3)
        self.assertEqual(result["opposition_gate_rejections"], 4)
        self.assertEqual(result["semantic_audit_rejections"], 5)
        cycle.assert_called_once()
        self.assertEqual(cycle.call_args.kwargs["correlation_id"], "corr-1")
        self.assertEqual(cycle.call_args.kwargs["as_of"], NOW.isoformat())


if __name__ == '__main__':
    unittest.main()
