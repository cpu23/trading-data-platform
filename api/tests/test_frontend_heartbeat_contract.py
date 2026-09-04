"""Frontend contracts for the single polling heartbeat."""

import unittest
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]


class MarketRefreshHeartbeatContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_js = (API_ROOT / "static/app.js").read_text()

    def test_single_interval_owner_at_ninety_second_cadence(self):
        self.assertEqual(self.app_js.count("setInterval("), 1)
        self.assertIn("var MARKET_REFRESH_INTERVAL_MS = 90000;", self.app_js)
        self.assertIn(
            "marketRefreshTimer = window.setInterval("
            "dispatchMarketRefresh, MARKET_REFRESH_INTERVAL_MS)",
            self.app_js,
        )

    def test_heartbeat_dispatches_bubbling_market_refresh_from_body(self):
        self.assertIn(
            "document.body.dispatchEvent("
            "new CustomEvent('marketRefresh', { bubbles: true }))",
            self.app_js,
        )

    def test_timer_creation_is_idempotent(self):
        self.assertIn("function ensureMarketRefresh()", self.app_js)
        self.assertIn("if (marketRefreshTimer || document.hidden) return;", self.app_js)
        self.assertIn("if (!refreshBound) {", self.app_js)
        self.assertIn("refreshBound = true;", self.app_js)
        self.assertIn("ensureMarketRefresh();", self.app_js)

    def test_hidden_documents_pause_polling(self):
        self.assertIn(
            "document.addEventListener('visibilitychange', handleVisibilityChange)",
            self.app_js,
        )
        self.assertIn("if (document.hidden) {", self.app_js)
        self.assertIn("window.clearInterval(marketRefreshTimer);", self.app_js)
        self.assertIn("marketRefreshTimer = null;", self.app_js)
        self.assertIn(
            "dispatchMarketRefresh();\n    ensureMarketRefresh();",
            self.app_js,
        )

    def test_no_stream_or_imperative_section_loader_remains(self):
        for removed in (
            "EventSource",
            "livePollingTimer",
            "startLivePolling",
            "stopLivePolling",
            "initLiveSections",
            "refreshLiveSections",
            "data-live-section",
        ):
            self.assertNotIn(removed, self.app_js)

    def test_htmx_lifecycle_does_not_create_timers(self):
        self.assertIn("['htmx:afterSwap', 'htmx:afterSettle'].forEach", self.app_js)
        self.assertIn("initDynamicUi(target)", self.app_js)
        self.assertIn("function initDynamicUi(root) {", self.app_js)
        self.assertEqual(self.app_js.count("setInterval("), 1)

    def test_topology_swaps_rebind_once_and_restore_focus(self):
        self.assertIn(
            "if (section.dataset.topologyBound === 'true') return;", self.app_js
        )
        self.assertIn("function toggleTopologyNode(section, node)", self.app_js)
        self.assertEqual(self.app_js.count("toggleTopologyNode(section, node);"), 2)
        self.assertIn("node.focus({preventScroll: true});", self.app_js)
        self.assertIn(
            "var replacement = target.id ? document.getElementById(target.id) : null;",
            self.app_js,
        )

    def test_cycle_complete_behavior_is_preserved(self):
        self.assertIn(
            "document.body.dispatchEvent("
            "new CustomEvent('cycleComplete', { bubbles: true }))",
            self.app_js,
        )


if __name__ == "__main__":
    unittest.main()
