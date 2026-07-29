import os
import sys
from pathlib import Path

# Ensure the api/ directory is on sys.path so that "import main" works
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unittest
import json
import tempfile
import httpx
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

# ── Environment (auth) ──────────────────────────────────────────────────────
os.environ["DASHBOARD_USER"] = "test"
os.environ["DASHBOARD_PASSWORD"] = "test"

from auth import mint_csrf_token

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
    "timezone": {"primary": {"name": "Europe/London", "label": "London"}},
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
AUTH = {
    "Authorization": "Basic dGVzdDp0ZXN0",  # test:test
    "Origin": "http://testserver",
    "X-CSRF-Token": mint_csrf_token(),
}


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

    @patch.object(app.state.orchestrator_client, "post", new_callable=AsyncMock)
    def test_news_validates_exact_source_before_forwarding(self, post):
        response = client.post("/api/triggers/news/not-real", headers=AUTH)
        self.assertEqual(response.status_code, 404)
        post.assert_not_awaited()

    @patch.object(app.state.orchestrator_client, "post", new_callable=AsyncMock)
    def test_news_forwards_with_shared_client_and_status_url(self, post):
        upstream = MagicMock(status_code=202)
        upstream.json.return_value = {"job_id": "news-job", "accepted_at": "now"}
        post.return_value = upstream

        response = client.post("/api/triggers/news/reuters", headers=AUTH)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(post.await_args.args[0], "http://orchestrator:8000/run_news/reuters")
        self.assertEqual(response.json()["status_url"], "/api/system/logs?correlation_id=news-job")

    @patch.object(app.state.orchestrator_client, "post", new_callable=AsyncMock)
    def test_news_maps_safe_orchestrator_statuses(self, post):
        for status in (409, 503):
            with self.subTest(status=status):
                upstream = MagicMock(status_code=status)
                upstream.json.return_value = {"detail": "safe rejection"}
                post.return_value = upstream
                response = client.post("/api/triggers/news/kobeissi", headers=AUTH)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["detail"], "safe rejection")


