import math
import unittest
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError

from contracts import (
    ResearchControlPlaneRunRequest,
    ResearchPrioritySnapshot,
    ResearchQuestionListResponse,
    SystemTopologyNode,
    SystemTopologyResponse,
)


class ResearchControlPlaneContractTests(unittest.TestCase):
    def test_unknown_priority_components_remain_distinct_from_zero(self):
        unknown = ResearchPrioritySnapshot(policy_version="v1")
        zero = ResearchPrioritySnapshot(
            policy_version="v1",
            materiality=0,
            uncertainty=0,
            discrimination_power=0,
            urgency=0,
            freshness_gap=0,
            resolvability=0,
            expected_cost_usd=0,
            expected_runtime_seconds=0,
            expected_human_review_minutes=0,
            score=0,
        )

        self.assertIsNone(unknown.materiality)
        self.assertEqual(zero.materiality, 0)
        self.assertEqual(zero.expected_cost_usd, 0)
        self.assertEqual(zero.score, 0)

    def test_nonfinite_priority_values_and_unbounded_lists_are_rejected(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    ResearchPrioritySnapshot(policy_version="v1", materiality=value)

        with self.assertRaises(ValidationError):
            ResearchQuestionListResponse(items=[], limit=101)

    def test_manual_budget_override_requires_a_bounded_reason(self):
        with self.assertRaisesRegex(ValidationError, "override_reason"):
            ResearchControlPlaneRunRequest(reason="operator drill", budget_override=True)
        with self.assertRaisesRegex(ValidationError, "requires budget_override"):
            ResearchControlPlaneRunRequest(
                reason="operator drill", override_reason="not requested"
            )
        request = ResearchControlPlaneRunRequest(
            reason="operator drill",
            budget_override=True,
            override_reason="incident review",
        )
        self.assertTrue(request.budget_override)

    def test_topology_is_bounded_strict_and_timezone_safe(self):
        node = SystemTopologyNode(
            id="planner",
            label="Planner",
            group="Research",
            kind="planner",
            status="idle",
            activity_state="no eligible questions",
            bounded_count=0,
            safe_detail="Latest persisted plan selected no work.",
        )
        response = SystemTopologyResponse(
            generated_at=datetime.now(UTC),
            status="available",
            nodes=[node],
            edges=[],
            summary="One bounded topology node.",
        )
        self.assertEqual(response.nodes[0].bounded_count, 0)

        with self.assertRaisesRegex(ValidationError, "timezone-aware"):
            SystemTopologyResponse(
                generated_at=datetime.now(),
                status="available",
                nodes=[],
                edges=[],
                summary="Unavailable.",
            )
        with self.assertRaisesRegex(ValidationError, "extra_forbidden"):
            SystemTopologyNode(
                id="planner",
                label="Planner",
                group="Research",
                kind="planner",
                status="idle",
                activity_state="idle",
                safe_detail="No work.",
                private_payload={"token": str(uuid4())},
            )


if __name__ == "__main__":
    unittest.main()
