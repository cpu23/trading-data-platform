import os
import sys
from pathlib import Path

# Ensure the api/ directory is on sys.path so that "import main" works
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unittest
from unittest.mock import patch

# ── Environment (auth) ──────────────────────────────────────────────────────
os.environ["DASHBOARD_USER"] = "test"
os.environ["DASHBOARD_PASSWORD"] = "test"
os.environ["LEGACY_BASIC_AUTH"] = "true"

# Discovery may import auth in an earlier test module. Keep these legacy-route
# tests independent from any mounted activation state.
_setup_complete_patcher = patch("auth.setup_complete", return_value=False)
_setup_complete_patcher.start()

# ── Minimal config that every route handler can consume ─────────────────────
MOCK_CONFIG = {
    "logging": {"level": "INFO"},
    "database": {
        "host": "localhost",
        "port": 5432,
        "name": "test",
        "user": "test",
        "password": "test",
    },
    "dashboard": {
        "indicators": [
            {
                "series_id": "T10Y2Y",
                "label": "10Y-2Y spread",
                "precision": 2,
                "category": "yield_curve",
            },
        ],
        "stale_thresholds": {
            "briefing_hours": 18,
            "regime_hours": 18,
            "macro_hours": 30,
            "events_hours": 8,
        },
    },
    "collectors": {},
    "processors": {},
    "budgets": {
        "daily_llm_usd": 2.00,
        "warn_at_pct": 80,
    },
}

# ── Patch config.load_config BEFORE importing main (and the whole tree) ────
_config_patcher = patch("config.load_config", return_value=MOCK_CONFIG)
_config_patcher.start()

from main import app  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)

# ── Auth helpers ────────────────────────────────────────────────────────────
AUTH = {"Authorization": "Basic dGVzdDp0ZXN0"}  # test:test


# ═════════════════════════════════════════════════════════════════════════════
# Test cases
# ═════════════════════════════════════════════════════════════════════════════

class TestSystemRoutes(unittest.TestCase):
    """Integration tests for /api/system/* endpoints."""

    @patch("routes.json.system.load_config", return_value=MOCK_CONFIG)
    @patch("routes.json.system.query_many", return_value=[])
    @patch("routes.json.system.httpx.get", side_effect=RuntimeError("offline"))
    def test_health_returns_200(self, _mock_http, _mock_qm, _mock_config):
        """GET /api/system/health with empty DB should return 200 + 'overall' key."""
        resp = client.get("/api/system/health", headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("overall", data)
        self.assertEqual(data["overall"], "healthy")
        self.assertIn("components", data)
        self.assertIsInstance(data["components"], list)

    def test_health_requires_auth(self):
        """GET /api/system/health without Basic-Auth header returns 401."""
        resp = client.get("/api/system/health")
        self.assertEqual(resp.status_code, 401)

    @patch("routes.json.system.load_config", return_value=MOCK_CONFIG)
    @patch("routes.json.system.query_many", return_value=[])
    def test_logs_returns_list(self, _mock_qm, _mock_config):
        """GET /api/system/logs returns a JSON object with 'logs' list."""
        resp = client.get("/api/system/logs", headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("logs", data)
        self.assertIsInstance(data["logs"], list)
        self.assertIn("limit", data)

    @patch("routes.json.system.load_config", return_value=MOCK_CONFIG)
    @patch("routes.json.system.query_many", return_value=[])
    def test_logs_hide_benchmark_runs_by_default(self, query_many, _mock_config):
        resp = client.get("/api/system/logs", headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        sql = query_many.call_args.args[0]
        self.assertIn("cr.triggered_by = 'benchmark'", sql)

    @patch("routes.json.system.load_config", return_value=MOCK_CONFIG)
    @patch("routes.json.system.query_many", return_value=[])
    def test_logs_can_include_benchmark_runs_for_audit(self, query_many, _mock_config):
        resp = client.get(
            "/api/system/logs?include_internal=true",
            headers=AUTH,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(
            "cr.triggered_by = 'benchmark'",
            query_many.call_args.args[0],
        )

    @patch("routes.json.system.load_config", return_value=MOCK_CONFIG)
    @patch("routes.json.system.query_many", return_value=[])
    def test_recent_runs_hide_benchmarks_by_default(self, query_many, _mock_config):
        resp = client.get("/api/system/runs", headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("triggered_by <> 'benchmark'", query_many.call_args.args[0])

    @patch("routes.json.system.load_config", return_value=MOCK_CONFIG)
    @patch("routes.json.system.query_many", return_value=[])
    def test_recent_runs_can_include_internal_runs(self, query_many, _mock_config):
        resp = client.get(
            "/api/system/runs?include_internal=true",
            headers=AUTH,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("triggered_by <> 'benchmark'", query_many.call_args.args[0])


class TestRegimeRoutes(unittest.TestCase):
    """Integration tests for /api/regime/* endpoints."""

    @patch("routes.json.regime.load_config", return_value=MOCK_CONFIG)
    @patch("routes.json.regime.query_one", return_value=None)
    def test_current_returns_stale_when_no_data(self, _mock_qo, _mock_config):
        """When no regime classification exists, the endpoint returns stale=True."""
        resp = client.get("/api/regime/current", headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["stale"])
        self.assertEqual(data["stale_reason"], "No regime classification available")

    @patch("routes.json.regime.load_config", return_value=MOCK_CONFIG)
    @patch("routes.json.regime.query_many", return_value=[])
    def test_history_returns_list(self, _mock_qm, _mock_config):
        """GET /api/regime/history returns a 'regimes' list (empty when no data)."""
        resp = client.get("/api/regime/history", headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("regimes", data)
        self.assertIsInstance(data["regimes"], list)
        self.assertIn("days", data)


class TestMacroRoutes(unittest.TestCase):
    """Integration tests for /api/macro/* endpoints."""

    @patch("routes.json.macro.load_config", return_value=MOCK_CONFIG)
    @patch("routes.json.macro.query_one", return_value=None)
    @patch("routes.json.macro.query_many", return_value=[])
    def test_dashboard_returns_indicators(self, _mock_qm, _mock_qo, _mock_config):
        """GET /api/macro/dashboard returns 'indicators' key even with no DB rows."""
        resp = client.get("/api/macro/dashboard", headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("indicators", data)
        self.assertIsInstance(data["indicators"], list)
        # The one configured indicator appears, albeit with None values
        self.assertEqual(len(data["indicators"]), 1)
        self.assertEqual(data["indicators"][0]["series_id"], "T10Y2Y")


class TestEvidenceRoutes(unittest.TestCase):
    """Integration tests for /api/evidence/* endpoints."""

    @patch("routes.json.evidence.load_config", return_value=MOCK_CONFIG)
    @patch("routes.json.evidence.query_one", return_value=None)
    def test_missing_opinion_returns_404(self, _mock_qo, _mock_config):
        """Requesting evidence for a non-existent opinion returns 404."""
        resp = client.get("/api/evidence/nonexistent-id", headers=AUTH)
        self.assertEqual(resp.status_code, 404)
        self.assertIn("detail", resp.json())


class TestBudgetRoute(unittest.TestCase):
    """Integration tests for /api/system/budget."""

    @patch("budgets.load_config", return_value=MOCK_CONFIG)
    @patch("budgets.query_one", return_value=None)
    def test_budget_returns_status(self, _mock_qo, _mock_config):
        """GET /api/system/budget returns a budget status dict."""
        resp = client.get("/api/system/budget", headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("today_cost_usd", data)
        self.assertIn("budget_cap_usd", data)
        self.assertIn("usage_pct", data)
