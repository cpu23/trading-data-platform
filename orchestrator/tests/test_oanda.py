import unittest
from datetime import UTC
from unittest.mock import Mock, patch

import httpx

from collectors import get_all_collectors
from collectors.oanda import OandaCollector
from price_stream import QuoteStream


class OandaCollectorTests(unittest.TestCase):
    def test_oanda_snapshot_collector_is_not_registered_for_cycles(self):
        self.assertNotIn("oanda", get_all_collectors())

    def test_production_without_stream_does_not_simulate_prices(self):
        stream = QuoteStream()

        stream.start(
            {
                "demo": {"enabled": False},
                "collectors": {"oanda": {"stream_enabled": False}},
            }
        )

        self.assertEqual(stream.state["status"], "disabled")
        self.assertEqual(stream.quotes, {})
        self.assertIsNone(stream._thread)

    def test_disabled_oanda_source_does_not_start_live_stream(self):
        stream = QuoteStream()

        stream.start(
            {
                "demo": {"enabled": False},
                "collectors": {"oanda": {"enabled": False, "stream_enabled": True}},
            }
        )

        self.assertEqual(stream.state["status"], "disabled")
        self.assertIsNone(stream._thread)

    def test_parse_oanda_nanosecond_timestamp_as_utc(self):
        collector = OandaCollector()

        parsed = collector._parse_oanda_time("2016-10-17T15:16:40.123456789Z")

        self.assertEqual(parsed.tzinfo, UTC)
        self.assertEqual(parsed.isoformat(), "2016-10-17T15:16:40.123456+00:00")

    def test_base_url_defaults_to_live(self):
        collector = OandaCollector()

        self.assertEqual(
            collector._get_base_url({}),
            "https://api-fxtrade.oanda.com",
        )

    def test_extract_mid_price_from_bid_ask(self):
        collector = OandaCollector()

        price = collector._extract_mid_price(
            {
                "bids": [{"price": "1.1000"}],
                "asks": [{"price": "1.1004"}],
            }
        )

        self.assertAlmostEqual(price, 1.1002)

    @patch("collectors.oanda.make_request")
    def test_handles_network_timeout(self, make_request):
        """health_check returns unhealthy on httpx.TimeoutException."""
        collector = OandaCollector()

        make_request.side_effect = httpx.TimeoutException("Connection timed out")

        config = {
            "collectors": {
                "oanda": {
                    "api_key": "test-key",
                    "environment": "practice",
                }
            }
        }

        result = collector.health_check(config)

        self.assertFalse(result["healthy"])
        self.assertIn("unreachable", result["message"].lower())
        self.assertGreaterEqual(result["latency_ms"], 0)

    @patch("collectors.oanda.make_request")
    def test_handles_malformed_api_response(self, make_request):
        """health_check handles non-200 or malformed responses gracefully."""
        collector = OandaCollector()

        response = Mock()
        response.status_code = 500
        response.text = "Internal Server Error"
        make_request.return_value = response

        config = {
            "collectors": {
                "oanda": {
                    "api_key": "test-key",
                    "environment": "practice",
                }
            }
        }

        result = collector.health_check(config)

        self.assertFalse(result["healthy"])
        self.assertIn("500", result["message"])
        self.assertGreaterEqual(result["latency_ms"], 0)


if __name__ == "__main__":
    unittest.main()
