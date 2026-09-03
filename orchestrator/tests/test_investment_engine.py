import copy
from dataclasses import FrozenInstanceError
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
        self.assertEqual(sensitivity["wacc_terminal_grid"], [])
        self.assertIsNone(sensitivity["range"]["enterprise_value_min"])

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


def _metric(
    value,
    unit="usd_billions",
    period="FY2024-Q4",
    currency=None,
):
    record = {"value": value, "unit": unit, "period": period, "evidence": ["x"]}
    if currency is not None:
        record["currency"] = currency
    return record


def _facts(metrics, qualitative=None, prior=None):
    payload = {"metrics": metrics, "qualitative": qualitative or {}}
    if prior is not None:
        payload["prior_metrics"] = prior
    return payload


# One economically identical quarter expressed at three unit scales. Every
# ratio the engine derives must be identical across scales; only absolute
# levels may carry the scale label.
_SCALE_CASES = (
    ("billions", "usd_billions", 37.2, 13.9),
    ("millions", "usd_millions", 37_200.0, 13_900.0),
    ("thousands", "usd_thousands", 37_200_000.0, 13_900_000.0),
)


class MonetaryUnitCompatibilityTests(unittest.TestCase):
    """Monetary arithmetic requires compatible currency, unit scale,
    definition, and period; incompatible operands yield unknown."""

    def test_derived_ratios_are_invariant_across_usd_unit_scales(self):
        baseline = None
        for label, unit, ocf, capex in _SCALE_CASES:
            with self.subTest(scale=label):
                current = _facts(
                    {
                        "revenue": _metric(64.727 * (10 ** _scale_power(unit)), unit),
                        "operating_cash_flow": _metric(ocf, unit),
                        "capex": _metric(capex, unit),
                        "net_income": _metric(22.036 * (10 ** _scale_power(unit)), unit),
                        "equity": _metric(200.0 * (10 ** _scale_power(unit)), unit),
                    }
                )
                result = build_deterministic_analysis(current)
                observed = {
                    "fcf_margin": result["metrics"]["fcf_margin"]["value"],
                    "cash_conversion": result["fundamentals"][
                        "operating_cash_conversion"
                    ],
                    "capex_intensity": result["fundamentals"][
                        "capex_to_revenue_pct"
                    ],
                    "net_margin": result["fundamentals"]["net_margin_pct"],
                }
                for name in (
                    "fcf_margin",
                    "cash_conversion",
                    "capex_intensity",
                    "net_margin",
                ):
                    self.assertIsNotNone(
                        observed[name], f"{name} must exist at scale {label}"
                    )
                if baseline is None:
                    baseline = dict(observed)
                for name, expected in baseline.items():
                    self.assertAlmostEqual(
                        observed[name],
                        expected,
                        places=6,
                        msg=f"{name} must be scale-invariant ({label})",
                    )
                self.assertGreater(result["metrics"]["fcf"]["value"], 0.0)
                self.assertAlmostEqual(
                    result["metrics"]["fcf"]["value"] / (10 ** _scale_power(unit)),
                    23.3,
                    places=4,
                    msg="derived FCF must normalize to the billions scale",
                )

    def test_scale_mismatch_blocks_fcf_arithmetic_but_not_independent_ratios(self):
        # Mismatched-scale OCF/capex may not produce an FCF number (pre-fix
        # this also yielded a ~0.001688 "cash conversion" by subtracting
        # usd_billions capex from usd_millions OCF), yet independently
        # compatible same-record ratios stay valid: OCF and net income share
        # one scale, so their cash conversion is exactly 37200 / 22036.
        current = _facts(
            {
                "revenue": _metric(64_727.0, "usd_millions"),
                "net_income": _metric(22_036.0, "usd_millions"),
                "operating_cash_flow": _metric(37_200.0, "usd_millions"),
                "capex": _metric(13.9, "usd_billions"),
            }
        )
        result = build_deterministic_analysis(current)
        self.assertIsNone(result["metrics"]["fcf"]["value"])
        self.assertIsNone(result["metrics"]["fcf_margin"]["value"])
        self.assertAlmostEqual(
            result["fundamentals"]["operating_cash_conversion"],
            37_200.0 / 22_036.0,
            places=9,
        )

    def test_currency_and_period_and_unknown_unit_mismatches_yield_unknown(self):
        cases = (
            (
                "currency mismatch",
                _metric(80.0, "EUR", currency="EUR"),
                _metric(20.0, "USD", currency="USD"),
            ),
            (
                "unknown unit vs known currency",
                _metric(80.0, "widgets"),
                _metric(20.0, "USD"),
            ),
            (
                "unknown-currency millions vs usd millions",
                _metric(80.0, "report_millions"),
                _metric(20.0, "usd_millions"),
            ),
            (
                "untyped legacy vs typed units",
                _metric(80.0),
                _metric(20.0, "usd_millions"),
            ),
        )
        for label, ocf, capex in cases:
            with self.subTest(case=label):
                result = build_deterministic_analysis(
                    _facts({"operating_cash_flow": ocf, "capex": capex})
                )
                self.assertIsNone(result["metrics"]["fcf"]["value"])
                self.assertIsNone(result["metrics"]["fcf_margin"]["value"])
                self.assertIsNone(result["valuation"]["dcf"]["per_share"])

    def test_adjacent_fiscal_year_same_quarter_normalizes_prior_scale(self):
        current = _facts(
            {
                "revenue": _metric(
                    120.0, "usd_billions", period="FY2025-Q2"
                ),
            }
        )
        prior = _facts(
            {
                "revenue": _metric(
                    100_000.0, "usd_millions", period="FY2024-Q2"
                ),
            }
        )

        result = build_deterministic_analysis(current, previous_facts=prior)
        revenue = result["metrics"]["revenue"]
        signal = result["signals"]["revenue"]

        self.assertEqual(revenue["prior_value"], 100.0)
        self.assertEqual(revenue["change"], 20.0)
        self.assertEqual(revenue["change_pct"], 20.0)
        self.assertEqual(signal["observed_value"], 120.0)
        self.assertEqual(signal["prior_value"], 100.0)
        self.assertTrue(signal["comparable"])
        self.assertEqual(signal["score"], 2)

    def test_q4_and_prior_annual_periods_are_not_comparable(self):
        current = _facts(
            {
                "revenue": _metric(
                    120.0, "usd_billions", period="FY2025-Q4"
                ),
            }
        )
        prior = _facts(
            {
                "revenue": _metric(
                    100.0, "usd_billions", period="FY2024-ANNUAL"
                ),
            }
        )

        result = build_deterministic_analysis(current, previous_facts=prior)
        revenue = result["metrics"]["revenue"]
        signal = result["signals"]["revenue"]

        self.assertIsNone(revenue["prior_value"])
        self.assertIsNone(revenue["change"])
        self.assertIsNone(revenue["change_pct"])
        self.assertIsNone(signal["prior_value"])
        self.assertFalse(signal["comparable"])
        self.assertEqual(signal["score"], 0)

    def test_fiscal_and_calendar_periods_are_not_comparable(self):
        current = _facts(
            {
                "revenue": _metric(
                    120.0, "usd_billions", period="FY2025-Q2"
                ),
            }
        )
        prior = _facts(
            {
                "revenue": _metric(
                    100.0, "usd_billions", period="CY2024-Q2"
                ),
            }
        )

        result = build_deterministic_analysis(current, previous_facts=prior)
        revenue = result["metrics"]["revenue"]
        signal = result["signals"]["revenue"]

        self.assertIsNone(revenue["prior_value"])
        self.assertIsNone(revenue["change"])
        self.assertIsNone(revenue["change_pct"])
        self.assertIsNone(signal["prior_value"])
        self.assertFalse(signal["comparable"])
        self.assertEqual(signal["score"], 0)

    def test_same_period_fcf_arithmetic_is_unchanged(self):
        current = _facts(
            {
                "revenue": _metric(
                    120.0, "usd_billions", period="FY2025-Q2"
                ),
                "operating_cash_flow": _metric(
                    80.0, "usd_billions", period="FY2025-Q2"
                ),
                "capex": _metric(
                    20.0, "usd_billions", period="FY2025-Q2"
                ),
            }
        )
        prior = _facts(
            {
                "revenue": _metric(
                    100.0, "usd_billions", period="FY2024-Q2"
                ),
                "operating_cash_flow": _metric(
                    70.0, "usd_billions", period="FY2024-Q2"
                ),
                "capex": _metric(
                    20.0, "usd_billions", period="FY2024-Q2"
                ),
            }
        )

        result = build_deterministic_analysis(current, previous_facts=prior)
        fcf = result["metrics"]["fcf"]
        margin = result["metrics"]["fcf_margin"]

        self.assertEqual(fcf["value"], 60.0)
        self.assertEqual(fcf["prior_value"], 50.0)
        self.assertEqual(fcf["change"], 10.0)
        self.assertEqual(fcf["change_pct"], 20.0)
        self.assertEqual(margin["value"], 50.0)
        self.assertEqual(margin["prior_value"], 50.0)
        self.assertEqual(fcf["source"], "derived")

    def test_compatible_operands_still_derive_fcf(self):
        result = build_deterministic_analysis(
            _facts(
                {
                    "revenue": _metric(64_727.0, "usd_millions"),
                    "operating_cash_flow": _metric(37_200.0, "usd_millions"),
                    "capex": _metric(13_900.0, "usd_millions"),
                }
            )
        )
        self.assertAlmostEqual(result["metrics"]["fcf"]["value"], 23_300.0, places=6)
        self.assertEqual(result["metrics"]["fcf"]["source"], "derived")
        self.assertAlmostEqual(
            result["metrics"]["fcf_margin"]["value"],
            23_300.0 / 64_727.0 * 100.0,
            places=9,
        )


