"""Numeric claim binding tests: live, direct, and JSON replay parity matrices."""

import copy
import json
import sys
from dataclasses import replace as dataclass_replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from company_quality_support import (
    MSFT_EXCERPT,
    NEWS_ITEM,
    NumericClaimBindingTestBase,
    finalized_for,
)

import investment_service as service
from research_intelligence import company_quality as cq


class NumericClaimReplayGateTests(NumericClaimBindingTestBase):
    """Tests for live, direct, and JSON replay parity matrices."""

    def test_target_period_and_unit_contract_live_direct_replay_matrix(self):
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
            "Azure and other cloud services revenue growth guidance was 28% "
            "for Q1 FY2025, and Azure and other cloud services revenue growth "
            "guidance was 29% for Q1 FY2025."
        )
        range_rows = [
            row(
                "azure-revenue-guide-low",
                "28%",
                "Azure and other cloud services revenue growth guidance",
                "FY2025-Q1",
                "percent",
                range_quote,
            ),
            row(
                "azure-revenue-guide-high",
                "29%",
                "Azure and other cloud services revenue growth guidance",
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

        def outcomes(summary, rows):
            payload = self._payload(summary, [dict(item) for item in rows])
            excerpt = f"{MSFT_EXCERPT} " + " ".join(
                dict.fromkeys(item["quote"] for item in rows)
            )
            producer = self._producer(excerpt=excerpt)
            try:
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
            except service.InvestmentValidationError as error:
                parsed = None
                live_error = error
            else:
                live_error = None
            report = self._run_payload_with_json_replay(
                payload,
                producer=producer,
            )
            self.assertEqual(live_error is None, report.passed, report.failures)
            return parsed, live_error, report

        for label, summary, rows in accepted:
            with self.subTest(case=label):
                parsed, live_error, report = outcomes(summary, rows)
                self.assertIsNone(live_error)
                self.assertEqual(parsed["numeric_claims"], rows)
                self.assertTrue(report.passed, report.failures)
                self.assertEqual(report.failures, ())

        for label, summary, source_row in rejected:
            with self.subTest(case=label):
                rejected_row = dict(source_row)
                parsed, live_error, report = outcomes(
                    summary, [rejected_row]
                )
                self.assertIsNone(parsed)
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
                    live_error.categories,
                    (service.VALIDATION_JSON_SCHEMA,),
                )
                self.assertEqual(live_error.problems, [expected_problem])
                self.assertEqual(
                    live_error.problems_by_category,
                    {service.VALIDATION_JSON_SCHEMA: [expected_problem]},
                )
                self.assertFalse(report.passed)
                self.assertEqual(
                    [
                        (failure.code, failure.path, failure.observed)
                        for failure in report.failures
                    ],
                    [
                        (
                            "numeric_claim_tuple_mismatch",
                            "numeric_claims[0]",
                            {"claim_id": rejected_row["claim_id"]},
                        )
                    ],
                    report.failures,
                )


    def test_arithmetic_tuple_live_direct_json_replay_parity(self):
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
        producer = self._producer(
            deterministic_current=deterministic_current
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
            report = self._run_payload_with_json_replay(
                payload,
                producer=producer,
                deterministic_current=deterministic_current,
            )
            self.assertEqual(live_error is None, report.passed, report.failures)
            return live_error, report

        live_error, report = outcomes(
            "Free cash flow was $18.2 billion in FY2024 Q4.",
            dict(base_row),
        )
        self.assertIsNone(live_error)
        self.assertTrue(report.passed, report.failures)
        self.assertEqual(report.failures, ())

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
        expected_problem = (
            "numeric_claims[0] (claim_id 'derived-free-cash-flow'): "
            "arithmetic source tuple does not match its authored target and "
            "producer-declared output"
        )
        for label, summary, row in cases:
            with self.subTest(case=label):
                live_error, report = outcomes(summary, row)
                self.assertEqual(
                    live_error.categories,
                    (service.VALIDATION_JSON_SCHEMA,),
                )
                self.assertEqual(live_error.problems, [expected_problem])
                self.assertEqual(
                    live_error.problems_by_category,
                    {service.VALIDATION_JSON_SCHEMA: [expected_problem]},
                )
                self.assertFalse(report.passed)
                self.assertEqual(
                    [
                        (failure.code, failure.path, failure.observed)
                        for failure in report.failures
                    ],
                    [
                        (
                            "numeric_claim_tuple_mismatch",
                            "numeric_claims[0]",
                            {"claim_id": "derived-free-cash-flow"},
                        )
                    ],
                    report.failures,
                )


    def test_metric_inheritance_edges_live_direct_json_replay_parity(self):
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

        accepted = (
            (
                "comma up inherits primary period",
                (
                    "Free cash flow was $23.3 billion in FY2024 Q4, up 18% "
                    "year over year."
                ),
                row(
                    "fcf-growth-comma",
                    "18%",
                    "free cash flow",
                    "FY2024-Q4",
                    "percent",
                    "",
                ),
            ),
            (
                "comparison-qualified forward horizon",
                (
                    "Revenue will grow 12% year over year during the next "
                    "12 months."
                ),
                row(
                    "revenue-comparison-horizon",
                    12,
                    "revenue growth",
                    "next 12 months",
                    "count",
                    "",
                ),
            ),
            (
                "contribution period interposition",
                (
                    "Azure growth included 8 points in FY2024 Q4 from AI "
                    "services."
                ),
                row(
                    "azure-ai-period-interposition",
                    8,
                    "Azure growth contribution from AI services",
                    "FY2024-Q4",
                    "percentage_points",
                    "",
                ),
            ),
        )
        rejected = (
            (
                "ordinary conjunction",
                (
                    "Free cash flow was $23.3 billion in FY2024 Q4 and "
                    "increased 18% year over year."
                ),
                accepted[0][2],
            ),
            (
                "semicolon boundary",
                (
                    "Free cash flow was $23.3 billion in FY2024 Q4; "
                    "up 18% year over year."
                ),
                accepted[0][2],
            ),
            (
                "swapped contribution",
                (
                    "AI services grew 8 points in FY2024 Q4 from Azure "
                    "growth."
                ),
                accepted[2][2],
            ),
            (
                "contribution sentence boundary",
                (
                    "Azure growth included 8 points in FY2024 Q4. "
                    "From AI services."
                ),
                accepted[2][2],
            ),
        )

        def outcomes(summary, source_row):
            bound_row = dict(source_row, quote=summary)
            rows = [bound_row]
            if bound_row["claim_id"] == "fcf-growth-comma":
                rows.append(
                    {
                        **bound_row,
                        "claim_id": "fcf-amount-comma-context",
                        "value": "$23.3 billion",
                        "unit": "usd_billions",
                        "currency": "USD",
                    }
                )
            elif bound_row["claim_id"] == "revenue-comparison-horizon":
                rows.append(
                    {
                        **bound_row,
                        "claim_id": "revenue-comparison-rate",
                        "value": "12%",
                        "unit": "percent",
                    }
                )
            payload = self._payload(summary, rows)
            producer = self._producer(excerpt=f"{MSFT_EXCERPT} {summary}")
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
            self.assertEqual(live_error is None, report.passed, report.failures)
            return bound_row, live_error, report

        for label, summary, source_row in accepted:
            with self.subTest(case=label):
                _, live_error, report = outcomes(summary, source_row)
                self.assertIsNone(live_error)
                self.assertTrue(report.passed, report.failures)
                self.assertEqual(report.failures, ())

        for label, summary, source_row in rejected:
            with self.subTest(case=label):
                bound_row, live_error, report = outcomes(summary, source_row)
                expected_problem = (
                    "numeric_claims[0] (claim_id "
                    f"{bound_row['claim_id']!r}): text source tuple does not "
                    "match its authored target and bound producer quote: metric "
                    f"{bound_row['metric']!r}, period "
                    f"{bound_row['period']!r}, unit {bound_row['unit']!r}, "
                    f"and currency {bound_row['currency']!r} do not match the "
                    "authored target around the claimed numeral"
                )
                self.assertEqual(
                    live_error.categories,
                    (service.VALIDATION_JSON_SCHEMA,),
                )
                self.assertEqual(live_error.problems, [expected_problem])
                self.assertEqual(
                    live_error.problems_by_category,
                    {service.VALIDATION_JSON_SCHEMA: [expected_problem]},
                )
                self.assertEqual(
                    [
                        (failure.code, failure.path, failure.observed)
                        for failure in report.failures
                    ],
                    [
                        (
                            "numeric_claim_tuple_mismatch",
                            "numeric_claims[0]",
                            {"claim_id": bound_row["claim_id"]},
                        )
                    ],
                    report.failures,
                )


    def test_text_source_tuple_live_direct_json_replay_parity(self):
        def text_row(claim_id, value, metric, period, unit, quote, currency=None):
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

        silent_quote = "Remaining performance obligation was $269 billion."
        compound_quote = (
            "Microsoft Cloud gross margin guidance was 70% for FY2025-Q1 "
            "guidance issued 2024-07-30."
        )
        accepted = (
            (
                "period-silent same-document metadata",
                "Remaining performance obligation was $269 billion in "
                "FY2024 Q4.",
                text_row(
                    "commercial-rpo-document-period",
                    "$269 billion",
                    "remaining performance obligation",
                    "FY2024-Q4",
                    "usd_billions",
                    silent_quote,
                    "USD",
                ),
                f"{MSFT_EXCERPT} {silent_quote}",
                [],
            ),
            (
                "compound FYQ and issue date",
                (
                    "Microsoft Cloud gross margin guidance was 70% for "
                    "Q1 FY2025."
                ),
                text_row(
                    "cloud-margin-compound-period",
                    "70%",
                    "Microsoft Cloud gross margin guidance",
                    "FY2025-Q1 guidance issued 2024-07-30",
                    "percent",
                    compound_quote,
                ),
                f"{MSFT_EXCERPT} {compound_quote}",
                [],
            ),
        )
        unrelated_quote = (
            "Revenue was $19 billion in FY2024 Q4."
        )
        ambiguous_quote = "Revenue growth was 12%."
        rejected = (
            (
                "unrelated verbatim quote",
                (
                    "Capital expenditures including finance leases were "
                    "$19 billion in FY2024 Q4."
                ),
                text_row(
                    "unrelated-verbatim-source",
                    "$19 billion",
                    "capital expenditures including finance leases",
                    "FY2024-Q4",
                    "usd_billions",
                    unrelated_quote,
                    "USD",
                ),
                f"{MSFT_EXCERPT} {unrelated_quote}",
                [],
            ),
            (
                "wrong explicit source period",
                (
                    "Capital expenditures including finance leases were "
                    "$19 billion in FY2024 Q4."
                ),
                text_row(
                    "wrong-explicit-source-period",
                    "$19 billion",
                    "capital expenditures including finance leases",
                    "FY2024-Q4",
                    "usd_billions",
                    (
                        "Capital expenditures including finance leases were "
                        "$19 billion in FY2023 Q4."
                    ),
                    "USD",
                ),
                (
                    f"{MSFT_EXCERPT} Capital expenditures including finance "
                    "leases were $19 billion in FY2023 Q4."
                ),
                [],
            ),
            (
                "cross-source occurrence ambiguity",
                "Revenue growth was 12% in FY2025 Q1.",
                text_row(
                    "cross-source-ambiguous-quote",
                    "12%",
                    "revenue growth",
                    "FY2025-Q1",
                    "percent",
                    ambiguous_quote,
                ),
                (
                    f"{MSFT_EXCERPT} In FY2024 Q4, {ambiguous_quote}"
                ),
                [
                    {
                        **NEWS_ITEM,
                        "summary": f"In FY2025 Q1, {ambiguous_quote}",
                    }
                ],
            ),
        )

        def outcomes(summary, row, excerpt, news_items):
            document = self._microsoft_document(
                title="Microsoft FY2024 Q4 results"
            )
            producer = self._producer(
                excerpt=excerpt,
                document=document,
                news_items=news_items,
            )
            payload = self._payload(summary, [row])
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
                    document_metadata=service._freeze_json_value(
                        dict(producer.document)
                    ),
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
            self.assertEqual(live_error is None, report.passed, report.failures)
            return live_error, report

        for label, summary, row, excerpt, news_items in accepted:
            with self.subTest(case=label):
                live_error, report = outcomes(
                    summary, row, excerpt, news_items
                )
                self.assertIsNone(live_error)
                self.assertTrue(report.passed, report.failures)
                self.assertEqual(report.failures, ())

        for label, summary, row, excerpt, news_items in rejected:
            with self.subTest(case=label):
                live_error, report = outcomes(
                    summary, row, excerpt, news_items
                )
                self.assertEqual(
                    live_error.categories,
                    (service.VALIDATION_JSON_SCHEMA,),
                )
                self.assertFalse(report.passed)
                expected_code = (
                    "numeric_claim_source_unresolved"
                    if row["claim_id"] == "cross-source-ambiguous-quote"
                    else "numeric_claim_tuple_mismatch"
                )
                self.assertEqual(
                    [
                        (failure.code, failure.path, failure.observed)
                        for failure in report.failures
                    ],
                    [
                        (
                            expected_code,
                            "numeric_claims[0]",
                            {"claim_id": row["claim_id"]},
                        )
                    ],
                    report.failures,
                )


    def test_reused_fact_rows_and_summary_binding_have_live_replay_parity(self):
        deterministic_current = {
            "revenue_growth": {
                "value": 8.0,
                "unit": "percent",
                "period": "FY2025",
                "evidence": ["demand remained durable"],
                "source": "reported",
                "relationship_tags": {
                    "role": "top_line",
                    "metric_family": "revenue",
                    "leaf": "growth",
                    "scope": "consolidated",
                    "comparison_basis": "year_over_year_gaap",
                    "temporal_basis": "rate_over_period",
                    "cash_basis": "not_applicable",
                },
            },
            "net_income_growth": {
                "value": 5.0,
                "unit": "percent",
                "period": "FY2025",
                "evidence": ["demand remained durable"],
                "source": "reported",
                "relationship_tags": {
                    "role": "bottom_line",
                    "metric_family": "net_income",
                    "leaf": "growth",
                    "scope": "consolidated",
                    "comparison_basis": "year_over_year_gaap",
                    "temporal_basis": "rate_over_period",
                    "cash_basis": "not_applicable",
                },
            },
        }
        producer = self._producer(deterministic_current=deterministic_current)
        evaluator = self._evaluator(producer)
        base_contract = cq.build_material_relationship_contract(
            producer.deterministic_current,
            producer.deterministic_prior,
        )
        source_relationship = base_contract.material_relationships[0]
        shared_ref = source_relationship.required_facts[0]
        relationships = tuple(
            dataclass_replace(
                source_relationship,
                relationship_id=f"reused-target-{index}",
                required_facts=(shared_ref,),
            )
            for index in range(2)
        )
        contract = dataclass_replace(
            base_contract,
            material_relationships=relationships,
        )
        contract_payload = contract.to_payload()
        fact_path = shared_ref.fact_path
        fact = contract_payload["relationship_facts"][
            fact_path.rsplit(".", 1)[-1]
        ]
        observation = (
            f"{fact['metric_label'].replace('_', ' ')} was {fact['value']}% "
            f"year over year in {fact['period']}."
        )
        summary_synthesis = (
            f"Revenue growth was {fact['value']}% in {fact['period']} and "
            "informs both relationships."
        )
        thesis_synthesis = "The shared growth fact supports both conclusions."
        interpretation = "The shared fact informs both relationships."
        uncertainty = "The shared relationship may change next period."
        payload = self._payload(summary_synthesis, [])
        payload["thesis"] = thesis_synthesis
        payload["relationship_reconciliations"] = [
            {
                "relationship_id": relationship.relationship_id,
                "status": "reconciled",
                "fact_paths": [fact_path],
                "observation": observation,
                "interpretation": interpretation,
                "uncertainty": uncertainty,
                "summary_synthesis": summary_synthesis,
                "thesis_synthesis": thesis_synthesis,
                "summary_fact_paths": [fact_path],
            }
            for relationship in relationships
        ]

        def row(claim_id, target):
            return {
                "claim_id": claim_id,
                "path": target,
                "value": fact["value"],
                "metric": fact["metric_label"],
                "period": fact["period"],
                "unit": fact["unit"],
                "currency": fact["currency"],
                "source_kind": "fact",
                "fact_path": fact_path,
            }

        observation_rows = [
            row(
                f"observation-{index}",
                f"relationship_reconciliations[{index}].observation",
            )
            for index in range(2)
        ]
        summary_row = row("summary-shared-binding", "summary")
        payload["numeric_claims"] = [*observation_rows, summary_row]

        # Source identity, period, and currency are all part of semantic row
        # identity. None may collapse merely because target/value/metric/unit
        # happen to match.
        identity_controls = [
            summary_row,
            {**summary_row, "fact_path": f"{fact_path}-other"},
            {**summary_row, "period": "FY2024"},
            {**summary_row, "currency": "CAD"},
        ]
        identity_keys = [
            service._numeric_claim_semantic_binding_key(control)
            for control in identity_controls
        ]
        self.assertNotIn(None, identity_keys)
        self.assertEqual(len(set(identity_keys)), len(identity_keys))

        def live_error(rows):
            candidate = copy.deepcopy(payload)
            candidate["numeric_claims"] = copy.deepcopy(rows)
            try:
                service._validated_investment_facts(
                    json.dumps(candidate),
                    excerpt=producer.excerpt,
                    news_items=[
                        service._freeze_json_value(dict(item))
                        for item in producer.news_items
                    ],
                    deterministic_current=producer.deterministic_current,
                    deterministic_prior=producer.deterministic_prior,
                    relationship_facts=service._freeze_json_value(
                        contract_payload["relationship_facts"]
                    ),
                    material_relationships=service._freeze_json_value(
                        contract_payload["material_relationships"]
                    ),
                )
            except service.InvestmentValidationError as error:
                return error
            return None

        def reports(rows):
            candidate = copy.deepcopy(payload)
            candidate["numeric_claims"] = copy.deepcopy(rows)
            finalized = finalized_for(
                candidate,
                deterministic_current=deterministic_current,
                relationship_facts=contract_payload["relationship_facts"],
                material_relationships=contract_payload[
                    "material_relationships"
                ],
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
            with patch.object(
                cq,
                "build_material_relationship_contract",
                return_value=contract,
            ):
                direct = cq.run_company_hard_gates(
                    producer,
                    evaluator,
                    finalized,
                )
                replay = cq.run_company_hard_gates(
                    producer,
                    evaluator,
                    service.InvestmentFinalizedAnalysis(**replay_blob),
                )
            self.assertEqual(replay, direct)
            return direct, replay

        self.assertEqual(payload["summary"].count(summary_synthesis), 1)
        self.assertNotIn(observation, payload["summary"])
        self.assertNotIn(uncertainty, payload["summary"])
        self.assertNotIn(interpretation, payload["thesis"])
        self.assertEqual(
            {
                row["summary_synthesis"]
                for row in payload["relationship_reconciliations"]
            },
            {summary_synthesis},
        )

        accepted_rows = [*observation_rows, summary_row]
        self.assertIsNone(live_error(accepted_rows))
        for report in reports(accepted_rows):
            self.assertTrue(report.passed, report.failures)
            self.assertEqual(report.failures, ())

        for label, changed_summary_rows in (
            ("missing", []),
            ("wrong value", [{**summary_row, "value": fact["value"] + 1}]),
        ):
            with self.subTest(summary_binding=label):
                rejected = [*observation_rows, *changed_summary_rows]
                error = live_error(rejected)
                self.assertIsNotNone(error)
                self.assertTrue(
                    any(
                        "selected relationship fact" in problem
                        for problem in error.problems
                    ),
                    error.problems,
                )
                for report in reports(rejected):
                    self.assertFalse(report.passed)
                    self.assertTrue(
                        any(
                            failure.path == "$.summary"
                            and "selected relationship fact"
                            in str(failure.observed)
                            for failure in report.failures
                        ),
                        report.failures,
                    )

        duplicate_summary = {
            **summary_row,
            "claim_id": "summary-shared-binding-duplicate",
        }
        rejected_rows = [*accepted_rows, duplicate_summary]
        error = live_error(rejected_rows)
        self.assertIsNotNone(error)
        self.assertEqual(
            error.categories,
            (service.VALIDATION_JSON_SCHEMA,),
        )
        self.assertEqual(
            [
                problem
                for problem in error.problems
                if "duplicate semantic numeric binding" in problem
            ],
            [
                "numeric_claims[3] (claim_id "
                "'summary-shared-binding-duplicate'): duplicate semantic "
                "numeric binding already carried by numeric_claims[2]"
            ],
        )
        for report in reports(rejected_rows):
            self.assertFalse(report.passed)
            self.assertEqual(
                [
                    failure.code
                    for failure in report.failures
                    if failure.code == "numeric_claim_duplicate"
                ],
                ["numeric_claim_duplicate"],
                report.failures,
            )


