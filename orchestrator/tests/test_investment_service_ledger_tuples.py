"""Tests for investment service."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from investment_service_support import (
    NumericClaimLedgerTestBase,
)

import investment_service as service


class NumericClaimLedgerTupleBindingTests(NumericClaimLedgerTestBase):
    """Tests for numeric claim ledger effect pairs, fact tuple binding, and contract matrices."""

    def test_effect_and_of_recipient_share_one_metric(self):
        sentence = (
            "In FY2024-Q4, Activision had a net -$0.06-per-share impact on "
            "consolidated diluted EPS of $2.95, while the supplied recipient "
            "fact also shows consolidated GAAP revenue growth."
        )
        rows = self._effect_fact_rows()
        payload = self._payload(rows)
        payload["drivers"] = [sentence]
        payload["summary"] = "Cloud demand remained durable."

        parsed = service._validated_investment_facts(
            json.dumps(payload),
            excerpt="Demand remained durable.",
            news_items=service._freeze_json_value([]),
            deterministic_current=service._freeze_json_value(
                self._effect_fact_sources()
            ),
            deterministic_prior=service._freeze_json_value({}),
        )

        expected_paths = (
            "deterministic_current.activision_net_impact_diluted_eps.value",
            "deterministic_current.diluted_eps.value",
        )
        self.assertEqual(
            tuple(row["fact_path"] for row in parsed["numeric_claims"]),
            expected_paths,
        )
        self.assertEqual(parsed["numeric_claims"], rows)
        for surface in ("-$0.06", "$2.95"):
            start = sentence.index(surface)
            self.assertEqual(
                service._numeric_target_metric_groups(
                    sentence, start, start + len(surface)
                ),
                frozenset({"diluted_eps"}),
            )


    def test_effect_sharing_does_not_remove_recipient_alias(self):
        sentence = (
            "FY2024 Q4 Activision had a -$0.06 impact on consolidated "
            "diluted EPS of $2.95."
        )
        impact_start = sentence.index("-$0.06")
        recipient_start = sentence.index("$2.95")

        self.assertEqual(
            service._numeric_target_metric_groups(
                sentence, impact_start, impact_start + len("-$0.06")
            ),
            frozenset({"diluted_eps"}),
        )
        self.assertEqual(
            service._numeric_target_metric_groups(
                sentence, recipient_start, recipient_start + len("$2.95")
            ),
            frozenset({"diluted_eps"}),
        )


    def test_effect_pair_rejects_semicolon_and_sentence(self):
        cases = (
            (
                "semicolon",
                (
                    "FY2024 Q4 Activision recorded -$0.06; impact on "
                    "consolidated diluted EPS of $2.95."
                ),
            ),
            (
                "sentence",
                (
                    "FY2024 Q4 Activision recorded -$0.06. Impact on "
                    "consolidated diluted EPS of $2.95 followed."
                ),
            ),
        )

        for label, sentence in cases:
            with self.subTest(case=label):
                impact_start = sentence.index("-$0.06")
                recipient_start = sentence.index("$2.95")
                self.assertEqual(
                    service._numeric_target_metric_groups(
                        sentence,
                        impact_start,
                        impact_start + len("-$0.06"),
                    ),
                    frozenset(),
                )
                self.assertEqual(
                    service._numeric_target_metric_groups(
                        sentence,
                        recipient_start,
                        recipient_start + len("$2.95"),
                    ),
                    frozenset({"diluted_eps"}),
                )


    def test_effect_pair_rejects_comma_or_and_instead_of_of(self):
        cases = (
            (
                "comma",
                (
                    "FY2024 Q4 Activision had a -$0.06 impact on "
                    "consolidated diluted EPS, $2.95."
                ),
            ),
            (
                "coordination",
                (
                    "FY2024 Q4 Activision had a -$0.06 impact on "
                    "consolidated diluted EPS and $2.95."
                ),
            ),
        )

        for label, sentence in cases:
            with self.subTest(case=label):
                impact_start = sentence.index("-$0.06")
                recipient_start = sentence.index("$2.95")
                self.assertEqual(
                    service._numeric_target_metric_groups(
                        sentence,
                        impact_start,
                        impact_start + len("-$0.06"),
                    ),
                    frozenset({"diluted_eps"}),
                )
                self.assertEqual(
                    service._numeric_target_metric_groups(
                        sentence,
                        recipient_start,
                        recipient_start + len("$2.95"),
                    ),
                    frozenset(),
                )


    def test_effect_pair_rejects_two_or_unrelated_metric_aliases(self):
        sentence = (
            "FY2024 Q4 Activision had a -$0.06 impact on diluted EPS revenue "
            "of $2.95."
        )
        impact_start = sentence.index("-$0.06")
        recipient_start = sentence.index("$2.95")

        self.assertEqual(
            service._numeric_target_metric_groups(
                sentence, impact_start, impact_start + len("-$0.06")
            ),
            frozenset(),
        )
        self.assertEqual(
            service._numeric_target_metric_groups(
                sentence, recipient_start, recipient_start + len("$2.95")
            ),
            frozenset({"diluted_eps", "revenue"}),
        )


    def test_effect_pair_rejects_intervening_scalar(self):
        sentence = (
            "FY2024 Q4 Activision had a -$0.06 plus 10% impact on "
            "consolidated diluted EPS of $2.95."
        )
        impact_start = sentence.index("-$0.06")
        recipient_start = sentence.index("$2.95")

        self.assertEqual(
            service._numeric_target_metric_groups(
                sentence, impact_start, impact_start + len("-$0.06")
            ),
            frozenset(),
        )
        self.assertEqual(
            service._numeric_target_metric_groups(
                sentence, recipient_start, recipient_start + len("$2.95")
            ),
            frozenset({"diluted_eps"}),
        )


    def test_effect_pair_rejects_conflicting_period(self):
        sentence = (
            "Activision had a -$0.06 impact on FY2024 Q4 consolidated "
            "diluted EPS of $2.95 in FY2025 Q1."
        )
        impact_start = sentence.index("-$0.06")
        recipient_start = sentence.index("$2.95")

        self.assertEqual(
            service._numeric_target_metric_groups(
                sentence, impact_start, impact_start + len("-$0.06")
            ),
            frozenset(),
        )
        self.assertEqual(
            service._numeric_target_metric_groups(
                sentence, recipient_start, recipient_start + len("$2.95")
            ),
            frozenset({"diluted_eps"}),
        )


    def test_equal_effect_and_recipient_values_keep_both_occurrences_owned(self):
        sentence = (
            "FY2024 Q4 Activision had a -$0.06 impact on consolidated "
            "diluted EPS of -$0.06."
        )
        first_start = sentence.index("-$0.06")
        second_start = sentence.index("-$0.06", first_start + 1)

        for start in (first_start, second_start):
            self.assertEqual(
                service._numeric_target_metric_groups(
                    sentence, start, start + len("-$0.06")
                ),
                frozenset({"diluted_eps"}),
            )

        rows = self._effect_fact_rows(eps_value="-$0.06")
        payload = self._payload(rows)
        payload["drivers"] = [sentence]
        payload["summary"] = "Cloud demand remained durable."
        parsed = service._validated_investment_facts(
            json.dumps(payload),
            excerpt="Demand remained durable.",
            news_items=service._freeze_json_value([]),
            deterministic_current=service._freeze_json_value(
                self._effect_fact_sources(eps_value=-0.06)
            ),
            deterministic_prior=service._freeze_json_value({}),
        )
        self.assertEqual(parsed["numeric_claims"], rows)


    def test_range_endpoint_is_not_an_effect_scalar(self):
        sentence = (
            "FY2024 Q4 Activision estimated a -$0.06 to -$0.04 impact on "
            "consolidated diluted EPS of $2.95."
        )
        low_start = sentence.index("-$0.06")
        high_start = sentence.index("-$0.04")
        recipient_start = sentence.index("$2.95")

        for start, surface in (
            (low_start, "-$0.06"),
            (high_start, "-$0.04"),
        ):
            self.assertEqual(
                service._numeric_target_metric_groups(
                    sentence, start, start + len(surface)
                ),
                frozenset(),
            )
        self.assertEqual(
            service._numeric_target_metric_groups(
                sentence, recipient_start, recipient_start + len("$2.95")
            ),
            frozenset({"diluted_eps"}),
        )


    def test_live_text_and_arithmetic_rows_enforce_target_tuple_compatibility(self):
        text_target = "FY2024 Q4 gross margin was 69%."
        text_row = self._row(
            claim_id="text-target-mismatch",
            path="summary",
            value="69%",
            metric="free cash flow",
            period="FY2024-Q4",
            unit="percent",
            currency=None,
            source_kind="text",
            quote=text_target,
        )
        arithmetic_row = self._row(
            claim_id="arithmetic-target-mismatch",
            path="drivers[0]",
            value="$23.3B",
            metric="free cash flow",
            period="FY2024-Q4",
            unit="usd_billions",
            currency="USD",
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
        del arithmetic_row["quote"]
        payload = self._payload([text_row, arithmetic_row])
        payload["summary"] = text_target
        payload["drivers"] = ["FY2024 Q4 revenue was $23.3B."]

        with self.assertRaises(service.InvestmentValidationError) as raised:
            service._validated_investment_facts(
                json.dumps(payload),
                excerpt=f"Demand remained durable. {text_target}",
                news_items=service._freeze_json_value([]),
                deterministic_current=service._freeze_json_value(
                    self._tuple_fact_sources()
                ),
                deterministic_prior=service._freeze_json_value({}),
            )

        expected_problems = [
            (
                "numeric_claims[0] (claim_id 'text-target-mismatch'): text "
                "source tuple does not match its authored target and bound "
                "producer quote: metric 'free cash flow', period 'FY2024-Q4', "
                "unit 'percent', and currency None do not match the authored "
                "target around the claimed numeral"
            ),
            (
                "numeric_claims[1] (claim_id 'arithmetic-target-mismatch'): "
                "arithmetic source tuple does not match its authored target "
                "and producer-declared output"
            ),
        ]
        self.assertEqual(
            raised.exception.categories,
            (service.VALIDATION_JSON_SCHEMA,),
        )
        self.assertEqual(raised.exception.problems, expected_problems)
        self.assertEqual(
            raised.exception.problems_by_category,
            {service.VALIDATION_JSON_SCHEMA: expected_problems},
        )


    def test_public_live_flow_accepts_exact_fact_scalar_and_range_endpoints(
        self,
    ):
        rows = [
            self._tuple_fact_row(),
            self._tuple_fact_row(
                claim_id="azure-guide-low",
                path="drivers[0]",
                value="28%",
                metric=(
                    "Azure and other cloud services revenue growth guidance"
                ),
                fact_path=(
                    "deterministic_current."
                    "azure_and_other_cloud_services_revenue_growth_guidance.value"
                ),
            ),
            self._tuple_fact_row(
                claim_id="azure-guide-high",
                path="drivers[1]",
                value="29%",
                metric=(
                    "Azure and other cloud services revenue growth guidance"
                ),
                fact_path=(
                    "deterministic_current."
                    "azure_and_other_cloud_services_revenue_growth_guidance.value"
                ),
            ),
        ]
        payload = self._payload(rows)
        payload["summary"] = (
            "Microsoft Cloud gross margin guidance was roughly 70% for "
            "Q1 FY2025."
        )
        payload["drivers"] = [
            (
                "Azure and other cloud services revenue growth guidance was "
                "28% year over year in Q1 FY2025."
            ),
            (
                "Azure and other cloud services revenue growth guidance was "
                "29% year over year in Q1 FY2025."
            ),
        ]
        excerpt = "Demand remained durable. Management provided its outlook."

        with self._live_aggregation_harness(
            [payload],
            deterministic_current=self._tuple_fact_sources(),
            excerpt=excerpt,
        ) as harness:
            result = service.analyze_document({}, harness.document_id)

        self.assertEqual(result, {"analysis_id": harness.analysis_id})
        harness.stage.call.assert_called_once()
        harness.finalize.assert_called_once()
        self.assertEqual(harness.finalize.call_args.args[0], payload)


    def test_public_live_flow_accepts_pass3_two_metric_sentence(self):
        sentence = (
            "Azure and other cloud services grew 29% in FY2024 Q4, with 8 "
            "percentage points from AI services in FY2024 Q4 despite demand "
            "exceeding available capacity."
        )
        payload = self._payload(self._pass3_azure_rows())
        payload["summary"] = "Cloud demand remained durable."
        payload["drivers"] = [sentence]
        excerpt = "Demand remained durable. Management discussed Azure capacity."

        with self._live_aggregation_harness(
            [payload],
            deterministic_current=self._tuple_fact_sources(),
            excerpt=excerpt,
        ) as harness:
            result = service.analyze_document({}, harness.document_id)

        self.assertEqual(result, {"analysis_id": harness.analysis_id})
        harness.stage.call.assert_called_once()
        harness.finalize.assert_called_once()
        self.assertEqual(harness.finalize.call_args.args[0], payload)


    def test_public_live_flow_accepts_candidate_contribution_verb(self):
        sentence = (
            "Microsoft exited FY24 with broad commercial momentum: Azure and "
            "other cloud services grew 29% in FY2024 Q4 year over year, with "
            "AI services contributing 8 points in FY2024 Q4, while demand "
            "remained above available capacity."
        )
        rows = self._candidate_summary_azure_rows()
        payload = self._payload(rows)
        payload["summary"] = sentence
        excerpt = "Demand remained durable. Management discussed Azure capacity."

        with self._live_aggregation_harness(
            [payload],
            deterministic_current=self._tuple_fact_sources(),
            excerpt=excerpt,
        ) as harness:
            result = service.analyze_document({}, harness.document_id)

        self.assertEqual(result, {"analysis_id": harness.analysis_id})
        harness.stage.call.assert_called_once()
        harness.finalize.assert_called_once()
        self.assertEqual(harness.finalize.call_args.args[0], payload)


    def test_public_live_flow_accepts_year_over_year_azure_recipient(self):
        sentence = (
            "AI services contributed 8 percentage points to year-over-year "
            "Azure growth in FY2024 Q4"
        )
        row = self._year_over_year_ai_contribution_row()
        payload = self._payload([row])
        payload["summary"] = sentence
        excerpt = "Demand remained durable. Management discussed Azure capacity."

        with self._live_aggregation_harness(
            [payload],
            deterministic_current=self._tuple_fact_sources(),
            excerpt=excerpt,
        ) as harness:
            result = service.analyze_document({}, harness.document_id)

        self.assertEqual(result, {"analysis_id": harness.analysis_id})
        harness.stage.call.assert_called_once()
        harness.finalize.assert_called_once()
        self.assertEqual(harness.finalize.call_args.args[0], payload)


    def test_year_over_year_azure_recipient_tuple_controls_are_bounded(self):
        exact_sentence = (
            "AI services contributed 8 percentage points to year-over-year "
            "Azure growth in FY2024 Q4"
        )
        frozen_current = service._freeze_json_value(self._tuple_fact_sources())
        exact_row = self._year_over_year_ai_contribution_row()
        payload = self._payload([exact_row])
        payload["summary"] = exact_sentence

        parsed = service._validated_investment_facts(
            json.dumps(payload),
            excerpt="Demand remained durable.",
            news_items=service._freeze_json_value([]),
            deterministic_current=frozen_current,
            deterministic_prior=service._freeze_json_value({}),
        )

        self.assertEqual(parsed["numeric_claims"], [exact_row])

        period = "FY2024-Q4 (three months ended 2024-06-30)"
        paired_sentence = (
            "Azure and other cloud services revenue grew 29% year over year in "
            f"{period}, while AI services contributed 8 percentage points to "
            f"year-over-year Azure growth in {period}."
        )
        cases = (
            (
                "swapped 29 and 8 metrics and sources",
                paired_sentence,
                {
                    0: {
                        "metric": (
                            "AI services contribution to year-over-year "
                            "Azure growth"
                        ),
                        "fact_path": (
                            "deterministic_current."
                            "azure_growth_from_ai_services_points.value"
                        ),
                    },
                    1: {
                        "metric": (
                            "Azure and other cloud services year-over-year growth"
                        ),
                        "fact_path": (
                            "deterministic_current."
                            "azure_and_other_cloud_services_growth_gaap_percent."
                            "value"
                        ),
                    },
                },
                (0, 1),
            ),
            (
                "percent unit",
                paired_sentence,
                {
                    1: {
                        "unit": "percent",
                        "fact_path": (
                            "deterministic_current."
                            "azure_growth_from_ai_services_percent.value"
                        ),
                    }
                },
                (1,),
            ),
            (
                "wrong same-valued source",
                paired_sentence,
                {
                    1: {
                        "fact_path": (
                            "deterministic_current."
                            "commercial_bookings_growth_contribution_points.value"
                        )
                    }
                },
                (1,),
            ),
            (
                "reversed contributor and recipient",
                (
                    "Azure and other cloud services revenue grew 29% year over "
                    f"year in {period}, while Azure growth contributed 8 "
                    "percentage points to year-over-year AI services growth in "
                    f"{period}."
                ),
                {},
                (1,),
            ),
            (
                "punctuation-separated unrelated Azure clause",
                (
                    "Azure and other cloud services revenue grew 29% year over "
                    f"year in {period}. Commercial bookings growth included 8 "
                    f"percentage points in {period}; year-over-year Azure "
                    f"growth was 8 percentage points in {period}."
                ),
                {},
                (1,),
            ),
        )

        for label, sentence, overrides_by_row, bad_indexes in cases:
            with self.subTest(case=label):
                rows = self._candidate_summary_azure_rows()
                rows[1]["metric"] = (
                    "AI services contribution to year-over-year Azure growth"
                )
                for index, overrides in overrides_by_row.items():
                    rows[index].update(overrides)
                rejected = self._payload(rows)
                rejected["summary"] = sentence

                with self.assertRaises(
                    service.InvestmentValidationError
                ) as raised:
                    service._validated_investment_facts(
                        json.dumps(rejected),
                        excerpt="Demand remained durable.",
                        news_items=service._freeze_json_value([]),
                        deterministic_current=frozen_current,
                        deterministic_prior=service._freeze_json_value({}),
                    )

                expected_problems = [
                    (
                        f"numeric_claims[{index}] (claim_id "
                        f"{rows[index]['claim_id']!r}): fact source tuple does "
                        "not match its authored target and deterministic leaf"
                    )
                    for index in bad_indexes
                ]
                self.assertEqual(
                    raised.exception.categories,
                    (service.VALIDATION_JSON_SCHEMA,),
                )
                self.assertEqual(raised.exception.problems, expected_problems)
                self.assertEqual(
                    raised.exception.problems_by_category,
                    {service.VALIDATION_JSON_SCHEMA: expected_problems},
                )


    def test_live_tuple_binding_preserves_points_range_and_percent_controls(
        self,
    ):
        frozen_current = service._freeze_json_value(self._tuple_fact_sources())
        controls = []
        for surface in (
            "point",
            "points",
            "percentage point",
            "percentage points",
        ):
            controls.extend(
                (
                    (
                        f"unpunctuated {surface}",
                        (
                            f"Azure grew 29% in FY2024 Q4 including 8 {surface} "
                            "from AI services in FY2024 Q4"
                        ),
                        self._pass3_azure_rows(),
                        False,
                        ("29%", 8),
                    ),
                    (
                        f"candidate {surface}",
                        (
                            "Microsoft exited FY24 with broad commercial "
                            "momentum: Azure and other cloud services grew 29% "
                            "in FY2024 Q4 year over year, with AI services "
                            f"contributing 8 {surface} in FY2024 Q4, while "
                            "demand remained above available capacity."
                        ),
                        self._candidate_summary_azure_rows(),
                        True,
                        ("29%", 8),
                    ),
                )
            )

        # "Index points" and "data points" do not render a percent sign.
        # A same-value percent fact must therefore fail tuple binding rather
        # than borrowing the bare numeral from either points surface.
        percent_rejection_controls = []
        for qualifier in ("index", "data"):
            rows = self._candidate_summary_azure_rows()
            rows[1].update(
                unit="percent",
                fact_path=(
                    "deterministic_current."
                    "azure_growth_from_ai_services_percent.value"
                ),
            )
            percent_rejection_controls.append(
                (
                    f"candidate {qualifier} points percent control",
                    (
                        "Azure and other cloud services grew 29% in FY2024 Q4 "
                        "year over year, with AI services contributing "
                        f"8 {qualifier} points in FY2024 Q4."
                    ),
                    rows,
                )
            )

        range_rows = [
            self._tuple_fact_row(
                claim_id="azure-guide-low-range",
                path="drivers[0]",
                value="28%",
                metric=(
                    "Azure and other cloud services revenue growth guidance"
                ),
                fact_path=(
                    "deterministic_current."
                    "azure_and_other_cloud_services_revenue_growth_guidance."
                    "value"
                ),
            ),
            self._tuple_fact_row(
                claim_id="azure-guide-high-range",
                path="drivers[0]",
                value="29%",
                metric=(
                    "Azure and other cloud services revenue growth guidance"
                ),
                fact_path=(
                    "deterministic_current."
                    "azure_and_other_cloud_services_revenue_growth_guidance."
                    "value"
                ),
            ),
        ]
        controls.append(
            (
                "explicit range cluster",
                (
                    "Azure and other cloud services revenue growth guidance "
                    "for Q1 FY2025 was 28–29%."
                ),
                range_rows,
                False,
                ("28%", "29%"),
            )
        )

        for label, sentence, rows, candidate, expected_values in controls:
            with self.subTest(control=label):
                payload = self._payload(rows)
                if candidate:
                    payload["summary"] = sentence
                else:
                    payload["summary"] = "Cloud demand remained durable."
                    payload["drivers"] = [sentence]
                parsed = service._validated_investment_facts(
                    json.dumps(payload),
                    excerpt="Demand remained durable.",
                    news_items=service._freeze_json_value([]),
                    deterministic_current=frozen_current,
                    deterministic_prior=service._freeze_json_value({}),
                )
                self.assertEqual(parsed["numeric_claims"], rows)
                self.assertEqual(
                    tuple(row["value"] for row in parsed["numeric_claims"]),
                    expected_values,
                )

        for label, sentence, rows in percent_rejection_controls:
            with self.subTest(control=label):
                payload = self._payload(rows)
                payload["summary"] = sentence

                with self.assertRaises(
                    service.InvestmentValidationError
                ) as raised:
                    service._validated_investment_facts(
                        json.dumps(payload),
                        excerpt="Demand remained durable.",
                        news_items=service._freeze_json_value([]),
                        deterministic_current=frozen_current,
                        deterministic_prior=service._freeze_json_value({}),
                    )

                expected_problems = [
                    (
                        "numeric_claims[1] (claim_id "
                        "'summary-ai-growth-contribution'): fact source tuple "
                        "does not match its authored target and deterministic "
                        "leaf"
                    )
                ]
                self.assertEqual(
                    raised.exception.categories,
                    (service.VALIDATION_JSON_SCHEMA,),
                )
                self.assertEqual(raised.exception.problems, expected_problems)
                self.assertEqual(
                    raised.exception.problems_by_category,
                    {service.VALIDATION_JSON_SCHEMA: expected_problems},
                )


    def test_percentage_point_target_bindings_accept_spaced_and_hyphenated_forms(
        self,
    ):
        frozen_current = service._freeze_json_value(self._tuple_fact_sources())
        surfaces = (
            "8 percentage point",
            "8 percentage points",
            "8-percentage-point",
            "8-percentage-points",
        )

        for surface in surfaces:
            with self.subTest(surface=surface):
                row = self._year_over_year_ai_contribution_row()
                payload = self._payload([row])
                payload["summary"] = (
                    f"AI services contributed {surface} to year-over-year "
                    "Azure growth in FY2024 Q4."
                )

                parsed = service._validated_investment_facts(
                    json.dumps(payload),
                    excerpt="Demand remained durable.",
                    news_items=service._freeze_json_value([]),
                    deterministic_current=frozen_current,
                    deterministic_prior=service._freeze_json_value({}),
                )

                self.assertEqual(parsed["numeric_claims"], [row])


    def test_percentage_point_target_bindings_reject_non_point_and_detached_units(
        self,
    ):
        frozen_current = service._freeze_json_value(self._tuple_fact_sources())
        cases = (
            (
                "percent word",
                (
                    "AI services contributed 8 percent to year-over-year "
                    "Azure growth in FY2024 Q4."
                ),
                None,
            ),
            (
                "percent symbol",
                (
                    "AI services contributed 8% to year-over-year Azure growth "
                    "in FY2024 Q4."
                ),
                None,
            ),
            (
                "basis points",
                (
                    "AI services contributed 8 basis points to year-over-year "
                    "Azure growth in FY2024 Q4."
                ),
                None,
            ),
            (
                "wrong source unit",
                (
                    "AI services contributed 8 percentage points to "
                    "year-over-year Azure growth in FY2024 Q4."
                ),
                (
                    "deterministic_current."
                    "azure_growth_from_ai_services_percent.value"
                ),
            ),
            (
                "detached points phrase",
                (
                    "AI services contributed 8 to year-over-year Azure growth "
                    "in FY2024 Q4, measured in percentage points."
                ),
                None,
            ),
        )

        for label, sentence, fact_path in cases:
            with self.subTest(case=label):
                row = self._year_over_year_ai_contribution_row()
                if fact_path is not None:
                    row["fact_path"] = fact_path
                payload = self._payload([row])
                payload["summary"] = sentence

                with self.assertRaises(
                    service.InvestmentValidationError
                ) as raised:
                    service._validated_investment_facts(
                        json.dumps(payload),
                        excerpt="Demand remained durable.",
                        news_items=service._freeze_json_value([]),
                        deterministic_current=frozen_current,
                        deterministic_prior=service._freeze_json_value({}),
                    )

                expected_problems = [
                    (
                        "numeric_claims[0] (claim_id "
                        "'summary-ai-growth-contribution'): fact source tuple "
                        "does not match its authored target and deterministic "
                        "leaf"
                    )
                ]
                self.assertEqual(
                    raised.exception.categories,
                    (service.VALIDATION_JSON_SCHEMA,),
                )
                self.assertEqual(raised.exception.problems, expected_problems)
                self.assertEqual(
                    raised.exception.problems_by_category,
                    {service.VALIDATION_JSON_SCHEMA: expected_problems},
                )


    def test_live_tuple_binding_rejects_pass3_row_cross_coverage(self):
        canonical_sentence = (
            "Azure and other cloud services grew 29% in FY2024 Q4, with 8 "
            "percentage points from AI services in FY2024 Q4 despite demand "
            "exceeding available capacity."
        )
        neighboring_sentences = (
            (
                "trailing other metric",
                (
                    "Azure and other cloud services grew 29% in FY2024 Q4 "
                    "while AI demand exceeded capacity; commercial bookings "
                    "growth included 8 percentage points in FY2024 Q4."
                ),
            ),
            (
                "leading other metric",
                (
                    "Commercial bookings growth included 8 percentage points "
                    "in FY2024 Q4, while Azure and other cloud services grew "
                    "29% in FY2024 Q4 and AI services demand exceeded capacity."
                ),
            ),
        )
        cases = [
            (
                "swapped metrics",
                canonical_sentence,
                {
                    0: {
                        "metric": "Azure growth contribution from AI services"
                    },
                    1: {
                        "metric": (
                            "Azure and other cloud services year-over-year growth"
                        )
                    },
                },
                (0, 1),
            ),
            (
                "swapped fact paths",
                canonical_sentence,
                {
                    0: {
                        "fact_path": (
                            "deterministic_current."
                            "azure_growth_from_ai_services_points.value"
                        )
                    },
                    1: {
                        "fact_path": (
                            "deterministic_current."
                            "azure_and_other_cloud_services_growth_gaap_percent."
                            "value"
                        )
                    },
                },
                (0, 1),
            ),
            (
                "same 8 attached to bookings",
                canonical_sentence,
                {
                    1: {
                        "metric": "commercial bookings growth contribution",
                        "fact_path": (
                            "deterministic_current."
                            "commercial_bookings_growth_contribution_points.value"
                        ),
                    }
                },
                (1,),
            ),
            (
                "total growth expressed as points",
                canonical_sentence,
                {0: {"unit": "percentage_points"}},
                (0,),
            ),
            (
                "AI contribution expressed as percent",
                canonical_sentence,
                {1: {"unit": "percent"}},
                (1,),
            ),
        ]
        cases.extend(
            (
                label,
                sentence,
                {},
                (1,),
            )
            for label, sentence in neighboring_sentences
        )
        frozen_current = service._freeze_json_value(self._tuple_fact_sources())

        for label, sentence, overrides_by_row, bad_indexes in cases:
            with self.subTest(case=label):
                rows = self._pass3_azure_rows()
                for index, overrides in overrides_by_row.items():
                    rows[index].update(overrides)
                payload = self._payload(rows)
                payload["summary"] = "Cloud demand remained durable."
                payload["drivers"] = [sentence]

                with self.assertRaises(
                    service.InvestmentValidationError
                ) as raised:
                    service._validated_investment_facts(
                        json.dumps(payload),
                        excerpt="Demand remained durable.",
                        news_items=service._freeze_json_value([]),
                        deterministic_current=frozen_current,
                        deterministic_prior=service._freeze_json_value({}),
                    )

                expected_problems = [
                    (
                        f"numeric_claims[{index}] (claim_id "
                        f"{rows[index]['claim_id']!r}): fact source tuple does "
                        "not match its authored target and deterministic leaf"
                    )
                    for index in bad_indexes
                ]
                self.assertEqual(
                    raised.exception.categories,
                    (service.VALIDATION_JSON_SCHEMA,),
                )
                self.assertEqual(raised.exception.problems, expected_problems)
                self.assertEqual(
                    raised.exception.problems_by_category,
                    {service.VALIDATION_JSON_SCHEMA: expected_problems},
                )


    def test_point_surfaces_reject_percent_fact_and_preserve_tuple_swaps(self):
        candidate_sentence = (
            "Microsoft exited FY24 with broad commercial momentum: Azure and "
            "other cloud services grew 29% in FY2024 Q4 year over year, with "
            "AI services contributing 8 points in FY2024 Q4, while demand "
            "remained above available capacity."
        )
        cases = [
            (
                "candidate bookings metric and source",
                candidate_sentence,
                self._candidate_summary_azure_rows,
                True,
                {
                    1: {
                        "metric": "commercial bookings growth contribution",
                        "fact_path": (
                            "deterministic_current."
                            "commercial_bookings_growth_contribution_points.value"
                        ),
                    }
                },
                (1,),
            ),
            (
                "candidate Azure 29 and 8 metric-source swap",
                candidate_sentence,
                self._candidate_summary_azure_rows,
                True,
                {
                    0: {
                        "metric": "Azure growth contribution from AI services",
                        "fact_path": (
                            "deterministic_current."
                            "azure_growth_from_ai_services_points.value"
                        ),
                    },
                    1: {
                        "metric": (
                            "Azure and other cloud services year-over-year growth"
                        ),
                        "fact_path": (
                            "deterministic_current."
                            "azure_and_other_cloud_services_growth_gaap_percent."
                            "value"
                        ),
                    },
                },
                (0, 1),
            ),
        ]
        for surface in (
            "point",
            "points",
            "percentage point",
            "percentage points",
        ):
            percent_override = {
                1: {
                    "unit": "percent",
                    "fact_path": (
                        "deterministic_current."
                        "azure_growth_from_ai_services_percent.value"
                    ),
                }
            }
            cases.extend(
                (
                    (
                        f"candidate percent fact against {surface}",
                        (
                            "Azure and other cloud services grew 29% in "
                            "FY2024 Q4 year over year, with AI services "
                            f"contributing 8 {surface} in FY2024 Q4."
                        ),
                        self._candidate_summary_azure_rows,
                        True,
                        percent_override,
                        (1,),
                    ),
                    (
                        f"unpunctuated percent fact against {surface}",
                        (
                            f"Azure grew 29% in FY2024 Q4 including 8 {surface} "
                            "from AI services in FY2024 Q4"
                        ),
                        self._pass3_azure_rows,
                        False,
                        percent_override,
                        (1,),
                    ),
                )
            )

        frozen_current = service._freeze_json_value(self._tuple_fact_sources())
        for (
            label,
            sentence,
            rows_factory,
            candidate,
            overrides_by_row,
            bad_indexes,
        ) in cases:
            with self.subTest(case=label):
                rows = rows_factory()
                for index, overrides in overrides_by_row.items():
                    rows[index].update(overrides)
                payload = self._payload(rows)
                if candidate:
                    payload["summary"] = sentence
                else:
                    payload["summary"] = "Cloud demand remained durable."
                    payload["drivers"] = [sentence]

                with self.assertRaises(
                    service.InvestmentValidationError
                ) as raised:
                    service._validated_investment_facts(
                        json.dumps(payload),
                        excerpt="Demand remained durable.",
                        news_items=service._freeze_json_value([]),
                        deterministic_current=frozen_current,
                        deterministic_prior=service._freeze_json_value({}),
                    )

                expected_problems = [
                    (
                        f"numeric_claims[{index}] (claim_id "
                        f"{rows[index]['claim_id']!r}): fact source tuple does "
                        "not match its authored target and deterministic leaf"
                    )
                    for index in bad_indexes
                ]
                self.assertEqual(
                    raised.exception.categories,
                    (service.VALIDATION_JSON_SCHEMA,),
                )
                self.assertEqual(raised.exception.problems, expected_problems)
                self.assertEqual(
                    raised.exception.problems_by_category,
                    {service.VALIDATION_JSON_SCHEMA: expected_problems},
                )


    def test_target_period_and_unit_contract_live_matrix(self):
        def row(claim_id, value, metric, period, unit, quote):
            return {
                "claim_id": claim_id,
                "path": "summary",
                "value": value,
                "metric": metric,
                "period": period,
                "unit": unit,
                "currency": None,
                "source_kind": "text",
                "quote": quote,
            }

        fiscal_quote = "Revenue growth was 12% in FY2025 Q1."
        fiscal_row = row(
            "revenue-growth-fy25q1",
            "12%",
            "revenue growth",
            "FY2025-Q1",
            "percent",
            fiscal_quote,
        )
        horizon_quote = "Revenue outlook spans the next 12 months."
        horizon_row = row(
            "revenue-outlook-horizon",
            12,
            "revenue outlook",
            "next 12 months",
            "count",
            horizon_quote,
        )
        range_quote = (
            "Revenue growth guidance was 28% to 29% for Q1 FY2025."
        )
        range_rows = [
            row(
                "revenue-guide-low",
                "28%",
                "revenue growth guidance",
                "FY2025-Q1",
                "percent",
                range_quote,
            ),
            row(
                "revenue-guide-high",
                "29%",
                "revenue growth guidance",
                "FY2025-Q1",
                "percent",
                range_quote,
            ),
        ]
        decimal_quote = (
            "Free cash flow was $23.3 billion in Q1 FY2025."
        )
        decimal_row = row(
            "free-cash-flow-decimal",
            "$23.3 billion",
            "free cash flow",
            "FY2025-Q1",
            "usd_billions",
            decimal_quote,
        )
        decimal_row["currency"] = "USD"
        dual_quote = (
            "Revenue will grow 12% year over year during the next 12 months."
        )
        dual_rows = [
            row(
                "revenue-growth-forward",
                "12%",
                "revenue growth",
                "next 12 months",
                "percent",
                dual_quote,
            ),
            row(
                "revenue-growth-horizon",
                12,
                "revenue growth",
                "next 12 months",
                "count",
                dual_quote,
            ),
        ]
        percent_quote = "Revenue growth was 8% in Q1 FY2025."
        percent_row = row(
            "revenue-growth-percent",
            "8%",
            "revenue growth",
            "FY2025-Q1",
            "percent",
            percent_quote,
        )
        points_quote = (
            "Azure growth contribution from AI services was 8 percentage "
            "points in Q1 FY2025."
        )
        points_row = row(
            "ai-growth-contribution-points",
            8,
            "Azure growth contribution from AI services",
            "FY2025-Q1",
            "percentage_points",
            points_quote,
        )
        compound_quote = (
            "Microsoft Cloud gross margin guidance was 70% for FY2025-Q1 "
            "guidance issued 2024-07-30."
        )
        compound_row = row(
            "cloud-margin-guidance",
            "70%",
            "Microsoft Cloud gross margin guidance",
            "FY2025-Q1 guidance issued 2024-07-30",
            "percent",
            compound_quote,
        )

        accepted = [
            *[
                (f"FYQ alias {alias}", f"Revenue growth was 12% in {alias}.",
                 [fiscal_row])
                for alias in (
                    "FY2025-Q1",
                    "FY25 Q1",
                    "Q1-FY2025",
                    "first quarter of fiscal year 2025",
                )
            ],
            *[
                (
                    f"forward alias {alias}",
                    f"Revenue outlook spans the {alias} 12 months.",
                    [horizon_row],
                )
                for alias in ("next", "following", "forward")
            ],
            (
                "compound reporting FYQ",
                (
                    "Microsoft Cloud gross margin guidance was 70% for "
                    "Q1 FY2025."
                ),
                [compound_row],
            ),
            ("shared range period", range_quote, range_rows),
            ("decimal is not a clause boundary", decimal_quote, [decimal_row]),
            (
                "equal percentage and horizon coefficients",
                "Revenue will grow 12% during the next 12 months.",
                dual_rows,
            ),
            ("percent surface", percent_quote, [percent_row]),
            ("percentage-point surface", points_quote, [points_row]),
        ]

        rejected = [
            ("FY is not FYQ", "Revenue growth was 12% in FY2025.", fiscal_row),
            (
                "date does not imply fiscal quarter",
                (
                    "Revenue growth was 12% for the quarter ended "
                    "September 30, 2024."
                ),
                fiscal_row,
            ),
            (
                "comparison basis cannot replace primary period",
                "Revenue growth was 12% year over year.",
                fiscal_row,
            ),
            (
                "period context is not target-local",
                "Q1 FY2025 outlook remained firm. Revenue growth was 12%.",
                fiscal_row,
            ),
            (
                "bare duration is not forward",
                "Revenue outlook spans 12 months.",
                horizon_row,
            ),
            (
                "percent is not percentage points",
                (
                    "Azure growth contribution from AI services was 8% in "
                    "Q1 FY2025."
                ),
                points_row,
            ),
            (
                "percentage points are not percent",
                (
                    "Revenue growth was 8 percentage points in "
                    "Q1 FY2025."
                ),
                percent_row,
            ),
        ]

        frozen_empty = service._freeze_json_value({})
        frozen_news = service._freeze_json_value([])

        for label, summary, rows in accepted:
            with self.subTest(case=label):
                payload = self._payload([dict(item) for item in rows])
                payload["summary"] = summary
                excerpt = "Demand remained durable. " + " ".join(
                    dict.fromkeys(item["quote"] for item in rows)
                )
                parsed = service._validated_investment_facts(
                    json.dumps(payload),
                    excerpt=excerpt,
                    news_items=frozen_news,
                    deterministic_current=frozen_empty,
                    deterministic_prior=frozen_empty,
                )
                self.assertEqual(parsed["numeric_claims"], rows)

        for label, summary, source_row in rejected:
            with self.subTest(case=label):
                rejected_row = dict(source_row)
                payload = self._payload([rejected_row])
                payload["summary"] = summary
                with self.assertRaises(
                    service.InvestmentValidationError
                ) as raised:
                    service._validated_investment_facts(
                        json.dumps(payload),
                        excerpt=(
                            "Demand remained durable. "
                            f"{rejected_row['quote']}"
                        ),
                        news_items=frozen_news,
                        deterministic_current=frozen_empty,
                        deterministic_prior=frozen_empty,
                    )

                expected_problem = (
                    "numeric_claims[0] (claim_id "
                    f"{rejected_row['claim_id']!r}): text source tuple does "
                    "not match its authored target and bound producer quote: "
                    "metric "
                    f"{rejected_row['metric']!r}, period "
                    f"{rejected_row['period']!r}, unit "
                    f"{rejected_row['unit']!r}, and currency "
                    f"{rejected_row['currency']!r} do not match the authored "
                    "target around the claimed numeral"
                )
                self.assertEqual(
                    raised.exception.categories,
                    (service.VALIDATION_JSON_SCHEMA,),
                )
                self.assertEqual(raised.exception.problems, [expected_problem])
                self.assertEqual(
                    raised.exception.problems_by_category,
                    {service.VALIDATION_JSON_SCHEMA: [expected_problem]},
                )


    def test_metric_inheritance_edge_contract_live_matrix(self):
        def text_row(
            claim_id, value, metric, period, unit, quote, *, currency=None
        ):
            return {
                "claim_id": claim_id,
                "path": "summary",
                "value": value,
                "metric": metric,
                "period": period,
                "unit": unit,
                "currency": currency,
                "source_kind": "text",
                "quote": quote,
            }

        comma_quote = (
            "Free cash flow was $23.3 billion in FY2024 Q4, up 18% "
            "year over year."
        )
        comma_row = text_row(
            "fcf-growth-comma",
            "18%",
            "free cash flow",
            "FY2024-Q4",
            "percent",
            comma_quote,
        )
        comparison_quote = (
            "Revenue will grow 12% year over year during the next 12 months."
        )
        horizon_row = text_row(
            "revenue-comparison-horizon",
            12,
            "revenue growth",
            "next 12 months",
            "count",
            comparison_quote,
        )
        contribution_quote = (
            "Azure growth included 8 points in FY2024 Q4 from AI services."
        )
        contribution_row = text_row(
            "azure-ai-period-interposition",
            8,
            "Azure growth contribution from AI services",
            "FY2024-Q4",
            "percentage_points",
            contribution_quote,
        )

        accepted = (
            ("comma up inherits primary period", comma_quote, comma_row),
            (
                "comparison-qualified forward horizon",
                comparison_quote,
                horizon_row,
            ),
            (
                "contribution period interposition",
                contribution_quote,
                contribution_row,
            ),
        )
        rejected = (
            (
                "ordinary conjunction does not inherit",
                (
                    "Free cash flow was $23.3 billion in FY2024 Q4 and "
                    "increased 18% year over year."
                ),
                comma_row,
            ),
            (
                "semicolon boundary does not inherit",
                (
                    "Free cash flow was $23.3 billion in FY2024 Q4; "
                    "up 18% year over year."
                ),
                comma_row,
            ),
            (
                "swapped contribution does not bind generic Azure",
                (
                    "AI services grew 8 points in FY2024 Q4 from Azure "
                    "growth."
                ),
                contribution_row,
            ),
            (
                "contribution does not cross a sentence boundary",
                (
                    "Azure growth included 8 points in FY2024 Q4. "
                    "From AI services."
                ),
                contribution_row,
            ),
        )
        frozen_empty = service._freeze_json_value({})
        frozen_news = service._freeze_json_value([])

        def companion_rows(summary, row):
            if row["claim_id"] == "fcf-growth-comma":
                return [
                    text_row(
                        "fcf-level-companion",
                        "$23.3 billion",
                        "free cash flow",
                        "FY2024-Q4",
                        "usd_billions",
                        summary,
                        currency="USD",
                    )
                ]
            if row["claim_id"] == "revenue-comparison-horizon":
                return [
                    text_row(
                        "revenue-growth-companion",
                        "12%",
                        "revenue growth",
                        "next 12 months",
                        "percent",
                        summary,
                    )
                ]
            return []

        for label, summary, source_row in accepted:
            with self.subTest(case=label):
                row = dict(source_row, quote=summary)
                rows = [row, *companion_rows(summary, row)]
                payload = self._payload(rows)
                payload["summary"] = summary
                parsed = service._validated_investment_facts(
                    json.dumps(payload),
                    excerpt=f"Demand remained durable. {summary}",
                    news_items=frozen_news,
                    deterministic_current=frozen_empty,
                    deterministic_prior=frozen_empty,
                )
                self.assertEqual(parsed["numeric_claims"], rows)

        for label, summary, source_row in rejected:
            with self.subTest(case=label):
                row = dict(source_row, quote=summary)
                payload = self._payload([row, *companion_rows(summary, row)])
                payload["summary"] = summary
                with self.assertRaises(
                    service.InvestmentValidationError
                ) as raised:
                    service._validated_investment_facts(
                        json.dumps(payload),
                        excerpt=f"Demand remained durable. {summary}",
                        news_items=frozen_news,
                        deterministic_current=frozen_empty,
                        deterministic_prior=frozen_empty,
                    )
                expected_problem = (
                    "numeric_claims[0] (claim_id "
                    f"{row['claim_id']!r}): text source tuple does not match "
                    "its authored target and bound producer quote: metric "
                    f"{row['metric']!r}, period {row['period']!r}, unit "
                    f"{row['unit']!r}, and currency {row['currency']!r} do "
                    "not match the authored target around the claimed numeral"
                )
                self.assertEqual(
                    raised.exception.categories,
                    (service.VALIDATION_JSON_SCHEMA,),
                )
                self.assertEqual(raised.exception.problems, [expected_problem])
                self.assertEqual(
                    raised.exception.problems_by_category,
                    {service.VALIDATION_JSON_SCHEMA: [expected_problem]},
                )


    def test_arithmetic_tuple_live_rejects_wrong_result_unit_and_period(self):
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
        base_row = {
            "claim_id": "derived-free-cash-flow",
            "path": "summary",
            "value": 18.2,
            "metric": "free cash flow",
            "period": "FY2024-Q4",
            "unit": "usd_billions",
            "currency": "USD",
            "source_kind": "arithmetic",
            "operation": "difference",
            "operands": [
                "deterministic_current.operating_cash_flow.value",
                "deterministic_current.capital_expenditures.value",
            ],
        }
        cases = (
            (
                "wrong result",
                "Free cash flow was $25 billion in FY2024 Q4.",
                dict(base_row, value=25.0),
            ),
            (
                "wrong unit",
                "Free cash flow was 18.2% in FY2024 Q4.",
                dict(
                    base_row,
                    value="18.2%",
                    unit="percent",
                    currency=None,
                ),
            ),
            (
                "wrong period",
                "Free cash flow was $18.2 billion in FY2025 Q1.",
                dict(base_row, period="FY2025-Q1"),
            ),
        )
        frozen_empty = service._freeze_json_value({})
        for label, summary, row in cases:
            with self.subTest(case=label):
                payload = self._payload([row])
                payload["summary"] = summary
                with self.assertRaises(
                    service.InvestmentValidationError
                ) as raised:
                    service._validated_investment_facts(
                        json.dumps(payload),
                        excerpt="Demand remained durable.",
                        news_items=service._freeze_json_value([]),
                        deterministic_current=deterministic_current,
                        deterministic_prior=frozen_empty,
                    )
                expected_problem = (
                    "numeric_claims[0] (claim_id "
                    "'derived-free-cash-flow'): arithmetic source tuple does "
                    "not match its authored target and producer-declared output"
                )
                self.assertEqual(
                    raised.exception.categories,
                    (service.VALIDATION_JSON_SCHEMA,),
                )
                self.assertEqual(raised.exception.problems, [expected_problem])
                self.assertEqual(
                    raised.exception.problems_by_category,
                    {service.VALIDATION_JSON_SCHEMA: [expected_problem]},
                )




if __name__ == '__main__':
    unittest.main()
