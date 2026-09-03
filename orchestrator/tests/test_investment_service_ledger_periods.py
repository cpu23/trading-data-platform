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


class NumericClaimLedgerPeriodResolutionTests(NumericClaimLedgerTestBase):
    """Tests for numeric claim ledger period inheritance, rebasing, and metric cluster resolution."""

    def test_leading_period_inherits_across_and_list(self):
        quote = (
            "In FY2024-Q4, consolidated GAAP revenue growth was 15.2% year "
            "over year and consolidated GAAP net income growth was 9.7% year "
            "over year, each derived from current and prior values and rounded "
            "to one decimal."
        )
        period = "FY2024-Q4 (three months ended 2024-06-30)"
        rows = [
            self._period_text_row(
                claim_id="coordinated-revenue-growth",
                quote=quote,
                value="15.2%",
                metric="consolidated GAAP revenue growth",
                period=period,
                unit="percent",
            ),
            self._period_text_row(
                claim_id="coordinated-net-income-growth",
                quote=quote,
                value="9.7%",
                metric="consolidated GAAP net income growth",
                period=period,
                unit="percent",
            ),
        ]

        parsed = self._validated_period_text_rows(quote, rows)

        self.assertEqual(parsed["numeric_claims"], rows)


    def test_leading_period_inherits_across_comma_list(self):
        quote = (
            "In FY2024-Q4, consolidated operating cash flow was $37.2 "
            "billion, consolidated free cash flow was $23.3 billion, and "
            "consolidated cash paid for property and equipment was $13.9 "
            "billion, all on a cash basis."
        )
        period = "FY2024-Q4 (three months ended 2024-06-30)"
        rows = [
            self._period_text_row(
                claim_id="coordinated-operating-cash-flow",
                quote=quote,
                value="$37.2 billion",
                metric="consolidated operating cash flow",
                period=period,
                unit="usd_billions",
                currency="USD",
            ),
            self._period_text_row(
                claim_id="coordinated-free-cash-flow",
                quote=quote,
                value="$23.3 billion",
                metric="consolidated free cash flow",
                period=period,
                unit="usd_billions",
                currency="USD",
            ),
            self._period_text_row(
                claim_id="coordinated-cash-capex",
                quote=quote,
                value="$13.9 billion",
                metric=(
                    "consolidated cash paid for property and equipment"
                ),
                period=period,
                unit="usd_billions",
                currency="USD",
            ),
        ]

        parsed = self._validated_period_text_rows(quote, rows)

        self.assertEqual(parsed["numeric_claims"], rows)


    def test_leading_period_stops_at_semicolon(self):
        quote = (
            "In FY2024-Q4, revenue growth was 15.2%; net income growth was "
            "9.7%."
        )
        rows = [
            self._period_text_row(
                claim_id="semicolon-revenue-growth",
                quote=quote,
                value="15.2%",
                metric="revenue growth",
                period="FY2024-Q4",
                unit="percent",
            ),
            self._period_text_row(
                claim_id="semicolon-net-income-growth",
                quote=quote,
                value="9.7%",
                metric="net income growth",
                period="FY2024-Q4",
                unit="percent",
            ),
        ]

        with self.assertRaises(service.InvestmentValidationError):
            self._validated_period_text_rows(quote, rows)


    def test_leading_period_stops_at_sentence(self):
        quote = (
            "In FY2024-Q4, revenue growth was 15.2%. Net income growth was "
            "9.7%."
        )
        rows = [
            self._period_text_row(
                claim_id="sentence-revenue-growth",
                quote=quote,
                value="15.2%",
                metric="revenue growth",
                period="FY2024-Q4",
                unit="percent",
            ),
            self._period_text_row(
                claim_id="sentence-net-income-growth",
                quote=quote,
                value="9.7%",
                metric="net income growth",
                period="FY2024-Q4",
                unit="percent",
            ),
        ]

        with self.assertRaises(service.InvestmentValidationError):
            self._validated_period_text_rows(quote, rows)


    def test_nearer_period_rebases_coordinated_suffix(self):
        quote = (
            "In FY2024-Q4, consolidated GAAP revenue growth was 15.2%, and "
            "in FY2025-Q1, consolidated GAAP net income growth was 9.7%."
        )
        rows = [
            self._period_text_row(
                claim_id="rebased-revenue-growth",
                quote=quote,
                value="15.2%",
                metric="consolidated GAAP revenue growth",
                period="FY2024-Q4",
                unit="percent",
            ),
            self._period_text_row(
                claim_id="rebased-net-income-growth",
                quote=quote,
                value="9.7%",
                metric="consolidated GAAP net income growth",
                period="FY2025-Q1",
                unit="percent",
            ),
        ]
        parsed = self._validated_period_text_rows(quote, rows)
        wrong_period_rows = [dict(row) for row in rows]
        wrong_period_rows[1]["period"] = "FY2024-Q4"

        self.assertEqual(parsed["numeric_claims"], rows)
        with self.assertRaises(service.InvestmentValidationError):
            self._validated_period_text_rows(quote, wrong_period_rows)


    def test_conflicting_periods_in_one_owner_reject(self):
        quote = (
            "In FY2024-Q4 and FY2025-Q1, revenue growth was 12%."
        )

        for period in ("FY2024-Q4", "FY2025-Q1"):
            with self.subTest(period=period):
                row = self._period_text_row(
                    claim_id=f"conflicting-period-owner-{period}",
                    quote=quote,
                    value="12%",
                    metric="revenue growth",
                    period=period,
                    unit="percent",
                )
                with self.assertRaises(service.InvestmentValidationError):
                    self._validated_period_text_rows(quote, [row])


    def test_unclassified_or_unowned_metric_cell_breaks_inheritance(self):
        quote = (
            "In FY2024-Q4, consolidated GAAP revenue growth was 15.2%, and "
            "management discussed Azure capacity, consolidated free cash flow "
            "was $23.3 billion."
        )
        rows = [
            self._period_text_row(
                claim_id="barrier-revenue-growth",
                quote=quote,
                value="15.2%",
                metric="consolidated GAAP revenue growth",
                period="FY2024-Q4",
                unit="percent",
            ),
            self._period_text_row(
                claim_id="barrier-free-cash-flow",
                quote=quote,
                value="$23.3 billion",
                metric="consolidated free cash flow",
                period="FY2024-Q4",
                unit="usd_billions",
                currency="USD",
            ),
        ]

        with self.assertRaises(service.InvestmentValidationError):
            self._validated_period_text_rows(quote, rows)


    def test_repeated_equal_values_bind_occurrence_local_periods(self):
        quote = (
            "In FY2024-Q4, revenue growth was 12%. In FY2025-Q1, revenue "
            "growth was 12%."
        )
        rows = [
            self._period_text_row(
                claim_id="equal-growth-fy24q4",
                quote=quote,
                value="12%",
                metric="revenue growth",
                period="FY2024-Q4",
                unit="percent",
            ),
            self._period_text_row(
                claim_id="equal-growth-fy25q1",
                quote=quote,
                value="12%",
                metric="revenue growth",
                period="FY2025-Q1",
                unit="percent",
            ),
        ]

        parsed = self._validated_period_text_rows(quote, rows)

        self.assertEqual(parsed["numeric_claims"], rows)


    def test_range_is_one_period_owned_cluster(self):
        quote = (
            "In FY2025-Q1, revenue growth guidance was 28% to 29%."
        )
        rows = [
            self._period_text_row(
                claim_id="owned-range-low",
                quote=quote,
                value="28%",
                metric="revenue growth guidance",
                period="FY2025-Q1",
                unit="percent",
            ),
            self._period_text_row(
                claim_id="owned-range-high",
                quote=quote,
                value="29%",
                metric="revenue growth guidance",
                period="FY2025-Q1",
                unit="percent",
            ),
        ]

        parsed = self._validated_period_text_rows(quote, rows)

        self.assertEqual(parsed["numeric_claims"], rows)


    def test_conflicting_periods_inside_range_reject(self):
        quote = (
            "In FY2024-Q4 and FY2025-Q1, revenue guidance was 10% to 12%."
        )

        for period in ("FY2024-Q4", "FY2025-Q1"):
            with self.subTest(period=period):
                rows = [
                    self._period_text_row(
                        claim_id=f"conflicting-range-low-{period}",
                        quote=quote,
                        value="10%",
                        metric="revenue guidance",
                        period=period,
                        unit="percent",
                    ),
                    self._period_text_row(
                        claim_id=f"conflicting-range-high-{period}",
                        quote=quote,
                        value="12%",
                        metric="revenue guidance",
                        period=period,
                        unit="percent",
                    ),
                ]
                with self.assertRaises(service.InvestmentValidationError):
                    self._validated_period_text_rows(quote, rows)


    def test_composite_source_period_matches_primary_quarter_only(self):
        quote = "In FY2024-Q4, revenue growth was 12%."
        row = self._period_text_row(
            claim_id="composite-source-period",
            quote=quote,
            value="12%",
            metric="revenue growth",
            period="FY2024-Q4 (three months ended 2024-06-30)",
            unit="percent",
        )

        parsed = self._validated_period_text_rows(quote, [row])
        wrong_quarter_row = dict(row, period="FY2025-Q1")

        self.assertEqual(parsed["numeric_claims"], [row])
        with self.assertRaises(service.InvestmentValidationError):
            self._validated_period_text_rows(quote, [wrong_quarter_row])


    def test_explicit_period_owns_existing_multi_scalar_claims(self):
        period = "FY2024-Q4"
        cases = [
            (
                (
                    "FY2024 Q4 free cash flow of $23.3B grew 18% "
                    "year-over-year despite $19B capex."
                ),
                (
                    (
                        "$23.3B",
                        "free cash flow",
                        "usd_billions",
                        "USD",
                    ),
                    ("18%", "free cash flow", "percent", None),
                    (
                        "$19B",
                        "capital expenditures",
                        "usd_billions",
                        "USD",
                    ),
                ),
            ),
            (
                (
                    "FY2024 Q4 Microsoft Cloud produced $36.8B of revenue at "
                    "69% gross margin."
                ),
                (
                    (
                        "$36.8B",
                        "Microsoft Cloud revenue",
                        "usd_billions",
                        "USD",
                    ),
                    ("69%", "gross margin", "percent", None),
                ),
            ),
        ]

        for case_index, (quote, claims) in enumerate(cases):
            rows = []
            for claim_index, (surface, metric, unit, currency) in enumerate(
                claims
            ):
                with self.subTest(quote=quote, surface=surface):
                    start = quote.index(surface)
                    owned_labels = (
                        service._numeric_target_owned_period_labels(
                            quote, start, start + len(surface)
                        )
                    )
                    self.assertEqual(
                        service._primary_period_labels(set(owned_labels)),
                        {"fiscal-quarter:2024:q4"},
                    )
                rows.append(
                    self._period_text_row(
                        claim_id=(
                            f"explicit-period-{case_index}-{claim_index}"
                        ),
                        quote=quote,
                        value=surface,
                        metric=metric,
                        period=period,
                        unit=unit,
                        currency=currency,
                    )
                )

            parsed = self._validated_period_text_rows(quote, rows)
            self.assertEqual(parsed["numeric_claims"], rows)


    def test_counted_forward_period_scalar_owns_canonical_relative_label(self):
        for direction in ("next", "following", "forward"):
            with self.subTest(direction=direction):
                quote = f"Revenue outlook spans the {direction} 12 months."
                start = quote.index("12")
                owned = service._numeric_target_owned_period_labels(
                    quote, start, start + len("12")
                )
                row = self._period_text_row(
                    claim_id=f"counted-{direction}-horizon",
                    quote=quote,
                    value="12",
                    metric="revenue outlook",
                    period=f"{direction} 12 months",
                    unit="count",
                )

                self.assertEqual(
                    service._primary_period_labels(set(owned)),
                    {"relative:next:12:month"},
                )
                parsed = self._validated_period_text_rows(quote, [row])
                self.assertEqual(parsed["numeric_claims"], [row])


    def test_growth_and_counted_horizon_scalars_share_forward_period(self):
        quote = (
            "Revenue will grow 12% year over year during the next 12 months."
        )
        period = "next 12 months"
        rows = [
            self._period_text_row(
                claim_id="forward-growth",
                quote=quote,
                value="12%",
                metric="revenue growth",
                period=period,
                unit="percent",
            ),
            self._period_text_row(
                claim_id="forward-horizon-count",
                quote=quote,
                value="12",
                metric="revenue",
                period=period,
                unit="count",
            ),
        ]

        for surface in ("12%", "12 months"):
            with self.subTest(surface=surface):
                start = quote.index(surface)
                owned = service._numeric_target_owned_period_labels(
                    quote, start, start + len("12")
                )
                self.assertEqual(
                    service._primary_period_labels(set(owned)),
                    {"relative:next:12:month"},
                )

        parsed = self._validated_period_text_rows(quote, rows)
        self.assertEqual(parsed["numeric_claims"], rows)


    def test_bare_duration_is_not_a_relative_period(self):
        quote = "Revenue outlook spans 12 months."
        start = quote.index("12")
        row = self._period_text_row(
            claim_id="bare-duration",
            quote=quote,
            value="12",
            metric="revenue outlook",
            period="next 12 months",
            unit="count",
        )

        self.assertEqual(
            service._primary_period_labels(
                set(
                    service._numeric_target_owned_period_labels(
                        quote, start, start + len("12")
                    )
                )
            ),
            set(),
        )
        with self.assertRaises(service.InvestmentValidationError):
            self._validated_period_text_rows(quote, [row])


    def test_same_family_period_conflict_rejects_despite_shared_date(self):
        quote = (
            "In FY2024-Q4 (three months ended 2024-06-30) and FY2025-Q1 "
            "(three months ended 2024-06-30), revenue growth was 12%."
        )
        start = quote.index("12%")
        owned = service._numeric_target_owned_period_labels(
            quote, start, start + len("12%")
        )
        primary = service._primary_period_labels(set(owned))
        self.assertEqual(
            primary,
            {
                "fiscal-quarter:2024:q4",
                "fiscal-quarter:2025:q1",
                "calendar-date:2024-06-30",
            },
        )
        self.assertTrue(service._period_bundle_conflict(primary))

        for period in (
            "FY2024-Q4 (three months ended 2024-06-30)",
            "FY2025-Q1 (three months ended 2024-06-30)",
        ):
            with self.subTest(period=period):
                row = self._period_text_row(
                    claim_id=f"shared-date-conflict-{period[:9]}",
                    quote=quote,
                    value="12%",
                    metric="revenue growth",
                    period=period,
                    unit="percent",
                )
                with self.assertRaises(service.InvestmentValidationError):
                    self._validated_period_text_rows(quote, [row])


    def test_composite_period_rejects_wrong_quarter_even_with_matching_date(self):
        quote = (
            "In FY2024-Q4 (three months ended 2024-06-30), revenue growth "
            "was 12%."
        )
        start = quote.index("12%")
        owned = service._numeric_target_owned_period_labels(
            quote, start, start + len("12%")
        )
        self.assertEqual(
            service._primary_period_labels(set(owned)),
            {
                "fiscal-quarter:2024:q4",
                "calendar-date:2024-06-30",
            },
        )
        wrong = self._period_text_row(
            claim_id="wrong-quarter-shared-date",
            quote=quote,
            value="12%",
            metric="revenue growth",
            period="FY2025-Q1 (three months ended 2024-06-30)",
            unit="percent",
        )

        with self.assertRaises(service.InvestmentValidationError):
            self._validated_period_text_rows(quote, [wrong])


    def test_explicit_period_accepts_local_unknown_derived_arithmetic_scalar(self):
        quote = "FY2024 Q4 cash investment total was $56.2B."
        deterministic = service._freeze_json_value(
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
            }
        )
        row = self._row(
            claim_id="local-unknown-derived",
            path="summary",
            value=56.2,
            metric="cash investment total",
            period="FY2024 Q4",
            unit="usd_billions",
            currency="USD",
            source_kind="arithmetic",
            operation="sum",
            operands=[
                "deterministic_current.operating_cash_flow.value",
                "deterministic_current.capital_expenditures.value",
            ],
        )
        del row["quote"]
        payload = self._payload([row])
        payload["summary"] = quote
        start = quote.index("$56.2B")

        self.assertEqual(
            service._primary_period_labels(
                set(
                    service._numeric_target_owned_period_labels(
                        quote, start, start + len("$56.2B")
                    )
                )
            ),
            {"fiscal-quarter:2024:q4"},
        )
        parsed = service._validated_investment_facts(
            json.dumps(payload),
            excerpt="Demand remained durable.",
            news_items=service._freeze_json_value([]),
            deterministic_current=deterministic,
            deterministic_prior=service._freeze_json_value({}),
        )
        self.assertEqual(parsed["numeric_claims"], [row])


    def test_unknown_derived_scalar_cannot_inherit_period_from_known_claim(self):
        quote = (
            "In FY2024-Q4, free cash flow was $18.2B, cash investment total "
            "was $56.2B."
        )
        deterministic = service._freeze_json_value(
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
            }
        )
        rows = [
            self._row(
                claim_id="known-derived-owner",
                path="summary",
                value=18.2,
                metric="free cash flow",
                period="FY2024-Q4",
                unit="usd_billions",
                currency="USD",
                source_kind="arithmetic",
                operation="difference",
                operands=[
                    "deterministic_current.operating_cash_flow.value",
                    "deterministic_current.capital_expenditures.value",
                ],
            ),
            self._row(
                claim_id="inherited-unknown-derived",
                path="summary",
                value=56.2,
                metric="cash investment total",
                period="FY2024-Q4",
                unit="usd_billions",
                currency="USD",
                source_kind="arithmetic",
                operation="sum",
                operands=[
                    "deterministic_current.operating_cash_flow.value",
                    "deterministic_current.capital_expenditures.value",
                ],
            ),
        ]
        for row in rows:
            del row["quote"]
        start = quote.index("$56.2B")
        self.assertEqual(
            service._primary_period_labels(
                set(
                    service._numeric_target_owned_period_labels(
                        quote, start, start + len("$56.2B")
                    )
                )
            ),
            set(),
        )
        payload = self._payload(rows)
        payload["summary"] = quote
        with self.assertRaises(service.InvestmentValidationError):
            service._validated_investment_facts(
                json.dumps(payload),
                excerpt="Demand remained durable.",
                news_items=service._freeze_json_value([]),
                deterministic_current=deterministic,
                deterministic_prior=service._freeze_json_value({}),
            )


    def test_locally_rebased_multi_scalar_claim_rebases_following_suffix(self):
        quote = (
            "In FY2024-Q4, revenue was $10B, and in FY2025-Q1, free cash "
            "flow was $12B and grew 20%, and capital expenditures were $3B."
        )
        claims = (
            ("$10B", "revenue", "FY2024-Q4", "usd_billions", "USD"),
            ("$12B", "free cash flow", "FY2025-Q1", "usd_billions", "USD"),
            ("20%", "free cash flow", "FY2025-Q1", "percent", None),
            (
                "$3B",
                "capital expenditures",
                "FY2025-Q1",
                "usd_billions",
                "USD",
            ),
        )
        rows = []
        for index, (surface, metric, period, unit, currency) in enumerate(
            claims
        ):
            start = quote.index(surface)
            owned = service._numeric_target_owned_period_labels(
                quote, start, start + len(surface)
            )
            with self.subTest(surface=surface):
                self.assertEqual(
                    service._primary_period_labels(set(owned)),
                    {
                        "fiscal-quarter:2024:q4"
                        if period == "FY2024-Q4"
                        else "fiscal-quarter:2025:q1"
                    },
                )
            rows.append(
                self._period_text_row(
                    claim_id=f"rebased-multi-{index}",
                    quote=quote,
                    value=surface,
                    metric=metric,
                    period=period,
                    unit=unit,
                    currency=currency,
                )
            )

        parsed = self._validated_period_text_rows(quote, rows)
        self.assertEqual(parsed["numeric_claims"], rows)


    def test_target_metric_alias_groups_preserve_bounded_capex_occurrences(self):
        cases = (
            (
                "multiword alias with modifier",
                "consolidated cash paid for property and equipment",
                frozenset({"capex"}),
            ),
            (
                "separate competing alias",
                "cash paid for property and equipment and revenue",
                frozenset({"capex", "revenue"}),
            ),
            (
                "lease-inclusive alias suppresses nested base capex",
                "consolidated capital expenditures including finance leases",
                frozenset({"lease_inclusive_capex"}),
            ),
            (
                "lease-additions alias suppresses nested base capex",
                (
                    "consolidated capital expenditure including finance "
                    "lease additions"
                ),
                frozenset({"lease_inclusive_capex"}),
            ),
        )
        for label, metric, expected in cases:
            with self.subTest(case=label):
                self.assertEqual(
                    service._numeric_target_metric_alias_groups(metric),
                    expected,
                )


    def test_same_group_alias_specificity_keeps_public_azure_tuples_exact(self):
        nested_aliases = (
            (
                "Azure and other cloud services revenue growth",
                "azure_growth",
            ),
            ("cash capital expenditures", "capex"),
        )
        for text, expected_group in nested_aliases:
            with self.subTest(alias=text):
                self.assertEqual(
                    service._metric_alias_occurrences(text),
                    [(0, len(text), expected_group)],
                )
                self.assertEqual(
                    service._numeric_target_metric_alias_groups(text),
                    frozenset({expected_group}),
                )

        competing = (
            "Azure and other cloud services revenue growth and revenue"
        )
        azure_alias = "Azure and other cloud services revenue growth"
        revenue_start = competing.rindex("revenue")
        self.assertEqual(
            service._metric_alias_occurrences(competing),
            [
                (0, len(azure_alias), "azure_growth"),
                (revenue_start, len(competing), "revenue"),
            ],
        )
        self.assertEqual(
            service._numeric_target_metric_alias_groups(competing),
            frozenset({"azure_growth", "revenue"}),
        )

        azure_sentence = (
            "Azure and other cloud services revenue growth was 29% in "
            "FY2024 Q4."
        )
        azure_row = self._tuple_fact_row(
            claim_id="specific-azure-growth",
            path="summary",
            value="29%",
            metric="Azure and other cloud services revenue growth",
            period="FY2024-Q4 (three months ended 2024-06-30)",
            unit="percent",
            fact_path=(
                "deterministic_current."
                "azure_and_other_cloud_services_growth_gaap_percent.value"
            ),
        )
        range_sentence = (
            "Azure and other cloud services revenue growth guidance for Q1 "
            "FY2025 was 28% to 29%."
        )
        range_rows = [
            self._tuple_fact_row(
                claim_id=f"specific-azure-range-{endpoint}",
                path="drivers[0]",
                value=f"{endpoint}%",
                metric=(
                    "Azure and other cloud services revenue growth guidance"
                ),
                fact_path=(
                    "deterministic_current."
                    "azure_and_other_cloud_services_revenue_growth_guidance."
                    "value"
                ),
            )
            for endpoint in (28, 29)
        ]
        sources = service._freeze_json_value(self._tuple_fact_sources())
        for sentence, rows, path in (
            (azure_sentence, [azure_row], "summary"),
            (range_sentence, range_rows, "drivers"),
        ):
            with self.subTest(public_positive=sentence):
                payload = self._payload(rows)
                if path == "summary":
                    payload["summary"] = sentence
                else:
                    payload["drivers"] = [sentence]
                    payload["summary"] = "Cloud demand remained durable."
                self.assertEqual(
                    service.numeric_claim_source_problems(
                        payload,
                        deterministic_current=self._tuple_fact_sources(),
                        deterministic_prior={},
                    ),
                    [],
                )
                parsed = service._validated_investment_facts(
                    json.dumps(payload),
                    excerpt="Demand remained durable.",
                    news_items=service._freeze_json_value([]),
                    deterministic_current=sources,
                    deterministic_prior=service._freeze_json_value({}),
                )
                self.assertEqual(parsed["numeric_claims"], rows)

        competing_sentence = (
            "Azure and other cloud services revenue growth was 29%, while "
            "revenue was 29% in FY2024 Q4."
        )
        first_start = competing_sentence.index("29%")
        second_start = competing_sentence.rindex("29%")
        self.assertEqual(
            service._numeric_target_metric_groups(
                competing_sentence, first_start, first_start + len("29%")
            ),
            frozenset({"azure_growth"}),
        )
        self.assertEqual(
            service._numeric_target_metric_groups(
                competing_sentence, second_start, second_start + len("29%")
            ),
            frozenset({"revenue"}),
        )
        swapped = dict(azure_row, metric="revenue")
        swapped_payload = self._payload([swapped])
        swapped_payload["summary"] = azure_sentence
        expected_swap_problem = [
            (
                "numeric_claims[0] (claim_id 'specific-azure-growth'): fact "
                "source tuple does not match its authored target and "
                "deterministic leaf"
            )
        ]
        self.assertEqual(
            service.numeric_claim_source_problems(
                swapped_payload,
                deterministic_current=self._tuple_fact_sources(),
                deterministic_prior={},
            ),
            expected_swap_problem,
        )
        with self.assertRaises(service.InvestmentValidationError):
            service._validated_investment_facts(
                json.dumps(swapped_payload),
                excerpt="Demand remained durable.",
                news_items=service._freeze_json_value([]),
                deterministic_current=sources,
                deterministic_prior=service._freeze_json_value({}),
            )


    def test_pending_and_explicit_period_bundles_conflict_before_assignment(self):
        conflicts = (
            (
                "pending then explicit scalar",
                (
                    "FY2024-Q4 and in FY2025-Q1, revenue growth was 12%."
                ),
                (("12%", "revenue growth", "percent", None),),
            ),
            (
                "reversed pending periods",
                (
                    "FY2025-Q1 and in FY2024-Q4, revenue growth was 12%."
                ),
                (("12%", "revenue growth", "percent", None),),
            ),
            (
                "pending then explicit range",
                (
                    "FY2024-Q4 and in FY2025-Q1, revenue growth guidance was "
                    "10% to 12%."
                ),
                (
                    ("10%", "revenue growth guidance", "percent", None),
                    ("12%", "revenue growth guidance", "percent", None),
                ),
            ),
            (
                "reversed pending range",
                (
                    "FY2025-Q1 and in FY2024-Q4, revenue growth guidance was "
                    "10% to 12%."
                ),
                (
                    ("10%", "revenue growth guidance", "percent", None),
                    ("12%", "revenue growth guidance", "percent", None),
                ),
            ),
        )
        for label, quote, claims in conflicts:
            for asserted_period in ("FY2024-Q4", "FY2025-Q1"):
                rows = [
                    self._period_text_row(
                        claim_id=(
                            f"merged-period-{case_index}-{asserted_period}"
                        ),
                        quote=quote,
                        value=surface,
                        metric=metric,
                        period=asserted_period,
                        unit=unit,
                        currency=currency,
                    )
                    for case_index, (
                        surface,
                        metric,
                        unit,
                        currency,
                    ) in enumerate(claims)
                ]
                with self.subTest(case=label, period=asserted_period):
                    for surface, _, _, _ in claims:
                        start = quote.index(surface)
                        owned = service._primary_period_labels(
                            set(
                                service._numeric_target_owned_period_labels(
                                    quote,
                                    start,
                                    start + len(surface),
                                )
                            )
                        )
                        self.assertEqual(
                            owned,
                            {
                                "fiscal-quarter:2024:q4",
                                "fiscal-quarter:2025:q1",
                            },
                        )
                    with self.assertRaises(
                        service.InvestmentValidationError
                    ):
                        self._validated_period_text_rows(quote, rows)

        rebased_quote = (
            "FY2024-Q4; in FY2025-Q1, revenue was $12B and free cash flow "
            "was $10B."
        )
        rebased_claims = (
            ("$12B", "revenue"),
            ("$10B", "free cash flow"),
        )
        rebased_rows = [
            self._period_text_row(
                claim_id=f"hard-rebase-{index}",
                quote=rebased_quote,
                value=surface,
                metric=metric,
                period="FY2025-Q1",
                unit="usd_billions",
                currency="USD",
            )
            for index, (surface, metric) in enumerate(rebased_claims)
        ]
        for surface, _ in rebased_claims:
            start = rebased_quote.index(surface)
            self.assertEqual(
                service._primary_period_labels(
                    set(
                        service._numeric_target_owned_period_labels(
                            rebased_quote,
                            start,
                            start + len(surface),
                        )
                    )
                ),
                {"fiscal-quarter:2025:q1"},
            )
        parsed = self._validated_period_text_rows(
            rebased_quote, rebased_rows
        )
        self.assertEqual(parsed["numeric_claims"], rebased_rows)


    def test_snake_case_capex_target_alias_keeps_exact_fact_tuple_bounded(self):
        statement = (
            "Cash paid for property and equipment was $13.9B in FY2024 Q4."
        )
        row = self._tuple_fact_row(
            claim_id="snake-case-cash-capex",
            value="$13.9B",
            metric="cash_paid_for_property_and_equipment",
            period="FY2024-Q4",
            unit="usd_billions",
            currency="USD",
            fact_path=(
                "deterministic_current."
                "cash_paid_for_property_and_equipment.value"
            ),
        )
        payload = self._payload([row])
        payload["summary"] = statement
        sources = self._capex_alias_fact_sources()

        self.assertEqual(
            service._numeric_target_metric_alias_groups(row["metric"]),
            frozenset({"capex"}),
        )
        self.assertEqual(
            service.numeric_claim_source_problems(
                payload,
                deterministic_current=sources,
                deterministic_prior={},
            ),
            [],
        )
        parsed = service._validated_investment_facts(
            json.dumps(payload),
            excerpt="Demand remained durable.",
            news_items=service._freeze_json_value([]),
            deterministic_current=service._freeze_json_value(sources),
            deterministic_prior=service._freeze_json_value({}),
        )
        self.assertEqual(parsed["numeric_claims"], [row])
        self.assertEqual(
            (
                row["fact_path"],
                row["value"],
                row["unit"],
                row["currency"],
                row["period"],
            ),
            (
                "deterministic_current."
                "cash_paid_for_property_and_equipment.value",
                "$13.9B",
                "usd_billions",
                "USD",
                "FY2024-Q4",
            ),
        )

        rejected_paths = (
            "deterministic_current.same_valued_revenue.value",
            (
                "deterministic_current."
                "capital_expenditures_including_finance_leases.value"
            ),
        )
        for fact_path in rejected_paths:
            with self.subTest(fact_path=fact_path):
                rejected_row = dict(row, fact_path=fact_path)
                rejected_payload = self._payload([rejected_row])
                rejected_payload["summary"] = statement
                self.assertEqual(
                    service.numeric_claim_source_problems(
                        rejected_payload,
                        deterministic_current=sources,
                        deterministic_prior={},
                    ),
                    [
                        (
                            "numeric_claims[0] (claim_id "
                            "'snake-case-cash-capex'): fact source tuple does "
                            "not match its authored target and deterministic "
                            "leaf"
                        )
                    ],
                )


    def test_public_live_flow_repairs_snake_case_capex_source_identity_swaps(self):
        statement = (
            "Cash paid for property and equipment was $13.9B in FY2024 Q4."
        )

        def capex_row(*, claim_id, path, fact_name):
            return self._tuple_fact_row(
                claim_id=claim_id,
                path=path,
                value="$13.9B",
                metric="cash_paid_for_property_and_equipment",
                period="FY2024-Q4",
                unit="usd_billions",
                currency="USD",
                fact_path=f"deterministic_current.{fact_name}.value",
            )

        invalid_rows = [
            capex_row(
                claim_id="same-valued-other-metric",
                path="summary",
                fact_name="same_valued_revenue",
            ),
            capex_row(
                claim_id="lease-inclusive-basis",
                path="drivers[0]",
                fact_name="capital_expenditures_including_finance_leases",
            ),
        ]
        invalid = self._payload(invalid_rows)
        invalid["summary"] = statement
        invalid["drivers"] = [statement]
        corrected_row = capex_row(
            claim_id="exact-cash-capex",
            path="summary",
            fact_name="cash_paid_for_property_and_equipment",
        )
        corrected = self._payload([corrected_row])
        corrected["summary"] = statement
        sources = self._capex_alias_fact_sources()

        with self._live_aggregation_harness(
            [invalid, corrected],
            deterministic_current=sources,
            excerpt="Demand remained durable.",
        ) as harness:
            result = service.analyze_document({}, harness.document_id)

        self.assertEqual(result, {"analysis_id": harness.analysis_id})
        self.assertEqual(harness.stage.call.call_count, 2)
        self.assertEqual(
            harness.stage.add_validation_warnings.call_args.args[0],
            ["response was not valid investment JSON"],
        )
        harness.finalize.assert_called_once()
        self.assertEqual(harness.finalize.call_args.args[0], corrected)
        self.assertEqual(
            (
                corrected_row["fact_path"],
                corrected_row["value"],
                corrected_row["unit"],
                corrected_row["currency"],
                corrected_row["period"],
            ),
            (
                "deterministic_current."
                "cash_paid_for_property_and_equipment.value",
                "$13.9B",
                "usd_billions",
                "USD",
                "FY2024-Q4",
            ),
        )


    def test_metric_cluster_resolver_assigns_residual_occurrences_locally(self):
        fcf_sentence = (
            "FY2024 Q4 free cash flow was $23.3B and grew 18% year-over-year "
            "despite $19B capex."
        )
        comma_source = "FY2024 Q4 FCF was $23.3B, up 18% YoY."
        cloud_sentence = (
            "FY2024 Q4 Microsoft Cloud produced $36.8B of revenue at 69% "
            "gross margin."
        )
        cases = (
            (fcf_sentence, "$23.3B", "free_cash_flow"),
            (fcf_sentence, "18%", "free_cash_flow"),
            (fcf_sentence, "$19B", "capex"),
            (comma_source, "18%", "free_cash_flow"),
            (cloud_sentence, "$36.8B", "microsoft_cloud_revenue"),
            (cloud_sentence, "69%", "gross_margin_dollars"),
        )

        for sentence, surface, expected_group in cases:
            with self.subTest(surface=surface, sentence=sentence):
                start = sentence.index(surface)
                self.assertEqual(
                    service._numeric_target_metric_groups(
                        sentence, start, start + len(surface)
                    ),
                    frozenset({expected_group}),
                )

        for surface in ("$23.3B", "18%", "$19B"):
            with self.subTest(despite_period_surface=surface):
                start = fcf_sentence.index(surface)
                self.assertEqual(
                    service._primary_period_labels(
                        set(
                            service._numeric_target_owned_period_labels(
                                fcf_sentence,
                                start,
                                start + len(surface),
                            )
                        )
                    ),
                    {"fiscal-quarter:2024:q4"},
                )


    def test_public_live_flow_accepts_fcf_inheritance_and_split_cloud_metric(self):
        fcf_sentence = (
            "FY2024 Q4 free cash flow was $23.3B and grew 18% year-over-year "
            "despite $19B capex."
        )
        comma_source = "FY2024 Q4 FCF was $23.3B, up 18% YoY."
        cloud_sentence = (
            "FY2024 Q4 Microsoft Cloud produced $36.8B of revenue at 69% "
            "gross margin."
        )
        rows = [
            self._cluster_fact_row(
                claim_id="fcf-value",
                path="summary",
                value="$23.3B",
                metric="free cash flow",
                unit="usd_billions",
                currency="USD",
                fact_name="free_cash_flow",
            ),
            self._cluster_fact_row(
                claim_id="fcf-growth",
                path="summary",
                value="18%",
                metric="free cash flow",
                unit="percent",
                fact_name="free_cash_flow_growth_percent",
            ),
            self._cluster_fact_row(
                claim_id="capex-value",
                path="summary",
                value="$19B",
                metric="capital expenditures",
                unit="usd_billions",
                currency="USD",
                fact_name="capital_expenditures",
            ),
            self._row(
                claim_id="fcf-comma-value",
                path="drivers[0]",
                value="$23.3B",
                metric="free cash flow",
                period="FY2024-Q4",
                unit="usd_billions",
                currency="USD",
                source_kind="text",
                quote=comma_source,
            ),
            self._row(
                claim_id="fcf-comma-growth",
                path="drivers[0]",
                value="18%",
                metric="free cash flow",
                period="FY2024-Q4",
                unit="percent",
                currency=None,
                source_kind="text",
                quote=comma_source,
            ),
            self._cluster_fact_row(
                claim_id="cloud-revenue",
                path="drivers[1]",
                value="$36.8B",
                metric="Microsoft Cloud revenue",
                unit="usd_billions",
                currency="USD",
                fact_name="microsoft_cloud_revenue",
            ),
            self._cluster_fact_row(
                claim_id="cloud-margin",
                path="drivers[1]",
                value="69%",
                metric="gross margin",
                unit="percent",
                fact_name="microsoft_cloud_gross_margin_percent",
            ),
        ]
        payload = self._payload(rows)
        payload["summary"] = fcf_sentence
        payload["drivers"] = [comma_source, cloud_sentence]
        excerpt = f"Demand remained durable. {comma_source}"

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


    def test_live_metric_binding_rejects_swaps_and_inheritance_boundaries(self):
        cases = (
            (
                "swapped FCF growth",
                (
                    "FY2024 Q4 free cash flow of $23.3B grew 18% "
                    "year-over-year despite elevated capex."
                ),
                "18%",
                frozenset({"free_cash_flow"}),
                {
                    "value": "18%",
                    "metric": "capital expenditures",
                    "unit": "percent",
                    "fact_name": "free_cash_flow_growth_percent",
                },
            ),
            (
                "comma starts a new subject",
                (
                    "FY2024 Q4 free cash flow was $23.3B, revenue grew 18% "
                    "year-over-year."
                ),
                "18%",
                frozenset({"revenue"}),
                {
                    "value": "18%",
                    "metric": "free cash flow",
                    "unit": "percent",
                    "fact_name": "free_cash_flow_growth_percent",
                },
            ),
            (
                "semicolon",
                (
                    "FY2024 Q4 free cash flow was $23.3B; grew 18% "
                    "year-over-year."
                ),
                "18%",
                frozenset(),
                {
                    "value": "18%",
                    "metric": "free cash flow",
                    "unit": "percent",
                    "fact_name": "free_cash_flow_growth_percent",
                },
            ),
            (
                "comma before coordinated direction",
                (
                    "FY2024 Q4 free cash flow was $23.3B, and grew 18% "
                    "year-over-year."
                ),
                "18%",
                frozenset(),
                {
                    "value": "18%",
                    "metric": "free cash flow",
                    "unit": "percent",
                    "fact_name": "free_cash_flow_growth_percent",
                },
            ),
            (
                "plain comma does not coordinate inheritance",
                (
                    "FY2024 Q4 free cash flow was $23.3B, grew 18% "
                    "year-over-year."
                ),
                "18%",
                frozenset(),
                {
                    "value": "18%",
                    "metric": "free cash flow",
                    "unit": "percent",
                    "fact_name": "free_cash_flow_growth_percent",
                },
            ),
            (
                "despite preserves unrelated explicit alias",
                (
                    "FY2024 Q4 free cash flow was $23.3B despite revenue "
                    "growing 18% year-over-year."
                ),
                "18%",
                frozenset({"revenue"}),
                {
                    "value": "18%",
                    "metric": "free cash flow",
                    "unit": "percent",
                    "fact_name": "free_cash_flow_growth_percent",
                },
            ),
            (
                "equidistant aliases",
                "FY2024 Q4 revenue: $23.3B; FCF.",
                "$23.3B",
                frozenset(),
                {
                    "value": "$23.3B",
                    "metric": "free cash flow",
                    "unit": "usd_billions",
                    "currency": "USD",
                    "fact_name": "free_cash_flow",
                },
            ),
            (
                "swapped split cloud metric",
                (
                    "FY2024 Q4 Microsoft Cloud produced $36.8B of revenue at "
                    "69% gross margin."
                ),
                "$36.8B",
                frozenset({"microsoft_cloud_revenue"}),
                {
                    "value": "$36.8B",
                    "metric": "gross margin",
                    "unit": "usd_billions",
                    "currency": "USD",
                    "fact_name": "microsoft_cloud_revenue",
                },
            ),
        )
        rows = []
        drivers = []
        for index, (label, sentence, surface, groups, row_fields) in enumerate(cases):
            with self.subTest(helper_case=label):
                start = sentence.index(surface)
                self.assertEqual(
                    service._numeric_target_metric_groups(
                        sentence, start, start + len(surface)
                    ),
                    groups,
                )
            rows.append(
                self._cluster_fact_row(
                    claim_id=f"rejected-cluster-{index}",
                    path=f"drivers[{index}]",
                    **row_fields,
                )
            )
            drivers.append(sentence)
        companion_rows = []
        for index, (_label, sentence, surface, _groups, _row_fields) in enumerate(
            cases
        ):
            if "$23.3B" in sentence and surface != "$23.3B":
                companion_rows.append(
                    self._row(
                        claim_id=f"fcf-value-companion-{index}",
                        path=f"drivers[{index}]",
                        value="$23.3B",
                        metric="free cash flow",
                        period="FY2024-Q4",
                        unit="usd_billions",
                        currency="USD",
                        source_kind="text",
                        quote=sentence,
                    )
                )
            if "18%" in sentence and surface != "18%":
                companion_rows.append(
                    self._row(
                        claim_id=f"fcf-growth-companion-{index}",
                        path=f"drivers[{index}]",
                        value="18%",
                        metric="free cash flow",
                        period="FY2024-Q4",
                        unit="percent",
                        currency=None,
                        source_kind="text",
                        quote=sentence,
                    )
                )
            if "69%" in sentence and surface != "69%":
                companion_rows.append(
                    self._row(
                        claim_id=f"cloud-margin-companion-{index}",
                        path=f"drivers[{index}]",
                        value="69%",
                        metric="gross margin",
                        period="FY2024-Q4",
                        unit="percent",
                        currency=None,
                        source_kind="text",
                        quote=sentence,
                    )
                )
        rows.extend(companion_rows)

        payload = self._payload(rows)
        payload["drivers"] = drivers
        payload["summary"] = "Cloud demand remained durable."
        with self.assertRaises(service.InvestmentValidationError) as raised:
            service._validated_investment_facts(
                json.dumps(payload),
                news_items=service._freeze_json_value([]),
                deterministic_current=service._freeze_json_value(
                    self._tuple_fact_sources()
                ),
                deterministic_prior=service._freeze_json_value({}),
                excerpt="Demand remained durable. " + " ".join(drivers),
            )

        expected_problems = [
            (
                f"numeric_claims[{index}] (claim_id "
                f"'rejected-cluster-{index}'): fact source tuple does not "
                "match its authored target and deterministic leaf"
            )
            for index in range(len(cases))
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




if __name__ == '__main__':
    unittest.main()
