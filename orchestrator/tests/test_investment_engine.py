import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from investment_engine import (
    build_deterministic_analysis,
    build_material_relationship_contract,
)


def facts(**values):
    metrics = {
        name: {
            "value": value,
            "unit": "currency",
            "period": "FY2025",
            "evidence": [name],
        }
        for name, value in values.items()
    }
    return {"metrics": metrics, "qualitative": {}}


def _metric(value, unit="currency", period="FY2025", currency=None):
    rec = {
        "value": value,
        "unit": unit,
        "period": period,
        "evidence": ["filing fact"],
        "source": "reported",
    }
    if currency:
        rec["currency"] = currency
    return rec


def _facts(metrics, qualitative=None):
    return {"metrics": metrics, "qualitative": qualitative or {}}


class InvestmentEngineTests(unittest.TestCase):
    def test_fcf_and_margin_use_positive_capex_outflow(self):
        result = build_deterministic_analysis(
            facts(revenue=200, operating_cash_flow=80, capex=20)
        )
        self.assertEqual(result["metrics"]["fcf"]["value"], 60.0)
        self.assertEqual(result["metrics"]["fcf_margin"]["value"], 30.0)

    def test_metric_output_preserves_deterministic_provenance(self):
        current = facts(revenue=200, operating_cash_flow=80, capex=20)
        current["metrics"]["revenue"].update(
            {"source": "sec_xbrl", "concept": "us-gaap:Revenues"}
        )

        result = build_deterministic_analysis(current)

        self.assertEqual(result["metrics"]["revenue"]["source"], "sec_xbrl")
        self.assertEqual(result["metrics"]["revenue"]["concept"], "us-gaap:Revenues")
        self.assertEqual(result["metrics"]["fcf"]["source"], "derived")

    def test_dcf_projects_five_years_and_caps_inferred_growth(self):
        current = facts(
            revenue=200,
            operating_cash_flow=200,
            capex=0,
            free_cash_flow=200,
            net_debt=10,
            shares_outstanding=10,
        )
        current["metrics"]["free_cash_flow"]["period"] = "FY2025"
        previous = facts(
            revenue=100,
            operating_cash_flow=100,
            capex=0,
            free_cash_flow=100,
        )
        previous["metrics"]["free_cash_flow"]["period"] = "FY2024"
        result = build_deterministic_analysis(
            current, previous, {"discount_rate": 0.10, "terminal_growth": 0.03}
        )
        dcf = result["valuation"]["dcf"]
        self.assertEqual(len(dcf["forecast"]), 5)
        self.assertEqual(dcf["assumptions"]["inferred_growth"], 0.20)
        self.assertTrue(math.isfinite(dcf["per_share"]))
        sensitivity = dcf["sensitivity"]
        self.assertEqual(sensitivity["status"], "calculated")
        self.assertLessEqual(len(sensitivity["wacc_terminal_grid"]), 9)
        self.assertTrue(
            all(
                item["discount_rate"] > item["terminal_growth"]
                for item in sensitivity["wacc_terminal_grid"]
            )
        )
        self.assertLessEqual(
            sensitivity["range"]["per_share_min"],
            dcf["per_share"],
        )
        self.assertGreaterEqual(
            sensitivity["range"]["per_share_max"],
            dcf["per_share"],
        )
        self.assertIn(
            sensitivity["largest_range_driver"],
            {"starting_fcf", "annual_growth", "discount_rate", "terminal_growth"},
        )

    def test_dcf_sensitivity_remains_enterprise_value_only_without_bridge(self):
        current = facts(
            revenue=200,
            operating_cash_flow=80,
            capex=20,
            free_cash_flow=60,
        )
        current["metrics"]["free_cash_flow"]["period"] = "TTM-2025"
        previous = facts(
            revenue=180,
            operating_cash_flow=70,
            capex=20,
            free_cash_flow=50,
        )
        previous["metrics"]["free_cash_flow"]["period"] = "TTM-2024"

        sensitivity = build_deterministic_analysis(current, previous)["valuation"][
            "dcf"
        ]["sensitivity"]

        self.assertEqual(sensitivity["status"], "enterprise_value_only")
        self.assertIsNone(sensitivity["range"]["per_share_min"])
        self.assertIsNotNone(sensitivity["range"]["enterprise_value_min"])

    def test_dcf_sensitivity_fails_closed_with_invalid_base(self):
        result = build_deterministic_analysis(
            facts(revenue=200, operating_cash_flow=10, capex=20),
            facts(revenue=180, operating_cash_flow=20, capex=10),
        )
        sensitivity = result["valuation"]["dcf"]["sensitivity"]
        self.assertEqual(sensitivity["status"], "unavailable")
        self.assertIsNone(sensitivity["range"]["per_share_min"])
        self.assertIsNone(sensitivity["range"]["enterprise_value_min"])

    def test_pe_uses_eps_then_market_cap_net_income_fallback(self):
        direct = build_deterministic_analysis(facts(market_price=100, diluted_eps=5))
        self.assertEqual(direct["valuation"]["pe"], 20.0)
        fallback = build_deterministic_analysis(
            facts(net_income=50), market_inputs={"market_cap": 1000}
        )
        self.assertEqual(fallback["valuation"]["pe"], 20.0)

    def test_zero_and_malformed_values_remain_missing(self):
        result = build_deterministic_analysis(
            facts(
                revenue=0,
                operating_cash_flow="nan",
                capex=True,
                gross_margin=None,
            )
        )
        self.assertEqual(result["metrics"]["revenue"]["value"], 0.0)
        self.assertIsNone(result["metrics"]["operating_cash_flow"]["value"])
        self.assertIsNone(result["metrics"]["capex"]["value"])
        self.assertIsNone(result["metrics"]["gross_margin"]["value"])

    def test_threshold_boundaries_are_explicit(self):
        prior = facts(revenue=100, operating_cash_flow=10, capex=1)
        at_ten = build_deterministic_analysis(
            facts(revenue=110, operating_cash_flow=11, capex=1), prior
        )
        self.assertEqual(at_ten["signals"]["revenue"]["score"], 2)
        just_under = build_deterministic_analysis(
            facts(revenue=109.99, operating_cash_flow=11, capex=1), prior
        )
        self.assertEqual(just_under["signals"]["revenue"]["score"], 1)

    def test_inventory_growth_above_revenue_is_a_risk(self):
        result = build_deterministic_analysis(
            facts(revenue=110, inventory=130), facts(revenue=100, inventory=100)
        )
        self.assertEqual(result["signals"]["inventory_vs_revenue"]["score"], -2)
        self.assertIn("inventory relative to revenue", result["risks"])

    def test_dram_leading_signals_compound(self):
        result = build_deterministic_analysis(
            {
                **facts(
                    revenue=120,
                    operating_cash_flow=50,
                    capex=30,
                    gross_margin=55,
                    inventory=10,
                    backlog=40,
                ),
                "qualitative": {
                    "ai_demand": {
                        "present": True,
                        "strength": "strong",
                        "evidence": ["ai"],
                    },
                    "data_centre_demand": {
                        "present": True,
                        "strength": "moderate",
                        "evidence": ["dc"],
                    },
                    "supply_constraints": {
                        "present": True,
                        "strength": "high",
                        "evidence": ["supply"],
                    },
                    "pricing_power": {
                        "present": True,
                        "strength": "high",
                        "evidence": ["price"],
                    },
                    "guidance_up": {
                        "present": True,
                        "strength": "raised",
                        "evidence": ["guidance"],
                    },
                },
            },
            facts(
                revenue=100,
                operating_cash_flow=30,
                capex=20,
                gross_margin=50,
                inventory=10,
                backlog=30,
            ),
        )
        self.assertGreaterEqual(result["score"], 8)
        self.assertEqual(result["signals"]["data_centre_demand"]["score"], 1)

    def test_rising_capex_and_raised_guidance_are_leading_signals(self):
        current = {
            **facts(revenue=115, operating_cash_flow=45, capex=20),
            "qualitative": {
                "guidance_up": {
                    "present": True,
                    "strength": "raised",
                    "evidence": ["raised guidance"],
                }
            },
        }
        prior = facts(revenue=100, operating_cash_flow=40, capex=15)
        result = build_deterministic_analysis(current, prior)
        self.assertEqual(result["signals"]["capex"]["score"], 2)
        self.assertEqual(result["signals"]["guidance_direction"]["score"], 2)
        self.assertIn("capital investment", result["drivers"])
        self.assertIn("guidance", result["drivers"])

    def test_rate_inputs_accept_percentage_points(self):
        current = facts(
            revenue=200,
            operating_cash_flow=80,
            capex=20,
            free_cash_flow=60,
            net_debt=10,
            shares_outstanding=10,
        )
        result = build_deterministic_analysis(
            current,
            facts(revenue=100, free_cash_flow=50),
            {"discount_rate": 10.0, "terminal_growth": 3.0},
        )
        assumptions = result["valuation"]["assumptions"]
        self.assertEqual(assumptions["discount_rate"], 0.10)
        self.assertEqual(assumptions["terminal_growth"], 0.03)

    def test_market_overrides_are_visible_and_keep_per_share_units(self):
        current = facts(
            revenue=200,
            operating_cash_flow=80,
            capex=20,
            free_cash_flow=60,
            net_debt=10,
            shares_outstanding=10,
        )
        result = build_deterministic_analysis(
            current,
            facts(revenue=100, free_cash_flow=50),
            {
                "market_price": 150.0,
                "shares_outstanding": 12.0,
                "net_debt": 8.0,
                "discount_rate": 0.10,
                "terminal_growth": 0.03,
            },
        )
        self.assertEqual(result["metrics"]["market_price"]["value"], 150.0)
        self.assertEqual(result["metrics"]["shares_outstanding"]["value"], 12.0)
        self.assertEqual(result["metrics"]["net_debt"]["value"], 8.0)
        self.assertEqual(result["valuation"]["market_price"], 150.0)
        self.assertAlmostEqual(result["valuation"]["market_cap"], 1800.0, places=6)

    def test_mature_requires_repeated_evidence_valuation_and_news(self):
        current = {
            **facts(
                revenue=120,
                operating_cash_flow=50,
                capex=30,
                gross_margin=55,
                inventory=10,
                backlog=40,
                diluted_eps=4.0,
            ),
            "qualitative": {
                "ai_demand": {"present": True, "strength": "strong", "evidence": ["e"]},
                "guidance_up": {"present": True, "strength": "raised", "evidence": ["e"]},
            },
        }
        prior = facts(
            revenue=100,
            operating_cash_flow=30,
            capex=20,
            gross_margin=50,
            inventory=10,
            backlog=30,
            diluted_eps=3.0,
        )
        news_items = ["news 1", "news 2", "news 3"]
        market_inputs = {"market_price": 120.0}  # P/E = 30 >= 25

        mature = build_deterministic_analysis(
            current,
            prior,
            market_inputs=market_inputs,
            previous_state="confirmed",
            prior_analysis_count=2,
            news_items=news_items,
        )
        self.assertEqual(mature["state"]["stage"], "mature")


