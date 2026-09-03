import os
import socket
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["STATE_DIR"] = "/tmp/trading-api-setup-conn-tests"
os.environ["DEPLOYMENT_MODE"] = "test"
os.environ["DASHBOARD_USER"] = "test"
os.environ["DASHBOARD_PASSWORD"] = "test"

from contracts.outbound_transport import PublicOnlyHTTPTransport  # noqa: E402
from routes.json import setup  # noqa: E402
from routes.json.setup import TestConnectionRequest  # noqa: E402


def _fake_request():
    return SimpleNamespace(session={})


def _public_answer(host, port, *_args, **_kwargs):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))
    ]


class SetupConnectionSecurityTests(unittest.TestCase):
    def test_pre_activation_requires_bootstrap_token(self):
        """The outbound probe is gated by the setup token before activation."""
        strong_token = "0123456789abcdef" * 4
        body = TestConnectionRequest(api_key="key")
        with (
            patch.object(setup, "setup_complete", return_value=False),
            patch.dict(
                os.environ,
                {"DEPLOYMENT_MODE": "production", "SETUP_TOKEN": strong_token},
            ),
        ):
            with self.assertRaises(setup.HTTPException) as raised:
                setup.test_connection(body, _fake_request())
            self.assertEqual(raised.exception.status_code, 403)

            response = httpx.Response(
                200,
                json={"data": []},
                request=httpx.Request(
                    "GET", "https://openrouter.ai/api/v1/models"
                ),
            )
            fake_client = MagicMock()
            fake_client.__enter__.return_value.get.return_value = response
            with (
                patch.object(setup, "_read_secrets", return_value={}),
                patch("socket.getaddrinfo", side_effect=_public_answer),
                patch.object(setup.httpx, "Client", return_value=fake_client),
            ):
                result = setup.test_connection(
                    TestConnectionRequest(api_key="key", token=strong_token),
                    _fake_request(),
                )
            self.assertEqual(result, {"connected": True})

    def test_canonical_origin_with_stored_key(self):
        body = TestConnectionRequest(api_key=None)
        response = httpx.Response(
            200,
            json={"data": []},
            request=httpx.Request("GET", "https://openrouter.ai/api/v1/models"),
        )
        fake_client = MagicMock()
        fake_client.__enter__.return_value.get.return_value = response
        with (
            patch.object(setup, "setup_complete", return_value=False),
            patch.object(
                setup,
                "_read_secrets",
                return_value={"OPENROUTER_API_KEY": "stored-key"},
            ),
            patch("socket.getaddrinfo", side_effect=_public_answer),
            patch.object(setup.httpx, "Client", return_value=fake_client),
        ):
            result = setup.test_connection(body, _fake_request())
        self.assertEqual(result, {"connected": True})
        fake_client.__enter__.return_value.get.assert_called_once_with(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": "Bearer stored-key"},
        )

    def test_missing_key_is_rejected_without_probing(self):
        body = TestConnectionRequest(api_key=None)
        with (
            patch.object(setup, "setup_complete", return_value=False),
            patch.object(setup, "_read_secrets", return_value={}),
            patch.object(setup, "managed_secret", return_value=""),
        ):
            with self.assertRaises(setup.HTTPException) as raised:
                setup.test_connection(body, _fake_request())
        self.assertEqual(raised.exception.status_code, 400)

    def test_unknown_fields_are_rejected(self):
        """No custom provider/base_url surface exists; unknown fields 422."""
        with patch.object(setup, "setup_complete", return_value=False):
            with self.assertRaises(ValueError):
                TestConnectionRequest(
                    api_key="key", base_url="https://evil.example.com/v1"
                )

    def test_cross_origin_redirect_never_receives_authorization(self):
        """A provider redirect to a different origin is rejected outright, so
        the Authorization header is never forwarded off the canonical origin."""
        body = TestConnectionRequest(api_key="key")
        redirect = httpx.Response(
            302,
            headers={"location": "https://evil.example.test/steal"},
            request=httpx.Request(
                "GET", "https://openrouter.ai/api/v1/models"
            ),
        )
        fake_client = MagicMock()
        fake_client.__enter__.return_value.get.return_value = redirect
        with (
            patch.object(setup, "setup_complete", return_value=False),
            patch.object(setup, "_read_secrets", return_value={}),
            patch("socket.getaddrinfo", side_effect=_public_answer),
            patch.object(setup.httpx, "Client", return_value=fake_client),
        ):
            with self.assertRaises(setup.HTTPException) as raised:
                setup.test_connection(body, _fake_request())
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("across origins", raised.exception.detail)
        fake_client.__enter__.return_value.get.assert_called_once()

    def test_same_origin_redirect_is_followed_with_authorization(self):
        body = TestConnectionRequest(api_key="key")
        redirect = httpx.Response(
            302,
            headers={"location": "https://openrouter.ai/api/v1/models?x=1"},
            request=httpx.Request(
                "GET", "https://openrouter.ai/api/v1/models"
            ),
        )
        final = httpx.Response(
            200,
            json={"data": []},
            request=httpx.Request(
                "GET", "https://openrouter.ai/api/v1/models?x=1"
            ),
        )
        fake_client = MagicMock()
        fake_client.__enter__.return_value.get.side_effect = [redirect, final]
        with (
            patch.object(setup, "setup_complete", return_value=False),
            patch.object(setup, "_read_secrets", return_value={}),
            patch("socket.getaddrinfo", side_effect=_public_answer),
            patch.object(setup.httpx, "Client", return_value=fake_client),
        ):
            result = setup.test_connection(body, _fake_request())
        self.assertEqual(result, {"connected": True})
        gets = fake_client.__enter__.return_value.get.call_args_list
        self.assertEqual(len(gets), 2)
        self.assertEqual(
            gets[1].kwargs["headers"]["Authorization"], "Bearer key"
        )

    def test_relative_redirects_resolve_against_current_url(self):
        """Relative Locations accumulate against the current hop URL, not
        the constant base URL."""
        body = TestConnectionRequest(api_key="key")
        hop2 = httpx.Response(
            302,
            headers={"location": "/api/v1/step2"},
            request=httpx.Request("GET", "https://openrouter.ai/api/v1/models"),
        )
        hop3 = httpx.Response(
            302,
            headers={"location": "step3"},
            request=httpx.Request("GET", "https://openrouter.ai/api/v1/step2"),
        )
        final = httpx.Response(
            200,
            json={"data": []},
            request=httpx.Request("GET", "https://openrouter.ai/api/v1/step3"),
        )
        fake_client = MagicMock()
        fake_client.__enter__.return_value.get.side_effect = [hop2, hop3, final]
        with (
            patch.object(setup, "setup_complete", return_value=False),
            patch.object(setup, "_read_secrets", return_value={}),
            patch("socket.getaddrinfo", side_effect=_public_answer),
            patch.object(setup.httpx, "Client", return_value=fake_client),
        ):
            result = setup.test_connection(body, _fake_request())
        self.assertEqual(result, {"connected": True})
        urls = [
            call.args[0]
            for call in fake_client.__enter__.return_value.get.call_args_list
        ]
        self.assertEqual(
            urls,
            [
                "https://openrouter.ai/api/v1/models",
                "https://openrouter.ai/api/v1/step2",
                "https://openrouter.ai/api/v1/step3",
            ],
        )

    def test_redirect_limit_exhaustion_is_rejected(self):
        """A never-ending redirect chain must fail closed, not report a
        successful connection."""
        body = TestConnectionRequest(api_key="key")
        loop = httpx.Response(
            302,
            headers={"location": "/api/v1/loop"},
            request=httpx.Request("GET", "https://openrouter.ai/api/v1/models"),
        )
        fake_client = MagicMock()
        fake_client.__enter__.return_value.get.return_value = loop
        with (
            patch.object(setup, "setup_complete", return_value=False),
            patch.object(setup, "_read_secrets", return_value={}),
            patch("socket.getaddrinfo", side_effect=_public_answer),
            patch.object(setup.httpx, "Client", return_value=fake_client),
        ):
            with self.assertRaises(setup.HTTPException) as raised:
                setup.test_connection(body, _fake_request())
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("redirected too many times", raised.exception.detail)


