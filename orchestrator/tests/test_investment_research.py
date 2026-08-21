import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from investment_engine import build_deterministic_analysis
from investment_facts import extract_sec_facts
from investment_news import ALL_INDUSTRIES, canonicalize_industry, classify_news_item
from investment_service import _news_monitoring, _trend_series
from investment_universe import industry_for, top_us_uk_eu_companies

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

    def test_checked_in_issuer_industry_lookup_prefers_symbol_then_company(self):
        self.assertEqual(industry_for("MU"), "Semiconductors & Compute")
        self.assertEqual(industry_for("mu"), "Semiconductors & Compute")
        # Symbol wins even when the company identity disagrees.
        self.assertEqual(industry_for("MU", "Not Micron"), "Semiconductors & Compute")
        self.assertEqual(
            industry_for(None, "Micron Technology"), "Semiconductors & Compute"
        )
        self.assertEqual(industry_for("STM"), "Semiconductors & Compute")
        self.assertEqual(industry_for("GS"), "Financials & Real Estate")
        self.assertEqual(industry_for("CVX"), "Energy & Utilities")
        self.assertEqual(industry_for("SPCX"), "Aerospace & Defence")
        self.assertEqual(industry_for("ARGX"), "Healthcare")
        self.assertEqual(industry_for("UNH"), "Healthcare")
        self.assertEqual(industry_for("TSLA"), "Consumer")
        self.assertEqual(industry_for("UBER"), "Consumer")
        self.assertEqual(industry_for("MCD"), "Consumer")
        # Amazon has one checked-in mapping, applied consistently by either key.
        self.assertEqual(industry_for("AMZN"), "Software, Cloud & Communications")
        self.assertEqual(industry_for("AMZN"), industry_for(None, "Amazon"))
        self.assertEqual(industry_for(None, "McDonald's"), "Consumer")
        # Truly unknown issuers fail closed.
        self.assertEqual(industry_for("ZZZZ"), "Unclassified")
        self.assertEqual(industry_for(None, "Not A Configured Issuer"), "Unclassified")

    def test_every_configured_issuer_resolves_to_a_concrete_industry(self):
        concrete = set(ALL_INDUSTRIES) - {"Unclassified"}
        universe = top_us_uk_eu_companies()
        self.assertEqual(len(universe), 300)
        unresolved = [
            (company.get("symbol"), company.get("company"), industry)
            for company in universe
            if (industry := industry_for(company.get("symbol"), company.get("company")))
            not in concrete
        ]
        self.assertEqual(unresolved, [])

    def test_configured_issuer_industry_samples_are_canonical(self):
        cases = {
            "SYK": "Healthcare",
            "MU": "Semiconductors & Compute",
            "STM": "Semiconductors & Compute",
            "ARM": "Semiconductors & Compute",
            "ASML": "Semiconductors & Compute",
            "NXPI": "Semiconductors & Compute",
            "IFNNY": "Semiconductors & Compute",
            "ASMIY": "Semiconductors & Compute",
            "SAP": "Software, Cloud & Communications",
            "NOK": "Software, Cloud & Communications",
            "NBIS": "Software, Cloud & Communications",
            "GS": "Financials & Real Estate",
            "CVX": "Energy & Utilities",
            "SPCX": "Aerospace & Defence",
            "ARGX": "Healthcare",
            "UNH": "Healthcare",
            "AMZN": "Software, Cloud & Communications",
            "BA.L": "Aerospace & Defence",
            "SHEL": "Energy & Utilities",
            "HSBC": "Financials & Real Estate",
            "NVO": "Healthcare",
        }
        for symbol, expected in cases.items():
            with self.subTest(symbol=symbol):
                self.assertEqual(industry_for(symbol), expected)

    def test_news_matches_checked_in_name_variants_for_configured_issuers(self):
        item = {
            "id": "alphabet1",
            "source": "reuters",
            "title": "Alphabet and Berkshire report results as Google parent shines",
            "summary": "",
            "symbols": [],
            "tags": [],
        }
        result = classify_news_item(item, top_us_uk_eu_companies())
        self.assertIn("Alphabet (Google)", result["companies"])
        self.assertIn("Berkshire Hathaway", result["companies"])
        self.assertIn("Software, Cloud & Communications", result["industries"])
        self.assertIn("Financials & Real Estate", result["industries"])

        item = {
            "id": "goldman1",
            "source": "reuters",
            "title": "Goldman lifts NVIDIA price target on AI demand",
            "summary": "",
            "symbols": [],
            "tags": [],
        }
        result = classify_news_item(item, top_us_uk_eu_companies())
        self.assertIn("Goldman Sachs", result["companies"])
        self.assertIn("NVIDIA", result["companies"])
        self.assertEqual(
            set(result["industries"]),
            {"Financials & Real Estate", "Semiconductors & Compute"},
        )
        self.assertEqual(len(result["companies"]), 2)
        self.assertNotIn("Unclassified", result["industries"])

    def test_news_uses_checked_in_issuer_industry_over_keyword_fallback(self):
        item = {
            "id": "mu1",
            "source": "reuters",
            "source_label": "Reuters",
            "title": "Micron lifts capex on AI data-centre demand",
            "summary": "The chipmaker guides capital spending higher.",
            "url": "https://example.test/mu",
            "published": "2026-08-01T08:00:00Z",
            "symbols": ["MU"],
            "tags": [],
        }
        result = classify_news_item(item, top_us_uk_eu_companies())
        self.assertEqual(result["companies"], ["Micron Technology"])
        self.assertEqual(result["industries"], ["Semiconductors & Compute"])
        # Keyword noise ("data centre") must not add a competing industry to
        # company-linked news, and Unclassified is never appended.
        self.assertNotIn("Software, Cloud & Communications", result["industries"])
        self.assertNotIn("Unclassified", result["industries"])
        # Themes still classify independently of the issuer industry.
        self.assertIn("capital_spending", result["themes"])
        self.assertTrue(result["macro_relevant"])

    def test_news_industry_ignores_stale_record_label_for_configured_issuer(self):
        item = {
            "id": "stm1",
            "source": "reuters",
            "title": "STMicroelectronics wins automotive chip orders",
            "summary": "The foundry expands manufacturing capacity.",
            "symbols": ["STM"],
            "tags": [],
        }
        result = classify_news_item(
            item,
            [
                {
                    "company": "STMicroelectronics",
                    "symbol": "STM",
                    "industry": "Information Technology",
                }
            ],
        )
        # The checked-in mapping wins over the legacy record label.
        self.assertEqual(result["industries"], ["Semiconductors & Compute"])

    def test_news_never_appends_unclassified_for_known_issuer(self):
        item = {
            "id": "cvx1",
            "source": "reuters",
            "title": "Chevron hikes dividend as buyback accelerates",
            "summary": "Shareholders receive a higher quarterly payout.",
            "symbols": ["CVX"],
            "tags": [],
        }
        # The company record carries no industry label at all.
        result = classify_news_item(item, [{"company": "Chevron", "symbol": "CVX"}])
        self.assertEqual(result["industries"], ["Energy & Utilities"])
        self.assertNotIn("Unclassified", result["industries"])

    def test_unknown_issuer_news_falls_back_to_keywords_and_fails_closed(self):
        item = {
            "id": "copper1",
            "source": "reuters",
            "title": "Copper mining output rises on new capacity",
            "summary": "",
            "symbols": [],
            "tags": [],
        }
        # No known issuer: keyword fallback still applies.
        result = classify_news_item(item, top_us_uk_eu_companies())
        self.assertEqual(result["industries"], ["Industrials & Materials"])

        # No known issuer and no keyword: the item stays unclassified and no
        # Unclassified label is appended to the industry list.
        item = {
            "id": "quiet1",
            "source": "reuters",
            "title": "Markets steady as traders await data",
            "summary": "",
            "symbols": [],
            "tags": [],
        }
        result = classify_news_item(item, top_us_uk_eu_companies())
        self.assertEqual(result["industries"], [])
        self.assertNotIn("Unclassified", result["industries"])
        self.assertEqual(result["ambiguity"], "unclassified")

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
