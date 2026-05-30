import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.forex_factory import ForexFactoryCollector


CONFIG = {
    "timezone": {"primary": {"name": "Europe/London", "label": "London"}},
    "collectors": {
        "forex_factory": {
            "source_url": "https://www.forexfactory.com/calendar",
            "weekly_export_base_url": "https://nfs.faireconomy.media",
            "currencies": ["USD", "EUR", "GBP", "JPY", "AUD", "CNY"],
            "min_impact": "medium",
            "user_agent": "test-agent",
        }
    },
}


def target_week():
    collector = ForexFactoryCollector()
    return collector._determine_target_week(
        CONFIG, datetime(2026, 5, 8, 9, tzinfo=ZoneInfo("Europe/London"))
    )


SAMPLE_PAYLOAD = [
    {
        "title": "Non-Farm Employment Change",
        "country": "USD",
        "date": "2026-05-08T08:30:00-04:00",
        "impact": "High",
        "forecast": "65K",
        "previous": "178K",
    },
    {
        "title": "ECB President Lagarde Speaks",
        "country": "EUR",
        "date": "2026-05-08T03:00:00-04:00",
        "impact": "Medium",
        "forecast": "",
        "previous": "",
    },
]


class ForexFactoryCollectorTests(unittest.TestCase):
    def test_cache_hit_avoids_network_call(self):
        collector = ForexFactoryCollector()
        collector._determine_target_week = Mock(return_value=target_week())
        collector._load_cached_payload = Mock(return_value=SAMPLE_PAYLOAD)
        collector._fetch_export_payload = Mock()

        records = collector.collect(CONFIG, "corr")

        self.assertEqual(len(records), 2)
        collector._fetch_export_payload.assert_not_called()

    def test_missing_week_fetches_and_stores_payload(self):
        collector = ForexFactoryCollector()
        collector._determine_target_week = Mock(return_value=target_week())
        collector._load_cached_payload = Mock(return_value=None)
        collector._fetch_export_payload = Mock(return_value=SAMPLE_PAYLOAD)
        collector._store_cached_payload = Mock()

        records = collector.collect(CONFIG, "corr")

        self.assertEqual(len(records), 2)
        collector._fetch_export_payload.assert_called_once()
        collector._store_cached_payload.assert_called_once()

    def test_failed_fetch_uses_existing_cache(self):
        collector = ForexFactoryCollector()
        collector._determine_target_week = Mock(return_value=target_week())
        collector._load_cached_payload = Mock(side_effect=[None, SAMPLE_PAYLOAD])
        collector._fetch_export_payload = Mock(side_effect=RuntimeError("boom"))

        records = collector.collect(CONFIG, "corr")

        self.assertEqual(len(records), 2)
        self.assertEqual(collector._load_cached_payload.call_count, 2)

    def test_weekend_selects_coming_monday_friday_week(self):
        collector = ForexFactoryCollector()
        week = collector._determine_target_week(
            CONFIG, datetime(2026, 5, 9, 9, tzinfo=ZoneInfo("Europe/London"))
        )

        self.assertEqual(week["displayed_week"], "next")
        self.assertEqual(week["week_key"], "2026-W20")
        self.assertEqual(week["period_start"].date().isoformat(), "2026-05-11")
        self.assertEqual(week["period_end"].date().isoformat(), "2026-05-15")

    def test_event_filtering_keeps_only_relevant_high_medium(self):
        collector = ForexFactoryCollector()
        payload = SAMPLE_PAYLOAD + [
            {
                "title": "Bank Holiday",
                "country": "JPY",
                "date": "2026-05-08T02:01:00-04:00",
                "impact": "Holiday",
            },
            {
                "title": "Low Impact Item",
                "country": "USD",
                "date": "2026-05-08T10:00:00-04:00",
                "impact": "Low",
            },
            {
                "title": "Canadian Employment Change",
                "country": "CAD",
                "date": "2026-05-08T08:30:00-04:00",
                "impact": "High",
            },
        ]

        records = collector._parse_export_payload(
            payload=payload,
            target_week=target_week(),
            min_impact="medium",
            currencies={"USD", "EUR", "GBP", "JPY", "AUD", "CNY"},
            payload_source="test",
            correlation_id="corr",
        )

        self.assertEqual(
            [r["event_name"] for r in records],
            ["Non-Farm Employment Change", "ECB President Lagarde Speaks"],
        )


if __name__ == "__main__":
    unittest.main()