class TestCycleModes(unittest.TestCase):
    def _accepted(self):
        response = MagicMock(status_code=202, text='{"job_id":"job-1"}')
        response.json.return_value = {"job_id": "job-1", "accepted_at": "now"}
        return response

    @patch.object(app.state.orchestrator_client, "post", new_callable=AsyncMock)
    def test_cycle_defaults_to_refresh_and_propagates_safe_confirmation(self, post):
        post.return_value = self._accepted()

        response = client.post("/api/triggers/cycle", headers=AUTH, json={})

        self.assertEqual(response.status_code, 202)
        payload = post.await_args.kwargs["json"]
        self.assertEqual(payload["mode"], "refresh")
        self.assertFalse(payload["budget_confirmed"])
        self.assertIn("auth", post.await_args.kwargs)

    @patch.object(app.state.orchestrator_client, "post", new_callable=AsyncMock)
    def test_invalid_mode_values_and_types_are_422_before_orchestrator_call(self, post):
        for mode in ("everything", ["refresh"], {"mode": "refresh"}, 1, True, None):
            with self.subTest(mode=mode):
                response = client.post(
                    "/api/triggers/cycle", headers=AUTH, json={"mode": mode}
                )
                self.assertEqual(response.status_code, 422)

        post.assert_not_awaited()

    @patch.object(app.state.orchestrator_client, "post", new_callable=AsyncMock)
    def test_invalid_body_and_confirmation_types_are_422_before_orchestrator_call(self, post):
        for body in (
            ["refresh"],
            "refresh",
            1,
            True,
            {"budget_confirmed": "true"},
            {"budget_confirmed": 1},
            {"budget_confirmed": [True]},
        ):
            with self.subTest(body=body):
                response = client.post("/api/triggers/cycle", headers=AUTH, json=body)
                self.assertEqual(response.status_code, 422)

        post.assert_not_awaited()

    @patch.object(app.state.orchestrator_client, "post", new_callable=AsyncMock)
    def test_absent_or_null_body_defaults_to_refresh(self, post):
        post.return_value = self._accepted()
        for send_null in (False, True):
            with self.subTest(send_null=send_null):
                if send_null:
                    response = client.post(
                        "/api/triggers/cycle",
                        headers={**AUTH, "Content-Type": "application/json"},
                        content="null",
                    )
                else:
                    response = client.post("/api/triggers/cycle", headers=AUTH)
                self.assertEqual(response.status_code, 202)
                self.assertEqual(post.await_args.kwargs["json"]["mode"], "refresh")

    @patch.object(app.state.orchestrator_client, "post", new_callable=AsyncMock)
    def test_force_full_requires_explicit_confirmation_before_call(self, post):
        response = client.post(
            "/api/triggers/cycle", headers=AUTH, json={"mode": "force_full"}
        )

        self.assertEqual(response.status_code, 422)
        post.assert_not_awaited()

    @patch.object(app.state.orchestrator_client, "post", new_callable=AsyncMock)
    def test_force_full_forwards_internal_basic_auth_after_global_auth(self, post):
        post.return_value = self._accepted()

        response = client.post(
            "/api/triggers/cycle",
            headers=AUTH,
            json={"mode": "force_full", "budget_confirmed": True},
        )

        self.assertEqual(response.status_code, 202)
        request = post.await_args
        self.assertEqual(request.kwargs["json"]["mode"], "force_full")
        self.assertTrue(request.kwargs["json"]["budget_confirmed"])
        self.assertIsInstance(request.kwargs["auth"], httpx.BasicAuth)

    @patch("routes.json.triggers._internal_basic_auth", side_effect=RuntimeError)
    @patch.object(app.state.orchestrator_client, "post", new_callable=AsyncMock)
    def test_force_full_missing_internal_credentials_fails_closed(self, post, _auth):
        response = client.post(
            "/api/triggers/cycle",
            headers=AUTH,
            json={"mode": "force_full", "budget_confirmed": True},
        )

        self.assertEqual(response.status_code, 503)
        post.assert_not_awaited()


# ═════════════════════════════════════════════════════════════════════════════
# Test cases
# ═════════════════════════════════════════════════════════════════════════════

class TestSystemRoutes(unittest.TestCase):
    """Integration tests for /api/system/* endpoints."""

    @patch("routes.json.system.query_many", return_value=[])
    @patch.object(app.state.orchestrator_client, "get", new_callable=AsyncMock)
    def test_health_returns_200(self, mock_httpx_get, _mock_qm):
        """GET /api/system/health with empty DB should return 200 + contract keys."""
        mock_health_resp = MagicMock()
        mock_health_resp.json.return_value = {
            "liveness": "ok",
            "readiness": "ready",
            "data_health": "healthy",
            "status": "healthy",
            "components": [],
            "scheduler": {"jobs": []},
            "stream": {"status": "connected", "last_heartbeat": "2026-01-01T00:00:00Z"},
            "quality": {"overall": "healthy", "checks": {}},
        }
        mock_httpx_get.return_value = mock_health_resp

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
        mock_httpx_get.assert_awaited_once()

    @patch("routes.json.system.query_many", return_value=[])
    @patch.object(app.state.orchestrator_client, "get", new_callable=AsyncMock)
    def test_degraded_quality_without_source_id_degrades_api_health(self, mock_get, _query):
        health = MagicMock()
        health.json.return_value = {
            "liveness": "ok", "readiness": "ready", "data_health": "healthy",
            "status": "healthy", "components": [],
            "scheduler": {"jobs": []},
            "stream": {"status": "connected"},
            "quality": {
                "overall": "degraded",
                "checks": {"fred_freshness": {"healthy": False, "detail": "stale"}},
            },
        }
        mock_get.return_value = health

        resp = client.get("/api/system/health", headers=AUTH)

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["data_health"], "degraded")
        self.assertEqual(data["overall"], "degraded")
        quality_component = next(c for c in data["components"] if c["name"] == "quality_checks")
        self.assertEqual(quality_component["last_status"], "degraded")
        self.assertIn("fred_freshness", quality_component["error_message"])
        mock_get.assert_awaited_once()

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


        for case, payload in malformed_payloads.items():
            with self.subTest(case=case):
                health = MagicMock()
                health.json.return_value = payload
                mock_get.return_value = health

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

        for case, component in malformed_components.items():
            with self.subTest(case=case):
                health = MagicMock()
                health.json.return_value = {
                    "liveness": "ok", "readiness": "ready", "data_health": "healthy",
                    "components": [component], "scheduler": {"jobs": []},
                    "stream": {"status": "connected"},
                    "quality": {"overall": "healthy", "checks": {}},
                }
                mock_get.reset_mock()
                mock_get.return_value = health

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

        for case, fields in malformed_fields.items():
            with self.subTest(case=case):
                health = MagicMock()
                health.json.return_value = {
                    "liveness": "ok", "readiness": "ready", "data_health": "healthy",
                    "components": [], **fields,
                    "quality": {"overall": "healthy", "checks": {}},
                }
                mock_get.reset_mock()
                mock_get.return_value = health

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
                health.json.return_value = {
                    "liveness": "ok", "readiness": "ready", "data_health": "healthy",
                    "components": [], "scheduler": {"jobs": []},
                    "stream": {"status": "connected"},
                    "quality": {"overall": "healthy", "checks": checks},
                }
                mock_get.reset_mock()
                mock_get.return_value = health

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


