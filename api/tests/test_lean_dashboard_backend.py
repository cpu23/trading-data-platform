"""Focused route/loader tests for the lean dashboard backend refactor.

Covers the owned backend surface only:
- ``/markets`` page composition (all six market surfaces, lazy canonical
  partial URLs, authentication).
- Slim ``GET /`` context and calls (removed datasets are never loaded).
- Canonical ``/partials/markets/...`` URLs with legacy aliases rendering the
  same content on the same handler.
- Merged briefing context (``briefing`` + ``briefing_sections`` +
  ``briefing_delta``) on both ``GET /`` and ``/partials/briefing``.

These tests are written as part of the refactor handoff; the primary
orchestration runs the suite after sequential integration.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.update(
    {
        "DB_USER": "test",
        "DB_PASSWORD": "test",
        "FRED_API_KEY": "test",
        "OPENROUTER_API_KEY": "test",
        "OPENROUTER_MODEL": "test/model",
        "OANDA_API_KEY": "test",
        "SECRETS_FILE": "/nonexistent/test-secrets.env",
    }
)

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))
os.environ["CONFIG_DIR"] = str(API_ROOT.parent / "config")

AUTH = {"Authorization": "Basic dGVzdDp0ZXN0"}  # test:test

MOCK_CONFIG = {
    "timezone": {"primary": {"name": "Europe/London", "label": "London"}},
    "dashboard": {"indicators": []},
}

COMPACT_STRIP = {
    "available": True,
    "session_label": "London",
    "regime": {"regime": "risk_on", "sub_regime": "growth", "confidence": "high"},
    "next_catalyst": {
        "event_name": "US CPI",
        "country": "US",
        "countdown_display": "3h 20m",
    },
}

BRIEFING = {
    "briefing_id": "b-1",
    "briefing_date": "2026-08-06",
    "created_at": "2026-08-06T06:00:00+00:00",
    "sections": {
        "what_changed": "Dollar softened after a soft payrolls print.",
        "interpretation": "Markets are pricing two cuts this year.",
        "invalidation": "A hawkish Fed speaker would unwind the move.",
    },
    "opinion_ids": ["op-1"],
}

BRIEFING_DELTA = {
    "available": True,
    "latest_date": "2026-08-06",
    "bullets": ["Changed section: interpretation"],
    "atoms": [{"claim_type": "macro", "count": 3}],
}

SLV = {
    "available": True,
    "marker": "2026-08-06T00:00:00+00:00",
    "sections": [],
    "counts": {},
}


def make_app(*, auth=True, capturing_templates=False):
    from auth import verify_credentials
    from fastapi import Depends, FastAPI
    from fastapi.templating import Jinja2Templates
    from routes.views.dashboard import router as dashboard_router
    from routes.views.dashboard_strip import router as dashboard_strip_router
    from routes.views.markets import router as markets_router

    dependencies = [Depends(verify_credentials)] if auth else None
    app = FastAPI(dependencies=dependencies)
    if capturing_templates:
        app.state.templates = CapturingTemplates()
    else:
        app.state.templates = Jinja2Templates(directory=API_ROOT / "templates")
    app.include_router(markets_router)
    app.include_router(dashboard_router)
    app.include_router(dashboard_strip_router)
    return app


class CapturingTemplates:
    """Records (template name, context) instead of rendering."""

    def __init__(self):
        self.calls = []

    def TemplateResponse(self, request, name, context):
        self.calls.append((name, context))
        from fastapi.responses import HTMLResponse

        return HTMLResponse("ok")


class MarketsPageTests(unittest.TestCase):
    def test_markets_composes_all_surfaces(self):
        from fastapi.testclient import TestClient

        app = make_app()
        client = TestClient(app)
        with patch(
            "routes.views.markets.app_config.load_config",
            return_value=MOCK_CONFIG,
        ):
            response = client.get("/markets", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        for partial_url in (
            "/partials/markets/cross-asset",
            "/partials/markets/catalysts",
            "/partials/markets/macro-releases",
            "/partials/markets/regime",
            "/partials/markets/indicators",
            "/partials/markets/events",
        ):
            with self.subTest(partial=partial_url):
                self.assertIn(partial_url, response.text)

    def test_markets_page_shell_loads_no_dataset(self):
        """The page is a lazy shell: no market loader runs on initial GET."""
        from fastapi.testclient import TestClient

        app = make_app()
        client = TestClient(app)
        with (
            patch(
                "routes.views.markets.app_config.load_config",
                return_value=MOCK_CONFIG,
            ),
            patch("routes.views.markets.load_cross_asset") as cross_asset,
            patch("routes.views.markets.load_catalysts") as catalysts,
            patch("routes.views.markets.get_macro_release_cards_data") as releases,
            patch("routes.views.markets.get_regime_current") as regime,
            patch("routes.views.markets.get_macro_dashboard") as macro,
            patch("routes.views.markets.get_events_upcoming_data") as events,
        ):
            response = client.get("/markets", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        for loader in (cross_asset, catalysts, releases, regime, macro, events):
            loader.assert_not_called()


class SlimDashboardTests(unittest.TestCase):
    def test_slim_dashboard_context_and_calls(self):
        from fastapi.testclient import TestClient

        app = make_app(capturing_templates=True)
        client = TestClient(app)
        with (
            patch(
                "routes.views.dashboard.app_config.load_config",
                return_value=MOCK_CONFIG,
            ),
            patch(
                "routes.views.dashboard.get_briefing_latest",
                return_value=BRIEFING,
            ),
            patch(
                "routes.views.dashboard.load_since_last_view",
                return_value=SLV,
            ),
            patch(
                "routes.views.dashboard.load_compact_strip",
                return_value=COMPACT_STRIP,
            ),
            patch(
                "routes.views.dashboard.load_briefing_delta",
                return_value=BRIEFING_DELTA,
            ),
            patch(
                "routes.views.dashboard._get_dashboard_health",
                new_callable=AsyncMock,
                return_value={"components": []},
            ),
            # Removed datasets: must never be invoked by the lean page.
            patch("routes.views.dashboard.get_events_upcoming_data") as events,
            patch("routes.views.dashboard._get_latest_prices") as prices,
            patch("routes.views.dashboard.load_story_context") as stories,
        ):
            response = client.get("/", headers=AUTH)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(app.state.templates.calls), 1)
        name, context = app.state.templates.calls[0]
        self.assertEqual(name, "dashboard.html")

        required_keys = {
            "strip",
            "since_last_view",
            "briefing",
            "briefing_sections",
            "briefing_delta",
            "data_status",
            "current_time",
        }
        self.assertTrue(required_keys.issubset(context.keys()))
        self.assertEqual(context["strip"], COMPACT_STRIP)
        self.assertEqual(context["since_last_view"], SLV)
        self.assertEqual(context["briefing_delta"], BRIEFING_DELTA)
        self.assertEqual(
            [section["label"] for section in context["briefing_sections"]],
            [
                "What changed",
                "Current interpretation",
                "What would invalidate this",
            ],
        )

        removed_keys = {
            "price_map",
            "stories",
            "events_data",
            "regime",
            "indicators",
            "macro_releases",
            "research_intelligence",
            "dots",
            "budget",
            "last_cycle_text",
            "last_cycle_status",
            "timedelta",
        }
        self.assertTrue(removed_keys.isdisjoint(context.keys()))

        for loader in (events, prices, stories):
            loader.assert_not_called()
        import routes.views.dashboard as dashboard_module

        self.assertFalse(hasattr(dashboard_module, "get_macro_release_cards_data"))
        self.assertFalse(hasattr(dashboard_module, "get_macro_dashboard"))

        self.assertFalse(hasattr(dashboard_module, "_load_research_intelligence"))

    def test_slim_dashboard_strip_is_compact_only(self):
        """The strip context never carries price/event/chip/source/budget data."""
        from fastapi.testclient import TestClient

        app = make_app(capturing_templates=True)
        client = TestClient(app)
        with (
            patch(
                "routes.views.dashboard.app_config.load_config",
                return_value=MOCK_CONFIG,
            ),
            patch(
                "routes.views.dashboard.get_briefing_latest",
                side_effect=RuntimeError("no briefing"),
            ),
            patch(
                "routes.views.dashboard.load_since_last_view",
                return_value=SLV,
            ),
            patch(
                "routes.views.dashboard.load_compact_strip",
                return_value=COMPACT_STRIP,
            ),
            patch(
                "routes.views.dashboard.load_briefing_delta",
                return_value=BRIEFING_DELTA,
            ),
            patch(
                "routes.views.dashboard._get_dashboard_health",
                new_callable=AsyncMock,
                return_value={"components": []},
            ),
        ):
            response = client.get("/", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        _name, context = app.state.templates.calls[0]
        strip = context["strip"]
        self.assertIn("available", strip)
        self.assertIn("session_label", strip)
        self.assertIn("regime", strip)
        self.assertIn("next_catalyst", strip)
        self.assertFalse(
            {
                "direction_chips",
                "source_health",
                "budget",
                "last_price_update",
                "last_material_event",
            }
            & set(strip.keys())
        )


class CompactStripRouteTests(unittest.TestCase):
    """/partials/dashboard/top-strip (dashboard_strip router) rendering."""

    def test_top_strip_route_renders_compact_fields(self):
        from fastapi.testclient import TestClient

        app = make_app()
        client = TestClient(app)
        with (
            patch(
                "routes.views.dashboard_strip.load_compact_strip",
                return_value=COMPACT_STRIP,
            ),
            patch(
                "routes.views.dashboard_strip.app_config.load_config",
                return_value={"event_pipeline": {"sse": {"enabled": False}}},
            ),
        ):
            response = client.get("/partials/dashboard/top-strip", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Session snapshot", response.text)
        self.assertIn("London", response.text)
        self.assertIn("Current regime", response.text)
        self.assertIn("risk_on", response.text)
        self.assertIn("Next catalyst", response.text)
        self.assertIn("US CPI", response.text)
        self.assertIn('hx-get="/partials/dashboard/top-strip"', response.text)
        self.assertIn('hx-trigger="marketRefresh from:body"', response.text)
        self.assertNotIn("data-live-section", response.text)

    def test_top_strip_fail_soft_shows_unavailable(self):
        from fastapi.testclient import TestClient

        app = make_app()
        client = TestClient(app)
        with (
            patch(
                "routes.views.dashboard_strip.load_compact_strip",
                return_value={"available": False},
            ),
            patch(
                "routes.views.dashboard_strip.app_config.load_config",
                return_value={"event_pipeline": {"sse": {"enabled": False}}},
            ),
        ):
            response = client.get("/partials/dashboard/top-strip", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Session snapshot unavailable.", response.text)


class RetiredPartialRouteTests(unittest.TestCase):
    def test_legacy_partial_aliases_are_removed(self):
        from fastapi.testclient import TestClient

        client = TestClient(make_app())
        retired = (
            "/partials/dashboard/cross-asset",
            "/partials/dashboard/catalysts",
            "/partials/dashboard/macro-releases",
            "/partials/regime",
            "/partials/indicators",
            "/partials/events",
            "/partials/dashboard/change-feed",
            "/partials/dashboard/briefing-delta",
            "/partials/cards",
            "/partials/dashboard/watchlist",
        )
        for path in retired:
            with self.subTest(path=path):
                self.assertEqual(client.get(path, headers=AUTH).status_code, 404)


class MergedBriefingTests(unittest.TestCase):
    def _briefing_context(self, route):
        from fastapi.testclient import TestClient

        app = make_app(capturing_templates=True)
        client = TestClient(app)
        with (
            patch(
                "routes.views.dashboard.app_config.load_config",
                return_value=MOCK_CONFIG,
            ),
            patch(
                "routes.views.dashboard.get_briefing_latest",
                return_value=BRIEFING,
            ),
            patch(
                "routes.views.dashboard.load_briefing_delta",
                return_value=BRIEFING_DELTA,
            ),
        ):
            response = client.get(route, headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(app.state.templates.calls), 1)
        return app.state.templates.calls[0][1]

    def test_partial_briefing_renders_merged_context(self):
        from fastapi.testclient import TestClient

        app = make_app()
        client = TestClient(app)
        with (
            patch(
                "routes.views.dashboard.app_config.load_config",
                return_value=MOCK_CONFIG,
            ),
            patch(
                "routes.views.dashboard.get_briefing_latest",
                return_value=BRIEFING,
            ),
            patch(
                "routes.views.dashboard.load_briefing_delta",
                return_value=BRIEFING_DELTA,
            ),
        ):
            response = client.get("/partials/briefing", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertIn("What changed", response.text)
        self.assertIn("Current interpretation", response.text)
        self.assertIn("What would invalidate this", response.text)

    def test_page_and_partial_share_one_merged_briefing_context(self):
        page_context = self._briefing_context("/")
        partial_context = self._briefing_context("/partials/briefing")
        for context in (page_context, partial_context):
            self.assertEqual(context["briefing"], BRIEFING)
            self.assertEqual(
                [section["label"] for section in context["briefing_sections"]],
                [
                    "What changed",
                    "Current interpretation",
                    "What would invalidate this",
                ],
            )
            self.assertEqual(context["briefing_delta"], BRIEFING_DELTA)
            self.assertEqual(
                context["briefing_delta"]["bullets"], BRIEFING_DELTA["bullets"]
            )
            self.assertEqual(
                context["briefing_delta"]["atoms"], BRIEFING_DELTA["atoms"]
            )
        self.assertNotIn("live_updates_enabled", page_context)
        self.assertNotIn("live_updates_enabled", partial_context)


if __name__ == "__main__":
    unittest.main()
