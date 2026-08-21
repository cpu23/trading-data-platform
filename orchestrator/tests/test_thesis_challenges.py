import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research_intelligence.contracts import EvidenceSignal, Scenario
from thesis_challenges import (
    MAX_CLAIMS,
    MAX_EVIDENCE,
    ChallengeDecision,
    ChallengeProposal,
    CitationFailure,
    RequiredData,
    ThesisClaim,
    ThesisCondition,
    ThesisSnapshot,
    audit_citations,
    audit_falsification,
    challenge_thesis,
    derive_priority,
)
from thesis_scoring import assess_evidence, scenario_valuation

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def signal(**overrides):
    base = {
        "evidence_id": "ev-1",
        "evidence_type": "source_claim",
        "relationship": "supports",
        "source_name": "reuters",
        "content": {"claim": "demand is accelerating"},
        "source_timestamp": NOW,
    }
    base.update(overrides)
    return EvidenceSignal.create(**base)


def condition(**overrides):
    base = {
        "condition_id": "c-1",
        "kind": "numeric",
        "operator": ">=",
        "threshold": 10.0,
        "observed": 12.0,
    }
    base.update(overrides)
    return ThesisCondition.create(**base)


def claim(**overrides):
    base = {"claim_id": "cl-1", "statement": "revenue will exceed guidance"}
    base.update(overrides)
    return ThesisClaim.create(**base)


def scenario(**overrides):
    base = {"label": "base", "probability": 1.0, "expected_return": 0.1}
    base.update(overrides)
    return Scenario.create(**base)


def snapshot(**overrides):
    base = {
        "thesis_id": "th-1",
        "statement": "upside from demand acceleration",
        "as_of": NOW,
        "conditions": (),
        "scenarios": (),
        "claims": (),
    }
    base.update(overrides)
    return ThesisSnapshot.create(**base)


def decide(
    evidence,
    *,
    conditions=(),
    scenarios=(),
    claims=(),
    known_models=None,
    runner=None,
    **snap_overrides,
):
    snap = snapshot(
        conditions=conditions,
        scenarios=scenarios,
        claims=claims,
        **snap_overrides,
    )
    return challenge_thesis(snap, evidence, known_models=known_models, runner=runner)


class IntactBaselineTests(unittest.TestCase):
    def test_clean_thesis_is_intact_with_low_priority(self):
        decision = decide(
            [signal()],
            conditions=(condition(),),
            scenarios=(scenario(),),
            claims=(claim(citations=["ev-1"]),),
        )
        self.assertEqual(decision.state, "intact")
        self.assertEqual(decision.recommended_priority, "low")
        self.assertEqual(decision.invalidation_ids, ())
        self.assertEqual(decision.breached_condition_ids, ())
        self.assertEqual(decision.citation_failures, ())
        self.assertEqual(decision.required_data, ())
        self.assertEqual(decision.contradiction_strength, 0.0)
        self.assertFalse(decision.runner_failed)
        self.assertIsNone(decision.runner_error)
        self.assertIsInstance(decision, ChallengeDecision)

    def test_decision_carries_valuation_from_scenario_valuation(self):
        scenarios = (
            scenario(),
            scenario(label="down", probability=0.0, expected_return=-0.2),
        )
        decision = decide([signal()], scenarios=scenarios)
        expected = scenario_valuation(scenarios)
        self.assertEqual(decision.valuation.expected_value, expected.expected_value)
        self.assertEqual(decision.valuation.probability_sum, 1.0)
        self.assertTrue(decision.valuation.probabilities_sum_to_one)

    def test_contradiction_strength_reused_from_assess_evidence(self):
        contested = [signal(evidence_id="ev-c", relationship="contradicts")]
        decision = decide(contested)
        self.assertEqual(
            decision.contradiction_strength,
            assess_evidence(contested).contradiction_mass,
        )


