import math
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from budgets import (
    BudgetContext,
    BudgetExceeded,
    BudgetPermit,
    BudgetUnavailable,
    ManualBudgetAuthorization,
    budget_status,
    get_today_spend,
    mint_trusted_manual_authorization,
    trusted_manual_budget_context,
    utc_day_bounds,
)
from llm_client import LLMStage, call_llm


class BudgetPolicyTests(unittest.TestCase):
    def test_utc_day_spend_query_is_half_open_and_null_safe(self):
        now = datetime(2026, 7, 15, 23, 59, tzinfo=UTC)
        session = Mock()
        row = {"total_cost": None, "total_tokens": None}
        session.execute.return_value.mappings.return_value.one_or_none.return_value = (
            row
        )
        with patch("budgets.get_session") as get_session:
            get_session.return_value.__enter__.return_value = session
            self.assertEqual(get_today_spend({}, now=now), (0.0, 0))

        sql, params = session.execute.call_args.args
        self.assertIn("started_at >= :today_start", str(sql))
        self.assertIn("started_at < :tomorrow_start", str(sql))
        self.assertIn("COALESCE(tokens_input, 0)", str(sql))
        self.assertEqual(params["today_start"], datetime(2026, 7, 15, tzinfo=UTC))
        self.assertEqual(params["tomorrow_start"], datetime(2026, 7, 16, tzinfo=UTC))
        self.assertEqual(
            utc_day_bounds(now), (params["today_start"], params["tomorrow_start"])
        )

    def test_boundary_policy_and_unlimited_semantics(self):
        below = budget_status(1.99, 2.0, 80)
        self.assertFalse(below["exceeded"])
        self.assertTrue(below["warning"])
        exact = budget_status(2.0, 2.0, 80)
        above = budget_status(2.01, 2.0, 80)
        self.assertTrue(exact["exceeded"])
        self.assertTrue(above["exceeded"])
        for cap in (0, -1):
            unlimited = budget_status(999, cap, 80)
            self.assertTrue(unlimited["unlimited"])
            self.assertFalse(unlimited["exceeded"])
            self.assertFalse(unlimited["warning"])

    def test_invalid_caps_fail_closed(self):
        for cap in ("bad", None, math.nan, math.inf, -math.inf, True):
            with self.subTest(cap=cap):
                with self.assertRaises(ValueError):
                    budget_status(0, cap, 80)


class LLMEnforcementTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "llm": {
                "api_key": "top-secret",
                "default_model": "provider/model",
                "max_retries": 1,
            },
            "budgets": {"daily_llm_usd": 2.0, "warn_at_pct": 80},
        }

    @patch("llm_client.make_request")
    @patch("budgets.get_today_spend", return_value=(1.99, 10))
    def test_below_cap_permits_http(self, _spend, make_request):
        response = Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {},
        }
        make_request.return_value = response
        self.assertEqual(call_llm("prompt", config=self.config)["content"], "ok")
        make_request.assert_called_once()

    @patch("llm_client.make_request")
    def test_at_or_above_cap_blocks_before_http_without_attempt_or_secrets(
        self, make_request
    ):
        for spend in (2.0, 2.01):
            with (
                self.subTest(spend=spend),
                patch("budgets.get_today_spend", return_value=(spend, 10)),
            ):
                stage = LLMStage(self.config, "briefing")
                with self.assertRaises(BudgetExceeded) as raised:
                    stage.call("private prompt")
                self.assertEqual(raised.exception.code, "daily_llm_budget_exceeded")
                self.assertEqual(raised.exception.telemetry.attempt_count, 0)
                self.assertNotIn("secret", str(raised.exception).lower())
                self.assertNotIn("private", str(raised.exception).lower())
        make_request.assert_not_called()

    @patch("llm_client.make_request")
    @patch("budgets.get_today_spend", side_effect=RuntimeError("raw db password"))
    def test_spend_failure_fails_closed_typed_before_http(self, _spend, make_request):
        stage = LLMStage(self.config, "briefing")
        with self.assertRaises(BudgetUnavailable) as raised:
            stage.call("private prompt")
        self.assertEqual(raised.exception.code, "daily_llm_budget_unavailable")
        self.assertEqual(raised.exception.telemetry.attempt_count, 0)
        self.assertNotIn("password", str(raised.exception).lower())
        make_request.assert_not_called()

    @patch("llm_client.make_request")
    @patch("budgets.get_today_spend", return_value=(99.0, 10))
    def test_public_flags_alone_do_not_bypass(self, _spend, make_request):
        for context in (
            BudgetContext(force=True),
            BudgetContext(manual_authorized=True),
            BudgetContext(force=True, manual_authorized=True),
        ):
            with self.subTest(context=context), self.assertRaises(BudgetExceeded):
                call_llm("prompt", config=self.config, budget_context=context)
        make_request.assert_not_called()

    @patch("llm_client.make_request")
    @patch("budgets.get_today_spend", return_value=(99.0, 10))
    def test_trusted_manual_force_bypasses(self, _spend, make_request):
        response = Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {},
        }
        make_request.return_value = response
        authorization = mint_trusted_manual_authorization()
        context = trusted_manual_budget_context(
            force=True,
            manual_authorized=True,
            authorization=authorization,
        )
        self.assertEqual(
            call_llm("prompt", config=self.config, budget_context=context)["content"],
            "ok",
        )
        make_request.assert_called_once()

    def test_manual_flags_and_forged_capabilities_cannot_bypass(self):
        authorization = mint_trusted_manual_authorization()
        with self.assertRaises(ValueError):
            trusted_manual_budget_context(
                force=False,
                manual_authorized=True,
                authorization=authorization,
            )
        for forged in (None, object(), {}, "authorized", ManualBudgetAuthorization()):
            with self.subTest(forged=forged), self.assertRaises(ValueError):
                trusted_manual_budget_context(
                    force=True,
                    manual_authorized=True,
                    authorization=forged,
                )
        self.assertFalse(
            BudgetContext(force=True, manual_authorized=True).trusted_manual_force
        )

    @patch("llm_client.make_request")
    @patch("budgets.get_today_spend", return_value=(99.0, 10))
    def test_public_budget_permit_constructor_cannot_bypass(self, _spend, make_request):
        with self.assertRaises(BudgetExceeded):
            call_llm("prompt", config=self.config, _budget_permit=BudgetPermit())
        make_request.assert_not_called()

    @patch("llm_client.make_request")
    def test_invalid_cap_is_typed_unavailable_before_http(self, make_request):
        for cap in ("bad", None, math.nan, math.inf, -math.inf, True):
            config = {
                **self.config,
                "budgets": {"daily_llm_usd": cap, "warn_at_pct": 80},
            }
            with self.subTest(cap=cap), self.assertRaises(BudgetUnavailable) as raised:
                call_llm("prompt", config=config)
            self.assertEqual(str(raised.exception), "daily LLM budget unavailable")
        make_request.assert_not_called()

    @patch("llm_client.call_llm")
    @patch("budgets.get_today_spend", return_value=(1.0, 10))
    def test_stage_checks_budget_once_and_retry_keeps_attempt_telemetry(
        self, spend, request
    ):
        request.side_effect = [{"content": "bad"}, {"content": "ok"}]
        stage = LLMStage(self.config, "briefing")
        stage.call("first")
        stage.call("validation retry")
        self.assertEqual(spend.call_count, 1)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(stage.telemetry.attempt_count, 2)
        self.assertIs(
            request.call_args_list[0].kwargs["_budget_permit"],
            request.call_args_list[1].kwargs["_budget_permit"],
        )

    @patch("llm_client.make_request")
    @patch("budgets.get_today_spend")
    def test_nonpositive_cap_is_explicitly_unlimited(self, spend, make_request):
        response = Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {},
        }
        make_request.return_value = response
        for cap in (0, -1):
            self.config["budgets"]["daily_llm_usd"] = cap
            call_llm("prompt", config=self.config)
        spend.assert_not_called()
        self.assertEqual(make_request.call_count, 2)