def _scale_power(unit):
    """Exponent lifting a billions-denominated base into ``unit``'s scale."""
    if "thousands" in unit:
        return 6
    if "millions" in unit:
        return 3
    return 0


class DcfPeriodEligibilityTests(unittest.TestCase):
    _NONANNUAL_REASON = (
        "starting FCF must be annual, TTM, LTM, or 12-month"
    )
    _NO_GROWTH_REASON = "comparable annual FCF growth unavailable"

    def assert_dcf_unavailable(
        self,
        result,
        reason,
        *,
        starting_fcf,
        starting_fcf_period,
        starting_fcf_basis=None,
    ):
        dcf = result["valuation"]["dcf"]
        assumptions = dcf["assumptions"]
        sensitivity = dcf["sensitivity"]

        self.assertEqual(dcf["status"], "unavailable")
        self.assertEqual(dcf["reason"], reason)
        self.assertEqual(assumptions["starting_fcf"], starting_fcf)
        self.assertEqual(
            assumptions["starting_fcf_period"], starting_fcf_period
        )
        self.assertEqual(
            assumptions["starting_fcf_basis"], starting_fcf_basis
        )
        self.assertIsNone(assumptions["growth_basis"])
        self.assertIsNone(assumptions["inferred_growth"])
        self.assertEqual(dcf["forecast"], [])
        self.assertIsNone(dcf["terminal_value"])
        self.assertIsNone(dcf["present_value_of_terminal"])
        self.assertIsNone(dcf["enterprise_value"])
        self.assertIsNone(dcf["equity_value"])
        self.assertIsNone(dcf["per_share"])
        self.assertEqual(sensitivity["status"], "unavailable")
        self.assertEqual(sensitivity["reason"], reason)
        self.assertEqual(sensitivity["wacc_terminal_grid"], [])
        self.assertEqual(sensitivity["drivers"], {})
        self.assertEqual(
            sensitivity["range"],
            {
                "enterprise_value_min": None,
                "enterprise_value_max": None,
                "per_share_min": None,
                "per_share_max": None,
            },
        )
        self.assertIsNone(sensitivity["largest_range_driver"])

    def test_quarterly_fcf_does_not_borrow_annual_revenue_growth(self):
        current = _facts(
            {
                "free_cash_flow": _metric(
                    60.0, "usd_billions", period="FY2025-Q2"
                ),
                "revenue": _metric(
                    120.0, "usd_billions", period="FY2025"
                ),
            }
        )
        prior = _facts(
            {
                "revenue": _metric(
                    100.0, "usd_billions", period="FY2024"
                ),
            }
        )

        result = build_deterministic_analysis(
            current, previous_facts=prior
        )

        self.assertEqual(result["signals"]["revenue"]["change_pct"], 20.0)
        self.assertEqual(result["metrics"]["fcf"]["value"], 60.0)
        self.assertEqual(result["metrics"]["fcf"]["period"], "FY2025-Q2")
        self.assert_dcf_unavailable(
            result,
            self._NONANNUAL_REASON,
            starting_fcf=None,
            starting_fcf_period="FY2025-Q2",
        )

    def test_current_and_prior_quarterly_fcf_still_do_not_enable_dcf(self):
        current = _facts(
            {
                "free_cash_flow": _metric(
                    60.0, "usd_billions", period="FY2025-Q2"
                ),
            }
        )
        prior = _facts(
            {
                "free_cash_flow": _metric(
                    50.0, "usd_billions", period="FY2024-Q2"
                ),
            }
        )

        result = build_deterministic_analysis(
            current, previous_facts=prior
        )

        fcf = result["metrics"]["fcf"]
        self.assertEqual(fcf["value"], 60.0)
        self.assertEqual(fcf["period"], "FY2025-Q2")
        self.assertEqual(fcf["prior_value"], 50.0)
        self.assertEqual(fcf["change_pct"], 20.0)
        self.assert_dcf_unavailable(
            result,
            self._NONANNUAL_REASON,
            starting_fcf=None,
            starting_fcf_period="FY2025-Q2",
        )

    def test_annual_and_ttm_current_prior_fcf_enable_dcf(self):
        cases = (
            ("annual", "FY2025", "FY2024", "annual", "annual_fcf"),
            (
                "trailing twelve months",
                "TTM-2025",
                "TTM-2024",
                "ttm",
                "ttm_fcf",
            ),
        )
        for label, current_period, prior_period, basis, growth_basis in cases:
            with self.subTest(case=label):
                current = _facts(
                    {
                        "free_cash_flow": _metric(
                            60.0,
                            "usd_billions",
                            period=current_period,
                        ),
                        "net_debt": _metric(
                            10.0,
                            "usd_billions",
                            period=current_period,
                        ),
                        "shares_outstanding": _metric(
                            10.0,
                            "shares_billions",
                            period=current_period,
                        ),
                    }
                )
                prior = _facts(
                    {
                        "free_cash_flow": _metric(
                            50.0,
                            "usd_billions",
                            period=prior_period,
                        ),
                    }
                )

                result = build_deterministic_analysis(
                    current, previous_facts=prior
                )
                dcf = result["valuation"]["dcf"]

                self.assertEqual(result["metrics"]["fcf"]["value"], 60.0)
                self.assertEqual(
                    result["metrics"]["fcf"]["period"], current_period
                )
                self.assertEqual(dcf["status"], "calculated")
                self.assertIsNone(dcf["reason"])
                self.assertEqual(
                    dcf["assumptions"]["starting_fcf"], 60.0
                )
                self.assertEqual(
                    dcf["assumptions"]["starting_fcf_period"],
                    current_period,
                )
                self.assertEqual(
                    dcf["assumptions"]["starting_fcf_basis"], basis
                )
                self.assertEqual(
                    dcf["assumptions"]["inferred_growth"], 0.20
                )
                self.assertEqual(
                    dcf["assumptions"]["growth_basis"], growth_basis
                )
                self.assertEqual(len(dcf["forecast"]), 5)
                self.assertEqual(dcf["forecast"][0]["fcf"], 72.0)
                self.assertIsNotNone(dcf["enterprise_value"])
                self.assertIsNotNone(dcf["per_share"])
                self.assertEqual(
                    dcf["sensitivity"]["status"], "calculated"
                )
                self.assertTrue(
                    dcf["sensitivity"]["wacc_terminal_grid"]
                )

    def test_annual_fcf_with_revenue_growth_only_abstains(self):
        current = _facts(
            {
                "free_cash_flow": _metric(
                    60.0, "usd_billions", period="FY2025"
                ),
                "revenue": _metric(
                    120.0, "usd_billions", period="FY2025"
                ),
            }
        )
        prior = _facts(
            {
                "revenue": _metric(
                    100.0, "usd_billions", period="FY2024"
                ),
            }
        )

        result = build_deterministic_analysis(
            current, previous_facts=prior
        )

        self.assertEqual(result["signals"]["revenue"]["change_pct"], 20.0)
        self.assertEqual(result["metrics"]["fcf"]["value"], 60.0)
        self.assertEqual(result["metrics"]["fcf"]["period"], "FY2025")
        self.assert_dcf_unavailable(
            result,
            self._NO_GROWTH_REASON,
            starting_fcf=60.0,
            starting_fcf_period="FY2025",
            starting_fcf_basis="annual",
        )

    def test_missing_or_unparseable_fcf_period_abstains(self):
        for label, period in (
            ("missing", None),
            ("unparseable", "current period"),
        ):
            with self.subTest(case=label):
                result = build_deterministic_analysis(
                    _facts(
                        {
                            "free_cash_flow": _metric(
                                60.0,
                                "usd_billions",
                                period=period,
                            ),
                        }
                    )
                )

                self.assertEqual(result["metrics"]["fcf"]["value"], 60.0)
                self.assertEqual(result["metrics"]["fcf"]["period"], period)
                self.assert_dcf_unavailable(
                    result,
                    self._NONANNUAL_REASON,
                    starting_fcf=None,
                    starting_fcf_period=period,
                )


