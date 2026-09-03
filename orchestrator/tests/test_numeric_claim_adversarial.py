"""Numeric claim binding tests: adversarial bindings, scanners, exemptions, and malformed rows."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from company_quality_support import (
    MSFT_EXCERPT,
    NumericClaimBindingTestBase,
    epistemic_catalyst,
    finalized_for,
    msft_claim_row,
    narrative_payload,
)

import investment_service as service
from research_intelligence import company_quality as cq


class NumericClaimAdversarialGateTests(NumericClaimBindingTestBase):
    """Adversarial tests: passing token-presence gate while failing semantic binding, scanners, exemptions."""

    def test_same_number_different_metric_fails(self):
        # Azure's real 29% growth re-authored as an unsupported dollar
        # amount: the token 29 exists in the packet, so the old token gate
        # passed this; the tuple does not.
        report = self._run(
            summary="Capital expenditures were $29 billion in FY2024 Q4.",
            rows=[
                msft_claim_row(
                    claim_id="bad_metric",
                    value="$29B",
                    metric="capital expenditures including finance leases",
                    quote=(
                        "Azure and other cloud services revenue grew 29% "
                        "and 30% in constant currency"
                    ),
                )
            ],
        )
        self.assertFalse(report.passed)
        failures = self._codes(report, "numeric_claim_tuple_mismatch")
        self.assertEqual(len(failures), 1, report.failures)


    def test_same_metric_different_period_fails(self):
        # The $19B figure belongs to FY2024 Q4; claiming it for the guided
        # quarter (Q1 FY2025) fails even though every token matches.
        report = self._run_with_json_replay(
            summary=(
                "Capital expenditures including finance leases were $19 "
                "billion in Q1 FY2025."
            ),
            rows=[
                msft_claim_row(
                    claim_id="bad_period",
                    period="FY2025 Q1",
                )
            ],
        )
        self.assertFalse(report.passed)
        self.assertEqual(
            [failure.code for failure in report.failures],
            ["numeric_claim_tuple_mismatch"],
            report.failures,
        )


    def test_percent_value_reused_as_currency_amount_fails(self):
        report = self._run(
            summary="Free cash flow rose to $23.3 billion in FY2024 Q4 on a 23% gain.",
            rows=[
                msft_claim_row(
                    claim_id="pct_as_usd",
                    value="$23B",
                    metric="free cash flow year-over-year growth",
                    unit="usd_billions",
                    quote="Free cash flow was $23.3 billion, up 18%",
                )
            ],
        )
        self.assertFalse(report.passed)
        codes = {failure.code for failure in report.failures}
        self.assertTrue(
            codes & {"numeric_claim_tuple_mismatch", "numeric_claim_unbound"},
            report.failures,
        )

        # Economically different quantity: $13.9 million claimed against a
        # source that says $13.9 billion. The numerals match; the declared
        # magnitude does not, so the row must fail tuple verification.
        report = self._run(
            summary=(
                "Cash paid for property and equipment was $13.9 million "
                "in FY2024 Q4."
            ),
            rows=[
                msft_claim_row(
                    claim_id="scale_confusion",
                    value="$13.9M",
                    unit="usd_millions",
                    metric="cash paid for property and equipment",
                    quote=(
                        "cash paid for P, P, and E was $13.9 billion "
                        "in FY2024 Q4"
                    ),
                )
            ],
        )
        self.assertFalse(report.passed)


    def test_unsupported_20b_capex_amid_unrelated_20s_fails(self):
        # THE false-pass of the old gate: "$20 billion" capex, where 20 only
        # ever appears in unrelated producer text ($20.3 billion segment
        # revenue). Token presence cannot ground it; the ledger has no row.
        excerpt = (
            "Revenue from Productivity and Business Processes was $20.3 "
            "billion. Capital expenditures including finance leases were "
            "$19 billion, in line with expectations."
        )
        report = self._run(
            producer=self._producer(excerpt=excerpt),
            summary=(
                "Capital expenditures including finance leases were $20 "
                "billion in FY2024 Q4."
            ),
            rows=[],
        )
        self.assertFalse(report.passed)
        unbound = self._codes(report, "numeric_claim_unbound")
        self.assertGreaterEqual(len(unbound), 1)
        self.assertTrue(any("20" in str(failure.observed) for failure in unbound))


    def test_wrong_capex_number_with_full_ledger_still_fails(self):
        # Even a well-formed ledger row cannot launder a wrong number: the
        # row's tuple must match its own cited source span.
        report = self._run(
            summary=(
                "Capital expenditures including finance leases were $20 "
                "billion in FY2024 Q4."
            ),
            rows=[
                msft_claim_row(claim_id="wrong_value", value="$20B"),
            ],
        )
        self.assertFalse(report.passed)
        self.assertEqual(len(self._codes(report, "numeric_claim_tuple_mismatch")), 1)


    def test_forged_source_pointer_text_and_missing_orphan_rows_fail(self):
        # Forged quote: not verbatim anywhere in the packet.
        forged_quote = msft_claim_row(
            claim_id="forged_quote",
            quote="Capex was exactly nineteen billion US dollars",
        )
        report = self._run(
            summary=(
                "Capital expenditures including finance leases were $19 "
                "billion in FY2024 Q4."
            ),
            rows=[forged_quote],
        )
        self.assertFalse(report.passed)
        self.assertEqual(len(self._codes(report, "numeric_claim_source_unresolved")), 1)
        # Orphan target: the bound path does not exist in this output.
        orphan = msft_claim_row(claim_id="orphan_target", path="drivers[0]")
        report = self._run(
            summary=(
                "Capital expenditures including finance leases were $19 "
                "billion in FY2024 Q4."
            ),
            rows=[orphan],
        )
        self.assertFalse(report.passed)
        self.assertEqual(len(self._codes(report, "numeric_claim_target_missing")), 1)
        # Unresolvable fact pointer.
        missing_fact = {
            key: value
            for key, value in msft_claim_row(
                claim_id="missing_fact", source_kind="fact"
            ).items()
            if key != "quote"
        }
        missing_fact["fact_path"] = "deterministic_current.free_cash_flow.value"
        report = self._run(
            summary=(
                "Capital expenditures including finance leases were $19 "
                "billion in FY2024 Q4."
            ),
            rows=[missing_fact],
        )
        self.assertFalse(report.passed)
        self.assertEqual(len(self._codes(report, "numeric_claim_source_unresolved")), 1)


    def test_duplicate_binding_rows_fail(self):
        report = self._run(
            summary=(
                "Capital expenditures including finance leases were $19 "
                "billion in FY2024 Q4."
            ),
            rows=[
                msft_claim_row(),
                msft_claim_row(claim_id="capex_fy24q4_dup"),
            ],
        )
        self.assertFalse(report.passed)
        self.assertEqual(len(self._codes(report, "numeric_claim_duplicate")), 1)


    def test_model_authored_arithmetic_without_verified_operation_fails(self):
        # A derived number whose operation identity is not one the producer
        # named: operands resolve, but the recomputed result disagrees.
        derived_lie = {
            "claim_id": "fcf_identity",
            "path": "summary",
            "value": 25.0,
            "metric": "free cash flow",
            "period": "FY2024 Q4",
            "unit": "usd_billions",
            "currency": "USD",
            "source_kind": "arithmetic",
            "operation": "difference",
            "operands": [
                "deterministic_current.operating_cash_flow.value",
                "deterministic_current.capital_expenditures_including_finance_leases.value",
            ],
        }
        deterministic_current = {
            "operating_cash_flow": {
                "value": 37.2,
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
        }
        report = self._run(
            summary="Free cash flow reached $25.0 billion in FY2024 Q4.",
            rows=[derived_lie],
            deterministic_current=deterministic_current,
        )
        self.assertFalse(report.passed)
        self.assertEqual(
            len(self._codes(report, "numeric_claim_operation_unverified")), 1
        )


    def test_arbitrary_same_unit_subtraction_without_declaration_fails(self):
        deterministic_current = {
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
        }
        row = {
            "claim_id": "undeclared_free_cash_flow",
            "path": "summary",
            "value": 18.2,
            "metric": "free cash flow",
            "period": "FY2024 Q4",
            "unit": "usd_billions",
            "currency": "USD",
            "source_kind": "arithmetic",
            "operation": "difference",
            "operands": [
                "deterministic_current.operating_cash_flow.value",
                "deterministic_current.capital_expenditures.value",
            ],
        }
        report = self._run(
            summary="Free cash flow was $18.2 billion in FY2024 Q4.",
            rows=[row],
            deterministic_current=deterministic_current,
        )
        self.assertFalse(report.passed)
        self.assertEqual(
            [failure.code for failure in report.failures],
            ["numeric_claim_operation_unverified"],
        )


    def test_declared_dimensionally_incompatible_product_and_quotient_fail(self):
        cases = (
            (
                "currency_product",
                "Currency product was $706.8 billion in FY2024 Q4.",
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
                    "currency_product": {
                        "value": 706.8,
                        "unit": "usd_billions",
                        "currency": "USD",
                        "period": "FY2024-Q4",
                        "source": "derived",
                        "concept": (
                            "derived: operating_cash_flow * "
                            "capital_expenditures"
                        ),
                    },
                },
                {
                    "claim_id": "currency_product",
                    "path": "summary",
                    "value": 706.8,
                    "metric": "currency product",
                    "period": "FY2024 Q4",
                    "unit": "usd_billions",
                    "currency": "USD",
                    "source_kind": "arithmetic",
                    "operation": "product",
                    "operands": [
                        "deterministic_current.operating_cash_flow.value",
                        "deterministic_current.capital_expenditures.value",
                    ],
                },
            ),
            (
                "currency_per_count",
                "Revenue per employee ratio was 2 in FY2024 Q4.",
                {
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
                },
                {
                    "claim_id": "revenue_per_employee",
                    "path": "summary",
                    "value": 2.0,
                    "metric": "revenue per employee",
                    "period": "FY2024 Q4",
                    "unit": "ratio",
                    "currency": None,
                    "source_kind": "arithmetic",
                    "operation": "quotient",
                    "operands": [
                        "deterministic_current.revenue.value",
                        "deterministic_current.employees.value",
                    ],
                },
            ),
        )
        for label, summary, deterministic_current, row in cases:
            with self.subTest(case=label):
                report = self._run(
                    summary=summary,
                    rows=[row],
                    deterministic_current=deterministic_current,
                )
                self.assertFalse(report.passed)
                self.assertEqual(
                    [failure.code for failure in report.failures],
                    ["numeric_claim_operation_unverified"],
                )


    def test_global_token_collision_never_grounds_a_claim(self):
        # The old gate admitted ANY case-wide digit; here 29 (Azure) and
        # 15 (bookings growth) collide across metrics. The supported 29
        # carries its own fully verified row; the adversarial 15 has none
        # and stays unbound — collision alone never grounds a claim.
        producer = self._producer(
            excerpt=(
                "This quarter, revenue was $64.7 billion. Azure and other "
                "cloud services revenue grew 29% and 30% in constant "
                "currency in FY2024 Q4. Demand remained durable."
            )
        )
        report = self._run(
            producer=producer,
            summary=(
                "Azure and other cloud services grew 29% while commercial "
                "bookings rose 15% in FY2024 Q4."
            ),
            rows=[
                msft_claim_row(
                    claim_id="azure_growth_supported",
                    value="29%",
                    metric="Azure and other cloud services revenue growth",
                    period="FY2024 Q4",
                    unit="percentage_points",
                    currency=None,
                    quote=(
                        "Azure and other cloud services revenue grew 29% "
                        "and 30% in constant currency in FY2024 Q4"
                    ),
                ),
            ],
        )
        self.assertFalse(report.passed)
        unbound = self._codes(report, "numeric_claim_unbound")
        self.assertEqual(len(unbound), 1, report.failures)
        self.assertIn("15", str(unbound[0].observed))


    def test_evidence_quotes_are_source_material_not_rescanned_prose(self):
        # Verbatim evidence quotes carry numbers by design; they are copied
        # source material and never require their own bindings.
        payload = narrative_payload(
            summary="Demand durable; supply tight through FY2024 Q4."
        )
        # Keep the qualitative quote inside the Microsoft excerpt so only
        # the evidence-exemption behavior is under test.
        payload["qualitative"]["ai_demand"]["evidence"] = (
            "revenue was $64.7 billion"
        )
        payload["catalysts"] = [
            epistemic_catalyst(
                "capacity additions",
                "FY2024 Q4",
                "Capital expenditures including finance leases were $19 billion",
            )
        ]
        payload["numeric_claims"] = []
        producer = self._producer()
        finalized = finalized_for(payload)
        report = cq.run_company_hard_gates(producer, self._evaluator(producer), finalized)
        self.assertTrue(report.passed, report.failures)


    def test_fiscal_and_valid_calendar_labels_need_no_numeric_rows(self):
        labels = (
            ("fiscal half", "H2 FY25"),
            ("fiscal year", "FY25"),
            ("fiscal quarter", "Q4 FY25"),
            ("ISO date", "2025-06-30"),
            ("month-first date", "June 30, 2025"),
            ("day-first date", "30 June 2025"),
        )
        for label, temporal_text in labels:
            with self.subTest(label=label):
                report = self._run_with_json_replay(
                    summary=f"Outlook remains weighted to {temporal_text}.",
                    rows=[],
                )
                self.assertTrue(report.passed, report.failures)
                self.assertEqual(report.failures, ())


    def test_lexical_numeric_identifiers_need_no_rows_after_json_roundtrip(self):
        cases = (
            (
                "proper-name identifier",
                "Atlas 365 Copilot adoption remained durable.",
            ),
            (
                "form identifier",
                "The annual report was filed on Form 4.",
            ),
            (
                "version identifier",
                "The platform migrated to version 2.0.",
            ),
        )
        for label, summary in cases:
            with self.subTest(label=label):
                report = self._run_with_json_replay(summary=summary, rows=[])
                self.assertTrue(report.passed, report.failures)
                self.assertEqual(report.failures, ())


    def test_proper_name_identifier_in_watch_items_survives_json_replay(self):
        payload = self._payload("Demand remained durable.", [])
        payload["watch_items"] = [
            "Enterprise adoption of Atlas 365 Copilot remained durable."
        ]

        report = self._run_payload_with_json_replay(payload)

        self.assertTrue(report.passed, report.failures)
        self.assertEqual(self._codes(report, "numeric_claim_unbound"), [])
        self.assertEqual(report.failures, ())


    def test_proper_name_identifier_does_not_hide_real_quantities(self):
        cases = (
            ("percentage", "Atlas 365 Copilot adoption grew 29%.", "29"),
            (
                "scaled currency",
                "Atlas 365 Copilot generated $365 million in revenue.",
                "365",
            ),
            (
                "explicit seat count",
                "Atlas 365 Copilot supports 365 seats.",
                "365",
            ),
            (
                "leading customer count",
                "365 customers adopted Atlas 365 Copilot.",
                "365",
            ),
        )
        for label, summary, expected_observed in cases:
            with self.subTest(label=label):
                report = self._run_with_json_replay(summary=summary, rows=[])
                self.assertFalse(report.passed)
                self.assertEqual(
                    [failure.code for failure in report.failures],
                    ["numeric_claim_unbound"],
                    report.failures,
                )
                self.assertEqual(
                    str(report.failures[0].observed),
                    expected_observed,
                )


    def test_invalid_calendar_shape_does_not_gain_a_date_exemption(self):
        report = self._run_with_json_replay(
            summary="The reporting date is 2025-13-40.",
            rows=[],
        )
        self.assertFalse(report.passed)
        self.assertEqual(
            [failure.code for failure in report.failures],
            ["numeric_claim_unbound", "numeric_claim_unbound"],
        )
        self.assertEqual(
            sorted(str(failure.observed) for failure in report.failures),
            ["13", "40"],
        )


    def test_mixed_fiscal_label_requires_only_the_material_percentage(self):
        summary = "H2 FY25; Azure grew 29% in FY2024 Q4."
        source_quote = "Azure grew 29% in FY2024 Q4."
        producer = self._producer(excerpt=f"{MSFT_EXCERPT} {source_quote}")
        percentage_row = msft_claim_row(
            claim_id="azure_growth_h2_fy25",
            value="29%",
            metric="Azure and other cloud services revenue growth",
            period="FY2024 Q4",
            unit="percent",
            currency=None,
            quote=source_quote,
        )
        self.assertEqual(percentage_row["source_kind"], "text")
        self.assertIn(percentage_row["quote"], producer.excerpt)

        supported = self._run_with_json_replay(
            producer=producer,
            summary=summary,
            rows=[percentage_row],
        )
        self.assertTrue(supported.passed, supported.failures)
        self.assertEqual(supported.failures, ())

        missing = self._run_with_json_replay(
            producer=producer,
            summary=summary,
            rows=[],
        )
        self.assertFalse(missing.passed)
        self.assertEqual(
            [failure.code for failure in missing.failures],
            ["numeric_claim_unbound"],
        )
        self.assertEqual(str(missing.failures[0].observed), "29")


    def test_year_shaped_quantities_require_rows_through_public_hard_gate(self):
        quantities = (
            "Capital expenditures were $2025 million.",
            "Capital expenditures were 2025 million.",
        )
        for summary in quantities:
            with self.subTest(summary=summary):
                report = self._run(summary=summary, rows=[])
                self.assertFalse(report.passed)
                self.assertEqual(
                    [failure.code for failure in report.failures],
                    ["numeric_claim_unbound"],
                )
                self.assertEqual(str(report.failures[0].observed), "2025")

        temporal_controls = (
            "Entering fiscal 2025, outlook remains unchanged.",
            "Outlook remains weighted to 2025.",
        )
        for summary in temporal_controls:
            with self.subTest(summary=summary):
                report = self._run(summary=summary, rows=[])
                self.assertTrue(report.passed, report.failures)
                self.assertEqual(report.failures, ())


    def test_year_shaped_quantities_require_rows_after_json_roundtrip(self):
        quantities = (
            "Capital expenditures were $2025 million.",
            "Capital expenditures were 2025 million.",
        )
        for summary in quantities:
            with self.subTest(summary=summary):
                report = self._run_with_json_replay(summary=summary, rows=[])
                self.assertFalse(report.passed)
                self.assertEqual(
                    [failure.code for failure in report.failures],
                    ["numeric_claim_unbound"],
                )
                self.assertEqual(str(report.failures[0].observed), "2025")

        temporal_controls = (
            "Entering fiscal 2025, outlook remains unchanged.",
            "Outlook remains weighted to 2025.",
        )
        for summary in temporal_controls:
            with self.subTest(summary=summary):
                report = self._run_with_json_replay(summary=summary, rows=[])
                self.assertTrue(report.passed, report.failures)
                self.assertEqual(report.failures, ())


    def test_material_number_scanner_live_direct_json_replay_parity(self):
        cases = (
            ("dollar currency", "Capital expenditures were $2B.", (), ("2",)),
            ("euro currency", "The annual fee was €2025.", (), ("2025",)),
            ("sterling currency", "The annual fee was £2025.", (), ("2025",)),
            ("signed currency", "The annual loss was -$2025.", (), ("-2025",)),
            ("percentage", "Growth was 2%.", (), ("2",)),
            (
                "valuation multiple",
                "Forward valuation multiple was 12x.",
                (),
                ("12",),
            ),
            (
                "percentage points",
                "Margin expanded 8 percentage points.",
                (),
                ("8",),
            ),
            (
                "range",
                "Capacity is expected in 2–3 quarters.",
                (),
                ("2", "3"),
            ),
            (
                "ISO date",
                "Outlook remains weighted to 2025-06-30.",
                (),
                (),
            ),
            (
                "written date",
                "Outlook remains weighted to June 30, 2025.",
                (),
                (),
            ),
            ("calendar year", "Outlook remains weighted to 2025.", (), ()),
            (
                "fiscal labels",
                "Outlook remains weighted to H2 FY25 and Q4 FY25.",
                (),
                (),
            ),
            (
                "lexical identifier",
                "Atlas 365 Copilot adoption remained durable.",
                (),
                (),
            ),
            (
                "relationship identifier",
                "Relationship r2 remains compatible.",
                (),
                (),
            ),
            (
                "fact identifier",
                "Fact f3 remains pending.",
                (),
                (),
            ),
            (
                "watch item lexical identifier",
                "Demand remained durable.",
                ("Monitor Atlas 365 Copilot adoption.",),
                (),
            ),
            (
                "watch item quantity",
                "Demand remained durable.",
                ("Monitor adoption across 365 customer accounts.",),
                ("365",),
            ),
            (
                "ordinary quantity",
                "365 customers adopted the platform.",
                (),
                ("365",),
            ),
            (
                "numeric-free control",
                "Demand remained durable while supply stayed tight.",
                (),
                (),
            ),
        )

        for label, summary, watch_items, expected_observed in cases:
            with self.subTest(case=label):
                payload = self._payload(summary, [])
                payload["watch_items"] = list(watch_items)
                producer = self._producer()
                try:
                    service._validated_investment_facts(
                        json.dumps(payload),
                        excerpt=producer.excerpt,
                        news_items=[
                            service._freeze_json_value(dict(item))
                            for item in producer.news_items
                        ],
                        deterministic_current=service._freeze_json_value({}),
                        deterministic_prior=service._freeze_json_value({}),
                        relationship_facts=service._freeze_json_value({}),
                        material_relationships=(),
                    )
                except service.InvestmentValidationError as error:
                    live_error = error
                else:
                    live_error = None

                report = self._run_payload_with_json_replay(
                    payload,
                    producer=producer,
                )
                unbound = self._codes(report, "numeric_claim_unbound")
                self.assertEqual(
                    sorted(str(failure.observed) for failure in unbound),
                    sorted(expected_observed),
                    report.failures,
                )
                self.assertEqual(live_error is None, not expected_observed)
                self.assertEqual(report.passed, not expected_observed)
                if live_error is not None:
                    self.assertEqual(
                        live_error.categories,
                        (service.VALIDATION_JSON_SCHEMA,),
                    )
                    self.assertEqual(
                        len(live_error.problems),
                        len(expected_observed),
                    )
                    for token in expected_observed:
                        self.assertTrue(
                            any(token in problem for problem in live_error.problems),
                            live_error.problems,
                        )


    def test_evidence_quote_numbers_are_excluded_in_all_three_modes(self):
        payload = self._payload("Demand remained durable.", [])
        payload["qualitative"]["ai_demand"]["evidence"] = (
            "revenue was $64.7 billion"
        )
        producer = self._producer()

        parsed = service._validated_investment_facts(
            json.dumps(payload),
            excerpt=producer.excerpt,
            news_items=[
                service._freeze_json_value(dict(item))
                for item in producer.news_items
            ],
            deterministic_current=service._freeze_json_value({}),
            deterministic_prior=service._freeze_json_value({}),
            relationship_facts=service._freeze_json_value({}),
            material_relationships=(),
        )
        report = self._run_payload_with_json_replay(payload, producer=producer)

        self.assertEqual(parsed, payload)
        self.assertTrue(report.passed, report.failures)
        self.assertEqual(self._codes(report, "numeric_claim_unbound"), [])


    def test_ratio_binding_covers_x_multiple_in_all_three_modes(self):
        quote = "Forward valuation multiple was 12x in FY2025."
        row = msft_claim_row(
            claim_id="forward-valuation-multiple",
            value="12x",
            metric="forward valuation multiple",
            period="FY2025",
            unit="ratio",
            currency=None,
            quote=quote,
        )
        payload = self._payload(quote, [row])
        producer = self._producer(excerpt=f"{MSFT_EXCERPT} {quote}")

        parsed = service._validated_investment_facts(
            json.dumps(payload),
            excerpt=producer.excerpt,
            news_items=[
                service._freeze_json_value(dict(item))
                for item in producer.news_items
            ],
            deterministic_current=service._freeze_json_value({}),
            deterministic_prior=service._freeze_json_value({}),
            relationship_facts=service._freeze_json_value({}),
            material_relationships=(),
        )
        report = self._run_payload_with_json_replay(payload, producer=producer)

        self.assertEqual(parsed["numeric_claims"], [row])
        self.assertTrue(report.passed, report.failures)
        self.assertEqual(self._codes(report, "numeric_claim_unbound"), [])


    def test_malformed_row_reports_specific_error_without_unbound_noise(self):
        summary = (
            "Capital expenditures including finance leases were $19 billion "
            "in FY2024 Q4."
        )
        row = msft_claim_row()
        del row["metric"]
        payload = self._payload(summary, [row])
        producer = self._producer()
        expected_problem = (
            "numeric_claims[0]: metric must be a nonblank string of at most "
            "200 characters"
        )

        with self.assertRaises(service.InvestmentValidationError) as raised:
            service._validated_investment_facts(
                json.dumps(payload),
                excerpt=producer.excerpt,
                news_items=[
                    service._freeze_json_value(dict(item))
                    for item in producer.news_items
                ],
                deterministic_current=service._freeze_json_value({}),
                deterministic_prior=service._freeze_json_value({}),
                relationship_facts=service._freeze_json_value({}),
                material_relationships=(),
            )
        self.assertEqual(
            raised.exception.categories,
            (service.VALIDATION_JSON_SCHEMA,),
        )
        self.assertIn(expected_problem, raised.exception.problems)
        self.assertFalse(
            any(
                "material numeric token" in problem
                for problem in raised.exception.problems
            ),
            raised.exception.problems,
        )

        report = self._run_payload_with_json_replay(payload, producer=producer)
        invalid_rows = self._codes(report, "numeric_claim_invalid_row")
        self.assertEqual(len(invalid_rows), 1, report.failures)
        self.assertEqual(
            (
                invalid_rows[0].path,
                invalid_rows[0].observed,
            ),
            ("numeric_claims", expected_problem),
        )
        self.assertEqual(self._codes(report, "numeric_claim_unbound"), [])


    def test_absent_and_empty_ledgers_reject_material_number_in_all_modes(self):
        summary = (
            "Capital expenditures including finance leases were $19 billion "
            "in FY2024 Q4."
        )
        for ledger in ("absent", "empty"):
            with self.subTest(ledger=ledger):
                payload = self._payload(summary, [])
                if ledger == "absent":
                    del payload["numeric_claims"]
                producer = self._producer()

                with self.assertRaises(
                    service.InvestmentValidationError
                ) as raised:
                    service._validated_investment_facts(
                        json.dumps(payload),
                        excerpt=producer.excerpt,
                        news_items=[
                            service._freeze_json_value(dict(item))
                            for item in producer.news_items
                        ],
                        deterministic_current=service._freeze_json_value({}),
                        deterministic_prior=service._freeze_json_value({}),
                        relationship_facts=service._freeze_json_value({}),
                        material_relationships=(),
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

                report = self._run_payload_with_json_replay(
                    payload,
                    producer=producer,
                )
                self.assertFalse(report.passed)
                self.assertEqual(
                    [
                        (failure.code, failure.path, str(failure.observed))
                        for failure in report.failures
                    ],
                    [("numeric_claim_unbound", "$.summary", "19")],
                )


    def test_fiscal_label_row_fails_but_deleting_it_leaves_no_unbound(self):
        summary = "Outlook remains weighted to H2 FY25."
        producer = self._producer(excerpt=f"{MSFT_EXCERPT} {summary}")
        bad_fiscal_row = msft_claim_row(
            claim_id="fiscal_half_label",
            value=2,
            metric="outlook period",
            period="FY25",
            unit="count",
            currency=None,
            quote=summary,
        )
        self.assertEqual(bad_fiscal_row["source_kind"], "text")
        self.assertIn(bad_fiscal_row["quote"], producer.excerpt)

        bad = self._run_with_json_replay(
            producer=producer,
            summary=summary,
            rows=[bad_fiscal_row],
        )
        self.assertFalse(bad.passed)
        self.assertEqual(
            [failure.code for failure in bad.failures],
            ["numeric_claim_tuple_mismatch"],
        )
        self.assertEqual(self._codes(bad, "numeric_claim_unbound"), [])

        cleaned = self._run_with_json_replay(
            producer=producer,
            summary=summary,
            rows=[],
        )
        self.assertTrue(cleaned.passed, cleaned.failures)
        self.assertEqual(cleaned.failures, ())


    def test_year_like_labels_do_not_need_bindings(self):
        report = self._run(
            summary=(
                "Entering fiscal 2025, capital expenditures including "
                "finance leases stay elevated after $19 billion in FY2024 Q4."
            ),
            rows=[msft_claim_row()],
        )
        self.assertTrue(report.passed, report.failures)


    def test_empty_ledger_with_numeric_free_narrative_passes(self):
        # Negative control: no material numerical prose, empty ledger — the
        # gate must be green exactly when there is nothing to bind.
        report = self._run(
            summary="Demand durable; supply tight.", rows=[]
        )
        self.assertTrue(report.passed, report.failures)
        self.assertEqual(report.failures, ())


    def test_binding_gate_failure_order_is_deterministic(self):
        first = self._run(
            summary="Capital expenditures were $29 billion in FY2024 Q4.",
            rows=[
                msft_claim_row(
                    claim_id="bad_metric",
                    value="$29B",
                    quote="Azure and other cloud services revenue grew 29%",
                )
            ],
        )
        second = self._run(
            summary="Capital expenditures were $29 billion in FY2024 Q4.",
            rows=[
                msft_claim_row(
                    claim_id="bad_metric",
                    value="$29B",
                    quote="Azure and other cloud services revenue grew 29%",
                )
            ],
        )
        self.assertEqual(first.failures, second.failures)
        ordered = [
            (failure.code, failure.path, failure.evidence)
            for failure in first.failures
        ]
        self.assertEqual(ordered, sorted(set(ordered)))


