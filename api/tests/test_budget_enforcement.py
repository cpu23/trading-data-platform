import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException  # noqa: E402

from budgets import get_budget_status  # noqa: E402
from routes.json.triggers import trigger_cycle, trigger_process  # noqa: E402


def _request_with_client(client):
    """Build a direct-call request whose app exposes the shared client."""
    request = Mock(client=Mock(host="testclient"))
    request.app.state.orchestrator_client = client
    return request


class BudgetEnforcementTests(unittest.TestCase):
    @patch(
        "budgets.query_one",
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

    @patch("routes.json.triggers.get_budget_status")
    def test_denied_processor_is_not_dispatched(self, budget_status):
        budget_status.return_value = {
            "available": True,
            "paid_calls_allowed": False,
            "today_cost_usd": 2.0,
            "budget_cap_usd": 2.0,
        }

        client = AsyncMock()
        request = _request_with_client(client)
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(trigger_process("briefing", request, None))

        self.assertEqual(raised.exception.status_code, 429)
        client.post.assert_not_awaited()

    @patch("routes.json.triggers.get_budget_status")
    def test_unavailable_budget_status_fails_closed_without_override(
        self, budget_status
    ):
        budget_status.return_value = {
            "available": False,
            "status": "unavailable",
        }

        client = AsyncMock()
        request = _request_with_client(client)
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(trigger_process("briefing", request, None))

        self.assertEqual(raised.exception.status_code, 503)
        client.post.assert_not_awaited()

    @patch("routes.json.triggers.register_manual_override")
    @patch("routes.json.triggers.get_budget_status")
    def test_override_proceeds_even_when_budget_status_unavailable(
        self,
        budget_status,
        register_override,
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
        orchestrator_response = Mock(status_code=202)
        orchestrator_response.json.return_value = {
            "job_id": "123",
            "accepted_at": "now",
        }
        client = AsyncMock()
        client.post = AsyncMock(return_value=orchestrator_response)
        request = _request_with_client(client)

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
        client.post.assert_awaited_once()

    @patch("routes.json.triggers.register_manual_override")
    @patch("routes.json.triggers.get_budget_status")
    def test_explicit_override_is_registered_and_dispatched(
        self,
        budget_status,
        register_override,
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
        orchestrator_response = Mock(status_code=202)
        orchestrator_response.json.return_value = {
            "job_id": "123",
            "accepted_at": "now",
        }
        client = AsyncMock()
        client.post = AsyncMock(return_value=orchestrator_response)
        request = _request_with_client(client)

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
        client.post.assert_awaited_once()

    @patch("routes.json.triggers.mark_override_dispatch_failed")
    @patch("routes.json.triggers.register_manual_override")
    @patch("routes.json.triggers.get_budget_status")
    def test_ambiguous_transport_error_does_not_revoke_override(
        self, budget_status, register_override, mark_failed
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
        client = AsyncMock()
        client.post = AsyncMock(side_effect=httpx.ReadTimeout("read timed out"))
        request = _request_with_client(client)

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
        mark_failed.assert_not_called()
        self.assertIn("correlation_id", raised.exception.detail)

    @patch("routes.json.triggers.mark_override_dispatch_failed")
    @patch("routes.json.triggers.register_manual_override")
    @patch("routes.json.triggers.get_budget_status")
    def test_connect_error_revokes_override(
        self, budget_status, register_override, mark_failed
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
        client = AsyncMock()
        client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        request = _request_with_client(client)

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
    @patch("routes.json.triggers.register_manual_override")
    @patch("routes.json.triggers.get_budget_status")
    def test_http_5xx_response_keeps_override_active(
        self, budget_status, register_override, mark_failed
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
        orchestrator_response = Mock(status_code=500)
        client = AsyncMock()
        client.post = AsyncMock(return_value=orchestrator_response)
        request = _request_with_client(client)

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

        self.assertEqual(raised.exception.status_code, 502)
        mark_failed.assert_not_called()
        self.assertIn("correlation_id", raised.exception.detail)

    @patch("routes.json.triggers.mark_override_dispatch_failed")
    @patch("routes.json.triggers.register_manual_override")
    @patch("routes.json.triggers.get_budget_status")
    def test_validated_4xx_rejection_revokes_override(
        self, budget_status, register_override, mark_failed
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
        orchestrator_response = Mock(status_code=409)
        client = AsyncMock()
        client.post = AsyncMock(return_value=orchestrator_response)
        request = _request_with_client(client)

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

        self.assertEqual(raised.exception.status_code, 502)
        mark_failed.assert_called_once()

    @patch("routes.json.triggers.mark_override_dispatch_failed")
    @patch("routes.json.triggers.register_manual_override")
    @patch("routes.json.triggers.get_budget_status")
    def test_malformed_202_body_keeps_override_active(
        self, budget_status, register_override, mark_failed
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
        orchestrator_response = Mock(status_code=202)
        orchestrator_response.json.return_value = {"unexpected": True}
        client = AsyncMock()
        client.post = AsyncMock(return_value=orchestrator_response)
        request = _request_with_client(client)

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

        self.assertEqual(raised.exception.status_code, 502)
        mark_failed.assert_not_called()

    @patch("routes.json.triggers.get_budget_status")
    def test_override_reason_must_be_string_not_coerced(self, budget_status):
        for reason in (123, ["nested"], {"nested": True}, None):
            with self.subTest(reason=reason):
                request = Mock(client=Mock(host="testclient"))
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
        request = Mock(client=Mock(host="testclient"))
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
                request = Mock(client=Mock(host="testclient"))
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
    def test_cycle_dispatches_collectors_when_budget_is_exhausted(
        self,
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
        client = AsyncMock()
        client.post = AsyncMock(return_value=orchestrator_response)
        request = _request_with_client(client)

        response = asyncio.run(trigger_cycle(request, None))

        self.assertEqual(response["job_id"], "123")
        self.assertFalse(response["budget"]["paid_calls_allowed"])
        client.post.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
