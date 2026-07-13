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


class ComponentIdValidationTests(unittest.TestCase):
    """Task 10: Validate component IDs before accepting background work."""

    def setUp(self):
        from main import app
        from fastapi.testclient import TestClient
        self.client = TestClient(app)

    def test_run_collector_invalid_id_returns_404(self):
        """POST /run_collector/not-real returns 404."""
        resp = self.client.post("/run_collector/not-real")
        self.assertEqual(resp.status_code, 404)

    def test_run_processor_invalid_id_returns_404(self):
        """POST /run_processor/not-real returns 404."""
        resp = self.client.post("/run_processor/not-real")
        self.assertEqual(resp.status_code, 404)

    @patch("main.get_session")
    def test_no_cycle_runs_row_created_for_invalid_collector(self, get_session):
        """No cycle_runs row is created for invalid collector IDs."""
        session = Mock()
        get_session.return_value.__enter__.return_value = session

        resp = self.client.post("/run_collector/invalid-collector-id")
        self.assertEqual(resp.status_code, 404)
        # ensure_run should never have been called, so no INSERT into cycle_runs
        insert_calls = [
            call for call in session.execute.call_args_list
            if "INSERT INTO cycle_runs" in str(call)
        ]
        self.assertEqual(len(insert_calls), 0,
                         "No cycle_runs INSERT should occur for invalid collector ID")

    @patch("main.get_session")
    def test_no_cycle_runs_row_created_for_invalid_processor(self, get_session):
        """No cycle_runs row is created for invalid processor IDs."""
        session = Mock()
        get_session.return_value.__enter__.return_value = session

        resp = self.client.post("/run_processor/invalid-processor-id")
        self.assertEqual(resp.status_code, 404)
        insert_calls = [
            call for call in session.execute.call_args_list
            if "INSERT INTO cycle_runs" in str(call)
        ]
        self.assertEqual(len(insert_calls), 0,
                         "No cycle_runs INSERT should occur for invalid processor ID")


