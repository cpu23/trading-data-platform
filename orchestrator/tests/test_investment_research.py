import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from investment_engine import build_deterministic_analysis
from investment_facts import extract_sec_facts
from investment_news import ALL_INDUSTRIES, canonicalize_industry, classify_news_item
from investment_service import _news_monitoring, _trend_series

ACCESSION = "0000789019-26-000042"


def fact(concept, unit, current, prior, *, duration=True):
    def entry(end, value, start):
        result = {
            "accn": ACCESSION,
            "end": end,
            "filed": "2026-07-29",
            "form": "10-K",
            "val": value,
        }
        if duration:
            result["start"] = start
        return result

    return {
        concept: {
            "units": {
                unit: [
                    entry("2026-06-30", current, "2025-07-01"),
                    entry("2025-06-30", prior, "2024-07-01"),
                    {**entry("2026-06-30", 999, "2025-07-01"), "accn": "other"},
                ]
            }
        }
    }


class DeterministicFactTests(unittest.TestCase):
    def test_sec_facts_are_accession_scoped_normalized_and_derived(self):
        us_gaap = {}
        us_gaap.update(
            fact(
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "USD",
                240_000_000,
                200_000_000,
            )
        )
        us_gaap.update(fact("GrossProfit", "USD", 120_000_000, 90_000_000))
        us_gaap.update(
            fact(
                "NetCashProvidedByUsedInOperatingActivities",
                "USD",
                70_000_000,
                55_000_000,
            )
        )
        us_gaap.update(
            fact(
                "PaymentsToAcquirePropertyPlantAndEquipment",
                "USD",
                20_000_000,
                15_000_000,
            )
        )
        us_gaap.update(fact("NetIncomeLoss", "USD", 48_000_000, 40_000_000))
        us_gaap.update(
            fact("LongTermDebt", "USD", 80_000_000, 90_000_000, duration=False)
        )
        us_gaap.update(
            fact(
                "CashAndCashEquivalentsAtCarryingValue",
                "USD",
                30_000_000,
                25_000_000,
                duration=False,
            )
        )
        us_gaap.update(fact("Assets", "USD", 400_000_000, 360_000_000, duration=False))
        us_gaap.update(
            fact("StockholdersEquity", "USD", 160_000_000, 140_000_000, duration=False)
        )
        us_gaap.update(
            fact("AssetsCurrent", "USD", 120_000_000, 100_000_000, duration=False)
        )
        us_gaap.update(
            fact("LiabilitiesCurrent", "USD", 60_000_000, 50_000_000, duration=False)
        )
        current, prior, metadata = extract_sec_facts(
            {"filing_id": ACCESSION},
            {"facts": {"us-gaap": us_gaap}},
        )

        self.assertEqual(current["revenue"]["value"], 240.0)
        self.assertEqual(current["revenue"]["unit"], "USDm")
        self.assertEqual(prior["revenue"]["value"], 200.0)
        self.assertEqual(current["gross_margin"]["value"], 50.0)
        self.assertEqual(current["net_debt"]["value"], 50.0)
        self.assertEqual(current["revenue"]["source"], "sec_xbrl")
        self.assertEqual(metadata["status"], "success")

    def test_fundamental_ratios_use_only_finite_report_values(self):
        metrics = {
            "revenue": {"value": 240},
            "operating_cash_flow": {"value": 70},
            "capex": {"value": 20},
            "net_income": {"value": 48},
            "total_assets": {"value": 400},
            "equity": {"value": 160},
            "total_debt": {"value": 80},
            "current_assets": {"value": 120},
            "current_liabilities": {"value": 60},
        }
        result = build_deterministic_analysis({"metrics": metrics})
        self.assertEqual(result["fundamentals"]["net_margin_pct"], 20.0)
        self.assertEqual(result["fundamentals"]["return_on_equity_pct"], 30.0)
        self.assertEqual(result["fundamentals"]["debt_to_equity"], 0.5)
        self.assertEqual(result["fundamentals"]["current_ratio"], 2.0)


class InvestmentNewsTests(unittest.TestCase):
    def test_industry_taxonomy_is_strict_and_covers_legacy_labels(self):
        cases = {
            "Semiconductors & Memory": "Semiconductors & Compute",
            "Information Technology": "Software, Cloud & Communications",
            "Crude Petroleum & Natural Gas": "Energy & Utilities",
            "Copper Mining": "Industrials & Materials",
            "Payment Networks": "Financials & Real Estate",
            "Health Care": "Healthcare",
            "Beverages": "Consumer",
            "Aircraft engines": "Aerospace & Defence",
            "unsupported specialist label": "Unclassified",
        }
        self.assertEqual(len(ALL_INDUSTRIES), 9)
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(canonicalize_industry(raw), expected)
                self.assertIn(expected, ALL_INDUSTRIES)

    def test_news_classification_links_company_industry_and_macro_theme(self):
        item = {
            "id": "r1",
            "source": "reuters",
            "source_label": "Reuters",
            "title": "NXP expands semiconductor capacity as supply chain pressure persists",
            "summary": "The investment plan raises capital spending.",
            "url": "https://example.test/story",
            "published": "2026-08-01T08:00:00Z",
            "symbols": ["NXPI"],
            "tags": [],
        }
        result = classify_news_item(
            item,
            [
                {
                    "company": "NXP Semiconductors",
                    "symbol": "NXPI",
                    "industry": "Semiconductors & Memory",
                }
            ],
        )
        self.assertIn("NXP Semiconductors", result["companies"])
        self.assertIn("Semiconductors & Compute", result["industries"])
        self.assertIn("capital_spending", result["themes"])
        self.assertIn("supply_chain", result["themes"])
        self.assertTrue(result["macro_relevant"])
        self.assertEqual(
            result["classification_method"], "deterministic_keywords_entities"
        )

    def test_plain_words_do_not_match_ticker_symbols_or_defence_industry(self):
        item = {
            "title": "Indian Oil boosts spot buying as defence minister returns",
            "summary": "",
            "symbols": [],
            "tags": [],
        }
        result = classify_news_item(
            item,
            [
                {
                    "company": "Spotify",
                    "symbol": "SPOT",
                    "industry": "Communication Services",
                },
                {
                    "company": "Lowe's",
                    "symbol": "LOW",
                    "industry": "Consumer, Retail & E-commerce",
                },
            ],
        )
        self.assertNotIn("Spotify", result["companies"])
        self.assertNotIn("Lowe's", result["companies"])
        self.assertNotIn("Aerospace & Defence", result["industries"])
        self.assertIn("Energy & Utilities", result["industries"])

    def test_report_and_news_trends_are_aggregated_without_model_output(self):
        analyses = [
            {
                "company": "A",
                "symbol": "A",
                "industry": "Semiconductors",
                "report_date": "2025-12-31",
                "state": {"score": 4},
                "metrics": {"revenue": {"change_pct": 10}, "fcf_margin": {"value": 20}},
            },
            {
                "company": "A",
                "symbol": "A",
                "industry": "Semiconductors",
                "report_date": "2024-12-31",
                "state": {"score": 1},
                "metrics": {"revenue": {"change_pct": 2}, "fcf_margin": {"value": 12}},
            },
        ]
        series = _trend_series(analyses)
        self.assertEqual(series[0]["industry"], "Semiconductors & Compute")
        self.assertEqual([point["score"] for point in series[0]["points"]], [1.0, 4.0])
        self.assertIsInstance(_news_monitoring([]), list)


if __name__ == "__main__":
    unittest.main()
