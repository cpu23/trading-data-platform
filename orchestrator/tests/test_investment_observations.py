import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from investment_observations import aggregate_industry_history


class InvestmentObservationTests(unittest.TestCase):
    def test_history_separates_unique_reports_news_and_deterministic_facts(self):
        observed_at = datetime(2026, 7, 31, tzinfo=UTC)
        rows = [
            {
                "source_kind": "report",
                "source_id": "report-1",
                "observed_at": observed_at,
                "industry": "Semiconductors & Compute",
                "symbol": "CHIP",
                "score": 8,
                "metrics": {
                    "revenue": {"value": 120},
                    "net_income": {"value": None},
                },
            },
            {
                "source_kind": "news",
                "source_id": "news-1",
                "observed_at": observed_at,
                "industry": "Semiconductors & Compute",
                "themes": ["ai_demand", "capex"],
            },
            {
                "source_kind": "news",
                "source_id": "news-1",
                "observed_at": observed_at,
                "industry": "Semiconductors & Compute",
                "themes": ["ai_demand"],
            },
        ]

        result = aggregate_industry_history(rows)

        self.assertEqual(len(result), 1)
        point = result[0]["points"][0]
        self.assertEqual(point["report_count"], 1)
        self.assertEqual(point["news_count"], 1)
        self.assertEqual(point["company_count"], 1)
        self.assertEqual(point["deterministic_metric_count"], 1)
        self.assertEqual(point["average_score"], 8.0)
        self.assertEqual(point["themes"][0], "ai_demand")


if __name__ == "__main__":
    unittest.main()