class TestBoundedDashboardInputs(unittest.TestCase):
    @patch("routes.json.events.query_many", return_value=[])
    def test_calendar_defaults_custom_values_and_upper_clamps_reach_one_bounded_query(self, query):
        for url, expected in (
            ("/api/calendar/events", (24, 100)),
            ("/api/calendar/events?hours=48&limit=25", (48, 25)),
            ("/api/calendar/events?hours=999&limit=9999", (168, 500)),
        ):
            with self.subTest(url=url):
                query.reset_mock()
                response = client.get(url, headers=AUTH)
                self.assertEqual(response.status_code, 200)
                self.assertEqual((response.json()["hours"], response.json()["limit"]), expected)
                params = query.call_args.kwargs["params"]
                self.assertEqual(params["limit"], expected[1])
                self.assertIn("LIMIT :limit", query.call_args.args[0])

    @patch("routes.json.events.query_many")
    def test_calendar_rejects_malformed_zero_and_negative_before_query(self, query):
        for name in ("hours", "limit"):
            for value in ("wat", "1.5", "0", "-1", "+2"):
                with self.subTest(name=name, value=value):
                    response = client.get(f"/api/calendar/events?{name}={value}", headers=AUTH)
                    self.assertEqual(response.status_code, 422)
        query.assert_not_called()

    @patch("routes.json.events.query_many")
    def test_calendar_renders_selected_timezone_only_after_cookie_allowlist(self, query):
        query.return_value = [{
            "event_id": "event-1", "event_name": "Open", "country": "JP",
            "scheduled_at": datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc),
        }]
        client.cookies.set("display_timezone", "Asia/Tokyo")
        try:
            response = client.get("/api/calendar/events", headers=AUTH)
        finally:
            client.cookies.clear()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["events"][0]["display_time"].endswith("+09:00"))

    @patch("routes.json.system.query_many", return_value=[])
    def test_api_logs_lines_default_custom_and_clamp(self, query):
        for url, expected in (("/api/logs", 200), ("/api/logs?lines=12", 12), ("/api/logs?lines=5000", 1000)):
            with self.subTest(url=url):
                query.reset_mock()
                response = client.get(url, headers=AUTH)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["lines"], expected)
                self.assertEqual(query.call_args.kwargs["params"]["limit"], expected)

    @patch("routes.json.system.query_many")
    def test_api_logs_rejects_malformed_zero_and_negative_before_read(self, query):
        for value in ("wat", "2.2", "0", "-4", "+2"):
            with self.subTest(value=value):
                response = client.get(f"/api/logs?lines={value}", headers=AUTH)
                self.assertEqual(response.status_code, 422)
        query.assert_not_called()


