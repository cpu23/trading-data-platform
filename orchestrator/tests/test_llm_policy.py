import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm_client import (
    LLMAttemptTelemetry,
    LLMStage,
    LLMStageFailure,
    LLMStageTimeout,
    call_llm,
    resolve_request_policy,
)


class SequenceClock:
    def __init__(self, *values):
        self._values = iter(values)

    def __call__(self):
        return next(self._values)


@patch("budgets._reserve_budget_quota", return_value="reservation-1")
class LLMRequestPolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "llm": {
                "provider": "openrouter",
                "api_key": "not-a-real-key",
                "models": {"default": "provider/default"},
                "max_output_tokens": {"macro_regime": 1800, "briefing": 2600},
                "temperatures": {"macro_regime": 0.1, "briefing": 0.3},
                "structured_response": {"macro_regime": True, "briefing": True},
                "max_prices": {"briefing": {"completion": 3.5}},
                "stage_timeout_seconds": 90,
                "validation_retries": 1,
            },
            "budgets": {"daily_llm_usd": 2.0},
        }

    @patch("llm_client.make_request")
    def test_macro_request_uses_its_bounded_structured_policy(
        self, make_request, _reserve
    ):
        response = Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": "{}"}}],
            "model": "provider/macro",
            "usage": {},
        }
        make_request.return_value = response

        call_llm("schema instructions", processor_id="macro_regime", config=self.config)

        request = make_request.call_args.kwargs
        self.assertEqual(request["json_body"]["model"], "provider/default")
        self.assertEqual(request["json_body"]["max_tokens"], 1800)
        self.assertEqual(request["json_body"]["temperature"], 0.1)
        self.assertEqual(
            request["json_body"]["response_format"], {"type": "json_object"}
        )
        self.assertEqual(request["timeout"], 90.0)
        self.assertEqual(request["max_retries"], 1)
        self.assertNotIn("idempotency_key", request)
        self.assertEqual(request["json_body"]["provider"], {"require_parameters": True})

    @patch("llm_client.make_request")
    def test_json_schema_requires_a_supporting_provider(self, make_request, _reserve):
        response = Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": "{}"}}],
            "usage": {},
        }
        make_request.return_value = response
        schema = {
            "name": "result",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        }

        call_llm(
            "exact JSON schema",
            processor_id="briefing",
            config=self.config,
            response_schema=schema,
        )

        body = make_request.call_args.kwargs["json_body"]
        self.assertEqual(
            body["response_format"],
            {"type": "json_schema", "json_schema": schema},
        )
        self.assertEqual(
            body["provider"],
            {"require_parameters": True, "max_price": {"completion": 3.5}},
        )

    @patch("llm_client.make_request")
    def test_stage_can_disable_openrouter_parameter_filtering(
        self, make_request, _reserve
    ):
        response = Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": "{}"}}],
            "usage": {},
        }
        make_request.return_value = response
        self.config["llm"]["structured_response"]["investment_analysis"] = True
        self.config["llm"]["require_parameters"] = {
            "investment_analysis": False,
        }
        self.config["llm"]["api_keys"] = {
            "investment_analysis": "stage-specific-key",
        }
        schema = {
            "name": "result",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        }

        call_llm(
            "exact JSON schema",
            processor_id="investment_analysis",
            config=self.config,
            response_schema=schema,
        )

        body = make_request.call_args.kwargs["json_body"]
        self.assertNotIn("provider", body)
        self.assertEqual(
            body["response_format"],
            {"type": "json_schema", "json_schema": schema},
        )
        self.assertEqual(
            make_request.call_args.kwargs["headers"]["Authorization"],
            "Bearer stage-specific-key",
        )

    @patch("llm_client.make_request")
    def test_briefing_uses_its_own_larger_bounded_policy(self, make_request, _reserve):
        response = Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": "{}"}}],
            "usage": {},
        }
        make_request.return_value = response

        call_llm("exact JSON shape", processor_id="briefing", config=self.config)

        body = make_request.call_args.kwargs["json_body"]
        self.assertEqual(body["model"], "provider/default")
        self.assertEqual(body["max_tokens"], 2600)
        self.assertEqual(body["temperature"], 0.3)
        self.assertEqual(
            body["provider"],
            {"require_parameters": True, "max_price": {"completion": 3.5}},
        )

    def test_unknown_processor_uses_explicit_bounded_defaults(self, _reserve):
        policy = resolve_request_policy(self.config, "new_processor")

        self.assertEqual(policy.model, "provider/default")
        self.assertGreater(policy.max_output_tokens, 0)
        self.assertLessEqual(policy.max_output_tokens, 4096)
        self.assertFalse(policy.structured_response)
        self.assertEqual(policy.validation_retries, 1)

    @patch("llm_client.make_request")
    def test_unsupported_structured_response_is_not_sent(self, make_request, _reserve):
        self.config["llm"]["structured_response"]["macro_regime"] = False
        response = Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": "{}"}}],
            "usage": {},
        }
        make_request.return_value = response

        call_llm("JSON please", processor_id="macro_regime", config=self.config)

        self.assertNotIn("response_format", make_request.call_args.kwargs["json_body"])

    @patch("llm_client.make_request")
    def test_malformed_usage_is_sanitized_to_finite_zeroes(
        self, make_request, _reserve
    ):
        response = Mock()
        response.json.side_effect = [
            {
                "choices": [{"message": {"content": "{}"}}],
                "usage": None,
            },
            {
                "choices": [{"message": {"content": "{}"}}],
                "usage": {
                    "prompt_tokens": "not-a-number",
                    "completion_tokens": -7,
                    "cost": float("nan"),
                },
            },
        ]
        make_request.return_value = response

        results = [
            call_llm("JSON please", processor_id="macro_regime", config=self.config),
            call_llm("JSON please", processor_id="macro_regime", config=self.config),
        ]

        for result in results:
            self.assertEqual(result["tokens_input"], 0)
            self.assertEqual(result["tokens_output"], 0)
            self.assertEqual(result["cost_usd"], 0.0)

    def test_invalid_unbounded_config_fails_closed(self, _reserve):
        self.config["llm"]["max_output_tokens"]["briefing"] = 0
        with self.assertRaisesRegex(ValueError, "max_output_tokens"):
            resolve_request_policy(self.config, "briefing")

    def test_large_bounded_tournament_output_is_supported(self, _reserve):
        policy = resolve_request_policy(
            self.config,
            "thesis_autonomy",
            max_output_tokens=16_384,
        )
        self.assertEqual(policy.max_output_tokens, 16_384)

        with self.assertRaisesRegex(ValueError, "max_output_tokens"):
            resolve_request_policy(
                self.config,
                "thesis_autonomy",
                max_output_tokens=16_385,
            )


