import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.oanda import OandaCollector
from collectors.fred import FredCollector


class FredEdgeCaseTests(unittest.TestCase):
    """Task 9: Per-series failure reporting for FRED collector."""

    def _make_fred_config(self, **overrides):
        base = {
            "collectors": {
                "fred": {
                    "api_key": "test-key",
                    "schedule": "0 6 * * *",
                    "series": [
                        {"id": "GDP", "frequency": "quarterly"},
                        {"id": "CPI", "frequency": "monthly"},
                        {"id": "UNRATE", "frequency": "monthly"},
                    ],
                }
            }
        }
        base["collectors"]["fred"].update(overrides)
        return base

    @patch("collectors.fred.make_request")
    def test_all_series_fail_returns_empty_with_errors(self, make_request):
        """When all series fail, collect returns CollectionResult with empty records and tracked errors."""
        collector = FredCollector()

        # Simulate network failure for all requests
        make_request.side_effect = httpx.ConnectError("Connection refused")

        result = collector.collect(
            self._make_fred_config(), correlation_id="test-cid"
        )

        # Task 9: collect() now returns CollectionResult, not bare list
        from collectors.base import CollectionResult
        self.assertIsInstance(result, CollectionResult,
                              "collect() should return CollectionResult")
        self.assertEqual(result.records, [],
                         "All series failed → records should be empty")
        self.assertEqual(len(result.errors), 3,
                         "Should have one error per failed series")
        self.assertTrue(result.all_failed,
                        "all_failed should be True when no series succeed")

        # Backward compat: collector.last_errors still populated
        self.assertTrue(hasattr(collector, "last_errors"),
                        "Collector should expose last_errors attribute")
        self.assertGreaterEqual(len(collector.last_errors), 1,
                                "Should have at least one per-series error")

    @patch("collectors.fred.make_request")
    def test_some_series_fail_returns_partial_with_errors(self, make_request):
        """When some series fail, records for successful ones are returned."""
        collector = FredCollector()

        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            url = kwargs.get("url", args[1] if len(args) > 1 else "")
            params = kwargs.get("params", {})
            series_id = params.get("series_id", "")
            if "observations" in url:
                # First series (GDP) succeeds, second (CPI) fails, third (UNRATE) succeeds
                if series_id == "GDP":
                    resp = Mock()
                    resp.status_code = 200
                    resp.json.return_value = {"observations": [
                        {"date": "2024-01-01", "value": "100.0"}
                    ]}
                    resp.raise_for_status.return_value = None
                    return resp
                elif series_id == "CPI":
                    raise httpx.ConnectError("timeout")
                elif series_id == "UNRATE":
                    resp = Mock()
                    resp.status_code = 200
                    resp.json.return_value = {"observations": [
                        {"date": "2024-01-01", "value": "3.5"}
                    ]}
                    resp.raise_for_status.return_value = None
                    return resp
            # For series metadata calls
            resp = Mock()
            resp.status_code = 200
            resp.json.return_value = {"seriess": [{"title": "Test", "units": "pct",
                                                    "seasonal_adjustment": "SA",
                                                    "frequency": "Monthly"}]}
            resp.raise_for_status.return_value = None
            return resp

        make_request.side_effect = side_effect

        result = collector.collect(
            self._make_fred_config(), correlation_id="test-cid"
        )

        # Task 9: collect() now returns CollectionResult
        from collectors.base import CollectionResult
        self.assertIsInstance(result, CollectionResult)

        # Should have records from GDP and UNRATE, but not CPI
        series_ids = {r["series_id"] for r in result.records}
        self.assertIn("GDP", series_ids)
        self.assertIn("UNRATE", series_ids)
        self.assertNotIn("CPI", series_ids,
                         "CPI series failed → no records for it")

        # Check error reporting on the CollectionResult
        self.assertEqual(len(result.errors), 1,
                         "Should have exactly one error (CPI)")
        self.assertEqual(result.errors[0]["series_id"], "CPI")
        self.assertTrue(result.partial_failure,
                        "partial_failure should be True")

        # Backward compat: collector.last_errors still populated
        self.assertTrue(hasattr(collector, "last_errors"),
                        "Collector should expose last_errors after partial failure")
        self.assertGreaterEqual(len(collector.last_errors), 1,
                                "Should report per-series failure for CPI")

    def test_last_errors_initialized_empty(self):
        """last_errors is initialized as empty list before any collection."""
        collector = FredCollector()
        self.assertEqual(collector.last_errors, [],
                         "last_errors should start empty")


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
