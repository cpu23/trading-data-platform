import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.fred import FredCollector
from collectors.oanda import OandaCollector


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

    @staticmethod
    def _cached_metadata(*, table_name, filters, **kwargs):
        if table_name == "macro_series":
            return []
        return [
            {
                "series_id": filters["series_id"],
                "title": "Test",
                "units": "pct",
                "seasonal_adjustment": "SA",
                "frequency": "Monthly",
                "fetched_at": datetime.now(UTC) - timedelta(seconds=1),
            }
        ]

    @patch("collectors.fred.query_latest")
    @patch("collectors.fred.make_request")
    def test_all_series_fail_returns_empty_with_errors(
        self, make_request, query_latest
    ):
        """When all series fail, collect returns CollectionResult with empty records and tracked errors."""
        collector = FredCollector()
        query_latest.side_effect = self._cached_metadata

        # Simulate network failure for all requests
        make_request.side_effect = httpx.ConnectError("Connection refused")

        result = collector.collect(self._make_fred_config(), correlation_id="test-cid")

        # Task 9: collect() now returns CollectionResult, not bare list
        from collectors.base import CollectionResult

        self.assertIsInstance(
            result, CollectionResult, "collect() should return CollectionResult"
        )
        self.assertEqual(
            result.records, [], "All series failed → records should be empty"
        )
        self.assertEqual(
            len(result.errors), 3, "Should have one error per failed series"
        )
        self.assertTrue(
            result.all_failed, "all_failed should be True when no series succeed"
        )

        # Backward compat: collector.last_errors still populated
        self.assertTrue(
            hasattr(collector, "last_errors"),
            "Collector should expose last_errors attribute",
        )
        self.assertGreaterEqual(
            len(collector.last_errors), 1, "Should have at least one per-series error"
        )

    @patch("collectors.fred.query_latest")
    @patch("collectors.fred.make_request")
    def test_some_series_fail_returns_partial_with_errors(
        self, make_request, query_latest
    ):
        """When some series fail, records for successful ones are returned."""
        collector = FredCollector()
        query_latest.side_effect = self._cached_metadata

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
                    resp.json.return_value = {
                        "observations": [{"date": "2024-01-01", "value": "100.0"}]
                    }
                    resp.raise_for_status.return_value = None
                    return resp
                elif series_id == "CPI":
                    raise httpx.ConnectError("timeout")
                elif series_id == "UNRATE":
                    resp = Mock()
                    resp.status_code = 200
                    resp.json.return_value = {
                        "observations": [{"date": "2024-01-01", "value": "3.5"}]
                    }
                    resp.raise_for_status.return_value = None
                    return resp
            # For series metadata calls
            resp = Mock()
            resp.status_code = 200
            resp.json.return_value = {
                "seriess": [
                    {
                        "title": "Test",
                        "units": "pct",
                        "seasonal_adjustment": "SA",
                        "frequency": "Monthly",
                    }
                ]
            }
            resp.raise_for_status.return_value = None
            return resp

        make_request.side_effect = side_effect

        result = collector.collect(self._make_fred_config(), correlation_id="test-cid")

        # Task 9: collect() now returns CollectionResult
        from collectors.base import CollectionResult

        self.assertIsInstance(result, CollectionResult)

        # Should have records from GDP and UNRATE, but not CPI
        series_ids = {r["series_id"] for r in result.records}
        self.assertIn("GDP", series_ids)
        self.assertIn("UNRATE", series_ids)
        self.assertNotIn("CPI", series_ids, "CPI series failed → no records for it")

        # Check error reporting on the CollectionResult
        self.assertEqual(len(result.errors), 1, "Should have exactly one error (CPI)")
        self.assertEqual(result.errors[0]["series_id"], "CPI")
        self.assertTrue(result.partial_failure, "partial_failure should be True")

        # Backward compat: collector.last_errors still populated
        self.assertTrue(
            hasattr(collector, "last_errors"),
            "Collector should expose last_errors after partial failure",
        )
        self.assertGreaterEqual(
            len(collector.last_errors), 1, "Should report per-series failure for CPI"
        )

    def test_last_errors_initialized_empty(self):
        """last_errors is initialized as empty list before any collection."""
        collector = FredCollector()
        self.assertEqual(collector.last_errors, [], "last_errors should start empty")


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


class FreeCollectorRegistryTests(unittest.TestCase):
    """The executable collector registry covers every KNOWN_COLLECTORS id."""

    def test_known_collectors_equal_executable_registry(self):
        from collectors import STANDALONE_COLLECTORS, get_all_collectors

        from contracts.runtime_config import KNOWN_COLLECTORS

        executable = set(get_all_collectors()) | set(STANDALONE_COLLECTORS)
        self.assertEqual(set(KNOWN_COLLECTORS), executable)
        # OANDA stays standalone (excluded from dependency cycles); it must
        # still be a KNOWN_COLLECTORS id so config and API agree.
        self.assertIn("oanda", KNOWN_COLLECTORS)
        self.assertNotIn("oanda", get_all_collectors())
        self.assertIn("oanda", STANDALONE_COLLECTORS)

    def test_every_known_collector_id_dispatches(self):
        from collectors import get_collector

        from contracts.runtime_config import KNOWN_COLLECTORS

        for source_id in sorted(KNOWN_COLLECTORS):
            with self.subTest(source_id=source_id):
                collector = get_collector(source_id)
                self.assertEqual(collector.source_id, source_id)

    def test_new_free_ids_dispatch_to_their_collectors(self):
        from collectors import get_collector
        from collectors.cboe_options import CboeOptionsCollector
        from collectors.company_expectations import CompanyExpectationsCollector
        from collectors.issuer_news import IssuerNewsCollector
        from collectors.issuer_transcripts import IssuerTranscriptsCollector
        from collectors.public_equities import PublicEquitiesCollector
        from collectors.public_positioning import (
            FinraShortVolumeCollector,
            SecForm4Collector,
        )

        expected = {
            "issuer_news": IssuerNewsCollector,
            "issuer_transcripts": IssuerTranscriptsCollector,
            "public_equities": PublicEquitiesCollector,
            "sec_form4": SecForm4Collector,
            "finra_short_volume": FinraShortVolumeCollector,
            "cboe_options": CboeOptionsCollector,
            "company_expectations": CompanyExpectationsCollector,
        }
        for source_id, collector_type in expected.items():
            with self.subTest(source_id=source_id):
                self.assertIsInstance(get_collector(source_id), collector_type)


if __name__ == "__main__":
    unittest.main()