class RuntimeBudgetOutcomeTests(unittest.TestCase):
    def test_processor_budget_block_is_safe_and_persisted_separately(self):
        import orchestrator

        processor = Mock()
        processor.process.side_effect = BudgetExceeded(2.0, 2.0, processor="briefing")
        with (
            patch.object(orchestrator, "get_processor", return_value=processor),
            patch.object(orchestrator, "_write_processing_log") as write_log,
        ):
            result = orchestrator._run_processor_impl(
                "briefing", config={}, correlation_id="cid", manage_lifecycle=False
            )

        self.assertEqual(result["status"], "budget_blocked")
        self.assertEqual(result["error"], "daily LLM budget reached")
        self.assertEqual(write_log.call_args.kwargs["status"], "budget_blocked")
        self.assertEqual(
            write_log.call_args.kwargs["error_message"], "daily LLM budget reached"
        )
        self.assertNotIn("traceback", str(result).lower())

    def test_processor_budget_lookup_failure_is_safe_and_distinct(self):
        import orchestrator

        processor = Mock()
        processor.process.side_effect = BudgetUnavailable(processor="macro_regime")
        with (
            patch.object(orchestrator, "get_processor", return_value=processor),
            patch.object(orchestrator, "_write_processing_log"),
        ):
            result = orchestrator._run_processor_impl(
                "macro_regime", config={}, correlation_id="cid", manage_lifecycle=False
            )
        self.assertEqual(result["status"], "budget_unavailable")
        self.assertEqual(result["error"], "daily LLM budget unavailable")

    def test_cycle_aggregation_preserves_budget_outcomes(self):
        import orchestrator

        self.assertEqual(
            orchestrator.aggregate_stage_statuses(["budget_blocked"]), "budget_blocked"
        )
        self.assertEqual(
            orchestrator.aggregate_stage_statuses(["budget_unavailable"]),
            "budget_unavailable",
        )
        self.assertEqual(
            orchestrator.aggregate_stage_statuses(["success", "budget_blocked"]),
            "partial",
        )
        self.assertEqual(
            orchestrator.aggregate_stage_statuses(
                ["budget_blocked", "budget_unavailable"]
            ),
            "partial",
        )

    def test_event_impact_llm_call_uses_budgeted_stage_and_propagates_context(self):
        from processors.event_impact import EventImpactProcessor

        processor = EventImpactProcessor()
        context = BudgetContext(force=True)
        stage = Mock()
        stage.policy.model = "provider/event"
        stage.call.return_value = {
            "content": '{"events": [], "overall_volatility_outlook": "", "risk_management_note": ""}',
            "model": "provider/event",
        }
        stage.telemetry.as_dict.return_value = {"attempt_count": 1}
        stage.telemetry.tokens_input_total = 0
        stage.telemetry.tokens_output_total = 0
        stage.telemetry.cost_usd_total = 0.0
        with (
            patch.object(
                processor,
                "_fetch_upcoming_events",
                return_value=[{"event_name": "CPI"}],
            ),
            patch.object(processor, "_format_watchlist", return_value="EURUSD"),
            patch.object(processor, "_get_current_regime", return_value="neutral"),
            patch.object(processor, "_build_prompt", return_value="prompt"),
            patch(
                "processors.event_impact.LLMStage", return_value=stage
            ) as stage_factory,
        ):
            result = processor.process({}, "cid", budget_context=context)

        self.assertIs(stage_factory.call_args.kwargs["budget_context"], context)
        stage.call.assert_called_once_with("prompt")
        self.assertEqual(result["processing_log"]["input_summary"]["attempt_count"], 1)

    def test_default_and_scheduler_paths_do_not_create_trusted_force(self):
        import orchestrator
        import scheduler

        processor = Mock()
        processor.process.side_effect = BudgetExceeded(2.0, 2.0, processor="briefing")
        with (
            patch.object(orchestrator, "get_processor", return_value=processor),
            patch.object(orchestrator, "_write_processing_log"),
        ):
            orchestrator._run_processor_impl(
                "briefing", config={}, correlation_id="cid", manage_lifecycle=False
            )
        self.assertIsNone(processor.process.call_args.kwargs["budget_context"])

        scheduled_processor = Mock(get_depends_on=Mock(return_value=["fred"]))
        with (
            patch.object(
                scheduler, "run_collector", return_value={"status": "success"}
            ),
            patch("processors.get_processor", return_value=scheduled_processor),
            patch.object(
                scheduler, "run_processor", return_value={"status": "budget_blocked"}
            ) as run,
        ):
            scheduler._run_scheduled_collector_stages(
                "fred",
                {
                    "processors": {
                        "macro_regime": {
                            "enabled": True,
                            "schedule": "after_dependency",
                        }
                    }
                },
                "cid",
            )
        self.assertNotIn("budget_context", run.call_args.kwargs)

    def test_budget_blocked_dependency_is_recorded_as_clear_skip(self):
        import orchestrator

        processors = {
            "macro_regime": Mock(get_depends_on=Mock(return_value=["fred"])),
            "briefing": Mock(get_depends_on=Mock(return_value=["macro_regime"])),
        }
        config = {"processors": {name: {"enabled": True} for name in processors}}
        with (
            patch.object(orchestrator, "get_all_processors", return_value=processors),
            patch.object(
                orchestrator,
                "run_processor",
                return_value={"processor": "macro_regime", "status": "budget_blocked"},
            ),
        ):
            results = orchestrator._resolve_and_run_processors(config, "cid", {"fred"})
        self.assertEqual(results["macro_regime"]["status"], "budget_blocked")
        self.assertEqual(results["briefing"]["status"], "skipped")
        self.assertIn("macro_regime", results["briefing"]["reason"])


if __name__ == "__main__":
    unittest.main()
