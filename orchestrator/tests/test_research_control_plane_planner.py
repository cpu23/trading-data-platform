import itertools
import math
import os
import sys
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DEPLOYMENT_MODE", "test")

from research_control_plane.domain import (  # noqa: E402
    PriorityInputs,
    QuestionCandidate,
    QuestionForPlanning,
    QuestionStatus,
    question_fingerprint,
    validate_question_transition,
)
from research_control_plane.planner import (  # noqa: E402
    PlanPolicy,
    plan_questions,
    score_priority,
)

NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)


def candidate(**overrides):
    values = {
        "origin_kind": "source_event",
        "question_type": "earnings_guidance_delta",
        "atomic_question": "Did ACME change full-year revenue guidance?",
        "target_kind": "entity",
        "target_ref": "acme",
        "accepted_cutoff": NOW,
        "required_evidence_shape": {"currency": "USD", "period": "FY2026"},
        "acceptable_source_families": ("regulatory_filing", "issuer_material"),
    }
    values.update(overrides)
    return QuestionCandidate(**values)


def priority(**overrides):
    values = {
        "materiality": Decimal("0.8"),
        "uncertainty": Decimal("0.5"),
        "discrimination_power": Decimal("0.9"),
        "urgency": Decimal("0.7"),
        "freshness_gap": Decimal("0.6"),
        "resolvability": Decimal("0.8"),
        "expected_cost_usd": Decimal("0.10"),
        "expected_runtime_seconds": 30,
        "expected_human_review_minutes": Decimal("0"),
    }
    values.update(overrides)
    return PriorityInputs(**values)


def question(index: int, **overrides):
    values = {
        "id": UUID(int=index),
        "accepted_cutoff": NOW,
        "priority": priority(),
        "status": QuestionStatus.PENDING,
        "not_before": NOW,
        "due_at": NOW + timedelta(hours=index),
        "expires_at": NOW + timedelta(days=2),
        "created_at": NOW - timedelta(hours=1),
    }
    values.update(overrides)
    return QuestionForPlanning(**values)


class QuestionDomainTests(unittest.TestCase):
    def test_fingerprint_is_canonical_and_cutoff_sensitive(self):
        first = candidate(
            atomic_question="  Did   ACME change full-year revenue guidance?  ",
            required_evidence_shape={"period": "FY2026", "currency": "USD"},
            acceptable_source_families=("issuer_material", "regulatory_filing"),
        )
        equivalent = candidate()
        later = candidate(accepted_cutoff=NOW + timedelta(seconds=1))
        other_origin = candidate(origin_kind="stale_dependency")

        self.assertEqual(question_fingerprint(first), question_fingerprint(equivalent))
        self.assertEqual(question_fingerprint(first), question_fingerprint(other_origin))
        self.assertNotEqual(question_fingerprint(first), question_fingerprint(later))
        self.assertRegex(question_fingerprint(first), r"^[0-9a-f]{64}$")

    def test_question_bounds_and_terminal_transitions(self):
        for bad in (
            {"atomic_question": ""},
            {"atomic_question": "x" * 2001},
            {"accepted_cutoff": datetime(2026, 8, 23)},
            {"acceptable_source_families": tuple(str(i) for i in range(33))},
        ):
            with self.subTest(bad=next(iter(bad))):
                with self.assertRaises(ValueError):
                    candidate(**bad)

        self.assertEqual(
            validate_question_transition(QuestionStatus.PENDING, QuestionStatus.PLANNED),
            QuestionStatus.PLANNED,
        )
        with self.assertRaisesRegex(ValueError, "terminal"):
            validate_question_transition(
                QuestionStatus.RESOLVED, QuestionStatus.PENDING
            )
        with self.assertRaisesRegex(ValueError, "invalid"):
            validate_question_transition(
                QuestionStatus.PENDING, QuestionStatus.RESOLVED
            )


