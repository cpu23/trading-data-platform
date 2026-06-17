import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.oanda import OandaCollector


class CollectorEdgeCaseTests(unittest.TestCase):
    """Edge-case / error-path tests for collectors.

    Uses OandaCollector as the test subject since it has a simple
    health_check() that exercises the http_client.make_request path.
    """

    def _make_oanda_config(self, **overrides):
        base = {
            "collectors": {
                "oanda": {
                    "api_key": "test-key",
                    "environment": "practice",
                }
            }
        }
        base["collectors"]["oanda"].update(overrides)
        return base

    # ── network-level failures ──────────────────────────────────────

    @patch("collectors.oanda.make_request")
    def test_network_connect_error_yields_unhealthy(self, make_request):
        """Collector health_check returns unhealthy on ConnectError."""
        collector = OandaCollector()
        make_request.side_effect = httpx.ConnectError("Connection refused")

        result = collector.health_check(self._make_oanda_config())

        self.assertFalse(result["healthy"])
        self.assertIn("unreachable", result["message"].lower())
        self.assertGreaterEqual(result["latency_ms"], 0)

    @patch("collectors.oanda.make_request")
    def test_network_timeout_yields_unhealthy(self, make_request):
        """Collector health_check returns unhealthy on TimeoutException."""
        collector = OandaCollector()
        make_request.side_effect = httpx.TimeoutException("timed out")

        result = collector.health_check(self._make_oanda_config())

        self.assertFalse(result["healthy"])
        self.assertIn("unreachable", result["message"].lower())
        self.assertGreaterEqual(result["latency_ms"], 0)

    @patch("collectors.oanda.make_request")
    def test_generic_exception_yields_unhealthy(self, make_request):
        """Collector health_check catches any Exception and reports unhealthy."""
        collector = OandaCollector()
        make_request.side_effect = Exception("something went wrong")

        result = collector.health_check(self._make_oanda_config())

        self.assertFalse(result["healthy"])
        self.assertIn("unreachable", result["message"].lower())

    # ── API-level failures ──────────────────────────────────────────

    @patch("collectors.oanda.make_request")
    def test_rate_limit_429_yields_unhealthy(self, make_request):
        """HTTP 429 (rate limit) is treated as unhealthy."""
        collector = OandaCollector()

        response = Mock()
        response.status_code = 429
        response.text = "Too Many Requests"
        make_request.return_value = response

        result = collector.health_check(self._make_oanda_config())

        self.assertFalse(result["healthy"])
        self.assertIn("429", result["message"])

    @patch("collectors.oanda.make_request")
    def test_empty_response_body_handled(self, make_request):
        """Malformed / empty response is handled without crashing."""
        collector = OandaCollector()

        response = Mock()
        response.status_code = 200
        response.text = ""
        # Simulate .json() raising a parse error
        response.json.side_effect = ValueError("No JSON could be decoded")
        make_request.return_value = response

        # health_check only inspects status_code, so a 200 with
        # unparseable body still reports healthy — but it must not crash.
        result = collector.health_check(self._make_oanda_config())

        self.assertTrue(result["healthy"])
        self.assertIn("reachable", result["message"].lower())

    # ── missing configuration ───────────────────────────────────────

    def test_missing_api_key_reports_unhealthy(self):
        """health_check returns unhealthy immediately when api_key is absent."""
        collector = OandaCollector()

        config = {"collectors": {"oanda": {}}}
        result = collector.health_check(config)

        self.assertFalse(result["healthy"])
        self.assertEqual(result["message"], "OANDA_API_KEY is not set")
        self.assertEqual(result["latency_ms"], 0)


if __name__ == "__main__":
    unittest.main()
