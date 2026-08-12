import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DASHBOARD_USER"] = "test"
os.environ["DASHBOARD_PASSWORD"] = "test"

from routes.json.investment import MAX_DOCUMENT_BYTES  # noqa: E402

os.environ["DEPLOYMENT_MODE"] = "test"

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
        cls.csrf_token = mint_csrf_token()
        cls.client.cookies.set("csrf-token", cls.csrf_token)
        cls.headers = {
            "Authorization": "Basic dGVzdDp0ZXN0",
            "Origin": "http://testserver",
            "X-CSRF-Token": cls.csrf_token,
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
        params = {
            "filename": "report.txt",
            "company": "Memory Co",
            "symbol": "MEM",
            "region": "ASIA",
            "industry": "DRAM",
            "document_type": "annual_report",
        }
        body = b"Annual report financial evidence. " * 8
        forwarded = []

        async def consume_and_respond(*args, **kwargs):
            content = kwargs.get("content")
            if content is not None:
                chunks = []
                async for chunk in content:
                    chunks.append(chunk)
                forwarded.append(b"".join(chunks))
            return self.response(
                201,
                {"document_id": document_id, "status": "ingested"},
                "/investment/documents",
            )

        self.upstream.request = AsyncMock(side_effect=consume_and_respond)
        result = self.client.post(
            "/api/investment/documents",
            params=params,
            content=body,
            headers={**self.headers, "Content-Type": "text/plain"},
        )

        self.assertEqual(result.status_code, 201)
        kwargs = self.upstream.request.await_args.kwargs
        self.assertEqual(forwarded, [body])
        self.assertEqual(kwargs["params"]["company"], "Memory Co")
        self.assertEqual(kwargs["headers"]["Content-Type"], "text/plain")

    def test_declared_oversize_upload_is_rejected_early(self):
        params = {
            "filename": "report.txt",
            "company": "Memory Co",
            "region": "ASIA",
            "industry": "DRAM",
            "document_type": "annual_report",
        }
        big = b"x" * (MAX_DOCUMENT_BYTES + 1)

        result = self.client.post(
            "/api/investment/documents",
            params=params,
            content=big,
            headers={**self.headers, "Content-Type": "application/octet-stream"},
        )

        self.assertEqual(result.status_code, 413)
        self.upstream.request.assert_not_awaited()

    def test_oversized_chunked_upload_rejected_and_spool_cleaned(self):
        import asyncio

        from httpx import ASGITransport, AsyncClient

        params = {
            "filename": "report.txt",
            "company": "Memory Co",
            "region": "ASIA",
            "industry": "DRAM",
            "document_type": "annual_report",
        }
        created = []
        real_named_temp = tempfile.NamedTemporaryFile

        def fake_named_temp(*args, **kwargs):
            handle = real_named_temp(*args, **kwargs)
            created.append(handle.name)
            return handle

        async def chunked_body():
            yield b"x" * (MAX_DOCUMENT_BYTES + 100)

        async def run():
            async with AsyncClient(
                transport=ASGITransport(app=self.app),
                base_url="http://testserver",
                cookies={"csrf-token": self.csrf_token},
            ) as client:
                return await client.post(
                    "/api/investment/documents",
                    params=params,
                    content=chunked_body(),
                    headers={
                        **self.headers,
                        "Content-Type": "application/octet-stream",
                    },
                )

        with patch(
            "routes.json.investment.tempfile.NamedTemporaryFile",
            side_effect=fake_named_temp,
        ):
            result = asyncio.run(run())

        self.assertEqual(result.status_code, 413)
        self.assertEqual(len(created), 1)
        self.assertFalse(os.path.exists(created[0]))
        self.upstream.request.assert_not_awaited()

    def test_upload_spool_is_cleaned_up_after_forward(self):
        import tempfile as _tempfile

        document_id = "11111111-1111-1111-1111-111111111111"
        params = {
            "filename": "report.txt",
            "company": "Memory Co",
            "region": "ASIA",
            "industry": "DRAM",
            "document_type": "annual_report",
        }
        created = []
        real_named_temp = _tempfile.NamedTemporaryFile

        def fake_named_temp(*args, **kwargs):
            handle = real_named_temp(*args, **kwargs)
            created.append(handle.name)
            return handle

        self.upstream.request = AsyncMock(
            return_value=self.response(
                201,
                {"document_id": document_id, "status": "ingested"},
                "/investment/documents",
            )
        )
        with patch(
            "routes.json.investment.tempfile.NamedTemporaryFile",
            side_effect=fake_named_temp,
        ):
            result = self.client.post(
                "/api/investment/documents",
                params=params,
                content=b"Annual report financial evidence. " * 8,
                headers={**self.headers, "Content-Type": "text/plain"},
            )

        self.assertEqual(result.status_code, 201)
        self.assertEqual(len(created), 1)
        self.assertFalse(os.path.exists(created[0]))

    def test_missing_orchestrator_client_fails_before_any_post(self):
        """A POST must never be re-sent on a fallback client: with no shared
        client the request fails closed before any network call."""
        params = {
            "filename": "report.txt",
            "company": "Memory Co",
            "region": "ASIA",
            "industry": "DRAM",
            "document_type": "annual_report",
        }
        saved = self.app.state.orchestrator_client
        try:
            del self.app.state.orchestrator_client
            result = self.client.post(
                "/api/investment/documents",
                params=params,
                content=b"small document",
                headers={**self.headers, "Content-Type": "text/plain"},
            )
        finally:
            self.app.state.orchestrator_client = saved

        self.assertEqual(result.status_code, 503)
        self.upstream.request.assert_not_awaited()

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
