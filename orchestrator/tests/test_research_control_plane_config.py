import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DEPLOYMENT_MODE", "test")

from contracts.runtime_config import AppConfig, ResearchControlPlaneConfig  # noqa: E402


class ResearchControlPlaneConfigTests(unittest.TestCase):
    def test_defaults_are_bounded_and_model_budget_is_global_cap_bounded(self):
        settings = ResearchControlPlaneConfig()

        self.assertTrue(settings.enabled)
        self.assertEqual(settings.planning_interval_minutes, 15)
        self.assertEqual(settings.event_debounce_seconds, 120)
        self.assertEqual(settings.maximum_questions_per_plan, 20)
        self.assertEqual(settings.maximum_work_orders_per_plan, 8)
        self.assertEqual(settings.maximum_runtime_seconds_per_plan, 900)
        self.assertEqual(settings.model_budget_usd_per_plan, 1.0)
        self.assertEqual(settings.priority_policy_version, "v1")
        self.assertEqual(settings.materiality_policy_version, "v1")

        with self.assertRaisesRegex(ValueError, "model_budget_usd_per_plan"):
            AppConfig.model_validate(
                {
                    "database": {
                        "host": "localhost",
                        "port": 5432,
                        "name": "test",
                        "user": "test",
                        "password": "test-password-value",
                    },
                    "thesis_autonomy": {"model_budget_usd_per_run": 0.4},
                    "budgets": {"daily_llm_usd": 0.5},
                    "research_control_plane": {"model_budget_usd_per_plan": 0.75},
                }
            )

    def test_unknown_and_out_of_range_values_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "extra_forbidden"):
            ResearchControlPlaneConfig.model_validate({"unbounded_history": True})
        for values in (
            {"planning_interval_minutes": 0},
            {"event_debounce_seconds": -1},
            {"maximum_questions_per_plan": 0},
            {"maximum_work_orders_per_plan": 101},
            {"maximum_runtime_seconds_per_plan": 0},
            {"model_budget_usd_per_plan": -0.01},
            {"minimum_priority": float("nan")},
        ):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    ResearchControlPlaneConfig.model_validate(values)


if __name__ == "__main__":
    unittest.main()
