import os
import sys
from pathlib import Path

# Ensure the api/ directory is on sys.path so that "import main" works
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unittest
import json
import tempfile
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

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
client.__enter__()


def tearDownModule():
    client.__exit__(None, None, None)

# ── Auth helpers ────────────────────────────────────────────────────────────
AUTH = {"Authorization": "Basic dGVzdDp0ZXN0"}  # test:test


# ═════════════════════════════════════════════════════════════════════════════
# Task 10: Validate component IDs
# ═════════════════════════════════════════════════════════════════════════════

class TestComponentIdValidation(unittest.TestCase):
    """Task 10: API validates component IDs before forwarding to orchestrator."""

    def test_collect_invalid_id_returns_404(self):
        """POST /api/triggers/collect/not-real returns 404."""
        resp = client.post("/api/triggers/collect/not-real", headers=AUTH)
        self.assertEqual(resp.status_code, 404)

    def test_process_invalid_id_returns_404(self):
        """POST /api/triggers/process/not-real returns 404."""
        resp = client.post("/api/triggers/process/not-real", headers=AUTH)
        self.assertEqual(resp.status_code, 404)


# ═════════════════════════════════════════════════════════════════════════════
# Test cases
# ═════════════════════════════════════════════════════════════════════════════

class TestSystemRoutes(unittest.TestCase):
    """Integration tests for /api/system/* endpoints."""

    @patch("routes.json.system.query_many", return_value=[])
    @patch.object(app.state.orchestrator_client, "get", new_callable=AsyncMock)
    def test_health_returns_200(self, mock_httpx_get, _mock_qm):
        """GET /api/system/health with empty DB should return 200 + contract keys."""
        # Mock orchestrator health response
        mock_health_resp = MagicMock()
        mock_health_resp.json.return_value = {
            "liveness": "ok",
            "readiness": "ready",
            "data_health": "healthy",
            "status": "healthy",
            "components": [],
            "scheduler": {"jobs": []},
            "stream": {"status": "connected", "last_heartbeat": "2026-01-01T00:00:00Z"},
        }
        # Mock orchestrator quality response
        mock_quality_resp = MagicMock()
        mock_quality_resp.json.return_value = {
            "overall": "healthy",
            "checks": {},
        }
        mock_httpx_get.side_effect = [mock_health_resp, mock_quality_resp]

        resp = client.get("/api/system/health", headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("liveness", data)
        self.assertEqual(data["liveness"], "ok")
        self.assertIn("readiness", data)
        self.assertIn("data_health", data)
        self.assertIn("components", data)
        self.assertIsInstance(data["components"], list)
        # With healthy stream and no stale components, should be ready/healthy
        self.assertEqual(data["readiness"], "ready")
        self.assertEqual(data["data_health"], "healthy")

    @patch("routes.json.system.query_many", return_value=[])
    @patch.object(app.state.orchestrator_client, "get", new_callable=AsyncMock)
    def test_degraded_quality_without_source_id_degrades_api_health(self, mock_get, _query):
        health = MagicMock()
        health.json.return_value = {
            "liveness": "ok", "readiness": "ready", "data_health": "healthy",
            "status": "healthy", "components": [],
            "scheduler": {"jobs": []},
            "stream": {"status": "connected"},
        }
        quality = MagicMock()
        quality.json.return_value = {
            "overall": "degraded",
            "checks": {"fred_freshness": {"healthy": False, "detail": "stale"}},
        }
        mock_get.side_effect = [health, quality]

        resp = client.get("/api/system/health", headers=AUTH)

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["data_health"], "degraded")
        self.assertEqual(data["overall"], "degraded")
        quality_component = next(c for c in data["components"] if c["name"] == "quality_checks")
        self.assertEqual(quality_component["last_status"], "degraded")
        self.assertIn("fred_freshness", quality_component["error_message"])

    @patch("routes.json.system.query_many", return_value=[])
    @patch.object(
        app.state.orchestrator_client, "get", new_callable=AsyncMock,
        side_effect=httpx.ConnectError("connection refused"),
    )
    def test_orchestrator_network_failure_returns_503(self, _get, _query):
        resp = client.get("/api/system/health", headers=AUTH)

        self.assertEqual(resp.status_code, 503)
        data = resp.json()
        self.assertEqual(data["liveness"], "ok")
        self.assertEqual(data["readiness"], "unready")
        self.assertEqual(data["data_health"], "degraded")
        component = next(c for c in data["components"] if c["name"] == "orchestrator")
        self.assertEqual(component["last_status"], "error")
        self.assertIn("connection refused", component["error_message"])

    @patch("routes.json.system.query_many", return_value=[])
    @patch.object(app.state.orchestrator_client, "get", new_callable=AsyncMock)
    def test_orchestrator_http_500_returns_503(self, mock_get, _query):
        request = httpx.Request("GET", "http://orchestrator:8000/health")
        response = httpx.Response(500, request=request)
        failed = MagicMock()
        failed.raise_for_status.side_effect = httpx.HTTPStatusError(
            "server error", request=request, response=response
        )
        mock_get.return_value = failed

        resp = client.get("/api/system/health", headers=AUTH)

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["readiness"], "unready")
        self.assertTrue(any(c["name"] == "orchestrator" for c in resp.json()["components"]))

    @patch("routes.json.system.query_many", return_value=[])
    @patch.object(app.state.orchestrator_client, "get", new_callable=AsyncMock)
    def test_invalid_orchestrator_contract_returns_503(self, mock_get, _query):
        invalid = MagicMock()
        invalid.json.return_value = {"status": "ok"}
        mock_get.return_value = invalid

        resp = client.get("/api/system/health", headers=AUTH)

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["readiness"], "unready")
        self.assertIn("invalid orchestrator health contract", resp.json()["components"][0]["error_message"])

    @patch("routes.json.system.query_many", return_value=[])
    @patch.object(app.state.orchestrator_client, "get", new_callable=AsyncMock)
    def test_malformed_complete_orchestrator_health_contract_returns_503(
        self, mock_get, _query
    ):
        malformed_payloads = {
            "invalid data_health": {
                "liveness": "ok", "readiness": "ready", "data_health": "nonsense",
                "components": [],
            },
            "null components": {
                "liveness": "ok", "readiness": "ready", "data_health": "healthy",
                "components": None,
            },
            "non-list components": {
                "liveness": "ok", "readiness": "ready", "data_health": "healthy",
                "components": {},
            },
            "non-dict component": {
                "liveness": "ok", "readiness": "ready", "data_health": "healthy",
                "components": ["database"],
            },
            "component missing name": {
                "liveness": "ok", "readiness": "ready", "data_health": "healthy",
                "components": [{"status": "available"}],
            },
            "component missing status": {
                "liveness": "ok", "readiness": "ready", "data_health": "healthy",
                "components": [{"name": "database"}],
            },
            "invalid readiness": {
                "liveness": "ok", "readiness": "unknown", "data_health": "healthy",
                "components": [],
            },
            "non-string liveness": {
                "liveness": True, "readiness": "ready", "data_health": "healthy",
                "components": [],
            },
        }

        valid_quality = MagicMock()
        valid_quality.json.return_value = {"overall": "healthy", "checks": {}}

        for case, payload in malformed_payloads.items():
            with self.subTest(case=case):
                health = MagicMock()
                health.json.return_value = payload
                mock_get.reset_mock(side_effect=True)
                mock_get.side_effect = [health, valid_quality]

                resp = client.get("/api/system/health", headers=AUTH)

                self.assertEqual(resp.status_code, 503)
                data = resp.json()
                self.assertEqual(data["liveness"], "ok")
                self.assertEqual(data["readiness"], "unready")
                self.assertEqual(data["data_health"], "degraded")
                component = next(
                    item for item in data["components"]
                    if item["name"] == "orchestrator"
                )
                self.assertEqual(component["last_status"], "error")
                self.assertIn("invalid orchestrator health contract", component["error_message"])

    @patch("routes.json.system.query_many", return_value=[])
    @patch.object(app.state.orchestrator_client, "get", new_callable=AsyncMock)
    def test_invalid_component_enums_return_controlled_503(self, mock_get, _query):
        malformed_components = {
            "invalid status": {"name": "database", "kind": "service", "status": "nonsense"},
            "invalid kind": {"name": "database", "kind": "nonsense", "status": "available"},
            "blank name": {"name": "   ", "kind": "service", "status": "available"},
        }
        valid_quality = MagicMock()
        valid_quality.json.return_value = {"overall": "healthy", "checks": {}}

        for case, component in malformed_components.items():
            with self.subTest(case=case):
                health = MagicMock()
                health.json.return_value = {
                    "liveness": "ok", "readiness": "ready", "data_health": "healthy",
                    "components": [component], "scheduler": {"jobs": []},
                    "stream": {"status": "connected"},
                }
                mock_get.reset_mock(side_effect=True)
                mock_get.side_effect = [health, valid_quality]

                resp = client.get("/api/system/health", headers=AUTH)

                self.assertEqual(resp.status_code, 503)
                data = resp.json()
                self.assertEqual(data["liveness"], "ok")
                self.assertEqual(data["readiness"], "unready")
                self.assertEqual(data["data_health"], "degraded")
                self.assertIn(
                    "invalid orchestrator health contract",
                    data["components"][0]["error_message"],
                )

    @patch("routes.json.system.query_many", return_value=[])
    @patch.object(app.state.orchestrator_client, "get", new_callable=AsyncMock)
    def test_invalid_scheduler_and_stream_contracts_return_controlled_503(
        self, mock_get, _query
    ):
        malformed_fields = {
            "scalar scheduler": {"scheduler": "broken", "stream": {"status": "connected"}},
            "scalar scheduler jobs": {"scheduler": {"jobs": "broken"}, "stream": {"status": "connected"}},
            "non-object scheduler job": {"scheduler": {"jobs": ["broken"]}, "stream": {"status": "connected"}},
            "scheduler job missing id": {"scheduler": {"jobs": [{"next_due_at": None}]}, "stream": {"status": "connected"}},
            "scalar stream": {"scheduler": {"jobs": []}, "stream": "broken"},
            "list stream": {"scheduler": {"jobs": []}, "stream": []},
            "invalid stream status type": {"scheduler": {"jobs": []}, "stream": {"status": ["connected"]}},
            "invalid stream heartbeat type": {"scheduler": {"jobs": []}, "stream": {"status": "connected", "last_heartbeat": []}},
        }
        valid_quality = MagicMock()
        valid_quality.json.return_value = {"overall": "healthy", "checks": {}}

        for case, fields in malformed_fields.items():
            with self.subTest(case=case):
                health = MagicMock()
                health.json.return_value = {
                    "liveness": "ok", "readiness": "ready", "data_health": "healthy",
                    "components": [], **fields,
                }
                mock_get.reset_mock(side_effect=True)
                mock_get.side_effect = [health, valid_quality]

                resp = client.get("/api/system/health", headers=AUTH)

                self.assertEqual(resp.status_code, 503)
                data = resp.json()
                self.assertEqual(data["liveness"], "ok")
                self.assertEqual(data["readiness"], "unready")
                self.assertEqual(data["data_health"], "degraded")
                self.assertIn(
                    "invalid orchestrator health contract",
                    data["components"][0]["error_message"],
                )

    @patch("routes.json.system.query_many", return_value=[])
    @patch.object(app.state.orchestrator_client, "get", new_callable=AsyncMock)
    def test_malformed_nested_orchestrator_quality_contract_returns_503(
        self, mock_get, _query
    ):
        health = MagicMock()
        health.json.return_value = {
            "liveness": "ok", "readiness": "ready", "data_health": "healthy",
            "components": [], "scheduler": {"jobs": []},
            "stream": {"status": "connected"},
        }

        malformed_checks = {
            "non-dict list entry": ["fresh"],
            "non-dict mapping value": {"fred_freshness": "fresh"},
            "invalid status enum": {
                "fred_freshness": {"healthy": True, "detail": "fresh", "status": "nonsense"}
            },
            "invalid freshness enum": {
                "fred_freshness": {"healthy": True, "detail": "fresh", "freshness": "nonsense"}
            },
            "empty mapping entry": {"fred_freshness": {}},
        }
        for case, checks in malformed_checks.items():
            with self.subTest(case=case):
                quality = MagicMock()
                quality.json.return_value = {"overall": "healthy", "checks": checks}
                mock_get.reset_mock(side_effect=True)
                mock_get.side_effect = [health, quality]

                resp = client.get("/api/system/health", headers=AUTH)

                self.assertEqual(resp.status_code, 503)
                component = next(
                    item for item in resp.json()["components"]
                    if item["name"] == "orchestrator"
                )
                self.assertIn("invalid orchestrator quality contract", component["error_message"])

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


