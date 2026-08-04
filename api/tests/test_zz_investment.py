import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DASHBOARD_USER"] = "test"
os.environ["DASHBOARD_PASSWORD"] = "test"

MOCK_CONFIG = {
    "logging": {"level": "INFO"},
    "database": {
        "host": "localhost",
        "port": 5432,
        "name": "test",
        "user": "test",
        "password": "test",
    },
    "dashboard": {"indicators": [], "stale_thresholds": {}},
    "collectors": {},
    "processors": {},
    "budgets": {"daily_llm_usd": 2.0, "warn_at_pct": 80},
    "timezone": {"primary": {"name": "UTC", "label": "UTC"}},
}


class InvestmentApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch("config.load_config", return_value=MOCK_CONFIG):
            from fastapi.testclient import TestClient

            from auth import mint_csrf_token
            from main import create_app

        cls.upstream = AsyncMock()
        cls.upstream.aclose = AsyncMock()
        with patch("main.load_config", return_value=MOCK_CONFIG):
            cls.app = create_app(
                orchestrator_client_factory=lambda **kwargs: cls.upstream
            )
        cls.client = TestClient(cls.app)
        cls.client.__enter__()
        cls.headers = {
            "Authorization": "Basic dGVzdDp0ZXN0",
            "Origin": "http://testserver",
            "X-CSRF-Token": mint_csrf_token(),
        }

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        self.upstream.reset_mock()

    @staticmethod
    def response(status, payload, path="/investment/dashboard"):
        return httpx.Response(
            status,
            json=payload,
            request=httpx.Request("GET", f"http://orchestrator:8000{path}"),
        )

    def test_dashboard_proxies_shared_orchestrator_client(self):
        payload = {
            "model": "google/gemini-3.5-flash-lite",
            "regions": [{"code": "US", "company_count": 0, "stage": "monitor"}],
            "industries": [],
            "documents": [],
            "analyses": [],
        }
        self.upstream.request.return_value = self.response(200, payload)

        result = self.client.get("/api/investment/dashboard", headers=self.headers)

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["model"], "google/gemini-3.5-flash-lite")
        args = self.upstream.request.await_args.args
        self.assertEqual(
            args[:2], ("GET", "http://orchestrator:8000/investment/dashboard")
        )

    def test_raw_document_upload_forwards_metadata_and_bytes(self):
        document_id = "11111111-1111-1111-1111-111111111111"
        self.upstream.request.return_value = self.response(
            201,
            {"document_id": document_id, "status": "ingested"},
            "/investment/documents",
        )
        params = {
            "filename": "report.txt",
            "company": "Memory Co",
            "symbol": "MEM",
            "region": "ASIA",
            "industry": "DRAM",
            "document_type": "annual_report",
        }
        body = b"Annual report financial evidence. " * 8

        result = self.client.post(
            "/api/investment/documents",
            params=params,
            content=body,
            headers={**self.headers, "Content-Type": "text/plain"},
        )

        self.assertEqual(result.status_code, 201)
        kwargs = self.upstream.request.await_args.kwargs
        self.assertEqual(kwargs["content"], body)
        self.assertEqual(kwargs["params"]["company"], "Memory Co")
        self.assertEqual(kwargs["headers"]["Content-Type"], "text/plain")

    def test_analyze_validates_uuid_before_forwarding(self):
        result = self.client.post(
            "/api/investment/documents/not-a-uuid/analyze",
            json={},
            headers={**self.headers, "Content-Type": "application/json"},
        )
        self.assertEqual(result.status_code, 422)
        self.upstream.request.assert_not_awaited()

    def test_analysis_forwards_market_inputs_and_returns_published_result(self):
        document_id = "11111111-1111-1111-1111-111111111111"
        analysis_id = "22222222-2222-2222-2222-222222222222"
        self.upstream.request.return_value = self.response(
            200,
            {
                "analysis_id": analysis_id,
                "document_id": document_id,
                "state": {"stage": "accelerating", "score": 11},
            },
            f"/investment/documents/{document_id}/analyze",
        )

        result = self.client.post(
            f"/api/investment/documents/{document_id}/analyze",
            json={"market_inputs": {"market_price": 100, "discount_rate": 10}},
            headers={**self.headers, "Content-Type": "application/json"},
        )

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["state"]["stage"], "accelerating")
        self.assertEqual(
            self.upstream.request.await_args.kwargs["json"]["market_inputs"][
                "discount_rate"
            ],
            10,
        )

    def test_investment_page_renders_required_sections(self):
        response = self.client.get("/investment", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Key industries", response.text)
        self.assertIn("Deterministic signals", response.text)
        self.assertIn("SEC EDGAR", response.text)
        self.assertIn("/api/investment/dashboard", response.text)
        self.assertIn(
            "All values come from deterministic filing extraction",
            response.text,
        )
        self.assertNotIn("model figures are overlaid", response.text)


if __name__ == "__main__":
    unittest.main()