class CanonicalCapexAliasTests(unittest.TestCase):
    def test_cash_paid_for_property_and_equipment_is_canonical_capex(self):
        result = build_deterministic_analysis(
            _facts(
                {
                    "revenue": _metric(64.727, "usd_billions"),
                    "operating_cash_flow": _metric(37.2, "usd_billions"),
                    "cash_paid_for_property_and_equipment": _metric(
                        13.9, "usd_billions"
                    ),
                }
            )
        )
        self.assertAlmostEqual(result["metrics"]["capex"]["value"], 13.9, places=9)
        self.assertAlmostEqual(result["metrics"]["fcf"]["value"], 23.3, places=9)

    def test_gross_margin_dollars_alias_feeds_margin_signal(self):
        current = _facts(
            {
                "revenue": _metric(64.727, "usd_billions"),
                "operating_cash_flow": _metric(37.2, "usd_billions"),
                "cash_paid_for_property_and_equipment": _metric(13.9, "usd_billions"),
                "gross_margin_dollars": _metric(45.043, "usd_billions"),
            }
        )
        result = build_deterministic_analysis(current)
        gross_profit = result["metrics"].get("gross_profit")
        self.assertIsNotNone(gross_profit)
        self.assertAlmostEqual(gross_profit["value"], 45.043, places=9)


