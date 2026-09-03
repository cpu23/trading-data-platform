import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import load_config as _load_config


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
                config = _load_config(str(config_path))

        self.assertEqual(config["database"]["user"], "configured")
        self.assertEqual(
            config["database"]["password"], "correct-horse-battery-staple"
        )
        self.assertEqual(config["kobeissi"]["api_key"], "")

    def test_env_substitution_names_absent_required_variable(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text("required: ${TRULY_REQUIRED}\n")
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "TRULY_REQUIRED"):
                    _load_config(str(config_path))

    def test_env_substitution_rejects_blank_required_variable(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text("required: ${BLANK_REQUIRED}\n")
            with patch.dict(os.environ, {"BLANK_REQUIRED": ""}, clear=True):
                with self.assertRaisesRegex(ValueError, "BLANK_REQUIRED"):
                    _load_config(str(config_path))

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
            "DASHBOARD_USER": "admin",
            "DASHBOARD_PASSWORD": "correct-horse-battery-staple",
        }
        with patch.dict(os.environ, env, clear=True):
            config = _load_config(str(config_path))

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
            "DASHBOARD_USER": "admin",
            "DASHBOARD_PASSWORD": "correct-horse-battery-staple",
        }
        with patch.dict(os.environ, env, clear=True):
            default_config = _load_config(str(config_path))
        self.assertEqual(default_config["logging"]["level"], "INFO")

        # A distinct path bypasses the production config cache and remains
        # isolated from the module-level load_config patches in route tests.
        with tempfile.TemporaryDirectory() as tmp:
            debug_path = Path(tmp) / "config.yaml"
            debug_path.write_text(config_path.read_text())
            with patch.dict(os.environ, {**env, "LOG_LEVEL": "DEBUG"}, clear=True):
                debug_config = _load_config(str(debug_path))
        self.assertEqual(debug_config["logging"]["level"], "DEBUG")


if __name__ == "__main__":
    unittest.main()