class PublicOnlyTransportTests(unittest.TestCase):
    def test_transport_pins_validated_address_and_preserves_host_and_sni(self):
        response = httpx.Response(
            200,
            content=b"ok",
            request=httpx.Request("GET", "https://93.184.216.34/doc"),
        )
        transport = PublicOnlyHTTPTransport()
        request = httpx.Request("GET", "https://public.example.test/doc?q=1")
        with (
            patch("socket.getaddrinfo", side_effect=_public_answer),
            patch.object(
                httpx.HTTPTransport,
                "handle_request",
                return_value=response,
            ) as parent,
        ):
            returned = transport.handle_request(request)
        rewritten = parent.call_args.args[0]
        self.assertEqual(rewritten.url.host, "93.184.216.34")
        self.assertEqual(rewritten.url.path, "/doc")
        self.assertEqual(rewritten.url.query, b"q=1")
        self.assertEqual(rewritten.headers["Host"], "public.example.test")
        self.assertEqual(rewritten.extensions["sni_hostname"], "public.example.test")
        # The caller's response must carry the ORIGINAL request/URL, never
        # the internal pinned-IP rewrite (redirect resolution and consumers
        # rely on it).
        self.assertIs(returned.request, request)
        self.assertEqual(returned.request.url.host, "public.example.test")

    def test_relative_redirect_chain_reresolves_original_host(self):
        """Each hop of a relative redirect chain re-resolves the ORIGINAL
        hostname, keeps Host/SNI, and never leaves the pinned IP as the
        response origin."""
        transport = PublicOnlyHTTPTransport()
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
            patch("socket.getaddrinfo", side_effect=_public_answer),
        ):
            with httpx.Client(
                transport=transport, follow_redirects=True
            ) as client:
                response = client.get("https://public.example.test/first")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(seen), 2)
        for hop in seen:
            self.assertEqual(hop.url.host, "93.184.216.34")
            self.assertEqual(hop.headers["Host"], "public.example.test")
            self.assertEqual(hop.extensions["sni_hostname"], "public.example.test")
        self.assertEqual(response.request.url.host, "public.example.test")

    def test_transport_preserves_request_body_through_rewrite(self):
        response = httpx.Response(
            200,
            content=b"ok",
            request=httpx.Request("POST", "https://93.184.216.34/doc"),
        )
        transport = PublicOnlyHTTPTransport()
        request = httpx.Request(
            "POST", "https://public.example.test/doc", content=b"bounded-payload"
        )
        with (
            patch("socket.getaddrinfo", side_effect=_public_answer),
            patch.object(
                httpx.HTTPTransport,
                "handle_request",
                return_value=response,
            ) as parent,
        ):
            transport.handle_request(request)
        rewritten = parent.call_args.args[0]
        self.assertEqual(rewritten.read(), b"bounded-payload")

    def test_transport_rejects_plain_http_origin(self):
        transport = PublicOnlyHTTPTransport()
        with self.assertRaises(ValueError):
            transport.handle_request(httpx.Request("GET", "http://example.test/doc"))

    def test_transport_rejects_private_resolution_at_send_time(self):
        transport = PublicOnlyHTTPTransport()
        request = httpx.Request("GET", "https://127.0.0.1/steal")
        with patch(
            "socket.getaddrinfo",
            side_effect=lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
            ],
        ):
            with self.assertRaises(ValueError):
                transport.handle_request(request)

    def test_transport_fails_closed_on_mixed_answers_at_send_time(self):
        transport = PublicOnlyHTTPTransport()
        request = httpx.Request("GET", "https://rebind.example.test/doc")
        with patch(
            "socket.getaddrinfo",
            side_effect=lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.9", 443)),
            ],
        ):
            with self.assertRaises(ValueError):
                transport.handle_request(request)

    def test_distinct_origins_never_share_child_transport(self):
        """Two hosts resolving to the same CDN IP must never share a pooled
        TLS connection: each original origin gets its own child transport."""
        transport = PublicOnlyHTTPTransport()
        response = httpx.Response(
            200,
            content=b"ok",
            request=httpx.Request("GET", "https://93.184.216.34/x"),
        )

        def send(url):
            with (
                patch("socket.getaddrinfo", side_effect=_public_answer),
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

        # Same original origin reuses its child transport.
        first_child = transport._child_transports[("https", "alpha.example.test", 443)]
        send("https://alpha.example.test/c")
        self.assertIs(
            transport._child_transports[("https", "alpha.example.test", 443)],
            first_child,
        )
        self.assertEqual(len(transport._child_transports), 2)

        # close() releases every child transport (no leaks).
        with patch.object(httpx.HTTPTransport, "close") as close_mock:
            transport.close()
        self.assertGreaterEqual(close_mock.call_count, 2)
        self.assertEqual(transport._child_transports, {})

    def test_transport_pins_ipv6_address_with_bracketed_netloc(self):
        response = httpx.Response(
            200,
            content=b"ok",
            request=httpx.Request("GET", "https://[2606:4700::1]/doc"),
        )
        transport = PublicOnlyHTTPTransport()
        request = httpx.Request("GET", "https://v6.example.test/doc")
        with (
            patch(
                "socket.getaddrinfo",
                side_effect=lambda *args, **kwargs: [
                    (
                        socket.AF_INET6,
                        socket.SOCK_STREAM,
                        6,
                        "",
                        ("2606:4700::1", 443, 0, 0),
                    )
                ],
            ),
            patch.object(
                httpx.HTTPTransport,
                "handle_request",
                return_value=response,
            ) as parent,
        ):
            transport.handle_request(request)
        rewritten = parent.call_args.args[0]
        self.assertEqual(rewritten.url.host, "2606:4700::1")
        self.assertEqual(rewritten.url.netloc, b"[2606:4700::1]")
        self.assertEqual(rewritten.headers["Host"], "v6.example.test")
        self.assertEqual(rewritten.extensions["sni_hostname"], "v6.example.test")


if __name__ == "__main__":
    unittest.main()
