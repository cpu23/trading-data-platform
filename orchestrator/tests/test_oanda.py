import unittest
from datetime import timezone

from collectors.oanda import OandaCollector


class OandaCollectorTests(unittest.TestCase):
    def test_parse_oanda_nanosecond_timestamp_as_utc(self):
        collector = OandaCollector()

        parsed = collector._parse_oanda_time("2016-10-17T15:16:40.123456789Z")

        self.assertEqual(parsed.tzinfo, timezone.utc)
        self.assertEqual(parsed.isoformat(), "2016-10-17T15:16:40.123456+00:00")

    def test_base_url_defaults_to_live(self):
        collector = OandaCollector()

        self.assertEqual(
            collector._get_base_url({}),
            "https://api-fxtrade.oanda.com",
        )

    def test_extract_mid_price_from_bid_ask(self):
        collector = OandaCollector()

        price = collector._extract_mid_price({
            "bids": [{"price": "1.1000"}],
            "asks": [{"price": "1.1004"}],
        })

        self.assertAlmostEqual(price, 1.1002)


if __name__ == "__main__":
    unittest.main()