class ConditionBreachBoundaryTests(unittest.TestCase):
    def test_inclusive_numeric_operators_hold_at_exact_boundary(self):
        for operator in (">=", "<="):
            decision = decide(
                [signal()],
                conditions=(condition(operator=operator, observed=10.0),),
            )
            self.assertEqual(decision.state, "intact", operator)
            self.assertEqual(decision.breached_condition_ids, (), operator)

    def test_exclusive_numeric_operators_breach_at_exact_boundary(self):
        for operator in (">", "<"):
            decision = decide(
                [signal()],
                conditions=(condition(operator=operator, observed=10.0),),
            )
            self.assertEqual(decision.state, "breached", operator)
            self.assertEqual(decision.breached_condition_ids, ("c-1",), operator)

    def test_equality_operator_boundary(self):
        equal = decide(
            [signal()],
            conditions=(condition(operator="==", observed=10.0),),
        )
        self.assertEqual(equal.state, "intact")
        off = decide(
            [signal()],
            conditions=(condition(operator="==", observed=10.0 + 1e-9),),
        )
        self.assertEqual(off.state, "breached")
        not_equal = decide(
            [signal()],
            conditions=(condition(operator="!=", observed=10.0),),
        )
        self.assertEqual(not_equal.state, "breached")

    def test_breach_side_of_threshold(self):
        below = decide(
            [signal()],
            conditions=(condition(operator=">=", observed=9.99),),
        )
        self.assertEqual(below.state, "breached")
        above = decide(
            [signal()],
            conditions=(condition(operator="<=", observed=10.01),),
        )
        self.assertEqual(above.state, "breached")

    def test_date_condition_boundary(self):
        base = dict(
            condition_id="c-date",
            kind="date",
            operator="<=",
            threshold="2026-09-01",
        )
        on_time = decide(
            [signal()],
            conditions=(condition(**base, observed="2026-09-01"),),
        )
        self.assertEqual(on_time.state, "intact")
        late = decide(
            [signal()],
            conditions=(condition(**base, observed="2026-09-02"),),
        )
        self.assertEqual(late.state, "breached")
        self.assertEqual(late.breached_condition_ids, ("c-date",))
        strict = decide(
            [signal()],
            conditions=(
                condition(**{**base, "operator": "<", "observed": "2026-09-01"}),
            ),
        )
        self.assertEqual(strict.state, "breached")

    def test_unobserved_numeric_condition_is_required_data_not_breach(self):
        decision = decide(
            [signal()],
            conditions=(condition(observed=None),),
        )
        self.assertEqual(decision.state, "threatened")
        self.assertEqual(decision.breached_condition_ids, ())
        kinds = {item.kind for item in decision.required_data}
        self.assertIn("observation", kinds)
        observation = next(
            item for item in decision.required_data if item.kind == "observation"
        )
        self.assertEqual(observation.refs, ("c-1",))

    def test_date_deadline_passed_unobserved_threatens(self):
        after_deadline = decide(
            [signal()],
            conditions=(
                condition(
                    condition_id="c-date",
                    kind="date",
                    operator="<=",
                    threshold="2026-09-01",
                    observed=None,
                ),
            ),
            as_of="2026-10-01T00:00:00Z",
        )
        self.assertEqual(after_deadline.state, "threatened")
        self.assertEqual(after_deadline.breached_condition_ids, ())
        self.assertEqual(
            [item.kind for item in after_deadline.required_data],
            ["date_observation"],
        )
        before_deadline = decide(
            [signal()],
            conditions=(
                condition(
                    condition_id="c-date",
                    kind="date",
                    operator="<=",
                    threshold="2026-09-01",
                    observed=None,
                ),
            ),
            as_of="2026-08-01T00:00:00Z",
        )
        self.assertEqual(
            [item.kind for item in before_deadline.required_data],
            ["observation"],
        )


class StalenessTests(unittest.TestCase):
    def test_stale_evidence_threatens_but_never_breaches(self):
        stale = signal(source_timestamp=NOW - timedelta(days=91))
        decision = decide([stale])
        self.assertEqual(decision.state, "threatened")
        self.assertNotEqual(decision.state, "breached")
        freshness = next(
            item for item in decision.required_data if item.kind == "freshness"
        )
        self.assertEqual(freshness.refs, ("ev-1",))

    def test_exact_staleness_horizon_is_fresh(self):
        boundary = signal(source_timestamp=NOW - timedelta(days=90))
        decision = decide([boundary])
        self.assertEqual(decision.state, "intact")
        self.assertEqual([item.kind for item in decision.required_data], [])

    def test_stale_evidence_does_not_breach_even_with_contradiction(self):
        stale_contradiction = signal(
            evidence_id="ev-x",
            relationship="contradicts",
            source_timestamp=NOW - timedelta(days=200),
            # Auditable contradiction: verbatim excerpt plus positive
            # scores, so it counts as opposition without breaching.
            quality_score=0.9,
            entailment_score=0.9,
            freshness_score=0.8,
            provenance={"excerpt": "Verbatim counterpoint excerpt."},
        )
        decision = decide([stale_contradiction])
        self.assertEqual(decision.state, "threatened")
        self.assertNotEqual(decision.state, "breached")


