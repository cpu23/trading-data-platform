import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.oanda import OandaCollector  # noqa: E402
from provider_origins import validate_configured_origin  # noqa: E402


class ProviderOriginValidationTests(unittest.TestCase):
    def test_custom_http_origin_is_rejected_by_default(self):
        with self.assertRaisesRegex(ValueError, "must use https"):
            validate_configured_origin(
                "http://provider.example.test/v1", {}, label="test provider"
            )

    def test_custom_private_origin_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-public address"):
            validate_configured_origin("https://10.0.0.5/v1", {}, label="test provider")

    def test_local_origin_is_rejected_without_escape_hatch(self):
        """There is no runtime private/local-provider opt-in: every
        configured provider origin must be HTTPS and public."""
        with self.assertRaisesRegex(ValueError, "non-public address"):
            validate_configured_origin(
                "https://127.0.0.1/v1",
                {"allow_private_origin": True},
                label="test provider",
            )

    def test_missing_origin_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "required"):
            validate_configured_origin(None, {}, label="test provider")

    def test_public_https_origin_passes(self):
        with patch(
            "socket.getaddrinfo",
            side_effect=lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
            ],
        ):
            url = validate_configured_origin(
                "https://provider.example.test/v1", {}, label="test provider"
            )
        self.assertEqual(url, "https://provider.example.test/v1")


class OandaOriginTests(unittest.TestCase):
    def setUp(self):
        self.collector = OandaCollector()

    def test_canonical_base_urls_are_accepted(self):
        from collectors.oanda import DEFAULT_BASE_URLS

        for canonical in DEFAULT_BASE_URLS.values():
            base = self.collector._get_base_url({"base_url": canonical})
            self.assertEqual(base, canonical)

    def test_custom_private_base_url_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid OANDA base_url"):
            self.collector._get_base_url(
                {"base_url": "https://10.0.0.5", "allow_private_origin": True}
            )

    def test_custom_public_base_url_passes(self):
        with patch(
            "socket.getaddrinfo",
            side_effect=lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
            ],
        ):
            base = self.collector._get_base_url(
                {"base_url": "https://custom.example.test"}
            )
        self.assertEqual(base, "https://custom.example.test")


class StreamClientSelectionTests(unittest.TestCase):
    def test_stream_always_uses_pinned_transport_without_redirects(self):
        """The streaming client is always built on the resolve-and-pin public
        transport with redirects disabled: the bearer credential never
        follows a Location (a 3xx is a failure, no second send)."""
        import price_stream

        with (
            patch("price_stream.httpx.Client") as client_class,
            patch.object(price_stream, "PublicOnlyHTTPTransport") as transport_class,
        ):
            price_stream._stream_client({})
        client_class.assert_called_once_with(
            transport=transport_class.return_value,
            timeout=None,
            follow_redirects=False,
        )

    def test_stream_config_with_private_flag_still_pinned(self):
        import price_stream

        with (
            patch("price_stream.httpx.Client") as client_class,
            patch.object(price_stream, "PublicOnlyHTTPTransport") as transport_class,
        ):
            price_stream._stream_client({"allow_private_origin": True})
        client_class.assert_called_once_with(
            transport=transport_class.return_value,
            timeout=None,
            follow_redirects=False,
        )