class PriorFcfAlignmentTests(unittest.TestCase):
    def test_compatible_annual_prior_scale_aligns_growth_and_forecast(self):
        current = _facts(
            {
                "free_cash_flow": _metric(
                    60.0, "usd_billions", period="FY2025"
                ),
            }
        )
        prior = _facts(
            {
                "free_cash_flow": _metric(
                    50_000.0, "usd_millions", period="FY2024"
                ),
            }
        )

        result = build_deterministic_analysis(current, previous_facts=prior)
        fcf = result["metrics"]["fcf"]
        signal = result["signals"]["fcf"]
        dcf = result["valuation"]["dcf"]

        self.assertEqual(fcf["value"], 60.0)
        self.assertEqual(fcf["unit"], "usd_billions")
        self.assertEqual(fcf["period"], "FY2025")
        self.assertEqual(fcf["prior_value"], 50.0)
        self.assertEqual(fcf["change"], 10.0)
        self.assertEqual(fcf["change_pct"], 20.0)
        self.assertEqual(signal["observed_value"], 60.0)
        self.assertEqual(signal["prior_value"], 50.0)
        self.assertEqual(signal["change"], 10.0)
        self.assertEqual(signal["change_pct"], 20.0)
        self.assertTrue(signal["comparable"])
        self.assertEqual(signal["score"], 2)
        self.assertEqual(dcf["assumptions"]["starting_fcf"], 60.0)
        self.assertEqual(dcf["assumptions"]["inferred_growth"], 0.20)
        self.assertEqual(dcf["forecast"][0]["fcf"], 72.0)

    def test_incompatible_prior_fcf_does_not_contribute_growth(self):
        cases = (
            (
                "quarter versus annual",
                _metric(60.0, "usd_billions", period="FY2025-Q4"),
                _metric(50.0, "usd_billions", period="FY2024-ANNUAL"),
                None,
                "starting FCF must be annual, TTM, LTM, or 12-month",
            ),
            (
                "currency mismatch",
                _metric(
                    60.0,
                    "usd_billions",
                    period="FY2025",
                    currency="USD",
                ),
                _metric(
                    50.0,
                    "eur_billions",
                    period="FY2024",
                    currency="EUR",
                ),
                60.0,
                "comparable annual FCF growth unavailable",
            ),
        )
        for label, current_fcf, prior_fcf, starting_fcf, dcf_reason in cases:
            with self.subTest(case=label):
                result = build_deterministic_analysis(
                    _facts({"free_cash_flow": current_fcf}),
                    previous_facts=_facts({"free_cash_flow": prior_fcf}),
                )
                fcf = result["metrics"]["fcf"]
                signal = result["signals"]["fcf"]
                dcf = result["valuation"]["dcf"]

                self.assertEqual(fcf["value"], 60.0)
                self.assertIsNone(fcf["prior_value"])
                self.assertIsNone(fcf["change"])
                self.assertIsNone(fcf["change_pct"])
                self.assertEqual(signal["observed_value"], 60.0)
                self.assertIsNone(signal["prior_value"])
                self.assertIsNone(signal["change"])
                self.assertIsNone(signal["change_pct"])
                self.assertFalse(signal["comparable"])
                self.assertEqual(signal["score"], 0)
                self.assertEqual(
                    dcf["assumptions"]["starting_fcf"], starting_fcf
                )
                self.assertIsNone(dcf["assumptions"]["inferred_growth"])
                self.assertEqual(dcf["status"], "unavailable")
                self.assertEqual(dcf["reason"], dcf_reason)
                self.assertEqual(dcf["forecast"], [])

    def test_explicit_and_derived_fcf_require_the_same_declared_basis(self):
        prior = _facts(
            {
                "operating_cash_flow": _metric(
                    70.0, "usd_billions", period="FY2024"
                ),
                "capex": _metric(
                    20.0, "usd_billions", period="FY2024"
                ),
            }
        )
        undeclared_current = _metric(
            60.0, "usd_billions", period="FY2025"
        )
        declared_current = {
            **undeclared_current,
            "calculation": "operating_cash_flow - capex",
        }

        undeclared = build_deterministic_analysis(
            _facts({"free_cash_flow": undeclared_current}),
            previous_facts=prior,
        )
        declared = build_deterministic_analysis(
            _facts({"free_cash_flow": declared_current}),
            previous_facts=prior,
        )

        self.assertIsNone(undeclared["metrics"]["fcf"]["prior_value"])
        self.assertFalse(undeclared["signals"]["fcf"]["comparable"])
        self.assertEqual(undeclared["signals"]["fcf"]["score"], 0)
        self.assertIsNone(
            undeclared["valuation"]["dcf"]["assumptions"]["inferred_growth"]
        )
        self.assertEqual(undeclared["valuation"]["dcf"]["forecast"], [])

        self.assertEqual(declared["metrics"]["fcf"]["prior_value"], 50.0)
        self.assertTrue(declared["signals"]["fcf"]["comparable"])
        self.assertEqual(declared["signals"]["fcf"]["change_pct"], 20.0)
        self.assertEqual(
            declared["valuation"]["dcf"]["assumptions"]["inferred_growth"],
            0.20,
        )

    def test_producer_derived_cash_capex_fcf_aligns_with_prior_fallback(self):
        current = _facts(
            {
                "free_cash_flow": {
                    **_metric(
                        60.0, "usd_billions", period="FY2025"
                    ),
                    "source": "derived",
                    "concept": (
                        "derived: operating_cash_flow - "
                        "cash_paid_for_property_and_equipment"
                    ),
                },
            }
        )
        prior = _facts(
            {
                "operating_cash_flow": _metric(
                    70_000.0, "usd_millions", period="FY2024"
                ),
                "cash_paid_for_property_and_equipment": _metric(
                    20_000.0, "usd_millions", period="FY2024"
                ),
            }
        )

        result = build_deterministic_analysis(current, previous_facts=prior)
        fcf = result["metrics"]["fcf"]
        signal = result["signals"]["fcf"]
        dcf = result["valuation"]["dcf"]

        self.assertEqual(fcf["value"], 60.0)
        self.assertEqual(fcf["prior_value"], 50.0)
        self.assertEqual(fcf["change"], 10.0)
        self.assertEqual(fcf["change_pct"], 20.0)
        self.assertEqual(signal["observed_value"], 60.0)
        self.assertEqual(signal["prior_value"], 50.0)
        self.assertEqual(signal["change"], 10.0)
        self.assertEqual(signal["change_pct"], 20.0)
        self.assertTrue(signal["comparable"])
        self.assertEqual(signal["score"], 2)
        self.assertEqual(dcf["assumptions"]["starting_fcf"], 60.0)
        self.assertEqual(dcf["assumptions"]["inferred_growth"], 0.20)
        self.assertEqual(dcf["forecast"][0]["fcf"], 72.0)

    def test_near_name_and_wrong_operand_derived_concepts_stay_unknown(self):
        prior = _facts(
            {
                "operating_cash_flow": _metric(
                    70.0, "usd_billions", period="FY2024"
                ),
                "cash_paid_for_property_and_equipment": _metric(
                    20.0, "usd_billions", period="FY2024"
                ),
            }
        )
        concepts = (
            (
                "near-name operand",
                (
                    "derived: operating_cash_flow - "
                    "cash_paid_for_property_and_equipments"
                ),
            ),
            (
                "wrong operand",
                (
                    "derived: operating_cash_flow - "
                    "capital_expenditures_including_finance_leases"
                ),
            ),
        )

        for label, concept in concepts:
            with self.subTest(case=label):
                current = _facts(
                    {
                        "free_cash_flow": {
                            **_metric(
                                60.0,
                                "usd_billions",
                                period="FY2025",
                            ),
                            "source": "derived",
                            "concept": concept,
                        },
                    }
                )
                result = build_deterministic_analysis(
                    current, previous_facts=prior
                )
                fcf = result["metrics"]["fcf"]
                signal = result["signals"]["fcf"]
                dcf = result["valuation"]["dcf"]

                self.assertEqual(fcf["value"], 60.0)
                self.assertIsNone(fcf["prior_value"])
                self.assertIsNone(fcf["change"])
                self.assertIsNone(fcf["change_pct"])
                self.assertEqual(signal["observed_value"], 60.0)
                self.assertIsNone(signal["prior_value"])
                self.assertIsNone(signal["change"])
                self.assertIsNone(signal["change_pct"])
                self.assertFalse(signal["comparable"])
                self.assertEqual(signal["score"], 0)
                self.assertEqual(
                    dcf["assumptions"]["starting_fcf"], 60.0
                )
                self.assertIsNone(
                    dcf["assumptions"]["inferred_growth"]
                )
                self.assertEqual(dcf["forecast"], [])


