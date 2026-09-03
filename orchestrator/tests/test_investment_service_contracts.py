"""Tests for investment service contracts and Pydantic schema validation."""

import json
import sys
import unittest
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from investment_service_support import (
    investment_report_payload,
    relationship_metric,
    session_context,
)

import investment_service as service
from investment_schemas import (
    filing_content_spans,
    material_numeric_tokens,
    validate_investment_report_payload,
    validate_numeric_claim_rows,
)


class InvestmentRequestImmutabilityTests(unittest.TestCase):
    """Regressions for InvestmentAnalysisRequest immutability and dispatch."""

    def _build(self):
        return service.build_investment_analysis_request(
            {"company": "Example Company", "document_id": "doc-1"},
            "Demand remained durable.",
            [],
            {},
            {},
        )

    def test_source_schema_mutation_cannot_alter_stored_request(self):
        request = self._build()
        stored_schema = request.schema
        stored_fingerprint = request.fingerprint

        self.assertIsInstance(stored_schema, MappingProxyType)
        properties = stored_schema["properties"]
        self.assertIsInstance(properties, MappingProxyType)

        source = service._response_schema()
        source["schema"] = {}
        source["name"] = "mutated"

        self.assertEqual(request.schema, stored_schema)
        self.assertEqual(request.schema_name, "investment_report_narrative_v7")
        self.assertTrue(request.strict)
        self.assertEqual(request.fingerprint, stored_fingerprint)

    def test_nested_stored_mutation_is_refused(self):
        request = self._build()
        with self.assertRaises(TypeError):
            request.schema["properties"] = {}

    def test_independent_builds_are_canonically_equal(self):
        first = self._build()
        second = service.build_investment_analysis_request(
            {"document_id": "doc-1", "company": "Example Company"},
            "Demand remained durable.",
            [],
            {},
            {},
        )
        self.assertEqual(first, second)
        self.assertIsNot(first.schema, second.schema)
        self.assertEqual(first.prompt, second.prompt)
        self.assertEqual(first.schema_name, second.schema_name)
        self.assertTrue(second.strict)
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_relationship_contract_is_frozen_prompted_and_fingerprinted(self):
        empty = self._build()
        self.assertIsInstance(empty.relationship_facts, MappingProxyType)
        self.assertEqual(dict(empty.relationship_facts), {})
        self.assertIsInstance(empty.material_relationships, tuple)
        self.assertEqual(empty.material_relationships, ())

        current = {
            "operating_cash_flow": relationship_metric(
                42,
                role="cash_generation",
                metric_family="operating_cash_flow",
                cash_basis="cash",
            ),
        }
        populated = service.build_investment_analysis_request(
            {"company": "Example Company", "document_id": "doc-1"},
            "Cash generation and investment were disclosed.",
            [],
            current,
            {},
        )
        self.assertNotEqual(populated.fingerprint, empty.fingerprint)

    def test_llm_stage_dispatch_receives_plain_independent_schema(self):
        document_id = "11111111-1111-1111-1111-111111111111"
        analysis_id = "22222222-2222-2222-2222-222222222222"
        document = {
            "document_id": document_id,
            "company": "Example Company",
            "symbol": "EX",
            "region": "US",
            "industry": "Industrial Technology",
            "document_type": "annual_report",
            "report_date": None,
            "source_url": "",
            "filing_source": None,
            "filename": "report.txt",
            "extracted_text": "Demand remained durable across markets.",
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
            MagicMock(),
        ]
        captured = {}

        def make_stage(config, processor_id, **kwargs):
            captured["processor_id"] = processor_id
            captured.update(kwargs)
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
            stage.call.return_value = {
                "content": json.dumps(investment_report_payload())
            }
            captured["stage"] = stage
            return stage

        with (
            patch.object(service, "_load_document", return_value=document),
            patch.object(service, "_ensure_extracted_text", return_value="stored_document"),
            patch.object(service, "load_deterministic_facts", return_value=({}, {}, {"source": "none"})),
            patch.object(service, "_load_news_context", return_value=[]),
            patch.object(service, "_previous_analysis", return_value=(None, 0)),
            patch.object(service, "LLMStage", side_effect=make_stage),
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
                "get_analysis",
                return_value={"analysis_id": analysis_id},
            ),
        ):
            service.analyze_document({}, document_id)

        self.assertEqual(captured["processor_id"], "investment_analysis")
        stage = captured["stage"]
        self.assertEqual(stage.call.call_count, 1)


