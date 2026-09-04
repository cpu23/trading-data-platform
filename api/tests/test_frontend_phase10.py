import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))


class Phase10FrontendContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_js = (API_ROOT / "static/app.js").read_text()
        cls.env = Environment(
            loader=FileSystemLoader(API_ROOT / "templates"),
            autoescape=select_autoescape(("html",)),
        )
        cls.css = (API_ROOT / "static/style.css").read_text()
        cls.base = (API_ROOT / "templates/base.html").read_text()
        cls.header = (API_ROOT / "templates/partials/header.html").read_text()
        cls.events = (API_ROOT / "templates/partials/events_section.html").read_text()
        cls.expansion = (
            API_ROOT / "templates/partials/expansion_content.html"
        ).read_text()
        cls.regime = (API_ROOT / "templates/partials/regime_section.html").read_text()
        cls.news = (API_ROOT / "templates/partials/news_section.html").read_text()
        cls.navigation = (API_ROOT / "templates/partials/navigation.html").read_text()
        cls.settings = (API_ROOT / "templates/settings.html").read_text()

    def test_page_declares_inline_favicon_to_avoid_browser_404(self):
        self.assertIn('<link rel="icon" href="data:,">', self.base)

    def test_charts_initialize_on_dom_and_htmx_lifecycle_idempotently(self):
        self.assertIn("function initCharts(root)", self.app_js)
        self.assertIn("initCharts(document);", self.app_js)
        self.assertIn("htmx:afterSwap", self.app_js)
        self.assertIn("htmx:afterSettle", self.app_js)
        self.assertIn("initDynamicUi(target)", self.app_js)
        self.assertIn("Chart.getChart(canvas)", self.app_js)
        self.assertIn("existing.destroy()", self.app_js)

    def test_chart_loading_error_and_empty_contract_is_accessible(self):
        for source in (self.base, self.regime):
            self.assertIn('aria-busy="true"', source)
            self.assertIn('role="status"', source)
            self.assertIn('aria-live="polite"', source)
        self.assertIn(".finally(function ()", self.app_js)
        self.assertIn("setChartBusy", self.app_js)
        self.assertIn("No observations", self.app_js)
        self.assertIn("Unable to load", self.app_js)

    def test_calendar_is_semantic_dense_and_scroll_contained(self):
        for marker in (
            'class="calendar-scroll scroll-affordance"',
            '<table class="calendar-table">',
            "<thead>",
            'scope="col"',
            "<tbody>",
        ):
            self.assertIn(marker, self.events)
        for heading in (
            "Time",
            "Event",
            "Market",
            "Impact",
            "Actual",
            "Forecast",
            "Previous",
        ):
            self.assertIn(heading, self.events)
        for impact in ("High", "Medium", "Low"):
            self.assertIn(impact, self.events)
        self.assertIn("current_timezone", self.events)
        self.assertIn("events_data.get('error')", self.events)
        self.assertIn("overflow-x: auto", self.css)

    def test_header_is_market_focused_with_collapsible_data_chip(self):
        # Dashboard header carries no operational cycle controls.
        self.assertNotIn('id="run-cycle-btn"', self.header)
        self.assertNotIn('id="force-cycle-btn"', self.header)
        self.assertNotIn("cycle-mode-select", self.header)
        # Navigation stays focused on primary market and research workspaces.
        self.assertIn("partials/navigation.html", self.header)
        for label in (
            "Dashboard",
            "Markets",
            "News",
            "Investments",
            "Research",
            "Settings",
        ):
            self.assertIn(label, self.navigation)
        for label in ("Logs", "Quality", "Operations"):
            self.assertNotIn(f"'{label}'", self.navigation)
        self.assertIn('aria-current="page"', self.navigation)
        self.assertIn("is-active", self.navigation)
        # Collapsible data chip with freshness summary and settings link.
        self.assertIn('id="data-chip"', self.header)
        self.assertIn("data-chip-label", self.header)
        self.assertIn("data-chip-settings", self.header)
        self.assertIn("initDataChip", self.app_js)

    def test_navigation_pages_in_exact_contract_order(self):
        # Contract order: Dashboard, Markets, News, Investments, Research,
        # Settings. Markets sits between Dashboard and News, and no
        # operational page may appear in the primary navigation.
        import re

        pages = re.findall(r"\('([^']*)', '([^']*)'\)", self.navigation)
        self.assertEqual(
            pages,
            [
                ("/", "Dashboard"),
                ("/markets", "Markets"),
                ("/news", "News"),
                ("/investment", "Investments"),
                ("/research", "Research"),
                ("/settings", "Settings"),
            ],
        )

    def test_settings_hosts_cycle_controls_and_operations(self):
        self.assertEqual(self.settings.count('id="run-cycle-btn"'), 1)
        self.assertIn('id="force-cycle-btn"', self.settings)
        self.assertIn("Data &amp; operations", self.settings)
        self.assertIn('id="timezone-status"', self.settings)
        self.assertIn("initTimezoneControl", self.app_js)
        self.assertIn("fetch('/api/settings/timezone'", self.app_js)
        self.assertIn("method: 'GET'", self.app_js)
        self.assertIn("method: 'POST'", self.app_js)
        self.assertIn("window.location.reload()", self.app_js)
        self.assertIn("select.value = previous", self.app_js)

    def test_mobile_layout_has_no_page_overflow_or_motion(self):
        self.assertIn("overflow-x: hidden", self.css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.css)
        self.assertNotIn("transition:", self.css)
        self.assertIn("min-height: 36px", self.css)
        self.assertIn(".chart-frame", self.css)
        self.assertIn("height: clamp(", self.css)

    def test_dashboard_health_forwards_request_context(self):
        from unittest.mock import patch

        from routes.views.dashboard import _get_dashboard_health

        request = object()
        with patch(
            "routes.views.dashboard.get_system_health",
            return_value={"overall": "healthy"},
        ) as health:
            self.assertEqual(
                asyncio.run(_get_dashboard_health(request)), {"overall": "healthy"}
            )
        health.assert_awaited_once_with(request)

    def test_dashboard_news_is_bounded_and_has_truthful_empty_state(self):
        from unittest.mock import patch

        from routes.views.news import load_story_context

        self.assertIn('id="news-section"', self.news)
        self.assertIn('hx-trigger="marketRefresh from:body"', self.news)
        self.assertIn("stories.status", self.news)
        with patch("routes.views.news.query_many", side_effect=RuntimeError("boom")):
            context = load_story_context()
        self.assertEqual(context["status"], "unavailable")
        self.assertEqual(context["clusters"], [])


class ResearchCaseRenderingContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Environment(
            loader=FileSystemLoader(API_ROOT / "templates"),
            autoescape=select_autoescape(("html",)),
        )
        cls.request = SimpleNamespace(
            state=SimpleNamespace(csrf_token="token"),
            url=SimpleNamespace(path="/research/cases/case-1"),
        )

    def test_case_workspace_renders_chain_capture_counterevidence_and_provenance(self):
        rendered = self.env.get_template("research_case.html").render(
            request=self.request,
            app_asset_version="test",
            research_case={
                "title": "Grid equipment constraint",
                "current_version": 3,
                "last_changed_at": "2026-08-08T10:00:00Z",
                "lifecycle_state": "research_ready",
                "case_type": "structural",
                "horizon": "months",
                "origin": "discovered",
                "definition": "Equipment capacity is tightening.",
                "economic_significance": "high",
                "market_sensitivity": "moderate",
                "persistence": "high",
                "breadth": "moderate",
                "investability": None,
                "evidence_strength": "high",
                "model_slug": "model",
                "prompt_version": "research_deliverable_v1",
                "input_fingerprint": "fingerprint",
            },
            deliverable={
                "what_changed": {"text": "Backlogs increased."},
                "why_it_matters": {"text": "Capacity can constrain supply."},
                "transmission_text": "Demand → capacity → pricing.",
                "potential_capture": [
                    {
                        "node_name": "Transformer equipment",
                        "node_type": "technology",
                        "text": "Pricing may accrue if supply remains constrained.",
                    }
                ],
                "evidence_for": [{"text": "Backlog evidence"}],
                "evidence_against": [{"text": "Capacity additions"}],
                "weak_links_unknowns": ["Company margin attribution"],
                "what_to_watch": ["Lead times"],
            },
            detail={
                "causal_edges": [
                    {
                        "from_name": "AI demand",
                        "relationship": "raises_demand_for",
                        "to_name": "Grid equipment",
                        "mechanism": "Construction requires power equipment.",
                        "epistemic_state": "hypothesis",
                        "confidence": 0.7,
                        "depth": 2,
                        "missing_evidence": ["Unit orders"],
                        "break_conditions": ["Capacity expands"],
                    }
                ],
                "value_capture": [
                    {
                        "node_name": "Grid equipment",
                        "node_type": "technology",
                        "demand_impulse": "high",
                        "scarcity": "high",
                        "pricing_power": "moderate",
                        "margin_sensitivity": None,
                        "supply_responsiveness": "low",
                        "public_market_investability": None,
                    }
                ],
                "counterevidence": [
                    {
                        "kind": "alternative_explanation",
                        "epistemic_state": "hypothesis",
                        "statement": "Orders may be double counted.",
                        "rationale": "Customer overlap is unresolved.",
                    }
                ],
                "data_requests": [
                    {
                        "subject": "Transformer lead times",
                        "priority": "high",
                        "status": "unresolved",
                        "requested_evidence_type": "industry_capacity",
                        "reason": "Weakest edge",
                        "desired_frequency": "monthly",
                    }
                ],
                "evidence": [
                    {
                        "title": "Backlog update",
                        "relationship": "supporting",
                        "evidence_type": "filing_delta",
                        "source_name": "company filing",
                        "source_timestamp": "2026-08-08T09:00:00Z",
                        "excerpt": "Backlog rose.",
                        "source_reference": "https://example.test/filing",
                    }
                ],
            },
            history=[
                {
                    "version": 3,
                    "lifecycle_state": "research_ready",
                    "created_at": "2026-08-08T10:00:00Z",
                    "change_summary": "Evidence strengthened",
                }
            ],
        )

        for text in (
            "Grid equipment constraint",
            "What changed",
            "Potential economic capture",
            "AI demand",
            "hypothesis",
            "Value-capture assessment",
            "unknown",
            "Orders may be double counted.",
            "Transformer lead times",
            "Evidence and provenance",
            "research_deliverable_v1",
            "Version history",
        ):
            self.assertIn(text, rendered)
        self.assertNotIn("BUY", rendered)
        self.assertNotIn("SELL", rendered)


if __name__ == "__main__":
    unittest.main()
