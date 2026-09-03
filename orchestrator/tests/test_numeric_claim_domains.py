"""Numeric claim binding tests: target domains, periods, horizons, and document aliases."""

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


class NumericClaimDomainsGateTests(NumericClaimBindingTestBase):
    """Tests for narrative and numeric target domains, period bindings, and document aliases."""

    def test_supported_19b_capex_quarterly_claim_passes(self):
        report = self._run(
            summary=(
                "Capital expenditures including finance leases were $19 "
                "billion in FY2024 Q4, in line with expectations."
            ),
            rows=[msft_claim_row()],
        )
        self.assertTrue(report.passed, report.failures)
        self.assertEqual(self._codes(report, "numeric_claim_unbound"), [])


    def test_narrative_target_domain_accepts_supported_text_leaves(self):
        claim_text = (
            "Capital expenditures including finance leases were $19 billion "
            "in FY2024 Q4, in line with expectations"
        )
        cases = (
            ("summary", "summary"),
            ("JSON Pointer catalyst horizon", "/catalysts/0/horizon"),
            (
                "JSON Pointer qualitative evidence",
                "/qualitative/ai_demand/evidence",
            ),
        )

        for label, path in cases:
            with self.subTest(case=label):
                payload = self._payload(
                    claim_text if path == "summary" else "Demand remains durable.",
                    [msft_claim_row(path=path)],
                )
                payload["catalysts"] = [
                    epistemic_catalyst(
                        "Capacity update",
                        (
                            claim_text
                            if path == "/catalysts/0/horizon"
                            else "Near term"
                        ),
                        "in line with expectations",
                    )
                ]
                if path == "/qualitative/ai_demand/evidence":
                    payload["qualitative"]["ai_demand"]["evidence"] = claim_text

                live_passed, live_failure_code, report = (
                    self._target_domain_outcomes(payload)
                )

                self.assertTrue(live_passed)
                self.assertIsNone(live_failure_code)
                self.assertTrue(report.passed, report.failures)
                self.assertEqual(report.failures, ())
                self.assertEqual(live_passed, report.passed)


    def test_narrative_target_domain_rejects_nonleaves_and_missing_leaves(self):
        cases = (
            ("catalyst container", "/catalysts/0"),
            ("classification confidence", "/classification/confidence"),
            ("numeric ledger quote", "/numeric_claims/0/quote"),
            ("missing leaf", "/catalysts/0/missing"),
            ("out-of-range leaf", "/catalysts/9/horizon"),
        )

        for label, path in cases:
            with self.subTest(case=label):
                payload = self._payload(
                    "Demand remains durable.",
                    [msft_claim_row(path=path)],
                )
                payload["catalysts"] = [
                    epistemic_catalyst(
                        "Capacity update",
                        "Near term",
                        "in line with expectations",
                    )
                ]

                live_passed, live_failure_code, report = (
                    self._target_domain_outcomes(payload)
                )

                self.assertFalse(live_passed)
                self.assertEqual(
                    live_failure_code,
                    service.VALIDATION_JSON_SCHEMA,
                )
                self.assertFalse(report.passed)
                self.assertIn(
                    "numeric_claim_target_missing",
                    [failure.code for failure in report.failures],
                )
                self.assertEqual(live_passed, report.passed)


    def test_split_eps_phrase_binds_but_bare_and_net_earnings_do_not(self):
        deterministic_current = {
            "earnings_per_share": {
                "value": -0.06,
                "unit": "usd_per_share",
                "currency": "USD",
                "period": "FY2026",
            }
        }
        row = {
            "claim_id": "eps_fy2026",
            "path": "summary",
            "value": -0.06,
            "metric": "earnings per share",
            "period": "FY2026",
            "unit": "usd_per_share",
            "currency": "USD",
            "source_kind": "fact",
            "fact_path": "deterministic_current.earnings_per_share.value",
        }

        report = self._run(
            summary="Earnings moved to negative $0.06 per share in FY2026.",
            rows=[row],
            deterministic_current=deterministic_current,
        )
        self.assertTrue(report.passed, report.failures)
        self.assertEqual(report.failures, ())

        for label, summary in (
            (
                "bare-earnings",
                "Earnings moved to negative $0.06 in FY2026.",
            ),
            (
                "net-earnings",
                "Net earnings moved to negative $0.06 per share in FY2026.",
            ),
        ):
            with self.subTest(case=label):
                report = self._run(
                    summary=summary,
                    rows=[row],
                    deterministic_current=deterministic_current,
                )
                self.assertFalse(report.passed)
                self.assertEqual(
                    [failure.code for failure in report.failures],
                    ["numeric_claim_tuple_mismatch"],
                    report.failures,
                )


    def test_fact_range_endpoints_bind_but_nonendpoint_fails(self):
        deterministic_current = {
            "azure_and_other_cloud_services_revenue_growth_guidance": {
                "value": "28% to 29%",
                "unit": "percent_yoy_range",
                "currency": None,
                "period": "FY2025-Q1 guidance issued 2024-07-30",
            }
        }

        def row(value):
            return {
                "claim_id": "azure_guidance_revenue_growth",
                "path": "summary",
                "value": value,
                "metric": (
                    "Azure and other cloud services revenue growth guidance"
                ),
                "period": "FY2025-Q1 guidance issued 2024-07-30",
                "unit": "percent",
                "currency": None,
                "source_kind": "fact",
                "fact_path": (
                    "deterministic_current."
                    "azure_and_other_cloud_services_revenue_growth_guidance.value"
                ),
            }

        for endpoint in ("28%", "29%"):
            with self.subTest(endpoint=endpoint):
                report = self._run_with_json_replay(
                    summary=(
                        "Azure and other cloud services revenue growth guidance "
                        f"was {endpoint} for Q1 FY2025."
                    ),
                    rows=[row(endpoint)],
                    deterministic_current=deterministic_current,
                )
                self.assertTrue(report.passed, report.failures)
                self.assertEqual(report.failures, ())

        for label, unsupported in (
            ("interpolated", "28.5%"),
            ("outside", "30%"),
            ("period-year", "2025%"),
            ("period-quarter", "1%"),
            ("negative", "-28%"),
        ):
            with self.subTest(case=label):
                report = self._run_with_json_replay(
                    summary=(
                        "Azure and other cloud services revenue growth guidance "
                        f"was {unsupported} for Q1 FY2025."
                    ),
                    rows=[row(unsupported)],
                    deterministic_current=deterministic_current,
                )
                self.assertFalse(report.passed)
                self.assertEqual(
                    [failure.code for failure in report.failures],
                    ["numeric_claim_tuple_mismatch"],
                    report.failures,
                )


    def test_live_and_hard_gate_replay_share_exact_fact_tuple_outcomes(self):
        deterministic_current = {
            "microsoft_cloud_gross_margin_guidance": {
                "value": 70,
                "unit": "percent",
                "currency": None,
                "period": "FY2025-Q1 guidance issued 2024-07-30",
            },
            "microsoft_cloud_gross_margin_reported": {
                "value": 69,
                "unit": "percent",
                "currency": None,
                "period": "FY2024-Q4",
            },
            "azure_and_other_cloud_services_revenue_growth_guidance": {
                "value": "28% to 29%",
                "unit": "percent_yoy_range",
                "currency": None,
                "period": "FY2025-Q1 guidance issued 2024-07-30",
            },
        }
        producer = self._producer(
            deterministic_current=deterministic_current
        )

        def fact_row(**overrides):
            row = {
                "claim_id": "cloud-margin-guide",
                "path": "summary",
                "value": "70%",
                "metric": "Microsoft Cloud gross margin guidance",
                "period": "FY2025-Q1 guidance issued 2024-07-30",
                "unit": "percent",
                "currency": None,
                "source_kind": "fact",
                "fact_path": (
                    "deterministic_current."
                    "microsoft_cloud_gross_margin_guidance.value"
                ),
            }
            row.update(overrides)
            return row

        def outcomes(summary, row):
            payload = self._payload(summary, [row])
            try:
                service._validated_investment_facts(
                    json.dumps(payload),
                    excerpt=producer.excerpt,
                    news_items=[
                        service._freeze_json_value(dict(item))
                        for item in producer.news_items
                    ],
                    deterministic_current=service._freeze_json_value(
                        deterministic_current
                    ),
                    deterministic_prior=service._freeze_json_value({}),
                    relationship_facts=service._freeze_json_value({}),
                    material_relationships=(),
                )
            except service.InvestmentValidationError as error:
                live_passed = False
                live_error = error
            else:
                live_passed = True
                live_error = None
            report = self._run_payload_with_json_replay(
                payload,
                producer=producer,
                deterministic_current=deterministic_current,
            )
            self.assertEqual(live_passed, report.passed, report.failures)
            return live_error, report

        accepted = (
            (
                "exact scalar",
                (
                    "Microsoft Cloud gross margin guidance was roughly 70% "
                    "for Q1 FY2025."
                ),
                fact_row(),
            ),
            (
                "range low endpoint",
                (
                    "Azure and other cloud services revenue growth guidance "
                    "was 28% for Q1 FY2025."
                ),
                fact_row(
                    claim_id="azure-guide-low",
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
            ),
            (
                "range high endpoint",
                (
                    "Azure and other cloud services revenue growth guidance "
                    "was 29% for Q1 FY2025."
                ),
                fact_row(
                    claim_id="azure-guide-high",
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
            ),
        )
        for label, summary, row in accepted:
            with self.subTest(case=label):
                live_error, report = outcomes(summary, row)
                self.assertIsNone(live_error)
                self.assertTrue(report.passed, report.failures)
                self.assertEqual(report.failures, ())

        rejected = (
            (
                "target lacks guidance identity",
                (
                    "Microsoft Cloud gross margin was roughly 70% for "
                    "Q1 FY2025."
                ),
                fact_row(),
            ),
            (
                "wrong coefficient",
                (
                    "Microsoft Cloud gross margin guidance was roughly 69% "
                    "for Q1 FY2025."
                ),
                fact_row(value="69%"),
            ),
            (
                "wrong metric",
                (
                    "Operating margin guidance was roughly 70% for "
                    "Q1 FY2025."
                ),
                fact_row(metric="operating margin guidance"),
            ),
            (
                "wrong period",
                (
                    "Microsoft Cloud gross margin guidance was roughly 70% "
                    "for Q4 FY2024."
                ),
                fact_row(period="FY2024-Q4"),
            ),
            (
                "wrong unit",
                (
                    "Microsoft Cloud gross margin guidance was roughly "
                    "$70 billion for Q1 FY2025."
                ),
                fact_row(value="$70B", unit="usd_billions", currency="USD"),
            ),
            (
                "wrong currency",
                (
                    "Microsoft Cloud gross margin guidance was roughly 70% "
                    "for Q1 FY2025."
                ),
                fact_row(currency="USD"),
            ),
            (
                "near fact path",
                (
                    "Microsoft Cloud gross margin guidance was roughly 70% "
                    "for Q1 FY2025."
                ),
                fact_row(
                    fact_path=(
                        "deterministic_current."
                        "microsoft_cloud_gross_margin_reported.value"
                    )
                ),
            ),
        )
        for label, summary, row in rejected:
            with self.subTest(case=label):
                live_error, report = outcomes(summary, row)
                self.assertIsNotNone(live_error)
                self.assertEqual(
                    live_error.categories,
                    (service.VALIDATION_JSON_SCHEMA,),
                )
                expected_problem = (
                    "numeric_claims[0] (claim_id "
                    f"{row['claim_id']!r}): fact source tuple does not match "
                    "its authored target and deterministic leaf"
                )
                self.assertEqual(live_error.problems, [expected_problem])
                self.assertEqual(
                    live_error.problems_by_category,
                    {service.VALIDATION_JSON_SCHEMA: [expected_problem]},
                )
                self.assertFalse(report.passed)
                self.assertEqual(
                    [failure.code for failure in report.failures],
                    ["numeric_claim_tuple_mismatch"],
                    report.failures,
                )


    def test_signed_currency_tokens_bind_across_live_direct_and_replay(self):
        cases = (
            ("leading sign", "-$0.06", -0.06),
            ("sign after symbol", "$-0.06", -0.06),
            ("positive control", "$0.06", 0.06),
        )

        for label, token, expected_value in cases:
            with self.subTest(case=label):
                deterministic_current = {
                    "earnings_per_share": {
                        "value": expected_value,
                        "unit": "usd_per_share",
                        "currency": "USD",
                        "period": "FY2026",
                    }
                }
                producer = self._producer(
                    deterministic_current=deterministic_current
                )
                row = {
                    "claim_id": "eps-fy2026",
                    "path": "summary",
                    "value": expected_value,
                    "metric": "earnings per share",
                    "period": "FY2026",
                    "unit": "usd_per_share",
                    "currency": "USD",
                    "source_kind": "fact",
                    "fact_path": (
                        "deterministic_current.earnings_per_share.value"
                    ),
                }
                payload = self._payload(
                    f"Earnings per share were {token} in FY2026.",
                    [row],
                )

                parsed = service._validated_investment_facts(
                    json.dumps(payload),
                    excerpt=producer.excerpt,
                    news_items=[
                        service._freeze_json_value(dict(item))
                        for item in producer.news_items
                    ],
                    deterministic_current=service._freeze_json_value(
                        deterministic_current
                    ),
                    deterministic_prior=service._freeze_json_value({}),
                    relationship_facts=service._freeze_json_value({}),
                    material_relationships=(),
                )
                self.assertEqual(parsed["numeric_claims"], [row])

                finalized = finalized_for(
                    payload,
                    deterministic_current=deterministic_current,
                )
                replay_blob = json.loads(
                    json.dumps(
                        {
                            "facts": finalized.facts,
                            "classified_industry": finalized.classified_industry,
                            "previous_facts": finalized.previous_facts,
                            "analysis": finalized.analysis,
                        }
                    )
                )
                replayed = service.InvestmentFinalizedAnalysis(**replay_blob)
                evaluator = self._evaluator(producer)
                direct_report = cq.run_company_hard_gates(
                    producer, evaluator, finalized
                )
                replay_report = cq.run_company_hard_gates(
                    producer, evaluator, replayed
                )

                self.assertEqual(replay_report, direct_report)
                for report in (direct_report, replay_report):
                    self.assertTrue(report.passed, report.failures)
                    self.assertEqual(report.failures, ())
                    self.assertEqual(
                        self._codes(report, "numeric_claim_unbound"),
                        [],
                    )


    def test_counter_thesis_numeric_claim_target_binds_with_replay_parity(self):
        producer = self._producer(
            excerpt="AI demand remained durable. Revenue rose 12 percent in FY2025."
        )
        evaluator = self._evaluator(producer)
        counter_thesis = "The thesis fails if revenue growth drops below 12% in FY2025."
        claim_row = {
            "claim_id": "counter-thesis-revenue-growth",
            "path": "counter_thesis",
            "value": 12,
            "metric": "revenue",
            "period": "FY2025",
            "unit": "percent",
            "currency": None,
            "source_kind": "text",
            "quote": "Revenue rose 12 percent in FY2025.",
        }
        payload = narrative_payload(
            counter_thesis=counter_thesis,
            numeric_claims=[claim_row],
        )
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
        self.assertEqual(parsed["numeric_claims"], [claim_row])
        finalized = finalized_for(payload)
        replay_blob = json.loads(
            json.dumps(
                {
                    "facts": finalized.facts,
                    "classified_industry": finalized.classified_industry,
                    "previous_facts": finalized.previous_facts,
                    "analysis": finalized.analysis,
                }
            )
        )
        replayed = service.InvestmentFinalizedAnalysis(**replay_blob)
        direct_report = cq.run_company_hard_gates(
            producer, evaluator, finalized
        )
        replay_report = cq.run_company_hard_gates(
            producer, evaluator, replayed
        )
        self.assertEqual(replay_report, direct_report)
        for report in (direct_report, replay_report):
            self.assertTrue(report.passed, report.failures)
            self.assertEqual(report.failures, ())
            self.assertEqual(
                self._codes(report, "numeric_claim_unbound"),
                [],
            )


    def test_signed_eps_and_unresolved_source_precedence_match_all_seams(self):
        deterministic_current = {
            "earnings_per_share": {
                "value": -0.06,
                "unit": "usd_per_share",
                "currency": "USD",
                "period": "FY2026",
            }
        }
        producer = self._producer(
            deterministic_current=deterministic_current
        )

        def fact_row(**overrides):
            row = {
                "claim_id": "eps-fy2026",
                "path": "summary",
                "value": -0.06,
                "metric": "earnings per share",
                "period": "FY2026",
                "unit": "usd_per_share",
                "currency": "USD",
                "source_kind": "fact",
                "fact_path": (
                    "deterministic_current.earnings_per_share.value"
                ),
            }
            row.update(overrides)
            return row

        def outcomes(summary, row):
            payload = self._payload(summary, [row])
            try:
                service._validated_investment_facts(
                    json.dumps(payload),
                    excerpt=producer.excerpt,
                    news_items=[
                        service._freeze_json_value(dict(item))
                        for item in producer.news_items
                    ],
                    deterministic_current=service._freeze_json_value(
                        deterministic_current
                    ),
                    deterministic_prior=service._freeze_json_value({}),
                    relationship_facts=service._freeze_json_value({}),
                    material_relationships=(),
                )
            except service.InvestmentValidationError as error:
                live_error = error
            else:
                live_error = None

            finalized = finalized_for(
                payload,
                deterministic_current=deterministic_current,
            )
            replay_blob = json.loads(
                json.dumps(
                    {
                        "facts": finalized.facts,
                        "classified_industry": finalized.classified_industry,
                        "previous_facts": finalized.previous_facts,
                        "analysis": finalized.analysis,
                    }
                )
            )
            replayed = service.InvestmentFinalizedAnalysis(**replay_blob)
            evaluator = self._evaluator(producer)
            direct_report = cq.run_company_hard_gates(
                producer, evaluator, finalized
            )
            replay_report = cq.run_company_hard_gates(
                producer, evaluator, replayed
            )
            self.assertEqual(replay_report, direct_report)
            return live_error, direct_report, replay_report

        live_error, direct_report, replay_report = outcomes(
            "Earnings moved to negative $0.06 per share in FY2026.",
            fact_row(),
        )
        self.assertIsNone(live_error)
        for report in (direct_report, replay_report):
            self.assertTrue(report.passed, report.failures)
            self.assertEqual(report.failures, ())

        for label, summary, row in (
            (
                "changed signed coefficient",
                "Earnings moved to negative $0.07 per share in FY2026.",
                fact_row(value=-0.07),
            ),
            (
                "bare earnings",
                "Earnings moved to negative $0.06 in FY2026.",
                fact_row(),
            ),
            (
                "mixed net earnings",
                "Net earnings moved to negative $0.06 per share in FY2026.",
                fact_row(),
            ),
        ):
            with self.subTest(case=label):
                live_error, direct_report, replay_report = outcomes(summary, row)
                expected_problem = (
                    "numeric_claims[0] (claim_id 'eps-fy2026'): fact source "
                    "tuple does not match its authored target and deterministic "
                    "leaf"
                )
                self.assertIsNotNone(live_error)
                self.assertEqual(
                    live_error.categories,
                    (service.VALIDATION_JSON_SCHEMA,),
                )
                self.assertEqual(live_error.problems, [expected_problem])
                self.assertEqual(
                    live_error.problems_by_category,
                    {service.VALIDATION_JSON_SCHEMA: [expected_problem]},
                )
                for report in (direct_report, replay_report):
                    self.assertFalse(report.passed)
                    self.assertEqual(
                        [failure.code for failure in report.failures],
                        ["numeric_claim_tuple_mismatch"],
                        report.failures,
                    )

        unresolved_row = fact_row(
            claim_id="missing-eps",
            fact_path="deterministic_current.missing_eps.value",
        )
        live_error, direct_report, replay_report = outcomes(
            "Net earnings moved to negative $0.06 per share in FY2026.",
            unresolved_row,
        )
        unresolved_detail = (
            "fact_path 'deterministic_current.missing_eps.value' does not "
            "resolve in deterministic current/prior metrics"
        )
        expected_problem = (
            "numeric_claims[0] (claim_id 'missing-eps'): "
            f"{unresolved_detail}"
        )
        self.assertIsNotNone(live_error)
        self.assertEqual(
            live_error.categories,
            (service.VALIDATION_JSON_SCHEMA,),
        )
        self.assertEqual(live_error.problems, [expected_problem])
        self.assertEqual(
            live_error.problems_by_category,
            {service.VALIDATION_JSON_SCHEMA: [expected_problem]},
        )
        for report in (direct_report, replay_report):
            self.assertFalse(report.passed)
            self.assertEqual(
                [failure.code for failure in report.failures],
                ["numeric_claim_source_unresolved"],
                report.failures,
            )
            self.assertEqual(
                report.failures[0].evidence,
                f"numeric_claims[0] (missing-eps): {unresolved_detail}",
            )


    def test_text_count_horizon_binds_only_the_bare_occurrence(self):
        quote = (
            "Revenue outlook rose 40%. Revenue outlook spans the next 12 "
            "months. Revenue outlook downside is 18%."
        )
        producer = self._producer(excerpt=f"{MSFT_EXCERPT} {quote}")

        def row(value, *, period="next 12 months"):
            return {
                "claim_id": "revenue_outlook_horizon",
                "path": "summary",
                "value": value,
                "metric": "revenue outlook",
                "period": period,
                "unit": "count",
                "currency": None,
                "source_kind": "text",
                "quote": quote,
            }

        report = self._run_with_json_replay(
            producer=producer,
            summary="Revenue outlook spans the next 12 months.",
            rows=[row(12)],
        )
        self.assertTrue(report.passed, report.failures)
        self.assertEqual(report.failures, ())

        for neighboring_percentage in (40, 18):
            with self.subTest(neighboring_percentage=neighboring_percentage):
                report = self._run_with_json_replay(
                    producer=producer,
                    summary=(
                        "Revenue outlook spans the next "
                        f"{neighboring_percentage} months."
                    ),
                    rows=[row(neighboring_percentage)],
                )
                self.assertFalse(report.passed)
                self.assertEqual(
                    [failure.code for failure in report.failures],
                    ["numeric_claim_tuple_mismatch"],
                    report.failures,
                )


    def test_text_horizon_rejects_period_not_rendered_in_source(self):
        quote = (
            "Revenue outlook rose 40%. Revenue outlook spans the next 12 "
            "months. Revenue outlook downside is 18%."
        )
        producer = self._producer(excerpt=f"{MSFT_EXCERPT} {quote}")
        row = {
            "claim_id": "revenue_outlook_horizon",
            "path": "summary",
            "value": 12,
            "metric": "revenue outlook",
            "period": "FY2025 Q1",
            "unit": "count",
            "currency": None,
            "source_kind": "text",
            "quote": quote,
        }
        report = self._run_with_json_replay(
            producer=producer,
            summary="Revenue outlook spans the next 12 months.",
            rows=[row],
        )
        self.assertFalse(report.passed)
        self.assertEqual(
            [failure.code for failure in report.failures],
            ["numeric_claim_tuple_mismatch"],
            report.failures,
        )


    def test_primary_text_periods_dominate_comparison_basis_labels(self):
        cases = (
            (
                "fiscal year",
                "Revenue grew 12% year over year in FY2026.",
                "Revenue grew 12% in FY2026.",
                "FY2026",
            ),
            (
                "prior quarter",
                "Revenue grew 12% year over year in the prior quarter.",
                "Revenue grew 12% in the prior quarter.",
                "prior quarter",
            ),
        )
        for label, quote, summary, period in cases:
            with self.subTest(label=label):
                producer = self._producer(
                    excerpt=f"{MSFT_EXCERPT} {quote}"
                )
                row = {
                    "claim_id": f"revenue-growth-{label.replace(' ', '-')}",
                    "path": "summary",
                    "value": "12%",
                    "metric": "revenue growth",
                    "period": period,
                    "unit": "percent",
                    "currency": None,
                    "source_kind": "text",
                    "quote": quote,
                }
                report = self._run_with_json_replay(
                    producer=producer,
                    summary=summary,
                    rows=[row],
                )
                self.assertTrue(report.passed, report.failures)
                self.assertEqual(report.failures, ())


    def test_forward_horizon_equal_coefficients_bind_by_unit(self):
        quote = (
            "Revenue will grow 12% year over year during the next 12 months."
        )
        summary = "Revenue will grow 12% during the next 12 months."
        producer = self._producer(excerpt=f"{MSFT_EXCERPT} {quote}")

        def row(claim_id, value, unit, *, metric="revenue growth",
                source_quote=quote):
            return {
                "claim_id": claim_id,
                "path": "summary",
                "value": value,
                "metric": metric,
                "period": "next 12 months",
                "unit": unit,
                "currency": None,
                "source_kind": "text",
                "quote": source_quote,
            }

        percentage_row = row("revenue-growth-forward", "12%", "percent")
        horizon_row = row("revenue-growth-horizon", 12, "count")

        report = self._run_with_json_replay(
            producer=producer,
            summary=summary,
            rows=[percentage_row, horizon_row],
        )
        self.assertTrue(report.passed, report.failures)
        self.assertEqual(report.failures, ())

        omitted = self._run_with_json_replay(
            producer=producer,
            summary=summary,
            rows=[percentage_row],
        )
        self.assertFalse(omitted.passed)
        self.assertEqual(
            [failure.code for failure in omitted.failures],
            ["numeric_claim_unbound"],
            omitted.failures,
        )
        self.assertEqual(
            [(failure.path, str(failure.observed))
             for failure in omitted.failures],
            [("$.summary", "12")],
            omitted.failures,
        )
        self.assertIn("next 12 months", omitted.failures[0].evidence)

        rejected_rows = (
            (
                "wrong unit",
                producer,
                [
                    percentage_row,
                    row(
                        "revenue-growth-horizon-wrong-unit",
                        12,
                        "percentage_points",
                    ),
                ],
                1,
            ),
            (
                "cross metric",
                producer,
                [
                    percentage_row,
                    row(
                        "operating-margin-horizon",
                        12,
                        "count",
                        metric="operating margin growth",
                    ),
                ],
                1,
            ),
            (
                "source percent differs from bare horizon coefficient",
                self._producer(
                    excerpt=(
                        f"{MSFT_EXCERPT} Revenue will grow 13% year over "
                        "year during the next 12 months."
                    )
                ),
                [
                    row(
                        "revenue-growth-source-mismatch",
                        "12%",
                        "percent",
                        source_quote=(
                            "Revenue will grow 13% year over year during "
                            "the next 12 months."
                        ),
                    ),
                    row(
                        "revenue-growth-horizon-source-control",
                        12,
                        "count",
                        source_quote=(
                            "Revenue will grow 13% year over year during "
                            "the next 12 months."
                        ),
                    ),
                ],
                0,
            ),
        )
        for label, rejected_producer, rows, bad_index in rejected_rows:
            with self.subTest(case=label):
                rejected = self._run_with_json_replay(
                    producer=rejected_producer,
                    summary=summary,
                    rows=rows,
                )
                self.assertFalse(rejected.passed)
                self.assertEqual(
                    [failure.code for failure in rejected.failures],
                    ["numeric_claim_tuple_mismatch"],
                    rejected.failures,
                )
                self.assertEqual(
                    [
                        (failure.path, failure.observed)
                        for failure in rejected.failures
                    ],
                    [
                        (
                            f"numeric_claims[{bad_index}]",
                            {"claim_id": rows[bad_index]["claim_id"]},
                        )
                    ],
                    rejected.failures,
                )

        duplicate_row = dict(horizon_row)
        duplicate_row["claim_id"] = "revenue-growth-horizon-duplicate"
        duplicate = self._run_with_json_replay(
            producer=producer,
            summary=summary,
            rows=[percentage_row, horizon_row, duplicate_row],
        )
        self.assertFalse(duplicate.passed)
        self.assertEqual(
            [failure.code for failure in duplicate.failures],
            ["numeric_claim_duplicate"],
            duplicate.failures,
        )
        self.assertEqual(
            [
                (failure.path, failure.observed)
                for failure in duplicate.failures
            ],
            [
                (
                    "numeric_claims[2]",
                    {"claim_id": "revenue-growth-horizon-duplicate"},
                )
            ],
            duplicate.failures,
        )


    def test_comparison_basis_binds_when_it_is_the_only_period_evidence(self):
        quote = "Revenue grew 12% year over year."
        producer = self._producer(excerpt=f"{MSFT_EXCERPT} {quote}")
        row = {
            "claim_id": "revenue-growth-yoy",
            "path": "summary",
            "value": "12%",
            "metric": "revenue growth",
            "period": "year over year",
            "unit": "percent",
            "currency": None,
            "source_kind": "text",
            "quote": quote,
        }
        report = self._run_with_json_replay(
            producer=producer,
            summary="Revenue grew 12% year over year.",
            rows=[row],
        )
        self.assertTrue(report.passed, report.failures)
        self.assertEqual(report.failures, ())


    def test_comparison_basis_does_not_hide_primary_period_conflicts(self):
        cases = (
            (
                "wrong fiscal year",
                "Revenue grew 12% year over year in FY2026.",
                "Revenue grew 12% in FY2025.",
                "FY2025",
            ),
            (
                "multiple primary periods",
                (
                    "Revenue grew 12% in FY2026 versus FY2025, year over "
                    "year."
                ),
                "Revenue grew 12% in FY2026.",
                "FY2026",
            ),
        )
        for label, quote, summary, period in cases:
            with self.subTest(label=label):
                producer = self._producer(
                    excerpt=f"{MSFT_EXCERPT} {quote}"
                )
                row = {
                    "claim_id": f"revenue-growth-{label.replace(' ', '-')}",
                    "path": "summary",
                    "value": "12%",
                    "metric": "revenue growth",
                    "period": period,
                    "unit": "percent",
                    "currency": None,
                    "source_kind": "text",
                    "quote": quote,
                }
                report = self._run_with_json_replay(
                    producer=producer,
                    summary=summary,
                    rows=[row],
                )
                self.assertFalse(report.passed)
                self.assertEqual(
                    [failure.code for failure in report.failures],
                    ["numeric_claim_tuple_mismatch"],
                    report.failures,
                )


    def test_period_silent_rpo_binds_bounded_document_title_aliases(self):
        quote = "Remaining performance obligation was $269 billion"
        aliases = (
            "FY2024-Q4",
            "FY24 Q4",
            "Q4 FY24",
            "FY24 Fourth Quarter",
            "Fiscal Year 2024 Fourth Quarter",
        )
        for alias in aliases:
            with self.subTest(alias=alias):
                producer = self._producer(
                    document=self._microsoft_document(
                        title=(
                            f"Microsoft {alias} Earnings Conference Call"
                        )
                    ),
                    excerpt=f"{MSFT_EXCERPT} {quote}.",
                )
                report = self._run_with_json_replay(
                    producer=producer,
                    summary=(
                        "Remaining performance obligation was $269 billion "
                        "in FY2024-Q4."
                    ),
                    rows=[self._rpo_row(quote=quote)],
                )
                self.assertTrue(report.passed, report.failures)
                self.assertEqual(report.failures, ())


    def test_period_silent_rpo_binds_matched_news_title_and_date(self):
        quote = "Remaining performance obligation was $269 billion"
        producer = self._producer(
            document=self._microsoft_document(),
            excerpt=MSFT_EXCERPT,
            news_items=[
                {
                    "title": (
                        "Microsoft FY24 Fourth Quarter Earnings "
                        "Conference Call"
                    ),
                    "summary": quote,
                    "report_date": "2024-06-30",
                    "published_at": "2024-07-30T20:00:00Z",
                    "available_at": "2024-07-30T21:00:00Z",
                }
            ],
        )
        report = self._run_with_json_replay(
            producer=producer,
            summary=(
                "Remaining performance obligation was $269 billion "
                "in FY2024-Q4."
            ),
            rows=[self._rpo_row(quote=quote)],
        )
        self.assertTrue(report.passed, report.failures)
        self.assertEqual(report.failures, ())


    def test_period_silent_rpo_rejects_unsafe_document_period_override(self):
        silent_quote = "Remaining performance obligation was $269 billion"
        matching_title = (
            "Microsoft FY24 Fourth Quarter Earnings Conference Call"
        )
        scenarios = (
            (
                "wrong claimed period",
                matching_title,
                silent_quote,
                "FY2025-Q1",
            ),
            (
                "date alone cannot infer a fiscal quarter",
                None,
                silent_quote,
                "FY2024-Q4",
            ),
            (
                "explicit quote conflict",
                matching_title,
                (
                    "Remaining performance obligation was $269 billion "
                    "in FY2025-Q1"
                ),
                "FY2024-Q4",
            ),
            (
                "forward period overrides title",
                matching_title,
                (
                    "Remaining performance obligation for the next 12 "
                    "months was $269 billion"
                ),
                "FY2024-Q4",
            ),
            (
                "prior period overrides title",
                matching_title,
                (
                    "Remaining performance obligation in the prior quarter "
                    "was $269 billion"
                ),
                "FY2024-Q4",
            ),
            (
                "ambiguous multi-period title",
                (
                    "Microsoft FY24 Fourth Quarter and FY2025-Q1 "
                    "Earnings Conference Call"
                ),
                silent_quote,
                "FY2024-Q4",
            ),
        )
        for label, title, quote, period in scenarios:
            with self.subTest(label=label):
                producer = self._producer(
                    document=self._microsoft_document(title=title),
                    excerpt=f"{MSFT_EXCERPT} {quote}.",
                )
                report = self._run_with_json_replay(
                    producer=producer,
                    summary=(
                        "Remaining performance obligation was $269 billion "
                        f"in {period}."
                    ),
                    rows=[self._rpo_row(quote=quote, period=period)],
                )
                self.assertFalse(report.passed)
                self.assertEqual(
                    [failure.code for failure in report.failures],
                    ["numeric_claim_tuple_mismatch"],
                    report.failures,
                )


    def test_period_silent_rpo_never_borrows_other_source_period_metadata(self):
        quote = "Remaining performance obligation was $269 billion"
        matching_title = (
            "Microsoft FY24 Fourth Quarter Earnings Conference Call"
        )

        def news(title, summary):
            return {
                "title": title,
                "summary": summary,
                "published_at": "2024-07-30T20:00:00Z",
                "available_at": "2024-07-30T21:00:00Z",
            }

        release_document = self._microsoft_document()
        release_document.update(
            {
                "release_title": matching_title,
                "source_url": "https://example.test/microsoft/fy24-q4",
                "release_source_url": (
                    "https://example.test/releases/fy24-q4"
                ),
                "release_date": "2024-06-30",
            }
        )
        release_news = news("Microsoft earnings call", quote)
        release_news.update(
            {
                "release_title": matching_title,
                "source_url": "https://example.test/microsoft/fy24-q4",
                "release_date": "2024-06-30",
            }
        )

        scenarios = (
            (
                "document quote cannot borrow news title",
                self._microsoft_document(),
                f"{MSFT_EXCERPT} {quote}.",
                [news(matching_title, "Management discussed the backlog.")],
            ),
            (
                "news quote cannot borrow document title",
                self._microsoft_document(title=matching_title),
                MSFT_EXCERPT,
                [news("Microsoft earnings call", quote)],
            ),
            (
                "matched news cannot borrow neighboring news title",
                self._microsoft_document(),
                MSFT_EXCERPT,
                [
                    news("Microsoft earnings call", quote),
                    news(matching_title, "Management discussed the backlog."),
                ],
            ),
            (
                "document quote cannot use release or URL hints",
                release_document,
                f"{MSFT_EXCERPT} {quote}.",
                [news("Microsoft earnings call", "Backlog remained durable.")],
            ),
            (
                "news quote cannot use release or URL hints",
                self._microsoft_document(),
                MSFT_EXCERPT,
                [release_news],
            ),
        )
        for label, document, excerpt, news_items in scenarios:
            with self.subTest(label=label):
                producer = self._producer(
                    document=document,
                    excerpt=excerpt,
                    news_items=news_items,
                )
                report = self._run_with_json_replay(
                    producer=producer,
                    summary=(
                        "Remaining performance obligation was $269 billion "
                        "in FY2024-Q4."
                    ),
                    rows=[self._rpo_row(quote=quote)],
                )
                self.assertFalse(report.passed)
                self.assertEqual(
                    [failure.code for failure in report.failures],
                    ["numeric_claim_tuple_mismatch"],
                    report.failures,
                )


    def test_period_silent_rpo_requires_one_unambiguous_source(self):
        quote = "Remaining performance obligation was $269 billion"
        title = "Microsoft FY24 Fourth Quarter Earnings Conference Call"
        producer = self._producer(
            document=self._microsoft_document(title=title),
            excerpt=f"{MSFT_EXCERPT} {quote}.",
            news_items=[
                {
                    "title": title,
                    "summary": quote,
                    "published_at": "2024-07-30T20:00:00Z",
                    "available_at": "2024-07-30T21:00:00Z",
                }
            ],
        )
        report = self._run_with_json_replay(
            producer=producer,
            summary=(
                "Remaining performance obligation was $269 billion "
                "in FY2024-Q4."
            ),
            rows=[self._rpo_row(quote=quote)],
        )
        self.assertFalse(report.passed)
        self.assertEqual(
            [failure.code for failure in report.failures],
            ["numeric_claim_source_unresolved"],
            report.failures,
        )


    def test_fiscal_quarter_alias_matching_is_bounded(self):
        for context in (
            "Microsoft FY2024-Q4 earnings",
            "Microsoft FY24 Q4 earnings",
            "Microsoft Q4 FY24 earnings",
            "Microsoft FY24 Fourth Quarter earnings",
            "Microsoft fiscal year 2024 fourth quarter earnings",
        ):
            with self.subTest(context=context):
                self.assertTrue(
                    service._period_alias_matches("FY2024-Q4", context)
                )

        for context in (
            "Microsoft XFY24 Q4 earnings",
            "Microsoft FY24 Q40 earnings",
            "Microsoft FY24 Fourth Quarterly earnings",
            "Microsoft Q4FY24 earnings",
        ):
            with self.subTest(context=context):
                self.assertFalse(
                    service._period_alias_matches("FY2024-Q4", context)
                )


