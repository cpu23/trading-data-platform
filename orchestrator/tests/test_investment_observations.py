import json
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from investment_observations import (
    aggregate_industry_history,
    upsert_report_observation,
)


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

    def test_quarterly_observation_flattens_deterministic_history(self):
        class RecordingSession:
            def __init__(self):
                self.calls = []

            def execute(self, statement, params):
                self.calls.append((statement, params))

        session = RecordingSession()
        upsert_report_observation(
            session,
            {
                "document_id": "report-2",
                "document_type": "quarterly_report",
                "report_date": datetime(2026, 6, 30, tzinfo=UTC),
                "industry": "Semiconductors & Compute",
                "company": "Chip Co",
                "symbol": "CHIP",
                "region": "US",
                "filing_source": "sec_edgar",
            },
            {"metrics": {"revenue": {"value": 120.0, "unit": "USDm"}}},
            {
                "metrics": {
                    "revenue": {"value": 120.0, "unit": "USDm"},
                    "fcf": {"value": 30.0, "unit": "USDm"},
                },
                "fundamentals": {"net_margin_pct": 18.5, "current_ratio": 1.4},
                "valuation": {"pe_ratio": 22.0, "market_price": 55.0},
            },
            model="deterministic-test",
        )

        metrics = json.loads(session.calls[-1][1]["metrics"])
        self.assertEqual(metrics["fcf"]["value"], 30.0)
        self.assertEqual(metrics["fundamental_net_margin_pct"]["value"], 18.5)
        self.assertEqual(metrics["fundamental_current_ratio"]["unit"], "ratio")
        self.assertEqual(metrics["valuation_pe_ratio"]["value"], 22.0)
        self.assertEqual(
            metrics["valuation_market_price"]["unit"], "currency_per_share"
        )
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0][1]["source_id"], "report-2")


if __name__ == "__main__":
    unittest.main()
