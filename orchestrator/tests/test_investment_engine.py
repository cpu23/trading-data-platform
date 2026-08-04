import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from investment_engine import build_deterministic_analysis


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
            net_debt=10,
            shares_outstanding=10,
        )
        previous = facts(revenue=100, operating_cash_flow=100, capex=0)
        result = build_deterministic_analysis(
            current, previous, {"discount_rate": 0.10, "terminal_growth": 0.03}
        )
        dcf = result["valuation"]["dcf"]
        self.assertEqual(len(dcf["forecast"]), 5)
        self.assertEqual(dcf["assumptions"]["inferred_growth"], 0.20)
        self.assertTrue(math.isfinite(dcf["per_share"]))

    def test_pe_uses_eps_then_market_cap_net_income_fallback(self):
        direct = build_deterministic_analysis(facts(market_price=100, diluted_eps=5))
        self.assertEqual(direct["valuation"]["pe"], 20.0)
        fallback = build_deterministic_analysis(
            facts(market_price=100, shares_outstanding=10, diluted_eps=0, net_income=50)
        )
        self.assertEqual(fallback["valuation"]["pe"], 20.0)
        self.assertEqual(fallback["valuation"]["pe_method"], "market_cap_net_income")

    def test_zero_and_malformed_values_remain_missing(self):
        result = build_deterministic_analysis(
            facts(
                revenue=0,
                operating_cash_flow="nan",
                capex=10,
                diluted_eps=0,
                shares_outstanding=0,
            )
        )
        self.assertIsNone(result["metrics"]["fcf_margin"]["value"])
        self.assertIsNone(result["valuation"]["pe"])
        self.assertIsNone(result["valuation"]["dcf_per_share"])

    def test_threshold_boundaries_are_explicit(self):
        prior = facts(revenue=100, operating_cash_flow=10, capex=1)
        at_ten = build_deterministic_analysis(
            facts(revenue=110, operating_cash_flow=11, capex=1), prior
        )
        just_under = build_deterministic_analysis(
            facts(revenue=109.99, operating_cash_flow=11, capex=1), prior
        )
        self.assertEqual(at_ten["signals"]["revenue"]["score"], 2)
        self.assertEqual(just_under["signals"]["revenue"]["score"], 1)
        signal = at_ten["signals"]["revenue"]
        self.assertEqual(signal["change"], 10.0)
        self.assertEqual(signal["change_pct"], 10.0)
        self.assertEqual(signal["basis"], "deterministic_metric")
        self.assertTrue(signal["comparable"])

    def test_inventory_growth_above_revenue_is_a_risk(self):
        result = build_deterministic_analysis(
            facts(revenue=110, inventory=130), facts(revenue=100, inventory=100)
        )
        signal = result["signals"]["inventory_vs_revenue"]
        self.assertEqual(signal["score"], -2)
        self.assertEqual(signal["prior_value"], 100.0)
        self.assertIn("inventory relative to revenue", result["risks"])

    def test_dram_leading_signals_compound(self):
        result = build_deterministic_analysis(
            {
                **facts(
                    revenue=120,
                    operating_cash_flow=60,
                    capex=10,
                    backlog=150,
                    inventory=105,
                    gross_margin=55,
                ),
                "qualitative": {
                    "ai_demand": {
                        "present": True,
                        "strength": "strong",
                        "evidence": ["ai"],
                    },
                    "data_centre_demand": {
                        "present": True,
                        "strength": "strong",
                        "evidence": ["dc"],
                    },
                    "pricing_power": {
                        "present": True,
                        "strength": "strong",
                        "evidence": ["price"],
                    },
                },
            },
            facts(
                revenue=100,
                operating_cash_flow=40,
                capex=10,
                backlog=100,
                inventory=100,
                gross_margin=50,
            ),
        )
        self.assertGreaterEqual(result["score"], 8)
        self.assertEqual(result["state"]["stage"], "accelerating")
        self.assertEqual(result["signals"]["data_centre_demand"]["score"], 1)
        qualitative_signal = result["signals"]["data_centre_demand"]
        self.assertEqual(qualitative_signal["basis"], "report_qualitative")
        self.assertFalse(qualitative_signal["comparable"])
        self.assertIsNone(qualitative_signal["change_pct"])

    def test_rising_capex_and_raised_guidance_are_leading_signals(self):
        current = {
            **facts(revenue=115, operating_cash_flow=45, capex=20),
            "qualitative": {
                "guidance_up": {
                    "present": True,
                    "strength": "strong",
                    "evidence": ["raised"],
                },
                "guidance_down": {"present": False, "strength": "none", "evidence": []},
            },
        }
        result = build_deterministic_analysis(
            current,
            facts(revenue=100, operating_cash_flow=40, capex=10),
        )
        self.assertEqual(result["signals"]["capex"]["score"], 2)
        self.assertEqual(result["signals"]["guidance_direction"]["score"], 2)

    def test_rate_inputs_accept_percentage_points(self):
        current = facts(
            revenue=200,
            operating_cash_flow=80,
            capex=20,
            net_debt=10,
            shares_outstanding=10,
        )
        previous = facts(revenue=180, operating_cash_flow=70, capex=20)
        result = build_deterministic_analysis(
            current,
            previous,
            {"discount_rate": 10, "terminal_growth": 3},
        )
        assumptions = result["valuation"]["dcf"]["assumptions"]
        self.assertEqual(assumptions["wacc"], 0.10)
        self.assertEqual(assumptions["terminal_growth"], 0.03)

    def test_market_overrides_are_visible_and_keep_per_share_units(self):
        current = facts(
            revenue=200,
            operating_cash_flow=80,
            capex=20,
            diluted_eps=2,
        )
        previous = facts(revenue=180, operating_cash_flow=70, capex=20)
        for item in current["metrics"].values():
            item["unit"] = "USDm"
        result = build_deterministic_analysis(
            current,
            previous,
            {
                "market_price": 30,
                "shares_outstanding": 10,
                "net_debt": 0,
            },
        )
        self.assertEqual(result["metrics"]["market_price"]["value"], 30.0)
        self.assertEqual(
            result["metrics"]["market_price"]["evidence"],
            "manual valuation override",
        )
        self.assertEqual(
            result["metrics"]["market_price"]["source"],
            "manual_input",
        )
        self.assertEqual(result["valuation"]["pe"], 15.0)
        self.assertEqual(result["valuation"]["per_share_unit"], "USD/share")

    def test_mature_requires_repeated_evidence_valuation_and_news(self):
        current = {
            **facts(
                revenue=120,
                operating_cash_flow=60,
                capex=10,
                backlog=150,
                gross_margin=55,
                market_price=100,
                diluted_eps=2,
                shares_outstanding=10,
                net_debt=0,
            ),
            "qualitative": {
                "ai_demand": {"present": True, "strength": "strong"},
                "pricing_power": {"present": True, "strength": "strong"},
            },
        }
        previous = facts(
            revenue=100, operating_cash_flow=40, capex=10, backlog=100, gross_margin=50
        )
        result = build_deterministic_analysis(
            current,
            previous,
            previous_state="accelerating",
            prior_analysis_count=2,
            news_items=[1, 2, 3],
        )
        self.assertEqual(result["state"]["stage"], "mature")
        not_repeated = build_deterministic_analysis(
            current,
            previous,
            previous_state="accelerating",
            prior_analysis_count=1,
            news_items=[1, 2, 3],
        )
        self.assertNotEqual(not_repeated["state"]["stage"], "mature")


if __name__ == "__main__":
    unittest.main()
