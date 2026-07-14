import sys
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import http_client


class ScriptedClient:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.requests = []
        self.close = Mock()

    def request(self, **kwargs):
        self.requests.append(kwargs)
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def response(status_code, *, headers=None, body=b"result"):
    request = httpx.Request("GET", "https://example.test/resource")
    return httpx.Response(status_code, headers=headers, content=body, request=request)


class ConfigurableRetryTests(unittest.TestCase):
    def test_max_retries_is_total_attempts(self):
        client = ScriptedClient([response(503)])

        with self.assertRaises(httpx.HTTPStatusError):
            http_client.make_request(
                "GET", "https://example.test/resource", max_retries=1,
                client=client, sleep=Mock(),
            )

        self.assertEqual(len(client.requests), 1)

    def test_retries_transient_statuses_then_returns_success(self):
        for status_code in (500, 502, 503, 504):
            with self.subTest(status_code=status_code):
                client = ScriptedClient([response(status_code), response(200)])
                sleeper = Mock()

                result = http_client.make_request(
                    "GET", "https://example.test/resource", max_retries=2,
                    client=client, sleep=sleeper,
                )

                self.assertEqual(result.status_code, 200)
                self.assertEqual(len(client.requests), 2)
                sleeper.assert_called_once_with(1.0)

    def test_429_respects_retry_after_seconds(self):
        client = ScriptedClient([
            response(429, headers={"Retry-After": "7"}),
            response(200),
        ])
        sleeper = Mock()

        http_client.make_request(
            "GET", "https://example.test/resource", max_retries=2,
            client=client, sleep=sleeper,
        )

        sleeper.assert_called_once_with(7.0)

    def test_429_malformed_retry_after_uses_capped_fallbacks(self):
        cases = (("not-seconds", 1.0), ("999999", 60.0), ("-3", 1.0))
        for retry_after, expected in cases:
            with self.subTest(retry_after=retry_after):
                client = ScriptedClient([
                    response(429, headers={"Retry-After": retry_after}),
                    response(200),
                ])
                sleeper = Mock()

                http_client.make_request(
                    "GET", "https://example.test/resource", max_retries=2,
                    client=client, sleep=sleeper,
                )

                sleeper.assert_called_once_with(expected)

    def test_429_non_finite_retry_after_uses_fallback_and_retries(self):
        for retry_after in ("NaN", "Infinity", "+inf", "-inf"):
            with self.subTest(retry_after=retry_after):
                client = ScriptedClient([
                    response(429, headers={"Retry-After": retry_after}),
                    response(200),
                ])
                delays = []

                def finite_only_sleep(delay):
                    if delay != delay or delay in (float("inf"), float("-inf")):
                        raise ValueError("sleep length must be finite")
                    delays.append(delay)

                result = http_client.make_request(
                    "GET", "https://example.test/resource", max_retries=2,
                    client=client, sleep=finite_only_sleep,
                )

                self.assertEqual(result.status_code, 200)
                self.assertEqual(delays, [1.0])

    def test_429_future_http_date_is_respected_and_capped(self):
        now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
        cases = ((timedelta(seconds=17), 17.0), (timedelta(minutes=5), 60.0))
        for offset, expected in cases:
            with self.subTest(offset=offset):
                client = ScriptedClient([
                    response(429, headers={"Retry-After": format_datetime(now + offset, usegmt=True)}),
                    response(200),
                ])
                sleeper = Mock()

                http_client.make_request(
                    "GET", "https://example.test/resource", max_retries=2,
                    client=client, sleep=sleeper, wall_clock=lambda: now,
                )

                sleeper.assert_called_once_with(expected)

    def test_429_past_http_date_retries_with_zero_delay(self):
        now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
        client = ScriptedClient([
            response(429, headers={"Retry-After": format_datetime(now - timedelta(seconds=30), usegmt=True)}),
            response(200),
        ])
        sleeper = Mock()

        result = http_client.make_request(
            "GET", "https://example.test/resource", max_retries=2,
            client=client, sleep=sleeper, wall_clock=lambda: now,
        )

        self.assertEqual(result.status_code, 200)
        sleeper.assert_called_once_with(0.0)

    def test_non_transient_4xx_is_returned_without_retry(self):
        client = ScriptedClient([response(400)])
        sleeper = Mock()

        result = http_client.make_request(
            "GET", "https://example.test/resource", max_retries=3,
            client=client, sleep=sleeper,
        )

        self.assertEqual(result.status_code, 400)
        self.assertEqual(len(client.requests), 1)
        sleeper.assert_not_called()

    def test_retries_timeout_and_transport_errors(self):
        request = httpx.Request("GET", "https://example.test/resource")
        errors = (
            httpx.ReadTimeout("slow", request=request),
            httpx.ConnectError("reset", request=request),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                client = ScriptedClient([error, response(200)])

                result = http_client.make_request(
                    "GET", "https://example.test/resource", max_retries=2,
                    client=client, sleep=Mock(),
                )

                self.assertEqual(result.status_code, 200)
                self.assertEqual(len(client.requests), 2)

    def test_exhaustion_raises_final_network_error(self):
        request = httpx.Request("GET", "https://example.test/resource")
        first = httpx.ConnectError("first", request=request)
        final = httpx.ReadTimeout("final", request=request)
        client = ScriptedClient([first, final])

        with self.assertRaises(httpx.ReadTimeout) as raised:
            http_client.make_request(
                "GET", "https://example.test/resource", max_retries=2,
                client=client, sleep=Mock(),
            )

        self.assertIs(raised.exception, final)

    def test_preserves_request_options_and_source_timeout_default(self):
        client = ScriptedClient([response(200)])
        headers = {"Authorization": "Bearer secret", "X-Test": "value"}
        body = {"hello": "world"}

        http_client.make_request(
            "post", "https://example.test/resource", params={"q": "term"},
            headers=headers, json_body=body, follow_redirects=True,
            max_retries=1, client=client,
        )

        sent = client.requests[0]
        self.assertEqual(sent["method"], "POST")
        self.assertEqual(sent["url"], "https://example.test/resource")
        self.assertEqual(sent["params"], {"q": "term"})
        self.assertEqual(sent["headers"], headers)
        self.assertEqual(sent["json"], body)
        self.assertTrue(sent["follow_redirects"])
        timeout = sent["timeout"]
        self.assertEqual(timeout.connect, 5)
        self.assertEqual(timeout.read, 30)
        self.assertEqual(timeout.write, 30)
        self.assertEqual(timeout.pool, 5)

    def test_explicit_llm_timeout_is_preserved(self):
        client = ScriptedClient([response(200)])

        http_client.make_request(
            "POST", "https://example.test/llm", timeout=120.0,
            max_retries=1, client=client,
        )

        self.assertEqual(client.requests[0]["timeout"], 120.0)

    @patch.object(http_client, "logger")
    def test_logs_attempt_metadata_without_sensitive_request_data(self, logger):
        client = ScriptedClient([response(503), response(200)])
        ticks = iter((10.0, 10.1, 10.2, 10.3))

        http_client.make_request(
            "GET", "https://example.test/path?api_key=secret",
            headers={"Authorization": "Bearer secret"}, max_retries=2,
            client=client, sleep=Mock(), clock=lambda: next(ticks),
        )

        events = logger.warning.call_args_list + logger.info.call_args_list
        self.assertTrue(events)
        serialized = repr(events)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("api_key", serialized)
        retry_kwargs = logger.warning.call_args.kwargs
        self.assertEqual(retry_kwargs["attempt"], 1)
        self.assertEqual(retry_kwargs["max_attempts"], 2)
        self.assertEqual(retry_kwargs["status_code"], 503)
        self.assertEqual(retry_kwargs["category"], "http_status")
        self.assertIn("total_duration_ms", retry_kwargs)


class SharedClientLifecycleTests(unittest.TestCase):
    def tearDown(self):
        close = getattr(http_client, "close_shared_client", None)
        if close is not None:
            close()

    @patch.object(http_client.httpx, "Client")
    def test_repeated_requests_reuse_one_client_until_shutdown(self, client_class):
        shared = ScriptedClient([response(200), response(200)])
        client_class.return_value = shared

        first = http_client.make_request(
            "GET", "https://example.test/one", max_retries=1,
        )
        second = http_client.make_request(
            "GET", "https://example.test/two", max_retries=1,
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        client_class.assert_called_once()
        shared.close.assert_not_called()

        http_client.close_shared_client()
        http_client.close_shared_client()
        shared.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
