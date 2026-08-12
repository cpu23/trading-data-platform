import base64
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DASHBOARD_USER", "internal-user")
os.environ.setdefault("DASHBOARD_PASSWORD", "internal-pass")
os.environ.setdefault("DEPLOYMENT_MODE", "test")
INTERNAL_AUTH = {
    "Authorization": "Basic "
    + base64.b64encode(b"internal-user:internal-pass").decode()
}

from config_loader import (  # noqa: E402
    ConfigError,
    config_version,
    load_config,
    reload_config,
    restart_required,
    restart_sensitive_changes,
)
from contracts.runtime_config import ConfigStore, committed_config_paths  # noqa: E402
from data_quality import evaluate_quality, required_quality_checks  # noqa: E402


def _write_config(path: Path, extra: str = "") -> None:
    path.write_text(
        "database:\n"
        "  host: localhost\n"
        "  port: 5432\n"
        "  name: test\n"
        "  user: runtime-user\n"
        "  password: correct-horse-battery\n"
        f"{extra}"
    )


class ConfigLoaderValidationTests(unittest.TestCase):
    def test_unknown_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(path, extra="mystery_section: 1\n")
            with self.assertRaises(ConfigError):
                load_config(str(path))

    def test_semantic_validation_reports_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(path, extra="orchestration:\n  collector_workers: 0\n")
            with self.assertRaisesRegex(ConfigError, "collector_workers"):
                load_config(str(path))

    def test_demo_transform_disables_investment_filing_jobs(self):
        from config_loader import _demo_transform

        raw = {
            "collectors": {},
            "processors": {},
            "investment_filings": {
                "enabled": True,
                "schedule": "0 8 * * 1-5",
                "run_on_startup": True,
            },
        }
        with patch.dict(os.environ, {"DEMO_MODE": "1"}, clear=False):
            _demo_transform(raw)
        self.assertFalse(raw["investment_filings"]["enabled"])
        self.assertIsNone(raw["investment_filings"]["schedule"])
        self.assertFalse(raw["investment_filings"]["run_on_startup"])

    def test_loaded_config_supports_existing_call_sites(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            _write_config(
                path,
                extra=(
                    "collectors:\n"
                    "  fred:\n"
                    "    enabled: true\n"
                    "    api_key: real-fred-key\n"
                    "    series:\n"
                    "      - id: DGS10\n"
                    "        frequency: daily\n"
                    "processors:\n"
                    "  briefing:\n"
                    "    enabled: false\n"
                ),
            )
            config = load_config(str(path))
            self.assertEqual(
                config.get("collectors", {}).get("fred", {}).get("enabled"), True
            )
            self.assertEqual(
                config.get("processors", {}).get("briefing", {}).get("enabled"), False
            )
            self.assertEqual(
                config.get("collectors", {}).get("fred", {}).get("series", [])[0]["id"],
                "DGS10",
            )
            self.assertEqual(dict(config["database"])["host"], "localhost")

    def test_version_and_restart_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            operator = tmp / "operator.yaml"
            secrets = tmp / "secrets.env"
            path = tmp / "config.yaml"
            _write_config(path)
            with patch.dict(
                os.environ,
                {
                    "OPERATOR_CONFIG": str(operator),
                    "SECRETS_FILE": str(secrets),
                },
                clear=True,
            ):
                load_config(str(path))
                self.assertIsNotNone(config_version())
                self.assertEqual(config_version(), config_version())
                # Scheduler-fed change (collector toggle) requires restart:
                # APScheduler captured the job definitions at startup.
                operator.write_text("collectors:\n  fred:\n    enabled: false\n")
                reload_config(str(path))
                self.assertTrue(restart_required())
                self.assertIn("collectors", restart_sensitive_changes())
                self.assertNotEqual(config_version(), None)
                # A later live-only change must not erase the pending restart
                # while the restart-sensitive collector delta remains active.
                operator.write_text(
                    "collectors:\n"
                    "  fred:\n"
                    "    enabled: false\n"
                    "logging:\n"
                    "  level: DEBUG\n"
                )
                reload_config(str(path))
                self.assertTrue(restart_required())
                self.assertIn("collectors", restart_sensitive_changes())
                # Restart-sensitive change (database): restart required.
                operator.write_text("database:\n  host: other-host\n")
                reload_config(str(path))
                self.assertTrue(restart_required())
                self.assertIn("database", restart_sensitive_changes())


class ConfigStoreConcurrencyTests(unittest.TestCase):
    def test_store_serializes_concurrent_reload_state_transitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "config.yaml"
            operator = root / "operator.yaml"
            secrets = root / "secrets.env"
            _write_config(path)
            store = ConfigStore()
            start = threading.Barrier(3)
            counter_lock = threading.Lock()
            active = 0
            max_active = 0
            errors = []

            def parse(candidate: str):
                nonlocal active, max_active
                with counter_lock:
                    active += 1
                    max_active = max(max_active, active)
                try:
                    time.sleep(0.05)
                    return yaml.safe_load(Path(candidate).read_text())
                finally:
                    with counter_lock:
                        active -= 1

            def run():
                start.wait()
                try:
                    store.reload(
                        config_path=str(path),
                        operator_path=str(operator),
                        secrets_path=str(secrets),
                        parse=parse,
                    )
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            start.wait()
            for thread in threads:
                thread.join(timeout=2)

            self.assertFalse(errors)
            self.assertEqual(max_active, 1)
            self.assertEqual(store.status()["ordinal"], 2)

    def test_committed_paths_lock_root_before_resolving_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            version = state / "versions" / "v1"
            version.mkdir(parents=True)
            (version / "operator.yaml").write_text("")
            (version / "secrets.env").write_text("")
            (state / "current").symlink_to("versions/v1")
            (state / "operator.yaml").symlink_to("current/operator.yaml")
            (state / "secrets.env").symlink_to("current/secrets.env")

            with committed_config_paths(
                str(state / "operator.yaml"), str(state / "secrets.env")
            ) as paths:
                self.assertEqual(Path(paths[0]).parent, version)
                self.assertEqual(Path(paths[1]).parent, version)

            self.assertTrue((state / ".setup.lock").exists())
            self.assertFalse((version / ".setup.lock").exists())


class QualitySemanticsTests(unittest.TestCase):
    def test_missing_required_check_is_not_healthy(self):
        required = {"fred_DGS10_freshness", "fred_DGS10_gaps"}
        results = {"fred_DGS10_freshness": {"healthy": True, "detail": "fresh"}}
        self.assertEqual(evaluate_quality(results, required), "unknown")

    def test_empty_required_registry_is_unknown(self):
        self.assertEqual(evaluate_quality({}, set()), "unknown")
        self.assertEqual(evaluate_quality({"a": {"healthy": True}}, set()), "degraded")

    def test_unhealthy_check_degrades_overall(self):
        results = {"fred_DGS10_freshness": {"healthy": False, "detail": "stale"}}
        self.assertEqual(evaluate_quality(results, set(results)), "degraded")

    def test_all_required_present_and_healthy_is_healthy(self):
        results = {"fred_DGS10_freshness": {"healthy": True}}
        self.assertEqual(evaluate_quality(results, set(results)), "healthy")

    def test_required_checks_follow_enabled_sources(self):
        config = {
            "collectors": {
                "fred": {
                    "enabled": True,
                    "series": [{"id": "DGS10", "frequency": "daily"}],
                },
                "oanda": {"enabled": False},
            }
        }
        required = required_quality_checks(config)
        self.assertIn("fred_DGS10_freshness", required)
        self.assertIn("fred_DGS10_gaps", required)
        self.assertIn("fred_DGS10_anomalies", required)
        self.assertIn("forex_factory_freshness", required)  # default enabled
        self.assertNotIn("oanda_freshness", required)  # disabled -> optional

    def test_disabled_fred_without_series_falls_back_to_fixed_checks(self):
        config = {"collectors": {"fred": {"enabled": True, "series": []}}}
        required = required_quality_checks(config)
        self.assertIn("fred_freshness", required)

    def test_loaded_config_feeds_real_budget_and_quality_consumers(self):
        """A validated snapshot must satisfy real config consumers end to end."""
        from budgets import _reservation_policy, get_budget_config

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            operator = tmp / "operator.yaml"
            secrets = tmp / "secrets.env"
            path = tmp / "config.yaml"
            _write_config(
                path,
                extra=(
                    "budgets:\n"
                    "  daily_llm_usd: 4.0\n"
                    "  warn_at_pct: 75\n"
                    "  reservation_estimate_usd: 0.10\n"
                    "  reservation_ttl_seconds: 900\n"
                    "collectors:\n"
                    "  fred:\n"
                    "    enabled: true\n"
                    "    api_key: real-fred-key\n"
                    "    series:\n"
                    "      - id: DGS10\n"
                    "        frequency: daily\n"
                    "  oanda:\n"
                    "    enabled: false\n"
                ),
            )
            with patch.dict(
                os.environ,
                {
                    "OPERATOR_CONFIG": str(operator),
                    "SECRETS_FILE": str(secrets),
                },
                clear=True,
            ):
                config = load_config(str(path))
                self.assertEqual(get_budget_config(config), (4.0, 75.0))
                estimate, ttl = _reservation_policy(config, "briefing")
                self.assertEqual((estimate, ttl), (0.10, 900.0))
                required = required_quality_checks(config)
                self.assertIn("fred_DGS10_freshness", required)
                self.assertNotIn("oanda_freshness", required)


    def test_invalid_reload_retains_prior_snapshot_and_reports_status(self):
        """A rejected reload keeps the last valid config and never 500s."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            operator = tmp / "operator.yaml"
            secrets = tmp / "secrets.env"
            path = tmp / "config.yaml"
            _write_config(path)
            with patch.dict(
                os.environ,
                {
                    "OPERATOR_CONFIG": str(operator),
                    "SECRETS_FILE": str(secrets),
                },
                clear=True,
            ):
                config = load_config(str(path))
                version_before = config_version()
                operator.write_text("collectors:\n  fred:\n    enabled: [broken\n")
                self.assertIs(reload_config(str(path)), config)
                # Prior snapshot retained; ordinary consumers do not raise.
                self.assertEqual(config_version(), version_before)
                self.assertIs(load_config(str(path)), config)
                self.assertEqual(restart_sensitive_changes(), ["reload_failed"])
                self.assertTrue(restart_required())
                from config_loader import config_status

                status = config_status()
                self.assertTrue(status["last_reload"]["failed"])
                self.assertIn("YAML parse error", status["last_reload"]["error"])
                # Repairing the source clears the rejection and applies live.
                operator.write_text("")
                reload_config(str(path))
                self.assertEqual(restart_sensitive_changes(), [])

    def test_reload_forces_environment_only_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            operator = root / "operator.yaml"
            secrets = root / "secrets.env"
            path = root / "config.yaml"
            _write_config(
                path,
                extra=(
                    "llm:\n"
                    "  api_key: k\n"
                    "  models:\n"
                    "    default: ${MODEL_NAME}\n"
                ),
            )
            with patch.dict(
                os.environ,
                {
                    "OPERATOR_CONFIG": str(operator),
                    "SECRETS_FILE": str(secrets),
                    "MODEL_NAME": "provider/model-a",
                },
                clear=True,
            ):
                first = reload_config(str(path))
                os.environ["MODEL_NAME"] = "provider/model-b"
                self.assertIs(load_config(str(path)), first)
                second = reload_config(str(path))
                self.assertEqual(second.llm.models["default"], "provider/model-b")
                self.assertIsNot(second, first)

    def test_run_quality_checks_skips_disabled_sources(self):
        with patch("data_quality.get_session"):
            from data_quality import run_quality_checks

            results = run_quality_checks(
                {
                    "collectors": {
                        "fred": {"enabled": False, "series": []},
                        "forex_factory": {"enabled": False},
                        "oanda": {"enabled": False},
                    }
                }
            )
        self.assertEqual(results, {})


class LiveAndReadyEndpointTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        from main import app

        config = {
            "demo": {"enabled": False},
            "collectors": {
                "fred": {"enabled": False},
                "forex_factory": {"enabled": False},
                "oanda": {"enabled": False},
            },
            "event_pipeline": {
                "enabled": False,
                "outbox_worker_enabled": False,
                "jobs": {"enabled": False},
            },
        }
        self.config_patch = patch("main._get_config", return_value=config)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.client = TestClient(app, headers=INTERNAL_AUTH)

    def _healthy_heartbeat(self, role: str) -> dict:
        from datetime import UTC, datetime, timedelta

        status = "running" if role != "quotes" else "connected"
        return {
            "role": role,
            "status": status,
            "last_heartbeat_at": datetime.now(UTC) - timedelta(seconds=5),
            "detail": {},
        }

    @patch("main.fresh_role_heartbeats")
    @patch("main.check_connection", return_value=True)
    def test_live_returns_200(self, _db, _heartbeat):
        response = self.client.get("/live")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    @patch("main.fresh_role_heartbeats")
    @patch("main.check_connection", return_value=True)
    def test_ready_returns_200_when_database_and_roles_ok(self, _db, heartbeat):
        heartbeat.side_effect = lambda config, role: [self._healthy_heartbeat(role)]
        response = self.client.get("/ready")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")
        self.assertEqual(response.json()["dependencies"]["database"], "ok")
        self.assertIn("api", response.json()["dependencies"]["roles"]["required"])

    @patch("main.fresh_role_heartbeats", return_value=[])
    @patch("main.check_connection", return_value=True)
    def test_ready_returns_503_when_required_role_missing(self, _db, _heartbeat):
        response = self.client.get("/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "unready")
        unhealthy = response.json()["dependencies"]["roles"]["unhealthy"]
        self.assertIn("api", unhealthy)

    @patch("main.fresh_role_heartbeats")
    @patch("main.check_connection", return_value=False)
    def test_ready_returns_503_when_database_unreachable(self, _db, _heartbeat):
        response = self.client.get("/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "unready")


if __name__ == "__main__":
    unittest.main()
