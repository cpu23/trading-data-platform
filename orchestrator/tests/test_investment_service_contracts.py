"""Tests for investment service."""

import copy
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


class InvestmentRequestImmutabilityTests(unittest.TestCase):
    """Regressions for ``InvestmentAnalysisRequest`` isolation: the stored
    schema and fingerprint survive any source-schema mutation, nested stored
    mutation is refused, independently built requests stay canonically equal,
    and the executor/``LLMStage`` seam receives only a plain independent
    schema so dispatch-side aliasing can never corrupt stored request state.
    Harness seams are mocked; no DB, network, or model is contacted."""

    def _build(self):
        return service.build_investment_analysis_request(
            {"company": "Example Company", "document_id": "doc-1"},
            "Demand remained durable.",
            [],
            {},
            {},
        )

    @staticmethod
    def _container_ids(node):
        """Collect ids of every mutable container reachable from ``node``."""
        found = set()

        def visit(current):
            if isinstance(current, (dict, list)):
                found.add(id(current))
                children = (
                    current.values() if isinstance(current, dict) else current
                )
                for child in children:
                    visit(child)

        visit(node)
        return found

    def _assert_plain_json_containers(self, node):
        if isinstance(node, dict):
            self.assertIs(type(node), dict)
            for child in node.values():
                self._assert_plain_json_containers(child)
        elif isinstance(node, list):
            self.assertIs(type(node), list)
            for child in node:
                self._assert_plain_json_containers(child)

    def test_source_schema_mutation_cannot_alter_stored_request(self):
        request = self._build()
        stored_schema = request.schema
        stored_fingerprint = request.fingerprint

        # The entire stored tree is immutable: mappings are proxies and
        # declared property sequences are tuples.
        self.assertIsInstance(stored_schema, MappingProxyType)
        properties = stored_schema["properties"]
        self.assertIsInstance(properties, MappingProxyType)
        self.assertIsInstance(
            properties["classification"]["properties"]["confidence"]["enum"],
            tuple,
        )
        self.assertIsInstance(properties["qualitative"]["required"], tuple)

        # ``_response_schema()`` builds a fresh plain schema per call, so a
        # reference held after construction can never alias the stored tree:
        # mutating it freely must leave the built request's schema, identity
        # fields, and fingerprint untouched.
        source = service._response_schema()
        source["schema"]["properties"]["classification"]["properties"][
            "confidence"
        ]["enum"].append("certain")
        source["schema"] = {}
        source["name"] = "mutated"

        self.assertEqual(request.schema, stored_schema)
        self.assertEqual(request.schema_name, "investment_report_narrative_v7")
        self.assertTrue(request.strict)
        self.assertEqual(request.fingerprint, stored_fingerprint)
        self.assertEqual(self._build().fingerprint, stored_fingerprint)

    def test_nested_stored_mutation_is_refused(self):
        request = self._build()
        with self.assertRaises(TypeError):
            request.schema["properties"]["classification"]["type"] = "string"
        with self.assertRaises(TypeError):
            request.schema["properties"]["qualitative"]["present"] = True
        with self.assertRaises(AttributeError):
            request.schema["properties"]["qualitative"]["required"].append(
                "extra"
            )

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
        self.assertIn('"relationship_facts": {}', empty.prompt)
        self.assertIn('"material_relationships": []', empty.prompt)

        current = {
            "operating_cash_flow": relationship_metric(
                42,
                role="cash_generation",
                metric_family="operating_cash_flow",
                cash_basis="cash",
            ),
            "capital_expenditures": relationship_metric(
                18,
                role="cash_investment",
                metric_family="capex",
                cash_basis="cash",
            ),
        }
        before = copy.deepcopy(current)
        populated = service.build_investment_analysis_request(
            {"company": "Example Company", "document_id": "doc-1"},
            "Cash generation and investment were disclosed.",
            [],
            current,
            {},
        )
        self.assertTrue(populated.relationship_facts)
        self.assertEqual(len(populated.material_relationships), 1)
        relationship_id = populated.material_relationships[0]["relationship_id"]
        self.assertIn(relationship_id, populated.prompt)
        self.assertNotEqual(populated.fingerprint, empty.fingerprint)

        current["operating_cash_flow"]["value"] = 0
        current["capital_expenditures"]["relationship_tags"]["cash_basis"] = "changed"
        rebuilt = service.build_investment_analysis_request(
            {"company": "Example Company", "document_id": "doc-1"},
            "Cash generation and investment were disclosed.",
            [],
            before,
            {},
        )
        changed = service.build_investment_analysis_request(
            {"company": "Example Company", "document_id": "doc-1"},
            "Cash generation and investment were disclosed.",
            [],
            current,
            {},
        )
        self.assertEqual(
            service._plain_json_value(populated.relationship_facts),
            service._plain_json_value(rebuilt.relationship_facts),
        )
        self.assertEqual(populated.fingerprint, rebuilt.fingerprint)
        self.assertNotEqual(populated.fingerprint, changed.fingerprint)
        with self.assertRaises(TypeError):
            populated.relationship_facts["new"] = {}
        with self.assertRaises(TypeError):
            populated.material_relationships[0]["compatibility"] = "changed"

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
            "extracted_text": (
                "Annual report evidence: AI demand accelerated, data-centre "
                "deployments expanded despite tight supply, higher pricing "
                "held. Demand remained durable."
            ),
        }
        request_inputs = {
            key: value
            for key, value in document.items()
            if key not in {"extracted_text", "raw_content"}
        }
        excerpt = document["extracted_text"]
        deterministic_current = {}
        deterministic_prior = {}
        expected = service.build_investment_analysis_request(
            request_inputs,
            excerpt,
            [],
            deterministic_current,
            deterministic_prior,
        )

        claim_session = MagicMock()
        claim_session.execute.return_value.fetchone.return_value = (
            document_id,
        )
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
            patch.object(
                service,
                "_ensure_extracted_text",
                return_value="stored_document",
            ),
            patch.object(
                service,
                "load_deterministic_facts",
                return_value=({}, {}, {"source": "none"}),
            ),
            patch.object(service, "_load_news_context", return_value=[]),
            patch.object(
                service, "_previous_analysis", return_value=(None, 0)
            ),
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
        self.assertEqual(stage.call.call_args.args[0], expected.prompt)
        # The default dispatch path hands LLMStage the request envelope with
        # a plain independent copy of the stored frozen schema.
        dispatched = captured["response_schema"]
        self.assertIs(type(dispatched), dict)
        self.assertEqual(
            sorted(dispatched), ["name", "schema", "strict"]
        )
        self.assertEqual(dispatched["name"], expected.schema_name)
        self.assertTrue(dispatched["strict"])
        self._assert_plain_json_containers(dispatched["schema"])
        self.assertEqual(
            dispatched["schema"],
            service._plain_json_value(expected.schema),
        )
        self.assertFalse(
            self._container_ids(dispatched)
            & self._container_ids(expected.schema)
        )

        # Mutating the executor-held copy can never diverge the stored
        # request: a rebuild from identical inputs stays canonically equal.
        dispatched["schema"]["properties"]["classification"]["properties"][
            "confidence"
        ]["enum"].append("certain")
        dispatched["schema"]["required"] = []
        dispatched["name"] = "mutated"
        tampered = service.build_investment_analysis_request(
            request_inputs,
            excerpt,
            [],
            deterministic_current,
            deterministic_prior,
        )
        self.assertEqual(tampered, expected)
        self.assertEqual(tampered.fingerprint, expected.fingerprint)

    def test_executor_packet_carries_plain_independent_schema(self):
        request = self._build()
        packet = request.packet()
        self.assertEqual(
            sorted(packet),
            ["prompt", "schema", "schema_name", "strict"],
        )
        self.assertEqual(packet["prompt"], request.prompt)
        self.assertEqual(packet["schema_name"], request.schema_name)
        self.assertTrue(packet["strict"])
        self._assert_plain_json_containers(packet["schema"])
        self.assertEqual(
            packet["schema"], service._plain_json_value(request.schema)
        )
        self.assertFalse(
            self._container_ids(packet["schema"])
            & self._container_ids(request.schema)
        )
        # The packet stays JSON-serializable for artifact/executor transport.
        self.assertIn("investment_report_narrative_v7", json.dumps(packet))
        # Packet mutation never reaches the stored request.
        packet["schema"]["properties"]["classification"]["properties"][
            "confidence"
        ]["enum"].append("certain")
        self.assertNotIn(
            "certain",
            request.schema["properties"]["classification"]["properties"][
                "confidence"
            ]["enum"],
        )