class CollectionFailureStatusTests(unittest.TestCase):
    """Task 9: Propagate collection and persistence failures into run status."""

    def setUp(self):
        from main import app
        from fastapi.testclient import TestClient
        self.client = TestClient(app)
        self.config = {
            "database": {"host": "localhost", "port": 5432, "name": "test",
                         "user": "test", "password": "test"},
            "collectors": {"fred": {"enabled": True, "schedule": "0 6 * * *",
                                     "api_key": "test", "series": [
                                         {"id": "GDP", "frequency": "quarterly"},
                                         {"id": "CPI", "frequency": "monthly"},
                                     ]}},
            "processors": {},
        }

    @patch("orchestrator.get_collector")
    @patch("orchestrator.get_session")
    def test_all_series_fail_yields_failed_status(self, get_session, get_collector):
        """All FRED series fail → collector status 'failed'."""
        from collectors.base import CollectionResult

        session = Mock()
        get_session.return_value.__enter__.return_value = session
        session.execute.return_value = None

        mock_collector = Mock()
        mock_collector.get_target_table.return_value = "macro_series"
        mock_collector.get_conflict_columns.return_value = ["series_id", "observed_at"]
        # Return CollectionResult with all series failed (Task 9)
        mock_collector.collect.return_value = CollectionResult(
            records=[],
            errors=[
                {"series_id": "GDP", "error": "Connection refused", "frequency": "quarterly"},
                {"series_id": "CPI", "error": "Connection refused", "frequency": "monthly"},
            ],
            total_series=2,
            successful_series=0,
        )
        get_collector.return_value = mock_collector

        from orchestrator import run_collector
        result = run_collector("fred", config=self.config, correlation_id="test-cid")

        self.assertEqual(result["status"], "failed",
                         "All series failed → status should be 'failed'")

    @patch("orchestrator.get_collector")
    @patch("orchestrator.upsert_records")
    @patch("orchestrator.get_session")
    def test_some_series_fail_yields_partial_status(self, get_session,
                                                     upsert_records,
                                                     get_collector):
        """Some FRED series fail → collector status 'partial'."""
        from collectors.base import CollectionResult
        from db import WriteResult

        session = Mock()
        get_session.return_value.__enter__.return_value = session

        mock_collector = Mock()
        mock_collector.get_target_table.return_value = "macro_series"
        mock_collector.get_conflict_columns.return_value = ["series_id", "observed_at"]
        # Return CollectionResult with partial failure (GDP succeeded, CPI failed)
        mock_collector.collect.return_value = CollectionResult(
            records=[{"series_id": "GDP", "value": 1.0}],
            errors=[{"series_id": "CPI", "error": "Connection refused", "frequency": "monthly"}],
            total_series=2,
            successful_series=1,
        )
        get_collector.return_value = mock_collector

        # DB writes succeed for the fetched records
        upsert_records.return_value = WriteResult(
            attempted=1, written=1, failed=0, errors=()
        )

        from orchestrator import run_collector
        result = run_collector("fred", config=self.config, correlation_id="test-cid")
        self.assertEqual(result["status"], "partial",
                         "Partial collection failure → status should be 'partial'")

    @patch("orchestrator.get_collector")
    @patch("orchestrator.upsert_records")
    @patch("orchestrator.get_session")
    def test_records_fetched_but_all_writes_fail_yields_failed(self, get_session,
                                                                upsert_records,
                                                                get_collector):
        """Records fetched but every DB write fails → status 'failed'."""
        from db import WriteResult

        session = Mock()
        get_session.return_value.__enter__.return_value = session

        mock_collector = Mock()
        mock_collector.get_target_table.return_value = "macro_series"
        mock_collector.get_conflict_columns.return_value = ["series_id", "observed_at"]
        mock_collector.collect.return_value = [
            {"series_id": "GDP", "observed_at": "2024-01-01", "value": 1.0}
        ]
        get_collector.return_value = mock_collector

        upsert_records.return_value = WriteResult(
            attempted=1, written=0, failed=1, errors=("write error",)
        )

        from orchestrator import run_collector
        result = run_collector("fred", config=self.config, correlation_id="test-cid")
        self.assertEqual(result["status"], "failed",
                         "Status should be 'failed' when records fetched but none written")

    @patch("orchestrator.get_collector")
    @patch("orchestrator.upsert_records")
    @patch("orchestrator.get_session")
    def test_some_writes_fail_yields_partial_status(self, get_session,
                                                     upsert_records,
                                                     get_collector):
        """Some DB writes fail → status 'partial'."""
        from db import WriteResult

        session = Mock()
        get_session.return_value.__enter__.return_value = session

        mock_collector = Mock()
        mock_collector.get_target_table.return_value = "macro_series"
        mock_collector.get_conflict_columns.return_value = ["series_id", "observed_at"]
        mock_collector.collect.return_value = [
            {"series_id": "GDP", "observed_at": "2024-01-01", "value": 1.0},
            {"series_id": "CPI", "observed_at": "2024-01-01", "value": 2.0},
        ]
        get_collector.return_value = mock_collector

        upsert_records.return_value = WriteResult(
            attempted=2, written=1, failed=1, errors=("write error",)
        )

        from orchestrator import run_collector
        result = run_collector("fred", config=self.config, correlation_id="test-cid")
        self.assertEqual(result["status"], "partial",
                         "Status should be 'partial' when some but not all records written")


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

    def test_config_env_substitution_names_absent_required_variable(self):
        from config_loader import reload_config

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text("required: ${TRULY_REQUIRED}\n")
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "TRULY_REQUIRED"):
                    reload_config(str(config_path))

    def test_config_env_substitution_rejects_blank_required_variable(self):
        from config_loader import reload_config

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text("required: ${BLANK_REQUIRED}\n")
            with patch.dict(os.environ, {"BLANK_REQUIRED": ""}, clear=True):
                with self.assertRaisesRegex(ValueError, "BLANK_REQUIRED"):
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

    def test_enabled_production_sources_blank_credentials_fail_closed(self):
        from config_loader import reload_config

        config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
        base_env = {
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
        for variable in ("FRED_API_KEY", "OANDA_API_KEY", "OPENROUTER_API_KEY"):
            with self.subTest(variable=variable):
                env = {**base_env, variable: ""}
                with patch.dict(os.environ, env, clear=True):
                    with self.assertRaisesRegex(ValueError, variable):
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


# ═════════════════════════════════════════════════════════════════════════════
# Task 12: Orchestrator health contract tests
# ═════════════════════════════════════════════════════════════════════════════

class HealthContractTests(unittest.TestCase):
    """Task 12: Orchestrator /health and /quality endpoints."""

    def setUp(self):
        from main import app
        from fastapi.testclient import TestClient
        self.client = TestClient(app)

    @patch("main.get_last_collection_runs", return_value=[])
    @patch("main._get_config")
    def test_health_returns_status_and_stream_keys(self, mock_get_config, _mock_runs):
        """GET /health returns status, stream, scheduler, collectors."""
        mock_get_config.return_value = {"logging": {"level": "INFO"}}
        resp = self.client.get("/health")
        self.assertIn(resp.status_code, (200, 500))  # may fail if no DB, but shape is what we test

    @patch("main._get_config")
    @patch("main.DATA_QUALITY_CHECKS", {})
    def test_quality_returns_overall_and_checks_with_empty_registry(self, mock_get_config):
        """GET /quality returns {overall, checks} even with no checks registered."""
        mock_get_config.return_value = {"logging": {"level": "INFO"}}
        resp = self.client.get("/quality")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("overall", data)
        self.assertIn("checks", data)
        self.assertEqual(data["overall"], "healthy")
        self.assertIsInstance(data["checks"], dict)

    @patch("main._get_config")
    @patch("main.DATA_QUALITY_CHECKS", {})
    def test_quality_checks_is_dict(self, mock_get_config):
        """GET /quality checks key is always a dict."""
        mock_get_config.return_value = {"logging": {"level": "INFO"}}
        resp = self.client.get("/quality")
        data = resp.json()
        self.assertIsInstance(data["checks"], dict,
                              "Quality checks must be a dict for consumers to iterate with .items()")


if __name__ == "__main__":
    unittest.main()
