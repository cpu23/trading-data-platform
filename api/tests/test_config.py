import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from contracts.runtime_config import RuntimeConfigLoader


def _load_local(path: Path):
    """Load an isolated sample without replacing the process shared loader."""
    return RuntimeConfigLoader().load(str(path))


class ConfigLoadingTests(unittest.TestCase):
    def test_env_substitution_supports_defaults_and_explicit_empty_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(
                "database:\n"
                "  host: localhost\n"
                "  port: 5432\n"
                "  name: trading_data\n"
                "  user: ${REQUIRED_VALUE}\n"
                "  password: ${ABSENT_VALUE:-correct-horse-battery-staple}\n"
                "kobeissi:\n"
                "  api_key: ${EMPTY_VALUE:-fallback}\n"
            )
            with patch.dict(
                os.environ,
                {"REQUIRED_VALUE": "configured", "EMPTY_VALUE": ""},
                clear=True,
            ):
                config = _load_local(config_path)

        self.assertEqual(config["database"]["user"], "configured")
        self.assertEqual(config["database"]["password"], "correct-horse-battery-staple")
        self.assertEqual(config["kobeissi"]["api_key"], "")

    def test_env_substitution_names_absent_required_variable(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text("required: ${TRULY_REQUIRED}\n")
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "TRULY_REQUIRED"):
                    _load_local(config_path)

    def test_env_substitution_rejects_blank_required_variable(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text("required: ${BLANK_REQUIRED}\n")
            with patch.dict(os.environ, {"BLANK_REQUIRED": ""}, clear=True):
                with self.assertRaisesRegex(ValueError, "BLANK_REQUIRED"):
                    _load_local(config_path)

    def test_production_config_preserves_empty_optional_twitter_key(self):
        config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
        env = {
            "DB_USER": "trading",
            "DB_PASSWORD": "correct-horse-battery-staple",
            "FRED_API_KEY": "configured-fred",
            "OPENROUTER_API_KEY": "configured-openrouter",
            "OPENROUTER_MODEL": "provider/model",
            "OANDA_API_KEY": "configured-oanda",
            "TWITTERAPI_KEY": "",
        }
        with patch.dict(os.environ, env, clear=True):
            config = _load_local(config_path)

        self.assertEqual(config["kobeissi"]["api_key"], "")

    def test_production_logging_defaults_to_info_stdout_and_allows_log_level_override(
        self,
    ):
        config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
        env = {
            "DB_USER": "trading",
            "DB_PASSWORD": "correct-horse-battery-staple",
            "FRED_API_KEY": "configured-fred",
            "OPENROUTER_API_KEY": "configured-openrouter",
            "OPENROUTER_MODEL": "provider/model",
            "OANDA_API_KEY": "configured-oanda",
            "TWITTERAPI_KEY": "",
        }
        with patch.dict(os.environ, env, clear=True):
            default_config = _load_local(config_path)
        self.assertEqual(default_config["logging"]["level"], "INFO")

        with tempfile.TemporaryDirectory() as tmp:
            debug_path = Path(tmp) / "config.yaml"
            debug_path.write_text(config_path.read_text())
            with patch.dict(os.environ, {**env, "LOG_LEVEL": "DEBUG"}, clear=True):
                debug_config = _load_local(debug_path)
        self.assertEqual(debug_config["logging"]["level"], "DEBUG")

    def test_demo_mode_transform_disables_all_external_scheduled_workloads(self):
        config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
        env = {
            "DEMO_MODE": "1",
            "DEPLOYMENT_MODE": "demo",
            "DB_USER": "trading",
            "DB_PASSWORD": "correct-horse-battery-staple",
        }
        with patch.dict(os.environ, env, clear=True):
            config = _load_local(config_path)

        self.assertTrue(config["demo"]["enabled"])
        self.assertFalse(config["investment_filings"]["enabled"])
        self.assertIsNone(config["investment_filings"]["schedule"])
        self.assertFalse(config["investment_filings"]["run_on_startup"])
        self.assertFalse(config["research_intelligence"]["schedule_enabled"])
        self.assertFalse(config["thesis_autonomy"]["schedule_enabled"])
        self.assertFalse(config["reuters"]["enabled"])
        self.assertFalse(config["reuters"]["schedule_enabled"])
        self.assertFalse(config["kobeissi"]["enabled"])
        self.assertFalse(config["kobeissi"]["schedule_enabled"])
        for collector in config["collectors"].values():
            self.assertFalse(collector["enabled"])
        for processor in config["processors"].values():
            self.assertFalse(processor["enabled"])


if __name__ == "__main__":
    unittest.main()
