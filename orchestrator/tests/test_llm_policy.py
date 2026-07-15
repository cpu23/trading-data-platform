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


class LLMRequestPolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "llm": {
                "provider": "openrouter",
                "api_key": "not-a-real-key",
                "default_model": "provider/default",
                "models": {
                    "macro_regime": "provider/macro",
                    "briefing": "provider/briefing",
                },
                "max_output_tokens": {"macro_regime": 1800, "briefing": 2600},
                "temperatures": {"macro_regime": 0.1, "briefing": 0.3},
                "structured_response": {"macro_regime": True, "briefing": True},
                "stage_timeout_seconds": 90,
                "validation_retries": 1,
                "max_retries": 1,
            }
        }

    @patch("llm_client.make_request")
    def test_macro_request_uses_its_bounded_structured_policy(self, make_request):
        response = Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": "{}"}}],
            "model": "provider/macro",
            "usage": {},
        }
        make_request.return_value = response

        call_llm("schema instructions", processor_id="macro_regime", config=self.config)

        request = make_request.call_args.kwargs
        self.assertEqual(request["json_body"]["model"], "provider/macro")
        self.assertEqual(request["json_body"]["max_tokens"], 1800)
        self.assertEqual(request["json_body"]["temperature"], 0.1)
        self.assertEqual(request["json_body"]["response_format"], {"type": "json_object"})
        self.assertEqual(request["timeout"], 90.0)
        self.assertEqual(request["max_retries"], 1)

    @patch("llm_client.make_request")
    def test_briefing_uses_its_own_larger_bounded_policy(self, make_request):
        response = Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": "{}"}}],
            "usage": {},
        }
        make_request.return_value = response

        call_llm("exact JSON shape", processor_id="briefing", config=self.config)

        body = make_request.call_args.kwargs["json_body"]
        self.assertEqual(body["model"], "provider/briefing")
        self.assertEqual(body["max_tokens"], 2600)
        self.assertEqual(body["temperature"], 0.3)

    def test_unknown_processor_uses_explicit_bounded_defaults(self):
        policy = resolve_request_policy(self.config, "new_processor")

        self.assertEqual(policy.model, "provider/default")
        self.assertGreater(policy.max_output_tokens, 0)
        self.assertLessEqual(policy.max_output_tokens, 4096)
        self.assertFalse(policy.structured_response)
        self.assertEqual(policy.validation_retries, 1)

    @patch("llm_client.make_request")
    def test_unsupported_structured_response_is_not_sent(self, make_request):
        self.config["llm"]["structured_response"]["macro_regime"] = False
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": "{}"}}], "usage": {}}
        make_request.return_value = response

        call_llm("JSON please", processor_id="macro_regime", config=self.config)

        self.assertNotIn("response_format", make_request.call_args.kwargs["json_body"])

    def test_invalid_unbounded_config_fails_closed(self):
        self.config["llm"]["max_output_tokens"]["briefing"] = 0
        with self.assertRaisesRegex(ValueError, "max_output_tokens"):
            resolve_request_policy(self.config, "briefing")


class LLMStageDeadlineAndTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "llm": {
                "api_key": "not-a-real-key",
                "default_model": "provider/default",
                "models": {"briefing": "provider/briefing"},
                "max_output_tokens": {"briefing": 2600},
                "temperatures": {"briefing": 0.2},
                "structured_response": {"briefing": True},
                "stage_timeout_seconds": 90,
                "validation_retries": 1,
                "max_retries": 1,
            }
        }

    @patch("llm_client.call_llm")
    def test_retry_uses_remaining_stage_budget_and_records_distinct_attempts(self, call):
        call.side_effect = [
            {"content": "bad", "duration_ms": 3000},
            {"content": "{}", "duration_ms": 4000},
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
        self.assertEqual(stage.telemetry.validation_warnings, ["watchlist_notes missing UK100"])
        self.assertEqual(stage.telemetry.model, "provider/briefing")
        self.assertEqual(stage.telemetry.max_output_tokens, 2600)

    @patch("llm_client.call_llm")
    def test_exhausted_budget_raises_typed_timeout_without_retry_request(self, call):
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
    def test_first_http_attempt_cannot_complete_past_stage_deadline(self, call):
        call.return_value = {"content": "{}", "duration_ms": 91000}
        stage = LLMStage(self.config, "briefing", clock=SequenceClock(0, 0, 0, 91))

        with self.assertRaises(LLMStageTimeout) as raised:
            stage.call("first")

        self.assertEqual(call.call_count, 1)
        self.assertEqual(raised.exception.telemetry.attempt_count, 1)
        self.assertEqual(raised.exception.telemetry.first_attempt_duration_ms, 91000)

    @patch("llm_client.call_llm")
    def test_first_valid_response_is_one_attempt_with_no_retry_duration(self, call):
        call.return_value = {"content": "{}", "duration_ms": 12}
        stage = LLMStage(self.config, "briefing", clock=SequenceClock(0, 0, 0, 0.012))

        stage.call("first")

        self.assertEqual(stage.telemetry.attempt_count, 1)
        self.assertEqual(stage.telemetry.first_attempt_duration_ms, 12)
        self.assertIsNone(stage.telemetry.validation_retry_duration_ms)

    def test_validation_warning_telemetry_is_bounded(self):
        stage = LLMStage(self.config, "briefing", clock=SequenceClock(0))
        stage.add_validation_warnings(["x" * 1000] * 20)

        self.assertLessEqual(len(stage.telemetry.validation_warnings), 10)
        self.assertTrue(all(len(warning) <= 200 for warning in stage.telemetry.validation_warnings))

    @patch("llm_client.call_llm", side_effect=RuntimeError("raw-provider-secret"))
    def test_failed_http_attempt_is_counted_without_leaking_provider_error(self, call):
        stage = LLMStage(
            self.config, "briefing", clock=SequenceClock(0, 0, 0, 0.025)
        )

        with self.assertRaises(LLMStageFailure) as raised:
            stage.call("private prompt")

        self.assertEqual(call.call_count, 1)
        self.assertEqual(raised.exception.telemetry.attempt_count, 1)
        self.assertEqual(raised.exception.telemetry.first_attempt_duration_ms, 25)
        self.assertNotIn("secret", str(raised.exception))

    @patch("llm_client.call_llm", side_effect=RuntimeError("raw-provider-secret"))
    def test_provider_error_completed_at_deadline_is_typed_timeout(self, call):
        stage = LLMStage(self.config, "briefing", clock=SequenceClock(0, 0, 0, 90))

        with self.assertRaises(LLMStageTimeout) as raised:
            stage.call("private prompt")

        self.assertEqual(call.call_count, 1)
        self.assertEqual(raised.exception.telemetry.attempt_count, 1)
        self.assertEqual(raised.exception.telemetry.first_attempt_duration_ms, 90000)
        self.assertNotIn("secret", str(raised.exception))

    def test_processor_failure_processing_log_keeps_safe_attempt_telemetry(self):
        from orchestrator import _run_processor_impl

        telemetry = LLMAttemptTelemetry(
            attempt_count=2,
            first_attempt_duration_ms=100,
            validation_retry_duration_ms=200,
            validation_warnings=["invalid JSON shape"],
            model="provider/briefing",
            max_output_tokens=2600,
        )
        processor = Mock()
        processor.process.side_effect = LLMStageFailure(
            "LLM response validation failed after retry", telemetry
        )
        with patch("orchestrator.get_processor", return_value=processor), patch(
            "orchestrator._write_processing_log"
        ) as write_log:
            result = _run_processor_impl(
                "briefing", config={}, correlation_id="cid", manage_lifecycle=False
            )

        self.assertEqual(result["status"], "failed")
        summary = write_log.call_args.kwargs["input_summary"]
        self.assertEqual(summary["attempt_count"], 2)
        self.assertEqual(summary["validation_retry_duration_ms"], 200)
        self.assertNotIn("raw", str(summary).lower())


if __name__ == "__main__":
    unittest.main()
