"""Frontend contract tests for the unified marketRefresh heartbeat.

The lean dashboard owns exactly one periodic timer in the browser: an
idempotent heartbeat that dispatches a bubbling ``marketRefresh`` CustomEvent
from ``document.body`` every 90 seconds. The dispatch pauses while the
document is hidden and fires one immediate refresh when visibility returns.
SSE-enabled sections are invalidated by the server-sent stream while it is
healthy; when EventSource is missing or disconnected the same heartbeat
drives registered ``data-live-*`` sections, so no second interval ever
exists. HTMX swaps and settles rebind dynamic UI without creating timers.

These tests assert the source-level structure that produces that behavior
(following the repo's static frontend-contract conventions) and are not
executed as part of the refactor validation per orchestration constraints.
"""

import sys
import unittest
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))


class MarketRefreshHeartbeatContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_js = (API_ROOT / "static/app.js").read_text()

    def test_single_interval_owner_at_ninety_second_cadence(self):
        # Exactly one periodic timer exists in the whole client and it
        # dispatches marketRefresh at the 90-second fallback cadence.
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

    def test_heartbeat_creation_is_idempotent_across_init_calls(self):
        # ensureMarketRefresh is the single choke point for timer creation and
        # listener registration; repeated boot, swap, and settle invocations
        # cannot produce a second interval or duplicate body listeners.
        self.assertIn("function ensureMarketRefresh()", self.app_js)
        self.assertIn("if (marketRefreshTimer || document.hidden) return;", self.app_js)
        self.assertIn("if (!refreshBound) {", self.app_js)
        self.assertIn("refreshBound = true;", self.app_js)
        self.assertIn("function initLiveSections() {\n    ensureMarketRefresh();", self.app_js)
        self.assertIn("ensureMarketRefresh();", self.app_js)

    def test_initial_hidden_startup_does_not_create_interval(self):
        # If DOMContentLoaded fires while the tab is already hidden (background
        # load or restore), ensureMarketRefresh must still bind the
        # visibilitychange listener idempotently but must NOT create the
        # interval; the restoration path starts it on first visibility return.
        self.assertIn(
            "if (marketRefreshTimer || document.hidden) return;", self.app_js
        )
        self.assertIn(
            "if (!refreshBound) {\n      refreshBound = true;", self.app_js
        )

    def test_periodic_dispatch_pauses_while_document_is_hidden(self):
        self.assertIn(
            "document.addEventListener('visibilitychange', handleVisibilityChange)",
            self.app_js,
        )
        self.assertIn("if (document.hidden) {", self.app_js)
        self.assertIn("window.clearInterval(marketRefreshTimer);", self.app_js)
        self.assertIn("marketRefreshTimer = null;", self.app_js)

    def test_visibility_restoration_dispatches_one_immediate_refresh(self):
        # Returning to the visible tab dispatches one marketRefresh right away
        # and restarts the paused heartbeat timer.
        self.assertIn(
            "dispatchMarketRefresh();\n    ensureMarketRefresh();", self.app_js
        )

    def test_no_second_sse_fallback_interval_remains(self):
        # The separate 45s live-polling interval and its helpers are gone;
        # the heartbeat is the only interval left.
        for removed in (
            "livePollingTimer",
            "startLivePolling",
            "stopLivePolling",
            "LIVE_POLL_INTERVAL_MS",
        ):
            self.assertNotIn(removed, self.app_js)
        self.assertEqual(self.app_js.count("setInterval("), 1)

    def test_sse_healthy_stream_suppresses_heartbeat_refresh_of_live_sections(self):
        # While the stream is open, SSE owns live-section invalidation; the
        # heartbeat must not poll registered live sections.
        self.assertIn("var liveStreamHealthy = false;", self.app_js)
        self.assertIn(
            "if (liveStreamHealthy || !registeredLiveSections().length) return;",
            self.app_js,
        )
        self.assertIn(
            "liveStream.onopen = function () { liveStreamHealthy = true; };",
            self.app_js,
        )
        self.assertIn(
            "liveStream.onerror = function () { liveStreamHealthy = false; };",
            self.app_js,
        )

    def test_disconnected_sse_drives_live_sections_through_htmx_only(self):
        # With EventSource missing or disconnected the heartbeat refreshes
        # registered live sections through existing HTMX endpoints; no
        # fetch-based section loader and no second interval.
        self.assertIn("refreshLiveSectionsOnHeartbeat", self.app_js)
        self.assertIn("refreshLiveSections();", self.app_js)
        self.assertIn("request = window.htmx.ajax('GET', url, {", self.app_js)
        self.assertNotIn("livePollingTimer", self.app_js)

    def test_htmx_lifecycle_cannot_duplicate_intervals(self):
        # Swaps and settles re-run initDynamicUi -> initLiveSections, which
        # funnels into the guarded ensureMarketRefresh; nothing in the swap
        # path calls setInterval directly.
        self.assertIn("['htmx:afterSwap', 'htmx:afterSettle'].forEach", self.app_js)
        self.assertIn("initDynamicUi(evt.detail.target)", self.app_js)
        self.assertIn("function initDynamicUi(root) {", self.app_js)
        self.assertIn("initLiveSections();", self.app_js)
        self.assertIn("if (marketRefreshTimer || document.hidden) return;", self.app_js)

    def test_cycle_complete_and_quote_stream_behavior_preserved(self):
        self.assertIn(
            "document.body.dispatchEvent("
            "new CustomEvent('cycleComplete', { bubbles: true }))",
            self.app_js,
        )
        self.assertIn("var source = new EventSource('/api/quotes/stream');", self.app_js)


if __name__ == "__main__":
    unittest.main()
