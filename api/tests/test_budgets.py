import math
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from budgets import budget_status, get_budget_status, get_today_spend


class ApiBudgetTests(unittest.TestCase):
    def test_spend_query_uses_bounded_utc_day_and_null_safe_tokens(self):
        now = datetime(2026, 7, 15, 23, 59, tzinfo=UTC)
        with patch(
            "budgets.query_one", return_value={"total_cost": None, "total_tokens": None}
        ) as query:
            self.assertEqual(get_today_spend({}, now=now), (0.0, 0))
        sql = query.call_args.args[0]
        params = query.call_args.kwargs["params"]
        self.assertIn("started_at >= :today_start", sql)
        self.assertIn("started_at < :tomorrow_start", sql)
        self.assertIn("COALESCE(tokens_input, 0)", sql)
        self.assertEqual(params["today_start"], datetime(2026, 7, 15, tzinfo=UTC))
        self.assertEqual(params["tomorrow_start"], datetime(2026, 7, 16, tzinfo=UTC))

    def test_api_boundary_matches_enforcement_contract(self):
        exact = budget_status(2.0, 2.0, 80)
        self.assertTrue(exact["exceeded"])
        self.assertFalse(exact["warning"])
        # A zero cap denies all paid calls; negative caps are malformed.
        zero = budget_status(0.0, 0, 80)
        self.assertTrue(zero["exceeded"])
        self.assertFalse(zero["unlimited"])
        self.assertFalse(zero["paid_calls_allowed"])
        with self.assertRaises(ValueError):
            budget_status(0.0, -1, 80)

    def test_api_status_counts_active_and_settled_reservations(self):
        config = {"budgets": {"daily_llm_usd": 2.0, "warn_at_pct": 80}}
        with (
            patch(
                "budgets.query_one",
                side_effect=[
                    {"total_cost": 1.0, "total_tokens": 10},
                    {"spent_usd": 0.5, "reserved_usd": 0.75},
                ],
            ) as query,
        ):
            status = get_budget_status(config)
        self.assertEqual(status["reserved_usd"], 0.75)
        self.assertEqual(status["unreserved_spend_usd"], 0.5)
        self.assertEqual(status["committed_usd"], 1.25)
        self.assertTrue(status["paid_calls_allowed"])
        admission_sql = query.call_args_list[1].args[0]
        self.assertIn("status = 'active'", admission_sql)
        self.assertIn("status = 'settled'", admission_sql)
        self.assertIn("GREATEST", admission_sql)
        self.assertIn("LEFT JOIN", admission_sql)

    def test_api_reports_lookup_failure_as_unavailable_not_zero(self):
        config = {"budgets": {"daily_llm_usd": 2.0, "warn_at_pct": 80}}
        for spend_result in (
            RuntimeError("secret db detail"),
            (math.nan, 10),
            (math.inf, 10),
        ):
            with (
                self.subTest(spend_result=spend_result),
                patch(
                    "budgets.get_today_spend",
                    side_effect=spend_result
                    if isinstance(spend_result, Exception)
                    else None,
                    return_value=None
                    if isinstance(spend_result, Exception)
                    else spend_result,
                ),
            ):
                status = get_budget_status(config)
            self.assertFalse(status["available"])
            self.assertEqual(status["status"], "unavailable")
            self.assertIsNone(status["today_cost_usd"])
            self.assertNotIn("secret", str(status).lower())

    def test_invalid_cap_is_reported_unavailable_and_never_unlimited(self):
        for cap in ("bad", None, math.nan, math.inf, -math.inf, True, -1, -0.01):
            with (
                self.subTest(cap=cap),
                patch("budgets.get_today_spend", return_value=(0, 0)),
            ):
                status = get_budget_status(
                    {"budgets": {"daily_llm_usd": cap, "warn_at_pct": 80}}
                )
                self.assertFalse(status["available"])
                self.assertFalse(status["unlimited"])
                self.assertEqual(status["status"], "invalid_config")


if __name__ == "__main__":
    unittest.main()
