"""Tests for autonomous thesis cycle execution, identity, proposal staging, and dispatch."""

import copy
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ORCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH_ROOT))

from research_intelligence.evidence import EvidenceCollection, EvidenceRegistry
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
    _cycle_key,
    canonical_fingerprint,
    canonical_thesis_key,
    cycle_config,
    enqueue_thesis_autonomy_job,
    evidence_item,
    evidence_items,
    run_autonomous_thesis_cycle,
    run_cycle,
    thesis_autonomy_identity,
)


class AutonomousCycleTests(unittest.TestCase):
    def test_credentialless_cycle_stages_proposals_with_zero_canonical_writes(self):
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
        self.assertEqual(result["raw_candidate_count"], 2)
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["proposals_staged"], 1)
        self.assertEqual(result["proposals_created"], 1)
        self.assertEqual(result["proposals_replayed"], 0)
        self.assertEqual(result["tournament_promoted_count"], 1)
        self.assertEqual(result["role_failures"], 0)
        self.assertEqual(result["challenger_failures"], 0)
        self.assertEqual(result["semantic_audit_rejections"], 0)
        self.assertEqual(result["cost_usd"], 0.02)
        self.assertEqual(result["theme_id"], THEME_ID)
        self.assertTrue(result["theme_created"])
        self.assertEqual(result["cycle_key"], "20260815T093000.000000")

        # Zero canonical writes
        self.assertEqual(len(session.theses), 0)
        self.assertEqual(session.scenarios, [])
        self.assertEqual(session.risks, [])
        self.assertEqual(session.groups, {})
        self.assertEqual(session.catalysts, [])
        self.assertEqual(session.evidence, [])
        self.assertEqual(session.position_links, [])

        # Staged review proposal
        self.assertEqual(len(session.proposals), 1)
        proposal = next(iter(session.proposals.values()))
        self.assertEqual(proposal["status"], "pending_review")
        self.assertEqual(proposal["company"], "acme-corporation")
        self.assertEqual(proposal["symbol"], "ACME")
        self.assertEqual(proposal["subject"], CANDIDATE["subject"])
        self.assertEqual(proposal["direction"], "long")
        self.assertEqual(proposal["horizon"], "months")
        self.assertEqual(len(proposal["scenarios"]), 3)
        self.assertEqual(
            {s["name"] for s in proposal["scenarios"]}, {"bull", "base", "bear"}
        )
        base = next(s for s in proposal["scenarios"] if s["name"] == "base")
        self.assertTrue(base["is_base_case"])
        self.assertEqual(
            {s["expected_return"] for s in proposal["scenarios"]}, {0.1, 0.0, -0.2}
        )
        self.assertEqual(len(proposal["evidence"]), 3)
        self.assertTrue(
            all(e["relationship"] == "supports" for e in proposal["evidence"])
        )
        self.assertTrue(all(e["excerpt"] for e in proposal["evidence"]))
        self.assertEqual(proposal["challenge"]["state"], "intact")
        self.assertEqual(proposal["scoring"]["opportunity_status"], "blocked")
        self.assertEqual(runner.calls, 2)
        self.assertEqual(auditor.calls, 1)

    def test_nullable_scenario_probabilities_stage_as_unknown(self):
        candidate = copy.deepcopy(CANDIDATE)
        candidate["scenarios"]["base"]["probability"] = None
        session = MemorySession()
        result = run_cycle(session, runner=ScriptedRunner(candidate))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["proposals_staged"], 1)
        self.assertEqual(result["proposals_created"], 1)
        self.assertEqual(result["proposals_replayed"], 0)
        self.assertEqual(len(session.proposals), 1)
        proposal = next(iter(session.proposals.values()))
        base = next(s for s in proposal["scenarios"] if s["name"] == "base")
        self.assertIsNone(base["probability"])
        self.assertEqual(proposal["challenge"]["state"], "threatened")
        self.assertEqual(session.theses, {})

    def test_rerun_with_identical_inputs_creates_no_duplicate_identities(self):
        session = MemorySession()
        runner = ScriptedRunner(CANDIDATE)
        first = run_cycle(session, runner=runner, challenger=ScriptedChallenger())
        second = run_cycle(session, runner=runner, challenger=ScriptedChallenger())

        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["error_count"], 0)
        self.assertEqual(second["promoted_count"], 0)
        self.assertEqual(second["proposals_staged"], 1)
        self.assertEqual(second["proposals_created"], 0)
        self.assertEqual(second["proposals_replayed"], 1)
        self.assertFalse(second["theme_created"])

        self.assertEqual(len(session.proposals), 1)
        self.assertEqual(len(session.theses), 0)
        self.assertEqual(first["cycle_key"], second["cycle_key"])

    def test_current_cycle_scores_its_own_staged_proposals_at_cutoff(self):
        session = MemorySession()
        session.market_data["ACME"] = [(NOW - timedelta(hours=1), 100.0)]
        result = run_cycle(
            session,
            as_of=NOW - timedelta(seconds=1),
            runner=ScriptedRunner(CANDIDATE),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["proposals_staged"], 1)
        self.assertEqual(result["proposals_created"], 1)
        self.assertEqual(result["proposals_replayed"], 0)
        self.assertEqual(len(session.proposals), 1)
        proposal = next(iter(session.proposals.values()))
        self.assertAlmostEqual(proposal["scoring"]["expected_value"], -0.01)
        self.assertAlmostEqual(proposal["scoring"]["expected_shortfall"], 0.04)
        self.assertEqual(proposal["scoring"]["catalyst_score"], 0.5)
        self.assertGreater(proposal["scoring"]["evidence_strength"], 0.0)
        self.assertEqual(proposal["scoring"]["opportunity_score"], 0.0)
        self.assertEqual(proposal["scoring"]["opportunity_status"], "blocked")
        self.assertEqual(len(session.theses), 0)

    def test_role_failure_is_isolated_and_other_roles_still_stage_proposals(self):
        session = MemorySession()
        runner = ScriptedRunner(CANDIDATE, fail_roles={"contrarian"})
        result = run_cycle(session, runner=runner)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(result["role_failures"], 1)
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["proposals_staged"], 1)
        self.assertEqual(result["proposals_created"], 1)
        self.assertEqual(result["proposals_replayed"], 0)
        self.assertEqual(len(session.proposals), 1)
        self.assertEqual(len(session.theses), 0)

    def test_all_roles_failing_still_returns_bounded_result(self):
        class FailingRunner:
            cost_usd = 0.0
            calls = 0

            def run(self, *, role, prompt, schema):
                raise RuntimeError("simulated outage")

        session = MemorySession()
        result = run_cycle(session, runner=FailingRunner())
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["role_failures"], 2)
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["proposals_staged"], 0)
        self.assertEqual(result["proposals_created"], 0)
        self.assertEqual(result["proposals_replayed"], 0)
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(len(session.proposals), 0)
        self.assertEqual(len(session.theses), 0)

    def test_challenger_failure_fails_closed_before_staging(self):
        session = MemorySession()
        result = run_cycle(
            session,
            runner=ScriptedRunner(CANDIDATE),
            challenger=ScriptedChallenger(fail=True),
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["challenger_failures"], 1)
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["proposals_staged"], 0)
        self.assertEqual(result["proposals_created"], 0)
        self.assertEqual(result["proposals_replayed"], 0)
        self.assertEqual(len(session.proposals), 0)
        self.assertEqual(len(session.theses), 0)

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
        self.assertEqual(result["proposals_staged"], 0)
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
        self.assertEqual(result["proposals_staged"], 0)
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
        self.assertEqual(result["proposals_staged"], 0)
        self.assertEqual(result["challenger_failures"], 1)
        self.assertEqual(result["opposition_gate_rejections"], 1)
        self.assertEqual(result["challenge_attempts"], 2)
        self.assertEqual(session.theses, {})
        self.assertEqual(len(session.proposals), 0)

    def test_opposing_variants_stage_proposals(self):
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
        self.assertEqual(result["tournament_promoted_count"], 2)
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["proposals_staged"], 2)
        self.assertEqual(result["proposals_created"], 2)
        self.assertEqual(result["proposals_replayed"], 0)
        self.assertEqual(len(session.proposals), 2)
        self.assertEqual(
            {p["direction"] for p in session.proposals.values()}, {"long", "short"}
        )
        self.assertEqual(len(session.theses), 0)

    def test_auditor_gates_staging_and_failures_reject_the_batch(self):
        session = MemorySession()
        result = run_cycle(
            session,
            runner=ScriptedRunner(CANDIDATE),
            auditor=ScriptedAuditor(verdict="unsupported"),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["semantic_audit_rejections"], 1)
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["proposals_staged"], 0)
        self.assertEqual(result["proposals_created"], 0)
        self.assertEqual(result["proposals_replayed"], 0)
        self.assertEqual(len(session.proposals), 0)
        self.assertEqual(len(session.theses), 0)

        session2 = MemorySession()
        result2 = run_cycle(
            session2,
            runner=ScriptedRunner(CANDIDATE),
            auditor=ScriptedAuditor(fail=True),
        )
        self.assertEqual(result2["status"], "completed")
        self.assertEqual(result2["promoted_count"], 0)
        self.assertEqual(result2["proposals_staged"], 0)
        self.assertEqual(result2["proposals_created"], 0)
        self.assertEqual(result2["proposals_replayed"], 0)
        self.assertEqual(result2["error_count"], 0)
        self.assertEqual(len(session2.proposals), 0)
        self.assertEqual(len(session2.theses), 0)

    def test_challenger_citations_attach_as_contradicts_and_score_recomputes(self):
        session = MemorySession()
        proposal_input = {
            "kind": "counter_evidence",
            "statement": "The cited evidence supports the opposite view",
            "citations": ["source_claim:claim:0005"],
        }
        result = run_cycle(
            session,
            runner=ScriptedRunner(CANDIDATE),
            challenger=ScriptedChallenger(proposal=proposal_input),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["challenger_failures"], 0)
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["proposals_staged"], 1)
        self.assertEqual(result["proposals_created"], 1)
        self.assertEqual(result["proposals_replayed"], 0)
        self.assertEqual(len(session.proposals), 1)
        prop = next(iter(session.proposals.values()))
        self.assertEqual(prop["challenge"]["state"], "threatened")
        self.assertEqual(len(prop["challenge"]["contradiction_refs"]), 1)
        self.assertGreater(prop["scoring"]["contradiction_strength"], 0.0)
        self.assertEqual(len(prop["evidence"]), 4)
        self.assertEqual(len(session.theses), 0)

    def test_staged_proposal_diff_against_seeded_canonical_thesis(self):
        session = MemorySession()
        canonical_key = canonical_thesis_key(
            theme_id=THEME_ID,
            subject=CANDIDATE["subject"],
            direction=CANDIDATE["direction"],
            horizon=CANDIDATE["horizon"],
            mechanism=CANDIDATE["mechanism"],
        )
        session.seed_thesis(
            EXISTING_ID,
            canonical_key=canonical_key,
            subject=CANDIDATE["subject"],
            claim="Old claim text",
            version=3,
        )
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["proposals_staged"], 1)
        self.assertEqual(result["proposals_created"], 1)
        self.assertEqual(result["proposals_replayed"], 0)
        self.assertEqual(len(session.theses), 1)
        self.assertEqual(len(session.proposals), 1)
        proposal = next(iter(session.proposals.values()))
        self.assertEqual(proposal["status"], "pending_review")
        self.assertEqual(proposal["canonical_key"], canonical_key)
        self.assertIsNotNone(proposal.get("diff"))
        self.assertEqual(proposal["diff"].get("matching_thesis_id"), EXISTING_ID)
        self.assertEqual(proposal["diff"].get("existing_version"), 3)
        self.assertEqual(proposal["diff"]["claim"]["old"], "Old claim text")
        self.assertEqual(proposal["diff"]["claim"]["new"], CANDIDATE["claim"])


