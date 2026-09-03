"""Tests for investment service."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from investment_service_support import (
    metric,
    session_context,
)

import investment_service as service


class InvestmentAggregationTests(unittest.TestCase):
    def test_industry_and_region_use_latest_company_breadth(self):
        analyses = [
            {
                "company": "A",
                "symbol": "A",
                "region": "US",
                "industry": "DRAM",
                "state": {"score": 10},
                "drivers": ["AI demand"],
                "risks": [{"inference": "Oversupply"}],
                "report_date": "2025-12-31",
                "metrics": {
                    "revenue": {"change_pct": 10},
                    "fcf_margin": {"value": 20},
                },
                "fundamentals": {
                    "net_margin_pct": 12,
                    "return_on_equity_pct": 18,
                    "debt_to_equity": 0.5,
                },
                "extraction": {"status": "success"},
            },
            {
                "company": "B",
                "symbol": "B",
                "region": "ASIA",
                "industry": "Semiconductors & Compute",
                "state": {"score": 4},
                "drivers": ["Backlog"],
                "risks": [],
                "report_date": "2025-12-31",
                "metrics": {
                    "revenue": {"change_pct": 2},
                    "fcf_margin": {"value": 12},
                },
                "fundamentals": {
                    "net_margin_pct": 8,
                    "return_on_equity_pct": 10,
                    "debt_to_equity": 1.0,
                },
                "extraction": {"status": "unavailable"},
            },
            {
                "company": "A",
                "symbol": "A",
                "region": "US",
                "industry": "DRAM",
                "state": {"score": -5},
                "drivers": [],
                "risks": [],
                "report_date": "2024-12-31",
                "metrics": {
                    "revenue": {"change_pct": 1},
                    "fcf_margin": {"value": 10},
                },
                "fundamentals": {
                    "net_margin_pct": 6,
                    "return_on_equity_pct": 7,
                    "debt_to_equity": 1.2,
                },
                "extraction": {"status": "success"},
            },
        ]
        industry = next(
            item
            for item in service._aggregate_industries(analyses)
            if item["name"] == "Semiconductors & Compute"
        )
        self.assertEqual(industry["company_count"], 2)
        self.assertEqual(industry["score"], 7.0)
        self.assertEqual(industry["stage"], "confirmed")
        self.assertEqual(industry["breadth_pct"], 50.0)
        self.assertEqual(industry["momentum"]["score_delta"], 12.0)
        self.assertEqual(industry["fundamentals"]["revenue_growth_pct"], 6.0)
        self.assertEqual(industry["fundamentals"]["fcf_margin_pct"], 16.0)
        self.assertEqual(industry["deterministic_company_count"], 1)
        self.assertEqual(industry["deterministic_coverage_pct"], 50.0)
        self.assertEqual(
            industry["driver_claims"][0],
            {"label": "AI demand", "company_count": 1, "breadth_pct": 50.0},
        )
        self.assertEqual(
            industry["risk_claims"][0],
            {"label": "Oversupply", "company_count": 1, "breadth_pct": 50.0},
        )
        comparisons = service._peer_comparisons(analyses)
        comparison = comparisons["a"]
        revenue_peer = comparison["metrics"]["revenue_growth_pct"]
        self.assertEqual(revenue_peer["median"], 2.0)
        self.assertEqual(revenue_peer["delta"], 8.0)
        self.assertIsNone(revenue_peer["percentile"])
        self.assertEqual(revenue_peer["sample_count"], 1)
        self.assertEqual(comparison["company_count"], 1)
        self.assertEqual(comparison["members"][0]["symbol"], "B")
        self.assertNotIn("A", [member["symbol"] for member in comparison["members"]])
        self.assertIn("same canonical industry", comparison["members"][0]["reasons"])

        regions = {item["code"]: item for item in service._aggregate_regions(analyses)}
        self.assertEqual(regions["US"]["company_count"], 1)
        self.assertEqual(regions["US"]["score"], 10.0)

    def test_peer_distance_penalizes_sparse_financial_comparability(self):
        subject = {
            "region": "US",
            "metrics": {
                "revenue": {"change_pct": 10},
                "fcf_margin": {"value": 20},
            },
            "fundamentals": {
                "net_margin_pct": 12,
                "return_on_equity_pct": 18,
                "debt_to_equity": 0.5,
                "capex_to_revenue_pct": 8,
            },
        }
        sparse = {
            "region": "US",
            "metrics": {"revenue": {"change_pct": 10}},
            "fundamentals": {},
        }
        comparable = {
            "region": "EU",
            "metrics": {
                "revenue": {"change_pct": 11},
                "fcf_margin": {"value": 18},
            },
            "fundamentals": {
                "net_margin_pct": 11,
                "return_on_equity_pct": 17,
            },
        }

        sparse_distance, sparse_reasons = service._peer_distance(subject, sparse)
        comparable_distance, _ = service._peer_distance(subject, comparable)

        self.assertEqual(sparse_distance, 10.0)
        self.assertLess(comparable_distance, sparse_distance)
        self.assertIn("limited comparable financial metrics (1/7)", sparse_reasons)

    def test_region_coverage_separates_analyzed_and_configured_companies(self):
        analyses = [
            {
                "company": "Unknown US issuer",
                "symbol": "UNKNOWN-US",
                "region": "US",
                "state": {"score": 3},
            },
            {
                "company": "Unknown Asia issuer",
                "symbol": "UNKNOWN-ASIA",
                "region": "ASIA",
                "state": {"score": 9},
            },
            {
                "region": "EU",
                "state": {"score": 8},
            },
        ]

        regions = {item["code"]: item for item in service._aggregate_regions(analyses)}

        self.assertEqual(regions["US"]["company_count"], 1)
        self.assertEqual(regions["US"]["configured_company_count"], 100)
        self.assertEqual(regions["US"]["coverage_status"], "configured")
        self.assertEqual(regions["EU"]["company_count"], 0)
        self.assertEqual(regions["EU"]["configured_company_count"], 200)
        self.assertEqual(regions["EU"]["coverage_status"], "configured")
        self.assertEqual(regions["ASIA"]["company_count"], 1)
        self.assertEqual(regions["ASIA"]["configured_company_count"], 0)
        self.assertEqual(regions["ASIA"]["coverage_status"], "not_configured")
        self.assertIsNone(regions["ASIA"]["score"])
        self.assertIsNone(regions["ASIA"]["stage"])

    def test_valuation_coverage_counts_only_actual_outputs_and_market_values(self):
        calculated = [
            {
                "valuation": {
                    "dcf": {"status": "calculated", "per_share": 100},
                    "pe_ratio": 20,
                    "margin_of_safety": 0.2,
                }
            }
            for _ in range(38)
        ]
        enterprise_only = [
            {
                "valuation": {
                    "dcf": {
                        "status": "enterprise_value_only",
                        "enterprise_value": 1_000,
                    }
                }
            }
            for _ in range(32)
        ]
        unavailable = [
            {"valuation": {"dcf": {"status": "unavailable"}}} for _ in range(148)
        ]

        coverage = service._valuation_coverage(
            calculated + enterprise_only + unavailable
        )

        self.assertEqual(
            coverage,
            {
                "dcf_calculated_count": 38,
                "dcf_enterprise_value_only_count": 32,
                "dcf_unavailable_count": 148,
                "market_price_count": 0,
                "pe_ratio_count": 0,
                "margin_of_safety_count": 0,
            },
        )

    def test_market_relative_coverage_requires_a_positive_market_price(self):
        analyses = [
            {
                "valuation": {
                    "market_price": 80,
                    "pe_ratio": 16,
                    "margin_of_safety": 0.2,
                    "dcf": {"status": "calculated", "per_share": 100},
                }
            },
            {
                "valuation": {
                    "market_price": None,
                    "pe_ratio": 12,
                    "margin_of_safety": 0.3,
                    "dcf": {"status": "unavailable"},
                }
            },
            {
                "valuation": {
                    "market_price": 0,
                    "pe_ratio": 10,
                    "margin_of_safety": 0.4,
                    "dcf": {"status": "calculated", "per_share": 100},
                }
            },
        ]

        coverage = service._valuation_coverage(analyses)

        self.assertEqual(coverage["market_price_count"], 1)
        self.assertEqual(coverage["pe_ratio_count"], 1)
        self.assertEqual(coverage["margin_of_safety_count"], 1)

    def test_public_close_revalues_only_matching_report_currency(self):
        facts = {
            "metrics": {
                "revenue": metric(1_000),
                "operating_cash_flow": metric(120),
                "capex": metric(20),
                "diluted_eps": metric(2, unit="USD/share"),
                "shares_outstanding": metric(100, unit="million shares"),
                "net_debt": metric(50),
            },
            "prior_metrics": {
                "revenue": metric(900, period="FY2024"),
                "operating_cash_flow": metric(100, period="FY2024"),
                "capex": metric(20, period="FY2024"),
            },
        }
        now = service.datetime.now(service.UTC)
        payload = {
            "company": "Example",
            "symbol": "EX",
            "metrics": {},
            "valuation": {"currency_unit": "USDm", "dcf": {"unit": "USDm"}},
            "public_price_timestamp": now,
            "public_price_close": 50,
            "public_price_source": "public_equities",
            "public_price_created_at": now,
            "public_price_metadata": {
                "currency": "USD",
                "provider_symbol": "EX",
                "source_reference": "https://example.test/chart/EX",
                "adjusted": False,
            },
        }

        result = service._attach_public_market_data(payload, facts)

        self.assertEqual(result["valuation"]["market_price"], 50)
        self.assertEqual(result["valuation"]["pe_ratio"], 25)
        self.assertEqual(
            result["valuation"]["market_data"]["comparison_status"], "comparable"
        )
        self.assertEqual(result["metrics"]["market_price"]["source"], "public_equities")
        self.assertIn(
            "unadjusted daily close", result["metrics"]["market_price"]["evidence"]
        )

    def test_public_close_preserves_stored_valuation_inputs(self):
        facts = {
            "metrics": {
                "revenue": metric(1_000),
                "operating_cash_flow": metric(120),
                "capex": metric(20),
                "diluted_eps": metric(2, unit="USD/share"),
            },
            "prior_metrics": {
                "revenue": metric(900, period="FY2024"),
                "operating_cash_flow": metric(100, period="FY2024"),
                "capex": metric(20, period="FY2024"),
            },
        }
        now = service.datetime.now(service.UTC)
        payload = {
            "company": "Example",
            "symbol": "EX",
            "metrics": {
                "shares_outstanding": {
                    "value": 100,
                    "unit": "million shares",
                    "source": "manual_input",
                    "period": "valuation input",
                },
                "net_debt": {
                    "value": 20,
                    "unit": "USDm",
                    "source": "manual_input",
                    "period": "valuation input",
                },
            },
            "valuation": {
                "currency_unit": "USDm",
                "dcf": {
                    "unit": "USDm",
                    "assumptions": {
                        "discount_rate": 0.08,
                        "terminal_growth": 0.02,
                        "shares_outstanding": 100,
                        "net_debt": 20,
                    },
                },
            },
            "public_price_timestamp": now,
            "public_price_close": 50,
            "public_price_source": "public_equities",
            "public_price_created_at": now,
            "public_price_metadata": {"currency": "USD"},
        }

        result = service._attach_public_market_data(payload, facts)
        assumptions = result["valuation"]["dcf"]["assumptions"]

        self.assertEqual(assumptions["discount_rate"], 0.08)
        self.assertEqual(assumptions["terminal_growth"], 0.02)
        self.assertEqual(assumptions["shares_outstanding"], 100)
        self.assertEqual(assumptions["net_debt"], 20)
        self.assertEqual(result["valuation"]["dcf"]["status"], "calculated")

    def test_cross_currency_public_close_is_visible_but_not_used(self):
        now = service.datetime.now(service.UTC)
        payload = {
            "company": "Example",
            "symbol": "EX",
            "metrics": {},
            "valuation": {"currency_unit": "EURm", "dcf": {"unit": "EURm"}},
            "public_price_timestamp": now,
            "public_price_close": 50,
            "public_price_source": "public_equities",
            "public_price_created_at": now,
            "public_price_metadata": {
                "currency": "USD",
                "source_reference": "https://example.test/chart/EX",
            },
        }

        result = service._attach_public_market_data(payload, {"metrics": {}})

        self.assertEqual(
            result["valuation"]["market_data"]["comparison_status"],
            "currency_mismatch",
        )
        self.assertNotIn("market_price", result["metrics"])
        self.assertNotIn("market_price", result["valuation"])

    def test_gbp_pence_quote_normalizes_before_comparison(self):
        now = service.datetime.now(service.UTC)
        payload = {
            "company": "UK Example",
            "symbol": "EX.L",
            "metrics": {},
            "valuation": {"currency_unit": "GBPm", "dcf": {"unit": "GBPm"}},
            "public_price_timestamp": now,
            "public_price_close": 1_250,
            "public_price_source": "public_equities",
            "public_price_created_at": now,
            "public_price_metadata": {"currency": "GBp"},
        }

        result = service._attach_public_market_data(payload, {"metrics": {}})

        self.assertEqual(result["valuation"]["market_price"], 12.5)
        self.assertEqual(result["valuation"]["market_data"]["quote_scale"], 0.01)

    def test_analysis_quality_exposes_freshness_completeness_and_warnings(self):
        now = service.datetime.now(service.UTC)
        current_report = service.date.fromordinal(
            service.date.today().toordinal() - 100
        )
        payload = {
            "company": "Example",
            "symbol": "EX",
            "report_date": current_report.isoformat(),
            "source_url": "https://example.test/filing",
            "analysis_updated_at": now,
            "extraction": {
                "status": "success",
                "deterministic_metric_count": 12,
            },
            "metrics": {"revenue": {"value": 100}},
            "evidence": [{"quote": "Revenue increased."}],
            "drivers": ["revenue growth"],
            "risks": ["competition"],
            "valuation": {
                "dcf": {
                    "status": "calculated",
                    "sensitivity": {"status": "calculated"},
                },
                "market_data": {
                    "status": "current",
                    "comparison_status": "comparable",
                },
            },
            "peer_comparison": {
                "company_count": 1,
                "industry_company_count": 5,
                "members": [{"symbol": "PEER"}],
            },
        }

        quality = service._attach_analysis_quality(payload)["quality"]

        self.assertEqual(quality["status"], "ready")
        self.assertEqual(quality["report"]["status"], "current")
        self.assertEqual(quality["deterministic"]["metric_count"], 12)
        self.assertEqual(quality["narrative"]["evidence_quote_count"], 1)
        self.assertEqual(quality["peers"]["selected_count"], 1)
        self.assertEqual(quality["warnings"], [])

        payload["report_date"] = "2020-12-31"
        stale = service._attach_analysis_quality(payload)["quality"]
        self.assertEqual(stale["status"], "stale")
        self.assertIn("annual_report_stale", stale["warnings"])

        payload["report_date"] = None
        unknown = service._attach_analysis_quality(payload)["quality"]
        self.assertEqual(unknown["report"]["status"], "unknown")
        self.assertEqual(unknown["status"], "partial")
        self.assertIn("report_date_unavailable", unknown["warnings"])

    def test_latest_company_baseline_prefers_verified_facts_over_newer_empty_report(
        self,
    ):
        analyses = [
            {
                "company": "Example",
                "symbol": "EX",
                "report_date": "2026-06-30",
                "metrics": {},
                "extraction": {"status": "unavailable"},
            },
            {
                "company": "Example",
                "symbol": "EX",
                "report_date": "2025-12-31",
                "metrics": {"revenue": {"value": 100}},
                "extraction": {
                    "status": "success",
                    "deterministic_metric_count": 1,
                },
            },
        ]

        result = service._latest_company_analyses(analyses)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["report_date"], "2025-12-31")

    @patch("investment_service.load_classified_news", return_value=[])
    @patch("investment_service.get_session")
    def test_dashboard_includes_the_complete_industry_research_ledger(
        self,
        get_session,
        _load_classified_news,
    ):
        session = MagicMock()
        session.execute.return_value.fetchall.return_value = []
        get_session.return_value = session_context(session)

        result = service.get_dashboard({})

        self.assertEqual(
            {item["name"] for item in result["industries"]},
            set(service.ALL_INDUSTRIES),
        )
        self.assertEqual(result["industry_history"], [])
        self.assertEqual(result["research_summary"]["history_point_count"], 0)
        self.assertEqual(
            result["research_summary"]["valuation_coverage"],
            {
                "dcf_calculated_count": 0,
                "dcf_enterprise_value_only_count": 0,
                "dcf_unavailable_count": 0,
                "market_price_count": 0,
                "pe_ratio_count": 0,
                "margin_of_safety_count": 0,
            },
        )
        self.assertEqual(len(result["industries"]), len(service.ALL_INDUSTRIES))

    @patch("investment_service.load_classified_news", return_value=[])
    @patch("investment_service.get_session")
    def test_dashboard_applies_checked_in_industry_to_legacy_rows(
        self,
        get_session,
        _load_classified_news,
    ):
        """Legacy rows with Unclassified or model-derived industries render
        with the checked-in canonical industry at read time (no DB mutation)."""

        def row(mapping):
            return SimpleNamespace(_mapping=mapping)

        def analysis_row(document, industry, classified_industry, analysis_id):
            return {
                "analysis_id": analysis_id,
                "document_id": document["document_id"],
                "facts": {},
                "analysis": {
                    "classification": {
                        "industry": classified_industry,
                        "confidence": "high",
                    },
                    "summary": "Legacy summary",
                    "thesis": "Legacy thesis",
                    "drivers": [],
                    "catalysts": [],
                    "risks": [],
                    "watch_items": [],
                    "score": 4.0,
                    "state": {"score": 4.0},
                },
                "model": "legacy",
                "tokens_input": 0,
                "tokens_output": 0,
                "cost_usd": 0.0,
                "duration_ms": 0,
                "created_at": None,
                "company": document["company"],
                "symbol": document["symbol"],
                "region": document["region"],
                "industry": industry,
                "document_type": "annual_report",
                "report_date": None,
                "source_url": None,
            }

        def document_row(document_id, company, symbol, region, industry):
            return {
                "document_id": document_id,
                "company": company,
                "symbol": symbol,
                "region": region,
                "industry": industry,
                "document_type": "annual_report",
                "report_date": None,
                "source_url": None,
                "filename": f"{document_id}.txt",
                "status": "analyzed",
                "error_message": None,
                "created_at": None,
            }

        mu = document_row("d-mu", "Micron Technology", "MU", "US", "Unclassified")
        stm = document_row(
            "d-stm",
            "STMicroelectronics",
            "STM",
            "EU",
            "Software, Cloud & Communications",
        )
        gs = document_row("d-gs", "Goldman Sachs", "GS", "US", "Unclassified")
        ex = document_row("d-ex", "Example PLC", "EX", "EU", "Unclassified")
        document_rows = [mu, stm, gs, ex]
        company_rows = [
            {key: item[key] for key in ("company", "symbol", "industry", "region")}
            for item in document_rows
        ]
        analysis_rows = [
            analysis_row(mu, "Unclassified", "Consumer", "a-mu"),
            analysis_row(
                stm,
                "Software, Cloud & Communications",
                "Software, Cloud & Communications",
                "a-stm",
            ),
            analysis_row(gs, "Unclassified", "Unclassified", "a-gs"),
            analysis_row(ex, "Unclassified", "Unclassified", "a-ex"),
        ]

        documents_result = MagicMock()
        documents_result.fetchall.return_value = [row(item) for item in document_rows]
        companies_result = MagicMock()
        companies_result.fetchall.return_value = [row(item) for item in company_rows]
        analyses_result = MagicMock()
        analyses_result.fetchall.return_value = [row(item) for item in analysis_rows]
        observations_result = MagicMock()
        observations_result.fetchall.return_value = []
        session = MagicMock()
        session.execute.side_effect = [
            documents_result,
            companies_result,
            analyses_result,
            observations_result,
        ]
        get_session.return_value = session_context(session)

        result = service.get_dashboard({})

        documents_by_symbol = {item["symbol"]: item for item in result["documents"]}
        self.assertEqual(
            documents_by_symbol["MU"]["industry"], "Semiconductors & Compute"
        )
        self.assertEqual(
            documents_by_symbol["STM"]["industry"], "Semiconductors & Compute"
        )
        self.assertEqual(
            documents_by_symbol["GS"]["industry"], "Financials & Real Estate"
        )
        self.assertEqual(documents_by_symbol["EX"]["industry"], "Unclassified")

        analyses_by_symbol = {item["symbol"]: item for item in result["analyses"]}
        self.assertEqual(
            analyses_by_symbol["MU"]["industry"], "Semiconductors & Compute"
        )
        self.assertEqual(
            analyses_by_symbol["MU"]["classification"]["industry"],
            "Semiconductors & Compute",
        )
        self.assertEqual(
            analyses_by_symbol["STM"]["classification"]["industry"],
            "Semiconductors & Compute",
        )
        self.assertEqual(
            analyses_by_symbol["GS"]["classification"]["industry"],
            "Financials & Real Estate",
        )
        self.assertEqual(
            analyses_by_symbol["EX"]["classification"]["industry"], "Unclassified"
        )

        industries = {item["name"]: item for item in result["industries"]}
        self.assertEqual(industries["Semiconductors & Compute"]["company_count"], 2)
        self.assertEqual(industries["Financials & Real Estate"]["company_count"], 1)
        self.assertEqual(industries["Unclassified"]["company_count"], 1)


if __name__ == '__main__':
    unittest.main()