class InvestmentReportContractValidationTests(unittest.TestCase):
    """Direct contract tests against investment_schemas Pydantic validation."""

    def test_valid_payload_passes_validation(self):
        payload = investment_report_payload()
        problems = validate_investment_report_payload(payload, excerpt="Demand remained durable.")
        self.assertEqual(problems, [])

    def test_missing_required_section_rejected(self):
        payload = investment_report_payload()
        del payload["thesis"]
        problems = validate_investment_report_payload(payload)
        self.assertTrue(any("thesis" in p for p in problems))

    def test_invalid_confidence_enum_rejected(self):
        payload = investment_report_payload()
        payload["classification"]["confidence"] = "ultra_high"
        problems = validate_investment_report_payload(payload)
        self.assertTrue(problems)

    def test_invalid_qualitative_strength_rejected(self):
        payload = investment_report_payload()
        payload["qualitative"]["pricing_power"]["strength"] = "super_strong"
        problems = validate_investment_report_payload(payload)
        self.assertTrue(problems)

    def test_overlong_string_rejected(self):
        payload = investment_report_payload()
        payload["summary"] = "A" * 3000
        problems = validate_investment_report_payload(payload)
        self.assertTrue(problems)

    def test_ungrounded_evidence_quote_rejected(self):
        payload = investment_report_payload()
        payload["qualitative"]["ai_demand"]["present"] = True
        payload["qualitative"]["ai_demand"]["evidence"] = "Completely ungrounded quote from nowhere."
        problems = validate_investment_report_payload(payload, excerpt="Demand remained durable.")
        self.assertTrue(any("not grounded" in p for p in problems))

    def test_grounded_evidence_quote_accepted(self):
        payload = investment_report_payload()
        payload["qualitative"]["ai_demand"]["present"] = True
        payload["qualitative"]["ai_demand"]["evidence"] = "Demand remained durable"
        problems = validate_investment_report_payload(payload, excerpt="Demand remained durable across markets.")
        self.assertEqual(problems, [])

    def test_prohibited_language_advisory_detected(self):
        payload = investment_report_payload()
        payload["summary"] = "We recommend you buy the stock at support level with a stop-loss."
        problems = validate_investment_report_payload(payload, excerpt="Demand remained durable.")
        self.assertTrue(any("prohibited" in p for p in problems))

    def test_materiality_assessment_status_and_evidence(self):
        payload = investment_report_payload()
        payload["materiality_assessment"]["forward_guidance"]["status"] = "addressed"
        payload["materiality_assessment"]["forward_guidance"]["evidence"] = "Guidance raised"
        payload["materiality_assessment"]["forward_guidance"]["observation"] = "Guidance raised"
        payload["materiality_assessment"]["forward_guidance"]["implication"] = "Positive outlook"
        problems = validate_investment_report_payload(
            payload, excerpt="Demand remained durable. Guidance raised for the fiscal year."
        )
        self.assertEqual(problems, [])

    def test_catalyst_and_risk_validation(self):
        payload = investment_report_payload()
        payload["catalysts"] = [
            {
                "trigger": "New product launch in Q3",
                "expected_outcome": "Accelerates revenue growth",
                "horizon": "6 months",
                "epistemic_state": "supported",
                "uncertainty": "Execution risks remain",
                "evidence": "Product launch planned",
            }
        ]
        payload["risks"] = [
            {
                "sourced_observation": "Supply constraints persisted",
                "inference": "May limit shipment volume",
                "epistemic_state": "supported",
                "uncertainty": "Duration of bottleneck",
                "likelihood": "high",
                "impact": "medium",
                "mitigation": "Dual sourcing components",
                "evidence": "Supply constraints",
            }
        ]
        problems = validate_investment_report_payload(
            payload, excerpt="Demand remained durable. Product launch planned. Supply constraints noted."
        )
        self.assertEqual(problems, [])

    def test_numeric_claims_valid_row(self):
        rows = [
            {
                "claim_id": "c1",
                "path": "$.summary",
                "value": 1500.0,
                "metric": "revenue",
                "period": "FY2025",
                "unit": "usd_millions",
                "currency": "USD",
                "source_kind": "fact",
            }
        ]
        problems = validate_numeric_claim_rows(rows)
        self.assertEqual(problems, [])

    def test_numeric_claims_missing_metric_or_period(self):
        rows = [
            {
                "claim_id": "c1",
                "path": "$.summary",
                "value": 1500.0,
                "metric": "   ",
                "period": "",
                "unit": "usd_millions",
            }
        ]
        problems = validate_numeric_claim_rows(rows)
        self.assertTrue(len(problems) >= 2)

    def test_numeric_claims_non_finite_value(self):
        rows = [
            {
                "claim_id": "c1",
                "path": "$.summary",
                "value": float("nan"),
                "metric": "revenue",
                "period": "FY2025",
                "unit": "usd_millions",
            }
        ]
        problems = validate_numeric_claim_rows(rows)
        self.assertTrue(problems)

    def test_numeric_claims_duplicate_id(self):
        rows = [
            {
                "claim_id": "c1",
                "path": "$.summary",
                "value": 100,
                "metric": "rev",
                "period": "FY25",
                "unit": "usd_millions",
            },
            {
                "claim_id": "c1",
                "path": "$.thesis",
                "value": 200,
                "metric": "rev",
                "period": "FY25",
                "unit": "usd_millions",
            },
        ]
        problems = validate_numeric_claim_rows(rows)
        self.assertTrue(any("duplicate" in p for p in problems))

    def test_filing_content_spans_splitting(self):
        text = "[Source characters 0-100]\nFirst block of text.\n[Source characters 100-200]\nSecond block of text."
        spans = filing_content_spans(text)
        self.assertEqual(len(spans), 2)
        self.assertIn("First block", spans[0])
        self.assertIn("Second block", spans[1])

    def test_material_numeric_tokens_extraction(self):
        text = "Revenue reached $42.5B in FY2025 with 18% margin growth."
        tokens = material_numeric_tokens(text)
        self.assertTrue(len(tokens) >= 2)