class InvalidationTests(unittest.TestCase):
    def test_invalidation_relationship_breaches(self):
        invalidating = signal(relationship="invalidation")
        decision = decide([invalidating])
        self.assertEqual(decision.state, "breached")
        self.assertEqual(decision.invalidation_ids, ("ev-1",))
        self.assertEqual(decision.recommended_priority, "critical")

    def test_invalidation_survives_fingerprint_deduplication(self):
        first = signal(
            evidence_id="ev-a",
            relationship="invalidation",
            content={"claim": "thesis is false"},
        )
        second = signal(
            evidence_id="ev-b",
            relationship="supports",
            content={"claim": "thesis is false"},
        )
        decision = decide([first, second])
        self.assertEqual(decision.state, "breached")
        self.assertEqual(decision.invalidation_ids, ("ev-a",))


class ScenarioDefectTests(unittest.TestCase):
    def test_missing_probability_is_a_defect(self):
        decision = decide(
            [signal()],
            scenarios=(scenario(probability=None),),
        )
        self.assertEqual(decision.state, "threatened")
        probability = next(
            item for item in decision.required_data if item.kind == "probability"
        )
        self.assertEqual(probability.refs, ("base",))
        self.assertEqual(decision.valuation.missing_probability_count, 1)

    def test_probability_sum_deviation_is_a_defect(self):
        decision = decide(
            [signal()],
            scenarios=(
                scenario(label="a", probability=0.4),
                scenario(label="b", probability=0.4),
            ),
        )
        self.assertEqual(decision.state, "threatened")
        normalization = next(
            item for item in decision.required_data if item.kind == "normalization"
        )
        self.assertEqual(normalization.refs, ("a", "b"))
        self.assertFalse(decision.valuation.probabilities_sum_to_one)

    def test_clean_scenarios_never_defect(self):
        decision = decide(
            [signal()],
            scenarios=(scenario(),),
        )
        self.assertEqual(decision.state, "intact")
        self.assertEqual([item.kind for item in decision.required_data], [])


