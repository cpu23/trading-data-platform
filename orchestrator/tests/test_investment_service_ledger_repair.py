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
    NumericClaimLedgerTestBase,
    session_context,
)

import investment_service as service


class NumericClaimLedgerRepairTests(NumericClaimLedgerTestBase):
    """Tests for numeric claim ledger live repair flows and nested object shape recovery."""

    def test_public_live_flow_combines_fact_tuple_rejections_into_one_repair(
        self,
    ):
        cases = [
            (
                "RAW_PRIVATE_TARGET_WITHOUT_GUIDANCE",
                (
                    "Microsoft Cloud gross margin was roughly 70% for "
                    "Q1 FY2025."
                ),
                {},
            ),
            (
                "RAW_PRIVATE_WRONG_COEFFICIENT",
                (
                    "Microsoft Cloud gross margin guidance was roughly 69% "
                    "for Q1 FY2025."
                ),
                {"value": "69%"},
            ),
            (
                "RAW_PRIVATE_WRONG_METRIC",
                "Operating margin guidance was roughly 70% for Q1 FY2025.",
                {"metric": "operating margin guidance"},
            ),
            (
                "RAW_PRIVATE_WRONG_PERIOD",
                (
                    "Microsoft Cloud gross margin guidance was roughly 70% "
                    "for Q4 FY2024."
                ),
                {"period": "FY2024-Q4"},
            ),
            (
                "RAW_PRIVATE_WRONG_UNIT",
                (
                    "Microsoft Cloud gross margin guidance was roughly "
                    "$70 billion for Q1 FY2025."
                ),
                {
                    "value": "$70B",
                    "unit": "usd_billions",
                    "currency": "USD",
                },
            ),
            (
                "RAW_PRIVATE_WRONG_CURRENCY",
                (
                    "Microsoft Cloud gross margin guidance was roughly 70% "
                    "for Q1 FY2025."
                ),
                {"currency": "USD"},
            ),
            (
                "RAW_PRIVATE_NEAR_FACT_PATH",
                (
                    "Microsoft Cloud gross margin guidance was roughly 70% "
                    "for Q1 FY2025."
                ),
                {
                    "fact_path": (
                        "deterministic_current."
                        "microsoft_cloud_gross_margin_reported.value"
                    )
                },
            ),
        ]
        rows = []
        drivers = []
        for index, (claim_id, statement, overrides) in enumerate(cases):
            path = "summary" if index == 0 else f"drivers[{index - 1}]"
            rows.append(
                self._tuple_fact_row(
                    claim_id=claim_id,
                    path=path,
                    **overrides,
                )
            )
            if index:
                drivers.append(statement)
        invalid = self._payload(rows)
        invalid["summary"] = cases[0][1]
        invalid["drivers"] = drivers
        corrected = self._payload([self._tuple_fact_row()])
        corrected["summary"] = (
            "Microsoft Cloud gross margin guidance was roughly 70% for "
            "Q1 FY2025."
        )
        excerpt = "Demand remained durable. Management provided its outlook."
        frozen_current = service._freeze_json_value(
            self._tuple_fact_sources()
        )
        expected_problems = [
            (
                f"numeric_claims[{index}] (claim_id {claim_id!r}): fact "
                "source tuple does not match its authored target and "
                "deterministic leaf"
            )
            for index, (claim_id, _statement, _overrides) in enumerate(cases)
        ]

        with self.assertRaises(service.InvestmentValidationError) as first:
            service._validated_investment_facts(
                json.dumps(invalid),
                excerpt=excerpt,
                news_items=service._freeze_json_value([]),
                deterministic_current=frozen_current,
                deterministic_prior=service._freeze_json_value({}),
            )

        self.assertEqual(
            first.exception.categories,
            (service.VALIDATION_JSON_SCHEMA,),
        )
        self.assertEqual(first.exception.problems, expected_problems)
        self.assertEqual(
            first.exception.problems_by_category,
            {service.VALIDATION_JSON_SCHEMA: expected_problems},
        )
        correction = first.exception.correction_requirement
        correction_folded = correction.casefold()
        correction_semantic_surface = (
            correction_folded.replace("no _", "no underscores")
            .replace("percent=%", "percent as %")
            .replace("professional currency", "formatted currency")
            .replace("!prose", "never prose")
        )
        tuple_identity_semantics = ("exact direct", "fact_path")
        for semantics in (
            ("correction", "json"),
            ("compatible relationships", "required facts", "exact request order"),
            ("atomic", "clause", "metric", "rendered value/unit", "exact period"),
            tuple_identity_semantics,
            ("fact_path", "metric_label", "period", "unit", "currency"),
            (
                "one fact row",
                "observation target",
                "summary repeats",
                "share one",
            ),
            (
                "no underscores",
                "percentage points",
                "currency",
            ),
            ("finite scalar", "token", "64", "never prose"),
            ("no child", "alias"),
        ):
            self.assertTrue(
                all(term in correction_semantic_surface for term in semantics),
                f"JSON repair omitted semantics: {semantics}",
            )
        self.assertLess(len(f"\n{correction}"), 700)
        with self._live_aggregation_harness(
            [invalid, corrected],
            deterministic_current=self._tuple_fact_sources(),
            excerpt=excerpt,
        ) as harness:
            result = service.analyze_document({}, harness.document_id)

        self.assertEqual(result, {"analysis_id": harness.analysis_id})
        self.assertEqual(harness.stage.call.call_count, 2)
        harness.finalize.assert_called_once()
        self.assertEqual(harness.finalize.call_args.args[0], corrected)
        base_prompt = harness.stage.call.call_args_list[0].args[0]
        repair_prompt = harness.stage.call.call_args_list[1].args[0]
        self.assertTrue(repair_prompt.startswith(f"{base_prompt}\n"))
        self.assertEqual(
            repair_prompt[len(base_prompt) + 1 :],
            first.exception.correction_requirement,
        )
        self.assertEqual(
            harness.stage.add_validation_warnings.call_args.args[0],
            ["response was not valid investment JSON"],
        )
        repair_suffix = repair_prompt[len(base_prompt) :]
        self.assertTrue(repair_suffix.startswith("\nCORRECTION: JSON"))
        self.assertLess(len(repair_suffix), 700)
        for claim_id, statement, _overrides in cases:
            self.assertNotIn(claim_id, repair_suffix)
            self.assertNotIn(statement, repair_suffix)
        self.assertNotIn(
            "microsoft_cloud_gross_margin_reported",
            repair_suffix,
        )


    def test_live_repair_reports_every_detectable_category_then_succeeds(self):
        invalid = self._aggregation_payload(corrected=False)
        corrected = self._aggregation_payload(corrected=True)
        frozen_current = service._freeze_json_value(
            {
                "capital_expenditures": {
                    "value": 19.0,
                    "unit": "usd_billions",
                    "currency": "USD",
                    "period": "FY2024-Q4",
                }
            }
        )
        frozen_prior = service._freeze_json_value({})
        frozen_news = service._freeze_json_value([])
        with self.assertRaises(service.InvestmentValidationError) as first:
            service._validated_investment_facts(
                json.dumps(invalid),
                excerpt=(
                    "Capital expenditures were $19B in FY2024 Q4. Demand "
                    "remained durable through the period."
                ),
                news_items=frozen_news,
                deterministic_current=frozen_current,
                deterministic_prior=frozen_prior,
            )

        schema_problems = [
            (
                "numeric_claims[0]: value must be a finite number or a numeric "
                'string (e.g. "19", "$19B", "28%") of at most 64 characters'
            ),
            (
                "numeric_claims[0] (claim_id 'RAW_PRIVATE_CLAIM_IDENTIFIER'): "
                "path 'drivers[99]' does not resolve to an eligible narrative "
                "text leaf"
            ),
            (
                "numeric_claims[0] (claim_id 'RAW_PRIVATE_CLAIM_IDENTIFIER'): "
                "fact_path "
                "'deterministic_current.raw_private_missing_metric.value' does "
                "not resolve in deterministic current/prior metrics"
            ),
            (
                "summary: material numeric token '19' has no numeric_claims "
                "binding"
            ),
        ]
        evidence_problems = [
            (
                "qualitative.pricing_power: evidence is not grounded in the "
                "filing excerpt"
            )
        ]
        self.assertEqual(
            first.exception.categories,
            (
                service.VALIDATION_JSON_SCHEMA,
                service.VALIDATION_FILING_EVIDENCE,
            ),
        )
        self.assertEqual(
            first.exception.problems_by_category,
            {
                service.VALIDATION_JSON_SCHEMA: schema_problems,
                service.VALIDATION_FILING_EVIDENCE: evidence_problems,
            },
        )
        self.assertEqual(
            first.exception.problems,
            schema_problems + evidence_problems,
        )
        correction = first.exception.correction_requirement
        correction_folded = correction.casefold()
        header_positions = [
            correction.index(header) for header in ("JSON:", "EVIDENCE:")
        ]
        self.assertEqual(header_positions, sorted(header_positions))
        for semantics in (
            ("relationship", "ordered", "exact"),
            ("one fact row", "observation", "summary", "shares row"),
            ("exact", "fact_path"),
            ("metric_label", "period", "unit", "currency"),
            ("evidence", "exact", "one filing region", "quote"),
            ("no joins", "scaffold", "commentary"),
            ("news", "item-bound"),
            ("deterministic", "time expansion"),
        ):
            self.assertTrue(
                all(term in correction_folded for term in semantics),
                f"combined repair omitted semantics: {semantics}",
            )
        self.assertLess(len(f"\n{correction}"), 700)

        with self._live_aggregation_harness([invalid, corrected]) as harness:
            result = service.analyze_document({}, harness.document_id)

        self.assertEqual(result, {"analysis_id": harness.analysis_id})
        self.assertEqual(harness.stage.call.call_count, 2)
        harness.finalize.assert_called_once()
        self.assertEqual(harness.finalize.call_args.args[0], corrected)
        base_prompt = harness.stage.call.call_args_list[0].args[0]
        repair_prompt = harness.stage.call.call_args_list[1].args[0]
        self.assertTrue(repair_prompt.startswith(f"{base_prompt}\n"))
        self.assertEqual(
            repair_prompt[len(base_prompt) + 1 :],
            first.exception.correction_requirement,
        )
        self.assertEqual(
            harness.stage.add_validation_warnings.call_args.args[0],
            [
                "response was not valid investment JSON",
                "filing evidence was blank or ungrounded",
            ],
        )
        repair_suffix = repair_prompt[len(base_prompt) :]
        self.assertLess(len(repair_suffix), 700)
        for raw_output in (
            "RAW_PRIVATE_VALUE",
            "RAW_PRIVATE_CLAIM_IDENTIFIER",
            "RAW_PRIVATE_UNGROUNDED_EVIDENCE",
            "raw_private_missing_metric",
            "drivers[99]",
        ):
            self.assertNotIn(raw_output, repair_suffix)


    def test_live_repair_combines_prohibited_language_without_raw_echo(self):
        invalid = self._aggregation_payload(corrected=False)
        invalid["watch_items"] = [
            "Reduce portfolio exposure before RAW_PRIVATE_EVENT."
        ]
        corrected = self._aggregation_payload(corrected=True)
        corrected["watch_items"] = [
            "Monitor inventory levels for signs of oversupply."
        ]
        expected_categories = (
            service.VALIDATION_JSON_SCHEMA,
            service.VALIDATION_FILING_EVIDENCE,
            service.VALIDATION_PROHIBITED_LANGUAGE,
        )

        with self.assertRaises(service.InvestmentValidationError) as first:
            service._validated_investment_facts(
                json.dumps(invalid),
                excerpt=(
                    "Capital expenditures were $19B in FY2024 Q4. Demand "
                    "remained durable through the period."
                ),
                news_items=service._freeze_json_value([]),
                deterministic_current=service._freeze_json_value(
                    {
                        "capital_expenditures": {
                            "value": 19.0,
                            "unit": "usd_billions",
                            "currency": "USD",
                            "period": "FY2024-Q4",
                        }
                    }
                ),
                deterministic_prior=service._freeze_json_value({}),
            )

        self.assertEqual(first.exception.categories, expected_categories)
        pairs = [
            (relationship, fact)
            for relationship in range(3)
            for fact in range(8)
        ]
        worst_case = service.InvestmentValidationError(
            first.exception.category,
            first.exception.problems,
            problems_by_category=first.exception.problems_by_category,
            missing_relationship_bindings=[*pairs, (0, 0), (2, 7)],
        ).correction_requirement
        folded = worst_case.casefold()
        header_positions = [
            worst_case.index(header)
            for header in ("JSON:", "EVIDENCE:", "LANGUAGE:")
        ]
        self.assertEqual(header_positions, sorted(header_positions))
        for semantics in (
            ("relationship", "ordered", "exact"),
            ("one fact row", "observation", "summary", "shares row"),
            ("exact", "fact_path"),
            ("metric_label", "period", "unit", "currency"),
            ("evidence", "exact", "one filing region", "quote"),
            ("no joins", "scaffold", "commentary"),
            ("news", "item-bound"),
            ("language", "portfolio", "trading", "technical", "execution"),
            ("grounded", "monitoring"),
        ):
            self.assertTrue(
                all(term in folded for term in semantics),
                f"combined repair omitted semantics: {semantics}",
            )
        tokens = [
            f"r{relationship}/f{fact}" for relationship, fact in pairs
        ]
        positions = [worst_case.index(token) for token in tokens]
        self.assertEqual(positions, sorted(positions))
        for token in tokens:
            self.assertEqual(worst_case.count(token), 1)
        self.assertLess(len(f"\n{worst_case}"), 700)

        with self._live_aggregation_harness([invalid, corrected]) as harness:
            result = service.analyze_document({}, harness.document_id)

        self.assertEqual(result, {"analysis_id": harness.analysis_id})
        self.assertEqual(harness.stage.call.call_count, 2)
        harness.finalize.assert_called_once()
        self.assertEqual(harness.finalize.call_args.args[0], corrected)
        warnings = harness.stage.add_validation_warnings.call_args.args[0]
        self.assertEqual(len(warnings), len(expected_categories))
        base_prompt = harness.stage.call.call_args_list[0].args[0]
        repair_prompt = harness.stage.call.call_args_list[1].args[0]
        repair_suffix = repair_prompt[len(base_prompt) :]
        self.assertTrue(repair_prompt.startswith(f"{base_prompt}\n"))
        self.assertEqual(
            repair_prompt[len(base_prompt) + 1 :],
            first.exception.correction_requirement,
        )
        self.assertLess(len(repair_suffix), 700)
        for raw_output in (
            "RAW_PRIVATE_VALUE",
            "RAW_PRIVATE_CLAIM_IDENTIFIER",
            "RAW_PRIVATE_UNGROUNDED_EVIDENCE",
            "raw_private_missing_metric",
            "drivers[99]",
            "RAW_PRIVATE_EVENT",
        ):
            self.assertNotIn(raw_output, repair_suffix)


    def test_live_repair_fails_closed_when_second_response_is_still_invalid(self):
        invalid = self._aggregation_payload(corrected=False)
        with self._live_aggregation_harness([invalid, invalid]) as harness:
            with self.assertRaises(service.LLMValidationError) as raised:
                service.analyze_document({}, harness.document_id)

        self.assertIs(type(raised.exception), service.LLMValidationError)
        self.assertIs(
            type(raised.exception.__cause__),
            service.InvestmentValidationError,
        )
        self.assertEqual(
            raised.exception.__cause__.categories,
            (
                service.VALIDATION_JSON_SCHEMA,
                service.VALIDATION_FILING_EVIDENCE,
            ),
        )
        self.assertEqual(harness.stage.call.call_count, 2)
        harness.finalize.assert_not_called()


    def test_nested_object_shape_repair_aggregates_catalyst_evidence_and_succeeds(
        self,
    ):
        expected_categories = (
            service.VALIDATION_JSON_SCHEMA,
            service.VALIDATION_FILING_EVIDENCE,
        )

        for malformed_field in ("classification", "qualitative"):
            with self.subTest(malformed_field=malformed_field):
                corrected = self._aggregation_payload(corrected=True)
                corrected["catalysts"] = [
                    {
                        "trigger": "Demand remained durable",
                        "expected_outcome": "Order volume could remain firm",
                        "horizon": "within the next year",
                        "epistemic_state": "supported",
                        "uncertainty": "The duration of demand is uncertain",
                        "evidence": "Demand remained durable",
                    }
                ]
                invalid = copy.deepcopy(corrected)
                invalid[malformed_field] = []
                invalid["catalysts"][0]["evidence"] = (
                    "RAW_PRIVATE_UNGROUNDED_CATALYST"
                )

                with self.assertRaises(service.InvestmentValidationError) as first:
                    service._validated_investment_facts(
                        json.dumps(invalid),
                        excerpt=(
                            "Capital expenditures were $19B in FY2024 Q4. "
                            "Demand remained durable through the period."
                        ),
                        news_items=service._freeze_json_value([]),
                        deterministic_current=service._freeze_json_value(
                            {
                                "capital_expenditures": {
                                    "value": 19.0,
                                    "unit": "usd_billions",
                                    "currency": "USD",
                                    "period": "FY2024-Q4",
                                }
                            }
                        ),
                        deterministic_prior=service._freeze_json_value({}),
                    )

                self.assertEqual(first.exception.categories, expected_categories)
                correction = first.exception.correction_requirement
                folded = correction.casefold()
                for semantics in (
                    ("correction", "json", "relationship", "ordered", "exact"),
                    ("one fact row", "observation", "summary", "shares row"),
                    ("exact", "fact_path"),
                    ("metric_label", "period", "unit", "currency"),
                    ("evidence", "exact", "one filing region", "quote"),
                    ("no joins", "scaffold", "commentary"),
                    ("news", "item-bound"),
                ):
                    self.assertTrue(
                        all(term in folded for term in semantics),
                        f"nested repair omitted semantics: {semantics}",
                    )
                self.assertLess(len(f"\n{correction}"), 700)
                self.assertNotIn("RAW_PRIVATE_UNGROUNDED_CATALYST", correction)

                with self._live_aggregation_harness(
                    [invalid, corrected]
                ) as harness:
                    result = service.analyze_document({}, harness.document_id)

                self.assertEqual(result, {"analysis_id": harness.analysis_id})
                self.assertEqual(harness.stage.call.call_count, 2)
                base_prompt = harness.stage.call.call_args_list[0].args[0]
                repair_prompt = harness.stage.call.call_args_list[1].args[0]
                self.assertTrue(repair_prompt.startswith(f"{base_prompt}\n"))
                self.assertEqual(
                    repair_prompt[len(base_prompt) + 1 :],
                    first.exception.correction_requirement,
                )
                self.assertLess(len(repair_prompt[len(base_prompt) :]), 700)
                self.assertNotIn(
                    "RAW_PRIVATE_UNGROUNDED_CATALYST",
                    repair_prompt[len(base_prompt) :],
                )
                self.assertEqual(
                    harness.stage.add_validation_warnings.call_args.args[0],
                    [
                        "response was not valid investment JSON",
                        "filing evidence was blank or ungrounded",
                    ],
                )
                harness.finalize.assert_called_once()
                self.assertEqual(harness.finalize.call_args.args[0], corrected)


    def test_nested_object_shape_invalid_second_response_fails_closed(self):
        expected_categories = (
            service.VALIDATION_JSON_SCHEMA,
            service.VALIDATION_FILING_EVIDENCE,
        )

        for malformed_field in ("classification", "qualitative"):
            with self.subTest(malformed_field=malformed_field):
                invalid = self._aggregation_payload(corrected=True)
                invalid[malformed_field] = []
                invalid["catalysts"] = [
                    {
                        "trigger": "Demand remained durable",
                        "expected_outcome": "Order volume could remain firm",
                        "horizon": "within the next year",
                        "epistemic_state": "supported",
                        "uncertainty": "The duration of demand is uncertain",
                        "evidence": "RAW_PRIVATE_UNGROUNDED_CATALYST",
                    }
                ]

                with self._live_aggregation_harness(
                    [invalid, invalid]
                ) as harness:
                    with self.assertRaises(service.LLMValidationError) as raised:
                        service.analyze_document({}, harness.document_id)

                self.assertIs(type(raised.exception), service.LLMValidationError)
                self.assertIs(
                    type(raised.exception.__cause__),
                    service.InvestmentValidationError,
                )
                self.assertEqual(
                    raised.exception.__cause__.categories,
                    expected_categories,
                )
                correction = raised.exception.__cause__.correction_requirement
                folded = correction.casefold()
                for semantics in (
                    ("correction", "json", "relationship", "ordered", "exact"),
                    ("one fact row", "observation", "summary", "shares row"),
                    ("exact", "fact_path"),
                    ("metric_label", "period", "unit", "currency"),
                    ("evidence", "exact", "one filing region", "quote"),
                    ("no joins", "scaffold", "commentary"),
                    ("news", "item-bound"),
                ):
                    self.assertTrue(
                        all(term in folded for term in semantics),
                        f"nested retry repair omitted semantics: {semantics}",
                    )
                self.assertLess(len(f"\n{correction}"), 700)
                self.assertNotIn("RAW_PRIVATE_UNGROUNDED_CATALYST", correction)
                self.assertEqual(harness.stage.call.call_count, 2)
                harness.finalize.assert_not_called()


    def test_analyze_rejects_invalid_repair_against_frozen_facts_before_finalize(
        self,
    ):
        document_id = "55555555-5555-5555-5555-555555555555"
        document = {
            "document_id": document_id,
            "company": "Example Co",
            "symbol": "EX",
            "region": "US",
            "industry": "Technology",
            "document_type": "annual_report",
            "report_date": None,
            "source_url": "https://example.com/report",
            "filename": "report.txt",
            "extracted_text": "Demand remained durable.",
        }
        excerpt = "Demand remained durable."
        deterministic_current = {
            "operating_cash_flow": {"value": 37.2},
        }
        bad_fact = self._row(
            claim_id="late-fact",
            source_kind="fact",
            fact_path="deterministic_current.late_metric.value",
        )
        del bad_fact["quote"]
        invalid_content = json.dumps(self._payload([bad_fact]))

        claim_session = MagicMock()
        claim_session.execute.return_value.fetchone.return_value = (document_id,)
        failure_session = MagicMock()
        stage = MagicMock()
        stage.policy = SimpleNamespace(
            model="openai/gpt-5.6-luna",
            validation_retries=1,
        )
        stage.telemetry = SimpleNamespace(
            tokens_input_total=100,
            tokens_output_total=50,
            cost_usd_total=0.001,
            first_attempt_duration_ms=10,
            validation_retry_duration_ms=10,
            validation_warnings=[],
        )

        def dispatch(_prompt):
            if stage.call.call_count == 2:
                # An adversarial producer mutation after the request was built
                # must not make the same invalid repair resolve.
                deterministic_current["late_metric"] = {"value": 19.0}
            return {"content": invalid_content}

        stage.call.side_effect = dispatch
        with (
            patch.object(service, "_load_document", return_value=document),
            patch.object(
                service,
                "get_session",
                side_effect=[
                    session_context(claim_session),
                    session_context(failure_session),
                ],
            ),
            patch.object(
                service, "_ensure_extracted_text", return_value="stored_document"
            ),
            patch.object(service, "_load_news_context", return_value=[]),
            patch.object(
                service,
                "load_deterministic_facts",
                return_value=(deterministic_current, {}, {}),
            ),
            patch.object(
                service,
                "_load_report_excerpt",
                return_value=(excerpt, "stored_document"),
            ),
            patch.object(service, "LLMStage", return_value=stage),
            patch.object(service, "finalize_investment_analysis") as finalize,
        ):
            with self.assertRaises(service.LLMValidationError) as raised:
                service.analyze_document({}, document_id)

        self.assertIs(type(raised.exception), service.LLMValidationError)
        cause = raised.exception.__cause__
        self.assertIs(type(cause), service.InvestmentValidationError)
        self.assertEqual(cause.category, service.VALIDATION_JSON_SCHEMA)
        self.assertEqual(
            cause.problems,
            [
                (
                    "numeric_claims[0] (claim_id 'late-fact'): fact_path "
                    "'deterministic_current.late_metric.value' does not resolve "
                    "in deterministic current/prior metrics"
                )
            ],
        )
        self.assertEqual(stage.call.call_count, 2)
        base_prompt = stage.call.call_args_list[0].args[0]
        repair_prompt = stage.call.call_args_list[1].args[0]
        self.assertTrue(repair_prompt.startswith(f"{base_prompt}\n"))
        repair_suffix = repair_prompt[len(base_prompt) :]
        folded = repair_suffix.casefold()
        repair_semantic_surface = folded.replace("not prose", "never prose")
        for semantics in (
            ("correction", "json"),
            ("compatible relationships", "required facts", "exact request order"),
            ("atomic", "metric", "value/unit", "period", "basis"),
            ("exact", "fact_path", "metric_label", "period", "unit", "currency"),
            (
                "one fact row per observation",
                "exact summary repeats share one",
            ),
            (
                "no underscores",
                "%",
                "percentage points",
                "format currency",
            ),
            ("finite scalar", "token", "64", "never prose"),
        ):
            self.assertTrue(
                all(term in repair_semantic_surface for term in semantics),
                f"frozen-fact repair omitted JSON semantics: {semantics}",
            )
        self.assertLess(len(repair_suffix), 700)
        self.assertNotIn("late-fact", repair_suffix)
        self.assertNotIn("late_metric", repair_suffix)
        finalize.assert_not_called()




if __name__ == '__main__':
    unittest.main()