class TestTimezoneSettings(unittest.TestCase):
    def tearDown(self):
        client.cookies.clear()

    @patch("routes.json.settings.load_config", return_value=MOCK_CONFIG)
    def test_get_uses_configured_default_and_returns_strict_choices(self, _config):
        response = client.get("/api/settings/timezone", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["current"], "Europe/London")
        self.assertEqual(response.json()["choices"], ["UTC", "Europe/London", "America/New_York", "Asia/Tokyo", "Australia/Sydney"])

    def test_post_accepts_allowlisted_timezone_and_sets_strict_cookie(self):
        response = client.post("/api/settings/timezone", headers=AUTH, json={"timezone": "Asia/Tokyo"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["current"], "Asia/Tokyo")
        cookie = response.headers["set-cookie"]
        self.assertEqual(response.cookies.get("display_timezone").strip('"'), "Asia/Tokyo")
        self.assertEqual(client.get("/api/settings/timezone", headers=AUTH).json()["current"], "Asia/Tokyo")
        self.assertIn("SameSite=strict", cookie)

    def test_post_rejects_unknown_without_setting_cookie(self):
        response = client.post("/api/settings/timezone", headers=AUTH, json={"timezone": "../../etc/passwd"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("set-cookie", response.headers)

    @patch("routes.json.settings.load_config", return_value=MOCK_CONFIG)
    def test_invalid_cookie_falls_back_before_zoneinfo_lookup(self, _config):
        client.cookies.set("display_timezone", "Mars/Olympus")
        response = client.get("/api/settings/timezone", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["current"], "Europe/London")


class TestOperationsOverview(unittest.TestCase):
    @patch("routes.views.operations.get_system_health", new_callable=AsyncMock, return_value={})
    @patch("routes.views.operations.run_in_threadpool", new_callable=AsyncMock)
    def test_operations_dispatches_local_snapshot_to_threadpool(self, threadpool, _health):
        threadpool.return_value = {
            "tz": {
                "display_timezone": "Europe/London",
                "display_timezone_label": "London",
                "timezone_options": [],
            },
            "processors": {"available": False, "message": "Unavailable"},
            "feed": {"available": False, "message": "Unavailable"},
            "runs": {"available": False, "message": "Unavailable"},
        }
        response = client.get("/operations", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        threadpool.assert_awaited_once()

    @patch("routes.views.operations.get_system_health", new_callable=AsyncMock)
    @patch("routes.views.operations.query_many")
    @patch("routes.views.operations._feed_snapshot")
    def test_populated_overview_is_bounded_and_renders_required_sections(self, feed, query, health):
        health.return_value = {"components": [{"name": "fred", "last_status": "success", "next_due_at": "later"}], "readiness": "ready"}
        query.side_effect = [
            [{"processor": "briefing", "status": "success", "model_used": "model-x", "cost_usd": 0.1, "started_at": "2026-07-16T00:00:00+00:00", "duration_ms": 1200}],
            [{"correlation_id": "run-1", "run_kind": "cycle", "requested_component": None, "status": "completed", "result_status": "success", "started_at": "2026-07-16T00:00:00+00:00", "completed_at": "2026-07-16T00:00:02+00:00", "error_message": "RAW_SECRET"}],
        ]
        feed.return_value = {"status": "published", "item_count": 4, "published_at": "now"}
        client.cookies.set("display_timezone", "Asia/Tokyo")
        try:
            response = client.get("/operations", headers=AUTH)
        finally:
            client.cookies.clear()
        self.assertEqual(response.status_code, 200)
        for label in ("Source &amp; scheduler state", "Latest processor outcomes", "Feed publication state", "Recent durable runs"):
            self.assertIn(label, response.text)
        self.assertIn("briefing", response.text)
        self.assertIn("Asia/Tokyo", response.text)
        self.assertNotIn("RAW_SECRET", response.text)
        self.assertEqual(query.call_count, 2)
        for call in query.call_args_list:
            self.assertEqual(call.kwargs["params"]["limit"], 10)
            self.assertIn("LIMIT :limit", call.args[0])

    @patch("routes.views.operations.get_system_health", new_callable=AsyncMock, side_effect=RuntimeError("RAW_HEALTH_SECRET"))
    @patch("routes.views.operations.query_many", side_effect=[RuntimeError("RAW_DB_SECRET"), []])
    @patch("routes.views.operations._feed_snapshot", side_effect=RuntimeError("RAW_FEED_SECRET"))
    def test_partial_failure_and_empty_sections_fail_soft_without_raw_errors(self, _feed, _query, _health):
        response = client.get("/operations", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.text.count("Unavailable"), 3)
        self.assertIn("No durable runs yet", response.text)
        self.assertNotIn("RAW_", response.text)


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

    @patch("routes.json.macro.query_many", return_value=[])
    def test_dashboard_returns_indicators(self, query):
        """GET /api/macro/dashboard returns 'indicators' key even with no DB rows."""
        resp = client.get("/api/macro/dashboard", headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("indicators", data)
        self.assertIsInstance(data["indicators"], list)
        # The one configured indicator appears, albeit with None values
        self.assertEqual(len(data["indicators"]), 1)
        self.assertEqual(data["indicators"][0]["series_id"], "T10Y2Y")
        query.assert_called_once()

    @patch("routes.json.macro.query_many", return_value=[{"observed_at": "2026-07-01T00:00:00+00:00", "value": 16.0}])
    def test_series_days_parameter_sets_requested_window(self, query):
        resp = client.get("/api/macro/VIXCLS?days=90", headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        from_date = query.call_args.kwargs["params"]["from_date"]
        self.assertEqual((date.today() - from_date).days, 90)


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
            "quality": {"overall": "healthy", "checks": {}},
        }
        mock_get.return_value = health
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
            "quality": {
                "overall": "degraded",
                "checks": {
                    "fred_DGS10_freshness": {
                        "healthy": False,
                        "detail": "stale",
                    }
                },
            },
        }
        mock_get.return_value = health
        resp = client.get("/api/system/health", headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # With no collectors/processors configured, all components are empty
        # but liveness should still be 'ok' (the app is alive)
        self.assertEqual(data["liveness"], "ok",
                         "Liveness stays 'ok' even when no data available")
        self.assertEqual(data["data_health"], "degraded")


class TestCalendarDisplayTimezone(unittest.TestCase):
    def test_serialized_time_and_day_boundary_use_selected_timezone(self):
        from routes.json.events import _serialize_event

        row = {
            "event_id": "event-1",
            "event_name": "Tokyo boundary",
            "country": "JP",
            "scheduled_at": datetime(2026, 1, 1, 15, 30, tzinfo=timezone.utc),
            "impact_level": "high",
            "consensus": None,
            "previous": None,
            "actual": None,
            "source": "fixture",
            "metadata": {"currency": "JPY"},
        }
        window = {
            "london": ZoneInfo("Europe/London"),
            "ny": ZoneInfo("America/New_York"),
            "london_label": "London",
            "ny_label": "New York",
        }

        event = _serialize_event(
            row,
            window,
            display_zone=ZoneInfo("Asia/Tokyo"),
            display_timezone="Asia/Tokyo",
        )

        self.assertEqual(event["time_display"], "00:30")
        self.assertEqual(event["day_key"], "2026-01-02")
        self.assertEqual(event["day_label_short"], "Fri")
        self.assertEqual(event["display_timezone"], "Asia/Tokyo")