class CitationAuditTests(unittest.TestCase):
    def test_uncited_claim_failure(self):
        decision = decide(
            [signal()],
            claims=(claim(citations=[]),),
        )
        self.assertEqual(decision.state, "threatened")
        failure = decision.citation_failures[0]
        self.assertEqual(failure.claim_id, "cl-1")
        self.assertEqual(failure.reason, "uncited")
        self.assertEqual(failure.refs, ())

    def test_unknown_evidence_citation_failure(self):
        decision = decide(
            [signal()],
            claims=(claim(citations=["missing-id"]),),
        )
        failure = next(
            item
            for item in decision.citation_failures
            if item.reason == "unknown_evidence"
        )
        self.assertEqual(failure.claim_id, "cl-1")
        self.assertEqual(failure.refs, ("missing-id",))

    def test_evidence_ref_form_citation_accepted(self):
        decision = decide(
            [signal()],
            claims=(claim(citations=["source_claim:ev-1"]),),
        )
        self.assertEqual(decision.state, "intact")
        self.assertEqual(decision.citation_failures, ())

    def test_unknown_model_citation_rejected(self):
        modeled = signal(provenance={"model_slug": "ghost-1"})
        decision = decide(
            [modeled],
            claims=(claim(citations=["ev-1"]),),
            known_models={"registered-1"},
        )
        failure = next(
            item
            for item in decision.citation_failures
            if item.reason == "unknown_model"
        )
        self.assertEqual(failure.claim_id, "cl-1")
        self.assertEqual(failure.refs, ("ev-1",))
        self.assertEqual(decision.state, "threatened")

    def test_known_model_citation_accepted(self):
        modeled = signal(provenance={"model_slug": "registered-1"})
        decision = decide(
            [modeled],
            claims=(claim(citations=["ev-1"]),),
            known_models={"registered-1"},
        )
        self.assertEqual(decision.state, "intact")
        self.assertEqual(decision.citation_failures, ())

    def test_source_evidence_without_model_slug_accepted(self):
        decision = decide(
            [signal()],
            claims=(claim(citations=["ev-1"]),),
        )
        self.assertEqual(decision.citation_failures, ())

    def test_duplicate_origin_citations_failure(self):
        first = signal(
            evidence_id="ev-a",
            content={"claim": "story one"},
            origin_key="feed-1",
        )
        second = signal(
            evidence_id="ev-b",
            content={"claim": "story two"},
            origin_key="feed-1",
        )
        decision = decide([first, second])
        failure = next(
            item
            for item in decision.citation_failures
            if item.reason == "duplicate_origin"
        )
        self.assertIsNone(failure.claim_id)
        self.assertEqual(failure.refs, ("ev-a", "ev-b"))
        self.assertEqual(decision.state, "threatened")

    def test_syndicated_same_fingerprint_is_not_duplication(self):
        first = signal(
            evidence_id="ev-a",
            content={"claim": "one story"},
            origin_key="feed-1",
        )
        second = signal(
            evidence_id="ev-b",
            content={"claim": "one story"},
            origin_key="feed-1",
        )
        self.assertEqual(first.evidence_fingerprint, second.evidence_fingerprint)
        decision = decide([first, second])
        self.assertEqual(decision.citation_failures, ())

    def test_distinct_origins_are_not_duplication(self):
        first = signal(
            evidence_id="ev-a",
            content={"claim": "story one"},
            origin_key="feed-1",
        )
        second = signal(
            evidence_id="ev-b",
            content={"claim": "story two"},
            origin_key="feed-2",
        )
        decision = decide([first, second])
        self.assertEqual(decision.citation_failures, ())

    def test_agent_agreement_never_multiplies_failures(self):
        analyst = signal(
            evidence_id="ev-a",
            content={"claim": "identical claim"},
            source_timestamp=NOW - timedelta(days=200),
            origin_key="feed-a",
            provenance={"agent": "analyst-alpha"},
        )
        critic = signal(
            evidence_id="ev-b",
            content={"claim": "identical claim"},
            source_timestamp=NOW - timedelta(days=200),
            origin_key="feed-b",
            provenance={"agent": "analyst-beta"},
        )
        self.assertEqual(analyst.evidence_fingerprint, critic.evidence_fingerprint)
        decision = decide([analyst, critic])
        freshness = next(
            item for item in decision.required_data if item.kind == "freshness"
        )
        self.assertEqual(freshness.refs, ("ev-a",))
        self.assertEqual([f.reason for f in decision.citation_failures], [])

    def test_cited_evidence_count_counts_distinct_resolved_ids(self):
        first = signal(evidence_id="ev-a", content={"claim": "one"})
        second = signal(evidence_id="ev-b", content={"claim": "two"})
        audit = audit_citations(
            snapshot(
                claims=(
                    claim(citations=["ev-a", "ev-b"]),
                    claim(claim_id="cl-2", citations=["ev-a"]),
                )
            ),
            [first, second],
        )
        self.assertEqual(audit.claim_count, 2)
        self.assertEqual(audit.cited_evidence_count, 2)
        self.assertEqual(audit.failures, ())


class BoundedInputTests(unittest.TestCase):
    def test_evidence_truncated_at_limit(self):
        rows = [
            signal(
                evidence_id=f"ev-{index}",
                content={"claim": f"story {index}"},
            )
            for index in range(MAX_EVIDENCE + 1)
        ]
        decision = decide(
            rows,
            claims=(claim(citations=["ev-256"]),),
        )
        failure = next(
            item
            for item in decision.citation_failures
            if item.reason == "unknown_evidence"
        )
        self.assertEqual(failure.refs, ("ev-256",))

    def test_claims_truncated_at_limit(self):
        claims = tuple(
            claim(claim_id=f"cl-{index}", citations=["ev-1"])
            for index in range(MAX_CLAIMS + 5)
        )
        audit = audit_citations(snapshot(claims=claims), [signal()])
        self.assertEqual(audit.claim_count, MAX_CLAIMS)

    def test_no_evidence_is_explicit_required_data(self):
        decision = decide([])
        self.assertEqual(decision.state, "threatened")
        evidence = next(
            item for item in decision.required_data if item.kind == "evidence"
        )
        self.assertIsInstance(evidence, RequiredData)
        self.assertEqual(evidence.refs, ())


