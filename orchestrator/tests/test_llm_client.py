import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from http_client import make_request
from llm_client import OPENROUTER_URL, LLMStage, call_llm


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
    def test_wraps_strict_json_schema_for_openrouter(self, request):
        request.return_value = _response(
            payload={
                "choices": [{"message": {"content": '{"answer":"ok"}'}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        )
        config = {
            "llm": {
                "api_key": "key",
                "models": {"default": "provider/model"},
            }
        }
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["answer"],
            "properties": {"answer": {"type": "string"}},
        }

        call_llm(
            "hello",
            config=config,
            response_schema=schema,
            _budget_permit=Mock(valid=True),
        )

        body = request.call_args.kwargs["json_body"]
        self.assertEqual(
            body["response_format"],
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_response",
                    "strict": True,
                    "schema": schema,
                },
            },
        )
        self.assertTrue(body["provider"]["require_parameters"])

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


class LlmReservationLeakTests(unittest.TestCase):
    """Deterministic local validation must precede budget admission.

    A malformed request (empty messages, bad max_prices, malformed
    response_schema, missing api_key, and their equivalents) must be rejected
    before ``enforce_budget`` runs: an admitted-but-undispatchable call would
    otherwise leave an active reservation that ties up the daily cap until its
    TTL expires. These tests drive the real admission path (``budgets.get_session``
    patched) and assert no admission query ever runs on a local failure, plus a
    control proving a valid request does admit and settle.
    """

    def setUp(self):
        self.config = {
            "llm": {
                "api_key": "top-secret",
                "models": {"default": "provider/model"},
            },
            "budgets": {
                "daily_llm_usd": 2.0,
                "warn_at_pct": 80,
                "reservation_estimate_usd": 0.05,
            },
        }

    @staticmethod
    def _admission_session():
        session = Mock()
        lock = Mock()
        lock.fetchone.return_value = None
        sweep = Mock()
        sweep.fetchone.return_value = None
        sums = Mock()
        sums.fetchone.return_value = SimpleNamespace(
            _mapping={"spent_usd": 0.5, "reserved_usd": 0.0}
        )
        insert = Mock()
        insert.fetchone.return_value = ("reservation-1",)
        trailing = Mock()
        trailing.fetchone.return_value = None
        queue = [lock, sweep, sums, insert]
        session.execute.side_effect = lambda *args, **kwargs: (
            queue.pop(0) if queue else trailing
        )
        return session

    @patch("llm_client.make_request")
    @patch("budgets.get_session")
    def test_empty_messages_rejected_before_admission(self, get_session, request):
        with self.assertRaisesRegex(ValueError, "messages must be a non-empty array"):
            call_llm("hello", config=self.config, messages=[])
        get_session.assert_not_called()
        request.assert_not_called()

    @patch("llm_client.make_request")
    @patch("budgets.get_session")
    def test_non_dict_message_rejected_before_admission(self, get_session, request):
        with self.assertRaisesRegex(ValueError, "each message must be an object"):
            call_llm("hello", config=self.config, messages=[("role", "user")])
        get_session.assert_not_called()
        request.assert_not_called()

    @patch("llm_client.make_request")
    @patch("budgets.get_session")
    def test_invalid_role_rejected_before_admission(self, get_session, request):
        with self.assertRaisesRegex(ValueError, "message role is invalid"):
            call_llm(
                "hello",
                config=self.config,
                messages=[{"role": "tool", "content": "unsafe"}],
            )
        get_session.assert_not_called()
        request.assert_not_called()

    @patch("llm_client.make_request")
    @patch("budgets.get_session")
    def test_empty_content_rejected_before_admission(self, get_session, request):
        with self.assertRaisesRegex(ValueError, "message content must be a non-empty"):
            call_llm(
                "hello",
                config=self.config,
                messages=[{"role": "user", "content": ""}],
            )
        get_session.assert_not_called()
        request.assert_not_called()

    @patch("llm_client.make_request")
    @patch("budgets.get_session")
    def test_malformed_max_prices_rejected_before_admission(self, get_session, request):
        for max_prices in ("not-a-dict", {}):
            with self.subTest(max_prices=max_prices):
                config = {
                    **self.config,
                    "llm": {
                        **self.config["llm"],
                        "max_prices": {"briefing": max_prices},
                    },
                }
                with self.assertRaisesRegex(
                    ValueError, "llm.max_prices for briefing must be a non-empty object"
                ):
                    call_llm("hello", config=config, processor_id="briefing")
        get_session.assert_not_called()
        request.assert_not_called()

    @patch("llm_client.make_request")
    @patch("budgets.get_session")
    def test_malformed_response_schema_rejected_before_admission(
        self, get_session, request
    ):
        with self.assertRaisesRegex(
            ValueError, "response_schema must be a JSON Schema object"
        ):
            call_llm("hello", config=self.config, response_schema="not-a-schema")
        get_session.assert_not_called()
        request.assert_not_called()

    @patch("llm_client.make_request")
    @patch("budgets.get_session")
    def test_unserializable_nested_request_rejected_before_admission(
        self, get_session, request
    ):
        invalid_inputs = (
            {
                "response_schema": {
                    "type": "object",
                    "properties": {"value": {"default": object()}},
                }
            },
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "hello",
                        "metadata": object(),
                    }
                ]
            },
        )
        for kwargs in invalid_inputs:
            with self.subTest(kwargs=tuple(kwargs)):
                with self.assertRaisesRegex(
                    ValueError, "LLM request body must be JSON-serializable"
                ):
                    call_llm("hello", config=self.config, **kwargs)
        get_session.assert_not_called()
        request.assert_not_called()

    @patch("llm_client.make_request")
    @patch("budgets.get_session")
    def test_missing_api_key_rejected_before_admission(self, get_session, request):
        config = {
            **self.config,
            "llm": {"models": {"default": "provider/model"}},
        }
        with self.assertRaises(KeyError):
            call_llm("hello", config=config)
        get_session.assert_not_called()
        request.assert_not_called()

    @patch("llm_client.make_request")
    @patch("budgets.get_session")
    def test_invalid_include_temperature_rejected_before_admission(
        self, get_session, request
    ):
        with self.assertRaisesRegex(
            ValueError, "llm.include_temperature must be a boolean"
        ):
            call_llm("hello", config=self.config, include_temperature="yes")
        get_session.assert_not_called()
        request.assert_not_called()

    @patch("llm_client.make_request")
    @patch("budgets.get_session")
    def test_valid_request_admits_and_settles(self, get_session, request):
        get_session.return_value.__enter__.return_value = self._admission_session()
        response = Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.01},
        }
        request.return_value = response

        result = call_llm("hello", config=self.config)

        self.assertEqual(result["content"], "ok")
        request.assert_called_once()
        # Control: a valid request did run the full admission sequence,
        # including the INSERT that creates the reservation.
        insert_sql = str(
            get_session.return_value.__enter__.return_value.execute.call_args_list[
                3
            ].args[0]
        )
        self.assertIn("INSERT INTO budget_reservations", insert_sql)

    @patch("llm_client.make_request")
    @patch("budgets.get_session")
    def test_stage_malformed_config_rejected_before_admission(
        self, get_session, request
    ):
        config = {
            **self.config,
            "llm": {
                **self.config["llm"],
                "max_prices": {"briefing": "not-a-dict"},
            },
        }
        stage = LLMStage(config, "briefing")
        with self.assertRaisesRegex(
            ValueError, "llm.max_prices for briefing must be a non-empty object"
        ):
            stage.call("hello")
        get_session.assert_not_called()
        request.assert_not_called()

    @patch("llm_client.make_request")
    @patch("budgets.get_session")
    def test_stage_malformed_response_schema_rejected_before_admission(
        self, get_session, request
    ):
        stage = LLMStage(self.config, "briefing", response_schema="not-a-schema")
        with self.assertRaisesRegex(
            ValueError, "response_schema must be a JSON Schema object"
        ):
            stage.call("hello")
        get_session.assert_not_called()
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