class CandidateRiskStagingTests(unittest.TestCase):
    def test_blank_scenario_description_rejects_staging(self):
        candidate = copy.deepcopy(CANDIDATE)
        candidate["scenarios"]["bull"]["description"] = "   "
        session = MemorySession()
        result = run_cycle(session, runner=ScriptedRunner(candidate))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["proposals_staged"], 0)
        self.assertEqual(result["proposals_created"], 0)
        self.assertEqual(result["proposals_replayed"], 0)
        self.assertEqual(session.theses, {})
        self.assertEqual(session.scenarios, [])
        self.assertEqual(session.risks, [])
        self.assertEqual(len(session.proposals), 0)

    def test_empty_invalidators_reject_staging(self):
        candidate = copy.deepcopy(CANDIDATE)
        candidate["invalidators"] = []
        session = MemorySession()
        result = run_cycle(session, runner=ScriptedRunner(candidate))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["proposals_staged"], 0)
        self.assertEqual(result["proposals_created"], 0)
        self.assertEqual(result["proposals_replayed"], 0)
        self.assertEqual(session.theses, {})
        self.assertEqual(session.risks, [])
        self.assertEqual(len(session.proposals), 0)


class CycleIdentityTests(unittest.TestCase):
    def test_cycle_key_renders_fixed_full_precision_utc(self):
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
        self.assertEqual(
            _cycle_key(datetime.fromisoformat("2026-08-15T11:30:10+02:00")),
            "20260815T093010.000000",
        )
        self.assertEqual(
            _cycle_key(datetime(2026, 8, 15, 9, 30, 10)),
            "20260815T093010.000000",
        )
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
        self.assertEqual(first["promoted_count"], 0)
        self.assertEqual(second["promoted_count"], 0)
        self.assertEqual(first["proposals_staged"], 1)
        self.assertEqual(second["proposals_staged"], 1)
        self.assertEqual(len(session.theses), 0)
        self.assertEqual(len(session.proposals), 2)
        keys = {p["proposal_key"] for p in session.proposals.values()}
        self.assertEqual(len(keys), 2)

    def test_fractional_microsecond_references_stay_distinct(self):
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
        self.assertEqual(first["promoted_count"], 0)
        self.assertEqual(second["promoted_count"], 0)
        self.assertEqual(first["proposals_staged"], 1)
        self.assertEqual(second["proposals_staged"], 1)
        self.assertEqual(len(session.theses), 0)
        self.assertEqual(len(session.proposals), 2)

    def test_exact_reference_rerun_coalesces_on_the_same_audit_keys(self):
        session = MemorySession()
        first = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        second = run_cycle(session, runner=ScriptedRunner(CANDIDATE))

        self.assertEqual(first["cycle_key"], "20260815T093000.000000")
        self.assertEqual(first["cycle_key"], second["cycle_key"])
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["error_count"], 0)
        self.assertEqual(first["promoted_count"], 0)
        self.assertEqual(second["promoted_count"], 0)
        self.assertEqual(first["proposals_staged"], 1)
        self.assertEqual(first["proposals_created"], 1)
        self.assertEqual(first["proposals_replayed"], 0)
        self.assertEqual(second["proposals_staged"], 1)
        self.assertEqual(second["proposals_created"], 0)
        self.assertEqual(second["proposals_replayed"], 1)
        self.assertEqual(len(session.theses), 0)
        self.assertEqual(len(session.proposals), 1)


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
                    "promoted_count": 0,
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
        self.assertEqual(result["promoted_count"], 0)
        self.assertEqual(result["source_gate_rejections"], 3)
        self.assertEqual(result["opposition_gate_rejections"], 4)
        self.assertEqual(result["semantic_audit_rejections"], 5)
        cycle.assert_called_once()
        self.assertEqual(cycle.call_args.kwargs["correlation_id"], "corr-1")
        self.assertEqual(cycle.call_args.kwargs["as_of"], NOW.isoformat())


if __name__ == "__main__":
    unittest.main()
