import socket
import sys
import threading
import unittest
from datetime import UTC, datetime, timedelta
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
                "GET",
                "https://example.test/resource",
                max_retries=1,
                client=client,
                sleep=Mock(),
            )

        self.assertEqual(len(client.requests), 1)

    def test_retries_transient_statuses_then_returns_success(self):
        for status_code in (500, 502, 503, 504):
            with self.subTest(status_code=status_code):
                client = ScriptedClient([response(status_code), response(200)])
                sleeper = Mock()

                result = http_client.make_request(
                    "GET",
                    "https://example.test/resource",
                    max_retries=2,
                    client=client,
                    sleep=sleeper,
                )

                self.assertEqual(result.status_code, 200)
                self.assertEqual(len(client.requests), 2)
                sleeper.assert_called_once()
                delay = sleeper.call_args.args[0]
                # Bounded full-jitter window for the first retry is [0, 1s].
                self.assertGreaterEqual(delay, 0.0)
                self.assertLessEqual(delay, 1.0)

    def test_429_respects_retry_after_seconds(self):
        client = ScriptedClient(
            [
                response(429, headers={"Retry-After": "7"}),
                response(200),
            ]
        )
        sleeper = Mock()

        http_client.make_request(
            "GET",
            "https://example.test/resource",
            max_retries=2,
            client=client,
            sleep=sleeper,
        )

        sleeper.assert_called_once_with(7.0)

    def test_429_malformed_retry_after_uses_capped_fallbacks(self):
        client = ScriptedClient(
            [
                response(429, headers={"Retry-After": "999999"}),
                response(200),
            ]
        )
        sleeper = Mock()

        http_client.make_request(
            "GET",
            "https://example.test/resource",
            max_retries=2,
            client=client,
            sleep=sleeper,
        )
        self.assertAlmostEqual(sleeper.call_args.args[0], 60.0, places=3)

    def test_429_absent_or_invalid_retry_after_uses_jittered_fallback(self):
        for retry_after in (None, "not-seconds", "-3"):
            with self.subTest(retry_after=retry_after):
                headers = {"Retry-After": retry_after} if retry_after else None
                client = ScriptedClient(
                    [
                        response(429, headers=headers),
                        response(200),
                    ]
                )
                sleeper = Mock()

                http_client.make_request(
                    "GET",
                    "https://example.test/resource",
                    max_retries=2,
                    client=client,
                    sleep=sleeper,
                )

                sleeper.assert_called_once()
                delay = sleeper.call_args.args[0]
                self.assertGreaterEqual(delay, 0.0)
                self.assertLessEqual(delay, 1.0)

    def test_429_non_finite_retry_after_uses_fallback_and_retries(self):
        for retry_after in ("NaN", "Infinity", "+inf", "-inf"):
            with self.subTest(retry_after=retry_after):
                client = ScriptedClient(
                    [
                        response(429, headers={"Retry-After": retry_after}),
                        response(200),
                    ]
                )
                delays = []

                def finite_only_sleep(delay):
                    if delay != delay or delay in (float("inf"), float("-inf")):
                        raise ValueError("sleep length must be finite")
                    delays.append(delay)

                result = http_client.make_request(
                    "GET",
                    "https://example.test/resource",
                    max_retries=2,
                    client=client,
                    sleep=finite_only_sleep,
                )

                self.assertEqual(result.status_code, 200)
                self.assertEqual(len(delays), 1)
                self.assertGreaterEqual(delays[0], 0.0)
                self.assertLessEqual(delays[0], 1.0)

    def test_429_future_http_date_is_respected_and_capped(self):
        now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
        cases = ((timedelta(seconds=17), 17.0), (timedelta(minutes=5), 60.0))
        for offset, expected in cases:
            with self.subTest(offset=offset):
                client = ScriptedClient(
                    [
                        response(
                            429,
                            headers={
                                "Retry-After": format_datetime(
                                    now + offset, usegmt=True
                                )
                            },
                        ),
                        response(200),
                    ]
                )
                sleeper = Mock()

                http_client.make_request(
                    "GET",
                    "https://example.test/resource",
                    max_retries=2,
                    client=client,
                    sleep=sleeper,
                    wall_clock=lambda: now,
                )
                self.assertAlmostEqual(sleeper.call_args.args[0], expected, places=3)

    def test_429_past_http_date_retries_with_zero_delay(self):
        now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
        client = ScriptedClient(
            [
                response(
                    429,
                    headers={
                        "Retry-After": format_datetime(
                            now - timedelta(seconds=30), usegmt=True
                        )
                    },
                ),
                response(200),
            ]
        )
        sleeper = Mock()

        result = http_client.make_request(
            "GET",
            "https://example.test/resource",
            max_retries=2,
            client=client,
            sleep=sleeper,
            wall_clock=lambda: now,
        )

        self.assertEqual(result.status_code, 200)
        sleeper.assert_called_once_with(0.0)

    def test_non_transient_4xx_is_returned_without_retry(self):
        client = ScriptedClient([response(400)])
        sleeper = Mock()

        result = http_client.make_request(
            "GET",
            "https://example.test/resource",
            max_retries=3,
            client=client,
            sleep=sleeper,
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
                    "GET",
                    "https://example.test/resource",
                    max_retries=2,
                    client=client,
                    sleep=Mock(),
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
                "GET",
                "https://example.test/resource",
                max_retries=2,
                client=client,
                sleep=Mock(),
            )

        self.assertIs(raised.exception, final)