class SupplementalMetricSurvivalTests(unittest.TestCase):
    def test_supplemental_metrics_pass_through_with_provenance(self):
        current = _facts(
            {
                "revenue": _metric(64_727.0, "usd_millions"),
                "microsoft_cloud_revenue": _metric(
                    36_800.0,
                    "usd_millions",
                    period="FY2024-Q4",
                    currency="USD",
                ),
            }
        )
        current["metrics"]["microsoft_cloud_revenue"].update(
            {"source": "transcript", "concept": "msft:cloud_revenue"}
        )
        result = build_deterministic_analysis(current)
        cloud = result["metrics"]["microsoft_cloud_revenue"]
        self.assertAlmostEqual(cloud["value"], 36_800.0, places=6)
        self.assertEqual(cloud["unit"], "usd_millions")
        self.assertEqual(cloud["period"], "FY2024-Q4")
        self.assertEqual(cloud["source"], "transcript")
        self.assertEqual(cloud["concept"], "msft:cloud_revenue")


class CoverageGatedStateTests(unittest.TestCase):
    def test_sparse_qualitative_positives_cannot_reach_confirmed(self):
        current = _facts(
            {
                "operating_cash_flow": _metric(37.2, "usd_billions"),
                "capex": _metric(13.9, "usd_billions"),
            },
            qualitative={
                "ai_demand": {
                    "present": True,
                    "strength": "strong",
                    "evidence": ["demand exceeded available capacity"],
                },
            },
        )
        result = build_deterministic_analysis(current)
        self.assertNotEqual(result["state"]["stage"], "confirmed")
        self.assertNotEqual(result["state"]["stage"], "accelerating")
        coverage = result["state"]["coverage"]
        self.assertFalse(coverage["eligible_for_high_states"])