class ExplicitFcfPrecedenceTests(unittest.TestCase):
    """Explicit reported FCF wins; OCF minus cash capex is fallback only."""

    def test_explicit_reported_fcf_wins_over_compatible_derivation(self):
        # OCF - cash capex = 23.3, but the company reported 25.0.
        result = build_deterministic_analysis(
            _facts(
                {
                    "revenue": _metric(100.0, "usd_billions"),
                    "operating_cash_flow": _metric(37.2, "usd_billions"),
                    "capex": _metric(13.9, "usd_billions"),
                    "free_cash_flow": _metric(25.0, "usd_billions"),
                }
            )
        )
        self.assertAlmostEqual(result["metrics"]["fcf"]["value"], 25.0, places=9)
        self.assertNotAlmostEqual(result["metrics"]["fcf"]["value"], 23.3, places=3)
        self.assertAlmostEqual(
            result["metrics"]["fcf_margin"]["value"], 25.0, places=9
        )
        self.assertEqual(result["signals"]["fcf"]["observed_value"], 25.0)
        self.assertIsNone(result["valuation"]["assumptions"]["starting_fcf"])

    def test_explicit_fcf_survives_incompatible_or_missing_operands(self):
        cases = (
            ("scale mismatch", {
                "operating_cash_flow": _metric(37_200.0, "usd_millions"),
                "capex": _metric(13.9, "usd_billions"),
                "free_cash_flow": _metric(23.3, "usd_billions"),
            }),
            ("missing capex", {
                "operating_cash_flow": _metric(37.2, "usd_billions"),
                "free_cash_flow": _metric(23.3, "usd_billions"),
            }),
        )
        for label, metrics in cases:
            with self.subTest(case=label):
                result = build_deterministic_analysis(_facts(metrics))
                self.assertAlmostEqual(
                    result["metrics"]["fcf"]["value"], 23.3, places=9
                )

    def test_missing_explicit_fcf_with_incompatible_operands_stays_unknown(self):
        result = build_deterministic_analysis(
            _facts(
                {
                    "operating_cash_flow": _metric(80.0, "EUR", currency="EUR"),
                    "capex": _metric(20.0, "USD", currency="USD"),
                }
            )
        )
        self.assertIsNone(result["metrics"]["fcf"]["value"])
        self.assertIsNone(result["metrics"]["free_cash_flow"]["value"])


class CanonicalCapexAliasTests(unittest.TestCase):
    """Cash PP&E is canonical cash capex; finance-lease-inclusive capex
    remains separately scoped and never substitutes for it."""

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


    def test_finance_lease_only_capex_does_not_become_cash_capex(self):
        # With no cash-capex candidate present, the FLI number stays scoped
        # under its own name and is NOT promoted into `capex` arithmetic.
        result = build_deterministic_analysis(
            _facts(
                {
                    "revenue": _metric(64.727, "usd_billions"),
                    "operating_cash_flow": _metric(37.2, "usd_billions"),
                    "capital_expenditures_including_finance_leases": _metric(
                        19.0, "usd_billions"
                    ),
                }
            )
        )
        self.assertIsNone(result["metrics"]["capex"]["value"])
        self.assertIsNone(result["metrics"]["fcf"]["value"])
        supplemental = result["metrics"].get(
            "capital_expenditures_including_finance_leases"
        )
        self.assertIsNotNone(supplemental)
        self.assertAlmostEqual(supplemental["value"], 19.0, places=9)


