"""Tests for investment service."""

import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from investment_service_support import (
    epistemic_catalyst,
    epistemic_risk,
    investment_report_payload,
    metric,
    relationship_metric,
    session_context,
)

import investment_service as service


class InvestmentStageSeamTests(unittest.TestCase):
    """Direct-call seam coverage: request identity, exact schema, and
    evidence grounding (no mocks, DB, network, or model)."""

    def _payload(self):
        return investment_report_payload()


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

    def test_schema_flags_missing_and_extra_properties(self):
        self.assertEqual(service.validate_investment_report_payload(self._payload()), [])
        missing_thesis = self._payload()
        del missing_thesis["thesis"]
        self.assertEqual(
            service.validate_investment_report_payload(missing_thesis),
            ["$: missing required property 'thesis'"],
        )
        extra = self._payload()
        extra["classification"]["ticker"] = "ACME"
        self.assertEqual(
            service.validate_investment_report_payload(extra),
            ["$.classification: unexpected property 'ticker'"],
        )

    def test_schema_rejects_wrong_enum_values(self):
        bad_confidence = self._payload()
        bad_confidence["classification"]["confidence"] = "certain"
        bad_strength = self._payload()
        bad_strength["qualitative"]["pricing_power"]["strength"] = "overwhelming"
        self.assertEqual(
            service.validate_investment_report_payload(bad_confidence),
            ["$.classification.confidence: must be one of ['low', 'moderate', 'high']"],
        )
        self.assertEqual(
            service.validate_investment_report_payload(bad_strength),
            [
                "$.qualitative.pricing_power.strength: must be one of "
                "['none', 'weak', 'moderate', 'strong']"
            ],
        )

    def test_v7_schema_declares_exact_epistemic_rows_and_bounds(self):
        response = service._response_schema()
        self.assertEqual(response["name"], "investment_report_narrative_v7")
        schema = response["schema"]
        self.assertIn("relationship_reconciliations", schema["required"])
        self.assertIn("counter_thesis", schema["required"])
        self.assertIn("materiality_assessment", schema["required"])
        expected = {
            "risks": (
                {
                    "sourced_observation",
                    "inference",
                    "epistemic_state",
                    "uncertainty",
                    "likelihood",
                    "impact",
                    "mitigation",
                    "evidence",
                },
                {
                    "sourced_observation": 600,
                    "inference": 600,
                    "uncertainty": 400,
                    "mitigation": 600,
                    "evidence": 600,
                },
            ),
            "catalysts": (
                {
                    "trigger",
                    "expected_outcome",
                    "horizon",
                    "epistemic_state",
                    "uncertainty",
                    "evidence",
                },
                {
                    "trigger": 600,
                    "expected_outcome": 600,
                    "horizon": 200,
                    "uncertainty": 400,
                    "evidence": 600,
                },
            ),
        }
        for collection, (keys, lengths) in expected.items():
            with self.subTest(collection=collection):
                declaration = schema["properties"][collection]
                self.assertEqual(declaration["maxItems"], 12)
                item = declaration["items"]
                self.assertFalse(item["additionalProperties"])
                self.assertEqual(set(item["required"]), keys)
                self.assertEqual(set(item["properties"]), keys)
                self.assertEqual(
                    item["properties"]["epistemic_state"]["enum"],
                    ["observed", "supported", "hypothesis"],
                )
                for field, max_length in lengths.items():
                    self.assertEqual(item["properties"][field]["minLength"], 1)
                    self.assertEqual(
                        item["properties"][field]["maxLength"], max_length
                    )
        self.assertNotIn(
            "risk", schema["properties"]["risks"]["items"]["properties"]
        )
        self.assertNotIn(
            "catalyst", schema["properties"]["catalysts"]["items"]["properties"]
        )
        reconciliations = schema["properties"]["relationship_reconciliations"]
        self.assertEqual(reconciliations["maxItems"], 3)
        reconciliation = reconciliations["items"]
        reconciliation_keys = {
            "relationship_id",
            "status",
            "fact_paths",
            "observation",
            "interpretation",
            "uncertainty",
            "summary_synthesis",
            "thesis_synthesis",
            "summary_fact_paths",
        }
        self.assertFalse(reconciliation["additionalProperties"])
        self.assertEqual(set(reconciliation["required"]), reconciliation_keys)
        self.assertEqual(set(reconciliation["properties"]), reconciliation_keys)
        self.assertEqual(
            reconciliation["properties"]["status"]["enum"],
            ["reconciled", "abstained_incompatible"],
        )
        fact_paths = reconciliation["properties"]["fact_paths"]
        self.assertEqual((fact_paths["minItems"], fact_paths["maxItems"]), (1, 8))
        self.assertTrue(fact_paths["uniqueItems"])
        self.assertEqual(fact_paths["items"]["maxLength"], 300)
        for field, max_length in (
            ("relationship_id", 80),
            ("observation", 450),
            ("interpretation", 350),
            ("uncertainty", 200),
        ):
            self.assertEqual(
                reconciliation["properties"][field]["maxLength"], max_length
            )

    def test_v7_schema_declares_closed_materiality_topic_contract(self):
        schema = service._response_schema()["schema"]
        counter_thesis = schema["properties"]["counter_thesis"]
        self.assertEqual(counter_thesis["type"], "string")
        self.assertEqual(counter_thesis["minLength"], 1)
        self.assertEqual(counter_thesis["maxLength"], 1200)

        assessment = schema["properties"]["materiality_assessment"]
        topics = {
            "forward_guidance",
            "reported_variance_driver",
            "margin_economics",
            "capital_commitment_duration",
        }
        self.assertEqual(set(assessment["required"]), topics)
        self.assertEqual(set(assessment["properties"]), topics)
        self.assertFalse(assessment["additionalProperties"])
        for topic in topics:
            with self.subTest(topic=topic):
                declaration = assessment["properties"][topic]
                self.assertFalse(declaration["additionalProperties"])
                self.assertEqual(
                    set(declaration["required"]),
                    {"status", "observation", "implication", "evidence"},
                )
                self.assertEqual(
                    set(declaration["properties"]),
                    {"status", "observation", "implication", "evidence"},
                )
                self.assertEqual(
                    declaration["properties"]["status"]["enum"],
                    ["addressed", "not_disclosed"],
                )
                for field in ("observation", "implication", "evidence"):
                    self.assertEqual(
                        declaration["properties"][field]["maxLength"], 600
                    )

    def test_v7_risk_and_catalyst_positive_and_adversarial_shapes(self):
        valid = self._payload()
        valid["risks"] = [epistemic_risk()]
        valid["catalysts"] = [epistemic_catalyst()]
        self.assertEqual(service.validate_investment_report_payload(valid), [])

        cases = {}
        removed_key = copy.deepcopy(valid)
        removed_key["risks"][0]["risk"] = removed_key["risks"][0].pop("inference")
        cases["removed risk key"] = removed_key
        removed_catalyst_key = copy.deepcopy(valid)
        removed_catalyst_key["catalysts"][0]["catalyst"] = (
            removed_catalyst_key["catalysts"][0].pop("trigger")
        )
        cases["removed catalyst key"] = removed_catalyst_key
        blank = copy.deepcopy(valid)
        blank["catalysts"][0]["uncertainty"] = " "
        cases["blank uncertainty"] = blank
        invalid_state = copy.deepcopy(valid)
        invalid_state["risks"][0]["epistemic_state"] = "certain"
        cases["invalid epistemic state"] = invalid_state
        too_many = copy.deepcopy(valid)
        too_many["risks"] = [epistemic_risk() for _ in range(13)]
        cases["risk row bound"] = too_many
        too_long = copy.deepcopy(valid)
        too_long["catalysts"][0]["horizon"] = "x" * 201
        cases["string bound"] = too_long
        for label, payload in cases.items():
            with self.subTest(case=label):
                self.assertTrue(
                    service.validate_investment_report_payload(payload),
                    f"{label} must fail closed",
                )

    def test_all_materiality_topics_accept_exact_evidence_and_implications(self):
        payload = self._payload()
        rows = {
            "forward_guidance": (
                "Management expects revenue growth in the next fiscal year",
                "The stated outlook supports near-term growth visibility",
            ),
            "reported_variance_driver": (
                "Revenue increased primarily due to higher unit volumes",
                "Volume rather than price drove the reported variance",
            ),
            "margin_economics": (
                "Gross margin improved because product mix shifted",
                "Mix is the stated source of margin improvement",
            ),
            "capital_commitment_duration": (
                "Construction commitments extend beyond the current year",
                "The investment program creates a multi-period cash commitment",
            ),
        }
        for topic, (evidence, implication) in rows.items():
            payload["materiality_assessment"][topic] = {
                "status": "addressed",
                "observation": evidence,
                "implication": implication,
                "evidence": evidence,
            }
        excerpt = "Demand remained durable. " + ". ".join(
            evidence for evidence, _ in rows.values()
        )

        self.assertEqual(service.validate_investment_report_payload(payload), [])
        self.assertEqual(
            service.investment_evidence_violations(
                payload, excerpt=excerpt, news_items=[]
            ),
            [],
        )
        for topic, (evidence, implication) in rows.items():
            with self.subTest(topic=topic):
                self.assertEqual(
                    payload["materiality_assessment"][topic]["observation"],
                    evidence,
                )
                self.assertEqual(
                    payload["materiality_assessment"][topic]["evidence"], evidence
                )
                self.assertEqual(
                    payload["materiality_assessment"][topic]["implication"],
                    implication,
                )

    def test_materiality_status_contract_rejects_blanks_and_filler(self):
        for field in ("observation", "implication", "evidence"):
            with self.subTest(status="addressed", field=field):
                payload = self._payload()
                payload["materiality_assessment"]["forward_guidance"] = {
                    "status": "addressed",
                    "observation": "Management expects revenue growth",
                    "implication": "The outlook supports growth visibility",
                    "evidence": "Management expects revenue growth",
                }
                payload["materiality_assessment"]["forward_guidance"][field] = "   "
                problems = service.validate_investment_report_payload(payload)
                self.assertTrue(problems)
                self.assertTrue(
                    any(
                        "materiality_assessment.forward_guidance" in item
                        for item in problems
                    )
                )

        for field in ("observation", "implication", "evidence"):
            with self.subTest(status="not_disclosed", field=field):
                payload = self._payload()
                payload["materiality_assessment"]["margin_economics"][field] = (
                    "Unsupported filler"
                )
                problems = service.validate_investment_report_payload(payload)
                self.assertTrue(problems)
                self.assertTrue(
                    any(
                        "materiality_assessment.margin_economics" in item
                        for item in problems
                    )
                )

        whitespace = self._payload()
        whitespace["materiality_assessment"]["margin_economics"]["evidence"] = "   "
        problems = service.validate_investment_report_payload(whitespace)
        self.assertTrue(
            any("materiality_assessment.margin_economics" in item for item in problems)
        )

    def test_materiality_and_counter_thesis_fail_closed_structurally(self):
        cases = {}
        missing_counter = self._payload()
        del missing_counter["counter_thesis"]
        cases["missing counter thesis"] = missing_counter
        blank_counter = self._payload()
        blank_counter["counter_thesis"] = " "
        cases["blank counter thesis"] = blank_counter
        missing_topic = self._payload()
        del missing_topic["materiality_assessment"]["reported_variance_driver"]
        cases["missing topic"] = missing_topic
        extra_topic = self._payload()
        extra_topic["materiality_assessment"]["generic_commentary"] = {
            "status": "not_disclosed",
            "observation": "",
            "implication": "",
            "evidence": "",
        }
        cases["extra topic"] = extra_topic
        invalid_status = self._payload()
        invalid_status["materiality_assessment"]["forward_guidance"]["status"] = (
            "not_material"
        )
        cases["invalid status"] = invalid_status
        for label, payload in cases.items():
            with self.subTest(case=label):
                self.assertTrue(
                    service.validate_investment_report_payload(payload),
                    f"{label} must fail closed",
                )

    def test_materiality_evidence_must_be_grounded_in_filing(self):
        payload = self._payload()
        payload["materiality_assessment"]["margin_economics"] = {
            "status": "addressed",
            "observation": "Gross margin improved because product mix shifted",
            "implication": "Mix supports current margin economics",
            "evidence": "Invented margin statement",
        }
        violations = service.investment_evidence_violations(
            payload,
            excerpt=(
                "Demand remained durable. Gross margin improved because "
                "product mix shifted."
            ),
            news_items=[],
        )
        self.assertEqual(len(violations), 1)
        self.assertIn("materiality_assessment.margin_economics", violations[0])
        self.assertIn("not grounded", violations[0])

    def test_numeric_materiality_observations_require_ledger_rows(self):
        observations = {
            "forward_guidance": "Revenue guidance was $10 million in FY2026.",
            "reported_variance_driver": (
                "Revenue increased 10% year over year in FY2025."
            ),
            "margin_economics": "Gross margin was 40% in FY2025.",
            "capital_commitment_duration": (
                "Capital commitments extend for 3 years from FY2025."
            ),
        }
        for topic, observation in observations.items():
            with self.subTest(topic=topic):
                payload = self._payload()
                payload["materiality_assessment"][topic] = {
                    "status": "addressed",
                    "observation": observation,
                    "implication": "The quantified disclosure is thesis-relevant",
                    "evidence": observation,
                }
                problems = service.numeric_claim_source_problems(
                    payload,
                    deterministic_current={},
                    deterministic_prior={},
                    excerpt=f"Demand remained durable. {observation}",
                    news_items=[],
                )
                self.assertTrue(problems)
                self.assertTrue(
                    any(
                        f"materiality_assessment.{topic}.observation" in item
                        and "numeric_claims" in item
                        for item in problems
                    )
                )

    def test_v7_rejects_normalized_equal_observation_and_inference_pairs(self):
        cases = (
            ("risks", epistemic_risk(), "sourced_observation", "inference"),
            ("catalysts", epistemic_catalyst(), "trigger", "expected_outcome"),
        )
        for collection, row, left, right in cases:
            with self.subTest(collection=collection):
                row[right] = f"  {row[left].swapcase()}  "
                payload = self._payload()
                payload[collection] = [row]
                problems = service.validate_investment_report_payload(payload)
                self.assertEqual(len(problems), 1)
                self.assertIn(f"$.{collection}[0]", problems[0])
                self.assertIn("must differ", problems[0])

    def test_epistemic_evidence_grounds_only_observation_or_trigger(self):
        payload = self._payload()
        risk = epistemic_risk()
        risk["inference"] = "A novel inference absent from the source"
        catalyst = epistemic_catalyst()
        catalyst["expected_outcome"] = "A distinct outcome absent from the source"
        payload["risks"] = [risk]
        payload["catalysts"] = [catalyst]
        self.assertEqual(
            service.investment_evidence_violations(
                payload,
                excerpt="Demand remained durable through the period.",
                news_items=[],
            ),
            [],
        )
        risk["evidence"] = risk["inference"]
        violations = service.investment_evidence_violations(
            payload,
            excerpt="Demand remained durable through the period.",
            news_items=[],
        )
        self.assertEqual(len(violations), 1)
        self.assertIn("risks[0]", violations[0])

    def test_evidence_requires_filing_text_not_news(self):
        excerpt = "Demand remained durable through the quarter."
        self.assertEqual(
            service.investment_evidence_violations(
                self._payload(), excerpt=excerpt, news_items=[]
            ),
            [],
        )
        invented = self._payload()
        invented["qualitative"]["ai_demand"]["evidence"] = "Margins expanded sharply"
        invented_violations = service.investment_evidence_violations(
            invented, excerpt=excerpt, news_items=[]
        )
        self.assertEqual(len(invented_violations), 1)
        self.assertIn("qualitative.ai_demand", invented_violations[0])
        self.assertIn("not grounded", invented_violations[0])
        news_only_violations = service.investment_evidence_violations(
            self._payload(),
            excerpt="Backlog eased modestly during the quarter.",
            news_items=[{"title": "Wire", "summary": "Demand remained durable"}],
        )
        self.assertEqual(len(news_only_violations), 1)
        self.assertIn("qualitative.ai_demand", news_only_violations[0])

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
        present_blank["qualitative"]["ai_demand"]["evidence"] = "   "
        blank_violations = service.investment_evidence_violations(
            present_blank, excerpt="Demand remained durable.", news_items=[]
        )
        self.assertEqual(len(blank_violations), 1)
        self.assertIn("qualitative.ai_demand", blank_violations[0])
        self.assertIn("must be nonblank", blank_violations[0])

    def test_evidence_rejects_scaffold_headers_and_cross_region_quotes(self):
        excerpt = (
            "[Source characters 0-40]\nDemand remained durable in the quarter.\n"
            "[Source characters 41-90]\nBacklog eased while pricing held firm."
        )
        self.assertEqual(
            service.investment_evidence_violations(
                self._payload(), excerpt=excerpt, news_items=[]
            ),
            [],
        )
        header_quote = self._payload()
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
        cross_region = (
            "in the quarter.\n[Source characters 41-90]\nBacklog eased"
        )
        cross_payload = self._payload()
        cross_payload["qualitative"]["ai_demand"]["evidence"] = cross_region
        self.assertEqual(
            len(
                service.investment_evidence_violations(
                    cross_payload, excerpt=excerpt, news_items=[]
                )
            ),
            1,
        )
        single_span_excerpt = (
            "Demand remained durable in the quarter. Backlog eased "
            "while pricing held firm."
        )
        stitched_payload = self._payload()
        stitched_payload["qualitative"]["ai_demand"]["evidence"] = (
            "in the quarter. Backlog eased"
        )
        self.assertEqual(
            service.investment_evidence_violations(
                stitched_payload, excerpt=single_span_excerpt, news_items=[]
            ),
            [],
        )

    def test_finalize_investment_analysis_is_pure_over_all_inputs(self):
        facts = self._payload()
        facts["metrics"] = {"revenue": metric(1_000)}
        facts["prior_metrics"] = {"revenue": metric(900, period="FY2024")}
        facts["relationship_reconciliations"] = [
            {
                "relationship_id": "rel-id",
                "status": "reconciled",
                "fact_paths": ["deterministic_current.relationship_facts.fact-id"],
                "observation": "Cash generation exceeded investment",
                "interpretation": "Cash generation covered investment",
                "uncertainty": "Measurement bases may differ",
                "summary_synthesis": "Cash generation covered investment.",
                "thesis_synthesis": "Internal funding supported reinvestment.",
                "summary_fact_paths": [
                    "deterministic_current.relationship_facts.fact-id"
                ],
            }
        ]
        document = {
            "company": "Example Company",
            "symbol": "EX",
            "region": "US",
            "industry": "Industrial Technology",
            "document_type": "annual_report",
        }
        deterministic_current = {
            "revenue": metric(1_100),
            "operating_cash_flow": metric(120),
            "capex": metric(20),
        }
        deterministic_prior = {"revenue": metric(900, period="FY2024")}
        market_inputs = {"market_price": 50}
        stored_previous_facts = {"metrics": {}, "qualitative": {}}
        news_items = [{"source": "Reuters", "title": "AI demand"}]
        extraction = {"report_text_source": "stored_document"}
        relationship_facts = {"fact-id": {"value": 42}}
        material_relationships = [
            {"relationship_id": "rel-id", "compatibility": "compatible"}
        ]
        inputs = copy.deepcopy(
            [
                facts,
                document,
                deterministic_current,
                deterministic_prior,
                market_inputs,
                stored_previous_facts,
                news_items,
                extraction,
                relationship_facts,
                material_relationships,
            ]
        )
        result = service.finalize_investment_analysis(
            facts,
            document=document,
            deterministic_current=deterministic_current,
            deterministic_prior=deterministic_prior,
            market_inputs=market_inputs,
            stored_previous_facts=stored_previous_facts,
            previous_state=None,
            prior_count=0,
            news_items=news_items,
            extraction=extraction,
            relationship_facts=relationship_facts,
            material_relationships=material_relationships,
        )
        self.assertEqual(
            [
                facts,
                document,
                deterministic_current,
                deterministic_prior,
                market_inputs,
                stored_previous_facts,
                news_items,
                extraction,
                relationship_facts,
                material_relationships,
            ],
            inputs,
        )
        for mutation in (
            (facts, ["summary"], "mutated"),
            (facts["qualitative"]["ai_demand"], ["strength"], "weak"),
            (document, ["industry"], "Consumer"),
            (deterministic_current["revenue"], ["value"], 0),
            (news_items[0], ["title"], "changed"),
            (extraction, ["report_text_source"], "changed"),
            (relationship_facts["fact-id"], ["value"], 0),
            (material_relationships[0], ["compatibility"], "changed"),
            (
                facts["relationship_reconciliations"][0],
                ["observation"],
                "changed",
            ),
        ):
            node, path, value = mutation
            node[path[0]] = value
        self.assertNotEqual(result.facts["summary"], "mutated")
        self.assertNotEqual(result.facts["qualitative"]["ai_demand"]["strength"], "weak")
        self.assertNotEqual(result.analysis["classification"]["industry"], "Consumer")
        self.assertNotEqual(
            result.analysis["metrics"]["revenue"]["value"], 0
        )
        self.assertNotEqual(result.analysis["news_context"][0]["title"], "changed")
        self.assertNotEqual(result.analysis["extraction"]["report_text_source"], "changed")
        self.assertEqual(result.facts["relationship_facts"]["fact-id"]["value"], 42)
        self.assertEqual(
            result.analysis["material_relationships"][0]["compatibility"],
            "compatible",
        )
        self.assertEqual(
            result.analysis["relationship_reconciliations"][0]["observation"],
            "Cash generation exceeded investment",
        )

    def test_exact_typography_quotes_ground_but_edits_do_not(self):
        filing_text = (
            "Management\u2019s \u201Cdemand remained durable\u201D \u2014 "
            "backlog\u00a0held."
        )
        exact_typographic_evidence = (
            "Management\u2019s \u201Cdemand remained durable\u201D \u2014 "
            "backlog\u00a0held"
        )
        source = self._payload()
        source["qualitative"]["ai_demand"]["evidence"] = exact_typographic_evidence
        self.assertEqual(
            service.investment_evidence_violations(
                source, excerpt=filing_text, news_items=[]
            ),
            [],
        )
        ascii_filing = (
            "management's \"demand remained durable\" - backlog held"
        )
        exact_ascii_evidence = (
            "management's \"demand remained durable\" - backlog held"
        )
        reverse = self._payload()
        reverse["qualitative"]["ai_demand"]["evidence"] = exact_ascii_evidence
        self.assertEqual(
            service.investment_evidence_violations(
                reverse, excerpt=ascii_filing, news_items=[]
            ),
            [],
        )
        # Negative controls: every content edit must still fail.
        negatives = {
            "omitted word": "management's demand remained durable",
            "changed digit": "management's \"demand remained 2023\" - backlog held",
            "reordered words": "\"durable remained demand\" management's - held backlog",
            "removed meaningful punctuation": (
                "management's demand remained durable - backlog held"
            ),
            "accent change": (
                "managements \"demand remained durable\" - backlog held"
            ),
        }
        for label, evidence in negatives.items():
            with self.subTest(negative=label):
                payload = self._payload()
                payload["qualitative"]["ai_demand"]["evidence"] = evidence
                violations = service.investment_evidence_violations(
                    payload, excerpt=filing_text, news_items=[]
                )
                self.assertEqual(len(violations), 1)
                self.assertIn("not grounded", violations[0])

    def test_base_prompt_requires_target_local_numeric_semantics(self):
        prompt = service.build_investment_analysis_request(
            {
                "company": "Example Company",
                "document_id": "doc-1",
                "title": "Fourth Quarter Results",
                "report_date": "2024-06-30",
            },
            "Revenue increased 12% year over year to $10 million in FY2024 Q4.",
            [],
            {},
            {},
        ).prompt.casefold()
        semantic_requirements = (
            (
                "every material numeral",
                "locally state",
                "metric",
                "value",
                "unit or currency",
                "exact period",
            ),
            ("growth or change", "comparison basis"),
            (
                "source metadata",
                "numeric_claims` rows",
                "cannot silently supply target semantics omitted from the prose",
            ),
            ("required_fact", "atomic clause", "locally repeat"),
        )
        for requirement in semantic_requirements:
            with self.subTest(requirement=requirement):
                self.assertTrue(
                    all(term in prompt for term in requirement),
                    f"base prompt omitted target-local semantics: {requirement}",
                )

    def test_base_prompt_states_same_source_period_precedence_contract(self):
        prompt = service.build_investment_analysis_request(
            {
                "company": "Example Company",
                "document_id": "doc-1",
                "title": "Fourth Quarter Results",
                "report_date": "2024-06-30",
            },
            "Revenue was $10 million.",
            [
                {
                    "title": "Company reports fourth quarter results",
                    "published_at": "2024-07-01",
                }
            ],
            {},
            {},
        ).prompt
        self.assertIn(
            'For a `source_kind="text"` row, an explicit fiscal, calendar, '
            "relative, prior, or forward period in the quote is authoritative "
            "and must match `period`; metadata cannot override it",
            prompt,
        )
        self.assertIn(
            "Only when the quote is period-silent may `period` use context "
            "from that exact source",
            prompt,
        )
        self.assertIn(
            "the document `title` with exact `report_date` for the FILING "
            "EXCERPT",
            prompt,
        )
        self.assertIn(
            "the matched news item's own title/headline with its own date fields",
            prompt,
        )
        self.assertIn(
            "Never infer a fiscal quarter from a date alone, invent a period, "
            "or borrow one from another source",
            prompt,
        )

    def test_base_prompt_preserves_quotes_and_labels_and_bounds_numeric_ledger(self):
        prompt = service.build_investment_analysis_request(
            {"company": "Example Company", "document_id": "doc-1"},
            "Demand remained durable.",
            [],
            {},
            {},
        ).prompt
        self.assertIn(
            "one short contiguous substring copied exactly", prompt
        )
        self.assertIn("single region of the FILING EXCERPT", prompt)
        self.assertIn("never combine text from multiple regions", prompt)
        self.assertIn("never append commentary", prompt)
        self.assertIn(
            "Preserve the filing's fiscal/time label exactly; expand it to "
            "calendar dates or months only when explicit deterministic "
            "fiscal-calendar metadata provides the expansion.",
            prompt,
        )
        self.assertIn("Fiscal/calendar labels", prompt)
        self.assertIn("period metadata, not quantitative coefficients", prompt)
        for temporal_label in ("`H2`", "`FY25`", "`Q4 FY25`", "valid date"):
            with self.subTest(temporal_label=temporal_label):
                self.assertIn(temporal_label, prompt)
        self.assertIn(
            "Do not create a `numeric_claims` row solely for digits embedded "
            "in such a label",
            prompt,
        )
        self.assertIn("Actual quantities", prompt)
        self.assertIn("remain material and require rows", prompt)
        self.assertIn("including when mixed with a period label", prompt)
        for actual_quantity in (
            "`29%`",
            "`$2B`",
            "`2%`",
            "`2–3 quarters`",
            "`2025%`",
            "`$2,025 million`",
        ):
            with self.subTest(actual_quantity=actual_quantity):
                self.assertIn(actual_quantity, prompt)

    def test_base_prompt_requires_epistemic_separation_and_qualifier_preservation(self):
        prompt = service.build_investment_analysis_request(
            {"company": "Example Company", "document_id": "doc-1"},
            "Demand remained durable.",
            [],
            {},
            {},
        ).prompt
        for requirement in (
            "Separate company-stated facts from your interpretation",
            "Preserve every fact qualifier",
            "do not blend sourced observations with interpretations",
            "Evidence grounds only sourced observations and catalyst triggers",
            "does not prove interpretations or outcomes",
            "sourced_observation",
            "inference",
            "epistemic_state",
            "uncertainty",
            "trigger",
            "expected_outcome",
            "preserve the source's material scope, basis, hedges, conditions, exclusions, and time qualifiers",
            "Planned spending, capacity, hiring, or another input alone is not a catalyst",
            "Without a supported thesis-moving outcome, place it in `watch_items`",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, prompt)

    def test_v7_prompt_requires_decision_relevant_materiality_assessment(self):
        prompt = service.build_investment_analysis_request(
            {"company": "Example Company", "document_id": "doc-1"},
            (
                "Management guided revenue to $10 million in FY2026 and "
                "explained the reported margin variance and investment duration."
            ),
            [],
            {},
            {},
        ).prompt.casefold()
        requirements = (
            (
                "forward_guidance",
                "explicit",
                "quantified",
                "guidance",
                "period",
            ),
            (
                "reported_variance_driver",
                "reported",
                "variance",
                "explanation",
            ),
            (
                "margin_economics",
                "price",
                "mix",
                "cost",
                "margin",
            ),
            (
                "capital_commitment_duration",
                "one-time",
                "multi-period",
                "recurring",
            ),
            (
                "counter_thesis",
                "nonblank",
                "invalidate",
            ),
            (
                "addressed",
                "source",
                "not_disclosed",
                "empty",
            ),
            (
                "thesis",
                "counter_thesis",
                "risks",
                "catalysts",
                "where supported",
            ),
            (
                "numeric materiality observations",
                "numeric_claims",
                "exact target",
            ),
        )
        for requirement in requirements:
            with self.subTest(requirement=requirement):
                self.assertTrue(
                    all(term in prompt for term in requirement),
                    f"base prompt omitted materiality semantics: {requirement}",
                )

    def test_base_prompt_requires_exact_relationship_reconciliation(self):
        prompt = service.build_investment_analysis_request(
            {"company": "Example Company", "document_id": "doc-1"},
            "Demand remained durable.",
            [],
            {},
            {},
        ).prompt.casefold()
        semantic_requirements = (
            (
                "every supplied material relationship",
                "once",
                "given order",
                "relationship id",
                "required fact paths",
            ),
            (
                "compatible relationship",
                "reconciled",
                "observation",
                "interpretation",
                "uncertainty",
                "summary",
                "thesis",
            ),
            (
                "incompatible relationship",
                "abstained_incompatible",
                "interpretation empty",
            ),
        )
        for requirement in semantic_requirements:
            with self.subTest(requirement=requirement):
                self.assertTrue(
                    all(term in prompt for term in requirement),
                    f"base prompt omitted relationship semantics: {requirement}",
                )

    def test_prompt_and_schema_repair_require_exact_normalized_relationship_fact_rows(
        self,
    ):
        prompt = service.build_investment_analysis_request(
            {"company": "Example Company", "document_id": "doc-1"},
            "Capital expenditures were $13.9 billion.",
            [],
            {},
            {},
        ).prompt.casefold()
        repair = service._CORRECTION_REQUIREMENTS[
            service.VALIDATION_JSON_SCHEMA
        ]
        repair_folded = repair.casefold()
        compact_repair = service._COMPACT_CORRECTION_REQUIREMENTS[
            service.VALIDATION_JSON_SCHEMA
        ].casefold()
        repair_semantic_surface = (
            repair_folded.replace("no _", "no underscores")
            .replace("percent=%", "percent as %")
            .replace("professional currency", "formatted currency")
        )
        prompt_semantics = (
            (
                "compatible relationship",
                "required_fact",
                "required_facts` order",
                "atomic clause",
                "locally repeat",
                "metric",
                "rendered value",
                "unit or currency",
                "exact period",
                "comparison basis",
            ),
            (
                "exact enum copying",
                "ledger fields",
                "not target prose",
                "no underscores",
                "percent",
                "%",
                "percentage_points",
                "percentage points",
                "professional currency",
            ),
            (
                "exact `fact_path`",
                "metric_label",
                "period",
                "unit",
                "currency",
                "verbatim into `metric`",
            ),
            (
                "every unique material numeric binding",
                "same fact/value binding",
                "one target leaf",
                "share one row",
                "distinct target paths",
                "distinct rows",
            ),
            (
                "finite numeric scalar",
                "compact numeric token",
                "64 characters",
                "never explanatory prose",
            ),
        )
        repair_semantics = (
            ("correction", "json", "relationships", "exact", "order"),
            (
                "required facts",
                "exact request order",
                "atomic",
                "clause",
                "metric",
                "rendered value/unit",
                "exact period",
                "comparison basis",
            ),
            (
                "exact direct fact_path",
                "metric_label",
                "period",
                "unit",
                "currency",
            ),
            (
                "no underscores",
                "distinguish %",
                "percentage points",
                "format currency",
            ),
            (
                "one fact row per observation target/semantic binding",
                "exact summary repeats share one",
            ),
            (
                "finite scalar",
                "numeric token",
                "64 chars",
                "never prose",
            ),
        )
        compact_repair_semantics = (
            (
                "exact ordered relationships/facts",
                "one fact row/observation",
                "duplicate summary binding shares row",
            ),
        )
        for surface_name, surface, requirements in (
            ("base prompt", prompt, prompt_semantics),
            ("sole repair", repair_semantic_surface, repair_semantics),
            ("compact repair", compact_repair, compact_repair_semantics),
        ):
            for requirement in requirements:
                with self.subTest(surface=surface_name, requirement=requirement):
                    self.assertTrue(
                        all(term in surface for term in requirement),
                        f"{surface_name} omitted fact-row semantics: {requirement}",
                    )
        self.assertLess(len(f"\n{repair}"), 700)

    def test_relationship_binding_hint_survives_earlier_problem_truncation(self):
        raw_markers = [
            f"RAW_MODEL_VALUE_{index} PRIVATE_CLAIM_{index}"
            for index in range(12)
        ]
        error = service.InvestmentValidationError(
            service.VALIDATION_JSON_SCHEMA,
            [
                *(
                    f"schema[{index}]: {marker}"
                    for index, marker in enumerate(raw_markers)
                ),
                (
                    "relationship_reconciliations[0].observation requires one "
                    "exact numeric_claims fact binding"
                ),
            ],
            missing_relationship_bindings=[
                (0, 0),
                (0, 1),
                (0, 1),
                (1, 0),
            ],
        )

        # Detailed diagnostics are capped, but the request-owned repair state is
        # independent of that cap and remains ordered and deduplicated.
        self.assertEqual(len(error.problems), 10)
        requirement = error.correction_requirement
        tokens = ("r0/f0", "r0/f1", "r1/f0")
        positions = [requirement.index(token) for token in tokens]
        self.assertEqual(positions, sorted(positions))
        for token in tokens:
            self.assertEqual(requirement.count(token), 1)
        folded = requirement.casefold()
        for semantics in (
            ("rn/fn", "observation"),
            ("exact", "fact", "row"),
            ("metric_label", "exact"),
        ):
            self.assertTrue(
                all(term in folded for term in semantics),
                f"relationship repair omitted semantics: {semantics}",
            )
        for marker in raw_markers:
            self.assertNotIn(marker, requirement)
        self.assertNotIn("PRIVATE_CLAIM", requirement)
        self.assertLess(len(f"\n{requirement}"), 700)

    def test_relationship_binding_hint_retains_complete_maximum_pair_set(self):
        pairs = [
            (relationship, fact)
            for relationship in range(3)
            for fact in range(8)
        ]
        requirement = service.InvestmentValidationError(
            service.VALIDATION_JSON_SCHEMA,
            ["RAW_RESPONSE_WITH_VALUE_987_AND_CLAIM_ID"],
            missing_relationship_bindings=[*pairs, (0, 0), (2, 7)],
        ).correction_requirement

        tokens = [f"r{relationship}/f{fact}" for relationship, fact in pairs]
        positions = [requirement.index(token) for token in tokens]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(
            requirement.rsplit("\n", 1)[-1],
            "".join(tokens),
        )
        for token in tokens:
            self.assertEqual(requirement.count(token), 1)
        suffix = f"\n{requirement}"
        self.assertTrue(suffix.startswith("\nCORRECTION:"))
        self.assertLess(len(suffix), 700)
        self.assertNotIn("987", requirement)
        self.assertNotIn("CLAIM_ID", requirement)
        self.assertNotIn("RAW_RESPONSE", requirement)

    def test_all_category_worst_case_correction_is_complete_and_bounded(self):
        pairs = [
            (relationship, fact)
            for relationship in range(3)
            for fact in range(8)
        ]
        problems_by_category = {
            service.VALIDATION_JSON_SCHEMA: ["schema failure"],
            service.VALIDATION_FILING_EVIDENCE: ["evidence failure"],
            service.VALIDATION_PROHIBITED_LANGUAGE: ["language failure"],
        }
        requirement = service.InvestmentValidationError(
            service.VALIDATION_JSON_SCHEMA,
            problems_by_category[service.VALIDATION_JSON_SCHEMA],
            problems_by_category=problems_by_category,
            missing_relationship_bindings=pairs,
        ).correction_requirement

        tokens = [f"r{relationship}/f{fact}" for relationship, fact in pairs]
        self.assertEqual(
            requirement.rsplit("\n", 1)[-1],
            "".join(tokens),
        )
        folded = requirement.casefold()
        for category_semantics in (
            ("correction", "json", "exact ordered relationships/facts", "atomic"),
            ("evidence", "quote"),
            (
                "language",
                "portfolio",
                "trading",
                "technical",
                "execution",
            ),
            (
                "prose",
                "no underscores",
                "%",
                "percentage points",
                "formatted currency",
            ),
        ):
            with self.subTest(category_semantics=category_semantics):
                self.assertTrue(
                    all(term in folded for term in category_semantics),
                    requirement,
                )
        suffix = f"\n{requirement}"
        self.assertTrue(suffix.startswith("\nCORRECTION:"))
        self.assertLess(len(suffix), 700)

    def test_sole_evidence_correction_preserves_legacy_text_exactly(self):
        expected = (
            "CORRECTION: The previous response had blank or ungrounded filing "
            "evidence. Each present qualitative signal/risk "
            "sourced_observation/catalyst trigger needs nonblank evidence: one "
            "short exact contiguous FILING EXCERPT quote in a single source "
            "region. Never join regions; no '[Source characters ...]' "
            "metadata, labels/wrappers/scaffolds absent from source, or "
            "commentary. News stays item-bound, not filing evidence. Evidence "
            "supports observation/trigger, not inference/outcome. Preserve the "
            "filing's fiscal/time label exactly; expand it to calendar dates or "
            "months only when explicit deterministic fiscal-calendar metadata "
            "provides the expansion."
        )
        requirement = service.InvestmentValidationError(
            service.VALIDATION_FILING_EVIDENCE,
            ["evidence failure"],
        ).correction_requirement

        self.assertEqual(requirement, expected)
        self.assertLess(len(f"\n{requirement}"), 700)

    def test_reused_fact_hint_identifies_only_the_missing_target_pair(self):
        request = service.build_investment_analysis_request(
            {"company": "Example Company", "document_id": "doc-1"},
            "Operating cash flow was $42 million in FY2025.",
            [],
            {
                "operating_cash_flow": relationship_metric(
                    42,
                    role="cash_generation",
                    metric_family="operating_cash_flow",
                    cash_basis="cash",
                ),
                "capital_expenditures": relationship_metric(
                    18,
                    role="cash_investment",
                    metric_family="capital_investment",
                    cash_basis="cash",
                ),
            },
            {},
        )
        source_relationship = service._plain_json_value(
            request.material_relationships[0]
        )
        shared_ref = source_relationship["required_facts"][0]
        relationships = [
            {
                **source_relationship,
                "relationship_id": f"reused-target-{index}",
                "required_facts": [shared_ref],
            }
            for index in range(2)
        ]
        relationship_facts = service._plain_json_value(request.relationship_facts)
        fact = relationship_facts[shared_ref["fact_path"].rsplit(".", 1)[-1]]
        target_metric = fact["metric_label"].replace("_", " ")
        target_value = fact["value"]
        target_period = fact["period"]
        payload = investment_report_payload()
        payload["summary"] = (
            f"Operating cash flow was ${target_value} million in "
            f"{target_period}, supporting both targets."
        )
        payload["thesis"] = "Cash generation supports both target outcomes."
        target_names = ("First", "Second")
        payload["relationship_reconciliations"] = [
            {
                "relationship_id": relationship["relationship_id"],
                "status": "reconciled",
                "fact_paths": [shared_ref["fact_path"]],
                "observation": (
                    f"{target_names[index]} target {target_metric} was "
                    f"${target_value} million in {target_period}"
                ),
                "interpretation": f"{target_names[index]} target interpretation",
                "uncertainty": f"{target_names[index]} target uncertainty",
                "summary_synthesis": payload["summary"],
                "thesis_synthesis": payload["thesis"],
                "summary_fact_paths": [shared_ref["fact_path"]],
            }
            for index, relationship in enumerate(relationships)
        ]
        payload["numeric_claims"] = [
            {
                "claim_id": "bound-first-target",
                "path": "relationship_reconciliations[0].observation",
                "value": fact["value"],
                "metric": fact["metric_label"],
                "period": fact["period"],
                "unit": fact["unit"],
                "currency": fact.get("currency"),
                "source_kind": "fact",
                "fact_path": shared_ref["fact_path"],
            },
            {
                "claim_id": "shared-summary-fact",
                "path": "summary",
                "value": fact["value"],
                "metric": fact["metric_label"],
                "period": fact["period"],
                "unit": fact["unit"],
                "currency": fact.get("currency"),
                "source_kind": "fact",
                "fact_path": shared_ref["fact_path"],
            },
        ]
        missing_bindings = []
        problems = service.numeric_claim_source_problems(
            payload,
            deterministic_current={},
            deterministic_prior={},
            relationship_facts=relationship_facts,
            material_relationships=relationships,
            missing_relationship_bindings=missing_bindings,
        )

        self.assertEqual(missing_bindings, [(1, 0)])
        requirement = service.InvestmentValidationError(
            service.VALIDATION_JSON_SCHEMA,
            problems,
            missing_relationship_bindings=missing_bindings,
        ).correction_requirement
        self.assertIn("r1/f0", requirement)
        self.assertNotIn("r0/f0", requirement)
        self.assertEqual(requirement.count("r1/f0"), 1)
        self.assertIn("row/observation", requirement)
        self.assertLess(len(f"\n{requirement}"), 700)

    def test_correction_requirement_lists_safe_paths_only(self):
        requirement = service.InvestmentValidationError(
            service.VALIDATION_FILING_EVIDENCE,
            [
                "catalysts[2]: evidence is not grounded in the filing excerpt",
                "risks[0]: evidence is required and must be nonblank",
                (
                    "qualitative.ai_demand: evidence is not grounded in the "
                    "filing excerpt"
                ),
                # Duplicates and unsafe/free-form paths are dropped.
                "catalysts[2]: evidence is not grounded in the filing excerpt",
                "metrics.revenue: evidence is not grounded",
                "summary: ungrounded claim 'Margins expanded sharply'",
            ],
        ).correction_requirement
        self.assertIn("Affected fields: ", requirement)
        affected = requirement.partition("Affected fields: ")[2].rstrip(".")
        self.assertEqual(affected, "catalysts[2], risks[0]")
        omitted_safe_path = "qualitative.ai_demand"
        self.assertNotIn(omitted_safe_path, requirement)
        self.assertLess(len(f"\n{requirement}"), 700)
        self.assertGreaterEqual(
            len(f"\n{requirement[:-1]}, {omitted_safe_path}."),
            700,
        )
        # Unsafe paths, violation messages, and quoted raw evidence never ride along.
        self.assertNotIn("metrics.revenue", requirement)
        self.assertNotIn("Margins expanded sharply", requirement)
        self.assertNotIn("not grounded in the filing excerpt:", requirement)
        # The bounded base requirement text is preserved verbatim.
        self.assertTrue(
            requirement.startswith(
                "CORRECTION: The previous response had blank or ungrounded "
                "filing evidence."
            )
        )

    def test_retry_correction_prompt_states_bounded_requirement_only(self):
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
            "extracted_text": (
                "Annual report evidence: AI demand accelerated, data-centre "
                "deployments expanded despite tight supply, higher pricing held, "
                "guidance was raised, capex grew, and capacity additions continue. "
                "Demand remained durable."
            ),
        }
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
        stage.policy = SimpleNamespace(model="openai/gpt-5.6-luna", validation_retries=1)
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
        suffix = repair_prompt[len(base_prompt) :]
        self.assertIn("CORRECTION:", suffix)
        self.assertIn("single source region", suffix)
        self.assertIn("nonblank evidence", suffix)
        self.assertIn("[Source characters ...]", suffix)
        self.assertIn("FILING EXCERPT", suffix)
        self.assertLess(len(suffix), 700)
        # Raw model output must never leak into the repair prompt.
        self.assertNotIn("Margins expanded sharply", repair_prompt)
        self.assertEqual(
            stage.add_validation_warnings.call_args.args[0],
            ["filing evidence was blank or ungrounded"],
        )


if __name__ == '__main__':
    unittest.main()