class InvestmentAnalysisServiceTests(unittest.TestCase):
    def _sample_analysis_facts(self, **overrides):
        facts = investment_report_payload()
        facts["classification"] = {
            "document_type": "annual_report",
            "sector": "Technology",
            "industry": "Consumer",
            "region": "US",
            "confidence": "low",
        }
        facts["qualitative"] = {
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
        }
        facts["summary"] = "Demand, capex and pricing are accelerating."
        facts["thesis"] = "The cycle is strengthening; falling demand would invalidate it."
        facts["counter_thesis"] = "Overcapacity or slowing demand could invalidate the thesis."
        facts["drivers"] = ["AI demand"]
        facts["catalysts"] = [
            {
                "trigger": "capacity additions continue",
                "expected_outcome": "Available capacity increases",
                "horizon": "within the next year",
                "epistemic_state": "supported",
                "uncertainty": "The timing and usable output remain uncertain",
                "evidence": "capacity additions continue",
            }
        ]
        facts["risks"] = [
            {
                "sourced_observation": "capacity additions continue",
                "inference": "Available supply could exceed demand",
                "epistemic_state": "hypothesis",
                "uncertainty": "Future demand and supply utilization remain uncertain",
                "likelihood": "medium",
                "impact": "high",
                "mitigation": "Monitor inventory",
                "evidence": "capacity additions continue",
            }
        ]
        facts["watch_items"] = ["Inventory growth"]
        facts.update(overrides)
        return facts

    def test_analysis_industry_precedence_uses_checked_in_metadata_first(self):
        cases = [
            # Checked-in issuer metadata wins over any model label.
            (
                {
                    "symbol": "MU",
                    "company": "Micron Technology",
                    "industry": "Unclassified",
                },
                {"industry": "Consumer", "confidence": "high"},
                "Semiconductors & Compute",
            ),
            (
                {
                    "symbol": "MU",
                    "company": "Micron Technology",
                    "industry": "Unclassified",
                },
                {"industry": "Unclassified", "confidence": "low"},
                "Semiconductors & Compute",
            ),
            # Model Unclassified must not overwrite the document industry.
            (
                {
                    "symbol": "ZZZZ",
                    "company": "Unknown Co",
                    "industry": "Semiconductors & Compute",
                },
                {"industry": "Unclassified", "confidence": "high"},
                "Semiconductors & Compute",
            ),
            # Low-confidence model labels must not overwrite the document industry.
            (
                {
                    "symbol": "ZZZZ",
                    "company": "Unknown Co",
                    "industry": "Semiconductors & Compute",
                },
                {"industry": "Consumer", "confidence": "low"},
                "Semiconductors & Compute",
            ),
            # A concrete, trusted model label still classifies unknown issuers.
            (
                {"symbol": "ZZZZ", "company": "Unknown Co", "industry": "Unclassified"},
                {"industry": "Beverages", "confidence": "moderate"},
                "Consumer",
            ),
            # Unknown issuer with only a low-confidence model label fails closed.
            (
                {"symbol": "ZZZZ", "company": "Unknown Co", "industry": "Unclassified"},
                {"industry": "Consumer", "confidence": "low"},
                "Unclassified",
            ),
        ]
        for document, classification, expected in cases:
            with self.subTest(document=document, classification=classification):
                self.assertEqual(
                    service._resolve_analysis_industry(document, classification),
                    expected,
                )

    def test_analysis_stores_checked_in_industry_over_model_classification(self):
        document_id = "33333333-3333-3333-3333-333333333333"
        analysis_id = "44444444-4444-4444-4444-444444444444"
        document = {
            "document_id": document_id,
            "company": "Micron Technology",
            "symbol": "MU",
            "region": "US",
            "industry": "Unclassified",
            "document_type": "annual_report",
            "report_date": None,
            "source_url": "https://example.com/report",
            "filename": "report.txt",
            "extracted_text": (
                "Annual report evidence: AI demand accelerated, data-centre "
                "deployments expanded despite tight supply, higher pricing held, "
                "guidance was raised, capex grew, and capacity additions continue."
            ),
        }
        facts = self._sample_analysis_facts()

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
            model="openai/gpt-5.6-luna",
            validation_retries=1,
        )
        stage.telemetry = SimpleNamespace(
            tokens_input_total=1000,
            tokens_output_total=400,
            cost_usd_total=0.001,
            first_attempt_duration_ms=150,
            validation_retry_duration_ms=None,
            validation_warnings=[],
        )
        stage.call.return_value = {"content": json.dumps(facts)}

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

        statements = [
            str(call.args[0]) for call in persist_session.execute.call_args_list
        ]
        update_index = next(
            index
            for index, statement in enumerate(statements)
            if "UPDATE investment_documents" in statement
        )
        self.assertEqual(
            persist_session.execute.call_args_list[update_index].args[1]["industry"],
            "Semiconductors & Compute",
        )
        insert_index = next(
            index
            for index, statement in enumerate(statements)
            if "INSERT INTO investment_analyses" in statement
        )
        stored_analysis = json.loads(
            persist_session.execute.call_args_list[insert_index].args[1]["analysis"]
        )
        self.assertEqual(
            stored_analysis["classification"]["industry"], "Semiconductors & Compute"
        )

    @patch("investment_service.get_session")
    def test_get_analysis_applies_checked_in_industry_to_legacy_row(self, get_session):
        row = SimpleNamespace(
            _mapping={
                "analysis_id": "a-mu",
                "document_id": "d-mu",
                "previous_document_id": None,
                "facts": {},
                "analysis": {
                    "classification": {
                        "industry": "Unclassified",
                        "confidence": "low",
                    },
                    "summary": "Legacy summary",
                    "thesis": "Legacy thesis",
                    "drivers": [],
                    "catalysts": [],
                    "risks": [],
                    "watch_items": [],
                },
                "model": "legacy",
                "created_at": None,
                "updated_at": None,
                "company": "Micron Technology",
                "symbol": "MU",
                "region": "US",
                "industry": "Unclassified",
                "document_type": "annual_report",
                "report_date": None,
                "source_url": None,
            }
        )
        session = MagicMock()
        session.execute.return_value.fetchone.return_value = row
        get_session.return_value = session_context(session)

        result = service.get_analysis({}, "a-mu")

        self.assertEqual(result["industry"], "Semiconductors & Compute")
        self.assertEqual(
            result["classification"]["industry"], "Semiconductors & Compute"
        )

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
            "extracted_text": (
                "Annual report evidence: AI demand accelerated, data-centre "
                "deployments expanded despite tight supply, higher pricing held, "
                "guidance was raised, capex grew, and capacity additions continue."
            ),
            "filename": "report.txt",
        }
        facts = self._sample_analysis_facts(
            classification={
                "document_type": "annual_report",
                "sector": "Technology",
                "industry": "Memory semiconductors",
                "region": "Asia",
                "confidence": "high",
            }
        )

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
        # The dispatch seam receives the request envelope whose nested
        # narrative schema is a plain independent copy, never the frozen
        # stored tree nor a shared mutable structure.
        self.assertIs(type(schema), dict)
        self.assertEqual(schema["name"], "investment_report_narrative_v7")
        self.assertTrue(schema["strict"])
        self.assertIs(type(schema["schema"]), dict)
        self.assertNotIn("metrics", schema["schema"]["properties"])
        self.assertNotIn("prior_metrics", schema["schema"]["properties"])
        prompt = stage.call.call_args.args[0]
        self.assertIn("Reuters", prompt)
        self.assertIn("FILING EXCERPT", prompt)
        prompt_policy = prompt.casefold()
        self.assertIn(
            "company-stated mitigation or a non-advisory monitoring response",
            prompt_policy,
        )
        self.assertNotIn("company, portfolio, or monitoring mitigation", prompt_policy)
        self.assertNotRegex(
            prompt_policy,
            r"\bportfolio\b[^\n]{0,40}\bmitigation\b|"
            r"\bmitigation\b[^\n]{0,40}\bportfolio\b",
        )
        self.assertRegex(
            prompt_policy,
            r"(?:do not|never)[^\n]*portfolio[^\n]*sizing[^\n]*"
            r"allocation[^\n]*exposure[^\n]*instructions",
        )
        statements = [
            str(call.args[0]) for call in persist_session.execute.call_args_list
        ]
        self.assertTrue(
            any("INSERT INTO processing_log" in statement for statement in statements)
        )
        processing_params = persist_session.execute.call_args_list[-1].args[1]
        self.assertEqual(processing_params["cost_usd"], 0.002)
        self.assertEqual(processing_params["model_used"], "openai/gpt-5.6-luna")
        request_metadata = json.loads(processing_params["request_metadata"])
        self.assertEqual(request_metadata["rule_version"], "7")
        get_analysis.assert_called_once_with({}, analysis_id)


