import io
import json
import sys
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import investment_service as service


@contextmanager
def session_context(session):
    yield session


def metric(value, unit="USDm", period="FY2025", evidence="report evidence"):
    return {"value": value, "unit": unit, "period": period, "evidence": evidence}


class InvestmentIntakeTests(unittest.TestCase):
    def test_html_extraction_removes_active_content(self):
        raw = (
            b"<html><script>steal()</script><body><h1>Annual report</h1><p>"
            + b"Revenue and cash flow evidence. " * 8
            + b"</p></body></html>"
        )
        extracted = service.extract_document_text(raw, "report.html", "text/html")
        self.assertIn("Annual report", extracted)
        self.assertNotIn("steal", extracted)

    def test_archive_magic_overrides_incorrect_pdf_metadata(self):
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w") as package:
            package.writestr(
                "annual-report.xhtml",
                "<html><body><h1>Annual report</h1><p>"
                + "Revenue and cash flow evidence. " * 8
                + "</p></body></html>",
            )

        extracted = service.extract_document_text(
            content.getvalue(),
            "incorrect.pdf",
            "application/pdf",
        )

        self.assertIn("Annual report", extracted)

    def test_oversized_text_preserves_financial_statements_and_document_end(self):
        statement = (
            "\nCONSOLIDATED INCOME STATEMENT\nUSD million 2025 2024\nRevenue 120 100\n"
        )
        raw = ("BEGIN\n" + "x" * 600_000 + statement + "y" * 600_000 + "\nEND").encode()

        extracted = service.extract_document_text(
            raw, "annual-report.txt", "text/plain"
        )

        self.assertLessEqual(len(extracted), service.MAX_EXTRACTED_CHARS)
        self.assertIn("BEGIN", extracted)
        self.assertIn("CONSOLIDATED INCOME STATEMENT", extracted)
        self.assertIn("END", extracted)
        excerpt = service.build_analysis_excerpt(extracted)
        self.assertLessEqual(len(excerpt), service.MAX_ANALYSIS_CHARS)
        self.assertIn("CONSOLIDATED INCOME STATEMENT", excerpt)
        self.assertIn("END", excerpt)

    def test_metadata_canonicalizes_regions_and_key_industries(self):
        result = service.normalize_metadata(
            {
                "company": "Memory Co",
                "symbol": "mem",
                "region": "asia",
                "industry": "DRAM chip manufacturing",
                "document_type": "annual_report",
                "report_date": "2025-12-31",
                "filename": "../report.txt",
            }
        )
        self.assertEqual(result["region"], "ASIA")
        self.assertEqual(result["symbol"], "MEM")
        self.assertEqual(result["industry"], "Semiconductors & Compute")
        self.assertEqual(result["filename"], "report.txt")

    def test_private_report_url_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "private or reserved"):
            service._validate_public_url("http://127.0.0.1/internal-report.pdf")

    def test_large_report_excerpt_keeps_financial_and_demand_windows(self):
        report = (
            "A" * 150_000
            + " Revenue increased on AI data-centre demand and capex. "
            + "Z" * 150_000
        )
        excerpt = service.build_analysis_excerpt(report)
        self.assertLessEqual(len(excerpt), service.MAX_ANALYSIS_CHARS + 500)
        self.assertIn("AI data-centre demand", excerpt)

    @patch("investment_service.extract_document_text")
    @patch("investment_service.get_session")
    def test_preserves_unextractable_regulatory_content(
        self,
        get_session,
        extract_document_text,
    ):
        extract_document_text.side_effect = ValueError(
            "document did not contain enough extractable text"
        )
        row = MagicMock()
        row._mapping = {
            "document_id": "doc-1",
            "company": "Example PLC",
            "symbol": "EX",
            "region": "EU",
            "industry": "Unclassified",
            "document_type": "annual_report",
            "report_date": None,
            "source_url": "https://example.test/document",
            "filing_source": "companies_house",
            "filing_id": "transaction-1",
            "filename": "transaction-1.pdf",
            "mime_type": "application/pdf",
            "status": "ingested",
            "created_at": None,
        }
        session = MagicMock()
        session.execute.return_value.fetchone.return_value = row
        get_session.return_value = session_context(session)
        content = b"%PDF-scanned-document"

        result = service.store_document(
            {},
            {
                "company": "Example PLC",
                "symbol": "EX",
                "region": "EU",
                "industry": "Unclassified",
                "document_type": "annual_report",
                "filing_source": "companies_house",
                "filing_id": "transaction-1",
                "filename": "transaction-1.pdf",
            },
            content,
            "application/pdf",
            preserve_content=True,
            allow_unextractable=True,
        )

        params = session.execute.call_args.args[1]
        self.assertEqual(result["document_id"], "doc-1")
        self.assertEqual(params["raw_content"], content)
        self.assertEqual(params["extracted_text"], "")


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
                "risks": [{"risk": "Oversupply"}],
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
        revenue_peer = comparisons["a"]["metrics"]["revenue_growth_pct"]
        self.assertEqual(revenue_peer["median"], 6.0)
        self.assertEqual(revenue_peer["delta"], 4.0)
        self.assertEqual(revenue_peer["percentile"], 75.0)
        self.assertEqual(revenue_peer["sample_count"], 2)
        regions = {item["code"]: item for item in service._aggregate_regions(analyses)}
        self.assertEqual(regions["US"]["company_count"], 1)
        self.assertEqual(regions["US"]["score"], 10.0)

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
        self.assertEqual(len(result["industries"]), len(service.ALL_INDUSTRIES))


