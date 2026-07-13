import os
import sys
from pathlib import Path

# Ensure the api/ directory is on sys.path so that "import main" works
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unittest
import httpx
from unittest.mock import MagicMock, patch

# ── Environment (auth) ──────────────────────────────────────────────────────
os.environ["DASHBOARD_USER"] = "test"
os.environ["DASHBOARD_PASSWORD"] = "test"

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

    @patch("routes.json.system.query_many", return_value=[])
    def test_health_returns_200(self, _mock_qm):
        """GET /api/system/health with empty DB should return 200 + 'overall' key."""
        resp = client.get("/api/system/health", headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("overall", data)
        self.assertEqual(data["overall"], "healthy")  # no collectors → all ok
        self.assertIn("components", data)
        self.assertIsInstance(data["components"], list)

    def test_health_requires_auth(self):
        """GET /api/system/health without Basic-Auth header returns 401."""
        resp = client.get("/api/system/health")
        self.assertEqual(resp.status_code, 401)

    @patch("routes.json.system.query_many", return_value=[])
    def test_logs_returns_list(self, _mock_qm):
        """GET /api/system/logs returns a JSON object with 'logs' list."""
        resp = client.get("/api/system/logs", headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("logs", data)
        self.assertIsInstance(data["logs"], list)
        self.assertIn("limit", data)


class TestRegimeRoutes(unittest.TestCase):
    """Integration tests for /api/regime/* endpoints."""

    @patch("routes.json.regime.query_one", return_value=None)
    def test_current_returns_stale_when_no_data(self, _mock_qo):
        """When no regime classification exists, the endpoint returns stale=True."""
        resp = client.get("/api/regime/current", headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["stale"])
        self.assertEqual(data["stale_reason"], "No regime classification available")

    @patch("routes.json.regime.query_many", return_value=[])
    def test_history_returns_list(self, _mock_qm):
        """GET /api/regime/history returns a 'regimes' list (empty when no data)."""
        resp = client.get("/api/regime/history", headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("regimes", data)
        self.assertIsInstance(data["regimes"], list)
        self.assertIn("days", data)


class TestMacroRoutes(unittest.TestCase):
    """Integration tests for /api/macro/* endpoints."""

    @patch("routes.json.macro.query_one", return_value=None)
    @patch("routes.json.macro.query_many", return_value=[])
    def test_dashboard_returns_indicators(self, _mock_qm, _mock_qo):
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

    @patch("routes.json.evidence.query_one", return_value=None)
    def test_missing_opinion_returns_404(self, _mock_qo):
        """Requesting evidence for a non-existent opinion returns 404."""
        resp = client.get("/api/evidence/nonexistent-id", headers=AUTH)
        self.assertEqual(resp.status_code, 404)
        self.assertIn("detail", resp.json())


class TestBudgetRoute(unittest.TestCase):
    """Integration tests for /api/system/budget."""

    @patch("budgets.query_one", return_value=None)
    def test_budget_returns_status(self, _mock_qo):
        """GET /api/system/budget returns a budget status dict."""
        resp = client.get("/api/system/budget", headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("today_cost_usd", data)
        self.assertIn("budget_cap_usd", data)
        self.assertIn("usage_pct", data)