class MaterialRelationshipContractTests(unittest.TestCase):
    def test_contract_builds_and_serializes_relationships(self):
        current = {
            "metrics": {
                "revenue": {"value": 100.0, "unit": "USDm", "period": "FY2025"},
                "revenue_growth": {"value": 10.0, "unit": "percent", "period": "FY2025"},
                "net_income": {"value": 20.0, "unit": "USDm", "period": "FY2025"},
                "net_income_growth": {"value": 15.0, "unit": "percent", "period": "FY2025"},
                "operating_cash_flow": {"value": 30.0, "unit": "USDm", "period": "FY2025"},
                "capex": {"value": 10.0, "unit": "USDm", "period": "FY2025"},
            }
        }
        prior = {
            "metrics": {
                "revenue": {"value": 90.0, "unit": "USDm", "period": "FY2024"},
                "net_income": {"value": 17.4, "unit": "USDm", "period": "FY2024"},
            }
        }
        contract = build_material_relationship_contract(current, prior)
        payload = contract.to_payload()

        self.assertIn("relationship_facts", payload)
        self.assertIn("material_relationships", payload)
        kinds = [r["kind"] for r in payload["material_relationships"]]
        self.assertIn("same_period_top_bottom_growth", kinds)
        self.assertIn("cash_generation_vs_investment", kinds)
        self.assertTrue(len(payload["relationship_facts"]) >= 2)


if __name__ == "__main__":
    unittest.main()