class InvestmentAnalysisServiceTests(unittest.TestCase):
    def test_analysis_uses_strict_luna_schema_and_records_cost_duration(self):
        document_id = "11111111-1111-1111-1111-111111111111"
        analysis_id = "22222222-2222-2222-2222-222222222222"
        document = {
            "document_id": document_id,
            "company": "Memory Co",
            "symbol": "MEM",
            "region": "ASIA",
            "industry": "DRAM",
            "document_type": "annual_report",
            "report_date": None,
            "source_url": "https://example.com/report",
            "filename": "report.txt",
            "extracted_text": "Annual report evidence. " * 20,
        }
        facts = {
            "classification": {
                "document_type": "annual_report",
                "sector": "Technology",
                "industry": "Memory semiconductors",
                "region": "Asia",
                "confidence": "high",
            },
            "qualitative": {
                "ai_demand": {
                    "present": True,
                    "strength": "strong",
                    "evidence": "AI demand",
                },
                "datacenter_demand": {
                    "present": True,
                    "strength": "strong",
                    "evidence": "data-centre",
                },
                "supply_constraints": {
                    "present": True,
                    "strength": "moderate",
                    "evidence": "tight supply",
                },
                "pricing_power": {
                    "present": True,
                    "strength": "strong",
                    "evidence": "higher pricing",
                },
                "guidance_up": {
                    "present": True,
                    "strength": "strong",
                    "evidence": "raised",
                },
                "guidance_down": {"present": False, "strength": "none", "evidence": ""},
            },
            "summary": "Demand, capex and pricing are accelerating.",
            "thesis": "The cycle is strengthening; falling demand would invalidate it.",
            "drivers": ["AI demand"],
            "catalysts": [
                {
                    "catalyst": "Capacity ramp",
                    "horizon": "12 months",
                    "evidence": "capex",
                }
            ],
            "risks": [
                {
                    "risk": "Oversupply",
                    "likelihood": "medium",
                    "impact": "high",
                    "mitigation": "Monitor inventory",
                    "evidence": "capacity",
                }
            ],
            "watch_items": ["Inventory growth"],
        }

        claim_session = MagicMock()
        claim_session.execute.return_value.fetchone.return_value = (document_id,)
        persist_session = MagicMock()
        insert_result = MagicMock()
        insert_result.fetchone.return_value = (analysis_id,)
        persist_session.execute.side_effect = [
            insert_result,
            MagicMock(),
            MagicMock(),
            MagicMock(),
        ]

        stage = MagicMock()
        stage.policy = SimpleNamespace(
            model="openai/gpt-5.6-luna",
            validation_retries=1,
        )
        stage.telemetry = SimpleNamespace(
            tokens_input_total=1200,
            tokens_output_total=500,
            cost_usd_total=0.002,
            first_attempt_duration_ms=200,
            validation_retry_duration_ms=None,
            validation_warnings=[],
        )
        stage.call.return_value = {"content": json.dumps(facts)}

        with (
            patch.object(service, "_load_document", return_value=document),
            patch.object(service, "enforce_budget"),
            patch.object(
                service,
                "get_session",
                side_effect=[
                    session_context(claim_session),
                    session_context(persist_session),
                ],
            ),
            patch.object(
                service,
                "_load_news_context",
                return_value=[{"source": "Reuters", "title": "AI demand"}],
            ),
            patch.object(service, "_previous_analysis", return_value=(None, 0)),
            patch.object(service, "LLMStage", return_value=stage) as stage_class,
            patch.object(
                service, "get_analysis", return_value={"analysis_id": analysis_id}
            ) as get_analysis,
        ):
            result = service.analyze_document({}, document_id)

        self.assertEqual(result["analysis_id"], analysis_id)
        self.assertEqual(stage_class.call_args.args[1], "investment_analysis")
        schema = stage_class.call_args.kwargs["response_schema"]
        self.assertEqual(schema["name"], "investment_report_narrative")
        self.assertNotIn("metrics", schema["schema"]["properties"])
        self.assertNotIn("prior_metrics", schema["schema"]["properties"])
        prompt = stage.call.call_args.args[0]
        self.assertIn("Reuters", prompt)
        self.assertIn("FILING EXCERPT", prompt)
        statements = [
            str(call.args[0]) for call in persist_session.execute.call_args_list
        ]
        self.assertTrue(
            any("INSERT INTO processing_log" in statement for statement in statements)
        )
        processing_params = persist_session.execute.call_args_list[-1].args[1]
        self.assertEqual(processing_params["cost_usd"], 0.002)
        self.assertEqual(processing_params["model_used"], "openai/gpt-5.6-luna")
        get_analysis.assert_called_once_with({}, analysis_id)


if __name__ == "__main__":
    unittest.main()
