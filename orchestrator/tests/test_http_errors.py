"""Focused tests for the credential-safe external HTTP error sanitizer."""

import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from http_errors import SafeHTTPError, safe_error_message, safe_http_error


def _status_error(url: str, status: int = 403, reason: str = "Forbidden"):
    request = httpx.Request("GET", url)
    response = httpx.Response(status, request=request, content=b"")
    return httpx.HTTPStatusError(
        f"{status} Client Error: {reason} for url '{url}'",
        request=request,
        response=response,
    )


class SafeErrorMessageTests(unittest.TestCase):
    def test_fred_api_key_query_is_never_present(self):
        secret = "SENTINEL-FRED-KEY"
        exc = _status_error(
            f"https://api.stlouisfed.org/fred/series?api_key={secret}"
            "&series_id=GDP&file_type=json"
        )
        message = safe_error_message(exc, provider="fred")
        self.assertNotIn(secret, message)
        self.assertNotIn("api_key", message)
        self.assertNotIn("series_id", message)
        self.assertNotIn("file_type", message)
        # Safe actionable context is retained: host and path survive.
        self.assertIn("api.stlouisfed.org", message)
        self.assertIn("/fred/series", message)

    def test_opendart_crtfc_key_query_is_never_present(self):
        secret = "SENTINEL-DART-KEY"
        exc = _status_error(
            f"https://opendart.fss.or.kr/api/list.json?crtfc_key={secret}"
            "&corp_code=00126380&page_count=100"
        )
        message = safe_error_message(exc, provider="opendart")
        self.assertNotIn(secret, message)
        self.assertNotIn("crtfc_key", message)
        self.assertNotIn("corp_code", message)
        self.assertIn("opendart.fss.or.kr", message)
        self.assertIn("/api/list.json", message)

    def test_userinfo_and_query_are_scrubbed_from_arbitrary_messages(self):
        secret_user = "SENTINEL-USER"
        secret_token = "SENTINEL-TOKEN"
        message = safe_error_message(
            RuntimeError(
                f"fetch failed for https://{secret_user}:s3cret@host.example/"
                f"data?token={secret_token}&page=2"
            )
        )
        self.assertNotIn(secret_user, message)
        self.assertNotIn("s3cret", message)
        self.assertNotIn(secret_token, message)
        self.assertNotIn("page=2", message)
        self.assertIn("host.example/data", message)

    def test_named_credential_values_are_redacted_outside_urls(self):
        secret = "SENTINEL-NAMED-SECRET"
        message = safe_error_message(
            RuntimeError(f"provider rejected api_key={secret} for the request")
        )
        self.assertNotIn(secret, message)
        self.assertIn("api_key", message)

    def test_bearer_and_authorization_values_are_redacted(self):
        secret = "SENTINEL-BEARER-TOKEN"
        message = safe_error_message(
            RuntimeError(f"Authorization: Bearer {secret} was rejected")
        )
        self.assertNotIn(secret, message)
        message2 = safe_error_message(RuntimeError(f"Bearer {secret} expired"))
        self.assertNotIn(secret, message2)

    def test_plain_messages_are_preserved_and_bounded(self):
        exc = httpx.ConnectError(
            "Connection refused", request=httpx.Request("GET", "https://h/p")
        )
        self.assertIn("Connection refused", safe_error_message(exc))
        long_exc = RuntimeError("x" * 5000)
        message = safe_error_message(long_exc, limit=100)
        self.assertLessEqual(len(message), 100)
        message2 = safe_error_message(long_exc)
        self.assertLessEqual(len(message2), 500)

    def test_provider_fallback_when_no_request_url(self):
        message = safe_error_message(RuntimeError("boom"), provider="oecd")
        self.assertIn("oecd", message)

    def test_transport_error_without_request_produces_safe_message(self):
        for exc in (
            httpx.ConnectError("Connection refused"),
            httpx.TimeoutException("timed out"),
            httpx.ReadTimeout("timed out"),
        ):
            with self.subTest(type(exc).__name__):
                # Accessing exc.request raises RuntimeError; diagnostics must
                # still produce a safe message using the provider fallback.
                message = safe_error_message(exc, provider="fred")
                self.assertIn(str(exc), message)
                self.assertIn("fred", message)

    def test_attached_credential_url_stays_redacted_for_transport_errors(self):
        secret = "SENTINEL-TRANSPORT-SECRET"
        exc = httpx.ConnectError(
            "Connection refused",
            request=httpx.Request(
                "GET",
                f"https://user:s3cret@example.test/data?api_key={secret}&page=1",
            ),
        )
        message = safe_error_message(exc, provider="fred")
        self.assertNotIn(secret, message)
        self.assertNotIn("s3cret", message)
        self.assertNotIn("api_key", message)
        self.assertNotIn("page=1", message)
        self.assertIn("example.test", message)
        self.assertIn("/data", message)


