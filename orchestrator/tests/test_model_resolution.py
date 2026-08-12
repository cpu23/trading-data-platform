import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm_client import (
    DEFAULT_MODEL_SLUG,
    LLMStage,
    call_llm,
    model_preflight,
    resolve_model,
)


def _response(payload=None, attempts=1):
    response = httpx.Response(
        200,
        json=payload
        or {
            "id": "gen-123",
            "model": "resolved/model",
            "provider": "DeepSeek",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "cost": "0.012",
                "details": {"reasoning_tokens": 3},
                "prompt_tokens_details": {"cached_tokens": 4},
            },
        },
        request=httpx.Request("POST", "http://provider.test/v1/chat/completions"),
    )
    response.extensions["request_metadata"] = {
        "attempts": attempts,
        "duration_ms": 5,
        "max_attempts": 3,
    }
    return response


class ResolveModelTests(unittest.TestCase):
    def test_models_default_is_single_source_of_truth(self):
        config = {"llm": {"models": {"default": "deepseek/deepseek-v4-flash-0731"}}}
        self.assertEqual(resolve_model(config), "deepseek/deepseek-v4-flash-0731")
        self.assertEqual(
            resolve_model(config, processor_id="briefing"),
            "deepseek/deepseek-v4-flash-0731",
        )

    def test_explicit_model_wins_over_configuration(self):
        config = {"llm": {"models": {"default": "pinned/model"}}}
        self.assertEqual(
            resolve_model(config, model="explicit/model"), "explicit/model"
        )

    def test_legacy_default_model_is_ignored_without_models_default(self):
        config = {"llm": {"default_model": "legacy/default"}}
        self.assertEqual(resolve_model(config), DEFAULT_MODEL_SLUG)

    def test_per_processor_selectors_are_ignored(self):
        config = {"llm": {"models": {"briefing": "provider/briefing"}}}
        self.assertEqual(
            resolve_model(config, processor_id="briefing"), DEFAULT_MODEL_SLUG
        )
        self.assertEqual(
            resolve_model(config, processor_id="event_impact"), DEFAULT_MODEL_SLUG
        )
        with_default = {
            "llm": {
                "models": {
                    "default": "pinned/model",
                    "briefing": "provider/briefing",
                }
            }
        }
        self.assertEqual(
            resolve_model(with_default, processor_id="briefing"), "pinned/model"
        )

    def test_fallback_is_pinned_production_slug(self):
        self.assertEqual(resolve_model({}), DEFAULT_MODEL_SLUG)
        self.assertEqual(resolve_model({"llm": {}}), DEFAULT_MODEL_SLUG)

    def test_structured_legacy_override_object_form_is_ignored(self):
        config = {"llm": {"models": {"briefing": {"model": "provider/briefing"}}}}
        self.assertEqual(
            resolve_model(config, processor_id="briefing"), DEFAULT_MODEL_SLUG
        )


class UsageAccountingTests(unittest.TestCase):
    @patch("llm_client.make_request")
    def test_call_llm_records_full_usage_accounting(self, request):
        request.return_value = _response(attempts=2)
        config = {"llm": {"api_key": "key", "models": {"default": "pinned/model"}}}

        result = call_llm(
            "hello", config=config, _budget_permit=unittest.mock.Mock(valid=True)
        )

        self.assertEqual(result["requested_model"], "pinned/model")
        self.assertEqual(result["model"], "resolved/model")
        self.assertEqual(result["provider"], "DeepSeek")
        self.assertEqual(result["generation_id"], "gen-123")
        self.assertEqual(result["tokens_reasoning"], 3)
        self.assertEqual(result["tokens_cached"], 4)
        self.assertEqual(result["retry_count"], 1)

    @patch("llm_client.make_request")
    def test_missing_usage_details_are_safe_zeroes(self, request):
        request.return_value = _response(
            payload={
                "model": "resolved/model",
                "choices": [{"message": {"content": "ok"}}],
            }
        )
        config = {"llm": {"api_key": "key", "models": {"default": "pinned/model"}}}

        result = call_llm(
            "hello", config=config, _budget_permit=unittest.mock.Mock(valid=True)
        )

        self.assertEqual(result["tokens_reasoning"], 0)
        self.assertEqual(result["tokens_cached"], 0)
        self.assertIsNone(result["provider"])
        self.assertIsNone(result["generation_id"])
        self.assertEqual(result["retry_count"], 0)

    @patch("llm_client.call_llm")
    def test_stage_telemetry_accumulates_extended_usage(self, call):
        call.return_value = {
            "content": "ok",
            "model": "resolved/model",
            "requested_model": "pinned/model",
            "provider": "DeepSeek",
            "generation_id": "gen-1",
            "tokens_input": 10,
            "tokens_output": 5,
            "tokens_reasoning": 2,
            "tokens_cached": 1,
            "cost_usd": 0.01,
            "duration_ms": 5,
            "retry_count": 0,
        }
        config = {"llm": {"api_key": "key", "models": {"default": "pinned/model"}}}
        stage = LLMStage(config, "briefing")
        stage._budget_permit = unittest.mock.Mock(valid=True)
        stage.call("hello")

        telemetry = stage.telemetry
        self.assertEqual(telemetry.tokens_reasoning_total, 2)
        self.assertEqual(telemetry.tokens_cached_total, 1)
        self.assertEqual(telemetry.last_requested_model, "pinned/model")
        self.assertEqual(telemetry.last_resolved_model, "resolved/model")
        self.assertEqual(telemetry.last_provider, "DeepSeek")
        self.assertEqual(telemetry.last_generation_id, "gen-1")
        as_dict = telemetry.as_dict()
        self.assertEqual(as_dict["requested_model"], "pinned/model")
        self.assertEqual(as_dict["resolved_model"], "resolved/model")


class PreflightTests(unittest.TestCase):
    @patch("llm_client.make_request")
    def test_preflight_reports_listed_slug(self, request):
        request.return_value = httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "deepseek/deepseek-v4-flash-0731",
                        "context_length": 163840,
                        "supported_parameters": ["structured_outputs"],
                    }
                ]
            },
            request=httpx.Request("GET", "http://provider.test/v1/models"),
        )
        config = {"llm": {"models": {"default": "deepseek/deepseek-v4-flash-0731"}}}

        result = model_preflight(config)

        self.assertTrue(result["listed"])
        self.assertTrue(result["structured_outputs"])
        self.assertEqual(result["model"], "deepseek/deepseek-v4-flash-0731")

    @patch("llm_client.make_request")
    def test_preflight_flags_unlisted_slug_without_inference(self, request):
        request.return_value = httpx.Response(
            200,
            json={"data": [{"id": "other/model"}]},
            request=httpx.Request("GET", "http://provider.test/v1/models"),
        )
        config = {"llm": {"models": {"default": "ghost/model"}}}

        result = model_preflight(config)

        self.assertFalse(result["listed"])
        self.assertIn("not present", result["error"])

    @patch("llm_client.make_request", side_effect=RuntimeError("network down"))
    def test_preflight_reports_unreachable_catalogue(self, request):
        config = {"llm": {"models": {"default": "pinned/model"}}}
        result = model_preflight(config)
        self.assertIsNone(result["listed"])
        self.assertIn("unreachable", result["error"])


if __name__ == "__main__":
    unittest.main()