class ReutersSitemapOriginTests(unittest.TestCase):
    def test_child_sitemap_private_url_is_rejected(self):
        import sources.reuters as reuters

        with self.assertRaisesRegex(ValueError, "invalid sitemap URL"):
            reuters._validated_sitemap_url("https://10.0.0.9/sitemap.xml")

    def test_child_sitemap_plain_http_is_rejected(self):
        import sources.reuters as reuters

        with self.assertRaisesRegex(ValueError, "must use https"):
            reuters._validated_sitemap_url("http://www.reuters.com/sitemap.xml")

    def test_child_sitemap_non_reuters_host_is_rejected(self):
        import sources.reuters as reuters

        with patch(
            "socket.getaddrinfo",
            side_effect=lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
            ],
        ):
            with self.assertRaisesRegex(ValueError, "must be on www.reuters.com"):
                reuters._validated_sitemap_url("https://evil.example.test/sitemap.xml")

    def test_child_sitemap_oversize_response_fails_closed(self):
        import sources.reuters as reuters

        fake_response = MagicMock()
        fake_response.raise_for_status = MagicMock()
        fake_response.iter_bytes.return_value = [b"x" * (reuters.MAX_SITEMAP_BYTES + 1)]
        fake_client = MagicMock()
        fake_client.stream.return_value.__enter__.return_value = fake_response
        with (
            patch.object(reuters, "get_shared_client", return_value=fake_client),
            patch(
                "socket.getaddrinfo",
                side_effect=lambda *args, **kwargs: [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
                ],
            ),
        ):
            with self.assertRaises(reuters._SitemapPageFetchError):
                reuters._parse_sitemap_page(
                    "https://www.reuters.com/sitemap.xml", set(), {}
                )


class KobeissiCredentialForwardingTests(unittest.TestCase):
    def test_kobeissi_uses_pinned_client_without_redirects(self):
        """Kobeissi fetches go through the shared resolve-and-pin client
        with redirects disabled: the X-API-Key credential is origin-bound and
        a poisoned/rebound api.twitterapi.io cannot reach private networks."""
        import sources.kobeissi as kobeissi

        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def raise_for_status(self):
                pass

            def iter_bytes(self):
                return [b'{"status":"success","data":{"tweets":[]}}']

        class FakeClient:
            def stream(self, *args, **kwargs):
                captured["method"], captured["url"] = args
                captured["kwargs"] = kwargs
                return FakeResponse()

        with (
            patch.object(kobeissi, "get_shared_client", return_value=FakeClient()),
            patch.object(kobeissi, "atomic_write_json"),
        ):
            kobeissi.run_kobeissi(
                {"kobeissi": {"api_key": "secret-key", "user_id": "1"}},
                count=5,
            )

        self.assertEqual(captured["method"], "GET")
        self.assertIn("api.twitterapi.io", captured["url"])
        self.assertEqual(captured["kwargs"]["headers"]["X-API-Key"], "secret-key")
        self.assertFalse(captured["kwargs"]["follow_redirects"])

    def test_kobeissi_redirect_fails_closed_without_second_send(self):
        import sources.kobeissi as kobeissi

        sent = []

        class RedirectResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            status_code = 302

            def iter_bytes(self):
                return [b""]

        class FakeClient:
            def stream(self, *args, **kwargs):
                sent.append((args, kwargs))
                return RedirectResponse()

        with (
            patch.object(kobeissi, "get_shared_client", return_value=FakeClient()),
            patch.object(kobeissi, "atomic_write_json"),
        ):
            result = kobeissi.run_kobeissi(
                {"kobeissi": {"api_key": "secret-key", "user_id": "1"}},
                count=5,
            )

        self.assertEqual(result.status, "error")
        # Exactly one send: the 3xx was never followed to a second request.
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0][1]["follow_redirects"])

    def test_kobeissi_oversize_response_fails_closed(self):
        import sources.kobeissi as kobeissi

        class OversizeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            status_code = 200

            def raise_for_status(self):
                pass

            def iter_bytes(self):
                return [b"x" * (kobeissi.MAX_KOBEISSI_BYTES + 1)]

        class FakeClient:
            def stream(self, *args, **kwargs):
                return OversizeResponse()

        with (
            patch.object(kobeissi, "get_shared_client", return_value=FakeClient()),
            patch.object(kobeissi, "atomic_write_json"),
        ):
            result = kobeissi.run_kobeissi(
                {"kobeissi": {"api_key": "secret-key", "user_id": "1"}},
                count=5,
            )

        self.assertEqual(result.status, "error")


if __name__ == "__main__":
    unittest.main()