class PriorityPolicyTests(unittest.TestCase):
    def test_unknowns_block_while_valid_zero_stays_zero(self):
        unknown = score_priority(priority(materiality=None))
        zero = score_priority(priority(materiality=Decimal("0")))

        self.assertIsNone(unknown.score)
        self.assertIn("materiality_unknown", unknown.blockers)
        self.assertEqual(zero.score, Decimal("0"))
        self.assertEqual(zero.blockers, ())

    def test_nonfinite_and_out_of_range_values_are_rejected(self):
        for value in (math.nan, math.inf, -math.inf, -0.01, 1.01):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    priority(materiality=value)
        with self.assertRaises(ValueError):
            priority(expected_runtime_seconds=-1)

    def test_scoring_is_deterministic_and_policy_versioned(self):
        result = score_priority(priority())
        expected = (
            Decimal("0.8")
            * Decimal("0.5")
            * Decimal("0.9")
            * Decimal("0.7")
            * Decimal("0.6")
            * Decimal("0.8")
        ) / (Decimal("0.10") + Decimal("0.001") * 30 + Decimal("0.001"))
        self.assertEqual(result.score, expected)
        self.assertEqual(result.policy_version, "v1")


class AgendaPlannerTests(unittest.TestCase):
    def policy(self, **overrides):
        values = {
            "now": NOW,
            "cost_budget_usd": Decimal("1.00"),
            "runtime_budget_seconds": 300,
            "maximum_work_orders": 8,
            "minimum_priority": Decimal("0"),
        }
        values.update(overrides)
        return PlanPolicy(**values)

    def test_order_is_permutation_invariant_and_uuid_breaks_exact_ties(self):
        questions = [question(3), question(1), question(2)]
        orders = set()
        for permutation in itertools.permutations(questions):
            agenda = plan_questions(permutation, self.policy())
            orders.add(tuple(item.question_id for item in agenda.selected))
        self.assertEqual(orders, {(UUID(int=1), UUID(int=2), UUID(int=3))})

    def test_dual_budgets_defer_without_blocking_lower_cost_work(self):
        expensive = question(
            1,
            priority=priority(
                expected_cost_usd=Decimal("0.90"), expected_runtime_seconds=290
            ),
        )
        affordable = question(
            2,
            priority=priority(
                expected_cost_usd=Decimal("0.20"), expected_runtime_seconds=20
            ),
        )
        third = question(
            3,
            priority=priority(
                expected_cost_usd=Decimal("0.20"), expected_runtime_seconds=20
            ),
        )
        agenda = plan_questions(
            [expensive, affordable, third],
            self.policy(cost_budget_usd=Decimal("0.40"), runtime_budget_seconds=40),
        )

        self.assertEqual(
            {item.question_id for item in agenda.selected}, {UUID(int=2), UUID(int=3)}
        )
        deferred = {item.question_id: item.reason_codes for item in agenda.deferred}
        self.assertIn("cost_budget_exceeded", deferred[UUID(int=1)])
        self.assertIn("runtime_budget_exceeded", deferred[UUID(int=1)])
        self.assertEqual(agenda.reserved_cost_usd, Decimal("0.40"))
        self.assertEqual(agenda.reserved_runtime_seconds, 40)

    def test_blockers_not_before_expiry_minimum_and_noop_are_explicit(self):
        blocked = question(1, priority=priority(uncertainty=None))
        future = question(2, not_before=NOW + timedelta(minutes=1))
        expired = question(3, due_at=None, expires_at=NOW)
        below = question(4, priority=priority(materiality=Decimal("0")))
        agenda = plan_questions(
            [blocked, future, expired, below],
            self.policy(minimum_priority=Decimal("0.01")),
        )

        self.assertEqual(agenda.selected, ())
        reasons = {
            item.question_id: set(item.reason_codes)
            for item in (*agenda.blocked, *agenda.deferred)
        }
        self.assertIn("uncertainty_unknown", reasons[UUID(int=1)])
        self.assertIn("not_before", reasons[UUID(int=2)])
        self.assertIn("expired", reasons[UUID(int=3)])
        self.assertIn("below_minimum_priority", reasons[UUID(int=4)])
        self.assertEqual(agenda.no_op_reason, "no_eligible_questions")

        empty = plan_questions([], self.policy())
        self.assertEqual(empty.no_op_reason, "no_questions")
        self.assertEqual(empty.decisions, ())


if __name__ == "__main__":
    unittest.main()
