import asyncio
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api_budgets import get_budget_status  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from routes.json.triggers import trigger_cycle, trigger_process  # noqa: E402
from run_lifecycle import RunAcceptanceConflict  # noqa: E402


def _request():
    """Build a direct-call request with a mock client."""
    return Mock(client=Mock(host="testclient"))


class BudgetEnforcementTests(unittest.TestCase):
    @patch(
        "api_budgets.query_one",
        side_effect=[
            {"total_cost": 2.0, "total_tokens": 100},
            {"spent_usd": 2.0, "reserved_usd": 0.0},
        ],
    )
    def test_exact_daily_cap_reaches_hard_limit(self, query_one):
        status = get_budget_status(
            {"budgets": {"daily_llm_usd": 2.0, "warn_at_pct": 80}}
        )

        self.assertTrue(status["hard_limit_reached"])
        self.assertFalse(status["paid_calls_allowed"])
        self.assertEqual(status["remaining_usd"], 0.0)

    @patch("routes.json.triggers.accept_and_enqueue_operation")
    @patch("routes.json.triggers.get_budget_status")
    def test_denied_processor_is_not_dispatched(
        self, budget_status, accept_and_enqueue
    ):
        budget_status.return_value = {
            "available": True,
            "paid_calls_allowed": False,
            "today_cost_usd": 2.0,
            "budget_cap_usd": 2.0,
        }

        request = _request()
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(trigger_process("briefing", request, None))

        self.assertEqual(raised.exception.status_code, 429)
        accept_and_enqueue.assert_not_called()

    @patch("routes.json.triggers.accept_and_enqueue_operation")
    @patch("routes.json.triggers.get_budget_status")
    def test_unavailable_budget_status_fails_closed_without_override(
        self, budget_status, accept_and_enqueue
    ):
        budget_status.return_value = {
            "available": False,
            "status": "unavailable",
        }

        request = _request()
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(trigger_process("briefing", request, None))

        self.assertEqual(raised.exception.status_code, 503)
        accept_and_enqueue.assert_not_called()

    @patch("routes.json.triggers.accept_and_enqueue_operation")
    @patch("routes.json.triggers.register_manual_override")
    @patch("routes.json.triggers.get_budget_status")
    def test_override_proceeds_even_when_budget_status_unavailable(
        self,
        budget_status,
        register_override,
        accept_and_enqueue,
    ):
        budget_status.return_value = {
            "available": False,
            "status": "unavailable",
        }
        register_override.return_value = {
            "requested": True,
            "reason": "manual review",
            "scope": "one_run",
        }
        accept_and_enqueue.return_value = (
            datetime(2026, 8, 4, 0, 0, 0),
            Mock(job=Mock(correlation_id="123")),
        )
        request = _request()

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

        self.assertEqual(response["job_id"], "123")
        accept_and_enqueue.assert_called_once()

    @patch("routes.json.triggers.accept_and_enqueue_operation")
    @patch("routes.json.triggers.register_manual_override")
    @patch("routes.json.triggers.get_budget_status")
    def test_explicit_override_is_registered_and_dispatched(
        self,
        budget_status,
        register_override,
        accept_and_enqueue,
    ):
        budget_status.return_value = {
            "available": True,
            "paid_calls_allowed": False,
            "today_cost_usd": 2.0,
            "budget_cap_usd": 2.0,
        }
        register_override.return_value = {
            "requested": True,
            "reason": "manual review",
            "scope": "one_run",
        }
        accept_and_enqueue.return_value = (
            datetime(2026, 8, 4, 0, 0, 0),
            Mock(job=Mock(correlation_id="123")),
        )
        request = _request()

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
        accept_and_enqueue.assert_called_once()

    @patch("routes.json.triggers.mark_override_dispatch_failed")
    @patch("routes.json.triggers.accept_and_enqueue_operation")
    @patch("routes.json.triggers.register_manual_override")
    @patch("routes.json.triggers.get_budget_status")
    def test_enqueue_failure_revokes_override(
        self, budget_status, register_override, accept_and_enqueue, mark_failed
    ):
        budget_status.return_value = {
            "available": True,
            "paid_calls_allowed": True,
        }
        register_override.return_value = {
            "requested": True,
            "reason": "manual review",
            "scope": "one_run",
        }
        accept_and_enqueue.side_effect = RuntimeError("queue connection failed")
        request = _request()

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(
                trigger_process(
                    "briefing",
                    request,
                    {
                        "budget_override": True,
                        "override_reason": "manual review",
                    },
                )
            )

        self.assertEqual(raised.exception.status_code, 503)
        mark_failed.assert_called_once()
        self.assertIn("correlation_id", raised.exception.detail)

    @patch("routes.json.triggers.mark_override_dispatch_failed")
    @patch("routes.json.triggers.accept_and_enqueue_operation")
    @patch("routes.json.triggers.register_manual_override")
    @patch("routes.json.triggers.get_budget_status")
    def test_acceptance_conflict_revokes_override(
        self, budget_status, register_override, accept_and_enqueue, mark_failed
    ):
        budget_status.return_value = {
            "available": True,
            "paid_calls_allowed": True,
        }
        register_override.return_value = {
            "requested": True,
            "reason": "manual review",
            "scope": "one_run",
        }
        accept_and_enqueue.side_effect = RunAcceptanceConflict("already running")
        request = _request()

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(
                trigger_process(
                    "briefing",
                    request,
                    {
                        "budget_override": True,
                        "override_reason": "manual review",
                    },
                )
            )

        self.assertEqual(raised.exception.status_code, 409)
        mark_failed.assert_called_once()
        self.assertEqual(raised.exception.detail, "already running")

    @patch("routes.json.triggers.get_budget_status")
    def test_override_reason_must_be_string_not_coerced(self, budget_status):
        for reason in (123, ["nested"], {"nested": True}, None):
            with self.subTest(reason=reason):
                request = _request()
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(
                        trigger_process(
                            "briefing",
                            request,
                            {
                                "budget_override": True,
                                "override_reason": reason,
                            },
                        )
                    )
                self.assertEqual(raised.exception.status_code, 422)
                self.assertNotIn(str(reason), str(raised.exception.detail))
        budget_status.assert_not_called()

    @patch("routes.json.triggers.get_budget_status")
    def test_override_reason_length_is_bounded(self, budget_status):
        request = _request()
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(
                trigger_process(
                    "briefing",
                    request,
                    {
                        "budget_override": True,
                        "override_reason": "x" * 501,
                    },
                )
            )
        self.assertEqual(raised.exception.status_code, 422)
        self.assertNotIn("x" * 501, str(raised.exception.detail))
        budget_status.assert_not_called()

    @patch("routes.json.triggers.get_budget_status")
    def test_budget_override_flag_must_be_boolean(self, budget_status):
        for flag in (1, "true", "yes", []):
            with self.subTest(flag=flag):
                request = _request()
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(
                        trigger_process(
                            "briefing",
                            request,
                            {
                                "budget_override": flag,
                                "override_reason": "manual review",
                            },
                        )
                    )
                self.assertEqual(raised.exception.status_code, 422)
        budget_status.assert_not_called()

    @patch("routes.json.triggers.get_budget_status")
    def test_override_requires_reason(self, budget_status):
        request = _request()
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(
                trigger_cycle(
                    request,
                    {"budget_override": True},
                )
            )

        self.assertEqual(raised.exception.status_code, 422)
        budget_status.assert_not_called()

    @patch("routes.json.triggers.accept_and_enqueue_operation")
    @patch("routes.json.triggers.get_budget_status")
    def test_cycle_dispatches_collectors_when_budget_is_exhausted(
        self,
        budget_status,
        accept_and_enqueue,
    ):
        budget_status.return_value = {
            "paid_calls_allowed": False,
            "today_cost_usd": 2.0,
            "budget_cap_usd": 2.0,
        }
        accept_and_enqueue.return_value = (
            datetime(2026, 8, 4, 0, 0, 0),
            Mock(job=Mock(correlation_id="123")),
        )
        request = _request()

        response = asyncio.run(trigger_cycle(request, None))

        self.assertEqual(response["job_id"], "123")
        self.assertFalse(response["budget"]["paid_calls_allowed"])
        accept_and_enqueue.assert_called_once()


if __name__ == "__main__":
    unittest.main()
