import os
import sys
import tempfile
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

    def test_config_env_substitution_supports_defaults_and_explicit_empty_values(self):
        from config_loader import reload_config

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(
                "required: ${REQUIRED_VALUE}\n"
                "defaulted: ${ABSENT_VALUE:-fallback}\n"
                "explicit_empty: ${EMPTY_VALUE:-fallback}\n"
            )
            with patch.dict(
                os.environ,
                {"REQUIRED_VALUE": "configured", "EMPTY_VALUE": ""},
                clear=True,
            ):
                config = reload_config(str(config_path))

        self.assertEqual(config["required"], "configured")
        self.assertEqual(config["defaulted"], "fallback")
        self.assertEqual(config["explicit_empty"], "")

    def test_config_env_substitution_names_missing_required_variable(self):
        from config_loader import reload_config

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text("required: ${TRULY_REQUIRED}\n")
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "TRULY_REQUIRED"):
                    reload_config(str(config_path))

    def test_demo_config_loads_without_twitter_credential(self):
        from config_loader import reload_config

        config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
        demo_env = {
            "DB_USER": "demo",
            "DB_PASSWORD": "demo",
            "FRED_API_KEY": "demo-disabled",
            "OPENROUTER_API_KEY": "demo-disabled",
            "OPENROUTER_MODEL": "demo/model",
            "OANDA_API_KEY": "demo-disabled",
            "DASHBOARD_USER": "demo",
            "DASHBOARD_PASSWORD": "demo",
            "DEMO_MODE": "true",
        }
        with patch.dict(os.environ, demo_env, clear=True):
            config = reload_config(str(config_path))

        self.assertTrue(config["demo"]["enabled"])
        self.assertEqual(config["kobeissi"]["api_key"], "")
        self.assertEqual(config["database"]["name"], "trading_data")

    def test_enabled_production_source_missing_credential_fails_closed(self):
        from config_loader import reload_config

        config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
        production_env = {
            "DB_USER": "trading",
            "DB_PASSWORD": "password",
            "OPENROUTER_API_KEY": "key",
            "OPENROUTER_MODEL": "provider/model",
            "OANDA_API_KEY": "key",
            "TWITTERAPI_KEY": "",
            "DASHBOARD_USER": "admin",
            "DASHBOARD_PASSWORD": "password",
        }
        with patch.dict(os.environ, production_env, clear=True):
            with self.assertRaisesRegex(ValueError, "FRED_API_KEY"):
                reload_config(str(config_path))

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