class IdempotencyAndDeadlineTests(unittest.TestCase):
    def test_post_without_idempotency_key_is_never_replayed(self):
        """A non-idempotent POST must not be retried: capability is decided
        before the first send."""
        client = ScriptedClient([response(503)])
        sleeper = Mock()

        with self.assertRaises(httpx.HTTPStatusError):
            http_client.make_request(
                "POST",
                "https://example.test/resource",
                json_body={"hello": "world"},
                max_retries=3,
                client=client,
                sleep=sleeper,
            )

        self.assertEqual(len(client.requests), 1)
        sleeper.assert_not_called()

    def test_post_with_idempotency_key_retries_with_stable_header(self):
        client = ScriptedClient([response(503), response(200)])
        sleeper = Mock()

        result = http_client.make_request(
            "POST",
            "https://example.test/resource",
            json_body={"hello": "world"},
            max_retries=3,
            client=client,
            sleep=sleeper,
            idempotency_key="op-42",
        )

        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(client.requests), 2)
        sent = client.requests
        self.assertEqual(sent[0]["headers"].get("Idempotency-Key"), "op-42")
        self.assertEqual(sent[1]["headers"].get("Idempotency-Key"), "op-42")

    def test_post_with_existing_idempotency_header_retries(self):
        client = ScriptedClient([response(500), response(201)])
        sleeper = Mock()

        result = http_client.make_request(
            "POST",
            "https://example.test/resource",
            json_body={"hello": "world"},
            headers={"Idempotency-Key": "from-header"},
            max_retries=2,
            client=client,
            sleep=sleeper,
        )

        self.assertEqual(result.status_code, 201)
        self.assertEqual(len(client.requests), 2)

    def test_put_and_delete_are_retried_as_idempotent(self):
        for method in ("PUT", "DELETE", "HEAD", "OPTIONS"):
            with self.subTest(method=method):
                client = ScriptedClient([response(503), response(200)])
                result = http_client.make_request(
                    method,
                    "https://example.test/resource",
                    max_retries=2,
                    client=client,
                    sleep=Mock(),
                )
                self.assertEqual(result.status_code, 200)
                self.assertEqual(len(client.requests), 2)

    def test_stable_idempotency_key_is_deterministic_and_sensitive_to_body(self):
        first = http_client.stable_idempotency_key(
            "POST", "https://example.test/r", b'{"a":1}'
        )
        second = http_client.stable_idempotency_key(
            "post", "https://example.test/r", b'{"a":1}'
        )
        self.assertEqual(first, second)
        different = http_client.stable_idempotency_key(
            "POST", "https://example.test/r", b'{"a":2}'
        )
        self.assertNotEqual(first, different)
        other_url = http_client.stable_idempotency_key(
            "POST", "https://example.test/other", b'{"a":1}'
        )
        self.assertNotEqual(first, other_url)

    def test_idempotency_key_is_scoped_to_logical_operation(self):
        """Identical method/URL/body across distinct operations must not
        collide: the scope (correlation id / operation nonce) separates them,
        while the same scope stays stable across transport retries."""
        body = b'{"prompt":"same prompt"}'
        key_a = http_client.stable_idempotency_key(
            "POST", "https://example.test/llm", body, scope="corr-1"
        )
        key_b = http_client.stable_idempotency_key(
            "POST", "https://example.test/llm", body, scope="corr-2"
        )
        self.assertNotEqual(key_a, key_b)
        retry_a = http_client.stable_idempotency_key(
            "POST", "https://example.test/llm", body, scope="corr-1"
        )
        self.assertEqual(key_a, retry_a)
        unscoped = http_client.stable_idempotency_key(
            "POST", "https://example.test/llm", body
        )
        self.assertNotEqual(key_a, unscoped)

    def test_total_deadline_bounds_retries_and_sleeps(self):
        client = ScriptedClient(
            [response(503), response(503), response(503), response(503)]
        )
        # start, attempt check, post-send check, retry budget/log, next attempt.
        ticks = iter((0.0, 1.0, 1.2, 1.5, 1.6, 2.5))
        sleeper = Mock()

        with self.assertRaises(http_client.RequestDeadlineExceeded):
            http_client.make_request(
                "GET",
                "https://example.test/resource",
                max_retries=4,
                client=client,
                sleep=sleeper,
                clock=lambda: next(ticks),
                deadline_seconds=2.0,
            )

        # The first retry was slept but the deadline expired before a second
        # send; the remaining attempts were never made.
        self.assertEqual(len(client.requests), 1)
        sleeper.assert_called_once()
        delay = sleeper.call_args.args[0]
        self.assertGreaterEqual(delay, 0.0)
        self.assertLessEqual(delay, 0.5)

    def test_in_flight_request_is_aborted_at_wall_deadline(self):
        release = threading.Event()

        class BlockingClient:
            def __init__(self):
                self.close = Mock(side_effect=release.set)

            def request(self, **_kwargs):
                release.wait(1.0)
                return response(200)

        client = BlockingClient()
        with http_client._shared_client_lock:
            http_client._shared_clients[threading.get_ident()] = client
        with self.assertRaises(http_client.RequestDeadlineExceeded):
            http_client.make_request(
                "GET",
                "https://example.test/resource",
                max_retries=3,
                deadline_seconds=0.02,
            )
        client.close.assert_called_once()

    def test_response_completed_after_deadline_is_rejected(self):
        late = response(200)
        client = ScriptedClient([late])
        ticks = iter((0.0, 0.0, 2.0))

        with self.assertRaises(http_client.RequestDeadlineExceeded):
            http_client.make_request(
                "GET",
                "https://example.test/resource",
                max_retries=1,
                client=client,
                clock=lambda: next(ticks),
                deadline_seconds=1.0,
            )
        self.assertTrue(late.is_closed)

    def test_deadline_never_raises_when_request_succeeds_quickly(self):
        client = ScriptedClient([response(200)])
        sleeper = Mock()

        result = http_client.make_request(
            "GET",
            "https://example.test/resource",
            max_retries=3,
            client=client,
            sleep=sleeper,
            clock=lambda: 1.0,
            deadline_seconds=5.0,
        )

        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(client.requests), 1)

    def test_preserves_request_options_and_source_timeout_default(self):
        client = ScriptedClient([response(200)])
        headers = {"Authorization": "Bearer secret", "X-Test": "value"}
        body = {"hello": "world"}

        http_client.make_request(
            "post",
            "https://example.test/resource",
            params={"q": "term"},
            headers=headers,
            json_body=body,
            follow_redirects=True,
            max_retries=1,
            client=client,
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
            "POST",
            "https://example.test/llm",
            timeout=120.0,
            max_retries=1,
            client=client,
            deadline_seconds=300.0,
        )

        self.assertEqual(client.requests[0]["timeout"], 120.0)

    def test_attempt_timeout_is_clamped_to_remaining_deadline(self):
        """A single attempt may not outlive the total operation deadline."""
        client = ScriptedClient([response(200)])
        ticks = iter((55.0, 110.0, 110.0, 110.0, 110.0))

        http_client.make_request(
            "GET",
            "https://example.test/llm",
            timeout=120.0,
            max_retries=1,
            client=client,
            clock=lambda: next(ticks),
            deadline_seconds=60.0,
        )

        sent_timeout = client.requests[0]["timeout"]
        self.assertEqual(sent_timeout.connect, 5.0)
        self.assertEqual(sent_timeout.read, 5.0)

    @patch.object(http_client, "logger")
    def test_logs_attempt_metadata_without_sensitive_request_data(self, logger):
        client = ScriptedClient([response(503), response(200)])
        clock = Mock(return_value=10.0)

        http_client.make_request(
            "GET",
            "https://example.test/path?api_key=secret",
            headers={"Authorization": "Bearer secret"},
            max_retries=2,
            client=client,
            sleep=Mock(),
            clock=clock,
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


class PublicOnlyTransportTests(unittest.TestCase):
    def test_shared_implementation_with_api(self):
        """The orchestrator imports the single contracts transport (no
        duplicated network logic in http_client)."""
        from contracts.outbound_transport import PublicOnlyHTTPTransport

        self.assertIs(http_client.PublicOnlyHTTPTransport, PublicOnlyHTTPTransport)

    @staticmethod
    def _public_answer(host, port, *_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))
        ]

    def test_transport_pins_and_reattaches_original_request(self):
        response = httpx.Response(
            200,
            content=b"ok",
            request=httpx.Request("GET", "https://93.184.216.34/doc"),
        )
        transport = http_client.PublicOnlyHTTPTransport()
        request = httpx.Request("GET", "https://public.example.test/doc")
        with (
            patch("socket.getaddrinfo", side_effect=self._public_answer),
            patch.object(
                httpx.HTTPTransport,
                "handle_request",
                return_value=response,
            ) as parent,
        ):
            returned = transport.handle_request(request)
        rewritten = parent.call_args.args[0]
        self.assertEqual(rewritten.url.host, "93.184.216.34")
        self.assertEqual(rewritten.headers["Host"], "public.example.test")
        self.assertEqual(rewritten.extensions["sni_hostname"], "public.example.test")
        self.assertIs(returned.request, request)

    def test_relative_redirect_chain_reresolves_original_host(self):
        transport = http_client.PublicOnlyHTTPTransport()
        first = httpx.Response(
            302,
            headers={"location": "/next"},
            request=httpx.Request("GET", "https://93.184.216.34/first"),
        )
        second = httpx.Response(
            200,
            json={"ok": True},
            request=httpx.Request("GET", "https://93.184.216.34/next"),
        )
        seen = []

        def fake_parent(_self, hop_request):
            seen.append(hop_request)
            return first if len(seen) == 1 else second

        with (
            patch.object(httpx.HTTPTransport, "handle_request", fake_parent),
            patch("socket.getaddrinfo", side_effect=self._public_answer),
        ):
            with httpx.Client(transport=transport, follow_redirects=True) as client:
                response = client.get("https://public.example.test/first")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(seen), 2)
        for hop in seen:
            self.assertEqual(hop.url.host, "93.184.216.34")
            self.assertEqual(hop.headers["Host"], "public.example.test")
            self.assertEqual(hop.extensions["sni_hostname"], "public.example.test")
        self.assertEqual(response.request.url.host, "public.example.test")

    def test_transport_rejects_private_resolution_at_send_time(self):
        transport = http_client.PublicOnlyHTTPTransport()
        request = httpx.Request("GET", "https://127.0.0.1/steal")
        with patch(
            "socket.getaddrinfo",
            side_effect=lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
            ],
        ):
            with self.assertRaises(ValueError):
                transport.handle_request(request)

    def test_distinct_origins_never_share_child_transport(self):
        """Two hosts resolving to the same CDN IP must never share a pooled
        TLS connection: each original origin gets its own child transport."""
        transport = http_client.PublicOnlyHTTPTransport()
        response = httpx.Response(
            200,
            content=b"ok",
            request=httpx.Request("GET", "https://93.184.216.34/x"),
        )

        def send(url):
            with (
                patch("socket.getaddrinfo", side_effect=self._public_answer),
                patch.object(
                    httpx.HTTPTransport,
                    "handle_request",
                    return_value=response,
                ),
            ):
                transport.handle_request(httpx.Request("GET", url))

        send("https://alpha.example.test/a")
        send("https://beta.example.test/b")
        self.assertEqual(len(transport._child_transports), 2)

        first_child = transport._child_transports[("https", "alpha.example.test", 443)]
        send("https://alpha.example.test/c")
        self.assertIs(
            transport._child_transports[("https", "alpha.example.test", 443)],
            first_child,
        )
        self.assertEqual(len(transport._child_transports), 2)

        with patch.object(httpx.HTTPTransport, "close") as close_mock:
            transport.close()
        self.assertGreaterEqual(close_mock.call_count, 2)
        self.assertEqual(transport._child_transports, {})