class SafeHTTPErrorTests(unittest.TestCase):
    def test_structured_fields_retain_status_origin_path_class(self):
        exc = _status_error(
            "https://api.stlouisfed.org/fred/series?api_key=SENTINEL-FRED-KEY",
            status=401,
            reason="Unauthorized",
        )
        safe = safe_http_error(exc, provider="fred")
        self.assertIsInstance(safe, SafeHTTPError)
        self.assertEqual(safe.error_type, "HTTPStatusError")
        self.assertEqual(safe.status_code, 401)
        self.assertEqual(safe.origin, "api.stlouisfed.org")
        self.assertEqual(safe.path, "/fred/series")
        self.assertNotIn("SENTINEL-FRED-KEY", safe.message)
        self.assertEqual(
            safe.to_dict(),
            {
                "error_type": "HTTPStatusError",
                "message": safe.message,
                "status_code": 401,
                "origin": "api.stlouisfed.org",
                "path": "/fred/series",
            },
        )

    def test_transport_error_has_no_status_and_safe_origin(self):
        request = httpx.Request("GET", "https://example.test/resource")
        exc = httpx.ReadTimeout("timed out", request=request)
        safe = safe_http_error(exc, provider="example")
        self.assertEqual(safe.status_code, None)
        self.assertEqual(safe.origin, "example.test")
        self.assertEqual(safe.path, "/resource")
        self.assertEqual(safe.error_type, "ReadTimeout")
        self.assertIn("timed out", safe.message)

    def test_transport_error_without_request_falls_back_to_provider(self):
        for exc in (
            httpx.ConnectError("Connection refused"),
            httpx.TimeoutException("timed out"),
        ):
            with self.subTest(type(exc).__name__):
                safe = safe_http_error(exc, provider="fred")
                self.assertEqual(safe.error_type, type(exc).__name__)
                self.assertEqual(safe.status_code, None)
                self.assertEqual(safe.origin, "fred")
                self.assertEqual(safe.path, None)
                self.assertIn(str(exc), safe.message)

    def test_attached_credential_url_redacted_in_structured_error(self):
        secret = "SENTINEL-STRUCTURED-SECRET"
        request = httpx.Request(
            "GET", f"https://user:s3cret@example.test/data?api_key={secret}"
        )
        exc = httpx.ConnectError("Connection refused", request=request)
        safe = safe_http_error(exc, provider="fred")
        self.assertEqual(safe.origin, "example.test")
        self.assertEqual(safe.path, "/data")
        self.assertNotIn(secret, safe.message)
        self.assertNotIn("s3cret", safe.message)
        self.assertNotIn("api_key", safe.message)

    def test_status_code_from_response_when_exception_carries_it(self):
        request = httpx.Request("GET", "https://example.test/x")
        response = httpx.Response(503, request=request, content=b"")
        exc = httpx.HTTPStatusError("503 Service Unavailable", request=request, response=response)
        safe = safe_http_error(exc)
        self.assertEqual(safe.status_code, 503)


if __name__ == "__main__":
    unittest.main()
