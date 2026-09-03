"""Tests for investment service."""

import copy
import json
import sys
import unittest
from pathlib import Path
from types import MappingProxyType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from investment_service_support import (
    NumericClaimLedgerTestBase,
    investment_report_payload,
)

import investment_service as service


class NumericClaimLedgerTargetValidationTests(NumericClaimLedgerTestBase):
    """Tests for numeric claim ledger target path resolution, schema validation, and signed currency binding."""

    def test_authored_target_path_forms_resolve_equivalently_and_remain_raw(
        self,
    ):
        quote = (
            "Capital expenditures including finance leases were $19 billion "
            "in FY2024 Q4, in line with expectations"
        )
        excerpt = f"Demand remained durable. {quote}."
        summary = "Capital expenditures were $19 billion in FY2024 Q4."
        canonical_paths = set()

        for authored_path in ("summary", "$.summary", "/summary"):
            with self.subTest(path=authored_path):
                payload = self._payload(
                    [
                        self._row(
                            claim_id="capex_path_form",
                            path=authored_path,
                            quote=quote,
                        )
                    ]
                )
                payload["qualitative"]["ai_demand"]["evidence"] = (
                    "Demand remained durable."
                )
                payload["summary"] = summary

                target, resolved = service._resolve_authored_target(
                    payload, authored_path
                )
                self.assertTrue(resolved)
                self.assertEqual(target, summary)
                canonical_paths.add(
                    service._normalize_claim_path(authored_path)
                )

                parsed = service._validated_investment_facts(
                    json.dumps(payload),
                    excerpt=excerpt,
                    news_items=service._freeze_json_value([]),
                    deterministic_current=service._freeze_json_value({}),
                    deterministic_prior=service._freeze_json_value({}),
                )
                self.assertEqual(
                    parsed["numeric_claims"][0]["path"], authored_path
                )

        self.assertEqual(canonical_paths, {"/summary"})


    def test_json_pointer_decodes_strict_escapes(self):
        payload = {
            "a/b": {"c~d": "Revenue rose 12%."},
            "a~1b": "Backlog rose 14%.",
        }
        cases = (
            ("/a~1b/c~0d", "Revenue rose 12%."),
            # ``~01`` is ``~0`` followed by literal ``1``, not a malformed
            # escape or an order-dependent decoding of ``~1``.
            ("/a~01b", "Backlog rose 14%."),
        )

        for authored_path, expected in cases:
            with self.subTest(path=authored_path):
                target, resolved = service._resolve_authored_target(
                    payload, authored_path
                )
                self.assertTrue(resolved)
                self.assertEqual(target, expected)
                self.assertEqual(
                    service._normalize_claim_path(authored_path),
                    authored_path,
                )


    def test_malformed_target_escapes_and_indexes_fail_closed(self):
        payload = {"a/b": {"c~d": "Revenue rose 12%."}, "drivers": ["12%"]}
        malformed_escapes = (
            "/a~",
            "/a~2b/c~0d",
            "/a~~0b/c~0d",
        )
        invalid_indexes = (
            "/drivers/",
            "/drivers/-",
            "/drivers/-1",
            "/drivers/00",
            "/drivers/01",
            "/drivers/one",
            "/drivers/1",
            "drivers[-1]",
            "drivers[01]",
            "drivers[1]",
        )

        for authored_path in malformed_escapes:
            with self.subTest(malformed_escape=authored_path):
                target, resolved = service._resolve_authored_target(
                    payload, authored_path
                )
                self.assertFalse(resolved)
                self.assertIsNone(target)
                self.assertEqual(
                    service._normalize_claim_path(authored_path), ""
                )

        for authored_path in invalid_indexes:
            with self.subTest(invalid_index=authored_path):
                target, resolved = service._resolve_authored_target(
                    payload, authored_path
                )
                self.assertFalse(resolved)
                self.assertIsNone(target)

        for authored_path in ("drivers[0]", "$.drivers[0]", "/drivers/0"):
            with self.subTest(valid_path=authored_path):
                target, resolved = service._resolve_authored_target(
                    payload, authored_path
                )
                self.assertTrue(resolved)
                self.assertEqual(target, "12%")
                self.assertEqual(
                    service._normalize_claim_path(authored_path),
                    "/drivers/0",
                )


    def test_validation_seam_rejects_invalid_authored_target_paths(self):
        excerpt = (
            "Demand remained durable. Capital expenditures including finance "
            "leases were $19 billion, in line with expectations."
        )

        for authored_path in ("/summary~", "/drivers/-", "/drivers/0"):
            with self.subTest(path=authored_path):
                payload = self._payload(
                    [
                        self._row(
                            claim_id="invalid_target",
                            path=authored_path,
                        )
                    ]
                )
                payload["summary"] = "Demand remained durable."

                with self.assertRaises(
                    service.InvestmentValidationError
                ) as raised:
                    service._validated_investment_facts(
                        json.dumps(payload),
                        excerpt=excerpt,
                        news_items=service._freeze_json_value([]),
                        deterministic_current=service._freeze_json_value({}),
                        deterministic_prior=service._freeze_json_value({}),
                    )

                self.assertTrue(
                    any(
                        authored_path in problem
                        and "does not resolve" in problem
                        for problem in raised.exception.problems
                    ),
                    raised.exception.problems,
                )


    def test_live_validation_limits_qualitative_evidence_targets_to_known_signals(
        self,
    ):
        excerpt = (
            "Demand remained durable. Capital expenditures including finance "
            "leases were $19 billion in FY2024 Q4, in line with expectations."
        )
        frozen_news = service._freeze_json_value([])
        deterministic = service._freeze_json_value({})
        quote = (
            "Capital expenditures including finance leases were $19 billion "
            "in FY2024 Q4, in line with expectations"
        )

        known = self._payload(
            [
                self._row(
                    claim_id="known-signal-evidence",
                    path="/qualitative/ai_demand/evidence",
                    quote=quote,
                )
            ]
        )
        known["summary"] = "Demand remained durable."
        known["qualitative"]["ai_demand"]["evidence"] = quote
        parsed = service._validated_investment_facts(
            json.dumps(known),
            excerpt=excerpt,
            news_items=frozen_news,
            deterministic_current=deterministic,
            deterministic_prior=deterministic,
        )
        self.assertEqual(
            parsed["numeric_claims"][0]["path"],
            "/qualitative/ai_demand/evidence",
        )

        forged = self._payload(
            [
                self._row(
                    claim_id="forged-signal-evidence",
                    path="/qualitative/forged_signal/evidence",
                    quote=quote,
                )
            ]
        )
        forged["summary"] = "Demand remained durable."
        forged["qualitative"]["forged_signal"] = {
            "present": True,
            "strength": "strong",
            "evidence": quote,
        }
        with self.assertRaises(service.InvestmentValidationError) as raised:
            service._validated_investment_facts(
                json.dumps(forged),
                excerpt=excerpt,
                news_items=frozen_news,
                deterministic_current=deterministic,
                deterministic_prior=deterministic,
            )

        self.assertEqual(
            raised.exception.category,
            service.VALIDATION_JSON_SCHEMA,
        )
        self.assertEqual(
            raised.exception.problems,
            [
                "$.qualitative: unexpected property 'forged_signal'",
                (
                    "numeric_claims[0] (claim_id 'forged-signal-evidence'): "
                    "text source tuple does not match its authored target and "
                    "bound producer quote: target path "
                    "'/qualitative/forged_signal/evidence' does not resolve to "
                    "an eligible narrative text leaf"
                ),
            ],
        )


    def test_production_validation_rejects_semantically_invalid_ledger_rows(self):
        excerpt = (
            "Demand remained durable. Capital expenditures including finance "
            "leases were $19 billion, in line with expectations."
        )
        deterministic_current = service._freeze_json_value(
            {
                "operating_cash_flow": {
                    "value": 37.2,
                    "unit": "usd_billions",
                    "currency": "USD",
                    "period": "FY2024 Q4",
                },
                "cash_paid_for_property_and_equipment": {
                    "value": 13.9,
                    "unit": "usd_billions",
                    "currency": "USD",
                    "period": "FY2024 Q4",
                },
            }
        )
        deterministic_prior = service._freeze_json_value({})
        frozen_news = service._freeze_json_value(
            [{"title": "Company update", "summary": "No additional figures."}]
        )

        bad_fact = self._row(
            claim_id="bad-fact-path",
            source_kind="fact",
            fact_path="deterministic_current.missing.value",
        )
        arithmetic = self._row(
            claim_id="undeclared-arithmetic",
            metric="free cash flow",
            value=23.3,
            source_kind="arithmetic",
            operation="difference",
            operands=[
                "deterministic_current.operating_cash_flow.value",
                (
                    "deterministic_current."
                    "cash_paid_for_property_and_equipment.value"
                ),
            ],
        )
        del bad_fact["quote"], arithmetic["quote"]
        cases = {
            "bad fact path": (
                bad_fact,
                (
                    "numeric_claims[0] (claim_id 'bad-fact-path'): fact_path "
                    "'deterministic_current.missing.value' does not resolve in "
                    "deterministic current/prior metrics"
                ),
            ),
            "non-verbatim quote": (
                self._row(
                    claim_id="non-verbatim-quote",
                    quote=(
                        "Capital expenditures including finance leases were "
                        "exactly $19 billion"
                    ),
                ),
                (
                    "numeric_claims[0] (claim_id 'non-verbatim-quote'): "
                    "quote is not verbatim inside any single producer-visible "
                    "surface (filing excerpt or recorded news item)"
                ),
            ),
            "orphan target": (
                self._row(claim_id="orphan-target", path="drivers[0]"),
                (
                    "numeric_claims[0] (claim_id 'orphan-target'): text source "
                    "tuple does not match its authored target and bound "
                    "producer quote: target path 'drivers[0]' does not resolve "
                    "to an eligible narrative text leaf"
                ),
            ),
            "undeclared arithmetic": (
                arithmetic,
                (
                    "numeric_claims[0] (claim_id 'undeclared-arithmetic'): "
                    "arithmetic is not producer-declared: no producer-derived "
                    "output fact declares this operation and operands"
                ),
            ),
        }

        for label, (row, expected_problem) in cases.items():
            with self.subTest(case=label):
                payload = self._payload([row])
                if label == "orphan target":
                    payload["summary"] = (
                        "Capital investment remained in line with expectations."
                    )
                if label == "undeclared arithmetic":
                    payload["summary"] = (
                        "FY2024 Q4 free cash flow was $23.3B."
                    )
                with self.assertRaises(
                    service.InvestmentValidationError
                ) as raised:
                    service._validated_investment_facts(
                        json.dumps(payload),
                        excerpt=excerpt,
                        news_items=frozen_news,
                        deterministic_current=deterministic_current,
                        deterministic_prior=deterministic_prior,
                    )
                self.assertIs(
                    type(raised.exception), service.InvestmentValidationError
                )
                self.assertEqual(
                    raised.exception.category, service.VALIDATION_JSON_SCHEMA
                )
                self.assertEqual(raised.exception.problems, [expected_problem])


    def test_live_validation_requires_exact_declared_operands_and_order(self):
        deterministic_current = service._freeze_json_value(
            {
                "operating_cash_flow": {
                    "value": 37.2,
                    "unit": "usd_billions",
                    "currency": "USD",
                    "period": "FY2024-Q4",
                },
                "capital_expenditures": {
                    "value": 19.0,
                    "unit": "usd_billions",
                    "currency": "USD",
                    "period": "FY2024-Q4",
                },
                "capital_expenditures_including_finance_leases": {
                    "value": 19.0,
                    "unit": "usd_billions",
                    "currency": "USD",
                    "period": "FY2024-Q4",
                },
                "revenue": {
                    "value": 20.0,
                    "unit": "usd_billions",
                    "currency": "USD",
                    "period": "FY2024-Q4",
                },
                "employees": {
                    "value": 10.0,
                    "unit": "count",
                    "currency": None,
                    "period": "FY2024-Q4",
                },
                "revenue_per_employee": {
                    "value": 2.0,
                    "unit": "ratio",
                    "currency": None,
                    "period": "FY2024-Q4",
                    "source": "derived",
                    "concept": "derived: revenue / employees",
                },
                "free_cash_flow": {
                    "value": 18.2,
                    "unit": "usd_billions",
                    "currency": "USD",
                    "period": "FY2024-Q4",
                    "source": "derived",
                    "concept": (
                        "derived: operating_cash_flow - capital_expenditures"
                    ),
                },
            }
        )
        operand_cases = {
            "near-name operand": {
                "metric": "free cash flow",
                "value": 18.2,
                "operation": "difference",
                "operands": [
                    "deterministic_current.operating_cash_flow.value",
                    (
                        "deterministic_current."
                        "capital_expenditures_including_finance_leases.value"
                    ),
                ],
            },
            "reversed difference": {
                "metric": "free cash flow",
                "value": -18.2,
                "operation": "difference",
                "operands": [
                    "deterministic_current.capital_expenditures.value",
                    "deterministic_current.operating_cash_flow.value",
                ],
            },
            "reversed quotient": {
                "metric": "revenue per employee",
                "value": 0.5,
                "operation": "quotient",
                "operands": [
                    "deterministic_current.employees.value",
                    "deterministic_current.revenue.value",
                ],
            },
        }

        for label, overrides in operand_cases.items():
            with self.subTest(case=label):
                row = self._row(
                    claim_id="declared_identity",
                    source_kind="arithmetic",
                    **overrides,
                )
                del row["quote"]
                payload = self._payload([row])
                rendered = (
                    f"${row['value']}B"
                    if row["unit"] == "usd_billions"
                    else str(row["value"])
                )
                payload["summary"] = (
                    f"FY2024 Q4 {row['metric']} was {rendered}."
                )
                with self.assertRaises(
                    service.InvestmentValidationError
                ) as raised:
                    service._validated_investment_facts(
                        json.dumps(payload),
                        excerpt="Demand remained durable.",
                        news_items=service._freeze_json_value([]),
                        deterministic_current=deterministic_current,
                        deterministic_prior=service._freeze_json_value({}),
                    )
                self.assertEqual(
                    raised.exception.category, service.VALIDATION_JSON_SCHEMA
                )
                self.assertEqual(
                    raised.exception.problems,
                    [
                        (
                            "numeric_claims[0] (claim_id "
                            "'declared_identity'): "
                            "arithmetic is not producer-declared: no "
                            "producer-derived output fact declares this "
                            "operation and operands"
                        )
                    ],
                )


    def test_live_validation_accepts_exact_and_commutative_declared_operands(
        self,
    ):
        deterministic_current = service._freeze_json_value(
            {
                "operating_cash_flow": {
                    "value": 37.2,
                    "unit": "usd_billions",
                    "currency": "USD",
                    "period": "FY2024-Q4",
                },
                "capital_expenditures": {
                    "value": 19.0,
                    "unit": "usd_billions",
                    "currency": "USD",
                    "period": "FY2024-Q4",
                },
                "scaling_ratio": {
                    "value": 2.0,
                    "unit": "ratio",
                    "currency": None,
                    "period": "FY2024-Q4",
                },
                "free_cash_flow": {
                    "value": 18.2,
                    "unit": "usd_billions",
                    "currency": "USD",
                    "period": "FY2024-Q4",
                    "source": "derived",
                    "concept": (
                        "derived: operating_cash_flow - capital_expenditures"
                    ),
                },
                "cash_investment_total": {
                    "value": 56.2,
                    "unit": "usd_billions",
                    "currency": "USD",
                    "period": "FY2024-Q4",
                    "source": "derived",
                    "concept": (
                        "derived: operating_cash_flow + capital_expenditures"
                    ),
                },
                "scaled_capex": {
                    "value": 38.0,
                    "unit": "usd_billions",
                    "currency": "USD",
                    "period": "FY2024-Q4",
                    "source": "derived",
                    "concept": (
                        "derived: capital_expenditures * scaling_ratio"
                    ),
                },
            }
        )
        rows = [
            self._row(
                claim_id="exact_difference",
                path="summary",
                metric="free cash flow",
                value=18.2,
                source_kind="arithmetic",
                operation="difference",
                operands=[
                    "deterministic_current.operating_cash_flow.value",
                    "deterministic_current.capital_expenditures.value",
                ],
            ),
            self._row(
                claim_id="reordered_sum",
                path="drivers[0]",
                metric="cash investment total",
                value=56.2,
                source_kind="arithmetic",
                operation="sum",
                operands=[
                    "deterministic_current.capital_expenditures.value",
                    "deterministic_current.operating_cash_flow.value",
                ],
            ),
            self._row(
                claim_id="reordered_product",
                path="drivers[1]",
                metric="scaled capex",
                value=38.0,
                source_kind="arithmetic",
                operation="product",
                operands=[
                    "deterministic_current.scaling_ratio.value",
                    "deterministic_current.capital_expenditures.value",
                ],
            ),
        ]
        for row in rows:
            del row["quote"]
        payload = self._payload(rows)
        payload["summary"] = "FY2024 Q4 free cash flow was $18.2B."
        payload["drivers"] = [
            "FY2024 Q4 cash investment total was $56.2B.",
            "FY2024 Q4 scaled capex was $38B.",
        ]

        parsed = service._validated_investment_facts(
            json.dumps(payload),
            excerpt="Demand remained durable.",
            news_items=service._freeze_json_value([]),
            deterministic_current=deterministic_current,
            deterministic_prior=service._freeze_json_value({}),
        )

        self.assertEqual(parsed, payload)


    def test_production_validation_requires_ledger_only_for_material_numbers(self):
        quote = (
            "Capital expenditures including finance leases were $19 billion "
            "in FY2024 Q4, in line with expectations"
        )
        excerpt = f"Demand remained durable. {quote}."
        frozen_current = service._freeze_json_value({})
        frozen_prior = service._freeze_json_value({})
        frozen_news = service._freeze_json_value([])
        absent = self._payload(None)
        del absent["numeric_claims"]
        empty = self._payload([])

        for label, payload in (("absent", absent), ("empty", empty)):
            with self.subTest(ledger=label):
                with self.assertRaises(
                    service.InvestmentValidationError
                ) as raised:
                    service._validated_investment_facts(
                        json.dumps(payload),
                        excerpt=excerpt,
                        news_items=frozen_news,
                        deterministic_current=frozen_current,
                        deterministic_prior=frozen_prior,
                    )
                self.assertEqual(
                    raised.exception.categories,
                    (service.VALIDATION_JSON_SCHEMA,),
                )
                self.assertEqual(
                    raised.exception.problems,
                    [
                        "summary: material numeric token '19' has no "
                        "numeric_claims binding"
                    ],
                )

        numeric_free = self._payload([])
        numeric_free["summary"] = "Demand remained durable while supply stayed tight."
        parsed = service._validated_investment_facts(
            json.dumps(numeric_free),
            excerpt=excerpt,
            news_items=frozen_news,
            deterministic_current=frozen_current,
            deterministic_prior=frozen_prior,
        )
        self.assertEqual(parsed, numeric_free)

        valid = self._payload(
            [self._row(claim_id="grounded-text", quote=quote)]
        )
        valid["summary"] = (
            "Capital expenditures including finance leases were $19 billion "
            "in FY2024 Q4."
        )
        parsed = service._validated_investment_facts(
            json.dumps(valid),
            excerpt=excerpt,
            news_items=frozen_news,
            deterministic_current=frozen_current,
            deterministic_prior=frozen_prior,
        )
        self.assertEqual(parsed, valid)


    def test_live_validation_rejects_exposure_sizing_instructions(self):
        frozen = service._freeze_json_value({})
        for instruction in (
            "Size exposure to allow for a slower capacity unlock.",
            "Reduce portfolio exposure ahead of earnings.",
        ):
            with self.subTest(instruction=instruction):
                payload = self._payload([])
                payload["summary"] = "Demand remained durable."
                payload["watch_items"] = [instruction]

                with self.assertRaises(
                    service.InvestmentValidationError
                ) as raised:
                    service._validated_investment_facts(
                        json.dumps(payload),
                        excerpt="Demand remained durable.",
                        news_items=service._freeze_json_value([]),
                        deterministic_current=frozen,
                        deterministic_prior=frozen,
                    )
                self.assertEqual(
                    raised.exception.categories,
                    (service.VALIDATION_PROHIBITED_LANGUAGE,),
                )

                self.assertEqual(
                    raised.exception.problems,
                    ["response contained prohibited advisory language"],
                )
                self.assertNotIn(instruction, str(raised.exception))


    def test_live_validation_allows_monitoring_and_company_descriptions(self):
        payload = self._payload([])
        payload["summary"] = "Demand remained durable."
        payload["watch_items"] = [
            "Monitor inventory levels for signs of oversupply.",
            "Customer exposure remains concentrated among large enterprises.",
            "Company capital allocation remains focused on data centers.",
        ]
        frozen = service._freeze_json_value({})

        parsed = service._validated_investment_facts(
            json.dumps(payload),
            excerpt="Demand remained durable.",
            news_items=service._freeze_json_value([]),
            deterministic_current=frozen,
            deterministic_prior=frozen,
        )

        self.assertEqual(parsed, payload)


    def test_response_schema_declares_optional_bounded_ledger(self):
        schema = service._response_schema()["schema"]
        claims = schema["properties"]["numeric_claims"]
        self.assertEqual(claims["type"], "array")
        self.assertLessEqual(claims["maxItems"], 40)
        item = claims["items"]
        self.assertEqual(item["type"], "object")
        self.assertFalse(item["additionalProperties"])
        for key in (
            "claim_id",
            "path",
            "value",
            "metric",
            "period",
            "unit",
            "currency",
            "source_kind",
        ):
            self.assertIn(key, item["required"])
        # The ledger is OPTIONAL: absence and [] both validate, so the
        # root schema must not declare numeric_claims required.
        self.assertNotIn("numeric_claims", schema["required"])
        absent = investment_report_payload()
        # The shared model helper ships an empty ledger by default; absence
        # is constructed explicitly here so the assertion stays honest.
        del absent["numeric_claims"]
        self.assertNotIn("numeric_claims", absent)
        self.assertEqual(service.validate_investment_report_payload(absent), [])
        self.assertEqual(
            service.validate_investment_report_payload(self._payload([])), []
        )


    def test_empty_ledger_with_numeric_free_narrative_validates(self):
        payload = investment_report_payload()
        payload["numeric_claims"] = []
        self.assertEqual(service.validate_investment_report_payload(payload), [])


    def test_malformed_rows_fail_closed_in_validation_seam(self):
        cases = {
            "not-an-object": ["nope"],
            "unknown-source-kind": [self._row(source_kind="vibes")],
            "text-with-fact-keys": [
                self._row(fact_path="deterministic_current.capex.value")
            ],
            "fact-with-quote": [
                self._row(source_kind="fact", fact_path="deterministic_current.revenue.value")
            ],
            "arithmetic-needs-operation-and-operands": [
                self._row(
                    source_kind="arithmetic",
                    operands=["deterministic_current.operating_cash_flow.value"],
                )
            ],
            "duplicate-claim-id": [
                self._row(),
                self._row(path="thesis"),
            ],
            "nonfinite-value": [self._row(value="19e999999")],
            "bad-unit": [self._row(unit="usd_gazillions")],
        }
        for label, rows in cases.items():
            with self.subTest(case=label):
                problems = service.validate_investment_report_payload(
                    self._payload(rows)
                )
                self.assertTrue(problems, f"{label} must be rejected")
                self.assertTrue(
                    any("numeric_claims" in problem for problem in problems),
                    problems,
                )


    def test_signed_currency_row_schema_accepts_one_adjacent_sign_only(self):
        fact_path = (
            "deterministic_current.relationship_facts."
            "cloud-margin-eps-impact"
        )
        for value in ("-$0.06", "$-0.06"):
            with self.subTest(value=value):
                row = self._row(
                    claim_id=f"signed-currency-{value}",
                    value=value,
                    metric="diluted earnings per share impact",
                    period="FY2025-Q1",
                    unit="usd_per_share",
                    source_kind="fact",
                    fact_path=fact_path,
                )
                del row["quote"]
                self.assertEqual(service.validate_numeric_claim_rows([row]), [])

        malformed = {
            "double sign around currency": "-$-0.06",
            "double leading sign": "--$0.06",
            "double currency": "$$0.06",
            "space after sign": "- $0.06",
            "space after currency": "$ -0.06",
        }
        for label, value in malformed.items():
            with self.subTest(case=label, value=value):
                row = self._row(value=value)
                self.assertEqual(
                    service.validate_numeric_claim_rows([row]),
                    [
                        (
                            "numeric_claims[0]: value must be a finite number "
                            'or a numeric string (e.g. "19", "$19B", "28%") '
                            "of at most 64 characters"
                        )
                    ],
                )


    def test_signed_currency_target_binding_preserves_negative_sign(self):
        fact_path = (
            "deterministic_current.relationship_facts."
            "cloud-margin-eps-impact"
        )
        relationship_fact = {
            "value": -0.06,
            "unit": "usd_per_share",
            "currency": "USD",
            "period": "FY2025-Q1",
            "metric_label": "diluted earnings per share impact",
            "metric_key": "diluted_earnings_per_share_impact",
            "source": "reported",
            "evidence": ["The diluted earnings per share impact was $0.06."],
        }

        for target_value in ("-$0.06", "$-0.06"):
            for row_value in ("-$0.06", "$-0.06"):
                with self.subTest(target=target_value, row=row_value):
                    row = self._row(
                        claim_id="signed-eps-impact",
                        value=row_value,
                        metric=relationship_fact["metric_label"],
                        period=relationship_fact["period"],
                        unit=relationship_fact["unit"],
                        currency=relationship_fact["currency"],
                        source_kind="fact",
                        fact_path=fact_path,
                    )
                    del row["quote"]
                    payload = self._payload([row])
                    payload["summary"] = (
                        "Diluted earnings per share impact was "
                        f"{target_value} per share in FY2025-Q1."
                    )
                    self.assertEqual(
                        service.numeric_claim_source_problems(
                            payload,
                            deterministic_current={},
                            deterministic_prior={},
                            relationship_facts={
                                "cloud-margin-eps-impact": relationship_fact
                            },
                        ),
                        [],
                    )


    def test_signed_currency_target_binding_rejects_malformed_and_wrong_signs(self):
        fact_path = (
            "deterministic_current.relationship_facts."
            "cloud-margin-eps-impact"
        )
        cases = (
            ("double sign", "-$-0.06", "-$0.06", -0.06, None),
            ("double currency", "$$0.06", "$0.06", 0.06, None),
            (
                "separated sign before currency",
                "- $0.06",
                "-$0.06",
                -0.06,
                None,
            ),
            ("separated sign after currency", "$ -0.06", "$-0.06", -0.06, None),
            ("sign loss", "-$0.06", "$0.06", 0.06, "-0.06"),
            (
                "positive target with negative row",
                "$0.06",
                "$-0.06",
                -0.06,
                "0.06",
            ),
        )
        for label, target_value, row_value, fact_value, unbound in cases:
            with self.subTest(case=label):
                relationship_fact = {
                    "value": fact_value,
                    "unit": "usd_per_share",
                    "currency": "USD",
                    "period": "FY2025-Q1",
                    "metric_label": "diluted earnings per share impact",
                    "metric_key": "diluted_earnings_per_share_impact",
                    "source": "reported",
                    "evidence": ["Diluted earnings per share impact."],
                }
                row = self._row(
                    claim_id="signed-eps-impact-mismatch",
                    value=row_value,
                    metric=relationship_fact["metric_label"],
                    period=relationship_fact["period"],
                    unit=relationship_fact["unit"],
                    currency=relationship_fact["currency"],
                    source_kind="fact",
                    fact_path=fact_path,
                )
                del row["quote"]
                payload = self._payload([row])
                payload["summary"] = (
                    "Diluted earnings per share impact was "
                    f"{target_value} per share in FY2025-Q1."
                )
                expected = [
                    (
                        "numeric_claims[0] (claim_id "
                        "'signed-eps-impact-mismatch'): fact source tuple "
                        "does not match its authored target and "
                        "deterministic leaf"
                    )
                ]
                if unbound is not None:
                    expected.append(
                        "summary: material numeric token "
                        f"{unbound!r} has no numeric_claims binding"
                    )
                self.assertEqual(
                    service.numeric_claim_source_problems(
                        payload,
                        deterministic_current={},
                        deterministic_prior={},
                        relationship_facts={
                            "cloud-margin-eps-impact": relationship_fact
                        },
                    ),
                    expected,
                )


    def test_supported_renderings_and_kinds_validate_cleanly(self):
        fact_row = self._row(
            claim_id="capex_fact",
            source_kind="fact",
            fact_path="deterministic_current.capital_expenditures_including_finance_leases.value",
            value=19.0,
        )
        arithmetic_row = self._row(
            claim_id="fcf_identity",
            source_kind="arithmetic",
            operation="difference",
            operands=[
                "deterministic_current.operating_cash_flow.value",
                "deterministic_current.cash_paid_for_property_and_equipment.value",
            ],
            value=23.3,
        )
        # Kind-exclusive shapes: non-text rows never carry a quote key.
        del fact_row["quote"], arithmetic_row["quote"]
        rows = [
            self._row(),
            # Equivalent renderings of one quantity normalize: $19B /
            # 19 billion / 19 are the same canonical value.
            self._row(claim_id="capex_words", path="thesis", value="19 billion"),
            fact_row,
            arithmetic_row,
        ]
        self.assertEqual(service.validate_investment_report_payload(self._payload(rows)), [])


    def test_source_resolution_rejects_orphan_pointers_and_forged_quotes(self):
        deterministic_current = {
            "operating_cash_flow": {
                "value": 37.2,
                "unit": "usd_billions",
                "currency": "USD",
                "period": "FY2024 Q4",
            },
            "cash_paid_for_property_and_equipment": {"value": 13.9},
        }
        facts = self._payload(
            [
                # Target path does not exist inside this payload.
                self._row(claim_id="orphan-target", path="drivers[0]"),
                # Quote is not verbatim producer text.
                self._row(claim_id="forged-quote", quote="capex was exactly $19.0 billion"),
                # Fact pointer names no root / no existing metric.
                self._row(
                    claim_id="missing-fact",
                    source_kind="fact",
                    fact_path="deterministic_current.free_cash_flow.value",
                ),
                # Arithmetic operand missing from the deterministic facts.
                self._row(
                    claim_id="missing-operand",
                    source_kind="arithmetic",
                    operation="sum",
                    operands=[
                        "deterministic_current.operating_cash_flow.value",
                        "deterministic_current.gone.value",
                    ],
                ),
            ]
        )
        problems = service.numeric_claim_source_problems(
            facts, deterministic_current=deterministic_current, deterministic_prior={}
        )
        labels = " ".join(problems)
        for claim_id in ("orphan-target", "forged-quote", "missing-fact", "missing-operand"):
            self.assertIn(claim_id, labels)
        # Four rows produce four primary source problems, each naming its
        # claim_id.
        self.assertEqual(len(problems), 4)
        # The same rows against resolved sources produce no problems beyond
        # the genuinely unresolvable target/pointer ones.
        ok = self._payload(
            [
                self._row(
                    claim_id="real",
                    path="summary",
                    quote=(
                        "Capital expenditures including finance leases were "
                        "$19 billion in FY2024 Q4"
                    ),
                ),
            ]
        )
        # The quote check reads producer-visible surfaces carried on the
        # payload itself (source_excerpt / news_context), exactly as the
        # hard gate resolves them from the frozen case.
        ok["source_excerpt"] = (
            "Capital expenditures including finance leases were $19 "
            "billion in FY2024 Q4, in line with expectations."
        )
        self.assertEqual(
            service.numeric_claim_source_problems(
                ok,
                deterministic_current=deterministic_current,
                deterministic_prior={},
            ),
            [],
        )


    def test_finalization_copies_ledger_into_facts_and_analysis(self):
        rows = [self._row()]
        parsed = investment_report_payload()
        parsed["summary"] = (
            "Capital expenditures including finance leases were $19 billion "
            "in FY2024 Q4."
        )
        parsed["numeric_claims"] = rows
        result = service.finalize_investment_analysis(
            parsed,
            document={
                "company": "Example Company",
                "symbol": "EX",
                "region": "US",
                "industry": "Industrial Technology",
                "document_type": "earnings_transcript",
            },
            deterministic_current={},
            deterministic_prior={},
            market_inputs={},
            stored_previous_facts={},
            previous_state=None,
            prior_count=0,
            news_items=[],
            extraction={},
            relationship_facts={},
            material_relationships=[],
        )
        self.assertEqual(result.facts.get("numeric_claims"), rows)
        self.assertEqual(result.analysis.get("numeric_claims"), rows)
        # Caller-side mutation after finalization cannot alter the record:
        # finalization took deep ownership of every input that reaches it.
        rows[0]["value"] = "$20B"
        rows.append("poison")
        self.assertEqual(result.facts["numeric_claims"][0]["value"], "$19B")
        self.assertEqual(len(result.analysis["numeric_claims"]), 1)


    def test_repeated_finalization_is_stable_over_the_ledger(self):
        def build():
            payload = investment_report_payload()
            payload["numeric_claims"] = [self._row()]
            return copy.deepcopy(payload)

        first = service.finalize_investment_analysis(
            build(),
            document={"region": "US", "document_type": "annual report"},
            deterministic_current={},
            deterministic_prior={},
            market_inputs={},
            stored_previous_facts={},
            previous_state=None,
            prior_count=0,
            news_items=[],
            extraction={},
            relationship_facts={},
            material_relationships=[],
        )
        second = service.finalize_investment_analysis(
            build(),
            document={"region": "US", "document_type": "annual report"},
            deterministic_current={},
            deterministic_prior={},
            market_inputs={},
            stored_previous_facts={},
            previous_state=None,
            prior_count=0,
            news_items=[],
            extraction={},
            relationship_facts={},
            material_relationships=[],
        )
        self.assertEqual(first.facts["numeric_claims"], second.facts["numeric_claims"])


    def test_request_schema_freeze_covers_the_ledger_declaration(self):
        request = service.build_investment_analysis_request(
            {"company": "Example Company", "document_id": "doc-1"},
            "Demand remained durable.",
            [],
            {},
            {},
        )
        stored = request.schema
        properties = stored["properties"]
        self.assertIsInstance(properties, MappingProxyType)
        self.assertIn("numeric_claims", properties)
        declaration = properties["numeric_claims"]
        self.assertIsInstance(declaration, MappingProxyType)
        with self.assertRaises(TypeError):
            declaration["maxItems"] = 1
        items = declaration["items"]
        with self.assertRaises(TypeError):
            items["required"] = []
        # Mutating a freshly built plain schema must never diverge the
        # already-built request's identity.
        fingerprint = request.fingerprint
        source = service._response_schema()
        source["schema"]["properties"]["numeric_claims"]["items"]["required"] = []
        self.assertEqual(request.fingerprint, fingerprint)
        self.assertEqual(
            list(stored["properties"]["numeric_claims"]["items"]["required"]),
            [
                "claim_id",
                "path",
                "value",
                "metric",
                "period",
                "unit",
                "currency",
                "source_kind",
            ],
        )




if __name__ == '__main__':
    unittest.main()
