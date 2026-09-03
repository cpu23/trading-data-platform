"""Numeric claim binding tests: metric resolution, inheritance, multi-metric sentences, aliases, and dimensions."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from company_quality_support import (
    MSFT_EXCERPT,
    NumericClaimBindingTestBase,
    epistemic_catalyst,
    msft_claim_row,
)


class NumericClaimMetricsGateTests(NumericClaimBindingTestBase):
    """Tests for metric context, metric inheritance, multi-metric sentences, aliases, and dimensions."""

    def test_verbose_azure_growth_binds_but_same_clause_revenue_does_not(self):
        producer = self._producer(
            excerpt=(
                "This quarter, revenue was $64.7 billion. "
                "Azure grew 29% in FY2024 Q4."
            )
        )
        row = msft_claim_row(
            claim_id="azure_growth_fy24q4",
            value="29%",
            metric="Azure and other cloud services revenue growth",
            period="FY2024 Q4",
            unit="percent",
            currency=None,
            quote="Azure grew 29% in FY2024 Q4",
        )

        report = self._run(
            producer=producer,
            summary="Azure grew 29% in FY2024 Q4.",
            rows=[row],
        )
        self.assertTrue(report.passed, report.failures)
        self.assertEqual(report.failures, ())

        mixed = self._run(
            producer=producer,
            summary=(
                "Azure grew 29% amid resilient revenue in FY2024 Q4."
            ),
            rows=[row],
        )
        self.assertFalse(mixed.passed)
        self.assertEqual(
            [failure.code for failure in mixed.failures],
            ["numeric_claim_tuple_mismatch"],
            mixed.failures,
        )


    def test_canonical_internal_and_alias_does_not_join_ordinary_metrics(self):
        quote = (
            "Azure and other cloud services revenue growth was 29% "
            "in FY2024 Q4"
        )
        producer = self._producer(
            excerpt=(
                "This quarter, revenue was $64.7 billion. "
                f"{quote}."
            )
        )
        row = msft_claim_row(
            claim_id="azure_growth_fy24q4",
            value="29%",
            metric="Azure and other cloud services revenue growth",
            period="FY2024 Q4",
            unit="percent",
            currency=None,
            quote=quote,
        )

        report = self._run_with_json_replay(
            producer=producer,
            summary=f"{quote}.",
            rows=[row],
        )
        self.assertTrue(report.passed, report.failures)
        self.assertEqual(report.failures, ())

        ordinary_conjunction = self._run_with_json_replay(
            producer=producer,
            summary=(
                "Azure demand held and revenue growth was 29% "
                "in FY2024 Q4."
            ),
            rows=[row],
        )
        self.assertFalse(ordinary_conjunction.passed)
        self.assertEqual(
            [failure.code for failure in ordinary_conjunction.failures],
            ["numeric_claim_tuple_mismatch"],
            ordinary_conjunction.failures,
        )


    def test_pass2_exact_azure_sentence_is_atomic_for_direct_and_replay(self):
        deterministic_current = {
            "azure_and_other_cloud_services_growth_gaap_percent": {
                "value": 29,
                "unit": "percent",
                "currency": None,
                "period": "FY2024-Q4",
            },
            "revenue_growth_percent": {
                "value": 29,
                "unit": "percent",
                "currency": None,
                "period": "FY2024-Q4",
            },
        }
        canonical_sentence = (
            "Azure and other cloud services revenue grew 29% year over year "
            "in FY2024-Q4."
        )
        azure_row = {
            "claim_id": "azure_growth_gaap",
            "path": "summary",
            "value": "29%",
            "metric": "Azure and other cloud services revenue growth",
            "period": "FY2024-Q4",
            "unit": "percent",
            "currency": None,
            "source_kind": "fact",
            "fact_path": (
                "deterministic_current."
                "azure_and_other_cloud_services_growth_gaap_percent"
            ),
        }

        report = self._run_with_json_replay(
            summary=canonical_sentence,
            rows=[azure_row],
            deterministic_current=deterministic_current,
        )
        self.assertTrue(report.passed, report.failures)
        self.assertEqual(report.failures, ())

        generic_revenue_row = dict(
            azure_row,
            claim_id="generic_revenue_growth",
            metric="revenue growth",
            fact_path="deterministic_current.revenue_growth_percent",
        )
        generic_revenue = self._run_with_json_replay(
            summary=canonical_sentence,
            rows=[generic_revenue_row],
            deterministic_current=deterministic_current,
        )
        self.assertFalse(generic_revenue.passed)
        self.assertEqual(
            [failure.code for failure in generic_revenue.failures],
            ["numeric_claim_tuple_mismatch"],
            generic_revenue.failures,
        )


    def test_metric_inheritance_and_split_aliases_keep_direct_replay_parity(self):
        fcf_quote = (
            "Free cash flow was $23.3 billion in FY2024 Q4, up 18% "
            "year-over-year"
        )
        cloud_revenue_quote = "Microsoft Cloud revenue was $36.8 billion"
        cloud_margin_quote = (
            "Microsoft Cloud gross margin was 69% year-over-year"
        )
        producer = self._producer(
            document=self._microsoft_document(),
            excerpt=(
                f"{MSFT_EXCERPT} {fcf_quote}. {cloud_revenue_quote}. "
                f"{cloud_margin_quote}."
            ),
        )

        fcf_value_row = {
            "claim_id": "fcf-fy24q4",
            "path": "summary",
            "value": "$23.3 billion",
            "metric": "free cash flow",
            "period": "FY2024-Q4",
            "unit": "usd_billions",
            "currency": "USD",
            "source_kind": "text",
            "quote": fcf_quote,
        }
        fcf_growth_row = {
            "claim_id": "fcf-growth-yoy",
            "path": "summary",
            "value": "18%",
            "metric": "free cash flow",
            "period": "FY2024-Q4",
            "unit": "percent",
            "currency": None,
            "source_kind": "text",
            "quote": fcf_quote,
        }
        cloud_revenue_row = {
            "claim_id": "microsoft-cloud-revenue-period-ended-2024-06-30",
            "path": "summary",
            "value": "$36.8 billion",
            "metric": "Microsoft Cloud revenue",
            "period": "2024-06-30",
            "unit": "usd_billions",
            "currency": "USD",
            "source_kind": "text",
            "quote": cloud_revenue_quote,
        }
        cloud_margin_row = {
            "claim_id": "microsoft-cloud-margin-yoy",
            "path": "summary",
            "value": "69%",
            "metric": "gross margin",
            "period": "year-over-year",
            "unit": "percent",
            "currency": None,
            "source_kind": "text",
            "quote": cloud_margin_quote,
        }
        complete_summary = (
            "Free cash flow was $23.3 billion in FY2024 Q4, up 18% "
            "year-over-year. Microsoft Cloud produced $36.8 billion of "
            "revenue for the period ended 2024-06-30; gross margin was 69% "
            "year-over-year."
        )
        complete_rows = [
            fcf_value_row,
            fcf_growth_row,
            cloud_revenue_row,
            cloud_margin_row,
        ]

        report = self._run_with_json_replay(
            producer=producer,
            summary=complete_summary,
            rows=complete_rows,
        )
        self.assertTrue(report.passed, report.failures)
        self.assertEqual(report.failures, ())

        rejected = []

        swapped_fcf = [dict(row) for row in complete_rows]
        swapped_fcf[1]["metric"] = "capital expenditures"
        rejected.append(
            (
                "swapped inherited FCF metric",
                complete_summary,
                swapped_fcf,
                1,
                "fcf-growth-yoy",
            )
        )

        swapped_cloud = [dict(row) for row in complete_rows]
        swapped_cloud[2]["metric"] = "gross margin"
        rejected.append(
            (
                "swapped split Microsoft Cloud metric",
                complete_summary,
                swapped_cloud,
                2,
                "microsoft-cloud-revenue-period-ended-2024-06-30",
            )
        )

        for label, separator in (
            ("semicolon inheritance boundary", "; "),
            ("ordinary-and inheritance boundary", " and "),
        ):
            boundary_summary = (
                "Free cash flow was $23.3 billion in FY2024 Q4"
                f"{separator}up 18% year-over-year. Microsoft Cloud produced "
                "$36.8 billion of revenue for the period ended 2024-06-30; "
                "gross margin was 69% year-over-year."
            )
            rejected.append(
                (
                    label,
                    boundary_summary,
                    [dict(row) for row in complete_rows],
                    1,
                    "fcf-growth-yoy",
                )
            )

        new_subject_summary = (
            "Free cash flow was $23.3 billion in FY2024 Q4, revenue was up "
            "18% year-over-year. Microsoft Cloud produced $36.8 billion of "
            "revenue for the period ended 2024-06-30; gross margin was 69% "
            "year-over-year."
        )
        rejected.append(
            (
                "comma followed by a new subject",
                new_subject_summary,
                [dict(row) for row in complete_rows],
                1,
                "fcf-growth-yoy",
            )
        )

        for label, summary, rows, bad_index, claim_id in rejected:
            with self.subTest(case=label):
                rejected_report = self._run_with_json_replay(
                    producer=producer,
                    summary=summary,
                    rows=rows,
                )
                self.assertFalse(rejected_report.passed)
                self.assertEqual(
                    [
                        (
                            failure.code,
                            failure.path,
                            failure.observed,
                        )
                        for failure in rejected_report.failures
                    ],
                    [
                        (
                            "numeric_claim_tuple_mismatch",
                            f"numeric_claims[{bad_index}]",
                            {"claim_id": claim_id},
                        )
                    ],
                    rejected_report.failures,
                )

        missing_fcf_reporting_period = self._run_with_json_replay(
            producer=producer,
            summary=(
                "Free cash flow was $23.3 billion, up 18% year-over-year. "
                "Microsoft Cloud produced $36.8 billion of revenue for the "
                "period ended 2024-06-30; gross margin was 69% year-over-year."
            ),
            rows=[dict(row) for row in complete_rows],
        )
        self.assertFalse(missing_fcf_reporting_period.passed)
        self.assertEqual(
            [
                (failure.code, failure.path, failure.observed)
                for failure in missing_fcf_reporting_period.failures
            ],
            [
                (
                    "numeric_claim_tuple_mismatch",
                    "numeric_claims[0]",
                    {"claim_id": "fcf-fy24q4"},
                ),
                (
                    "numeric_claim_tuple_mismatch",
                    "numeric_claims[1]",
                    {"claim_id": "fcf-growth-yoy"},
                ),
            ],
            missing_fcf_reporting_period.failures,
        )

        missing_reporting_period = self._run_with_json_replay(
            producer=producer,
            summary=(
                "Free cash flow was $23.3 billion in FY2024 Q4, up 18% "
                "year-over-year. Microsoft Cloud produced $36.8 billion of "
                "revenue; gross margin was 69% year-over-year."
            ),
            rows=[dict(row) for row in complete_rows],
        )
        self.assertFalse(missing_reporting_period.passed)
        self.assertEqual(
            [
                (failure.code, failure.path, failure.observed)
                for failure in missing_reporting_period.failures
            ],
            [
                (
                    "numeric_claim_tuple_mismatch",
                    "numeric_claims[2]",
                    {
                        "claim_id": (
                            "microsoft-cloud-revenue-period-ended-2024-06-30"
                        )
                    },
                )
            ],
            missing_reporting_period.failures,
        )


    def test_pass3_two_metric_sentence_binds_each_numeric_occurrence(self):
        period = "FY2024-Q4 (three months ended 2024-06-30)"
        deterministic_current = {
            "azure_and_other_cloud_services_growth_gaap_percent": {
                "value": 29,
                "unit": "percent_yoy",
                "currency": None,
                "period": period,
            },
            "azure_growth_from_ai_services_points": {
                "value": 8,
                "unit": "percentage_points",
                "currency": None,
                "period": period,
            },
            "commercial_bookings_growth_contribution_points": {
                "value": 8,
                "unit": "percentage_points",
                "currency": None,
                "period": period,
            },
            "azure_growth_from_ai_services_percent": {
                "value": 8,
                "unit": "percent",
                "currency": None,
                "period": period,
            },
            "azure_and_other_cloud_services_revenue_growth_guidance": {
                "value": "28% to 29%",
                "unit": "percent_yoy_range",
                "currency": None,
                "period": "FY2025-Q1 guidance issued 2024-07-30",
            },
        }

        def azure_rows():
            return [
                {
                    "claim_id": "azure-growth-gaap",
                    "path": "/drivers/0",
                    "value": "29%",
                    "metric": (
                        "Azure and other cloud services year-over-year growth"
                    ),
                    "period": period,
                    "unit": "percent",
                    "currency": None,
                    "source_kind": "fact",
                    "fact_path": (
                        "deterministic_current."
                        "azure_and_other_cloud_services_growth_gaap_percent."
                        "value"
                    ),
                },
                {
                    "claim_id": "azure-ai-growth-contribution",
                    "path": "/drivers/0",
                    "value": 8,
                    "metric": "Azure growth contribution from AI services",
                    "period": period,
                    "unit": "percentage_points",
                    "currency": None,
                    "source_kind": "fact",
                    "fact_path": (
                        "deterministic_current."
                        "azure_growth_from_ai_services_points.value"
                    ),
                },
            ]

        def candidate_rows():
            return [
                {
                    "claim_id": "summary-azure-growth",
                    "path": "/summary",
                    "value": "29%",
                    "metric": (
                        "Azure and other cloud services year-over-year growth"
                    ),
                    "period": period,
                    "unit": "percent",
                    "currency": None,
                    "source_kind": "fact",
                    "fact_path": (
                        "deterministic_current."
                        "azure_and_other_cloud_services_growth_gaap_percent."
                        "value"
                    ),
                },
                {
                    "claim_id": "summary-ai-growth-contribution",
                    "path": "/summary",
                    "value": 8,
                    "metric": "Azure growth contribution from AI services",
                    "period": period,
                    "unit": "percentage_points",
                    "currency": None,
                    "source_kind": "fact",
                    "fact_path": (
                        "deterministic_current."
                        "azure_growth_from_ai_services_points.value"
                    ),
                },
            ]

        def year_over_year_candidate_rows():
            rows = candidate_rows()
            rows[1]["metric"] = (
                "AI services contribution to year-over-year Azure growth"
            )
            return rows

        def report_for(sentence, rows, *, candidate=False):
            payload = self._payload(
                sentence if candidate else "Cloud demand remained durable.",
                rows,
            )
            if not candidate:
                payload["drivers"] = [sentence]
            return self._run_payload_with_json_replay(
                payload,
                deterministic_current=deterministic_current,
            )

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
                            "Azure grew 29% in FY2024 Q4 including "
                            f"8 {surface} from AI services in FY2024 Q4"
                        ),
                        azure_rows(),
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
                        candidate_rows(),
                        True,
                        ("29%", 8),
                    ),
                )
            )
        exact_year_over_year_sentence = (
            "AI services contributed 8 percentage points to year-over-year "
            "Azure growth in FY2024 Q4"
        )
        controls.append(
            (
                "year-over-year Azure contribution recipient",
                exact_year_over_year_sentence,
                year_over_year_candidate_rows()[1:],
                True,
                (8,),
            )
        )

        controls.append(
            (
                "explicit endpoint cluster",
                (
                    "Azure and other cloud services revenue growth guidance "
                    "was 28% for FY2025 Q1, and Azure and other cloud services "
                    "revenue growth guidance was 29% for FY2025 Q1."
                ),
                [
                    {
                        "claim_id": "azure-guide-low-range",
                        "path": "/drivers/0",
                        "value": "28%",
                        "metric": (
                            "Azure and other cloud services revenue growth "
                            "guidance"
                        ),
                        "period": (
                            "FY2025-Q1 guidance issued 2024-07-30"
                        ),
                        "unit": "percent",
                        "currency": None,
                        "source_kind": "fact",
                        "fact_path": (
                            "deterministic_current."
                            "azure_and_other_cloud_services_revenue_growth_guidance."
                            "value"
                        ),
                    },
                    {
                        "claim_id": "azure-guide-high-range",
                        "path": "/drivers/0",
                        "value": "29%",
                        "metric": (
                            "Azure and other cloud services revenue growth "
                            "guidance"
                        ),
                        "period": (
                            "FY2025-Q1 guidance issued 2024-07-30"
                        ),
                        "unit": "percent",
                        "currency": None,
                        "source_kind": "fact",
                        "fact_path": (
                            "deterministic_current."
                            "azure_and_other_cloud_services_revenue_growth_guidance."
                            "value"
                        ),
                    },
                ],
                False,
                ("28%", "29%"),
            )
        )
        for label, sentence, rows, candidate, expected_values in controls:
            with self.subTest(control=label):
                report = report_for(sentence, rows, candidate=candidate)
                self.assertTrue(report.passed, report.failures)
                self.assertEqual(report.failures, ())
                self.assertEqual(
                    tuple(row["value"] for row in rows),
                    expected_values,
                )

        canonical_sentence = (
            "Azure and other cloud services grew 29% in FY2024 Q4, with "
            "8 percentage points from AI services in FY2024 Q4 despite demand "
            "exceeding available capacity."
        )
        candidate_sentence = (
            "Microsoft exited FY24 with broad commercial momentum: Azure and "
            "other cloud services grew 29% in FY2024 Q4 year over year, with "
            "AI services contributing 8 points in FY2024 Q4, while demand "
            "remained above available capacity."
        )
        period = "FY2024-Q4 (three months ended 2024-06-30)"
        paired_year_over_year_sentence = (
            "Azure and other cloud services revenue grew 29% year over year in "
            f"{period}, while AI services contributed 8 percentage points to "
            f"year-over-year Azure growth in {period}."
        )

        cases = [
            (
                "year-over-year recipient swapped 29 and 8 metrics and sources",
                paired_year_over_year_sentence,
                year_over_year_candidate_rows,
                True,
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
                "year-over-year recipient percent unit",
                paired_year_over_year_sentence,
                year_over_year_candidate_rows,
                True,
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
                "year-over-year recipient wrong same-valued source",
                paired_year_over_year_sentence,
                year_over_year_candidate_rows,
                True,
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
                "year-over-year recipient reversed roles",
                (
                    "Azure and other cloud services revenue grew 29% year over "
                    f"year in {period}, while Azure growth contributed 8 "
                    "percentage points to year-over-year AI services growth in "
                    f"{period}."
                ),
                year_over_year_candidate_rows,
                True,
                {},
                (1,),
            ),
            (
                "year-over-year recipient punctuation-separated Azure",
                (
                    "Azure and other cloud services revenue grew 29% year over "
                    f"year in {period}. Commercial bookings growth included 8 "
                    f"percentage points in {period}; year-over-year Azure "
                    f"growth was 8 percentage points in {period}."
                ),
                year_over_year_candidate_rows,
                True,
                {},
                (1,),
            ),
            (
                "swapped metrics",
                canonical_sentence,
                azure_rows,
                False,
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
                azure_rows,
                False,
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
                azure_rows,
                False,
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
                "29 total growth expressed as points",
                canonical_sentence,
                azure_rows,
                False,
                {0: {"unit": "percentage_points"}},
                (0,),
            ),
            (
                "trailing other-metric 8",
                (
                    "Azure and other cloud services grew 29% in FY2024 Q4 "
                    "while AI demand exceeded capacity; commercial bookings "
                    "growth included 8 percentage points in FY2024 Q4."
                ),
                azure_rows,
                False,
                {},
                (1,),
            ),
            (
                "leading other-metric 8",
                (
                    "Commercial bookings growth included 8 percentage points "
                    "in FY2024 Q4, while Azure and other cloud services grew "
                    "29% in FY2024 Q4 and AI services demand exceeded capacity."
                ),
                azure_rows,
                False,
                {},
                (1,),
            ),
            (
                "candidate bookings metric and source",
                candidate_sentence,
                candidate_rows,
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
                candidate_rows,
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
                        candidate_rows,
                        True,
                        percent_override,
                        (1,),
                    ),
                    (
                        f"unpunctuated percent fact against {surface}",
                        (
                            "Azure grew 29% in FY2024 Q4 including "
                            f"8 {surface} from AI services in FY2024 Q4"
                        ),
                        azure_rows,
                        False,
                        percent_override,
                        (1,),
                    ),
                )
            )
        for qualifier in ("index", "data"):
            cases.append(
                (
                    f"candidate {qualifier} points percent control",
                    (
                        "Azure and other cloud services grew 29% in FY2024 Q4 "
                        "year over year, with AI services contributing "
                        f"8 {qualifier} points in FY2024 Q4."
                    ),
                    candidate_rows,
                    True,
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
                )
            )

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
                report = report_for(sentence, rows, candidate=candidate)

                self.assertFalse(report.passed)
                self.assertEqual(
                    [failure.code for failure in report.failures],
                    ["numeric_claim_tuple_mismatch"] * len(bad_indexes),
                    report.failures,
                )
                self.assertEqual(
                    [failure.path for failure in report.failures],
                    [
                        f"numeric_claims[{index}]"
                        for index in bad_indexes
                    ],
                    report.failures,
                )
                self.assertEqual(
                    [failure.observed for failure in report.failures],
                    [
                        {"claim_id": rows[index]["claim_id"]}
                        for index in bad_indexes
                    ],
                    report.failures,
                )


    def test_metric_context_does_not_cross_an_ordinary_and_boundary(self):
        deterministic_current = {
            "azure_and_other_cloud_services_growth_gaap_percent": {
                "value": 29,
                "unit": "percent",
                "currency": None,
                "period": "FY2024-Q4",
            },
        }
        row = {
            "claim_id": "azure_growth_gaap",
            "path": "summary",
            "value": "29%",
            "metric": "Azure and other cloud services revenue growth",
            "period": "FY2024-Q4",
            "unit": "percent",
            "currency": None,
            "source_kind": "fact",
            "fact_path": (
                "deterministic_current."
                "azure_and_other_cloud_services_growth_gaap_percent"
            ),
        }

        for label, summary in (
            (
                "same clause",
                "The Azure growth contribution was 29% in FY2024 Q4.",
            ),
            (
                "canonical internal and",
                "Azure and other cloud services revenue growth was 29% "
                "in FY2024 Q4.",
            ),
        ):
            with self.subTest(control=label):
                report = self._run_with_json_replay(
                    summary=summary,
                    rows=[row],
                    deterministic_current=deterministic_current,
                )
                self.assertTrue(report.passed, report.failures)
                self.assertEqual(report.failures, ())

        leakage = self._run_with_json_replay(
            summary=(
                "Azure growth remained strong and the contribution was 29% "
                "in FY2024 Q4."
            ),
            rows=[row],
            deterministic_current=deterministic_current,
        )
        self.assertFalse(leakage.passed)
        self.assertEqual(
            [failure.code for failure in leakage.failures],
            ["numeric_claim_tuple_mismatch"],
            leakage.failures,
        )


    def test_guide_and_guidance_aliases_require_an_explicit_guidance_label(self):
        for alias in ("guide", "guidance"):
            with self.subTest(alias=alias):
                quote = (
                    f"Revenue {alias} was $65 billion in Q1 FY2025"
                )
                producer = self._producer(
                    excerpt=(
                        "This quarter, revenue was $64.7 billion. "
                        f"{quote}."
                    )
                )
                row = msft_claim_row(
                    claim_id=f"revenue_{alias}_fy25q1",
                    value="$65B",
                    metric="revenue guidance",
                    period="Q1 FY2025",
                    unit="usd_billions",
                    currency="USD",
                    quote=quote,
                )
                report = self._run_with_json_replay(
                    producer=producer,
                    summary=f"{quote}.",
                    rows=[row],
                )
                self.assertTrue(report.passed, report.failures)
                self.assertEqual(report.failures, ())

        quote = "Revenue guidance was $65 billion in Q1 FY2025"
        producer = self._producer(
            excerpt=(
                "This quarter, revenue was $64.7 billion. "
                f"{quote}."
            )
        )
        row = msft_claim_row(
            claim_id="revenue_guidance_fy25q1",
            value="$65B",
            metric="revenue guidance",
            period="Q1 FY2025",
            unit="usd_billions",
            currency="USD",
            quote=quote,
        )
        future_actual = self._run_with_json_replay(
            producer=producer,
            summary="Revenue will be $65 billion in Q1 FY2025.",
            rows=[row],
        )
        self.assertFalse(future_actual.passed)
        self.assertEqual(
            [failure.code for failure in future_actual.failures],
            ["numeric_claim_tuple_mismatch"],
            future_actual.failures,
        )


    def test_catalyst_horizon_cannot_borrow_sibling_expected_outcome_metric(self):
        quote = "Azure grew 29% in FY2024 Q4"
        producer = self._producer(
            excerpt=(
                "This quarter, revenue was $64.7 billion. "
                f"{quote}."
            )
        )
        row = msft_claim_row(
            claim_id="azure_growth_horizon",
            path="/catalysts/0/horizon",
            value="29%",
            metric="Azure and other cloud services revenue growth",
            period="FY2024 Q4",
            unit="percent",
            currency=None,
            quote=quote,
        )
        payload = self._payload("Demand remains durable.", [row])
        catalyst = epistemic_catalyst(
            "Capacity update",
            "29% in FY2024 Q4",
            quote,
        )
        catalyst["expected_outcome"] = (
            "Azure and other cloud services revenue growth becomes measurable."
        )
        payload["catalysts"] = [catalyst]

        report = self._run_payload_with_json_replay(
            payload,
            producer=producer,
        )
        self.assertFalse(report.passed)
        self.assertEqual(
            [
                (failure.code, failure.path, failure.root_category)
                for failure in report.failures
            ],
            [
                (
                    "numeric_claim_tuple_mismatch",
                    "numeric_claims[0]",
                    "filing_evidence",
                )
            ],
            report.failures,
        )


    def test_catalyst_horizon_rejects_evidence_neighbor_and_period_borrowing(self):
        quote = "Azure grew 29% in FY2024 Q4"
        producer = self._producer(
            excerpt=(
                "This quarter, revenue was $64.7 billion. "
                f"{quote}."
            )
        )

        cases = (
            (
                "sibling evidence",
                [
                    epistemic_catalyst(
                        "Capacity update",
                        "29% in FY2024 Q4",
                        quote,
                    )
                ],
                "/catalysts/0/horizon",
            ),
            (
                "neighbor catalyst",
                [
                    epistemic_catalyst("Azure growth", "Near term", quote),
                    epistemic_catalyst(
                        "Capacity update",
                        "29% in FY2024 Q4",
                        quote,
                    ),
                ],
                "/catalysts/1/horizon",
            ),
            (
                "sibling period",
                [
                    epistemic_catalyst(
                        "Azure growth in FY2024 Q4",
                        "29% in FY2025 Q1",
                        quote,
                    )
                ],
                "/catalysts/0/horizon",
            ),
        )
        for label, catalysts, path in cases:
            with self.subTest(case=label):
                row = msft_claim_row(
                    claim_id=f"azure_growth_{label.replace(' ', '_')}",
                    path=path,
                    value="29%",
                    metric="Azure and other cloud services revenue growth",
                    period="FY2024 Q4",
                    unit="percent",
                    currency=None,
                    quote=quote,
                )
                payload = self._payload("Demand remains durable.", [row])
                payload["catalysts"] = catalysts
                report = self._run_payload_with_json_replay(
                    payload,
                    producer=producer,
                )
                self.assertFalse(report.passed)
                self.assertEqual(
                    [failure.code for failure in report.failures],
                    ["numeric_claim_tuple_mismatch"],
                    report.failures,
                )


    def test_equivalent_renderings_normalize_to_one_tuple(self):
        for label, value in (
            ("suffix", "$19B"),
            ("words", "19 billion"),
            ("bare", "19"),
        ):
            with self.subTest(rendering=label):
                report = self._run(
                    summary=(
                        "Capital expenditures including finance leases were "
                        f"{value} in FY2024 Q4."
                    ),
                    rows=[msft_claim_row(value=value)],
                )
                self.assertTrue(report.passed, report.failures)


    def test_fact_backed_and_derived_claims_pass_with_operation_identity(self):
        deterministic_current = {
            "capital_expenditures_including_finance_leases": {
                "value": 19.0,
                "unit": "usd_billions",
                "currency": "USD",
                "period": "FY2024-Q4",
            },
            "operating_cash_flow": {
                "value": 37.2,
                "unit": "usd_billions",
                "currency": "USD",
                "period": "FY2024-Q4",
            },
            "cash_paid_for_property_and_equipment": {
                "value": 13.9,
                "unit": "usd_billions",
                "currency": "USD",
                "period": "FY2024-Q4",
            },
            "free_cash_flow": {
                "value": 23.3,
                "unit": "usd_billions",
                "currency": "USD",
                "period": "FY2024-Q4",
                "source": "derived",
                "concept": (
                    "derived: operating_cash_flow - "
                    "cash_paid_for_property_and_equipment"
                ),
            },
        }
        fact_row = dict(
            msft_claim_row(claim_id="capex_fact", source_kind="fact"),
            fact_path=(
                "deterministic_current"
                ".capital_expenditures_including_finance_leases.value"
            ),
            value=19.0,
        )
        del fact_row["quote"]
        derived_row = {
            "claim_id": "fcf_identity",
            "path": "summary",
            "value": 23.3,
            "metric": "free cash flow",
            "period": "FY2024 Q4",
            "unit": "usd_billions",
            "currency": "USD",
            "source_kind": "arithmetic",
            "operation": "difference",
            "operands": [
                "deterministic_current.operating_cash_flow.value",
                "deterministic_current.cash_paid_for_property_and_equipment.value",
            ],
        }
        report = self._run(
            summary=(
                "Capex including finance leases reached $19 billion in "
                "FY2024 Q4 and free cash flow was $23.3 billion in FY2024 Q4."
            ),
            rows=[fact_row, derived_row],
            deterministic_current=deterministic_current,
        )
        self.assertTrue(report.passed, report.failures)


    def test_same_number_at_one_path_binds_only_its_matching_metric(self):
        producer = self._producer(
            excerpt=(
                "This quarter, revenue was $64.7 billion. Capital "
                "expenditures including finance leases were $19 billion in "
                "FY2024 Q4. Free cash flow was $19 billion in FY2024 Q4."
            )
        )
        summary = (
            "Capital expenditures including finance leases were $19 billion "
            "in FY2024 Q4 and free cash flow was $19 billion in FY2024 Q4."
        )
        capex_row = msft_claim_row(
            quote=(
                "Capital expenditures including finance leases were $19 "
                "billion in FY2024 Q4"
            )
        )

        report = self._run(
            producer=producer,
            summary=summary,
            rows=[capex_row],
        )
        self.assertFalse(report.passed)
        self.assertEqual(
            [failure.code for failure in report.failures],
            ["numeric_claim_unbound"],
        )

        free_cash_flow_row = msft_claim_row(
            claim_id="free_cash_flow_fy24q4",
            metric="free cash flow",
            quote="Free cash flow was $19 billion in FY2024 Q4",
        )
        report = self._run(
            producer=producer,
            summary=summary,
            rows=[capex_row, free_cash_flow_row],
        )
        self.assertTrue(report.passed, report.failures)
        self.assertEqual(report.failures, ())


    def test_fact_row_metric_must_match_equal_valued_referenced_fact(self):
        deterministic_current = {
            "capital_expenditures": {
                "value": 19.0,
                "unit": "usd_billions",
                "currency": "USD",
                "period": "FY2024-Q4",
            },
            "free_cash_flow": {
                "value": 19.0,
                "unit": "usd_billions",
                "currency": "USD",
                "period": "FY2024-Q4",
            },
        }
        row = {
            "claim_id": "free_cash_flow_fy24q4",
            "path": "summary",
            "value": 19.0,
            "metric": "free cash flow",
            "period": "FY2024 Q4",
            "unit": "usd_billions",
            "currency": "USD",
            "source_kind": "fact",
            "fact_path": "deterministic_current.capital_expenditures.value",
        }

        report = self._run(
            summary="Free cash flow was $19 billion in FY2024 Q4.",
            rows=[row],
            deterministic_current=deterministic_current,
        )
        self.assertFalse(report.passed)
        self.assertEqual(
            [failure.code for failure in report.failures],
            ["numeric_claim_tuple_mismatch"],
            report.failures,
        )

        supported = self._run(
            summary="Free cash flow was $19 billion in FY2024 Q4.",
            rows=[
                dict(
                    row,
                    fact_path="deterministic_current.free_cash_flow.value",
                )
            ],
            deterministic_current=deterministic_current,
        )
        self.assertTrue(supported.passed, supported.failures)
        self.assertEqual(supported.failures, ())


    def test_untyped_source_page_scalar_cannot_supply_usd_fact(self):
        deterministic_current = {
            "revenue": {
                "value": 64.7,
                "unit": "usd_millions",
                "currency": "USD",
                "period": "FY2024-Q4",
                "source_page": 19,
            }
        }
        row = {
            "claim_id": "revenue_fy24q4",
            "path": "summary",
            "value": 19.0,
            "metric": "revenue",
            "period": "FY2024 Q4",
            "unit": "usd_millions",
            "currency": "USD",
            "source_kind": "fact",
            "fact_path": "deterministic_current.revenue.source_page",
        }

        report = self._run(
            summary="Revenue was $19 million in FY2024 Q4.",
            rows=[row],
            deterministic_current=deterministic_current,
        )
        self.assertFalse(report.passed)
        self.assertEqual(
            [failure.code for failure in report.failures],
            ["numeric_claim_source_unresolved"],
            report.failures,
        )


    def test_nested_percent_fact_leaf_inherits_current_period_and_null_currency(self):
        deterministic_current = {
            "revenue": {
                "value": 64.7,
                "unit": "usd_billions",
                "currency": "USD",
                "period": "FY2024-Q4",
                "yoy_growth_gaap_percent": 15.0,
            }
        }
        row = {
            "claim_id": "revenue_gaap_growth",
            "path": "summary",
            "value": "15%",
            "metric": "revenue GAAP year-over-year growth",
            "period": "FY2024 Q4",
            "unit": "percent",
            "currency": None,
            "source_kind": "fact",
            "fact_path": (
                "deterministic_current.revenue.yoy_growth_gaap_percent"
            ),
        }
        report = self._run(
            summary=(
                "Revenue GAAP year-over-year growth was 15% in FY2024 Q4."
            ),
            rows=[row],
            deterministic_current=deterministic_current,
        )
        self.assertTrue(report.passed, report.failures)
        self.assertEqual(report.failures, ())


    def test_declared_dimensionally_compatible_difference_passes(self):
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
        row = {
            "claim_id": "declared_free_cash_flow",
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
        self.assertTrue(report.passed, report.failures)
        self.assertEqual(report.failures, ())


