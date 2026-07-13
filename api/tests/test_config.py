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
                "required: ${REQUIRED_VALUE}\n"
                "defaulted: ${ABSENT_VALUE:-fallback}\n"
                "explicit_empty: ${EMPTY_VALUE:-fallback}\n"
            )
            with patch.dict(
                os.environ,
                {"REQUIRED_VALUE": "configured", "EMPTY_VALUE": ""},
                clear=True,
            ):
                config = _load_config(str(config_path))

        self.assertEqual(config["required"], "configured")
        self.assertEqual(config["defaulted"], "fallback")
        self.assertEqual(config["explicit_empty"], "")

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
            "DB_PASSWORD": "password",
            "FRED_API_KEY": "configured-fred",
            "OPENROUTER_API_KEY": "configured-openrouter",
            "OPENROUTER_MODEL": "provider/model",
            "OANDA_API_KEY": "configured-oanda",
            "TWITTERAPI_KEY": "",
            "DASHBOARD_USER": "admin",
            "DASHBOARD_PASSWORD": "password",
        }
        with patch.dict(os.environ, env, clear=True):
            config = _load_config(str(config_path))

        self.assertEqual(config["kobeissi"]["api_key"], "")


if __name__ == "__main__":
    unittest.main()
