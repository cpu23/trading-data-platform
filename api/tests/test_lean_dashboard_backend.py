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
        "DASHBOARD_USER": "test",
        "DASHBOARD_PASSWORD": "test",
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
    "event_pipeline": {"sse": {"enabled": True}},
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

SLV = {"available": True, "marker": "2026-08-06T00:00:00+00:00", "sections": [], "counts": {}}


def make_app(*, auth=True, capturing_templates=False):
    from fastapi import Depends, FastAPI
    from fastapi.templating import Jinja2Templates

    from auth import verify_credentials
    from routes.views.dashboard import router as dashboard_router
    from routes.views.markets import router as markets_router

    dependencies = [Depends(verify_credentials)] if auth else None
    app = FastAPI(dependencies=dependencies)
    if capturing_templates:
        app.state.templates = CapturingTemplates()
    else:
        app.state.templates = Jinja2Templates(directory=API_ROOT / "templates")
    app.include_router(markets_router)
    app.include_router(dashboard_router)
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
    def test_markets_is_authenticated_and_composes_all_surfaces(self):
        from fastapi.testclient import TestClient

        app = make_app()
        client = TestClient(app)
        self.assertEqual(client.get("/markets").status_code, 401)
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
                "routes.views.dashboard._load_compact_strip",
                return_value=COMPACT_STRIP,
            ),
            patch(
                "routes.views.dashboard.load_briefing_delta",
                return_value=BRIEFING_DELTA,
            ),
            patch(
                "routes.views.dashboard._last_cycle_text",
                return_value="No cycle run yet",
            ),
            patch(
                "routes.views.dashboard._latest_cycle_status",
                return_value="unknown",
            ),
            patch(
                "routes.views.dashboard._get_dashboard_health",
                new_callable=AsyncMock,
                return_value={"components": []},
            ),
            # Removed datasets: must never be invoked by the lean page.
            patch("routes.views.dashboard.get_events_upcoming_data") as events,
            patch("routes.views.dashboard.get_macro_release_cards_data") as releases,
            patch("routes.views.dashboard.get_macro_dashboard") as macro,
            patch("routes.views.dashboard._get_latest_prices") as prices,
            patch("routes.views.dashboard.get_budget_status") as budget,
            patch("routes.views.dashboard.load_story_context") as stories,
            patch("routes.views.cockpit_panels.load_top_strip") as full_strip,
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
            "live_updates_enabled",
            "data_status",
            "current_time",
            "last_cycle_text",
            "last_cycle_status",
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
            "timedelta",
        }
        self.assertTrue(removed_keys.isdisjoint(context.keys()))

        for loader in (events, releases, macro, prices, budget, stories):
            loader.assert_not_called()
        full_strip.assert_not_called()

        import routes.views.dashboard as dashboard_module

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
                "routes.views.dashboard._load_compact_strip",
                return_value=COMPACT_STRIP,
            ),
            patch(
                "routes.views.dashboard.load_briefing_delta",
                return_value=BRIEFING_DELTA,
            ),
            patch(
                "routes.views.dashboard._last_cycle_text",
                return_value="No cycle run yet",
            ),
            patch(
                "routes.views.dashboard._latest_cycle_status",
                return_value="unknown",
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
            {"direction_chips", "source_health", "budget", "last_price_update",
             "last_material_event"} & set(strip.keys())
        )


class MarketPartialAliasTests(unittest.TestCase):
    def _assert_aliases_identical(self, canonical, legacy, patches):
        from fastapi.testclient import TestClient

        app = make_app()
        client = TestClient(app)
        with patch(
            "routes.views.markets.app_config.load_config",
            return_value=MOCK_CONFIG,
        ), patches:
            canonical_response = client.get(canonical, headers=AUTH)
            legacy_response = client.get(legacy, headers=AUTH)
        self.assertEqual(canonical_response.status_code, 200)
        self.assertEqual(legacy_response.status_code, 200)
        self.assertEqual(canonical_response.text, legacy_response.text)

    def test_macro_releases_canonical_and_legacy_alias(self):
        self._assert_aliases_identical(
            "/partials/markets/macro-releases",
            "/partials/dashboard/macro-releases",
            patch(
                "routes.views.markets.get_macro_release_cards_data",
                return_value={"cards": []},
            ),
        )

    def test_regime_canonical_and_legacy_alias(self):
        self._assert_aliases_identical(
            "/partials/markets/regime",
            "/partials/regime",
            patch(
                "routes.views.markets.get_regime_current",
                return_value={"regime": "risk_on", "sub_regime": "growth"},
            ),
        )

    def test_indicators_canonical_and_legacy_alias(self):
        self._assert_aliases_identical(
            "/partials/markets/indicators",
            "/partials/indicators",
            patch(
                "routes.views.markets.get_macro_dashboard",
                return_value={"indicators": []},
            ),
        )

    def test_events_canonical_and_legacy_alias(self):
        self._assert_aliases_identical(
            "/partials/markets/events",
            "/partials/events",
            patch(
                "routes.views.markets.get_events_upcoming_data",
                return_value={"events": [], "grouped": {}},
            ),
        )

    def test_dashboard_compat_partials_still_serve(self):
        """Drawer/watchlist/news compatibility URLs keep working unchanged."""
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
                "routes.views.dashboard._get_latest_prices",
                return_value={},
            ),
            patch(
                "routes.views.dashboard.load_story_context",
                return_value={"status": "empty", "clusters": [], "lanes": {}},
            ),
        ):
            self.assertEqual(
                client.get("/partials/dashboard/news", headers=AUTH).status_code,
                200,
            )
            self.assertEqual(
                client.get("/partials/cards", headers=AUTH).status_code,
                200,
            )
            self.assertEqual(
                client.get("/partials/dashboard/watchlist", headers=AUTH).status_code,
                200,
            )
            self.assertEqual(
                client.get("/partials/cards/clear", headers=AUTH).status_code,
                200,
            )


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
                ["What changed", "Current interpretation", "What would invalidate this"],
            )
            self.assertEqual(context["briefing_delta"], BRIEFING_DELTA)
            self.assertEqual(context["briefing_delta"]["bullets"], BRIEFING_DELTA["bullets"])
            self.assertEqual(context["briefing_delta"]["atoms"], BRIEFING_DELTA["atoms"])
            self.assertIn("live_updates_enabled", context)


if __name__ == "__main__":
    unittest.main()
