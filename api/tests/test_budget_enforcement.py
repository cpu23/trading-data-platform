import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException  # noqa: E402
from budgets import get_budget_status  # noqa: E402
from routes.json.triggers import trigger_cycle, trigger_process  # noqa: E402


class BudgetEnforcementTests(unittest.TestCase):
    @patch(
        "budgets.query_one",
        return_value={"total_cost": 2.0, "total_tokens": 100},
    )
    def test_exact_daily_cap_reaches_hard_limit(self, query_one):
        status = get_budget_status(
            {"budgets": {"daily_llm_usd": 2.0, "warn_at_pct": 80}}
        )

        self.assertTrue(status["hard_limit_reached"])
        self.assertFalse(status["paid_calls_allowed"])
        self.assertEqual(status["remaining_usd"], 0.0)

    @patch("routes.json.triggers.get_budget_status")
    @patch("routes.json.triggers.httpx.AsyncClient")
    def test_denied_processor_is_not_dispatched(self, async_client, budget_status):
        budget_status.return_value = {
            "paid_calls_allowed": False,
            "today_cost_usd": 2.0,
            "budget_cap_usd": 2.0,
        }

        request = Mock(client=Mock(host="testclient"))
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(trigger_process("briefing", request, None))

        self.assertEqual(raised.exception.status_code, 429)
        async_client.assert_not_called()

    @patch("routes.json.triggers.register_manual_override")
    @patch("routes.json.triggers.get_budget_status")
    @patch("routes.json.triggers.httpx.AsyncClient")
    def test_explicit_override_is_registered_and_dispatched(
        self,
        async_client,
        budget_status,
        register_override,
    ):
        budget_status.return_value = {
            "paid_calls_allowed": False,
            "today_cost_usd": 2.0,
            "budget_cap_usd": 2.0,
        }
        register_override.return_value = {
            "requested": True,
            "reason": "manual review",
            "scope": "one_run",
        }
        orchestrator_response = Mock(status_code=202)
        orchestrator_response.json.return_value = {
            "job_id": "123",
            "accepted_at": "now",
        }
        async_client.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=orchestrator_response
        )
        request = Mock(client=Mock(host="testclient"))

        response = asyncio.run(
            trigger_process(
                "briefing",
                request,
                {
                "budget_override": True,
                "override_reason": "manual review",
                },
            )
        )

        register_override.assert_called_once()
        self.assertTrue(response["budget_override"]["requested"])

    @patch("routes.json.triggers.get_budget_status")
    def test_override_requires_reason(self, budget_status):
        request = Mock(client=Mock(host="testclient"))
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(
                trigger_cycle(
                    request,
                    {"budget_override": True},
                )
            )

        self.assertEqual(raised.exception.status_code, 422)
        budget_status.assert_not_called()

    @patch("routes.json.triggers.get_budget_status")
    @patch("routes.json.triggers.httpx.AsyncClient")
    def test_cycle_dispatches_collectors_when_budget_is_exhausted(
        self,
        async_client,
        budget_status,
    ):
        budget_status.return_value = {
            "paid_calls_allowed": False,
            "today_cost_usd": 2.0,
            "budget_cap_usd": 2.0,
        }
        orchestrator_response = Mock(status_code=202)
        orchestrator_response.json.return_value = {
            "job_id": "123",
            "accepted_at": "now",
        }
        post = AsyncMock(return_value=orchestrator_response)
        async_client.return_value.__aenter__.return_value.post = post

        response = asyncio.run(
            trigger_cycle(Mock(client=Mock(host="testclient")), None)
        )

        self.assertEqual(response["job_id"], "123")
        self.assertFalse(response["budget"]["paid_calls_allowed"])
        post.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
