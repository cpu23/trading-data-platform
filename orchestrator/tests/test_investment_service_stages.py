"""Tests for investment service observable pipeline stages and behavior."""

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import investment_service as service
from investment_service_support import (
    investment_report_payload,
    metric,
    session_context,
)
from llm_client import LLMValidationError


class InvestmentStagePipelineTests(unittest.TestCase):
    """Observable pipeline behavior: request construction, pure finalization,
    evidence grounding, retry loop execution, and error handling."""

    def _payload(self):
        return investment_report_payload()

    def _mock_document(self, document_id="11111111-1111-1111-1111-111111111111"):
        return {
            "document_id": document_id,
            "company": "Memory Co",
            "symbol": "MEM",
            "region": "ASIA",
            "industry": "DRAM",
            "document_type": "annual_report",
            "report_date": None,
            "source_url": "https://example.com/report",
            "filename": "report.txt",
            "extracted_text": (
                "Annual report evidence: AI demand accelerated, data-centre "
                "deployments expanded despite tight supply, higher pricing held, "
                "guidance was raised, capex grew, and capacity additions continue. "
                "Demand remained durable."
            ),
        }

    def test_request_fingerprint_is_deterministic_and_excerpt_sensitive(self):
        excerpt = "Data centre revenue accelerated. Demand remained durable."
        first = service.build_investment_analysis_request(
            {"company": "Example Company", "document_id": "doc-1"},
            excerpt,
            [],
            {},
            {},
        )
        second = service.build_investment_analysis_request(
            {"document_id": "doc-1", "company": "Example Company"},
            excerpt,
            [],
            {},
            {},
        )
        self.assertEqual(first.schema_name, "investment_report_narrative_v7")
        self.assertTrue(first.strict)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertIn(excerpt, first.prompt)
        reworded = service.build_investment_analysis_request(
            {"company": "Example Company", "document_id": "doc-1"},
            excerpt.replace("durable", "soft"),
            [],
            {},
            {},
        )
        self.assertNotEqual(reworded.fingerprint, first.fingerprint)

    def test_finalize_investment_analysis_is_pure_over_all_inputs(self):
        facts = self._payload()
        facts["metrics"] = {"revenue": metric(1_000)}
        facts["prior_metrics"] = {"revenue": metric(900, period="FY2024")}
        document = {
            "company": "Test Co",
            "symbol": "TST",
            "region": "US",
            "industry": "Software",
            "document_type": "annual_report",
        }
        finalized = service.finalize_investment_analysis(
            facts,
            document=document,
            deterministic_current={},
            deterministic_prior={},
            market_inputs={},
            stored_previous_facts={},
            previous_state=None,
            prior_count=0,
            news_items=[],
            extraction={},
            relationship_facts={},
            material_relationships=(),
        )
        self.assertIsInstance(finalized, service.InvestmentFinalizedAnalysis)
        self.assertEqual(finalized.classified_industry, "Semiconductors & Compute")
        self.assertIn("summary", finalized.analysis)
        self.assertIn("thesis", finalized.analysis)
        self.assertIn("counter_thesis", finalized.analysis)

    def test_exact_typography_quotes_ground_but_edits_do_not(self):
        filing_text = (
            "Management\u2019s \u201cdemand remained durable\u201d \u2014 "
            "backlog\u00a0held."
        )
        source = self._payload()
        source["qualitative"]["ai_demand"]["present"] = True
        source["qualitative"]["ai_demand"]["evidence"] = (
            'Management\'s "demand remained durable" - backlog held.'
        )
        self.assertEqual(
            service.investment_evidence_violations(
                source, excerpt=filing_text, news_items=[]
            ),
            [],
        )

    def test_evidence_requires_filing_text_not_news(self):
        excerpt = "Demand remained durable through the quarter."
        self.assertEqual(
            service.investment_evidence_violations(
                self._payload(), excerpt=excerpt, news_items=[]
            ),
            [],
        )
        invented = self._payload()
        invented["qualitative"]["ai_demand"]["present"] = True
        invented["qualitative"]["ai_demand"]["evidence"] = "Margins expanded sharply"
        invented_violations = service.investment_evidence_violations(
            invented, excerpt=excerpt, news_items=[]
        )
        self.assertEqual(len(invented_violations), 1)
        self.assertIn("not grounded", invented_violations[0])

    def test_blank_evidence_allowed_only_for_absent_qualitative_signals(self):
        all_absent = self._payload()
        for name in service.QUALITATIVE_NAMES:
            all_absent["qualitative"][name] = {
                "present": False,
                "strength": "none",
                "evidence": "",
            }
        self.assertEqual(
            service.investment_evidence_violations(
                all_absent, excerpt="No narrative available.", news_items=[]
            ),
            [],
        )
        present_blank = self._payload()
        present_blank["qualitative"]["ai_demand"]["present"] = True
        present_blank["qualitative"]["ai_demand"]["evidence"] = "   "
        blank_violations = service.investment_evidence_violations(
            present_blank, excerpt="Demand remained durable.", news_items=[]
        )
        self.assertEqual(len(blank_violations), 1)

    def test_evidence_rejects_scaffold_headers_and_cross_region_quotes(self):
        excerpt = (
            "[Source characters 0-40]\nDemand remained durable in the quarter.\n"
            "[Source characters 41-90]\nBacklog eased while pricing held firm."
        )
        header_quote = self._payload()
        header_quote["qualitative"]["ai_demand"]["present"] = True
        header_quote["qualitative"]["ai_demand"]["evidence"] = (
            "[Source characters 0-40]"
        )
        self.assertEqual(
            len(
                service.investment_evidence_violations(
                    header_quote, excerpt=excerpt, news_items=[]
                )
            ),
            1,
        )

    def test_retry_correction_loop_retries_and_succeeds_on_grounded_response(self):
        document_id = "11111111-1111-1111-1111-111111111111"
        analysis_id = "22222222-2222-2222-2222-222222222222"
        document = self._mock_document(document_id)

        ungrounded = self._payload()
        ungrounded["qualitative"]["pricing_power"]["present"] = True
        ungrounded["qualitative"]["pricing_power"]["strength"] = "strong"
        ungrounded["qualitative"]["pricing_power"]["evidence"] = (
            "Margins expanded sharply"
        )

        grounded = self._payload()
        grounded["qualitative"]["pricing_power"]["present"] = True
        grounded["qualitative"]["pricing_power"]["strength"] = "strong"
        grounded["qualitative"]["pricing_power"]["evidence"] = "higher pricing held"

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
            MagicMock(),
        ]
        stage = MagicMock()
        stage.policy = SimpleNamespace(
            model="openai/gpt-5.6-luna", validation_retries=1
        )
        stage.telemetry = SimpleNamespace(
            tokens_input_total=1200,
            tokens_output_total=500,
            cost_usd_total=0.002,
            first_attempt_duration_ms=200,
            validation_retry_duration_ms=None,
            validation_warnings=[],
        )
        stage.call.side_effect = [
            {"content": json.dumps(ungrounded)},
            {"content": json.dumps(grounded)},
        ]
        with (
            patch.object(service, "_load_document", return_value=document),
            patch.object(
                service,
                "get_session",
                side_effect=[
                    session_context(claim_session),
                    session_context(persist_session),
                ],
            ),
            patch.object(service, "_load_news_context", return_value=[]),
            patch.object(service, "_previous_analysis", return_value=(None, 0)),
            patch.object(service, "LLMStage", return_value=stage),
            patch.object(
                service, "get_analysis", return_value={"analysis_id": analysis_id}
            ),
        ):
            service.analyze_document({}, document_id)

        self.assertEqual(stage.call.call_count, 2)
        repair_prompt = stage.call.call_args_list[1].args[0]
        base_prompt = stage.call.call_args_list[0].args[0]
        self.assertTrue(repair_prompt.startswith(base_prompt))

    def test_retry_exhaustion_raises_llm_validation_error(self):
        document_id = "11111111-1111-1111-1111-111111111111"
        document = self._mock_document(document_id)

        invalid = self._payload()
        invalid["qualitative"]["pricing_power"]["present"] = True
        invalid["qualitative"]["pricing_power"]["strength"] = "strong"
        invalid["qualitative"]["pricing_power"]["evidence"] = "Ungrounded quote"

        claim_session = MagicMock()
        claim_session.execute.return_value.fetchone.return_value = (document_id,)
        stage = MagicMock()
        stage.policy = SimpleNamespace(
            model="openai/gpt-5.6-luna", validation_retries=1
        )
        stage.telemetry = SimpleNamespace(
            tokens_input_total=1200,
            tokens_output_total=500,
            cost_usd_total=0.002,
            first_attempt_duration_ms=200,
            validation_retry_duration_ms=None,
            validation_warnings=[],
        )
        stage.call.return_value = {"content": json.dumps(invalid)}

        with (
            patch.object(service, "_load_document", return_value=document),
            patch.object(
                service,
                "get_session",
                side_effect=[
                    session_context(claim_session),
                    session_context(MagicMock()),
                ],
            ),
            patch.object(service, "_load_news_context", return_value=[]),
            patch.object(service, "_previous_analysis", return_value=(None, 0)),
            patch.object(service, "LLMStage", return_value=stage),
        ):
            with self.assertRaises(LLMValidationError):
                service.analyze_document({}, document_id)

    def test_analysis_in_progress_raises_when_already_analyzing(self):
        document_id = "11111111-1111-1111-1111-111111111111"
        document = self._mock_document(document_id)
        claim_session = MagicMock()
        claim_session.execute.return_value.fetchone.return_value = (
            None  # Already running
        )

        with (
            patch.object(service, "_load_document", return_value=document),
            patch.object(
                service,
                "get_session",
                return_value=session_context(claim_session),
            ),
        ):
            with self.assertRaises(service.AnalysisInProgress):
                service.analyze_document({}, document_id)

    def test_document_not_found_raises_lookup_error(self):
        with patch.object(service, "_load_document", return_value=None):
            with self.assertRaises(LookupError):
                service.analyze_document({}, "missing-id")


if __name__ == "__main__":
    unittest.main()