class TestNewsRoutes(unittest.TestCase):
    def test_feed_handles_invalid_utf8_as_temporarily_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "feed.json").write_bytes(b'\xff\xfe{"items": []}')
            cfg = {"news_feed": {"output_path": tmp}}
            with patch("config.load_config", return_value=cfg):
                resp = client.get("/api/news/feed", headers=AUTH)

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json(), {"error": "News feed is temporarily unavailable."})

    def test_sources_handles_invalid_utf8_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp, "reuters/state.json")
            state_path.parent.mkdir(parents=True)
            state_path.write_bytes(b"\xff\xfeRAW_STATE_SENTINEL")
            cfg = {
                "news_feed": {"output_path": tmp},
                "reuters": {"enabled": True},
                "kobeissi": {"enabled": False},
            }
            with patch("config.load_config", return_value=cfg):
                resp = client.get("/api/news/sources", headers=AUTH)

        self.assertEqual(resp.status_code, 200)
        reuters = next(x for x in resp.json()["sources"] if x["name"] == "reuters")
        self.assertEqual(reuters["status"], "error")
        self.assertEqual(reuters["error"], "state file is invalid")
        self.assertNotIn("RAW_STATE_SENTINEL", json.dumps(resp.json()))

    def test_sources_handles_non_dictionary_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp, "reuters/state.json")
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps([]))
            cfg = {
                "news_feed": {"output_path": tmp},
                "reuters": {"enabled": True},
                "kobeissi": {"enabled": False},
            }
            with patch("config.load_config", return_value=cfg):
                resp = client.get("/api/news/sources", headers=AUTH)

        self.assertEqual(resp.status_code, 200)
        reuters = next(x for x in resp.json()["sources"] if x["name"] == "reuters")
        self.assertEqual(reuters["status"], "error")
        self.assertEqual(reuters["error"], "state file is invalid")