class InvestmentUrlIngestModelTests(unittest.TestCase):
    def _model(self):
        from contracts import InvestmentUrlIngestRequest

        return InvestmentUrlIngestRequest

    def test_rejects_unknown_metadata_fields(self):
        with self.assertRaises(ValueError):
            self._model().model_validate(
                {
                    "url": "https://example.test/r",
                    "company": "C",
                    "mystery_field": 1,
                }
            )

    def test_rejects_oversized_url(self):
        with self.assertRaises(ValueError):
            self._model().model_validate(
                {
                    "url": "https://example.test/" + "x" * 2100,
                    "company": "C",
                }
            )

    def test_rejects_non_bool_analyze(self):
        with self.assertRaises(ValueError):
            self._model().model_validate(
                {"url": "https://example.test/r", "company": "C", "analyze": "yes"}
            )

    def test_requires_url_and_company(self):
        with self.assertRaises(ValueError):
            self._model().model_validate({"company": "C"})
        with self.assertRaises(ValueError):
            self._model().model_validate({"url": "https://example.test/r"})

    def test_accepts_known_bounded_shape(self):
        model = self._model().model_validate(
            {
                "url": "https://example.test/r",
                "company": "C",
                "symbol": "X",
                "region": "US",
                "document_type": "annual_report",
                "analyze": True,
            }
        )
        self.assertEqual(model.url, "https://example.test/r")
        self.assertTrue(model.analyze)
        self.assertIsNone(model.filename)


