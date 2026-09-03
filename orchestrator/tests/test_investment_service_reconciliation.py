"""Tests for investment service."""

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from investment_service_support import (
    investment_report_payload,
    relationship_metric,
)

import investment_service as service


class RelationshipReconciliationContractTests(unittest.TestCase):
    def _contract(self):
        return [
            {
                "relationship_id": "rel-compatible",
                "compatibility": "compatible",
                "required_facts": [
                    {
                        "fact_path": (
                            "deterministic_current.relationship_facts.fact-a"
                        )
                    },
                    {
                        "fact_path": (
                            "deterministic_current.relationship_facts.fact-b"
                        )
                    },
                ],
            },
            {
                "relationship_id": "rel-incompatible",
                "compatibility": "incompatible",
                "required_facts": [
                    {
                        "fact_path": (
                            "deterministic_current.relationship_facts.fact-c"
                        )
                    }
                ],
            },
        ]

    def test_v7_prompt_requires_concise_synthesis_audit_and_deduped_summary_rows(
        self,
    ):
        request = service.build_investment_analysis_request(
            {"company": "Example Company", "document_id": "doc-1"},
            "Demand remained durable.",
            [],
            {},
            {},
        )
        self.assertIn(
            "keep the complete audit rendering in nonblank observation, "
            "interpretation, and uncertainty",
            request.prompt,
        )
        self.assertIn(
            "copy those exact synthesis strings contiguously into summary "
            "and thesis",
            request.prompt,
        )
        self.assertIn(
            "Do not copy the full observation, interpretation, or uncertainty",
            request.prompt,
        )
        self.assertIn(
            "shared selected facts and shared summary segments are written once",
            request.prompt,
        )
        self.assertIn(
            "exactly one deduplicated fact row targeting `summary`",
            request.prompt,
        )


    def test_professional_atomic_relationship_surfaces_bind_each_target(self):
        period = "FY2025"
        facts = {
            "revenue-growth": {
                "value": 12,
                "unit": "percent",
                "currency": None,
                "period": period,
                "metric_label": "revenue_growth",
                "comparison_basis": "year_over_year",
            },
            "ai-contribution": {
                "value": 3,
                "unit": "percentage_points",
                "currency": None,
                "period": period,
                "metric_label": "ai_services_contribution_to_revenue_growth",
                "comparison_basis": "year_over_year",
            },
            "revenue": {
                "value": 100,
                "unit": "usd_millions",
                "currency": "USD",
                "period": period,
                "metric_label": "revenue",
                "comparison_basis": "none",
            },
        }
        relationship = {
            "relationship_id": "growth-contribution-and-scale",
            "compatibility": "compatible",
            "required_facts": [
                {
                    "fact_path": (
                        f"deterministic_current.relationship_facts.{fact_id}"
                    )
                }
                for fact_id in facts
            ],
        }
        professional = (
            "Revenue growth was 12% in FY2025 year over year. "
            "AI services contribution to revenue growth was 3 percentage "
            "points in FY2025 year over year. Revenue was $100 million in "
            "FY2025."
        )

        def payload_for(observation):
            payload = investment_report_payload()
            payload["summary"] = (
                "Revenue growth was 12% in FY2025 year over year. AI services "
                "contribution to revenue growth was 3 percentage points in "
                "FY2025 year over year."
            )
            payload["thesis"] = (
                "Growth contribution aligned with the overall growth rate."
            )
            payload["relationship_reconciliations"] = [
                {
                    "relationship_id": relationship["relationship_id"],
                    "status": "reconciled",
                    "fact_paths": [
                        ref["fact_path"]
                        for ref in relationship["required_facts"]
                    ],
                    "observation": observation,
                    "interpretation": "Growth and scale are mutually consistent",
                    "uncertainty": "The durability of growth remains uncertain",
                    "summary_synthesis": payload["summary"],
                    "thesis_synthesis": payload["thesis"],
                    "summary_fact_paths": [
                        ref["fact_path"]
                        for ref in relationship["required_facts"][:2]
                    ],
                }
            ]
            rendered_values = ("12%", "3", "$100 million")
            payload["numeric_claims"] = [
                {
                    "claim_id": f"{target}-{fact_id}",
                    "path": (
                        "relationship_reconciliations[0].observation"
                        if target == "observation"
                        else "summary"
                    ),
                    "value": rendered_values[index],
                    "metric": fact["metric_label"],
                    "period": fact["period"],
                    "unit": fact["unit"],
                    "currency": fact["currency"],
                    "source_kind": "fact",
                    "fact_path": (
                        "deterministic_current.relationship_facts."
                        f"{fact_id}"
                    ),
                }
                for target in ("observation", "summary")
                for index, (fact_id, fact) in enumerate(facts.items())
                if target == "observation" or index < 2
            ]
            return payload

        cases = (
            ("professional atomic clauses", professional, True),
            (
                "enum literals in prose",
                (
                    "Revenue_growth was 12 percent in FY2025 year_over_year. "
                    "AI_services_contribution_to_revenue_growth was 3 "
                    "percentage_points in FY2025 year_over_year. Revenue was "
                    "100 usd_millions in FY2025."
                ),
                False,
            ),
            (
                "period and basis shared by later clauses",
                (
                    "Revenue growth was 12% and AI services contribution to "
                    "revenue growth was 3 percentage points. Revenue was $100 "
                    "million in FY2025 year over year."
                ),
                False,
            ),
        )
        for label, observation, accepted in cases:
            with self.subTest(surface=label):
                problems = service.numeric_claim_source_problems(
                    payload_for(observation),
                    deterministic_current={},
                    deterministic_prior={},
                    relationship_facts=facts,
                    material_relationships=[relationship],
                )
                if accepted:
                    self.assertEqual(problems, [])
                else:
                    self.assertTrue(
                        any(
                            "fact source tuple does not match its authored target"
                            in problem
                            for problem in problems
                        ),
                        problems,
                    )
                    self.assertTrue(
                        any(
                            "requires exactly one numeric_claims fact binding"
                            in problem
                            for problem in problems
                        ),
                        problems,
                    )

    def test_relationship_claim_values_are_scalars_or_compact_numeric_tokens(self):
        valid_values = (12, -0.5, "12%", "$100 million")
        invalid_values = (
            True,
            float("nan"),
            "12 percent revenue growth in FY2025 year over year",
            "1" * 65,
        )
        for value in valid_values:
            with self.subTest(valid=value):
                row = {
                    "claim_id": "relationship-value",
                    "path": "relationship_reconciliations[0].observation",
                    "value": value,
                    "metric": "revenue_growth",
                    "period": "FY2025",
                    "unit": "percent",
                    "currency": None,
                    "source_kind": "fact",
                    "fact_path": (
                        "deterministic_current.relationship_facts.revenue-growth"
                    ),
                }
                self.assertEqual(service.validate_numeric_claim_rows([row]), [])
        for value in invalid_values:
            with self.subTest(invalid=value):
                row = {
                    "claim_id": "relationship-value",
                    "path": "relationship_reconciliations[0].observation",
                    "value": value,
                    "metric": "revenue_growth",
                    "period": "FY2025",
                    "unit": "percent",
                    "currency": None,
                    "source_kind": "fact",
                    "fact_path": (
                        "deterministic_current.relationship_facts.revenue-growth"
                    ),
                }
                problems = service.validate_numeric_claim_rows([row])
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("value must", problems[0])

    def test_compatible_relationship_numbers_require_exact_ledger_bindings(self):
        request = service.build_investment_analysis_request(
            {"company": "Example Company", "document_id": "doc-1"},
            (
                "Operating cash flow was $42 million in FY2025. Capital "
                "expenditures were $18 million in FY2025."
            ),
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
        relationship = service._plain_json_value(
            request.material_relationships[0]
        )
        facts_by_id = service._plain_json_value(request.relationship_facts)
        observation = (
            "Operating cash flow was $42 million in FY2025. Capital "
            "expenditures were $18 million in FY2025"
        )
        payload = investment_report_payload()
        payload["summary"] = observation + ". Measurement bases remain comparable."
        payload["thesis"] = "Cash generation covered investment."
        payload["relationship_reconciliations"] = [
            {
                "relationship_id": relationship["relationship_id"],
                "status": "reconciled",
                "fact_paths": [
                    ref["fact_path"] for ref in relationship["required_facts"]
                ],
                "observation": observation,
                "interpretation": "Cash generation covered investment",
                "uncertainty": "Measurement bases remain comparable",
                "summary_synthesis": payload["summary"],
                "thesis_synthesis": payload["thesis"],
                "summary_fact_paths": [
                    ref["fact_path"] for ref in relationship["required_facts"]
                ],
            }
        ]
        missing = service.numeric_claim_source_problems(
            payload,
            deterministic_current={},
            deterministic_prior={},
            relationship_facts=facts_by_id,
            material_relationships=[relationship],
        )
        coverage = {
            item
            for item in missing
            if "material numeric token" in item
        }
        self.assertEqual(coverage, set())
        observation_missing = [
            item
            for item in missing
            if "relationship_reconciliations[0].observation" in item
        ]
        self.assertEqual(
            len(observation_missing),
            len(relationship["required_facts"]),
        )
        expected_summary_missing = [
            (
                f"summary: selected relationship fact {ref['fact_path']!r} "
                "requires exactly one numeric_claims fact binding"
            )
            for ref in relationship["required_facts"]
        ]
        self.assertEqual(
            [item for item in missing if item.startswith("summary:")],
            expected_summary_missing,
        )

        rows = []
        for index, ref in enumerate(relationship["required_facts"]):
            fact = facts_by_id[ref["fact_path"].rsplit(".", 1)[-1]]
            rows.append(
                {
                    "claim_id": f"relationship-fact-{index}",
                    "path": "relationship_reconciliations[0].observation",
                    "value": fact["value"],
                    "metric": fact["metric_label"],
                    "period": fact["period"],
                    "unit": fact["unit"],
                    "currency": fact.get("currency"),
                    "source_kind": "fact",
                    "fact_path": ref["fact_path"],
                }
            )
        payload["numeric_claims"] = rows
        self.assertEqual(
            service.numeric_claim_source_problems(
                payload,
                deterministic_current={},
                deterministic_prior={},
                relationship_facts=facts_by_id,
                material_relationships=[relationship],
            ),
            expected_summary_missing,
        )
        payload["numeric_claims"].extend(
            {
                **row,
                "claim_id": f"summary-fact-{index}",
                "path": "summary",
            }
            for index, row in enumerate(rows)
        )
        self.assertEqual(
            service.numeric_claim_source_problems(
                payload,
                deterministic_current={},
                deterministic_prior={},
                relationship_facts=facts_by_id,
                material_relationships=[relationship],
            ),
            [],
        )

    def test_reused_relationship_fact_requires_one_binding_per_target(self):
        fact_path = "deterministic_current.relationship_facts.shared-cash-flow"
        relationship_facts = {
            "shared-cash-flow": {
                "value": 42,
                "unit": "usd_millions",
                "currency": "USD",
                "period": "FY2025",
                "metric_label": "operating_cash_flow",
                "metric_key": "operating_cash_flow",
            }
        }
        material_relationships = [
            {
                "relationship_id": "cash-funds-investment",
                "compatibility": "compatible",
                "required_facts": [{"fact_path": fact_path}],
            },
            {
                "relationship_id": "cash-supports-liquidity",
                "compatibility": "compatible",
                "required_facts": [{"fact_path": fact_path}],
            },
        ]
        payload = investment_report_payload()
        payload["summary"] = (
            "Operating cash flow was $42 million in FY2025, supporting "
            "investment and liquidity."
        )
        payload["thesis"] = (
            "Cash generation supports both reinvestment and liquidity."
        )
        payload["relationship_reconciliations"] = [
            {
                "relationship_id": "cash-funds-investment",
                "status": "reconciled",
                "fact_paths": [fact_path],
                "observation": (
                    "Operating cash flow was $42 million in FY2025, funding "
                    "investment"
                ),
                "interpretation": "Cash generation covered investment",
                "uncertainty": "Future investment needs may change",
                "summary_synthesis": payload["summary"],
                "thesis_synthesis": payload["thesis"],
                "summary_fact_paths": [fact_path],
            },
            {
                "relationship_id": "cash-supports-liquidity",
                "status": "reconciled",
                "fact_paths": [fact_path],
                "observation": (
                    "Operating cash flow was $42 million in FY2025, supporting "
                    "liquidity"
                ),
                "interpretation": "Cash generation supported liquidity",
                "uncertainty": "Future liquidity needs may change",
                "summary_synthesis": payload["summary"],
                "thesis_synthesis": payload["thesis"],
                "summary_fact_paths": [fact_path],
            },
        ]

        def row(claim_id, target_index, **changes):
            result = {
                "claim_id": claim_id,
                "path": (
                    f"relationship_reconciliations[{target_index}].observation"
                ),
                "value": 42,
                "metric": "operating_cash_flow",
                "period": "FY2025",
                "unit": "usd_millions",
                "currency": "USD",
                "source_kind": "fact",
                "fact_path": fact_path,
            }
            result.update(changes)
            return result

        summary_row = {
            "claim_id": "shared-fact-summary",
            "path": "summary",
            "value": 42,
            "metric": "operating_cash_flow",
            "period": "FY2025",
            "unit": "usd_millions",
            "currency": "USD",
            "source_kind": "fact",
            "fact_path": fact_path,
        }

        def problems(rows, summary_rows=(summary_row,)):
            payload["numeric_claims"] = [*rows, *summary_rows]
            return service.numeric_claim_source_problems(
                payload,
                deterministic_current={},
                deterministic_prior={},
                relationship_facts=relationship_facts,
                material_relationships=material_relationships,
            )

        first_row = row("shared-fact-first-target", 0)
        second_row = row("shared-fact-second-target", 1)
        second_missing = problems([first_row])
        self.assertEqual(len(second_missing), 1, second_missing)
        self.assertIn(
            "relationship_reconciliations[1].observation",
            second_missing[0],
        )
        self.assertIn(
            "requires exactly one numeric_claims fact binding",
            second_missing[0],
        )
        self.assertEqual(problems([first_row, second_row]), [])

        shared_synthesis = payload["relationship_reconciliations"][0][
            "summary_synthesis"
        ]
        self.assertEqual(payload["summary"].count(shared_synthesis), 1)
        self.assertEqual(
            payload["relationship_reconciliations"][0]["summary_synthesis"],
            payload["relationship_reconciliations"][1]["summary_synthesis"],
        )
        missing_summary = problems([first_row, second_row], summary_rows=())
        self.assertTrue(
            any(
                "summary" in item
                and "requires exactly one numeric_claims fact binding" in item
                for item in missing_summary
            ),
            missing_summary,
        )
        duplicate_summary = problems(
            [first_row, second_row],
            summary_rows=(
                summary_row,
                {**summary_row, "claim_id": "duplicate-summary"},
            ),
        )
        self.assertTrue(
            any(
                "summary" in item
                and "requires exactly one numeric_claims fact binding" in item
                for item in duplicate_summary
            ),
            duplicate_summary,
        )
        wrong_summary = problems(
            [first_row, second_row],
            summary_rows=({**summary_row, "value": 41},),
        )
        self.assertTrue(wrong_summary)

        def validate_live(summary_rows):
            payload["numeric_claims"] = [
                first_row,
                second_row,
                *summary_rows,
            ]
            return service._validated_investment_facts(
                json.dumps(payload),
                excerpt="Demand remained durable.",
                news_items=[],
                deterministic_current={},
                deterministic_prior={},
                relationship_facts=relationship_facts,
                material_relationships=material_relationships,
            )

        parsed = validate_live((summary_row,))
        self.assertEqual(parsed["summary"], payload["summary"])
        for label, summary_rows in (
            ("missing", ()),
            ("wrong", ({**summary_row, "value": 41},)),
        ):
            with self.subTest(live_summary_row=label):
                with self.assertRaises(service.InvestmentValidationError):
                    validate_live(summary_rows)

        child_path_escape = problems(
            [
                first_row,
                second_row,
                row(
                    "shared-fact-child-path-escape",
                    0,
                    fact_path=f"{fact_path}.value",
                ),
            ]
        )
        self.assertTrue(
            any("numeric_claims[2]" in item for item in child_path_escape),
            child_path_escape,
        )

        rejected_second_rows = (
            (
                "relationship fact child path",
                row(
                    "shared-fact-child-path",
                    1,
                    fact_path=f"{fact_path}.value",
                ),
            ),
            (
                "wrong value",
                row("shared-fact-wrong-value", 1, value=41),
            ),
            (
                "wrong unit",
                row("shared-fact-wrong-unit", 1, unit="usd_billions"),
            ),
            (
                "wrong currency",
                row("shared-fact-wrong-currency", 1, currency="EUR"),
            ),
            (
                "wrong period",
                row("shared-fact-wrong-period", 1, period="FY2024"),
            ),
            (
                "cross-target binding",
                row("shared-fact-wrong-target", 0),
            ),
        )
        for label, rejected_second_row in rejected_second_rows:
            with self.subTest(case=label):
                rejection_problems = problems([first_row, rejected_second_row])
                self.assertTrue(
                    any(
                        "relationship_reconciliations[1].observation" in item
                        and "requires exactly one numeric_claims fact binding" in item
                        for item in rejection_problems
                    ),
                    rejection_problems,
                )

    def test_relationship_fact_metric_requires_exact_normalized_label(self):
        fact_path = "deterministic_current.relationship_facts.cash-flow"
        relationship_fact = {
            "value": 42,
            "unit": "usd_millions",
            "currency": "USD",
            "period": "FY2025",
            "metric_label": "operating_cash_flow",
            "metric_key": "operating_cash_flow",
        }
        relationship = {
            "relationship_id": "cash-funds-investment",
            "compatibility": "compatible",
            "required_facts": [{"fact_path": fact_path}],
        }
        payload = investment_report_payload()
        payload["summary"] = "Operating cash flow was $42 million in FY2025."
        payload["thesis"] = "Cash generation supported investment."
        payload["relationship_reconciliations"] = [
            {
                "relationship_id": relationship["relationship_id"],
                "status": "reconciled",
                "fact_paths": [fact_path],
                "observation": (
                    "Operating cash flow was $42 million in FY2025, funding "
                    "investment"
                ),
                "interpretation": "Cash generation covered investment",
                "uncertainty": "Future investment needs may change",
                "summary_synthesis": payload["summary"],
                "thesis_synthesis": payload["thesis"],
                "summary_fact_paths": [fact_path],
            }
        ]
        row = {
            "claim_id": "cash-flow-relationship-fact",
            "path": "relationship_reconciliations[0].observation",
            "value": 42,
            "metric": relationship_fact["metric_label"],
            "period": "FY2025",
            "unit": "usd_millions",
            "currency": "USD",
            "source_kind": "fact",
            "fact_path": fact_path,
        }
        summary_row = {
            **row,
            "claim_id": "cash-flow-summary-fact",
            "path": "summary",
        }

        def problems(metric):
            payload["numeric_claims"] = [
                {**row, "metric": metric},
                summary_row,
            ]
            return service.numeric_claim_source_problems(
                payload,
                deterministic_current={},
                deterministic_prior={},
                relationship_facts={"cash-flow": relationship_fact},
                material_relationships=[relationship],
            )

        self.assertEqual(problems("operating_cash_flow"), [])
        for metric in (
            "cash flow from operations",
            "adjusted operating_cash_flow",
        ):
            with self.subTest(metric=metric):
                metric_problems = problems(metric)
                self.assertTrue(
                    any(
                        "requires exactly one numeric_claims fact binding" in item
                        for item in metric_problems
                    ),
                    metric_problems,
                )

    def test_ordinary_deterministic_fact_metric_alias_remains_valid(self):
        payload = investment_report_payload()
        payload["summary"] = (
            "Capital expenditures were $18 million in FY2025."
        )
        payload["numeric_claims"] = [
            {
                "claim_id": "ordinary-capex-alias",
                "path": "summary",
                "value": 18,
                "metric": "capex",
                "period": "FY2025",
                "unit": "usd_millions",
                "currency": "USD",
                "source_kind": "fact",
                "fact_path": (
                    "deterministic_current.capital_expenditures.value"
                ),
            }
        ]
        self.assertEqual(
            service.numeric_claim_source_problems(
                payload,
                deterministic_current={
                    "capital_expenditures": {
                        "value": 18,
                        "unit": "usd_millions",
                        "currency": "USD",
                        "period": "FY2025",
                    }
                },
                deterministic_prior={},
            ),
            [],
        )

    def test_cash_capex_alias_preserves_exact_fact_and_target_tuple(self):
        cash_fact = {
            "value": 13.9,
            "unit": "usd_billions",
            "currency": "USD",
            "period": "FY2024-Q4",
            "metric_label": "cash_paid_for_property_and_equipment",
            "metric_key": "cash_paid_for_property_and_equipment",
            "source": "reported",
            "evidence": ["Cash paid for property and equipment was $13.9 billion"],
            "cash_basis": "cash",
            "relationship_tags": {
                "metric_family": "capex",
            },
        }
        relationship_facts = {
            "cash-capex": cash_fact,
            "equal-valued-other-leaf": {
                **cash_fact,
                "metric_label": "free_cash_flow",
                "metric_key": "free_cash_flow",
            },
            "lease-inclusive-capex": {
                **cash_fact,
                "value": 19.0,
                "metric_label": "capital_expenditures_including_finance_leases",
                "metric_key": "capital_expenditures_including_finance_leases",
                "cash_basis": "cash_plus_finance_leases",
                "relationship_tags": {
                    "metric_family": "capex",
                },
            },
        }
        summary = (
            "Cash capital expenditures were $13.9 billion in FY2024 Q4."
        )
        row = {
            "claim_id": "cash-capex-fy24q4",
            "path": "summary",
            "value": 13.9,
            "metric": "cash_paid_for_property_and_equipment",
            "period": "FY2024 Q4",
            "unit": "usd_billions",
            "currency": "USD",
            "source_kind": "fact",
            "fact_path": (
                "deterministic_current.relationship_facts.cash-capex"
            ),
        }

        def source_problems(authored_summary, authored_row):
            payload = investment_report_payload()
            payload["summary"] = authored_summary
            payload["numeric_claims"] = [authored_row]
            return service.numeric_claim_source_problems(
                payload,
                deterministic_current={},
                deterministic_prior={},
                relationship_facts=relationship_facts,
            )

        self.assertEqual(source_problems(summary, row), [])

        rejected = (
            (
                "missing fact path",
                summary,
                {
                    **row,
                    "fact_path": (
                        "deterministic_current.relationship_facts.missing"
                    ),
                },
            ),
            (
                "wrong equal-valued fact leaf",
                summary,
                {
                    **row,
                    "fact_path": (
                        "deterministic_current.relationship_facts."
                        "equal-valued-other-leaf"
                    ),
                },
            ),
            (
                "lease-inclusive amount and source",
                "Cash capital expenditures were $19 billion in FY2024 Q4.",
                {
                    **row,
                    "value": 19.0,
                    "fact_path": (
                        "deterministic_current.relationship_facts."
                        "lease-inclusive-capex"
                    ),
                },
            ),
            (
                "lease-inclusive target prose",
                (
                    "Capital expenditures including finance leases were "
                    "$13.9 billion in FY2024 Q4."
                ),
                row,
            ),
            (
                "wrong period",
                summary,
                {**row, "period": "FY2023 Q4"},
            ),
            (
                "wrong unit",
                summary,
                {**row, "unit": "usd_millions"},
            ),
            (
                "wrong currency",
                summary,
                {**row, "currency": "EUR"},
            ),
            (
                "unrelated target-local metric",
                "Free cash flow was $13.9 billion in FY2024 Q4.",
                row,
            ),
        )
        for label, authored_summary, authored_row in rejected:
            with self.subTest(case=label):
                problems = source_problems(authored_summary, authored_row)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("numeric_claims[0]", problems[0])


    def test_lease_inclusive_target_only_guards_normalized_relationship_basis(
        self,
    ):
        summary = (
            "Capital expenditures including finance leases were $19 billion "
            "in FY2024 Q4."
        )
        metric = "capital_expenditures_including_finance_leases"
        deterministic_current = {
            "capital_expenditures_including_finance_leases": {
                "value": 19.0,
                "unit": "usd_billions",
                "currency": "USD",
                "period": "FY2024-Q4",
            }
        }
        relationship_facts = {
            "cash-capex": {
                "value": 19.0,
                "unit": "usd_billions",
                "currency": "USD",
                "period": "FY2024-Q4",
                "metric_label": (
                    "capital_expenditures_including_finance_leases"
                ),
                "metric_key": (
                    "capital_expenditures_including_finance_leases"
                ),
                "cash_basis": "cash",
            },
            "lease-capex": {
                "value": 19.0,
                "unit": "usd_billions",
                "currency": "USD",
                "period": "FY2024-Q4",
                "metric_label": (
                    "capital_expenditures_including_finance_leases"
                ),
                "metric_key": (
                    "capital_expenditures_including_finance_leases"
                ),
                "cash_basis": "cash_plus_finance_leases",
            },
        }

        def source_problems(source_kind, **source_fields):
            payload = investment_report_payload()
            payload["summary"] = summary
            payload["source_excerpt"] = summary
            payload["numeric_claims"] = [
                {
                    "claim_id": f"lease-inclusive-{source_kind}",
                    "path": "summary",
                    "value": 19.0,
                    "metric": metric,
                    "period": "FY2024 Q4",
                    "unit": "usd_billions",
                    "currency": "USD",
                    "source_kind": source_kind,
                    **source_fields,
                }
            ]
            return service.numeric_claim_source_problems(
                payload,
                deterministic_current=deterministic_current,
                deterministic_prior={},
                relationship_facts=relationship_facts,
            )

        accepted = (
            (
                "text source has no cash-basis guard",
                "text",
                {"quote": summary[:-1]},
            ),
            (
                "ordinary deterministic fact has no cash-basis guard",
                "fact",
                {
                    "fact_path": (
                        "deterministic_current."
                        "capital_expenditures_including_finance_leases.value"
                    )
                },
            ),
            (
                "normalized lease relationship matches the explicit target",
                "fact",
                {
                    "fact_path": (
                        "deterministic_current.relationship_facts.lease-capex"
                    )
                },
            ),
        )
        for label, source_kind, source_fields in accepted:
            with self.subTest(case=label):
                self.assertEqual(
                    source_problems(source_kind, **source_fields),
                    [],
                )

        problems = source_problems(
            "fact",
            fact_path=(
                "deterministic_current.relationship_facts.cash-capex"
            ),
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("numeric_claims[0]", problems[0])
        self.assertIn(
            "fact source tuple does not match its authored target",
            problems[0],
        )

    def test_target_cash_basis_matches_exact_normalized_relationship_fact(self):
        common = {
            "value": 13.9,
            "unit": "usd_billions",
            "currency": "USD",
            "period": "FY2024-Q4",
            "source": "reported",
            "evidence": ["Capital investment was $13.9 billion"],
        }
        capex_identity = {
            **common,
            "metric_label": "capital_expenditures",
            "metric_key": "capital_expenditures",
        }
        lease_identity = {
            **common,
            "metric_label": "capital_expenditures_including_finance_leases",
            "metric_key": "capital_expenditures_including_finance_leases",
        }
        relationship_facts = {
            "cash": {**capex_identity, "cash_basis": "cash"},
            "cash-opposite": {
                **capex_identity,
                "cash_basis": "cash_plus_finance_leases",
            },
            "cash-absent": capex_identity,
            "cash-not-applicable": {
                **capex_identity,
                "cash_basis": "not_applicable",
            },
            "lease-inclusive": {
                **lease_identity,
                "cash_basis": "cash_plus_finance_leases",
            },
            "lease-opposite": {**lease_identity, "cash_basis": "cash"},
            "lease-absent": lease_identity,
            "lease-not-applicable": {
                **lease_identity,
                "cash_basis": "not_applicable",
            },
            "neutral-absent": capex_identity,
            "neutral-not-applicable": {
                **capex_identity,
                "cash_basis": "not_applicable",
            },
            "wrong-leaf": {
                **common,
                "metric_label": "free_cash_flow",
                "metric_key": "free_cash_flow",
                "cash_basis": "cash",
            },
        }
        cash_summary = (
            "Cash paid for property and equipment was $13.9 billion "
            "in FY2024 Q4."
        )
        lease_summary = (
            "Capital expenditures including finance leases were "
            "$13.9 billion in FY2024 Q4."
        )
        neutral_summary = (
            "Capital expenditures were $13.9 billion in FY2024 Q4."
        )

        def source_problems(summary, fact_id, metric, *, fact_path=None):
            payload = investment_report_payload()
            payload["summary"] = summary
            payload["numeric_claims"] = [
                {
                    "claim_id": f"basis-{fact_id}",
                    "path": "summary",
                    "value": 13.9,
                    "metric": metric,
                    "period": "FY2024 Q4",
                    "unit": "usd_billions",
                    "currency": "USD",
                    "source_kind": "fact",
                    "fact_path": fact_path
                    or (
                        "deterministic_current.relationship_facts."
                        f"{fact_id}"
                    ),
                }
            ]
            return service.numeric_claim_source_problems(
                payload,
                deterministic_current={},
                deterministic_prior={},
                relationship_facts=relationship_facts,
            )

        accepted = (
            (
                "cash prose and cash fact",
                cash_summary,
                "cash",
                "capital_expenditures",
            ),
            (
                "lease-inclusive prose and lease-inclusive fact",
                lease_summary,
                "lease-inclusive",
                "capital_expenditures_including_finance_leases",
            ),
            (
                "neutral prose and absent basis",
                neutral_summary,
                "neutral-absent",
                "capital_expenditures",
            ),
            (
                "neutral prose and not-applicable basis",
                neutral_summary,
                "neutral-not-applicable",
                "capital_expenditures",
            ),
        )
        for label, summary, fact_id, metric in accepted:
            with self.subTest(case=label):
                self.assertEqual(
                    source_problems(summary, fact_id, metric),
                    [],
                )

        combined = investment_report_payload()
        combined["summary"] = f"{cash_summary} {lease_summary}"
        combined["numeric_claims"] = [
            {
                "claim_id": "basis-cash-local",
                "path": "summary",
                "value": 13.9,
                "metric": "capital_expenditures",
                "period": "FY2024 Q4",
                "unit": "usd_billions",
                "currency": "USD",
                "source_kind": "fact",
                "fact_path": "deterministic_current.relationship_facts.cash",
            },
            {
                "claim_id": "basis-lease-local",
                "path": "summary",
                "value": 13.9,
                "metric": "capital_expenditures_including_finance_leases",
                "period": "FY2024 Q4",
                "unit": "usd_billions",
                "currency": "USD",
                "source_kind": "fact",
                "fact_path": (
                    "deterministic_current.relationship_facts.lease-inclusive"
                ),
            },
        ]
        self.assertEqual(
            service.numeric_claim_source_problems(
                combined,
                deterministic_current={},
                deterministic_prior={},
                relationship_facts=relationship_facts,
            ),
            [],
        )

        rejected = (
            (
                "cash prose and lease-inclusive basis",
                cash_summary,
                "cash-opposite",
                "capital_expenditures",
                None,
            ),
            (
                "lease-inclusive prose and cash basis",
                lease_summary,
                "lease-opposite",
                "capital_expenditures_including_finance_leases",
                None,
            ),
            (
                "cash prose and absent basis",
                cash_summary,
                "cash-absent",
                "capital_expenditures",
                None,
            ),
            (
                "cash prose and not-applicable basis",
                cash_summary,
                "cash-not-applicable",
                "capital_expenditures",
                None,
            ),
            (
                "lease-inclusive prose and absent basis",
                lease_summary,
                "lease-absent",
                "capital_expenditures_including_finance_leases",
                None,
            ),
            (
                "lease-inclusive prose and not-applicable basis",
                lease_summary,
                "lease-not-applicable",
                "capital_expenditures_including_finance_leases",
                None,
            ),
            (
                "wrong equal-valued leaf",
                cash_summary,
                "wrong-leaf",
                "capital_expenditures",
                None,
            ),
            (
                "wrong child path",
                cash_summary,
                "cash",
                "capital_expenditures",
                "deterministic_current.relationship_facts.cash.value",
            ),
            (
                "missing path",
                cash_summary,
                "cash",
                "capital_expenditures",
                "deterministic_current.relationship_facts.missing",
            ),
        )
        for label, summary, fact_id, metric, fact_path in rejected:
            with self.subTest(case=label):
                problems = source_problems(
                    summary,
                    fact_id,
                    metric,
                    fact_path=fact_path,
                )
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("numeric_claims[0]", problems[0])


    def _payload(self):
        payload = investment_report_payload()
        payload["summary"] = (
            "Cash generation supported reinvestment without external funding."
        )
        payload["thesis"] = (
            "Internal funding capacity strengthens the reinvestment case."
        )
        payload["relationship_reconciliations"] = [
            {
                "relationship_id": "rel-compatible",
                "status": "reconciled",
                "fact_paths": [
                    "deterministic_current.relationship_facts.fact-a",
                    "deterministic_current.relationship_facts.fact-b",
                ],
                "observation": "Cash generation exceeded investment",
                "interpretation": (
                    "Cash generation covered investment, supporting internally "
                    "funded reinvestment"
                ),
                "uncertainty": "Measurement bases remain comparable",
                "summary_synthesis": (
                    "Cash generation supported reinvestment without external "
                    "funding."
                ),
                "thesis_synthesis": (
                    "Internal funding capacity strengthens the reinvestment case."
                ),
                "summary_fact_paths": [
                    "deterministic_current.relationship_facts.fact-a"
                ],
            },
            {
                "relationship_id": "rel-incompatible",
                "status": "abstained_incompatible",
                "fact_paths": [
                    "deterministic_current.relationship_facts.fact-c"
                ],
                "observation": (
                    "The external effect and recipient use incompatible periods"
                ),
                "interpretation": "",
                "uncertainty": "No cross-period conclusion is supported",
                "summary_synthesis": "",
                "thesis_synthesis": "",
                "summary_fact_paths": [],
            },
        ]
        return payload

    def test_empty_and_populated_relationship_contracts_validate(self):
        empty = investment_report_payload()
        self.assertEqual(
            service.relationship_reconciliation_problems(
                empty, material_relationships=[]
            ),
            [],
        )
        populated = self._payload()
        self.assertEqual(
            service.validate_investment_report_payload(populated), []
        )
        self.assertEqual(
            service.relationship_reconciliation_problems(
                populated, material_relationships=self._contract()
            ),
            [],
        )

    def test_relationship_rows_are_an_ordered_bijection_with_exact_paths(self):
        cases = {}
        missing = self._payload()
        missing["relationship_reconciliations"].pop()
        cases["missing row"] = missing
        extra = self._payload()
        extra["relationship_reconciliations"].append(
            copy.deepcopy(extra["relationship_reconciliations"][0])
        )
        cases["extra row"] = extra
        swapped = self._payload()
        swapped["relationship_reconciliations"].reverse()
        cases["swapped order"] = swapped
        omitted_path = self._payload()
        omitted_path["relationship_reconciliations"][0]["fact_paths"].pop()
        cases["omitted path"] = omitted_path
        reordered_paths = self._payload()
        reordered_paths["relationship_reconciliations"][0]["fact_paths"].reverse()
        cases["reordered paths"] = reordered_paths
        duplicate_paths = self._payload()
        duplicate_paths["relationship_reconciliations"][0]["fact_paths"][1] = (
            duplicate_paths["relationship_reconciliations"][0]["fact_paths"][0]
        )
        cases["duplicate paths"] = duplicate_paths
        foreign_path = self._payload()
        foreign_path["relationship_reconciliations"][1]["fact_paths"] = [
            "deterministic_current.relationship_facts.fact-foreign"
        ]
        cases["foreign path"] = foreign_path
        for label, payload in cases.items():
            with self.subTest(case=label):
                self.assertTrue(
                    service.relationship_reconciliation_problems(
                        payload, material_relationships=self._contract()
                    ),
                    f"{label} must fail closed",
                )

    def test_relationship_status_synthesis_and_audit_fields_fail_closed(self):
        cases = {}
        wrong_status = self._payload()
        wrong_status["relationship_reconciliations"][0][
            "status"
        ] = "abstained_incompatible"
        cases["compatible status"] = wrong_status
        compatible_blank = self._payload()
        compatible_blank["relationship_reconciliations"][0]["interpretation"] = " "
        cases["compatible blank interpretation"] = compatible_blank
        incompatible_interpretation = self._payload()
        incompatible_interpretation["relationship_reconciliations"][1][
            "interpretation"
        ] = "A forbidden conclusion"
        cases["incompatible interpretation"] = incompatible_interpretation
        blank_observation = self._payload()
        blank_observation["relationship_reconciliations"][0]["observation"] = " "
        cases["blank observation"] = blank_observation
        blank_uncertainty = self._payload()
        blank_uncertainty["relationship_reconciliations"][1]["uncertainty"] = " "
        cases["blank uncertainty"] = blank_uncertainty
        missing_summary_synthesis = self._payload()
        missing_summary_synthesis["summary"] = "Cash remained available."
        cases["summary synthesis coverage"] = missing_summary_synthesis
        missing_thesis_synthesis = self._payload()
        missing_thesis_synthesis["thesis"] = "Reinvestment remains possible."
        cases["thesis synthesis coverage"] = missing_thesis_synthesis
        for label, payload in cases.items():
            with self.subTest(case=label):
                problems = service.relationship_reconciliation_problems(
                    payload, material_relationships=self._contract()
                )
                self.assertTrue(problems, f"{label} must fail closed")

    def test_full_audit_rows_remain_without_old_summary_concatenation(self):
        payload = self._payload()
        compatible, incompatible = payload["relationship_reconciliations"]
        self.assertNotIn(compatible["observation"], payload["summary"])
        self.assertNotIn(compatible["interpretation"], payload["thesis"])
        self.assertNotIn(compatible["uncertainty"], payload["summary"])
        self.assertNotIn(incompatible["observation"], payload["summary"])
        self.assertNotIn(incompatible["uncertainty"], payload["summary"])
        self.assertEqual(
            payload["summary"], compatible["summary_synthesis"]
        )
        self.assertEqual(
            payload["thesis"], compatible["thesis_synthesis"]
        )

    def test_summary_fact_paths_are_unique_bounded_required_fact_subsets(self):
        cases = {}
        empty = self._payload()
        empty["relationship_reconciliations"][0]["summary_fact_paths"] = []
        cases["empty"] = empty
        duplicate = self._payload()
        duplicate["relationship_reconciliations"][0]["summary_fact_paths"] = [
            "deterministic_current.relationship_facts.fact-a",
            "deterministic_current.relationship_facts.fact-a",
        ]
        cases["duplicate"] = duplicate
        foreign = self._payload()
        foreign["relationship_reconciliations"][0]["summary_fact_paths"] = [
            "deterministic_current.relationship_facts.fact-c"
        ]
        cases["foreign"] = foreign
        too_many = self._payload()
        too_many["relationship_reconciliations"][0]["summary_fact_paths"] = [
            "deterministic_current.relationship_facts.fact-a",
            "deterministic_current.relationship_facts.fact-b",
            "deterministic_current.relationship_facts.fact-c",
        ]
        cases["too many"] = too_many
        for label, payload in cases.items():
            with self.subTest(case=label):
                self.assertTrue(
                    service.relationship_reconciliation_problems(
                        payload, material_relationships=self._contract()
                    ),
                    f"{label} must fail closed",
                )

    def test_incompatible_rows_require_empty_synthesis_and_summary_fact_paths(self):
        cases = (
            ("summary_synthesis", "Cross-period cash comparison is unavailable."),
            ("thesis_synthesis", "No cash conclusion can be drawn."),
            (
                "summary_fact_paths",
                ["deterministic_current.relationship_facts.fact-c"],
            ),
        )
        for field, value in cases:
            with self.subTest(field=field):
                payload = self._payload()
                payload["relationship_reconciliations"][1][field] = value
                self.assertTrue(
                    service.relationship_reconciliation_problems(
                        payload, material_relationships=self._contract()
                    )
                )

    def test_strict_schema_rejects_removed_and_extra_reconciliation_keys(self):
        removed = self._payload()
        del removed["relationship_reconciliations"][0]["summary_synthesis"]
        extra = self._payload()
        extra["relationship_reconciliations"][0]["compatibility"] = "compatible"
        for label, payload in (("removed", removed), ("extra", extra)):
            with self.subTest(case=label):
                problems = service.validate_investment_report_payload(payload)
                self.assertTrue(problems)
                self.assertTrue(
                    any("relationship_reconciliations[0]" in item for item in problems)
                )

    def test_relationship_validation_failure_uses_repository_owned_repair_text(self):
        invalid = self._payload()
        invalid["relationship_reconciliations"][0][
            "relationship_id"
        ] = "RAW_PRIVATE_RELATIONSHIP"
        with self.assertRaises(service.InvestmentValidationError) as raised:
            service._validated_investment_facts(
                json.dumps(invalid),
                excerpt="Demand remained durable.",
                news_items=[],
                material_relationships=self._contract(),
            )
        self.assertEqual(
            raised.exception.categories, (service.VALIDATION_JSON_SCHEMA,)
        )
        self.assertEqual(
            raised.exception.correction_requirement,
            service._CORRECTION_REQUIREMENTS[service.VALIDATION_JSON_SCHEMA],
        )
        self.assertNotIn(
            "RAW_PRIVATE_RELATIONSHIP",
            raised.exception.correction_requirement,
        )


if __name__ == '__main__':
    unittest.main()
