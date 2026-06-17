import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.oanda import OandaCollector
from llm_client import resolve_model
from orchestrator import update_run_progress
from scheduler import scheduler_status, start_scheduler, stop_scheduler


class RuntimeFeatureTests(unittest.TestCase):
    def tearDown(self):
        stop_scheduler()

    def test_model_resolution_is_provider_model_agnostic(self):
        config = {
            "llm": {
                "default_model": "deepseek/deepseek-v4-flash",
                "models": {"briefing": "provider/custom-model"},
            }
        }
        self.assertEqual(resolve_model(config), "deepseek/deepseek-v4-flash")
        self.assertEqual(
            resolve_model(config, processor_id="briefing"),
            "provider/custom-model",
        )
        self.assertEqual(
            resolve_model(config, processor_id="briefing", model="explicit/model"),
            "explicit/model",
        )

    @patch("collectors.oanda.make_request")
    def test_oanda_filters_unsupported_instruments(self, make_request):
        response = Mock()
        response.json.return_value = {"instruments": [{"name": "EUR_USD"}]}
        response.raise_for_status.return_value = None
        make_request.return_value = response

        result = OandaCollector()._filter_supported_instruments(
            "https://api-fxpractice.oanda.com",
            "token",
            "account",
            [
                {"symbol": "EURUSD", "oanda_instrument": "EUR_USD"},
                {"symbol": "OLD", "oanda_instrument": "OLD_NAME"},
            ],
            "correlation",
        )

        self.assertEqual([item["symbol"] for item in result], ["EURUSD"])
        self.assertTrue(make_request.call_args.kwargs["follow_redirects"])

    def test_scheduler_registers_configured_cron_jobs(self):
        config = {
            "collectors": {"fred": {"enabled": True, "schedule": "0 6 * * *"}},
            "processors": {
                "briefing": {"enabled": True, "schedule": "0 7 * * *"},
                "macro_regime": {"enabled": True, "schedule": "after_dependency"},
            },
        }

        start_scheduler(config)
        ids = {job["id"] for job in scheduler_status()["jobs"]}

        self.assertEqual(ids, {"collector:fred", "processor:briefing"})

    def test_demo_fixture_is_public_safe_and_deterministic(self):
        seed_path = (
            Path(__file__).resolve().parents[2]
            / "db"
            / "demo"
            / "900_demo_seed.sql"
        )
        if not seed_path.exists():
            self.skipTest("Demo fixtures are intentionally public-repository only")
        seed = seed_path.read_text()

        self.assertIn("Fictional deterministic fixtures", seed)
        self.assertIn("77777777-7777-4777-8777-777777777777", seed)
        self.assertNotIn("OPENROUTER_API_KEY", seed)

    @patch("orchestrator.get_session")
    def test_cycle_progress_is_persisted_while_run_is_active(self, get_session):
        session = Mock()
        get_session.return_value.__enter__.return_value = session

        update_run_progress(
            "run-id",
            {"current_stage": "fred", "completed_stages": 0, "total_stages": 3},
            {},
        )

        params = session.execute.call_args.args[1]
        self.assertEqual(params["cid"], "run-id")
        self.assertIn('"current_stage": "fred"', params["summary"])


if __name__ == "__main__":
    unittest.main()
