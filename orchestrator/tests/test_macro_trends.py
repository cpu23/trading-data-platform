import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from processors.macro_trends import (
    analyze_macro_trends,
    build_macro_synthesis,
    format_macro_synthesis,
    format_trend_signals,
)


def history(values: list[float]) -> list[dict]:
    latest = datetime(2026, 7, 28, tzinfo=UTC)
    return [
        {"value": value, "observed_at": latest - timedelta(days=index)}
        for index, value in enumerate(values)
    ]


def signal(
    series_id: str,
    direction: str,
    latest: float = 1.0,
    reference: float = 1.0,
) -> dict:
    return {
        "series_id": series_id,
        "label": series_id,
        "value_unit": "percent",
        "direction": direction,
        "latest": {"value": latest},
        "reference": {"value": reference},
    }


class MacroTrendTests(unittest.TestCase):
    def test_policy_rate_hold_is_deterministic_against_previous_observation(self):
        signals = analyze_macro_trends(
            {"FEDFUNDS": {"history": history([5.33, 5.33, 5.33])}}
        )

        signal = signals[0]
        self.assertEqual(signal["series_id"], "FEDFUNDS")
        self.assertEqual(signal["direction"], "unchanged")
        self.assertEqual(signal["transition"], "stable")
        self.assertEqual(signal["change"], {"value": 0.0, "unit": "percentage_points"})
        self.assertIn("held at 5.33%", signal["statement"])
        self.assertIn("prior observation", signal["statement"])

    def test_five_observation_yield_move_records_a_reversal(self):
        values = [4.30, 4.28, 4.24, 4.20, 4.16, 4.10, 4.14, 4.18, 4.22, 4.26, 4.30]
        signals = analyze_macro_trends({"DGS10": {"history": history(values)}})

        signal = signals[0]
        self.assertEqual(signal["comparison_observations"], 5)
        self.assertEqual(signal["direction"], "higher")
        self.assertEqual(signal["transition"], "reversal")
        self.assertEqual(signal["change"], {"value": 20.0, "unit": "basis_points"})
        self.assertIn("reversed the preceding window", signal["statement"])

    def test_annual_inflation_growth_is_derived_before_comparison(self):
        values = [
            130.0,
            128.0,
            126.0,
            126.0,
            126.0,
            126.0,
            126.0,
            126.0,
            126.0,
            126.0,
            126.0,
            126.0,
            125.0,
            124.0,
            123.0,
        ]
        signals = analyze_macro_trends({"CPIAUCSL": {"history": history(values)}})

        signal = signals[0]
        self.assertAlmostEqual(signal["latest"]["value"], 4.0)
        self.assertEqual(signal["direction"], "higher")
        self.assertEqual(signal["transition"], "persisting")
        self.assertIn("Headline CPI annual growth accelerated", signal["statement"])

    def test_real_gdp_growth_is_derived_from_quarterly_levels(self):
        values = [
            110.0,
            108.0,
            106.0,
            104.0,
            100.0,
            99.0,
            98.0,
            97.0,
            96.0,
        ]

        signals = analyze_macro_trends({"GDPC1": {"history": history(values)}})

        signal = signals[0]
        self.assertEqual(signal["series_id"], "GDPC1")
        self.assertAlmostEqual(signal["latest"]["value"], 10.0)
        self.assertEqual(signal["direction"], "higher")

    def test_official_uk_and_energy_series_are_checked(self):
        signals = analyze_macro_trends(
            {
                "BOE:BANK_RATE": {
                    "history": history([4.0, 4.25, 4.25, 4.25, 4.25, 4.25])
                },
                "DCOILBRENTEU": {"history": history([82.0, 79.0, 78.0])},
                "OECD:CLI_GB": {"history": history([100.2, 100.0, 99.8])},
            }
        )
        by_id = {item["series_id"]: item for item in signals}

        self.assertEqual(by_id["BOE:BANK_RATE"]["direction"], "lower")
        self.assertEqual(by_id["DCOILBRENTEU"]["direction"], "higher")
        self.assertEqual(by_id["OECD:CLI_GB"]["direction"], "higher")

    def test_boe_flow_series_are_not_treated_as_stock_growth(self):
        signals = analyze_macro_trends(
            {
                "BOE:M4": {"history": history([12_000.0, 4_000.0, -2_000.0])},
                "BOE:MORTGAGE_APPROVALS": {
                    "history": history([68_000.0, 65_000.0, 63_000.0])
                },
            }
        )
        by_id = {item["series_id"]: item for item in signals}

        self.assertEqual(by_id["BOE:M4"]["latest"]["value"], 12_000.0)
        self.assertEqual(by_id["BOE:M4"]["change"]["unit"], "million_pounds")
        self.assertIn("£12,000 million", by_id["BOE:M4"]["statement"])
        self.assertEqual(by_id["BOE:MORTGAGE_APPROVALS"]["direction"], "higher")
        self.assertEqual(by_id["BOE:MORTGAGE_APPROVALS"]["change"]["unit"], "count")

    def test_regional_and_energy_domains_activate_bounded_channels(self):
        synthesis = build_macro_synthesis(
            [
                signal("FEDFUNDS", "unchanged"),
                signal("GDPC1", "higher"),
                signal("PAYEMS", "higher"),
                signal("OECD:CLI_US", "higher"),
                signal("CPIAUCSL", "lower"),
                signal("PCEPILFE", "lower"),
                signal("UNRATE", "unchanged"),
                signal("ICSA", "lower"),
                signal("BAMLH0A0HYM2", "lower"),
                signal("VIXCLS", "lower"),
                signal("DCOILBRENTEU", "higher"),
                signal("DCOILWTICO", "higher"),
                signal("BOE:BANK_RATE", "lower"),
                signal("ECB:DEPOSIT_RATE", "higher"),
            ]
        )

        self.assertEqual(synthesis["composite_state"], "soft_landing_configuration")
        self.assertEqual(synthesis["domains"]["us_growth"]["state"], "strengthening")
        self.assertEqual(synthesis["domains"]["energy_prices"]["state"], "rising")
        self.assertEqual(synthesis["domains"]["uk_policy"]["state"], "easing")
        self.assertTrue(
            any(
                "Energy-cost channel" in item
                for item in synthesis["transmission_channels"]
            )
        )
        self.assertTrue(
            any(
                "UK policy channel" in item
                for item in synthesis["transmission_channels"]
            )
        )

    def test_curve_signal_carries_level_state_without_inferring_causality(self):
        thresholds = {
            "yield_curve": {
                "deep_inversion": -0.5,
                "inverted": 0.0,
                "flat": 0.5,
                "normal": 1.5,
            }
        }
        signals = analyze_macro_trends(
            {"T10Y2Y": {"history": history([0.42, 0.38, 0.36])}}, thresholds
        )

        signal = signals[0]
        self.assertEqual(signal["level_state"], "positive but flat")
        self.assertIn("level state: positive but flat", signal["statement"])

    def test_insufficient_history_has_an_explicit_summary(self):
        self.assertEqual(
            format_trend_signals([]),
            "No deterministic trend signals had sufficient history.",
        )

    def test_synthesis_detects_policy_easing_with_market_stress(self):
        synthesis = build_macro_synthesis(
            [
                signal("FEDFUNDS", "lower"),
                signal("CPIAUCSL", "lower"),
                signal("PCEPILFE", "lower"),
                signal("UNRATE", "higher"),
                signal("ICSA", "higher"),
                signal("DGS2", "lower"),
                signal("DGS10", "lower"),
                signal("BAMLH0A0HYM2", "higher"),
                signal("VIXCLS", "higher"),
            ]
        )

        self.assertEqual(
            synthesis["composite_state"], "policy_easing_with_market_stress"
        )
        self.assertEqual(synthesis["domains"]["labor"]["state"], "weakening")
        self.assertTrue(
            any(
                "Corporate-finance channel" in item
                for item in synthesis["transmission_channels"]
            )
        )
        self.assertTrue(
            any(
                "Household-income channel" in item
                for item in synthesis["transmission_channels"]
            )
        )

    def test_sparse_synthesis_caps_confidence_and_forbids_real_rate_inference(self):
        synthesis = build_macro_synthesis(
            [
                signal("FEDFUNDS", "unchanged", 4.5, 4.5),
                signal("DGS10", "higher", 4.6, 4.25),
                signal("T5YIE", "lower", 2.15, 2.3),
            ]
        )
        formatted = format_macro_synthesis(synthesis)

        self.assertEqual(synthesis["composite_state"], "insufficient_coverage")
        self.assertEqual(synthesis["confidence_ceiling"], "low")
        self.assertIn("real-rate proxy: unavailable", formatted)

    def test_real_rate_proxy_requires_matching_ten_year_series(self):
        synthesis = build_macro_synthesis(
            [
                signal("DGS10", "higher", 4.7, 4.5),
                signal("T10YIE", "lower", 2.2, 2.3),
            ]
        )

        proxy = synthesis["real_rate_proxy"]
        self.assertEqual(proxy["direction"], "higher")
        self.assertAlmostEqual(proxy["latest"], 2.5)
        self.assertEqual(proxy["change_basis_points"], 30.0)


if __name__ == "__main__":
    unittest.main()