@patch("budgets._reserve_budget_quota", return_value="reservation-1")
class LLMStageDeadlineAndTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "llm": {
                "api_key": "not-a-real-key",
                "models": {"default": "provider/default"},
                "max_output_tokens": {"briefing": 2600},
                "temperatures": {"briefing": 0.2},
                "structured_response": {"briefing": True},
                "stage_timeout_seconds": 90,
                "validation_retries": 1,
            },
            "budgets": {"daily_llm_usd": 2.0},
        }

    @patch("llm_client.call_llm")
    def test_retry_uses_remaining_stage_budget_and_records_distinct_attempts(
        self, call, _reserve
    ):
        call.side_effect = [
            {
                "content": "bad",
                "duration_ms": 3000,
                "tokens_input": 11,
                "tokens_output": 7,
                "cost_usd": 0.012,
            },
            {
                "content": "{}",
                "duration_ms": 4000,
                "tokens_input": 13,
                "tokens_output": 5,
                "cost_usd": 0.008,
            },
        ]
        stage = LLMStage(
            self.config,
            "briefing",
            clock=SequenceClock(10, 10, 10, 13, 13, 13, 17),
        )

        stage.call("first")
        stage.add_validation_warnings(["watchlist_notes missing UK100"])
        stage.call("retry")

        self.assertEqual(call.call_args_list[0].kwargs["timeout"], 90.0)
        self.assertEqual(call.call_args_list[1].kwargs["timeout"], 87.0)
        self.assertEqual(stage.telemetry.attempt_count, 2)
        self.assertEqual(stage.telemetry.first_attempt_duration_ms, 3000)
        self.assertEqual(stage.telemetry.validation_retry_duration_ms, 4000)
        self.assertEqual(
            stage.telemetry.validation_warnings, ["watchlist_notes missing UK100"]
        )
        self.assertEqual(stage.telemetry.model, "provider/default")
        self.assertEqual(stage.telemetry.max_output_tokens, 2600)
        self.assertEqual(stage.telemetry.tokens_input_total, 24)
        self.assertEqual(stage.telemetry.tokens_output_total, 12)
        self.assertAlmostEqual(stage.telemetry.cost_usd_total, 0.02)
        self.assertEqual(stage.telemetry.as_dict()["tokens_input_total"], 24)
        self.assertNotIn("content", stage.telemetry.as_dict())

    @patch("llm_client.call_llm")
    def test_exhausted_budget_raises_typed_timeout_without_retry_request(
        self, call, _reserve
    ):
        call.return_value = {"content": "bad", "duration_ms": 89999}
        stage = LLMStage(
            self.config, "briefing", clock=SequenceClock(0, 0, 0, 89.999, 90)
        )

        stage.call("first")
        stage.add_validation_warnings(["invalid JSON shape"])
        with self.assertRaises(LLMStageTimeout) as raised:
            stage.call("retry")

        self.assertEqual(call.call_count, 1)
        self.assertEqual(raised.exception.telemetry.attempt_count, 1)
        self.assertNotIn("bad", str(raised.exception))

    @patch("llm_client.call_llm")
    def test_first_http_attempt_cannot_complete_past_stage_deadline(
        self, call, _reserve
    ):
        call.return_value = {"content": "{}", "duration_ms": 91000}
        stage = LLMStage(self.config, "briefing", clock=SequenceClock(0, 0, 0, 91))

        with self.assertRaises(LLMStageTimeout) as raised:
            stage.call("first")

        self.assertEqual(call.call_count, 1)
        self.assertEqual(raised.exception.telemetry.attempt_count, 1)
        self.assertEqual(raised.exception.telemetry.first_attempt_duration_ms, 91000)

    @patch("llm_client.call_llm")
    def test_first_valid_response_is_one_attempt_with_no_retry_duration(
        self, call, _reserve
    ):
        call.return_value = {"content": "{}", "duration_ms": 12}
        stage = LLMStage(self.config, "briefing", clock=SequenceClock(0, 0, 0, 0.012))

        stage.call("first")

        self.assertEqual(stage.telemetry.attempt_count, 1)
        self.assertEqual(stage.telemetry.first_attempt_duration_ms, 12)
        self.assertIsNone(stage.telemetry.validation_retry_duration_ms)

    def test_validation_warning_telemetry_is_bounded(self, _reserve):
        stage = LLMStage(self.config, "briefing", clock=SequenceClock(0))
        stage.add_validation_warnings(["x" * 1000] * 20)

        self.assertLessEqual(len(stage.telemetry.validation_warnings), 10)
        self.assertTrue(
            all(len(warning) <= 200 for warning in stage.telemetry.validation_warnings)
        )

    @patch("llm_client.call_llm", side_effect=RuntimeError("raw-provider-secret"))
    def test_failed_http_attempt_is_counted_without_leaking_provider_error(
        self, call, _reserve
    ):
        stage = LLMStage(self.config, "briefing", clock=SequenceClock(0, 0, 0, 0.025))

        with self.assertRaises(LLMStageFailure) as raised:
            stage.call("private prompt")

        self.assertEqual(call.call_count, 1)
        self.assertEqual(raised.exception.telemetry.attempt_count, 1)
        self.assertEqual(raised.exception.telemetry.first_attempt_duration_ms, 25)
        self.assertNotIn("secret", str(raised.exception))

    @patch("llm_client.call_llm", side_effect=RuntimeError("raw-provider-secret"))
    def test_provider_error_completed_at_deadline_is_typed_timeout(
        self, call, _reserve
    ):
        stage = LLMStage(self.config, "briefing", clock=SequenceClock(0, 0, 0, 90))

        with self.assertRaises(LLMStageTimeout) as raised:
            stage.call("private prompt")

        self.assertEqual(call.call_count, 1)
        self.assertEqual(raised.exception.telemetry.attempt_count, 1)
        self.assertEqual(raised.exception.telemetry.first_attempt_duration_ms, 90000)
        self.assertNotIn("secret", str(raised.exception))

    def test_processor_failure_processing_log_keeps_safe_attempt_telemetry(
        self, _reserve
    ):
        from orchestrator import _run_processor_impl

        telemetry = LLMAttemptTelemetry(
            attempt_count=2,
            first_attempt_duration_ms=100,
            validation_retry_duration_ms=200,
            validation_warnings=["invalid JSON shape"],
            model="provider/briefing",
            max_output_tokens=2600,
            tokens_input_total=30,
            tokens_output_total=12,
            cost_usd_total=0.03,
        )
        processor = Mock()
        processor.process.side_effect = LLMStageFailure(
            "LLM response validation failed after retry", telemetry
        )
        with (
            patch("orchestrator.get_processor", return_value=processor),
            patch("orchestrator._write_processing_log") as write_log,
        ):
            result = _run_processor_impl(
                "briefing", config={}, correlation_id="cid", manage_lifecycle=False
            )

        self.assertEqual(result["status"], "failed")
        summary = write_log.call_args.kwargs["input_summary"]
        self.assertEqual(summary["attempt_count"], 2)
        self.assertEqual(summary["validation_retry_duration_ms"], 200)
        self.assertNotIn("raw", str(summary).lower())
        persisted = write_log.call_args.kwargs
        self.assertEqual(
            (persisted["tokens_input"], persisted["tokens_output"]), (30, 12)
        )
        self.assertAlmostEqual(persisted["cost_usd"], 0.03)
        self.assertIsNone(persisted["prompt_text"])
        self.assertIsNone(persisted["raw_response"])


if __name__ == "__main__":
    unittest.main()
