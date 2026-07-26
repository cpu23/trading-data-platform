import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from http_client import make_request
from llm_client import call_llm, resolve_model


def _response(
    status_code=200,
    payload=None,
    *,
    attempts=1,
    headers=None,
):
    response = httpx.Response(
        status_code,
        json=payload
        or {
            "id": "body-request-id",
            "model": "returned-model",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
                "cost": "0.012",
            },
        },
        headers=headers,
        request=httpx.Request("POST", "http://provider.test/v1/chat/completions"),
    )
    response.extensions["request_metadata"] = {
        "attempts": attempts,
        "duration_ms": 5,
        "max_attempts": 3,
    }
    return response


class LlmClientTests(unittest.TestCase):
    @unittest.skip("skip: codex/market-intelligence-expansion contract not implemented in master")
    def test_resolve_model_supports_string_and_structured_overrides(self):
        config = {
            "llm": {
                "default_model": "default",
                "models": {
                    "briefing": "briefing-model",
                    "macro": {"model": "macro-model"},
                },
                "model_overrides": {"event": "event-model"},
            }
        }

        self.assertEqual(resolve_model(config), "default")
        self.assertEqual(resolve_model(config, "event"), "event-model")
        self.assertEqual(resolve_model(config, "briefing"), "briefing-model")
        self.assertEqual(resolve_model(config, "macro"), "macro-model")
        self.assertEqual(resolve_model(config, "macro", "explicit"), "explicit")

    @patch("llm_client.make_request")
    @unittest.skip("skip: codex/market-intelligence-expansion contract not implemented in master")
    def test_provider_neutral_endpoint_optional_auth_usage_and_metadata(self, request):
        request.return_value = _response(
            attempts=2, headers={"x-request-id": "header-request-id"}
        )
        config = {
            "llm": {
                "provider": "local",
                "base_url": "http://localhost:1234/v1/",
                "api_key": "",
                "default_model": "local-model",
                "temperature": 0.1,
                "max_retries": 4,
            }
        }

        result = call_llm("hello", config=config)

        kwargs = request.call_args.kwargs
        self.assertEqual(kwargs["url"], "http://localhost:1234/v1/chat/completions")
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertEqual(kwargs["max_retries"], 4)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["duration_ms"], result["request_metadata"]["duration_ms"])
        self.assertEqual(result["tokens_input"], 11)
        self.assertEqual(result["tokens_output"], 7)
        self.assertEqual(result["total_tokens"], 18)
        self.assertEqual(result["cost_usd"], 0.012)
        self.assertEqual(result["provider_request_id"], "header-request-id")
        self.assertEqual(result["usage"]["total_tokens"], 18)

    @patch("llm_client.make_request")
    def test_sends_openrouter_reasoning_effort(self, request):
        request.return_value = _response()
        config = {
            "llm": {
                "api_key": "key",
                "default_model": "openai/gpt-oss-120b",
            }
        }

        call_llm(
            "hello",
            config=config,
            reasoning_effort="low",
            _budget_permit=Mock(valid=True),
        )

        body = request.call_args.kwargs["json_body"]
        self.assertEqual(body["reasoning"], {"effort": "low"})

    @patch("llm_client.make_request")
    @unittest.skip("skip: codex/market-intelligence-expansion contract not implemented in master")
    def test_sends_output_cap_and_provider_preferences(self, request):
        request.return_value = _response()
        config = {
            "llm": {
                "base_url": "https://openrouter.ai/api/v1",
                "default_model": "openai/gpt-oss-120b",
            }
        }
        provider = {"order": ["WandB"], "allow_fallbacks": False}

        result = call_llm(
            "hello",
            config=config,
            max_tokens=2400,
            provider_preferences=provider,
        )

        body = request.call_args.kwargs["json_body"]
        self.assertEqual(body["max_tokens"], 2400)
        self.assertEqual(body["provider"], provider)
        self.assertEqual(result["request_metadata"]["max_tokens"], 2400)

    @patch("llm_client.make_request")
    @unittest.skip("skip: codex/market-intelligence-expansion contract not implemented in master")
    def test_reasoning_falls_back_only_for_explicit_unsupported_parameter(self, request):
        request.side_effect = [
            _response(
                400,
                {
                    "error": {
                        "code": "unsupported_parameter",
                        "param": "reasoning_effort",
                        "message": "Unsupported parameter: reasoning_effort",
                    }
                },
                attempts=2,
            ),
            _response(attempts=1),
        ]
        config = {
            "llm": {
                "base_url": "http://provider.test/v1",
                "api_key": "key",
                "default_model": "reasoning-model",
                "reasoning_effort": "high",
                "capability_fallback": "auto",
            }
        }

        result = call_llm("reason", config=config)

        first_body = request.call_args_list[0].kwargs["json_body"]
        second_body = request.call_args_list[1].kwargs["json_body"]
        self.assertEqual(first_body["reasoning_effort"], "high")
        self.assertNotIn("reasoning_effort", second_body)
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(
            result["request_metadata"]["fallback_parameters"], ["reasoning_effort"]
        )
        self.assertIsNone(
            result["request_metadata"]["reasoning_effort_applied"]
        )

    @patch("llm_client.make_request")
    @unittest.skip("skip: codex/market-intelligence-expansion contract not implemented in master")
    def test_generic_bad_request_does_not_trigger_capability_fallback(self, request):
        request.return_value = _response(
            400, {"error": {"message": "Malformed request"}}
        )
        config = {
            "llm": {
                "base_url": "http://provider.test/v1",
                "default_model": "model",
                "reasoning_effort": "high",
            }
        }

        with self.assertRaises(httpx.HTTPStatusError):
            call_llm("reason", config=config)

        request.assert_called_once()

    @patch("llm_client.make_request")
    @unittest.skip("skip: codex/market-intelligence-expansion contract not implemented in master")
    def test_sampling_fallback_removes_sampling_parameters(self, request):
        request.side_effect = [
            _response(
                422,
                {
                    "error": {
                        "param": "temperature",
                        "message": "temperature is not supported by this model",
                    }
                },
            ),
            _response(),
        ]
        config = {
            "llm": {
                "base_url": "http://provider.test/v1",
                "default_model": "model",
                "sampling": {"temperature": 0.3, "top_p": 0.9},
            }
        }

        result = call_llm("sample", config=config)

        final_body = request.call_args_list[-1].kwargs["json_body"]
        self.assertNotIn("temperature", final_body)
        self.assertNotIn("top_p", final_body)
        self.assertEqual(
            result["request_metadata"]["fallback_parameters"],
            ["temperature", "top_p"],
        )


class HttpClientRetryTests(unittest.TestCase):
    @patch("http_client.time.sleep")
    @patch("http_client._do_request")
    @unittest.skip("skip: codex/market-intelligence-expansion contract not implemented in master")
    def test_configurable_attempt_limit_and_response_metadata(self, do_request, sleep):
        do_request.side_effect = [
            httpx.ConnectError("one"),
            httpx.TimeoutException("two"),
            _response(),
        ]

        response = make_request(
            "GET", "http://example.test", max_retries=3
        )

        self.assertEqual(do_request.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(response.extensions["request_metadata"]["attempts"], 3)

    @patch("http_client.time.sleep")
    @patch("http_client._do_request")
    @unittest.skip("skip: codex/market-intelligence-expansion contract not implemented in master")
    def test_one_retry_setting_means_one_attempt(self, do_request, sleep):
        error = httpx.ConnectError("failed")
        do_request.side_effect = error

        with self.assertRaises(httpx.ConnectError) as raised:
            make_request("GET", "http://example.test", max_retries=1)

        self.assertEqual(do_request.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(raised.exception.request_metadata["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
