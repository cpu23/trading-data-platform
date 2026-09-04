import os
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes.json.investment import MAX_DOCUMENT_BYTES  # noqa: E402

os.environ["DEPLOYMENT_MODE"] = "test"
os.environ["DISABLE_AUTH"] = "true"

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


class _InvestmentDomProbe(HTMLParser):
    def __init__(self):
        super().__init__()
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs)))

    def has(self, tag, **attrs):
        def matches(node_attrs):
            for name, value in attrs.items():
                key = name.replace("__", "-")
                if key not in node_attrs or node_attrs[key] != value:
                    return False
            return True

        return any(
            node_tag == tag and matches(node_attrs)
            for node_tag, node_attrs in self.nodes
        )


class InvestmentApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch("config.load_config", return_value=MOCK_CONFIG):
            from auth import mint_csrf_token
            from fastapi.testclient import TestClient
            from main import create_app

        with patch("main.load_config", return_value=MOCK_CONFIG):
            cls.app = create_app()
        cls.client = TestClient(cls.app)
        cls.client.__enter__()
        cls.config_patcher = patch(
            "routes.json.investment.load_config",
            return_value=MOCK_CONFIG,
        )
        cls.config_patcher.start()
        cls.csrf_token = mint_csrf_token()
        cls.client.cookies.set("csrf-token", cls.csrf_token)
        cls.headers = {
            "Origin": "http://testserver",
            "X-CSRF-Token": cls.csrf_token,
        }

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.config_patcher.stop()

    @patch("routes.json.investment.get_investment_dashboard")
    def test_dashboard_endpoint_calls_investment_dashboard_service(
        self, mock_get_dashboard
    ):
        payload = {
            "model": "google/gemini-3.5-flash-lite",
            "regions": [
                {
                    "code": "US",
                    "company_count": 12,
                    "configured_company_count": 100,
                    "coverage_status": "configured",
                    "stage": "forming",
                },
                {
                    "code": "EU",
                    "company_count": 8,
                    "configured_company_count": 200,
                    "coverage_status": "configured",
                    "stage": "monitor",
                },
                {
                    "code": "ASIA",
                    "company_count": 0,
                    "configured_company_count": 0,
                    "coverage_status": "not_configured",
                    "stage": None,
                },
            ],
            "industries": [],
            "documents": [],
            "analyses": [],
        }
        mock_get_dashboard.return_value = payload

        result = self.client.get("/api/investment/dashboard", headers=self.headers)

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["model"], "google/gemini-3.5-flash-lite")
        self.assertEqual(result.json()["regions"], payload["regions"])
        mock_get_dashboard.assert_called_once_with(MOCK_CONFIG)

    @patch("routes.json.investment.get_investment_analysis")
    def test_get_analysis_returns_payload_and_handles_not_found(
        self, mock_get_analysis
    ):
        analysis_id = "22222222-2222-2222-2222-222222222222"
        mock_get_analysis.return_value = {
            "analysis_id": analysis_id,
            "document_id": "11111111-1111-1111-1111-111111111111",
            "state": {"stage": "accelerating"},
        }

        result = self.client.get(
            f"/api/investment/analyses/{analysis_id}", headers=self.headers
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["analysis_id"], analysis_id)
        mock_get_analysis.assert_called_once_with(MOCK_CONFIG, analysis_id)

        mock_get_analysis.return_value = None
        result_404 = self.client.get(
            f"/api/investment/analyses/{analysis_id}", headers=self.headers
        )
        self.assertEqual(result_404.status_code, 404)
        self.assertIn("Investment analysis not found", result_404.json()["detail"])

    @patch("routes.json.investment.enqueue_investment_analysis")
    @patch("routes.json.investment.store_investment_document_path")
    def test_raw_document_upload_stores_path_and_optionally_enqueues_analysis(
        self, mock_store, mock_enqueue
    ):
        document_id = "11111111-1111-1111-1111-111111111111"
        params = {
            "filename": "report.txt",
            "company": "Memory Co",
            "symbol": "MEM",
            "region": "ASIA",
            "industry": "DRAM",
            "document_type": "annual_report",
            "analyze": "true",
        }
        body = b"Annual report financial evidence. " * 8
        mock_store.return_value = {
            "document_id": document_id,
            "status": "ingested",
        }
        mock_enqueue.return_value = {
            "job_id": "job-123",
            "enqueued_at": "2026-09-04T00:00:00Z",
        }

        result = self.client.post(
            "/api/investment/documents",
            params=params,
            content=body,
            headers={**self.headers, "Content-Type": "text/plain"},
        )

        self.assertEqual(result.status_code, 201)
        data = result.json()
        self.assertEqual(data["document_id"], document_id)
        self.assertEqual(data["analysis"]["job_id"], "job-123")
        self.assertTrue(mock_store.called)
        call_args = mock_store.call_args[0]
        self.assertEqual(call_args[0], MOCK_CONFIG)
        self.assertEqual(call_args[1]["company"], "Memory Co")
        self.assertEqual(call_args[3], "text/plain")
        self.assertFalse(mock_store.call_args[1].get("extract", True))
        mock_enqueue.assert_called_once_with(MOCK_CONFIG, document_id)

    @patch("routes.json.investment.store_investment_document_path")
    def test_declared_oversize_upload_is_rejected_early(self, mock_store):
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
        mock_store.assert_not_called()

    @patch("routes.json.investment.store_investment_document_path")
    def test_oversized_chunked_upload_rejected_and_spool_cleaned(self, mock_store):
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
        mock_store.assert_not_called()

    @patch("routes.json.investment.store_investment_document_path")
    def test_upload_spool_is_cleaned_up_after_store(self, mock_store):
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

        mock_store.return_value = {
            "document_id": document_id,
            "status": "ingested",
        }
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

    @patch("routes.json.investment.store_investment_document_path")
    def test_upload_document_error_handling(self, mock_store):
        params = {"filename": "bad.txt"}
        mock_store.side_effect = ValueError("Invalid document metadata")
        res_422 = self.client.post(
            "/api/investment/documents",
            params=params,
            content=b"data",
            headers={**self.headers, "Content-Type": "text/plain"},
        )
        self.assertEqual(res_422.status_code, 422)

        mock_store.side_effect = RuntimeError("Storage failed")
        res_503 = self.client.post(
            "/api/investment/documents",
            params=params,
            content=b"data",
            headers={**self.headers, "Content-Type": "text/plain"},
        )
        self.assertEqual(res_503.status_code, 503)

    @patch(
        "routes.json.investment.enqueue_investment_analysis",
        side_effect=RuntimeError("queue down"),
    )
    @patch(
        "routes.json.investment.store_investment_document_path",
        return_value={"document_id": "11111111-1111-1111-1111-111111111111"},
    )
    def test_upload_document_enqueue_failure_returns_503(
        self, mock_store, mock_enqueue
    ):
        params = {"filename": "good.txt", "analyze": "true"}
        res = self.client.post(
            "/api/investment/documents",
            params=params,
            content=b"data",
            headers={**self.headers, "Content-Type": "text/plain"},
        )
        self.assertEqual(res.status_code, 503)
        self.assertIn(
            "Investment analysis could not be scheduled", res.json()["detail"]
        )

    @patch("routes.json.investment.enqueue_investment_analysis")
    @patch("routes.json.investment.store_investment_document_url")
    def test_url_ingestion_success_and_analysis_enqueue(
        self, mock_store_url, mock_enqueue
    ):
        document_id = "11111111-1111-1111-1111-111111111111"
        mock_store_url.return_value = {
            "document_id": document_id,
            "status": "ingested",
        }
        mock_enqueue.return_value = {
            "job_id": "job-456",
            "enqueued_at": "2026-09-04T00:00:00Z",
        }

        payload = {
            "url": "https://example.com/sec/10k.htm",
            "company": "Memory Co",
            "symbol": "MEM",
            "region": "ASIA",
            "industry": "DRAM",
            "document_type": "annual_report",
            "analyze": True,
        }
        res = self.client.post(
            "/api/investment/urls",
            json=payload,
            headers={**self.headers, "Content-Type": "application/json"},
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["document_id"], document_id)
        self.assertEqual(res.json()["analysis"]["job_id"], "job-456")
        mock_store_url.assert_called_once()
        mock_enqueue.assert_called_once_with(MOCK_CONFIG, document_id)

    @patch("routes.json.investment.store_investment_document_url")
    def test_url_ingestion_error_handling(self, mock_store_url):
        payload = {
            "url": "https://example.com/sec/10k.htm",
            "company": "Memory Co",
            "symbol": "MEM",
            "region": "ASIA",
            "industry": "DRAM",
            "document_type": "annual_report",
        }
        mock_store_url.side_effect = ValueError("Unsupported URL scheme")
        res_422 = self.client.post(
            "/api/investment/urls",
            json=payload,
            headers={**self.headers, "Content-Type": "application/json"},
        )
        self.assertEqual(res_422.status_code, 422)

        mock_store_url.side_effect = ConnectionError("Failed to fetch URL")
        res_502 = self.client.post(
            "/api/investment/urls",
            json=payload,
            headers={**self.headers, "Content-Type": "application/json"},
        )
        self.assertEqual(res_502.status_code, 502)
        self.assertIn(
            "Investment document could not be fetched", res_502.json()["detail"]
        )

    def test_analyze_validates_uuid(self):
        result = self.client.post(
            "/api/investment/documents/not-a-uuid/analyze",
            json={},
            headers={**self.headers, "Content-Type": "application/json"},
        )
        self.assertEqual(result.status_code, 422)

    def test_analyze_validates_market_inputs_type(self):
        document_id = "11111111-1111-1111-1111-111111111111"
        result = self.client.post(
            f"/api/investment/documents/{document_id}/analyze",
            json={"market_inputs": "not-a-dict"},
            headers={**self.headers, "Content-Type": "application/json"},
        )
        self.assertEqual(result.status_code, 422)
        self.assertIn("market_inputs must be an object", result.json()["detail"])

    @patch("routes.json.investment.analyze_investment_document")
    def test_analysis_forwards_market_inputs_and_returns_result(self, mock_analyze):
        document_id = "11111111-1111-1111-1111-111111111111"
        analysis_id = "22222222-2222-2222-2222-222222222222"
        mock_analyze.return_value = {
            "analysis_id": analysis_id,
            "document_id": document_id,
            "state": {"stage": "accelerating", "score": 11},
        }

        result = self.client.post(
            f"/api/investment/documents/{document_id}/analyze",
            json={"market_inputs": {"market_price": 100, "discount_rate": 10}},
            headers={**self.headers, "Content-Type": "application/json"},
        )

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["state"]["stage"], "accelerating")
        mock_analyze.assert_called_once_with(
            MOCK_CONFIG,
            document_id,
            {"market_price": 100, "discount_rate": 10},
        )

    @patch("routes.json.investment.analyze_investment_document")
    def test_analysis_budget_and_conflict_errors(self, mock_analyze):
        from budgets import BudgetBlock, BudgetExceeded
        from investment_service import AnalysisInProgress

        document_id = "11111111-1111-1111-1111-111111111111"

        mock_analyze.side_effect = BudgetExceeded(processor="investment")
        res_429 = self.client.post(
            f"/api/investment/documents/{document_id}/analyze",
            json={},
            headers={**self.headers, "Content-Type": "application/json"},
        )
        self.assertEqual(res_429.status_code, 429)

        mock_analyze.side_effect = BudgetBlock(processor="investment")
        res_503 = self.client.post(
            f"/api/investment/documents/{document_id}/analyze",
            json={},
            headers={**self.headers, "Content-Type": "application/json"},
        )
        self.assertEqual(res_503.status_code, 503)

        mock_analyze.side_effect = AnalysisInProgress(
            "Analysis already running for document"
        )
        res_409 = self.client.post(
            f"/api/investment/documents/{document_id}/analyze",
            json={},
            headers={**self.headers, "Content-Type": "application/json"},
        )
        self.assertEqual(res_409.status_code, 409)

        mock_analyze.side_effect = LookupError("Document not found")
        res_404 = self.client.post(
            f"/api/investment/documents/{document_id}/analyze",
            json={},
            headers={**self.headers, "Content-Type": "application/json"},
        )
        self.assertEqual(res_404.status_code, 404)

        mock_analyze.side_effect = ValueError("Invalid parameters")
        res_422 = self.client.post(
            f"/api/investment/documents/{document_id}/analyze",
            json={},
            headers={**self.headers, "Content-Type": "application/json"},
        )
        self.assertEqual(res_422.status_code, 422)

        mock_analyze.side_effect = RuntimeError("Unexpected failure")
        res_503_gen = self.client.post(
            f"/api/investment/documents/{document_id}/analyze",
            json={},
            headers={**self.headers, "Content-Type": "application/json"},
        )
        self.assertEqual(res_503_gen.status_code, 503)

    @patch("routes.json.investment.get_filing_source_status")
    def test_filings_status(self, mock_status):
        mock_status.return_value = {
            "sec_edgar": {
                "status": "ok",
                "last_collected": "2026-09-04T00:00:00Z",
            },
        }
        res = self.client.get("/api/investment/filings/status", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["sec_edgar"]["status"], "ok")
        mock_status.assert_called_once_with(MOCK_CONFIG)

    @patch("routes.json.investment.accept_and_enqueue_operation")
    def test_filings_collect_acceptance_and_conflict(self, mock_accept_enqueue):
        from datetime import UTC, datetime

        from run_lifecycle import RunAcceptanceConflict

        accepted_time = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
        mock_accept_enqueue.return_value = (accepted_time, "job-id-123")

        body = {
            "correlation_id": "33333333-3333-3333-3333-333333333333",
            "idempotency_key": "idem-key-1",
            "auto_analyze": True,
        }
        res = self.client.post(
            "/api/investment/filings/collect",
            json=body,
            headers={**self.headers, "Content-Type": "application/json"},
        )
        self.assertEqual(res.status_code, 202)
        self.assertEqual(res.json()["job_id"], "33333333-3333-3333-3333-333333333333")
        self.assertEqual(res.json()["accepted_at"], accepted_time.isoformat())
        mock_accept_enqueue.assert_called_once_with(
            MOCK_CONFIG,
            correlation_id="33333333-3333-3333-3333-333333333333",
            triggered_by="api",
            run_kind="filings",
            requested_component="investment_filings",
            idempotency_key="idem-key-1",
            request_summary={"auto_analyze": True},
            dedupe_key=None,
            input_fingerprint=None,
            payload={"auto_analyze": True},
            priority=100,
            max_attempts=3,
        )

        mock_accept_enqueue.side_effect = RunAcceptanceConflict(
            "Job already in progress"
        )
        res_409 = self.client.post(
            "/api/investment/filings/collect",
            json=body,
            headers={**self.headers, "Content-Type": "application/json"},
        )
        self.assertEqual(res_409.status_code, 409)

        mock_accept_enqueue.side_effect = RuntimeError("Database error")
        res_503 = self.client.post(
            "/api/investment/filings/collect",
            json=body,
            headers={**self.headers, "Content-Type": "application/json"},
        )
        self.assertEqual(res_503.status_code, 503)

    def test_investment_page_renders_required_sections(self):
        response = self.client.get("/investment", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        dom = _InvestmentDomProbe()
        dom.feed(response.text)
        self.assertTrue(dom.has("section", data__thesis__view="investment"))
        self.assertTrue(dom.has("button", type="button", data__thesis__run=None))
        self.assertTrue(dom.has("tbody", data__thesis__opportunities=None))
        self.assertTrue(dom.has("strong", data__status__value="model-cost"))
        self.assertTrue(dom.has("div", role="status", aria__live="polite"))
        self.assertTrue(dom.has("option", value="0"))
        self.assertTrue(dom.has("option", value="0.25", selected=None))
        self.assertIn("Key industries", response.text)
        self.assertIn("Deterministic signals", response.text)
        self.assertIn("SEC EDGAR", response.text)
        self.assertIn("/api/investment/dashboard", response.text)
        self.assertIn(
            "All values come from deterministic filing extraction",
            response.text,
        )
        self.assertNotIn("model figures are overlaid", response.text)

    def test_v7_epistemic_narrative_renderers_use_only_current_contract_keys(self):
        response = self.client.get("/investment", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        page = response.text

        model_narrative_block = page.split("// --- Model narrative (LLM) ---", 1)[
            1
        ].split("// --- Risks & watch items", 1)[0]
        catalyst_renderer = page.split("function addCatalystsSection", 1)[1].split(
            "function addMaterialitySection", 1
        )[0]
        materiality_renderer = page.split("function addMaterialitySection", 1)[1].split(
            "function addRelationshipReconciliations", 1
        )[0]
        relationship_renderer = page.split(
            "function addRelationshipReconciliations", 1
        )[1].split("function addSignalsSection", 1)[0]
        risk_renderer = page.split("function addRisksSection", 1)[1].split(
            "function addQualitySection", 1
        )[0]

        for token in (
            "a.counter_thesis",
            "inv-counter-thesis",
            "addCatalystsSection(narGroup, a.catalysts)",
            "addMaterialitySection(narGroup, a.materiality_assessment)",
            "addRelationshipReconciliations(narGroup, a.relationship_reconciliations)",
            "update.counter_thesis",
            "addMaterialitySection(item, update.materiality_assessment)",
            "addRelationshipReconciliations(item, update.relationship_reconciliations)",
            "addCatalystsSection(item, update.catalysts)",
            "addRisksSection(item, update.risks)",
        ):
            self.assertIn(token, model_narrative_block)

        for token in (
            "c.trigger",
            "c.expected_outcome",
            "c.epistemic_state",
            "c.uncertainty",
            "c.horizon",
            "c.evidence",
        ):
            self.assertIn(token, catalyst_renderer)

        for topic in (
            "forward_guidance",
            "reported_variance_driver",
            "margin_economics",
            "capital_commitment_duration",
        ):
            self.assertIn(topic, materiality_renderer)
        for token in (
            "item.status",
            "not_disclosed",
            "addressed",
            "item.observation",
            "item.implication",
            "item.evidence",
            "Materiality assessment",
        ):
            self.assertIn(token, materiality_renderer)

        for token in (
            "relationship.relationship_id",
            "relationship.status",
            "relationship.observation",
            "relationship.interpretation",
            "relationship.uncertainty",
            "relationship.fact_paths",
            "relationship.summary_synthesis",
            "relationship.thesis_synthesis",
            "relationship.summary_fact_paths",
            "Material relationships",
            "Required facts: ",
            "Summary facts: ",
            "Summary: ",
            "Thesis: ",
        ):
            self.assertIn(token, relationship_renderer)

        for token in (
            "r.sourced_observation",
            "r.inference",
            "r.epistemic_state",
            "r.uncertainty",
            "r.likelihood",
            "r.impact",
            "r.mitigation",
            "r.evidence",
            "severityOrdinal",
        ):
            self.assertIn(token, risk_renderer)

        for renderer, legacy_tokens in (
            (catalyst_renderer, ("c.catalyst", "c.description", "c.timeframe")),
            (
                risk_renderer,
                ("r.risk", "r.description", "r.probability", "r.timeframe"),
            ),
        ):
            for token in legacy_tokens:
                self.assertNotIn(token, renderer)

    def test_region_strip_distinguishes_analysis_from_configuration(self):
        response = self.client.get("/investment", headers=self.headers)
        self.assertEqual(response.status_code, 200)

        page = response.text
        self.assertIn("stateLine.textContent = 'not configured';", page)
        self.assertIn(
            "countLine.textContent = analyzedCount + ' of ' + configuredCount + ' analyzed';",
            page,
        )
        self.assertIn(
            "card.setAttribute('data-coverage-status', isConfigured ? 'configured' : 'not-configured');",
            page,
        )
        self.assertNotIn("count + (count === 1 ? ' company' : ' companies')", page)

    def test_percentage_points_render_without_sub_one_scaling(self):
        response = self.client.get("/investment", headers=self.headers)
        self.assertEqual(response.status_code, 200)

        page = response.text
        self.assertIn("else if (isPct) sval = fmtCompact(n) + '%';", page)
        self.assertIn(
            "else if (isPct) v.textContent = n.toFixed(1) + '%';",
            page,
        )
        self.assertNotIn("Math.abs(n) < 1", page)

        val_cell_examples = (
            (0.4397, "0.4%"),
            (-0.4397, "-0.4%"),
            (12.3, "12.3%"),
        )
        for percentage_points, expected in val_cell_examples:
            with self.subTest(percentage_points=percentage_points):
                self.assertEqual(f"{percentage_points:.1f}%", expected)

    def test_valuation_rendering_uses_only_comparable_public_market_prices(self):
        response = self.client.get("/investment", headers=self.headers)
        self.assertEqual(response.status_code, 200)

        page = response.text
        for label in (
            "DCF scenario / share calculated",
            "DCF enterprise value only",
            "DCF unavailable",
            "Comparable market inputs",
            "P/E with market price",
            "Margin of safety with market price",
            "DCF scenario / model output",
            "Public market close",
            "Market source",
            "Market capture",
            "DCF status",
            "DCF assumptions",
            "Starting FCF",
            "Inferred annual growth",
            "WACC",
            "Terminal growth",
            "Shares outstanding",
            "Net debt",
            "DCF sensitivity",
            "Sensitivity status",
            "Enterprise value low",
            "Enterprise value high",
            "Per-share low",
            "Per-share high",
            "Largest range driver",
            "Analysis quality ready",
            "Quality & freshness",
        ):
            with self.subTest(label=label):
                self.assertIn(label, page)
        self.assertIn(
            "var pe = hasMarketPrice ? finiteValue",
            page,
        )
        self.assertIn(
            "var marginOfSafety = hasMarketPrice && dcfPerShare != null && dcfPerShare > 0 ? finiteValue",
            page,
        )
        self.assertIn(
            "if (marginOfSafety != null) marginOfSafety *= 100;",
            page,
        )
        self.assertIn(
            "Unavailable without a fresh currency-comparable public close or explicit manual input.",
            page,
        )
        self.assertIn("public daily close or explicit manual input", page)
        self.assertNotIn("marketData.comparison_status || 'comparable'", page)
        self.assertIn(
            'name="market_price" placeholder="not supplied"',
            page,
        )
        self.assertNotIn("Intrinsic / share", page)


if __name__ == "__main__":
    unittest.main()