class PriorityTests(unittest.TestCase):
    def test_deterministic_priority_repeatable(self):
        evidence = [signal()]
        snap = snapshot(
            conditions=(condition(observed=8.0),),
            scenarios=(scenario(),),
            claims=(claim(citations=["ev-1"]),),
        )
        first = challenge_thesis(snap, evidence)
        second = challenge_thesis(snap, evidence)
        self.assertEqual(first.recommended_priority, second.recommended_priority)
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_breached_priority_critical(self):
        decision = decide(
            [signal()],
            conditions=(condition(observed=8.0),),
        )
        self.assertEqual(decision.state, "breached")
        self.assertEqual(decision.recommended_priority, "critical")

    def test_threatened_contradiction_boundary_high(self):
        self.assertEqual(
            derive_priority(state="threatened", contradiction_strength=0.5),
            "high",
        )
        self.assertEqual(
            derive_priority(state="threatened", contradiction_strength=0.5 - 1e-9),
            "medium",
        )

    def test_threatened_strong_contradiction_high_end_to_end(self):
        contested = [
            signal(
                evidence_id=f"ev-{index}",
                relationship="contradicts",
                content={"claim": f"counterpoint {index}"},
                quality_score=1.0,
                entailment_score=1.0,
                freshness_score=1.0,
                effective_weight=1.0,
                provenance={
                    "excerpt": f"Verbatim counterpoint excerpt {index}.",
                },
            )
            for index in range(2)
        ]
        decision = decide(contested)
        self.assertEqual(decision.state, "threatened")
        self.assertGreaterEqual(decision.contradiction_strength, 0.5)
        self.assertEqual(decision.recommended_priority, "high")

    def test_threatened_citation_failure_high(self):
        decision = decide(
            [signal()],
            claims=(claim(citations=[]),),
        )
        self.assertEqual(decision.state, "threatened")
        self.assertEqual(decision.recommended_priority, "high")

    def test_threatened_plain_medium(self):
        decision = decide([signal(source_timestamp=NOW - timedelta(days=91))])
        self.assertEqual(decision.state, "threatened")
        self.assertEqual(decision.recommended_priority, "medium")

    def test_intact_priority_low(self):
        decision = decide([signal()])
        self.assertEqual(decision.state, "intact")
        self.assertEqual(decision.recommended_priority, "low")

    def test_derive_priority_rejects_invalid_inputs(self):
        with self.assertRaises(ValueError):
            derive_priority(state="maybe")
        with self.assertRaises(ValueError):
            derive_priority(state="intact", contradiction_strength=1.5)
        with self.assertRaises(ValueError):
            derive_priority(state="intact", citation_failure_count=-1)


class RunnerIsolationTests(unittest.TestCase):
    class ExplodingRunner:
        def challenge(self, snapshot, evidence):
            raise RuntimeError("boom")

    class WrongTypeRunner:
        def challenge(self, snapshot, evidence):
            return "not a proposal"

    class UnknownCitationRunner:
        def challenge(self, snapshot, evidence):
            return ChallengeProposal.create(
                kind="counter_evidence",
                statement="story contradicts the thesis",
                citations=["ghost-id"],
            )

    class ValidRunner:
        def challenge(self, snapshot, evidence):
            return ChallengeProposal.create(
                kind="counter_evidence",
                statement="competitor capacity undercuts the thesis",
                citations=["ev-1"],
            )

    class MutatingSnapshotRunner:
        def challenge(self, snapshot, evidence):
            snapshot.conditions = ()

    class MutatingEvidenceRunner:
        def challenge(self, snapshot, evidence):
            evidence[0].evidence_id = "tampered"

    def test_runner_exception_isolated(self):
        evidence = [signal()]
        snap = snapshot()
        plain = challenge_thesis(snap, evidence)
        with_runner = challenge_thesis(snap, evidence, runner=self.ExplodingRunner())
        self.assertTrue(with_runner.runner_failed)
        self.assertTrue(with_runner.runner_error.startswith("RuntimeError:"))
        self.assertEqual(with_runner.state, plain.state)
        self.assertEqual(with_runner.recommended_priority, plain.recommended_priority)
        self.assertEqual(with_runner.runner_findings, ())
        self.assertEqual(with_runner.citation_failures, plain.citation_failures)

    def test_runner_invalid_return_isolated(self):
        decision = challenge_thesis(
            snapshot(), [signal()], runner=self.WrongTypeRunner()
        )
        self.assertTrue(decision.runner_failed)
        self.assertIn("ChallengeProposal", decision.runner_error)

    def test_runner_unknown_citation_rejected(self):
        decision = challenge_thesis(
            snapshot(), [signal()], runner=self.UnknownCitationRunner()
        )
        self.assertTrue(decision.runner_failed)
        self.assertIn("unknown evidence", decision.runner_error)
        rejected = next(
            item
            for item in decision.citation_failures
            if item.reason == "unknown_evidence"
        )
        self.assertEqual(rejected.refs, ("ghost-id",))
        self.assertEqual(decision.runner_findings, ())

    def test_runner_valid_proposal_adds_finding_and_threatens(self):
        decision = challenge_thesis(snapshot(), [signal()], runner=self.ValidRunner())
        self.assertFalse(decision.runner_failed)
        self.assertIsNone(decision.runner_error)
        self.assertEqual(len(decision.runner_findings), 1)
        finding = decision.runner_findings[0]
        self.assertEqual(finding.kind, "counter_evidence")
        self.assertEqual(finding.citations, ("ev-1",))
        self.assertEqual(decision.state, "threatened")

    def test_runner_cannot_mutate_snapshot(self):
        evidence = [signal()]
        snap = snapshot()
        before = snap.to_dict()
        decision = challenge_thesis(
            snap, evidence, runner=self.MutatingSnapshotRunner()
        )
        self.assertTrue(decision.runner_failed)
        self.assertIn("FrozenInstanceError", decision.runner_error)
        self.assertEqual(snap.to_dict(), before)

    def test_runner_cannot_mutate_evidence(self):
        evidence = [signal()]
        before = evidence[0].to_dict()
        decision = challenge_thesis(
            snapshot(), evidence, runner=self.MutatingEvidenceRunner()
        )
        self.assertTrue(decision.runner_failed)
        self.assertIn("FrozenInstanceError", decision.runner_error)
        self.assertEqual(evidence[0].to_dict(), before)

    def test_runner_without_challenge_method_fails_cleanly(self):
        decision = challenge_thesis(snapshot(), [signal()], runner=object())
        self.assertTrue(decision.runner_failed)
        self.assertIn("challenge()", decision.runner_error)