class InvestmentFinalizationDeterminismTests(unittest.TestCase):
    """Finalized analyses preserve supplied monetary facts: supplemental
    metrics survive with provenance, explicit FCF wins, incompatible units
    stay unknown."""

    def _document(self):
        return {
            "company": "Microsoft Corporation",
            "symbol": "MSFT",
            "region": "US",
            "industry": "Software, Cloud & Communications",
            "document_type": "earnings_transcript",
        }

    def _finalize(self, deterministic_current):
        return service.finalize_investment_analysis(
            investment_report_payload(),
            document=self._document(),
            deterministic_current=deterministic_current,
            deterministic_prior={},
            market_inputs={},
            stored_previous_facts={},
            previous_state=None,
            prior_count=0,
            news_items=[],
            extraction={"report_text_source": "stored_document"},
            relationship_facts={},
            material_relationships=[],
        )

    @staticmethod
    def _metric(value, unit, period="FY2024-Q4"):
        return {
            "value": value,
            "unit": unit,
            "period": period,
            "evidence": ["press release"],
            "source_url": "https://www.microsoft.com/en-us/investor",
        }

    def test_supplemental_metrics_survive_finalization_with_provenance(self):
        result = self._finalize(
            {
                "revenue": self._metric(64_727.0, "usd_millions"),
                "operating_cash_flow": self._metric(37_200.0, "usd_millions"),
                "capex": self._metric(13_900.0, "usd_millions"),
                "microsoft_cloud_revenue": self._metric(36_800.0, "usd_millions"),
                "microsoft_cloud_gross_margin_percent": self._metric(
                    69.0, "percent"
                ),
                "azure_growth_from_ai_services_points": self._metric(
                    8.0, "percentage_points"
                ),
            }
        )
        metrics = result.analysis["metrics"]
        # Supplied Microsoft metrics are not rewritten to null/missing.
        cloud_revenue = metrics["microsoft_cloud_revenue"]
        self.assertEqual(cloud_revenue["value"], 36_800.0)
        self.assertEqual(cloud_revenue["unit"], "usd_millions")
        self.assertEqual(cloud_revenue["period"], "FY2024-Q4")
        self.assertEqual(cloud_revenue["evidence"], ["press release"])
        cloud_gm = metrics["microsoft_cloud_gross_margin_percent"]
        self.assertEqual(cloud_gm["value"], 69.0)
        self.assertEqual(cloud_gm["unit"], "percent")
        ai_points = metrics["azure_growth_from_ai_services_points"]
        self.assertEqual(ai_points["value"], 8.0)
        self.assertIn("press release", str(ai_points["evidence"]))

    def test_explicit_reported_fcf_wins_over_derivation_in_final_output(self):
        result = self._finalize(
            {
                "revenue": self._metric(100.0, "usd_billions"),
                "operating_cash_flow": self._metric(37.2, "usd_billions"),
                "capex": self._metric(13.9, "usd_billions"),
                "free_cash_flow": self._metric(25.0, "usd_billions"),
            }
        )
        valuation = result.analysis["valuation"]
        self.assertEqual(result.analysis["metrics"]["fcf"]["value"], 25.0)
        self.assertIsNone(valuation["assumptions"]["starting_fcf"])
        self.assertEqual(valuation["dcf"]["status"], "unavailable")
        self.assertEqual(
            valuation["dcf"]["reason"],
            "starting FCF must be annual, TTM, LTM, or 12-month",
        )
        self.assertEqual(
            valuation["dcf"]["sensitivity"]["status"], "unavailable"
        )
        self.assertEqual(
            valuation["dcf"]["sensitivity"]["reason"],
            "starting FCF must be annual, TTM, LTM, or 12-month",
        )

    def test_scale_incompatible_operands_finalize_to_unknown(self):
        result = self._finalize(
            {
                "revenue": self._metric(64_727.0, "usd_millions"),
                "operating_cash_flow": self._metric(37_200.0, "usd_millions"),
                "capex": self._metric(13.9, "usd_billions"),
            }
        )
        self.assertIsNone(result.analysis["metrics"]["fcf"]["value"])
        self.assertIsNone(result.analysis["fundamentals"]["operating_cash_conversion"])

    def test_finalization_does_not_restore_satisfied_missing_fact_watches(self):
        parsed = investment_report_payload()
        parsed["watch_items"] = [
            "gross margin: missing comparable evidence",
            "guidance: missing comparable evidence",
            "customer concentration",
        ]
        guidance = self._metric(120.0, "usd_billions", period="FY2025")
        guidance["relationship_tags"] = {"temporal_basis": "guidance"}

        result = service.finalize_investment_analysis(
            parsed,
            document=self._document(),
            deterministic_current={
                "gross_margin": self._metric(63.0, "percent"),
                "guidance_revenue": guidance,
            },
            deterministic_prior={},
            market_inputs={},
            stored_previous_facts={},
            previous_state=None,
            prior_count=0,
            news_items=[],
            extraction={"report_text_source": "stored_document"},
            relationship_facts={},
            material_relationships=[],
        )

        watch_items = result.analysis["watch_items"]
        self.assertNotIn(
            "gross margin: missing comparable evidence",
            watch_items,
        )
        self.assertNotIn("guidance: missing comparable evidence", watch_items)
        self.assertEqual(watch_items[0], "customer concentration")

    def test_finalization_retains_direct_fact_warning_when_evidence_is_missing(self):
        parsed = investment_report_payload()
        parsed["watch_items"] = [
            "gross margin: missing comparable evidence",
            "customer concentration",
        ]
        result = service.finalize_investment_analysis(
            parsed,
            document=self._document(),
            deterministic_current={
                "revenue": self._metric(100.0, "usd_billions"),
            },
            deterministic_prior={},
            market_inputs={},
            stored_previous_facts={},
            previous_state=None,
            prior_count=0,
            news_items=[],
            extraction={"report_text_source": "stored_document"},
            relationship_facts={},
            material_relationships=[],
        )
        watch_items = result.analysis["watch_items"]
        self.assertIn("gross margin: missing comparable evidence", watch_items)
        self.assertIn("customer concentration", watch_items)

    def test_finalization_preserves_v7_narrative_and_contract_fields(self):
        parsed = investment_report_payload()
        parsed["counter_thesis"] = (
            "Downside risks to memory pricing could invalidate the cycle thesis."
        )
        parsed["materiality_assessment"]["forward_guidance"] = {
            "status": "addressed",
            "observation": "Management guided FY2026 revenue to $100B.",
            "implication": "Outlook provides medium-term revenue visibility.",
            "evidence": "FY2026 revenue expectation is $100 billion.",
        }
        parsed["relationship_reconciliations"] = [
            {
                "relationship_id": "rel-fcf",
                "status": "reconciled",
                "fact_paths": ["deterministic_current.relationship_facts.fcf"],
                "observation": "Free cash flow was $25B in FY2024 Q4.",
                "interpretation": "Cash generation covered investment.",
                "uncertainty": "Working capital changes may alter conversion.",
                "summary_synthesis": "Cash generation covered investment.",
                "thesis_synthesis": "Internal funding supports the reinvestment thesis.",
                "summary_fact_paths": [
                    "deterministic_current.relationship_facts.fcf"
                ],
            }
        ]
        result = service.finalize_investment_analysis(
            parsed,
            document=self._document(),
            deterministic_current={
                "revenue": self._metric(100.0, "usd_billions"),
            },
            deterministic_prior={},
            market_inputs={},
            stored_previous_facts={},
            previous_state=None,
            prior_count=0,
            news_items=[],
            extraction={"report_text_source": "stored_document"},
            relationship_facts={"fcf": {"value": 25.0}},
            material_relationships=[
                {"relationship_id": "rel-fcf", "compatibility": "compatible"}
            ],
        )
        self.assertEqual(
            result.analysis["counter_thesis"],
            "Downside risks to memory pricing could invalidate the cycle thesis.",
        )
        self.assertEqual(
            result.analysis["materiality_assessment"]["forward_guidance"]["status"],
            "addressed",
        )
        reconciliations = result.analysis["relationship_reconciliations"]
        self.assertEqual(len(reconciliations), 1)
        self.assertEqual(reconciliations[0]["relationship_id"], "rel-fcf")
        self.assertEqual(reconciliations[0]["status"], "reconciled")
        self.assertEqual(
            reconciliations[0]["summary_synthesis"],
            "Cash generation covered investment.",
        )
        self.assertEqual(
            reconciliations[0]["summary_fact_paths"],
            ["deterministic_current.relationship_facts.fcf"],
        )


if __name__ == '__main__':
    unittest.main()
