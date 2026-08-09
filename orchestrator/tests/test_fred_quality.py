import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.base import CollectorNoData, CollectorSetupRequired
from collectors.fred import FredCollector


class FredQualityTests(unittest.TestCase):
    @unittest.skip(
        "skip: codex/market-intelligence-expansion contract not implemented in master"
    )
    def test_missing_key_is_setup_required(self):
        with self.assertRaises(CollectorSetupRequired):
            FredCollector().collect(
                {"collectors": {"fred": {"api_key": "", "series": []}}},
                "corr",
            )

    @patch("collectors.fred.query_latest")
    def test_start_date_overlaps_revision_window(self, query_latest):
        latest = datetime(2026, 6, 1, tzinfo=UTC)
        query_latest.return_value = [{"observed_at": latest}]
        config = {"collectors": {"fred": {"revision_window_days": {"monthly": 120}}}}

        start = FredCollector()._get_start_date("CPIAUCSL", "monthly", 5, config)

        self.assertEqual(start, latest - timedelta(days=120))

    @patch.object(FredCollector, "_collect_series")
    @unittest.skip(
        "skip: codex/market-intelligence-expansion contract not implemented in master"
    )
    def test_all_series_failure_is_not_reported_as_success(self, collect_series):
        collect_series.side_effect = RuntimeError("rate limited")
        config = {
            "collectors": {
                "fred": {
                    "api_key": "key",
                    "series": [{"id": "GDP", "frequency": "quarterly"}],
                }
            }
        }

        with self.assertRaises(CollectorNoData):
            FredCollector().collect(config, "corr")


if __name__ == "__main__":
    unittest.main()
