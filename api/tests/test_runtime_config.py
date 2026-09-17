import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure api/ is on sys.path so "import main" resolves here.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["STATE_DIR"] = "/tmp/test_runtime_config_state"
os.environ["DEPLOYMENT_MODE"] = "test"  # bypass signing-key startup validation

MOCK_CONFIG = {
    "database": {
        "host": "localhost",
        "port": 5432,
        "name": "test",
        "user": "test",
        "password": "test",
    },
    "collectors": {},
    "processors": {},
}

# Import the app with a patched loader so create_app() sees a valid config.
with patch("contracts.runtime_config.load_config", return_value=MOCK_CONFIG):
    from main import create_app  # noqa: E402
from contracts.runtime_config import ConfigError, RuntimeConfigLoader  # noqa: E402


def _write_config(path: Path, extra: str = "", database: str | None = None) -> None:
    db = database or (
        "database:\n"
        "  host: localhost\n"
        "  port: 5432\n"
        "  name: test\n"
        "  user: runtime-user\n"
        "  password: correct-horse-battery\n"
    )
    path.write_text(f"{db}{extra}")


def _load_local(path: Path):
    """Load an isolated sample without replacing the process shared loader."""
    return RuntimeConfigLoader().load(str(path))


class ConfigValidationTests(unittest.TestCase):
    def test_unknown_top_level_key_raises_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(path, extra="unknown_section: 1\n")
            with self.assertRaises(ConfigError):
                _load_local(path)

    def test_unknown_nested_key_raises_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(path, extra="budgets:\n  not_a_budget_key: 1\n")
            with self.assertRaises(ConfigError):
                _load_local(path)

    def test_operator_config_cannot_retarget_orchestrator(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(
                path,
                extra="api:\n  orchestrator_url: http://attacker.test\n",
            )
            with self.assertRaisesRegex(ConfigError, "orchestrator_url"):
                _load_local(path)

    def test_port_out_of_range_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(
                path,
                database=(
                    "database:\n"
                    "  host: localhost\n"
                    "  port: 99999\n"
                    "  name: test\n"
                    "  user: runtime-user\n"
                    "  password: correct-horse-battery\n"
                ),
            )
            with self.assertRaisesRegex(ConfigError, "database.port"):
                _load_local(path)

    def test_numeric_port_string_is_coerced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(
                path,
                database=(
                    "database:\n"
                    "  host: localhost\n"
                    '  port: "5432"\n'
                    "  name: test\n"
                    "  user: runtime-user\n"
                    "  password: correct-horse-battery\n"
                ),
            )
            config = _load_local(path)
            self.assertEqual(config["database"]["port"], 5432)

    def test_budget_range_is_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(path, extra="budgets:\n  warn_at_pct: 150\n")
            with self.assertRaisesRegex(ConfigError, "budgets.warn_at_pct"):
                _load_local(path)

    def test_bad_logging_level_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(path, extra="logging:\n  level: LOUD\n")
            with self.assertRaisesRegex(ConfigError, "logging.level"):
                _load_local(path)

    def test_llm_key_required_outside_demo(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(
                path,
                extra=('llm:\n  provider: openrouter\n  api_key: ""\n'),
            )
            with self.assertRaisesRegex(ConfigError, "llm.api_key"):
                _load_local(path)

    def test_missing_database_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("logging:\n  level: INFO\n")
            with self.assertRaises(ConfigError):
                _load_local(path)

    def test_market_state_strict_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(
                path,
                extra=(
                    "market_state:\n"
                    "  lookback: {value: 3, unit: hours}\n"
                    "  state_thresholds: {trend_slope_epsilon: 0.01, high_volatility_threshold: 0.03}\n"
                    "  baskets: {global_risk: [EURUSD, AUDJPY]}\n"
                    "  yield_curves: {us_10y_2y: [DGS10, DGS2]}\n"
                ),
            )
            config = _load_local(path)
            market_state = config["market_state"]
            self.assertEqual(market_state.lookback.value, 3)
            self.assertEqual(market_state.lookback.unit, "hours")
            self.assertEqual(market_state.state_thresholds.trend_slope_epsilon, 0.01)
            self.assertEqual(market_state.baskets["global_risk"], ["EURUSD", "AUDJPY"])

    def test_market_state_rejects_unknown_key_and_bad_yield_curve(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_key = Path(tmp) / "bad_key.yaml"
            _write_config(
                bad_key, extra="market_state:\n  realized_volatility_window: 30\n"
            )
            with self.assertRaisesRegex(ConfigError, "market_state"):
                _load_local(bad_key)
            bad_curve = Path(tmp) / "bad_curve.yaml"
            _write_config(
                bad_curve,
                extra="market_state:\n  yield_curves: {us_10y_2y: [DGS10]}\n",
            )
            with self.assertRaisesRegex(ConfigError, "yield_curves"):
                _load_local(bad_curve)

    def test_loaded_config_behaves_as_a_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(path, extra="collectors:\n  fred:\n    enabled: true\n")
            config = _load_local(path)
            self.assertEqual(config["database"]["user"], "runtime-user")
            self.assertEqual(
                config.get("collectors", {}).get("fred", {}).get("enabled"), True
            )
            self.assertEqual(config.get("missing", "fallback"), "fallback")
            self.assertIn("database", config)
            self.assertEqual(
                dict(config.get("collectors", {}).get("fred", {}))["enabled"], True
            )

    def test_loaded_config_feeds_real_budget_consumer(self):
        """A validated snapshot must satisfy real config consumers end to end."""
        from api_budgets import get_budget_config

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(
                path,
                extra=("budgets:\n  daily_llm_usd: 3.5\n  warn_at_pct: 90\n"),
            )
            config = _load_local(path)
            cap, warn_at = get_budget_config(config)
            self.assertEqual((cap, warn_at), (3.5, 90.0))


class VersionAndRestartTests(unittest.TestCase):
    def _config_with_llm(self, tmp: str, model: str) -> Path:
        path = Path(tmp) / "config.yaml"
        _write_config(
            path,
            extra=f"llm:\n  api_key: k\n  models:\n    default: {model}\n",
        )
        return path

    def test_version_is_stable_for_immutable_process_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            operator = Path(tmp) / "operator.yaml"
            secrets = Path(tmp) / "secrets.env"
            with patch.dict(
                os.environ,
                {
                    "OPERATOR_CONFIG": str(operator),
                    "SECRETS_FILE": str(secrets),
                    "OPENROUTER_API_KEY": "k",
                },
                clear=True,
            ):
                path = self._config_with_llm(tmp, "model-a")
                loader = RuntimeConfigLoader()
                first = loader.load(str(path))
                first_version = loader.config_version()
                self.assertIsNotNone(first_version)
                self.assertIs(loader.load(str(path)), first)
                snapshot = loader.config_snapshot()
                self.assertIsNotNone(snapshot)
                self.assertEqual(snapshot.version, first_version)

    def test_model_change_is_immutable_until_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            operator = Path(tmp) / "operator.yaml"
            secrets = Path(tmp) / "secrets.env"
            with patch.dict(
                os.environ,
                {
                    "OPERATOR_CONFIG": str(operator),
                    "SECRETS_FILE": str(secrets),
                    "OPENROUTER_API_KEY": "k",
                },
                clear=True,
            ):
                path = self._config_with_llm(tmp, "model-a")
                loader = RuntimeConfigLoader()
                first = loader.load(str(path))
                operator.write_text("llm:\n  models:\n    default: model-b\n")
                self.assertIs(loader.load(str(path)), first)
                self.assertEqual(first["llm"]["models"]["default"], "model-a")
                self.assertTrue(loader.restart_required())
                self.assertNotEqual(loader.config_version(), None)

    def test_database_change_requires_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            operator = Path(tmp) / "operator.yaml"
            secrets = Path(tmp) / "secrets.env"
            with patch.dict(
                os.environ,
                {
                    "OPERATOR_CONFIG": str(operator),
                    "SECRETS_FILE": str(secrets),
                    "OPENROUTER_API_KEY": "k",
                },
                clear=True,
            ):
                path = self._config_with_llm(tmp, "model-a")
                loader = RuntimeConfigLoader()
                loader.load(str(path))
                operator.write_text("database:\n  host: other-host\n")
                loader.load(str(path))
                self.assertTrue(loader.restart_required())


class SecretHandlingTests(unittest.TestCase):
    def test_secrets_are_immutable_until_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            operator = tmp / "operator.yaml"
            secrets = tmp / "secrets.env"
            path = tmp / "config.yaml"
            path.write_text(
                "database:\n"
                "  host: localhost\n"
                "  port: 5432\n"
                "  name: test\n"
                "  user: runtime-user\n"
                "  password: correct-horse-battery\n"
                "llm:\n"
                "  api_key: ${OPENROUTER_API_KEY}\n"
            )
            with patch.dict(
                os.environ,
                {
                    "OPERATOR_CONFIG": str(operator),
                    "SECRETS_FILE": str(secrets),
                    "OPENROUTER_API_KEY": "env-key",
                },
                clear=True,
            ):
                loader = RuntimeConfigLoader()
                self.assertEqual(loader.load(str(path)).llm.api_key, "env-key")
                secrets.write_text("OPENROUTER_API_KEY=file-key\n")
                with self.assertRaisesRegex(ConfigError, "restart required"):
                    loader.load(str(path))
                # A new process loader sees the committed replacement.
                replacement_loader = RuntimeConfigLoader()
                self.assertEqual(
                    replacement_loader.load(str(path)).llm.api_key, "file-key"
                )
                self.assertEqual(os.environ["OPENROUTER_API_KEY"], "env-key")

    def test_deleted_secret_fails_closed_until_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            operator = tmp / "operator.yaml"
            secrets = tmp / "secrets.env"
            path = tmp / "config.yaml"
            path.write_text(
                "database:\n"
                "  host: localhost\n"
                "  port: 5432\n"
                "  name: test\n"
                "  user: runtime-user\n"
                "  password: correct-horse-battery\n"
                "kobeissi:\n"
                "  api_key: ${TWITTERAPI_KEY:-}\n"
            )
            with patch.dict(
                os.environ,
                {
                    "OPERATOR_CONFIG": str(operator),
                    "SECRETS_FILE": str(secrets),
                    "TWITTERAPI_KEY": "",
                },
                clear=True,
            ):
                secrets.write_text("TWITTERAPI_KEY=deleted-later\n")
                loader = RuntimeConfigLoader()
                self.assertEqual(
                    loader.load(str(path)).kobeissi.api_key, "deleted-later"
                )
                secrets.write_text("")
                with self.assertRaisesRegex(ConfigError, "restart required"):
                    loader.load(str(path))

    def test_malformed_managed_secrets_never_fall_back_to_cached_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            operator = tmp / "operator.yaml"
            secrets = tmp / "secrets.env"
            path = tmp / "config.yaml"
            path.write_text(
                "database:\n"
                "  host: localhost\n"
                "  port: 5432\n"
                "  name: test\n"
                "  user: runtime-user\n"
                "  password: correct-horse-battery\n"
                "llm:\n"
                "  api_key: ${OPENROUTER_API_KEY}\n"
            )
            with patch.dict(
                os.environ,
                {
                    "OPERATOR_CONFIG": str(operator),
                    "SECRETS_FILE": str(secrets),
                },
                clear=True,
            ):
                secrets.write_text("OPENROUTER_API_KEY=live-key\n")
                loader = RuntimeConfigLoader()
                self.assertEqual(loader.load(str(path)).llm.api_key, "live-key")
                secrets.write_text("malformed-secret-line\n")
                with self.assertRaisesRegex(ConfigError, "restart required"):
                    loader.load(str(path))
                # Repeated reads remain fail-closed rather than serving keys
                # from the startup snapshot.
                with self.assertRaises(ConfigError):
                    loader.load(str(path))


class DatabaseUrlTests(unittest.TestCase):
    def test_database_url_escapes_special_credentials(self):
        import api_db

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(
                path,
                database=(
                    "database:\n"
                    "  host: db.example.com\n"
                    "  port: 5432\n"
                    "  name: trading_data\n"
                    "  user: trader\n"
                    "  password: p@ss:w/rd#x\n"
                ),
            )
            config = _load_local(path)
            with (
                patch.object(api_db, "_engine", None),
                patch.object(api_db, "_engine_url", None),
                patch.object(api_db, "_SessionFactory", None),
            ):
                engine = api_db.get_engine(config)
                rendered = engine.url.render_as_string(hide_password=False)
                self.assertIn("%40", rendered)  # '@' escaped
                self.assertIn("%2F", rendered)  # '/' escaped
                self.assertEqual(engine.url.password, "p@ss:w/rd#x")
                self.assertEqual(engine.url.username, "trader")
                self.assertEqual(engine.url.host, "db.example.com")
                self.assertEqual(engine.url.database, "trading_data")
                engine.dispose()

    def test_database_cache_switches_atomically_when_profile_url_changes(self):
        import api_db

        first_config = {
            "database": {
                "host": "db-one",
                "port": 5432,
                "name": "one",
                "user": "trader",
                "password": "secret",
            }
        }
        second_config = {
            "database": {
                "host": "db-two",
                "port": 5432,
                "name": "two",
                "user": "trader",
                "password": "secret",
            }
        }
        with (
            patch.object(api_db, "_engine", None),
            patch.object(api_db, "_engine_url", None),
            patch.object(api_db, "_SessionFactory", None),
        ):
            first = api_db.get_engine(first_config)
            first_factory = api_db._SessionFactory
            with patch.object(first, "dispose") as dispose:
                second = api_db.get_engine(second_config)

            self.assertIsNot(second, first)
            self.assertEqual(second.url.host, "db-two")
            self.assertEqual(second.url.database, "two")
            self.assertIsNot(api_db._SessionFactory, first_factory)
            dispose.assert_called_once_with()
            second.dispose()


class HealthEndpointTests(unittest.TestCase):
    def make_client(self):
        app = create_app()
        from fastapi.testclient import TestClient

        return TestClient(app)

    def test_live_is_always_ok(self):
        with self.make_client() as client:
            response = client.get("/live")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_ready_returns_503_when_database_unavailable(self):
        with self.make_client() as client:
            with patch("main.check_connection", return_value=False):
                with patch(
                    "contracts.runtime_config.load_config", return_value=MOCK_CONFIG
                ):
                    response = client.get("/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["dependencies"]["database"], "unavailable")

    def test_ready_handles_database_check_exception_as_503(self):
        """A raising DB check must be a bounded 503 dependency state, not a 500."""
        with self.make_client() as client:
            with patch(
                "main.check_connection",
                side_effect=RuntimeError("RAW_DB_EXCEPTION"),
            ):
                with patch(
                    "contracts.runtime_config.load_config", return_value=MOCK_CONFIG
                ):
                    response = client.get("/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "unready")
        self.assertEqual(response.json()["dependencies"]["database"], "unavailable")
        self.assertNotIn("RAW_DB_EXCEPTION", response.text)

    def test_ready_returns_200_when_all_dependencies_pass(self):
        with self.make_client() as client:
            with patch("main.check_connection", return_value=True):
                with patch(
                    "contracts.runtime_config.load_config", return_value=MOCK_CONFIG
                ):
                    response = client.get("/ready")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["dependencies"]["database"], "ok")


if __name__ == "__main__":
    unittest.main()