class SupplementalMetricSurvivalTests(unittest.TestCase):
    """Valid finite non-STANDARD deterministic metrics survive finalization."""

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
                "azure_growth_from_ai_services_points": _metric(
                    8.0,
                    "percentage_points",
                    period="FY2024-Q4",
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
        ai_points = result["metrics"]["azure_growth_from_ai_services_points"]
        self.assertAlmostEqual(ai_points["value"], 8.0, places=9)

    def test_nonfinite_supplemental_values_do_not_surface_as_numbers(self):
        current = _facts(
            {
                "revenue": _metric(100.0, "usd_billions"),
                "segment_metric": _metric("nan", "percent"),
            }
        )
        result = build_deterministic_analysis(current)
        self.assertNotIn("segment_metric", result["metrics"])
        self.assertIsNotNone(result["metrics"]["revenue"]["value"])



class DirectFactWatchConsistencyTests(unittest.TestCase):
    """Current facts can satisfy a watch item without fabricating a trend."""

    _MARGIN_WARNING = "gross margin: missing comparable evidence"
    _GUIDANCE_WARNING = "guidance: missing comparable evidence"

    def assert_signal_has_no_trend(self, signal):
        self.assertIsNone(signal["prior_value"])
        self.assertIsNone(signal["change"])
        self.assertIsNone(signal["change_pct"])
        self.assertFalse(signal["comparable"])
        self.assertEqual(signal["score"], 0)

    def test_current_only_direct_margin_suppresses_only_missing_watch(self):
        result = build_deterministic_analysis(
            _facts({"gross_margin": _metric(63.0, "percent")})
        )

        self.assertNotIn(self._MARGIN_WARNING, result["watch_items"])
        signal = result["signals"]["gross_margin_delta"]
        self.assertEqual(signal["observed_value"], 63.0)
        self.assert_signal_has_no_trend(signal)

    def test_compatible_gross_profit_and_revenue_suppress_margin_watch(self):
        result = build_deterministic_analysis(
            _facts(
                {
                    "gross_profit": _metric(40.0, "usd_billions"),
                    "revenue": _metric(100.0, "usd_billions"),
                }
            )
        )

        self.assertNotIn(self._MARGIN_WARNING, result["watch_items"])
        signal = result["signals"]["gross_margin_delta"]
        self.assertIsNone(signal["observed_value"])
        self.assert_signal_has_no_trend(signal)

    def test_generic_forward_guidance_fact_suppresses_guidance_watch(self):
        guidance = _metric(120.0, "usd_billions", period="FY2025")
        guidance["relationship_tags"] = {"temporal_basis": "guidance"}
        result = build_deterministic_analysis(
            _facts({"guidance_revenue": guidance})
        )

        self.assertNotIn(self._GUIDANCE_WARNING, result["watch_items"])
        signal = result["signals"]["guidance_direction"]
        self.assertIsNone(signal["observed_value"])
        self.assert_signal_has_no_trend(signal)

    def test_missing_direct_facts_keep_margin_and_guidance_watches(self):
        result = build_deterministic_analysis(_facts({}))

        self.assertIn(self._MARGIN_WARNING, result["watch_items"])
        self.assertIn(self._GUIDANCE_WARNING, result["watch_items"])
        for name in ("gross_margin_delta", "guidance_direction"):
            with self.subTest(signal=name):
                signal = result["signals"][name]
                self.assertIsNone(signal["observed_value"])
                self.assert_signal_has_no_trend(signal)


def _covered_metrics(unit="usd_billions", period="FY2024-Q4"):
    return {
        "revenue": _metric(120.0, unit, period),
        "operating_cash_flow": _metric(40.0, unit, period),
        "capex": _metric(12.0, unit, period),
        "gross_margin": _metric(63.0, "percent", period),
        "inventory": _metric(6.0, unit, period),
        "backlog": _metric(30.0, unit, period),
    }


def _covered_prior():
    return {
        "revenue": _metric(100.0, "usd_billions", "FY2024-Q3"),
        "operating_cash_flow": _metric(30.0, "usd_billions", "FY2024-Q3"),
        "capex": _metric(10.0, "usd_billions", "FY2024-Q3"),
        "gross_margin": _metric(60.0, "percent", "FY2024-Q3"),
        "inventory": _metric(5.0, "usd_billions", "FY2024-Q3"),
        "backlog": _metric(25.0, "usd_billions", "FY2024-Q3"),
    }


_STRONG_QUALITATIVE = {
    "ai_demand": {"present": True, "strength": "strong", "evidence": ["e1"]},
    "datacenter_demand": {
        "present": True,
        "strength": "strong",
        "evidence": ["e2"],
    },
    "pricing_power": {"present": True, "strength": "moderate", "evidence": ["e3"]},
    "guidance_up": {"present": True, "strength": "raised", "evidence": ["e4"]},
}


class CoverageGatedStateTests(unittest.TestCase):
    """A confirmed/accelerating state requires documented valid signal
    coverage; sparse positive qualitative signals cannot reach it."""

    def test_sparse_qualitative_positives_cannot_reach_confirmed(self):
        # FCF + AI demand only: revenue/capex/gross-margin signals are not
        # comparable, so material finance coverage is incomplete even though
        # every present signal is positive.
        current = _facts(
            {
                "operating_cash_flow": _metric(37.2, "usd_billions"),
                "cash_paid_for_property_and_equipment": _metric(
                    13.9, "usd_billions"
                ),
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
        self.assertGreaterEqual(result["score"], 2)
        self.assertLess(
            result["score"], 5, "sparse positives must not score into confirmed band"
        )
        self.assertNotEqual(result["state"]["stage"], "confirmed")
        self.assertNotEqual(result["state"]["stage"], "accelerating")
        coverage = result["state"].get("coverage")
        self.assertIsInstance(coverage, dict)
        self.assertFalse(coverage.get("eligible_for_high_states"))
        uncovered = set(coverage.get("uncovered", []))
        self.assertTrue(
            {"revenue", "capex"} <= uncovered or not coverage.get(
                "material_signals", {}
            ).get("revenue"),
            f"revenue must be reported as uncovered: {coverage}",
        )

    def test_invalid_material_finance_is_not_confidence_neutral(self):
        # Revenue grew on paper but the operands mix scales: the growth signal
        # is invalid and cannot silently support a high state.
        current = _facts(
            {
                "revenue": _metric(120.0, "usd_billions"),
                "prior_metrics": {
                    "revenue": _metric(100_000.0, "usd_thousands", "FY2024-Q3")
                },
                "operating_cash_flow": _metric(40.0, "usd_billions"),
                "cash_paid_for_property_and_equipment": _metric(12.0, "usd_billions"),
                "gross_margin": _metric(63.0, "percent"),
                "prior_gross_margin": None,
            },
            qualitative=_STRONG_QUALITATIVE,
        )
        result = build_deterministic_analysis(
            current,
            previous_facts={"metrics": {}, "qualitative": {}},
        )
        stage = result["state"]["stage"]
        if result["score"] >= 5:
            self.assertEqual(stage, "forming")

    def test_fully_covered_valid_case_still_confirms(self):
        prior = {"metrics": _covered_prior(), "qualitative": {}}
        current = _facts(_covered_metrics(), qualitative=_STRONG_QUALITATIVE)
        result = build_deterministic_analysis(current, previous_facts=prior)
        coverage = result["state"]["coverage"]
        self.assertTrue(coverage["eligible_for_high_states"])
        for name in ("revenue", "capex", "fcf", "gross_margin_delta"):
            self.assertIn(name, coverage["covered"])
        self.assertGreaterEqual(result["score"], 5)
        self.assertIn(result["state"]["stage"], ("confirmed", "accelerating"))

    def test_mixed_scale_prior_revenue_blocks_high_state_despite_positives(self):
        prior = {"metrics": _covered_prior(), "qualitative": {}}
        prior["metrics"]["revenue"] = _metric(
            100_000.0, "usd_thousands", "FY2024-Q3"
        )
        current = _facts(_covered_metrics(), qualitative=_STRONG_QUALITATIVE)
        result = build_deterministic_analysis(current, previous_facts=prior)
        coverage = result["state"]["coverage"]
        if "revenue" in coverage.get("uncovered", []):
            self.assertFalse(coverage["eligible_for_high_states"])
            self.assertEqual(result["state"]["stage"], "forming")


def _relationship_metric(
    value,
    *,
    role,
    metric_family,
    leaf,
    unit,
    period="FY2025",
    scope="consolidated",
    comparison_basis=None,
    temporal_basis=None,
    cash_basis="not_applicable",
    currency=None,
    **extra_tags,
):
    if comparison_basis is None:
        comparison_basis = "year_over_year_gaap" if leaf == "growth" else "none"
    if temporal_basis is None:
        temporal_basis = "rate_over_period" if leaf == "growth" else "period_flow"
    tags = {
        "role": role,
        "metric_family": metric_family,
        "leaf": leaf,
        "scope": scope,
        "comparison_basis": comparison_basis,
        "temporal_basis": temporal_basis,
        "cash_basis": cash_basis,
    }
    tags.update(extra_tags)
    record = {
        "value": value,
        "unit": unit,
        "period": period,
        "evidence": ["Neutral filing disclosure"],
        "source": "reported",
        "relationship_tags": tags,
    }
    if currency is not None:
        record["currency"] = currency
    return record


def _relationship_facts(**metrics):
    return {"metrics": metrics, "qualitative": {}}


def _relationship_payload(current, prior=None):
    return build_material_relationship_contract(current, prior).to_payload()


class MaterialRelationshipContractTests(unittest.TestCase):
    def test_emits_all_three_kinds_in_fixed_order_with_generic_external_effect(self):
        current = _relationship_facts(
            revenue_growth=_relationship_metric(
                8.0,
                role="top_line",
                metric_family="revenue",
                leaf="growth",
                unit="percent",
            ),
            net_income_growth=_relationship_metric(
                5.0,
                role="bottom_line",
                metric_family="net_income",
                leaf="growth",
                unit="percent",
            ),
            integration_contribution=_relationship_metric(
                1.5,
                role="external_effect",
                metric_family="external_effect",
                leaf="external_effect",
                unit="percentage_points",
                group_id="external-group-1",
                category="business_combination",
                effect_kind="contribution",
                effect_basis="percentage_points",
                comparison_basis="year_over_year_gaap",
                temporal_basis="rate_over_period",
                recipient_path="metrics.revenue_growth",
                qualifiers=(),
            ),
            operating_cash_flow=_relationship_metric(
                42,
                role="cash_generation",
                metric_family="operating_cash_flow",
                leaf="level",
                unit="USDm",
                cash_basis="cash",
                currency="USD",
            ),
            capex=_relationship_metric(
                18,
                role="cash_investment",
                metric_family="capex",
                leaf="level",
                unit="USDm",
                cash_basis="cash",
                currency="USD",
            ),
        )

        payload = _relationship_payload(current)

        self.assertEqual(
            [row["kind"] for row in payload["material_relationships"]],
            [
                "same_period_top_bottom_growth",
                "external_effect_on_recipient",
                "cash_generation_vs_investment",
            ],
        )
        self.assertTrue(
            all(
                row["compatibility"] == "compatible"
                for row in payload["material_relationships"]
            )
        )
        external = payload["material_relationships"][1]
        self.assertEqual(
            [ref["role"] for ref in external["required_facts"]],
            ["effect", "recipient"],
        )

    def test_generic_typed_company_twins_ignore_issuer_and_source_labels(self):
        def typed_company(issuer_name, source_label):
            metrics = {
                "sales_growth": _relationship_metric(
                    11.0,
                    role="top_line",
                    metric_family="revenue",
                    leaf="growth",
                    unit="percent",
                ),
                "earnings_growth": _relationship_metric(
                    7.0,
                    role="bottom_line",
                    metric_family="net_income",
                    leaf="growth",
                    unit="percent",
                ),
                "portfolio_contribution": _relationship_metric(
                    2.0,
                    role="external_effect",
                    metric_family="external_effect",
                    leaf="external_effect",
                    unit="percentage_points",
                    group_id="portfolio-effect",
                    category="business_combination",
                    effect_kind="contribution",
                    effect_basis="percentage_points",
                    comparison_basis="year_over_year_gaap",
                    temporal_basis="rate_over_period",
                    recipient_path="metrics.sales_growth",
                    qualifiers=(),
                ),
                "cash_from_operations": _relationship_metric(
                    54,
                    role="cash_generation",
                    metric_family="operating_cash_flow",
                    leaf="level",
                    unit="USDm",
                    cash_basis="cash",
                    currency="USD",
                ),
                "capital_spending": _relationship_metric(
                    21,
                    role="cash_investment",
                    metric_family="capex",
                    leaf="level",
                    unit="USDm",
                    cash_basis="cash",
                    currency="USD",
                ),
            }
            for metric_key, metric in metrics.items():
                metric["concept"] = f"{issuer_name} {metric_key}"
                metric["source"] = source_label
                metric["evidence"] = [
                    f"{issuer_name} disclosure labeled {source_label}"
                ]
            return {
                "issuer_name": issuer_name,
                "source_label": source_label,
                "metrics": metrics,
                "qualitative": {},
            }

        northstar = _relationship_payload(
            typed_company("Northstar Industries", "annual filing")
        )
        redwood = _relationship_payload(
            typed_company("Redwood Systems", "results release")
        )

        expected_kinds = [
            "same_period_top_bottom_growth",
            "external_effect_on_recipient",
            "cash_generation_vs_investment",
        ]
        expected_roles = [
            ["top_line_growth", "bottom_line_growth"],
            ["effect", "recipient"],
            ["cash_generation", "cash_investment"],
        ]
        for payload in (northstar, redwood):
            self.assertEqual(
                [row["kind"] for row in payload["material_relationships"]],
                expected_kinds,
            )
            self.assertEqual(
                [
                    [ref["role"] for ref in row["required_facts"]]
                    for row in payload["material_relationships"]
                ],
                expected_roles,
            )
            self.assertTrue(
                all(
                    row["compatibility"] == "compatible"
                    for row in payload["material_relationships"]
                )
            )

        self.assertEqual(
            list(northstar["relationship_facts"]),
            list(redwood["relationship_facts"]),
        )
        self.assertEqual(
            northstar["material_relationships"],
            redwood["material_relationships"],
        )
        self.assertNotEqual(
            [
                fact["metric_label"]
                for fact in northstar["relationship_facts"].values()
            ],
            [
                fact["metric_label"]
                for fact in redwood["relationship_facts"].values()
            ],
        )

    def test_missing_relationship_tags_are_empty_or_fail_closed(self):
        untagged = _relationship_facts(
            sales_growth={
                "value": 11.0,
                "unit": "percent",
                "period": "FY2025",
                "concept": "Northstar Industries sales growth",
                "source": "annual filing",
                "evidence": ["Northstar Industries annual filing"],
            },
            earnings_growth={
                "value": 7.0,
                "unit": "percent",
                "period": "FY2025",
                "concept": "Northstar Industries earnings growth",
                "source": "annual filing",
                "evidence": ["Northstar Industries annual filing"],
            },
        )
        self.assertEqual(
            _relationship_payload(untagged),
            {"relationship_facts": {}, "material_relationships": []},
        )

        only_top_line_tagged = copy.deepcopy(untagged)
        only_top_line_tagged["metrics"]["sales_growth"] = _relationship_metric(
            11.0,
            role="top_line",
            metric_family="revenue",
            leaf="growth",
            unit="percent",
        )
        payload = _relationship_payload(only_top_line_tagged)
        self.assertEqual(len(payload["relationship_facts"]), 1)
        self.assertEqual(len(payload["material_relationships"]), 1)
        row = payload["material_relationships"][0]
        self.assertEqual(row["kind"], "same_period_top_bottom_growth")
        self.assertEqual(row["compatibility"], "incompatible")
        self.assertEqual(row["incompatibility_reasons"], ["missing_required_role"])

    def test_reported_growth_is_preferred_to_derivation(self):
        current = _relationship_facts(
            revenue=_relationship_metric(
                120,
                role="top_line",
                metric_family="revenue",
                leaf="level",
                unit="USDm",
                currency="USD",
            ),
            revenue_growth=_relationship_metric(
                17.25,
                role="top_line",
                metric_family="revenue",
                leaf="growth",
                unit="percent",
            ),
            net_income_growth=_relationship_metric(
                9.0,
                role="bottom_line",
                metric_family="net_income",
                leaf="growth",
                unit="percent",
            ),
        )
        prior = _relationship_facts(
            revenue=_relationship_metric(
                100,
                role="top_line",
                metric_family="revenue",
                leaf="level",
                unit="USDm",
                period="FY2024",
                currency="USD",
            )
        )

        payload = _relationship_payload(current, prior)
        values = tuple(payload["relationship_facts"].values())
        revenue_growth = [
            fact
            for fact in values
            if fact["metric_key"] == "revenue_growth"
            and fact["comparison_basis"] == "year_over_year_gaap"
        ]
        self.assertEqual(len(revenue_growth), 1)
        self.assertEqual(float(revenue_growth[0]["value"]), 17.25)
        self.assertEqual(revenue_growth[0]["derivation"], "reported")
        self.assertNotIn(
            "derived_from_current_and_prior", revenue_growth[0]["qualifiers"]
        )

    def test_growth_is_derived_deterministically_only_from_compatible_periods(self):
        current = _relationship_facts(
            revenue=_relationship_metric(
                110,
                role="top_line",
                metric_family="revenue",
                leaf="level",
                unit="USDm",
                currency="USD",
            ),
            net_income_growth=_relationship_metric(
                4.0,
                role="bottom_line",
                metric_family="net_income",
                leaf="growth",
                unit="percent",
            ),
        )
        prior = _relationship_facts(
            revenue=_relationship_metric(
                100,
                role="top_line",
                metric_family="revenue",
                leaf="level",
                unit="USDm",
                period="FY2024",
                currency="USD",
            )
        )

        payload = _relationship_payload(current, prior)
        derived = [
            fact
            for fact in payload["relationship_facts"].values()
            if "derived_from_current_and_prior" in fact["qualifiers"]
        ]
        self.assertEqual(len(derived), 1)
        self.assertEqual(float(derived[0]["value"]), 10.0)
        self.assertEqual(derived[0]["derivation"], "current_and_prior_percent_change")
        self.assertIn("rounded_to_one_decimal", derived[0]["qualifiers"])
        self.assertEqual(len(derived[0]["source_paths"]), 2)

        incompatible_prior = copy.deepcopy(prior)
        incompatible_prior["metrics"]["revenue"]["period"] = "FY2024-Q4"
        incompatible = _relationship_payload(current, incompatible_prior)
        self.assertFalse(
            any(
                "derived_from_current_and_prior" in fact["qualifiers"]
                for fact in incompatible["relationship_facts"].values()
            )
        )

    def test_cash_relationship_orders_four_distinct_reported_facts(self):
        current = _relationship_facts(
            operating_cash_flow=_relationship_metric(
                80,
                role="cash_generation",
                metric_family="operating_cash_flow",
                leaf="level",
                unit="USDm",
                cash_basis="cash",
                currency="USD",
                duration_days=365,
            ),
            free_cash_flow=_relationship_metric(
                55,
                role="cash_generation",
                metric_family="free_cash_flow",
                leaf="level",
                unit="USDm",
                cash_basis="cash",
                currency="USD",
                duration_days=365,
            ),
            cash_paid_for_property_and_equipment=_relationship_metric(
                25,
                role="cash_investment",
                metric_family="capital_investment",
                leaf="level",
                unit="USDm",
                cash_basis="cash",
                currency="USD",
                duration_days=365,
            ),
            capital_expenditures_including_finance_leases=_relationship_metric(
                31,
                role="cash_investment",
                metric_family="capital_investment",
                leaf="level",
                unit="USDm",
                cash_basis="cash_plus_finance_leases",
                currency="USD",
                duration_days=365,
            ),
        )

        payload = _relationship_payload(current)
        cash_row = payload["material_relationships"][0]
        facts_by_id = payload["relationship_facts"]
        selected = [
            facts_by_id[ref["fact_path"].rsplit(".", 1)[-1]]
            for ref in cash_row["required_facts"]
        ]

        self.assertEqual(cash_row["kind"], "cash_generation_vs_investment")
        self.assertEqual(cash_row["compatibility"], "compatible")
        self.assertEqual(
            [fact["metric_key"] for fact in selected],
            [
                "operating_cash_flow",
                "free_cash_flow",
                "cash_paid_for_property_and_equipment",
                "capital_expenditures_including_finance_leases",
            ],
        )
        self.assertEqual(
            [ref["role"] for ref in cash_row["required_facts"]],
            [
                "cash_generation",
                "cash_generation",
                "cash_investment",
                "cash_investment_supplemental",
            ],
        )
        self.assertEqual(
            [fact["cash_basis"] for fact in selected],
            ["cash", "cash", "cash", "cash_plus_finance_leases"],
        )
        self.assertEqual(
            [float(fact["value"]) for fact in selected],
            [80.0, 55.0, 25.0, 31.0],
        )
        self.assertTrue(
            all(fact["derivation"] == "reported" for fact in selected)
        )
        self.assertFalse(
            any(float(fact["value"]) == 6.0 for fact in facts_by_id.values())
        )

    def test_cash_relationship_falls_back_without_broader_basis(self):
        current = _relationship_facts(
            operating_cash_flow=_relationship_metric(
                80,
                role="cash_generation",
                metric_family="operating_cash_flow",
                leaf="level",
                unit="USDm",
                cash_basis="cash",
                currency="USD",
            ),
            free_cash_flow=_relationship_metric(
                55,
                role="cash_generation",
                metric_family="free_cash_flow",
                leaf="level",
                unit="USDm",
                cash_basis="cash",
                currency="USD",
            ),
            cash_paid_for_property_and_equipment=_relationship_metric(
                25,
                role="cash_investment",
                metric_family="capital_investment",
                leaf="level",
                unit="USDm",
                cash_basis="cash",
                currency="USD",
            ),
        )

        payload = _relationship_payload(current)
        row = payload["material_relationships"][0]
        facts_by_id = payload["relationship_facts"]
        selected = [
            facts_by_id[ref["fact_path"].rsplit(".", 1)[-1]]
            for ref in row["required_facts"]
        ]

        self.assertEqual(row["compatibility"], "compatible")
        self.assertEqual(
            [fact["metric_key"] for fact in selected],
            [
                "operating_cash_flow",
                "free_cash_flow",
                "cash_paid_for_property_and_equipment",
            ],
        )
        self.assertNotIn(
            "cash_investment_supplemental",
            [ref["role"] for ref in row["required_facts"]],
        )
        self.assertTrue(
            all(fact["derivation"] == "reported" for fact in facts_by_id.values())
        )

    def test_broader_investment_basis_is_supplemental_only_when_compatible(self):
        mismatches = (
            ("cash_basis", "not_applicable"),
            ("scope", "segment"),
            ("comparison_basis", "sequential"),
            ("period", "FY2024"),
            ("currency", "EUR"),
            ("unit", "shares_millions"),
        )
        for field, value in mismatches:
            with self.subTest(field=field):
                current = _relationship_facts(
                    operating_cash_flow=_relationship_metric(
                        80,
                        role="cash_generation",
                        metric_family="operating_cash_flow",
                        leaf="level",
                        unit="USDm",
                        cash_basis="cash",
                        currency="USD",
                        duration_days=365,
                    ),
                    free_cash_flow=_relationship_metric(
                        55,
                        role="cash_generation",
                        metric_family="free_cash_flow",
                        leaf="level",
                        unit="USDm",
                        cash_basis="cash",
                        currency="USD",
                        duration_days=365,
                    ),
                    cash_capex=_relationship_metric(
                        25,
                        role="cash_investment",
                        metric_family="capital_investment",
                        leaf="level",
                        unit="USDm",
                        cash_basis="cash",
                        currency="USD",
                        duration_days=365,
                    ),
                    broader_capex=_relationship_metric(
                        31,
                        role="cash_investment",
                        metric_family="capital_investment",
                        leaf="level",
                        unit="USDm",
                        cash_basis="cash_plus_finance_leases",
                        currency="USD",
                        duration_days=365,
                    ),
                )
                broader = current["metrics"]["broader_capex"]
                target = (
                    broader["relationship_tags"]
                    if field in {"cash_basis", "scope", "comparison_basis"}
                    else broader
                )
                target[field] = value

                payload = _relationship_payload(current)
                row = payload["material_relationships"][0]
                selected_ids = {
                    ref["fact_path"].rsplit(".", 1)[-1]
                    for ref in row["required_facts"]
                }
                selected = [
                    payload["relationship_facts"][fact_id]
                    for fact_id in selected_ids
                ]

                self.assertEqual(row["compatibility"], "compatible")
                self.assertNotIn(
                    "broader_capex",
                    {fact["metric_key"] for fact in selected},
                )
                self.assertFalse(
                    any(
                        ref["role"] == "cash_investment_supplemental"
                        for ref in row["required_facts"]
                    )
                )

    def test_untagged_effect_like_name_is_ignored_and_unresolved_recipient_abstains(self):
        current = _relationship_facts(
            acquisition_growth_contribution={
                "value": 2.0,
                "unit": "percentage_points",
                "period": "FY2025",
                "evidence": ["Neutral filing disclosure"],
            },
            unresolved_effect=_relationship_metric(
                1.0,
                role="external_effect",
                metric_family="external_effect",
                leaf="external_effect",
                unit="percentage_points",
                group_id="external-group-2",
                category="other",
                effect_kind="drag",
                effect_basis="percentage_points",
                recipient_path=None,
                qualifiers=(),
                compatibility="incompatible",
                incompatibility_reasons=("unresolved_recipient",),
            ),
        )

        payload = _relationship_payload(current)
        self.assertNotIn(
            "acquisition_growth_contribution",
            {fact["metric_key"] for fact in payload["relationship_facts"].values()},
        )
        self.assertEqual(len(payload["material_relationships"]), 1)
        row = payload["material_relationships"][0]
        self.assertEqual(row["kind"], "external_effect_on_recipient")
        self.assertEqual(row["compatibility"], "incompatible")
        self.assertIn("unresolved_recipient", row["incompatibility_reasons"])

    def test_growth_compatibility_fails_closed_for_basis_period_scope_and_unit(self):
        mismatches = (
            (
                "comparison_basis",
                "year_over_year_constant_currency",
                "comparison_basis_mismatch",
            ),
            ("period", "FY2025-Q4", "period_mismatch"),
            ("scope", "segment", "scope_mismatch"),
            ("unit", "basis_points", "unit_mismatch"),
            ("temporal_basis", "point_in_time_stock", "temporal_basis_mismatch"),
        )
        for field, incompatible_value, reason in mismatches:
            with self.subTest(field=field):
                top = _relationship_metric(
                    7.0,
                    role="top_line",
                    metric_family="revenue",
                    leaf="growth",
                    unit="percent",
                )
                bottom = _relationship_metric(
                    4.0,
                    role="bottom_line",
                    metric_family="net_income",
                    leaf="growth",
                    unit="percent",
                )
                target = (
                    bottom["relationship_tags"]
                    if field in {"comparison_basis", "scope", "temporal_basis"}
                    else bottom
                )
                target[field] = incompatible_value
                row = _relationship_payload(
                    _relationship_facts(top_growth=top, bottom_growth=bottom)
                )["material_relationships"][0]
                self.assertEqual(row["compatibility"], "incompatible")
                self.assertIn(reason, row["incompatibility_reasons"])

    def test_cash_compatibility_fails_closed_for_currency_and_stock_flow(self):
        cases = (
            ("currency", "EUR", "currency_mismatch"),
            ("unit", "shares_millions", "unit_mismatch"),
            ("temporal_basis", "point_in_time_stock", "temporal_basis_mismatch"),
        )
        for field, incompatible_value, reason in cases:
            with self.subTest(field=field):
                generated = _relationship_metric(
                    40,
                    role="cash_generation",
                    metric_family="operating_cash_flow",
                    leaf="level",
                    unit="USDm",
                    cash_basis="cash",
                    currency="USD",
                )
                invested = _relationship_metric(
                    12,
                    role="cash_investment",
                    metric_family="capex",
                    leaf="level",
                    unit="USDm",
                    cash_basis="cash",
                    currency="USD",
                )
                target = (
                    invested["relationship_tags"]
                    if field == "temporal_basis"
                    else invested
                )
                target[field] = incompatible_value
                row = _relationship_payload(
                    _relationship_facts(generated=generated, invested=invested)
                )["material_relationships"][0]
                self.assertEqual(row["compatibility"], "incompatible")
                self.assertIn(reason, row["incompatibility_reasons"])

    def test_nonfinite_values_are_rejected_and_inputs_are_not_mutated(self):
        current = _relationship_facts(
            nan_growth=_relationship_metric(
                math.nan,
                role="top_line",
                metric_family="revenue",
                leaf="growth",
                unit="percent",
            ),
            infinite_cash=_relationship_metric(
                math.inf,
                role="cash_generation",
                metric_family="operating_cash_flow",
                leaf="level",
                unit="USDm",
                cash_basis="cash",
                currency="USD",
            ),
        )
        before = copy.deepcopy(current)

        contract = build_material_relationship_contract(current)
        payload = contract.to_payload()

        self.assertEqual(payload["relationship_facts"], {})
        self.assertEqual(payload["material_relationships"], [])
        self.assertEqual(
            current["metrics"]["nan_growth"]["relationship_tags"],
            before["metrics"]["nan_growth"]["relationship_tags"],
        )
        self.assertEqual(
            current["metrics"]["infinite_cash"]["relationship_tags"],
            before["metrics"]["infinite_cash"]["relationship_tags"],
        )
        self.assertTrue(math.isnan(current["metrics"]["nan_growth"]["value"]))
        self.assertTrue(math.isinf(current["metrics"]["infinite_cash"]["value"]))
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            contract.relationship_facts = ()

    def test_caps_order_and_opaque_ids_are_stable_across_input_order(self):
        metrics = {}
        for index in range(30):
            metrics[f"cash_source_{index:02d}"] = _relationship_metric(
                index + 1,
                role="cash_generation" if index < 15 else "cash_investment",
                metric_family=(
                    "operating_cash_flow" if index < 15 else "capex"
                ),
                leaf="level",
                unit="USDm",
                cash_basis="cash",
                currency="USD",
            )
        forward = _relationship_payload(_relationship_facts(**metrics))
        reverse = _relationship_payload(
            _relationship_facts(**dict(reversed(tuple(metrics.items()))))
        )

        self.assertEqual(forward, reverse)
        self.assertLessEqual(len(forward["relationship_facts"]), 24)
        self.assertLessEqual(len(forward["material_relationships"]), 3)
        for row in forward["material_relationships"]:
            self.assertLessEqual(len(row["required_facts"]), 8)
            self.assertLessEqual(len(row["relationship_id"]), 80)
            self.assertNotIn("cash_source", row["relationship_id"])
        for fact_id, fact in forward["relationship_facts"].items():
            self.assertLessEqual(len(fact_id), 80)
            self.assertLessEqual(len(fact["source_paths"]), 2)

    def test_period_mismatch_never_annualizes_or_reconciles_cash_flows(self):
        current = _relationship_facts(
            quarterly_cash=_relationship_metric(
                20,
                role="cash_generation",
                metric_family="operating_cash_flow",
                leaf="level",
                unit="USDm",
                period="FY2025-Q4",
                cash_basis="cash",
                currency="USD",
            ),
            annual_investment=_relationship_metric(
                60,
                role="cash_investment",
                metric_family="capex",
                leaf="level",
                unit="USDm",
                period="FY2025",
                cash_basis="cash",
                currency="USD",
            ),
        )

        payload = _relationship_payload(current)
        row = payload["material_relationships"][0]
        self.assertEqual(row["compatibility"], "incompatible")
        self.assertIn("period_mismatch", row["incompatibility_reasons"])
        self.assertEqual(
            sorted(float(fact["value"]) for fact in payload["relationship_facts"].values()),
            [20.0, 60.0],
        )
        self.assertTrue(
            all(
                fact["derivation"] == "reported"
                for fact in payload["relationship_facts"].values()
            )
        )


if __name__ == "__main__":
    unittest.main()