class SharedClientLifecycleTests(unittest.TestCase):
    def tearDown(self):
        close = getattr(http_client, "close_shared_client", None)
        if close is not None:
            close()

    def test_shared_client_uses_pinned_public_transport(self):
        """The shared client is built on the resolve-and-pin transport so
        every make_request send re-validates DNS and pins the connection."""
        with (
            patch("http_client.httpx.Client") as client_class,
            patch.object(http_client, "PublicOnlyHTTPTransport") as transport_class,
        ):
            shared = http_client.get_shared_client()
        self.assertIs(shared, client_class.return_value)
        client_class.assert_called_once_with(
            transport=transport_class.return_value,
            follow_redirects=False,
        )

    @patch.object(http_client.httpx, "Client")
    def test_repeated_requests_reuse_one_client_until_shutdown(self, client_class):
        shared = ScriptedClient([response(200), response(200)])
        client_class.return_value = shared

        first = http_client.make_request(
            "GET",
            "https://example.test/one",
            max_retries=1,
        )
        second = http_client.make_request(
            "GET",
            "https://example.test/two",
            max_retries=1,
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        client_class.assert_called_once()
        shared.close.assert_not_called()

        http_client.close_shared_client()
        http_client.close_shared_client()
        shared.close.assert_called_once()

    def test_one_thread_timeout_does_not_close_peer_provider_pool(self):
        http_client.close_shared_client()
        entered = threading.Event()
        release = threading.Event()

        class BlockingClient:
            def __init__(self):
                self.close = Mock(side_effect=release.set)

            def request(self, **_kwargs):
                entered.set()
                release.wait(1)
                return response(200)

        slow = BlockingClient()
        peer = ScriptedClient([response(200)])
        errors = []

        def run_slow():
            try:
                http_client.make_request(
                    "GET",
                    "https://slow.example.test/resource",
                    max_retries=1,
                    deadline_seconds=0.2,
                )
            except Exception as exc:
                errors.append(exc)

        with patch.object(http_client.httpx, "Client", side_effect=[slow, peer]):
            thread = threading.Thread(target=run_slow)
            thread.start()
            if not entered.wait(1):
                thread.join(timeout=1)
                self.fail(f"slow request did not start: {errors!r}")
            result = http_client.make_request(
                "GET",
                "https://peer.example.test/resource",
                max_retries=1,
            )
            thread.join(timeout=1)

        self.assertEqual(result.status_code, 200)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], http_client.RequestDeadlineExceeded)
        slow.close.assert_called_once()
        peer.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
