"""Company hard-gate coverage: evidence grounding, numeric support,
hindsight/prohibited language, pairing, and deterministic-check ledgers.

The report itself carries the required SHA-256 ``producer_fingerprint``
derived from the producer case: identity is never caller-injected, malformed
identities fail closed at construction, and every run stamps its own
producer's fingerprint into the returned report.
"""

import copy
import json
import sys
import tempfile
import unittest
from dataclasses import replace as dataclass_replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from company_quality_support import (
    ARITHMETIC_METRICS,
    ARITHMETIC_ROW,
    EXCERPT,
    NEWS_ITEM,
    epistemic_catalyst,
    evaluator_raw,
    finalized_for,
    ledger_row,
    narrative_payload,
    nonblank_ledger_row,
    producer_raw,
    write_yaml,
)

import investment_service as service
from research_intelligence import company_benchmarks as cb
from research_intelligence import company_quality as cq


class RelationshipReconciliationHardGateTests(unittest.TestCase):
    def setUp(self):
        self.relationships = (
            {
                "relationship_id": "mr_growth",
                "compatibility": "compatible",
                "required_facts": (
                    {"fact_path": "deterministic_current.relationship_facts.rf_revenue"},
                    {"fact_path": "deterministic_current.relationship_facts.rf_profit"},
                ),
            },
            {
                "relationship_id": "mr_cash",
                "compatibility": "incompatible",
                "required_facts": (
                    {"fact_path": "deterministic_current.relationship_facts.rf_cash"},
                    {"fact_path": "deterministic_current.relationship_facts.rf_spending"},
                ),
            },
        )
        self.payload = {
            "summary": "Revenue growth did not translate into profit growth.",
            "thesis": "The divergence points to margin pressure.",
            "relationship_reconciliations": [
                {
                    "relationship_id": "mr_growth",
                    "status": "reconciled",
                    "fact_paths": [
                        "deterministic_current.relationship_facts.rf_revenue",
                        "deterministic_current.relationship_facts.rf_profit",
                    ],
                    "observation": "Revenue grew while profit lagged.",
                    "interpretation": "This suggests margin pressure.",
                    "uncertainty": "Growth linkage remains uncertain.",
                    "summary_synthesis": (
                        "Revenue growth did not translate into profit growth."
                    ),
                    "thesis_synthesis": (
                        "The divergence points to margin pressure."
                    ),
                    "summary_fact_paths": [
                        "deterministic_current.relationship_facts.rf_revenue"
                    ],
                },
                {
                    "relationship_id": "mr_cash",
                    "status": "abstained_incompatible",
                    "fact_paths": [
                        "deterministic_current.relationship_facts.rf_cash",
                        "deterministic_current.relationship_facts.rf_spending",
                    ],
                    "observation": "Cash bases cannot be compared.",
                    "interpretation": "",
                    "uncertainty": "Cash basis mismatch remains uncertain.",
                    "summary_synthesis": "",
                    "thesis_synthesis": "",
                    "summary_fact_paths": [],
                },
            ],
        }

    def _problems(self, payload):
        return service.relationship_reconciliation_problems(
            payload,
            material_relationships=self.relationships,
        )

    def test_exact_ordered_bijection_and_concise_synthesis_passes(self):
        payload = copy.deepcopy(self.payload)
        self.assertEqual(self._problems(payload), [])
        self.assertNotIn(
            payload["relationship_reconciliations"][0]["observation"],
            payload["summary"],
        )
        self.assertNotIn(
            payload["relationship_reconciliations"][0]["interpretation"],
            payload["thesis"],
        )
        self.assertNotIn(
            payload["relationship_reconciliations"][0]["uncertainty"],
            payload["summary"],
        )

    def test_response_validation_hard_gate_rejects_every_bijection_forgery(self):
        base = narrative_payload(
            summary=self.payload["summary"],
            thesis=self.payload["thesis"],
        )
        base["relationship_reconciliations"] = copy.deepcopy(
            self.payload["relationship_reconciliations"]
        )

        missing = copy.deepcopy(base)
        missing["relationship_reconciliations"] = []
        partial = copy.deepcopy(base)
        partial["relationship_reconciliations"] = partial[
            "relationship_reconciliations"
        ][:1]
        forged = copy.deepcopy(base)
        forged["relationship_reconciliations"][0]["relationship_id"] = "mr_forged"
        duplicate = copy.deepcopy(base)
        duplicate["relationship_reconciliations"][1] = copy.deepcopy(
            duplicate["relationship_reconciliations"][0]
        )
        false_abstention = copy.deepcopy(base)
        false_abstention["relationship_reconciliations"][0]["status"] = (
            "abstained_incompatible"
        )

        cases = (
            ("missing", missing, "expected exactly 2 ordered rows"),
            ("partial", partial, "expected exactly 2 ordered rows"),
            ("forged", forged, "must equal request relationship 'mr_growth'"),
            ("duplicate", duplicate, "must equal request relationship 'mr_cash'"),
            (
                "false abstention",
                false_abstention,
                "must be 'reconciled' for request compatibility 'compatible'",
            ),
        )
        for label, payload, expected in cases:
            with self.subTest(case=label):
                with self.assertRaises(service.InvestmentValidationError) as raised:
                    service._validated_investment_facts(
                        json.dumps(payload),
                        excerpt=EXCERPT,
                        news_items=(NEWS_ITEM,),
                        material_relationships=self.relationships,
                        relationship_facts={},
                    )
                self.assertEqual(
                    raised.exception.category,
                    service.VALIDATION_JSON_SCHEMA,
                )
                self.assertIn(expected, " ".join(raised.exception.problems))

    def test_missing_and_partial_reconciliations_fail_closed(self):
        for rows, expected_count in (([], 0), (self.payload["relationship_reconciliations"][:1], 1)):
            with self.subTest(row_count=expected_count):
                payload = copy.deepcopy(self.payload)
                payload["relationship_reconciliations"] = copy.deepcopy(rows)
                self.assertIn(
                    "relationship_reconciliations: expected exactly 2 ordered rows from the request contract",
                    self._problems(payload),
                )

    def test_forged_id_and_partial_fact_paths_are_rejected(self):
        payload = copy.deepcopy(self.payload)
        payload["relationship_reconciliations"][0]["relationship_id"] = "mr_forged"
        payload["relationship_reconciliations"][0]["fact_paths"].pop()
        self.assertEqual(
            self._problems(payload),
            [
                "relationship_reconciliations[0].relationship_id: must equal request relationship 'mr_growth' at this position",
                "relationship_reconciliations[0].fact_paths: must equal the complete ordered request fact path list",
            ],
        )

    def test_duplicate_relationship_row_is_rejected_at_its_ordered_position(self):
        payload = copy.deepcopy(self.payload)
        payload["relationship_reconciliations"][1] = copy.deepcopy(
            payload["relationship_reconciliations"][0]
        )
        problems = self._problems(payload)
        self.assertIn(
            "relationship_reconciliations[1].relationship_id: must equal request relationship 'mr_cash' at this position",
            problems,
        )
        self.assertIn(
            "relationship_reconciliations[1].fact_paths: must equal the complete ordered request fact path list",
            problems,
        )

    def test_compatible_relationship_cannot_falsely_abstain(self):
        payload = copy.deepcopy(self.payload)
        payload["relationship_reconciliations"][0]["status"] = (
            "abstained_incompatible"
        )
        self.assertIn(
            (
                "relationship_reconciliations[0].status: must be 'reconciled' "
                "for request compatibility 'compatible'"
            ),
            self._problems(payload),
        )

    def test_selected_summary_facts_are_a_unique_one_or_two_fact_subset(self):
        cases = {}
        empty = copy.deepcopy(self.payload)
        empty["relationship_reconciliations"][0]["summary_fact_paths"] = []
        cases["empty"] = empty
        duplicate = copy.deepcopy(self.payload)
        duplicate["relationship_reconciliations"][0]["summary_fact_paths"] = [
            "deterministic_current.relationship_facts.rf_revenue",
            "deterministic_current.relationship_facts.rf_revenue",
        ]
        cases["duplicate"] = duplicate
        foreign = copy.deepcopy(self.payload)
        foreign["relationship_reconciliations"][0]["summary_fact_paths"] = [
            "deterministic_current.relationship_facts.rf_cash"
        ]
        cases["foreign"] = foreign
        too_many = copy.deepcopy(self.payload)
        too_many["relationship_reconciliations"][0]["summary_fact_paths"] = [
            "deterministic_current.relationship_facts.rf_revenue",
            "deterministic_current.relationship_facts.rf_profit",
            "deterministic_current.relationship_facts.rf_cash",
        ]
        cases["too many"] = too_many
        for label, payload in cases.items():
            with self.subTest(case=label):
                self.assertTrue(self._problems(payload), f"{label} must fail closed")

    def test_exact_synthesis_inclusion_is_required_but_audit_text_is_not(self):
        for target, replacement in (
            ("summary", "Revenue and profit moved differently."),
            ("thesis", "Margins may be under pressure."),
        ):
            with self.subTest(target=target):
                payload = copy.deepcopy(self.payload)
                payload[target] = replacement
                self.assertTrue(self._problems(payload))

        concise = copy.deepcopy(self.payload)
        concise["summary"] = (
            concise["relationship_reconciliations"][0]["summary_synthesis"]
        )
        concise["thesis"] = (
            concise["relationship_reconciliations"][0]["thesis_synthesis"]
        )
        self.assertEqual(self._problems(concise), [])

    def test_incompatible_relationship_requires_empty_synthesis_fields(self):
        cases = (
            ("summary_synthesis", "Cash measures are incomparable."),
            ("thesis_synthesis", "No cash conclusion is supportable."),
            (
                "summary_fact_paths",
                ["deterministic_current.relationship_facts.rf_cash"],
            ),
        )
        for field, value in cases:
            with self.subTest(field=field):
                payload = copy.deepcopy(self.payload)
                payload["relationship_reconciliations"][1][field] = value
                self.assertTrue(self._problems(payload))



class CompanyQualityGateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)

    def _producer(self, excerpt=None, deterministic_current=None):
        return cb.load_producer_case(
            write_yaml(
                self.directory,
                "producer.yaml",
                producer_raw(excerpt, deterministic_current),
            )
        )

    def _evaluator(self, producer, **overrides):
        raw = evaluator_raw(producer.fingerprint, **overrides)
        return cb.load_evaluator_case(
            write_yaml(self.directory, "evaluator.yaml", raw), producer=producer
        )

    def _run(self, producer=None, evaluator=None, finalized=None, payload=None):
        producer = producer or self._producer()
        evaluator = evaluator or self._evaluator(producer)
        finalized = finalized or finalized_for(payload or narrative_payload())
        return cq.run_company_hard_gates(producer, evaluator, finalized)

    def _codes(self, report, code):
        return [failure for failure in report.failures if failure.code == code]

    def test_clean_case_passes(self):
        report = self._run()
        self.assertTrue(report.passed)
        self.assertEqual(report.failures, ())

    def test_cross_span_and_fabricated_evidence_fail(self):
        producer = self._producer(
            excerpt=(
                "[Source characters 0-40]\nAI demand remained durable.\n"
                "[Source characters 41-90]\nSupply stayed tight throughout."
            )
        )
        evaluator = self._evaluator(producer)
        cross_payload = narrative_payload()
        cross_payload["qualitative"]["ai_demand"]["evidence"] = (
            "remained durable. Supply stayed tight"
        )
        for name, payload in (
            ("cross-span", cross_payload),
            ("fabricated", narrative_payload()),
        ):
            with self.subTest(name=name):
                if name == "fabricated":
                    payload["qualitative"]["ai_demand"]["evidence"] = (
                        "Orders surged unexpectedly"
                    )
                report = self._run(
                    producer=producer,
                    evaluator=evaluator,
                    finalized=finalized_for(payload),
                )
                self.assertFalse(report.passed)
                violations = self._codes(report, "investment_evidence_violation")
                self.assertEqual(len(violations), 1)


    def test_risk_and_catalyst_evidence_grounding_does_not_claim_entailment(self):
        payload = narrative_payload()
        payload["risks"] = [
            {
                "sourced_observation": "Demand remained durable.",
                "inference": "A future downturn could still compress margins.",
                "epistemic_state": "hypothesis",
                "uncertainty": "Future demand is not yet observable.",
                "likelihood": "low",
                "impact": "high",
                "mitigation": "Monitor order trends.",
                "evidence": "demand remained durable",
            }
        ]
        payload["catalysts"] = [
            epistemic_catalyst(
                "Supply stayed tight.",
                "Near term",
                "supply stayed tight",
            )
        ]
        self.assertEqual(
            service.investment_evidence_violations(
                payload,
                excerpt=EXCERPT,
                news_items=(),
            ),
            [],
        )

        payload["risks"][0]["evidence"] = "fabricated risk quote"
        payload["catalysts"][0]["evidence"] = "fabricated catalyst quote"
        self.assertEqual(
            service.investment_evidence_violations(
                payload,
                excerpt=EXCERPT,
                news_items=(),
            ),
            [
                "catalysts[0]: evidence is not grounded in the filing excerpt",
                "risks[0]: evidence is not grounded in the filing excerpt",
            ],
        )

    def test_normalized_equal_observation_inference_and_trigger_outcome_reject(self):
        payload = narrative_payload()
        payload["risks"] = [
            {
                "sourced_observation": "Demand—remained   durable.",
                "inference": " demand-remained durable. ",
                "epistemic_state": "supported",
                "uncertainty": "Future demand remains uncertain.",
                "likelihood": "low",
                "impact": "medium",
                "mitigation": "Monitor order trends.",
                "evidence": "demand remained durable",
            }
        ]
        payload["catalysts"] = [
            {
                "trigger": "Supply—stayed tight.",
                "expected_outcome": " supply-stayed   tight. ",
                "horizon": "Near term",
                "epistemic_state": "supported",
                "uncertainty": "Timing remains uncertain.",
                "evidence": "supply stayed tight",
            }
        ]
        self.assertEqual(
            service.risk_catalyst_contract_violations(payload),
            [
                "$.risks[0]: sourced_observation and inference must differ",
                "$.catalysts[0]: trigger and expected_outcome must differ",
            ],
        )

    def test_counter_thesis_and_materiality_contract_and_grounding_gates(self):
        producer = self._producer(
            excerpt=(
                "AI demand remained durable while supply stayed tight. "
                "Revenue rose 12 percent."
            )
        )
        evaluator = self._evaluator(producer)

        # 1. Clean valid payload with default not_disclosed materiality passes.
        clean_payload = narrative_payload()
        clean_report = self._run(
            producer=producer, evaluator=evaluator, finalized=finalized_for(clean_payload)
        )
        self.assertTrue(clean_report.passed, clean_report.failures)
        self.assertEqual(clean_report.failures, ())

        # 2. Blank or whitespace-only counter_thesis fails closed.
        for blank_thesis in ("", "   "):
            with self.subTest(counter_thesis=repr(blank_thesis)):
                payload = narrative_payload(counter_thesis=blank_thesis)
                problems = service.validate_investment_report_payload(payload)
                self.assertIn("$.counter_thesis: must be nonblank", problems)
                report = self._run(
                    producer=producer, evaluator=evaluator, finalized=finalized_for(payload)
                )
                self.assertFalse(report.passed)
                failures = self._codes(report, "investment_narrative_contract_violation")
                self.assertTrue(
                    any("counter_thesis" in failure.observed for failure in failures),
                    report.failures,
                )

        # 3. Addressed materiality topic with grounded filing evidence passes.
        addressed_materiality = {
            topic: {
                "status": "not_disclosed",
                "observation": "",
                "implication": "",
                "evidence": "",
            }
            for topic in (
                "forward_guidance",
                "reported_variance_driver",
                "margin_economics",
                "capital_commitment_duration",
            )
        }
        addressed_materiality["reported_variance_driver"] = {
            "status": "addressed",
            "observation": "Revenue rose 12 percent driven by demand.",
            "implication": "Variance driver supports top-line growth assumption.",
            "evidence": "Revenue rose 12 percent",
        }
        addressed_payload = narrative_payload(
            materiality_assessment=addressed_materiality
        )
        self.assertEqual(service.validate_investment_report_payload(addressed_payload), [])
        self.assertEqual(
            service.investment_evidence_violations(
                addressed_payload, excerpt=producer.excerpt, news_items=()
            ),
            [],
        )
        addressed_report = self._run(
            producer=producer, evaluator=evaluator, finalized=finalized_for(addressed_payload)
        )
        self.assertTrue(addressed_report.passed, addressed_report.failures)

        # 4. Addressed topic with ungrounded evidence fails with evidence violation.
        ungrounded_materiality = copy.deepcopy(addressed_materiality)
        ungrounded_materiality["reported_variance_driver"]["evidence"] = (
            "Cloud revenue accelerated unexpectedly"
        )
        ungrounded_payload = narrative_payload(
            materiality_assessment=ungrounded_materiality
        )
        self.assertIn(
            "materiality_assessment.reported_variance_driver: evidence is not grounded in the filing excerpt",
            service.investment_evidence_violations(
                ungrounded_payload, excerpt=producer.excerpt, news_items=()
            ),
        )
        ungrounded_report = self._run(
            producer=producer, evaluator=evaluator, finalized=finalized_for(ungrounded_payload)
        )
        self.assertFalse(ungrounded_report.passed)
        evidence_failures = self._codes(ungrounded_report, "investment_evidence_violation")
        self.assertTrue(
            any(
                "materiality_assessment.reported_variance_driver" in failure.observed
                for failure in evidence_failures
            ),
            ungrounded_report.failures,
        )

        # 5. Addressed topic with blank observation, implication, or evidence fails contract/evidence.
        blank_cases = (
            ("observation", "", "must be nonblank when status is addressed"),
            ("implication", "", "must be nonblank when status is addressed"),
            ("evidence", "", "must be nonblank when status is addressed"),
        )
        for field, empty_val, expected_msg in blank_cases:
            with self.subTest(blank_field=field):
                bad_mat = copy.deepcopy(addressed_materiality)
                bad_mat["reported_variance_driver"][field] = empty_val
                bad_payload = narrative_payload(materiality_assessment=bad_mat)
                problems = service.materiality_assessment_contract_violations(bad_payload)
                self.assertTrue(
                    any(expected_msg in p for p in problems),
                    problems,
                )
                bad_report = self._run(
                    producer=producer, evaluator=evaluator, finalized=finalized_for(bad_payload)
                )
                self.assertFalse(bad_report.passed)

        # 6. not_disclosed topic with non-empty fields fails contract.
        for field in ("observation", "implication", "evidence"):
            with self.subTest(not_disclosed_field=field):
                not_disc = copy.deepcopy(clean_payload["materiality_assessment"])
                not_disc["forward_guidance"][field] = "should not be here"
                bad_payload = narrative_payload(materiality_assessment=not_disc)
                problems = service.materiality_assessment_contract_violations(bad_payload)
                self.assertTrue(
                    any("must be exactly empty when status is not_disclosed" in p for p in problems),
                    problems,
                )
                bad_report = self._run(
                    producer=producer, evaluator=evaluator, finalized=finalized_for(bad_payload)
                )
                self.assertFalse(bad_report.passed)
    def test_unsupported_authored_number_fails_grounded_passes(self):
        # The excerpt carries the supported figure together with its unit
        # rendering, metric identity, and source-carried fiscal period so
        # one quotable span can verify the row below.
        producer = self._producer(
            excerpt=(
                "AI demand remained durable while supply stayed tight. "
                "Revenue rose 12% in FY2025."
            )
        )
        evaluator = self._evaluator(producer)
        # An authored number with no binding row fails the semantic gate:
        # 13% appears nowhere in this case, and no ledger row covers it.
        unsupported = finalized_for(
            narrative_payload(summary="Revenue up 13% in FY2025.")
        )
        report = self._run(
            producer=producer, evaluator=evaluator, finalized=unsupported
        )
        self.assertFalse(report.passed)
        self.assertEqual(len(self._codes(report, "numeric_claim_unbound")), 1)
        # A supported number needs a REAL source-bound row: marking numeric
        # prose supported with an empty ledger would recreate the old
        # global token-presence gate and must never pass.
        # ``InvestmentFinalizedAnalysis`` is immutable: authored rows are
        # supplied through the plain payload mapping before finalization,
        # mirroring the range-grounding test below.
        supported_payload = narrative_payload(
            summary="Revenue up 12% in FY2025."
        )
        supported_payload["numeric_claims"] = [
            {
                "claim_id": "revenue_growth_fy2025",
                "path": "summary",
                "value": "12%",
                "metric": "revenue growth",
                "period": "FY2025",
                "unit": "percent",
                "currency": None,
                "source_kind": "text",
                "quote": "Revenue rose 12% in FY2025.",
            }
        ]
        supported = finalized_for(supported_payload)
        report = self._run(
            producer=producer, evaluator=evaluator, finalized=supported
        )
        self.assertTrue(report.passed)

    def test_target_path_forms_cover_the_same_material_narrative_number(self):
        producer = self._producer(
            excerpt=(
                "AI demand remained durable while supply stayed tight. "
                "Revenue rose 12% in FY2025."
            )
        )
        evaluator = self._evaluator(producer)

        for authored_path in ("summary", "$.summary", "/summary"):
            with self.subTest(path=authored_path):
                payload = narrative_payload(
                    summary="Revenue up 12% in FY2025."
                )
                payload["numeric_claims"] = [
                    {
                        "claim_id": "revenue_growth_fy2025",
                        "path": authored_path,
                        "value": "12%",
                        "metric": "revenue growth",
                        "period": "FY2025",
                        "unit": "percent",
                        "currency": None,
                        "source_kind": "text",
                        "quote": "Revenue rose 12% in FY2025.",
                    }
                ]
                finalized = finalized_for(payload)

                report = self._run(
                    producer=producer,
                    evaluator=evaluator,
                    finalized=finalized,
                )

                self.assertTrue(report.passed, report.failures)
                self.assertEqual(
                    finalized.facts["numeric_claims"][0]["path"],
                    authored_path,
                )

    def test_qualitative_evidence_target_names_match_replayed_hard_gates(self):
        quote = "AI demand rose 12% in FY2025."
        producer = self._producer(
            excerpt=f"AI demand remained durable. {quote}"
        )
        evaluator = self._evaluator(producer)

        def payload_for(signal):
            payload = narrative_payload()
            signal_evidence = {
                "present": True,
                "strength": "strong",
                "evidence": quote,
            }
            payload["qualitative"][signal] = signal_evidence
            payload["numeric_claims"] = [
                {
                    "claim_id": f"{signal}_growth_fy2025",
                    "path": f"/qualitative/{signal}/evidence",
                    "value": "12%",
                    "metric": "AI demand",
                    "period": "FY2025",
                    "unit": "percent",
                    "currency": None,
                    "source_kind": "text",
                    "quote": quote,
                }
            ]
            return payload

        def replay(finalized):
            blob = json.loads(
                json.dumps(
                    {
                        "facts": finalized.facts,
                        "classified_industry": finalized.classified_industry,
                        "previous_facts": finalized.previous_facts,
                        "analysis": finalized.analysis,
                    }
                )
            )
            return service.InvestmentFinalizedAnalysis(**blob)

        known = finalized_for(payload_for("ai_demand"))
        for mode, finalized in (
            ("direct", known),
            ("replay", replay(known)),
        ):
            with self.subTest(signal="ai_demand", mode=mode):
                report = cq.run_company_hard_gates(
                    producer, evaluator, finalized
                )
                self.assertTrue(report.passed, report.failures)
                self.assertEqual(report.failures, ())

        forged = finalized_for(payload_for("forged_signal"))
        for mode, finalized in (
            ("direct", forged),
            ("replay", replay(forged)),
        ):
            with self.subTest(signal="forged_signal", mode=mode):
                report = cq.run_company_hard_gates(
                    producer, evaluator, finalized
                )
                self.assertFalse(report.passed)
                self.assertEqual(
                    [failure.code for failure in report.failures],
                    ["numeric_claim_target_missing"],
                )

    def test_range_and_negative_magnitude_number_grounding(self):
        """Each range endpoint binds through its own verified row sharing
        the verbatim source quote; fact-backed negatives bind through the
        deterministic ledger, not token sets."""
        producer = self._producer(
            excerpt=(
                "AI demand remained durable while supply stayed tight. "
                "Revenue rose 28% to 29% year over year in FY2026."
            )
        )
        evaluator = self._evaluator(producer)

        def range_row(**overrides):
            row = {
                "claim_id": "range_growth_low",
                "path": "summary",
                "value": "28%",
                "metric": "revenue growth",
                "period": "FY2026",
                "unit": "percent",
                "currency": None,
                "source_kind": "text",
                "quote": "Revenue rose 28% to 29% year over year in FY2026.",
            }
            row.update(overrides)
            return row

        # One finite quantity per row: the schema deliberately refuses a
        # range value like "28%-29%", so each endpoint gets its own row.
        # Distinct values at one target path are distinct bindings, never
        # duplicates; both rows cite the same verbatim source span.
        range_payload = narrative_payload(
            summary="Revenue growth was 28%-29% in FY2026."
        )
        range_payload["numeric_claims"] = [
            range_row(),
            range_row(claim_id="range_growth_high", value="29%"),
        ]
        report = self._run(
            producer=producer,
            evaluator=evaluator,
            finalized=finalized_for(range_payload),
        )
        self.assertTrue(report.passed, report.failures)
        # The endpoints ground as 28 and 29 — never as a spurious -29 — so
        # a pass here already proves no ungrounded '-29' token was emitted.
        # Grounding flows from ProducerCase deterministic_current/prior, so
        # the -0.06 fact lives on a dedicated producer carrying the full
        # tuple a fact row must match (value/unit/currency/period), not
        # just the finalized analysis metrics.
        eps_producer = self._producer(
            excerpt=(
                "AI demand remained durable while supply stayed tight. "
                "Earnings per share shifted modestly."
            ),
            deterministic_current={
                "earnings_per_share": {
                    "value": -0.06,
                    "unit": "usd_per_share",
                    "currency": "USD",
                    "period": "FY2026",
                }
            },
        )
        eps_evaluator = self._evaluator(eps_producer)
        negative_payload = narrative_payload(
            summary="Earnings moved to negative $0.06 per share in FY2026."
        )
        negative_payload["numeric_claims"] = [
            {
                "claim_id": "eps_fact",
                "path": "summary",
                "value": -0.06,
                "metric": "earnings per share",
                "period": "FY2026",
                "unit": "usd_per_share",
                "currency": "USD",
                "source_kind": "fact",
                "fact_path": "deterministic_current.earnings_per_share.value",
            }
        ]
        grounded = self._run(
            producer=eps_producer,
            evaluator=eps_evaluator,
            finalized=finalized_for(negative_payload),
        )
        self.assertTrue(grounded.passed, grounded.failures)
        # Drifting the authored magnitude off the fact fails closed: the
        # row's tuple no longer matches its cited deterministic source.
        changed = narrative_payload(
            summary="Earnings moved to negative $0.07 per share in FY2026."
        )
        changed["numeric_claims"] = [
            dict(negative_payload["numeric_claims"][0], value=-0.07)
        ]
        report = self._run(
            producer=eps_producer,
            evaluator=eps_evaluator,
            finalized=finalized_for(changed),
        )
        self.assertFalse(report.passed)
        self.assertGreaterEqual(
            len(self._codes(report, "numeric_claim_tuple_mismatch")), 1
        )

    def test_required_material_evidence_is_filing_span_integrity_only(self):
        producer = self._producer()
        # A quote absent from every producer filing span fails: the evaluator
        # fixture itself is broken, regardless of what the output contains.
        absent_from_filing = self._evaluator(
            producer, required_material_evidence=["Backlog doubled"]
        )
        report = self._run(
            producer=producer, evaluator=absent_from_filing
        )
        self.assertFalse(report.passed)
        self.assertEqual(
            len(self._codes(report, "required_evidence_absent_from_filing_span")), 1
        )
        self.assertEqual(
            self._codes(report, "required_evidence_absent_from_output"), []
        )
        # A quote present in a filing span passes even when the model output
        # never quotes it: output omission is judged by materiality judges,
        # never forced by an evaluator quote.
        filed_but_unquoted = self._evaluator(
            producer, required_material_evidence=["Revenue rose 12 percent"]
        )
        report = self._run(
            producer=producer, evaluator=filed_but_unquoted
        )
        self.assertTrue(report.passed, report.failures)
        # An evaluator quote can never force an artificial risk or catalyst:
        # the payload below has none, and the gate stays green.
        forcing_attempt = self._evaluator(
            producer,
            required_material_evidence=[
                "Revenue rose 12 percent",
                "supply stayed tight",
            ],
        )
        empty_narrative = narrative_payload()
        empty_narrative["risks"] = []
        empty_narrative["catalysts"] = []
        report = self._run(
            producer=producer,
            evaluator=forcing_attempt,
            finalized=finalized_for(empty_narrative),
        )
        self.assertTrue(report.passed, report.failures)

    def test_structured_claim_catches_paraphrase_across_fields(self):
        producer = self._producer()
        claim_row = {
            "claim_id": "capex_q1_fy25",
            "metric_aliases": ["capex", "capital expenditures"],
            "value": 20,
            "period_aliases": ["Q1 FY2025", "next quarter"],
            "available_after": "2026-06-01T00:00:00Z",
        }

        def claim_evaluator(**overrides):
            row = dict(claim_row)
            row.update(overrides)
            return self._evaluator(producer, forbidden_hindsight=[row])

        # A leak is the same forbidden value whether its scale is written out
        # or compact. A leading positive sign is also value-preserving.
        positive_surfaces = (
            "$20 billion",
            "$20B",
            "$20bn",
            "+$20B",
        )
        for surface in positive_surfaces:
            with self.subTest(forbidden_value_surface=surface):
                paraphrase = narrative_payload(
                    summary=f"Next quarter capex reached {surface}, up sharply."
                )
                report = self._run(
                    producer=producer,
                    evaluator=claim_evaluator(),
                    finalized=finalized_for(paraphrase),
                )
                self.assertFalse(report.passed)
                leaks = self._codes(report, "forbidden_company_claim_present")
                self.assertEqual(len(leaks), 1, report.failures)
                self.assertEqual(
                    leaks[0].observed["claim_id"],
                    "capex_q1_fy25",
                )
        # Wrong metric and wrong period each break the triple, even with the
        # same $20 billion value. Split fields place alias, value, and period
        # in separate authored values: the gate matches per string and must
        # never stitch a case-wide triple.
        split_payload = narrative_payload(summary="Capex rose.")
        split_payload["thesis"] = "Revenue was $20 billion."
        split_payload["watch_items"] = ["Q1 FY2025 starts soon."]
        negatives = [
            (
                "wrong-metric",
                narrative_payload(
                    summary="Revenue reached $20 billion next quarter."
                ),
            ),
            (
                "wrong-period",
                narrative_payload(
                    summary="Current-quarter capex reached $20 billion."
                ),
            ),
            ("split-fields", split_payload),
            (
                "wrong-value",
                narrative_payload(
                    summary="Next quarter capex reached $19 billion."
                ),
            ),
            (
                "opposite-signed-value",
                narrative_payload(
                    summary="Next quarter capex reached -$20B."
                ),
            ),
        ]
        for label, payload in negatives:
            with self.subTest(negative=label):
                report = self._run(
                    producer=producer,
                    evaluator=claim_evaluator(),
                    finalized=finalized_for(payload),
                )
                self.assertEqual(
                    self._codes(report, "forbidden_company_claim_present"), []
                )

    def test_fixture_claims_are_detectable_individually(self):
        claims = [
            {
                "claim_id": "azure_growth",
                "metric_aliases": ["Azure growth", "cloud services growth"],
                "value": 33,
                "period_aliases": ["Q1 FY2025"],
                "available_after": "2026-06-01T00:00:00Z",
            },
            {
                "claim_id": "cloud_margin",
                "metric_aliases": ["cloud gross margin"],
                "value": 71,
                "period_aliases": ["Q1 FY2025"],
                "available_after": "2026-06-01T00:00:00Z",
            },
            {
                "claim_id": "capex_total",
                "metric_aliases": ["capex"],
                "value": 20,
                "period_aliases": ["Q1 FY2025"],
                "available_after": "2026-06-01T00:00:00Z",
            },
        ]
        for claim in claims:
            with self.subTest(claim_id=claim["claim_id"]):
                producer = self._producer()
                evaluator = self._evaluator(
                    producer, forbidden_hindsight=[dict(claim)]
                )
                leaked = {
                    "azure_growth": "Azure growth hit 33% in Q1 FY2025.",
                    "cloud_margin": "Cloud gross margin was 71% in Q1 FY2025.",
                    "capex_total": "Capex totaled 20 billion in Q1 FY2025.",
                }[claim["claim_id"]]
                report = self._run(
                    producer=producer,
                    evaluator=evaluator,
                    finalized=finalized_for(narrative_payload(summary=leaked)),
                )
                self.assertFalse(report.passed)
                self.assertEqual(
                    len(self._codes(report, "forbidden_company_claim_present")), 1
                )

    def test_prohibited_trading_instruction_fails(self):
        payload = narrative_payload()
        payload["watch_items"] = ["Investors may buy shares ahead of results."]
        report = self._run(finalized=finalized_for(payload))
        self.assertFalse(report.passed)
        failures = self._codes(report, "prohibited_language_present")
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].root_category, "trading_instruction")

    def test_public_gate_rejects_portfolio_sizing_and_exposure_instructions(self):
        for instruction in (
            "Size exposure to reflect downside risk.",
            "Reduce portfolio exposure ahead of earnings.",
        ):
            with self.subTest(instruction=instruction):
                payload = narrative_payload()
                payload["watch_items"] = [instruction]
                report = self._run(finalized=finalized_for(payload))

                failures = self._codes(report, "prohibited_language_present")
                self.assertFalse(report.passed)
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0].root_category, "sizing_allocation")

    def test_public_gate_allows_monitoring_and_company_descriptions(self):
        payload = narrative_payload()
        payload["watch_items"] = [
            "Monitor inventory levels for signs of oversupply.",
            "Customer exposure remains concentrated among large enterprises.",
            "Company capital allocation remains focused on data centers.",
        ]

        report = self._run(finalized=finalized_for(payload))

        self.assertTrue(report.passed, report.failures)
        self.assertEqual(self._codes(report, "prohibited_language_present"), [])

    def test_fingerprint_mismatch_fails(self):
        producer = self._producer()
        evaluator = dataclass_replace(
            self._evaluator(producer), producer_fingerprint="f" * 64
        )
        report = self._run(producer=producer, evaluator=evaluator)
        self.assertFalse(report.passed)
        mismatches = self._codes(report, "producer_evaluator_fingerprint_mismatch")
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0].severity, cq.SEVERITY_CRITICAL)

    def test_report_carries_derived_producer_identity(self):
        producer = self._producer()
        report = self._run(producer=producer)
        self.assertRegex(report.producer_fingerprint, r"[a-f0-9]{64}")
        self.assertEqual(report.producer_fingerprint, producer.fingerprint)

    def test_report_identity_is_required_nonblank_sha256(self):
        rejections = [
            "",
            "   ",
            "not-a-fingerprint",
            "F" * 64,  # uppercase hex is never a canonical SHA-256 identity
            "abc",  # truncated digest
            "g" * 64,  # non-hex text of the right length
            None,
            123,
        ]
        for identity in rejections:
            with self.subTest(identity=repr(identity)[:24]):
                with self.assertRaises(ValueError):
                    cq.HardGateReport(
                        passed=True, producer_fingerprint=identity, failures=()
                    )

    def test_run_hard_gates_fail_closed_without_producer_identity(self):
        producer = self._producer()
        evaluator = self._evaluator(producer)
        for broken_identity in ("", "   ", "nothex", None):
            with self.subTest(identity=repr(broken_identity)):
                broken = dataclass_replace(producer, fingerprint=broken_identity)
                with self.assertRaises(ValueError):
                    cq.run_company_hard_gates(
                        broken, evaluator, finalized_for(narrative_payload())
                    )

    def test_each_ledger_kind_has_pass_and_plausible_fail(self):
        producer = self._producer()
        evaluator = self._evaluator(producer)
        cases = [
            ("equals", ledger_row("equals", "facts.summary",
                                  "Demand durable, supply tight."),
             ledger_row("equals", "facts.summary", "Different entirely.")),
            ("contains", ledger_row("contains", "facts.summary", "Demand"),
             ledger_row("contains", "facts.summary", "orders exploded")),
            ("not_contains", ledger_row("not_contains", "facts.summary",
                                        "buy shares"),
             ledger_row("not_contains", "facts.summary", "Demand")),
            ("nonblank", nonblank_ledger_row("facts.summary"),
             nonblank_ledger_row("facts.watch_items")),
            # No-tolerance pass is intentional: tolerance is optional and
            # defaults to zero, so an exact 100 vs 100 match passes.
            ("number_close",
             ledger_row("number_close", "facts.metrics.revenue.value", 100),
             None),
        ]
        for kind, passing_row, failing_row in cases:
            with self.subTest(kind=f"{kind}-pass"):
                evaluator = dataclass_replace(
                    self._evaluator(producer),
                    deterministic_checks=[copy.deepcopy(passing_row)],
                )
                report = self._run(
                    producer=producer,
                    evaluator=evaluator,
                    finalized=finalized_for(
                        narrative_payload(),
                        deterministic_current={"revenue": {"value": 100}},
                    ),
                )
                self.assertTrue(report.passed, report.failures)
            if failing_row is None:
                continue
            with self.subTest(kind=f"{kind}-fail"):
                evaluator = dataclass_replace(
                    self._evaluator(producer),
                    deterministic_checks=[copy.deepcopy(failing_row)],
                )
                report = self._run(
                    producer=producer,
                    evaluator=evaluator,
                    finalized=finalized_for(
                        narrative_payload(),
                        deterministic_current={"revenue": {"value": 100}},
                    ),
                )
                self.assertFalse(report.passed)
                self.assertEqual(
                    len(self._codes(report, "deterministic_check_failed")), 1
                )
        with self.subTest(kind="number_close-tolerance-fail"):
            evaluator = dataclass_replace(
                self._evaluator(producer),
                deterministic_checks=[
                    ledger_row("number_close", "facts.metrics.revenue.value", 150)
                ],
            )
            report = self._run(
                producer=producer,
                evaluator=evaluator,
                finalized=finalized_for(
                    narrative_payload(), deterministic_current={"revenue": {"value": 100}}
                ),
            )
            self.assertFalse(report.passed)
        with self.subTest(kind="arithmetic_close-pass"):
            evaluator = dataclass_replace(
                self._evaluator(producer), deterministic_checks=[dict(ARITHMETIC_ROW)]
            )
            report = self._run(
                producer=producer,
                evaluator=evaluator,
                finalized=finalized_for(
                    narrative_payload(), deterministic_current=copy.deepcopy(ARITHMETIC_METRICS)
                ),
            )
            self.assertTrue(report.passed, report.failures)
        with self.subTest(kind="arithmetic_close-fail"):
            metrics = copy.deepcopy(ARITHMETIC_METRICS)
            metrics["eps"]["value"] = 3
            evaluator = dataclass_replace(
                self._evaluator(producer), deterministic_checks=[dict(ARITHMETIC_ROW)]
            )
            report = self._run(
                producer=producer,
                evaluator=evaluator,
                finalized=finalized_for(
                    narrative_payload(), deterministic_current=metrics
                ),
            )
            self.assertFalse(report.passed)
            self.assertEqual(
                len(self._codes(report, "deterministic_check_failed")), 1
            )

    def test_malformed_or_unknown_ledger_rows_fail_critical(self):
        producer = self._producer()
        evaluator = dataclass_replace(
            self._evaluator(producer),
            deterministic_checks=["not-an-object"],
        )
        report = self._run(producer=producer, evaluator=evaluator)
        self.assertFalse(report.passed)
        self.assertEqual(
            len(self._codes(report, "deterministic_checks_contract_violation")), 1
        )
        unknown = dataclass_replace(
            self._evaluator(producer),
            deterministic_checks=[
                ledger_row("teleport", "facts.summary", "x")
            ],
        )
        report = self._run(producer=producer, evaluator=unknown)
        self.assertFalse(report.passed)
        self.assertEqual(
            len(self._codes(report, "deterministic_checks_contract_violation")), 1
        )

    def test_arithmetic_edge_cases_fail_closed(self):
        producer = self._producer()
        zero_scale_row = dict(ARITHMETIC_ROW, scale=0)
        missing_row = dict(
            ARITHMETIC_ROW, numerator_path="facts.metrics.gone.value"
        )
        scenarios = (
            ("zero-scale", zero_scale_row, {}, "contract"),
            ("missing-numerator", missing_row, {}, "failed"),
            ("zero-denominator", ARITHMETIC_ROW,
             {"shares_outstanding": {"value": 0}}, "failed"),
            ("nonfinite-denominator", ARITHMETIC_ROW,
             {"shares_outstanding": {"value": float("nan")}}, "failed"),
        )
        for name, row, overrides, expectation in scenarios:
            with self.subTest(name=name):
                metrics = copy.deepcopy(ARITHMETIC_METRICS)
                metrics.update(copy.deepcopy(overrides))
                evaluator = dataclass_replace(
                    self._evaluator(producer), deterministic_checks=[dict(row)]
                )
                report = self._run(
                    producer=producer,
                    evaluator=evaluator,
                    finalized=finalized_for(
                        narrative_payload(), deterministic_current=metrics
                    ),
                )
                self.assertFalse(report.passed)
                if expectation == "contract":
                    self.assertEqual(
                        len(
                            self._codes(
                                report, "deterministic_checks_contract_violation"
                            )
                        ),
                        1,
                    )
                else:
                    failures = self._codes(report, "deterministic_check_failed")
                    self.assertEqual(len(failures), 1)
                    if name == "zero-denominator":
                        self.assertIn("division_by_zero", failures[0].evidence)
                    elif name == "missing-numerator":
                        self.assertIn("numerator missing", failures[0].evidence)
                    else:
                        self.assertIn("denominator missing", failures[0].evidence)

    def test_failure_ordering_is_stable_and_sorted(self):
        producer = self._producer()
        evaluator = dataclass_replace(
            self._evaluator(producer), producer_fingerprint="f" * 64
        )
        payload = narrative_payload(summary="Demand up 13 percent.")
        payload["watch_items"] = ["Investors may buy shares early."]
        payload["qualitative"]["ai_demand"]["evidence"] = (
            "Orders surged unexpectedly"
        )
        first = self._run(
            producer=producer, evaluator=evaluator, finalized=finalized_for(payload)
        )
        second = self._run(
            producer=producer, evaluator=evaluator, finalized=finalized_for(payload)
        )
        self.assertFalse(first.passed)
        self.assertEqual(first.failures, second.failures)
        ordered = [
            (failure.code, failure.path, failure.evidence)
            for failure in first.failures
        ]
        self.assertEqual(ordered, sorted(set(ordered)))
        self.assertEqual(
            [failure.code for failure in first.failures],
            sorted(failure.code for failure in first.failures),
        )

