import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path


API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))


class Phase10FrontendContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_js = (API_ROOT / "static/app.js").read_text()
        cls.css = (API_ROOT / "static/style.css").read_text()
        cls.base = (API_ROOT / "templates/base.html").read_text()
        cls.header = (API_ROOT / "templates/partials/header.html").read_text()
        cls.events = (API_ROOT / "templates/partials/events_section.html").read_text()
        cls.cards = (API_ROOT / "templates/partials/cards_section.html").read_text()
        cls.expansion = (API_ROOT / "templates/partials/expansion_content.html").read_text()
        cls.regime = (API_ROOT / "templates/partials/regime_section.html").read_text()
        cls.news = (API_ROOT / "templates/partials/news_section.html").read_text()
        cls.navigation = (API_ROOT / "templates/partials/navigation.html").read_text()

    def test_page_declares_inline_favicon_to_avoid_browser_404(self):
        self.assertIn('<link rel="icon" href="data:,">', self.base)

    def test_charts_initialize_on_dom_and_htmx_lifecycle_idempotently(self):
        self.assertIn("function initCharts(root)", self.app_js)
        self.assertIn("initCharts(document);", self.app_js)
        self.assertIn("htmx:afterSwap", self.app_js)
        self.assertIn("htmx:afterSettle", self.app_js)
        self.assertIn("initCharts(evt.detail.target)", self.app_js)
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
        for marker in ('class="calendar-scroll scroll-affordance"', '<table class="calendar-table">', '<thead>', 'scope="col"', '<tbody>'):
            self.assertIn(marker, self.events)
        for heading in ("Time", "Event", "Market", "Impact", "Actual", "Forecast", "Previous"):
            self.assertIn(heading, self.events)
        for impact in ("High", "Medium", "Low"):
            self.assertIn(impact, self.events)
        self.assertIn("current_timezone", self.events)
        self.assertIn("events_data.get('error')", self.events)
        self.assertIn("overflow-x: auto", self.css)

    def test_symbol_links_use_only_central_exact_allowlist(self):
        from routes.views.dashboard import SYMBOL_LINKS

        expected = {
            "AUDJPY": "https://finance.yahoo.com/quote/AUDJPY=X",
            "XAUUSD": "https://finance.yahoo.com/quote/GC=F",
            "XPTUSD": "https://finance.yahoo.com/quote/PL=F",
            "JP225": "https://finance.yahoo.com/quote/%5EN225",
            "UK100": "https://finance.yahoo.com/quote/%5EFTSE",
            "DE40": "https://finance.yahoo.com/quote/%5EGDAXI",
            "EURCHF": "https://finance.yahoo.com/quote/EURCHF=X",
        }
        self.assertEqual({key: value["url"] for key, value in SYMBOL_LINKS.items()}, expected)
        for source in (self.cards, self.expansion):
            self.assertIn("symbol_links.get", source)
            self.assertIn('target="_blank"', source)
            self.assertIn('rel="noopener noreferrer"', source)
            self.assertIn("View", source)
            self.assertNotIn("finance.yahoo.com/quote/{{", source)

    def test_header_wires_timezone_and_operations_without_duplicate_refresh(self):
        self.assertEqual(self.header.count('id="run-cycle-btn"'), 1)
        self.assertIn('partials/navigation.html', self.header)
        for href in ('/', '/logs', '/quality', '/news', '/operations'):
            self.assertIn(f'href="{href}"', self.navigation)
        self.assertIn('id="timezone-status"', self.header)
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
        with patch("routes.views.dashboard.get_system_health", return_value={"overall": "healthy"}) as health:
            self.assertEqual(asyncio.run(_get_dashboard_health(request)), {"overall": "healthy"})
        health.assert_awaited_once_with(request)

    def test_dashboard_news_is_bounded_and_has_truthful_empty_state(self):
        from routes.views.news import load_news_context

        self.assertIn('id="news-section"', self.news)
        self.assertIn("not_published", self.news)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.json"
            path.write_text(json.dumps({
                "generated_at": "2026-07-16T12:00:00+00:00",
                "items": [
                    {"title": f"Item {index}", "source_label": "Demo", "published": "2026-07-16T12:00:00+00:00"}
                    for index in range(7)
                ],
            }))
            context = load_news_context({"news_feed": {"output_path": directory}}, limit=5)
        self.assertEqual(context["status"], "published")
        self.assertEqual(len(context["items"]), 5)


if __name__ == "__main__":
    unittest.main()
