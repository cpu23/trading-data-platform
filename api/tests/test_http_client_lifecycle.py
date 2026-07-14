import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["DASHBOARD_USER"] = "test"
os.environ["DASHBOARD_PASSWORD"] = "test"

MOCK_CONFIG = {
    "logging": {"level": "INFO"},
    "database": {"host": "localhost", "name": "test", "user": "test", "password": "test"},
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
    "budgets": {"daily_llm_usd": 2.0, "warn_at_pct": 80},
}
AUTH = {"Authorization": "Basic dGVzdDp0ZXN0"}

with patch("config.load_config", return_value=MOCK_CONFIG):
    from main import create_app
from fastapi.testclient import TestClient
from routes.json.watchlist import _quote_events


def upstream_response(method, url, status_code=200, json_data=None):
    request = httpx.Request(method, url)
    return httpx.Response(status_code, json=json_data or {}, request=request)


class FakeOrchestratorClient:
    def __init__(self):
        self.calls = []
        self.close_count = 0

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url.endswith("/quotes"):
            return upstream_response("GET", url, json_data={"EURUSD": 1.1})
        if url.endswith("/quality"):
            return upstream_response("GET", url, json_data={
                "overall": "healthy",
                "checks": {"fred_freshness": {"healthy": True, "detail": "fresh"}},
            })
        raise AssertionError(f"unexpected GET {url}")

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return upstream_response("POST", url, status_code=202, json_data={"job_id": "job-1"})

    async def aclose(self):
        self.close_count += 1


class ApiClientLifecycleTests(unittest.TestCase):
    @patch("main.load_config", return_value=MOCK_CONFIG)
    @patch("main.setup_logging")
    def test_lifespan_reuses_one_client_across_routes_and_closes_once(
        self, _setup_logging, _load_config,
    ):
        upstream = FakeOrchestratorClient()
        factory = Mock(return_value=upstream)
        app = create_app(orchestrator_client_factory=factory)

        with TestClient(app) as client:
            self.assertIs(app.state.orchestrator_client, upstream)
            self.assertEqual(client.get("/api/quotes", headers=AUTH).status_code, 200)
            self.assertEqual(client.get("/api/quotes", headers=AUTH).status_code, 200)
            self.assertEqual(client.post("/api/collect/fred", headers=AUTH).status_code, 202)
            self.assertEqual(client.get("/quality", headers=AUTH).status_code, 200)
            factory.assert_called_once()
            self.assertEqual(upstream.close_count, 0)

        self.assertEqual(upstream.close_count, 1)
        self.assertEqual([call[0] for call in upstream.calls], ["GET", "GET", "POST", "GET"])

    def test_sse_multiple_polls_reuse_client_and_stop_on_disconnect(self):
        upstream = FakeOrchestratorClient()

        class Request:
            app = type("App", (), {"state": type("State", (), {"orchestrator_client": upstream})()})()

            def __init__(self):
                self.checks = 0

            async def is_disconnected(self):
                self.checks += 1
                return self.checks > 3

        sleeps = []

        async def no_wait(seconds):
            sleeps.append(seconds)

        async def collect():
            return [event async for event in _quote_events(Request(), sleep=no_wait)]

        events = asyncio.run(collect())

        self.assertEqual(len(events), 2)
        self.assertEqual(len(upstream.calls), 2)
        self.assertEqual(sleeps, [2])

    def test_sse_cancellation_is_reraised_without_recreating_or_closing_shared_client(self):
        upstream = FakeOrchestratorClient()

        async def cancelled_get(_url, **_kwargs):
            raise asyncio.CancelledError()

        upstream.get = cancelled_get

        class Request:
            app = type("App", (), {"state": type("State", (), {"orchestrator_client": upstream})()})()

            async def is_disconnected(self):
                return False

        generator = _quote_events(Request())

        async def consume_one():
            await anext(generator)

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(consume_one())

        self.assertIs(Request.app.state.orchestrator_client, upstream)
        self.assertEqual(upstream.close_count, 0)


if __name__ == "__main__":
    unittest.main()