# ═════════════════════════════════════════════════════════════════════════════
# Task 11: Quality page fix
# ═════════════════════════════════════════════════════════════════════════════

class TestQualityPage(unittest.TestCase):
    """Task 11: Quality page returns 200 with mocked orchestrator response."""

    @patch.object(app.state.orchestrator_client, "get", new_callable=AsyncMock)
    def test_quality_page_returns_200_with_orchestrator_response(self, mock_get):
        """GET /quality returns 200 when orchestrator quality is healthy."""
        orchestrator_response = MagicMock()
        orchestrator_response.is_success = True
        orchestrator_response.json.return_value = {
            "overall": "healthy",
            "checks": {
                "fred_freshness": {"healthy": True, "detail": "fresh"},
                "oanda_freshness": {"healthy": True, "detail": "fresh"},
            },
        }
        mock_get.return_value = orchestrator_response

        resp = client.get("/quality", headers=AUTH)
        self.assertEqual(resp.status_code, 200,
                         "Quality page should return 200 with mocked orchestrator")
        self.assertIn("fred freshness", resp.text)
        self.assertIn("fresh", resp.text)


# ═════════════════════════════════════════════════════════════════════════════
# Task 12: Health contract tests
# ═════════════════════════════════════════════════════════════════════════════

class TestHealthContract(unittest.TestCase):
    """Task 12: Separate liveness/readiness/data-health contract."""

    @patch("routes.json.system.query_many", return_value=[])
    @patch.object(app.state.orchestrator_client, "get", new_callable=AsyncMock)
    def test_health_returns_contract_shape(self, mock_get, _mock_qm):
        """GET /api/system/health returns liveness, readiness, data_health keys."""
        health = MagicMock()
        health.json.return_value = {
            "liveness": "ok", "readiness": "ready", "data_health": "healthy",
            "components": [], "scheduler": {"jobs": []},
            "stream": {"status": "connected"},
        }
        quality = MagicMock()
        quality.json.return_value = {"overall": "healthy", "checks": {}}
        mock_get.side_effect = [health, quality]
        resp = client.get("/api/system/health", headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        self.assertIn("liveness", data,
                      "Response must include liveness key")
        self.assertIn("readiness", data,
                      "Response must include readiness key")
        self.assertIn("data_health", data,
                      "Response must include data_health key")
        self.assertIn("components", data,
                      "Response must include components key")

        self.assertEqual(data["liveness"], "ok",
                         "Liveness should be 'ok' when application is running")
        self.assertIn(data["readiness"], ("ready", "degraded", "unready"),
                      "Readiness should be one of: ready, degraded, unready")
        self.assertIn(data["data_health"], ("healthy", "degraded"),
                      "Data health should be one of: healthy, degraded")
        self.assertIsInstance(data["components"], list)

    @patch("routes.json.system.query_many", return_value=[])
    @patch.object(app.state.orchestrator_client, "get", new_callable=AsyncMock)
    def test_stale_data_yields_liveness_ok_data_health_degraded(self, mock_get, _mock_qm):
        """Stale data keeps liveness 'ok' but degrades data_health."""
        health = MagicMock()
        health.json.return_value = {
            "liveness": "ok", "readiness": "ready", "data_health": "degraded",
            "components": [], "scheduler": {"jobs": []},
            "stream": {"status": "connected"},
        }
        quality = MagicMock()
        quality.json.return_value = {
            "overall": "degraded",
            "checks": {"fred_DGS10_freshness": {"healthy": False, "detail": "stale"}},
        }
        mock_get.side_effect = [health, quality]
        resp = client.get("/api/system/health", headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # With no collectors/processors configured, all components are empty
        # but liveness should still be 'ok' (the app is alive)
        self.assertEqual(data["liveness"], "ok",
                         "Liveness stays 'ok' even when no data available")
        self.assertEqual(data["data_health"], "degraded")
