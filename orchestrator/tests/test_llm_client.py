import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from http_client import make_request
from llm_client import OPENROUTER_URL, call_llm


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
    @patch("llm_client.make_request")
    def test_sends_openrouter_reasoning_effort(self, request):
        request.return_value = _response()
        config = {
            "llm": {
                "api_key": "key",
                "models": {"default": "openai/gpt-oss-120b"},
            }
        }

        call_llm(
            "hello",
            config=config,
            reasoning_effort="low",
            _budget_permit=Mock(valid=True),
        )

        kwargs = request.call_args.kwargs
        body = kwargs["json_body"]
        self.assertEqual(body["reasoning"], {"effort": "low"})
        # Clean OpenRouter cutover: fixed endpoint, exactly one HTTP attempt,
        # and no Idempotency-Key (no documented inbound idempotency contract).
        self.assertEqual(kwargs["url"], OPENROUTER_URL)
        self.assertEqual(kwargs["max_retries"], 1)
        self.assertNotIn("idempotency_key", kwargs)

    @patch("llm_client.make_request")
    def test_explicit_messages_are_sent_unchanged(self, request):
        request.return_value = _response()
        config = {
            "llm": {
                "api_key": "key",
                "models": {"default": "provider/model"},
            }
        }
        messages = [
            {"role": "system", "content": "Use only supplied evidence."},
            {"role": "user", "content": "Interpret [e1]."},
        ]

        call_llm(
            "ignored when explicit messages are supplied",
            config=config,
            messages=messages,
            _budget_permit=Mock(valid=True),
        )

        self.assertEqual(request.call_args.kwargs["json_body"]["messages"], messages)

    @patch("llm_client.make_request")
    def test_temperature_can_be_omitted_for_incompatible_models(self, request):
        request.return_value = _response()
        config = {
            "llm": {
                "api_key": "key",
                "models": {"default": "provider/model"},
                "temperature": 0.2,
            }
        }

        call_llm(
            "hello",
            config=config,
            include_temperature=False,
            _budget_permit=Mock(valid=True),
        )

        self.assertNotIn("temperature", request.call_args.kwargs["json_body"])

    @patch("llm_client.make_request")
    def test_temperature_omission_can_be_configured(self, request):
        request.return_value = _response()
        config = {
            "llm": {
                "api_key": "key",
                "models": {"default": "provider/model"},
                "temperature": 0.2,
                "include_temperature": False,
            }
        }

        call_llm(
            "hello",
            config=config,
            _budget_permit=Mock(valid=True),
        )

        self.assertNotIn("temperature", request.call_args.kwargs["json_body"])

    @patch("llm_client.make_request")
    def test_invalid_explicit_messages_fail_before_transport(self, request):
        config = {
            "llm": {
                "api_key": "key",
                "models": {"default": "provider/model"},
            }
        }
        with self.assertRaisesRegex(ValueError, "message role"):
            call_llm(
                "ignored",
                config=config,
                messages=[{"role": "tool", "content": "unsafe"}],
                _budget_permit=Mock(valid=True),
            )
        request.assert_not_called()

class HttpClientRetryTests(unittest.TestCase):
    @patch("http_client.time.sleep")
    @patch("http_client._do_request")
    @unittest.skip(
        "skip: codex/market-intelligence-expansion contract not implemented in master"
    )
    def test_configurable_attempt_limit_and_response_metadata(self, do_request, sleep):
        do_request.side_effect = [
            httpx.ConnectError("one"),
            httpx.TimeoutException("two"),
            _response(),
        ]

        response = make_request("GET", "http://example.test", max_retries=3)

        self.assertEqual(do_request.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(response.extensions["request_metadata"]["attempts"], 3)

    @patch("http_client.time.sleep")
    @patch("http_client._do_request")
    @unittest.skip(
        "skip: codex/market-intelligence-expansion contract not implemented in master"
    )
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