class ImmutabilityAndHistoryTests(unittest.TestCase):
    def test_inputs_unchanged_after_challenge(self):
        evidence = [
            signal(evidence_id="ev-a", content={"claim": "one"}),
            signal(
                evidence_id="ev-b",
                relationship="contradicts",
                content={"claim": "two"},
                origin_key="feed-1",
            ),
        ]
        snap = snapshot(
            conditions=(condition(observed=None),),
            scenarios=(scenario(probability=None),),
            claims=(claim(citations=["ev-a"]),),
        )
        snapshot_before = snap.to_dict()
        evidence_before = [item.to_dict() for item in evidence]
        fingerprint_before = snap.fingerprint

        class Exploding:
            def challenge(self, snapshot, evidence):
                raise RuntimeError("boom")

        challenge_thesis(snap, evidence, runner=Exploding())
        self.assertEqual(snap.to_dict(), snapshot_before)
        self.assertEqual([item.to_dict() for item in evidence], evidence_before)
        self.assertEqual(snap.fingerprint, fingerprint_before)

    def test_decision_is_frozen(self):
        decision = decide([signal()])
        with self.assertRaises(Exception):
            decision.state = "breached"
        failure = CitationFailure.create(claim_id="cl-1", reason="uncited")
        with self.assertRaises(Exception):
            failure.reason = "unknown_evidence"

    def test_repeat_runs_produce_identical_history(self):
        evidence = [signal()]
        snap = snapshot(
            conditions=(condition(observed=8.0),),
            claims=(claim(citations=[]),),
        )
        first = challenge_thesis(snap, evidence)
        second = challenge_thesis(snap, evidence)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.snapshot_fingerprint, snap.fingerprint)
        self.assertEqual(first.as_of, snap.as_of)

    def test_snapshot_fingerprint_anchors_point_in_time(self):
        snap_a = snapshot(statement="thesis version one")
        snap_b = snapshot(statement="thesis version two")
        self.assertNotEqual(snap_a.fingerprint, snap_b.fingerprint)
        self.assertNotEqual(
            challenge_thesis(snap_a, [signal()]).snapshot_fingerprint,
            challenge_thesis(snap_b, [signal()]).snapshot_fingerprint,
        )

    def test_audits_are_independent_contracts(self):
        evidence = [signal()]
        snap = snapshot()
        falsification = audit_falsification(snap, evidence)
        citation = audit_citations(snap, evidence)
        self.assertIsInstance(falsification.invalidation_ids, tuple)
        self.assertIsInstance(citation.failures, tuple)
        self.assertEqual(falsification.contradiction_strength, 0.0)
        self.assertEqual(citation.claim_count, 0)


if __name__ == "__main__":
    unittest.main()
