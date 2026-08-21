import os
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

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
        "DEPLOYMENT_MODE": "test",
        "SECRETS_FILE": "/nonexistent/test-secrets.env",
    }
)

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))
os.environ["CONFIG_DIR"] = str(API_ROOT.parent / "config")

AUTH = {"Authorization": "Basic dGVzdDp0ZXN0"}
NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
CLUSTER_ID = UUID("11111111-1111-4111-8111-111111111111")


def story_row(*, evidence_count=8, lane="low_confidence"):
    return {
        "id": CLUSTER_ID,
        "canonical_key": "story:key",
        "title": "Fed holds rates",
        "summary": "Canonical summary",
        "state": "developing",
        "lane": lane,
        "first_seen_at": NOW,
        "last_seen_at": NOW,
        "last_material_change_at": NOW,
        "importance": 0.8,
        "novelty": 0.4,
        "confidence": 0.5,
        "entities": [{"canonical_id": "FED", "display_name": "Federal Reserve"}],
        "markets": [{"canonical_id": "EURUSD", "symbol": "EURUSD"}],
        "source_count": 1,
        "version": 1,
        "change_summary": "Initial report",
        "evidence": [
            {
                "source": "reuters",
                "source_label": "Reuters",
                "title": f"Evidence {index}",
                "summary": "Evidence summary",
                "url": "https://example.com/story",
                "published_at": NOW.isoformat(),
                "similarity_score": 0.9,
                "contribution_type": "repeated_coverage",
                "materially_changed": False,
                "raw_payload": "must-not-escape",
            }
            for index in range(evidence_count)
        ],
        "market_confirmations": [
            {
                "market_symbol": "EURUSD",
                "headline_at": NOW.isoformat(),
                "observed_at": NOW.isoformat(),
                "pre_headline_move": 0.1,
                "move_5m": 0.4,
                "move_30m": -0.2,
                "move_session": None,
                "flags": ["confirmed_by_market"],
                "missing_reasons": {"move_session": "not_due"},
                "provenance": {"private": "must-not-escape"},
            }
        ],
    }


class NewsStoryQueryTests(unittest.TestCase):
    def test_query_caps_limit_offset_and_public_timeline(self):
        from routes.views.news import load_story_context

        with patch("routes.views.news.query_many", return_value=[story_row()]) as query:
            payload = load_story_context(limit=999, offset=999999)
        self.assertEqual(payload["status"], "published")
        self.assertEqual(query.call_args.args[1]["limit"], 100)
        self.assertEqual(query.call_args.args[1]["offset"], 10_000)
        self.assertIn("LIMIT 5", query.call_args.args[0])
        self.assertIn("LIMIT 20", query.call_args.args[0])
        self.assertEqual(len(payload["clusters"]), 1)
        self.assertEqual(len(payload["clusters"][0]["evidence"]), 5)
        self.assertNotIn("raw_payload", payload["clusters"][0]["evidence"][0])
        self.assertNotIn(
            "provenance", payload["clusters"][0]["market_confirmations"][0]
        )

    def test_invalid_filters_are_rejected_before_database_access(self):
        from fastapi import HTTPException

        from routes.views.news import load_story_context

        with patch("routes.views.news.query_many") as query:
            with self.assertRaises(HTTPException) as caught:
                load_story_context(lane="private-payloads")
        self.assertEqual(caught.exception.status_code, 422)
        query.assert_not_called()

    def test_database_failure_is_generic_and_fail_soft(self):
        from routes.views.news import load_story_context

        with patch(
            "routes.views.news.query_many", side_effect=RuntimeError("secret sql")
        ):
            payload = load_story_context()
        self.assertEqual(payload["status"], "unavailable")
        self.assertNotIn("secret sql", str(payload))


class NewsStoryRouteTests(unittest.TestCase):
    def test_canonical_cluster_api_requires_auth_and_rejects_bad_filter(self):
        from fastapi.testclient import TestClient

        from main import app

        client = TestClient(app)
        with patch("routes.views.news.query_many", return_value=[story_row()]) as query:
            self.assertEqual(client.get("/api/news/clusters").status_code, 401)
            response = client.get("/api/news/clusters", headers=AUTH)
            invalid = client.get("/api/news/clusters?lane=not-a-lane", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["clusters"]), 1)
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(query.call_count, 1)
        self.assertNotIn("raw_payload", response.text)

    def test_news_page_groups_one_cluster_in_low_confidence_lane(self):
        from fastapi import FastAPI
        from fastapi.templating import Jinja2Templates
        from fastapi.testclient import TestClient

        from routes.views.news import router

        cluster = story_row(evidence_count=2)
        context = {
            "status": "published",
            "clusters": [cluster],
            "lanes": {
                "market_moving": [],
                "watchlist_related": [],
                "macro_central_banks": [],
                "filings_regulators": [],
                "developing": [],
                "low_confidence": [cluster],
            },
        }
        app = FastAPI()
        app.state.templates = Jinja2Templates(directory=API_ROOT / "templates")
        app.include_router(router)
        with (
            patch("routes.views.news.load_config", return_value={}),
            patch("routes.views.news.load_story_context", return_value=context),
            patch("routes.views.news.load_source_states", return_value=[]),
        ):
            response = TestClient(app).get("/news?lane=low_confidence")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text.count('data-story-id="'), 1)
        self.assertIn("Low confidence / single source", response.text)
        self.assertIn("Evidence timeline · 2 shown", response.text)
        self.assertIn("Market observations are descriptive", response.text)
        self.assertNotIn("raw_payload", response.text)

    def test_dashboard_partial_has_sse_identity_and_polling_fallback(self):
        from fastapi import FastAPI
        from fastapi.templating import Jinja2Templates
        from fastapi.testclient import TestClient

        from routes.views.dashboard import router

        cluster = story_row()
        context = {"status": "published", "clusters": [cluster], "lanes": {}}
        app = FastAPI()
        app.state.templates = Jinja2Templates(directory=API_ROOT / "templates")
        app.include_router(router)
        with (
            patch("routes.views.dashboard.load_story_context", return_value=context),
            patch(
                "routes.views.dashboard.app_config.load_config",
                return_value={"event_pipeline": {"sse": {"enabled": True}}},
            ),
        ):
            live = TestClient(app).get("/partials/dashboard/news")
        self.assertIn('data-live-section="news_clusters"', live.text)
        self.assertIn('data-live-event="section_changed"', live.text)
        with (
            patch("routes.views.dashboard.load_story_context", return_value=context),
            patch("routes.views.dashboard.app_config.load_config", return_value={}),
        ):
            polling = TestClient(app).get("/partials/dashboard/news")
        self.assertIn('hx-get="/partials/dashboard/news"', polling.text)
        self.assertIn('hx-trigger="marketRefresh from:body"', polling.text)
        self.assertNotIn("every 90s", polling.text)


def change_feed_row():
    return {
        "title": "Fed holds rates",
        "state": "confirmed",
        "state_class": "bullish",
        "time_display": "06 Aug 12:00 UTC",
        "observed_at": NOW.isoformat(),
        "source": "reuters",
        "importance_label": "High",
        "importance_class": "high",
        "novelty_display": "0.50",
        "interpretation_available": True,
        "markets": ["EURUSD"],
        "reaction_windows": [
            {
                "horizon": "5m",
                "display": "+0.40%",
                "direction": "up",
                "reaction_state": "confirmed_by_market",
            }
        ],
    }


def change_feed_payload(*, has_earlier=True, limit=30):
    return {
        "available": True,
        "rows": [change_feed_row()],
        "has_earlier": has_earlier,
        "limit": limit,
        "oldest_observed_at": NOW.isoformat(),
    }


class NewsChangeFeedTests(unittest.TestCase):
    """Continuous change feed on /news: canonical partial, loader reuse,
    load-earlier, compatibility alias, and refresh/SSE exclusivity."""

    def make_client(self, *routers):
        from fastapi import FastAPI
        from fastapi.templating import Jinja2Templates
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.state.templates = Jinja2Templates(directory=API_ROOT / "templates")
        for router in routers:
            app.include_router(router)
        return TestClient(app)

    def test_news_page_renders_sources_feed_placeholder_and_story_monitor(self):
        from routes.views.news import router

        cluster = story_row(evidence_count=2)
        context = {
            "status": "published",
            "clusters": [cluster],
            "lanes": {
                "market_moving": [],
                "watchlist_related": [],
                "macro_central_banks": [],
                "filings_regulators": [],
                "developing": [],
                "low_confidence": [cluster],
            },
        }
        client = self.make_client(router)
        with (
            patch("routes.views.news.load_config", return_value={}),
            patch("routes.views.news.load_story_context", return_value=context),
            patch("routes.views.news.load_source_states", return_value=[]),
        ):
            response = client.get("/news?lane=low_confidence")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Source state", response.text)
        self.assertIn('data-news-source="reuters"', response.text)
        self.assertIn("Canonical story monitor", response.text)
        self.assertIn('hx-get="/partials/news/change-feed"', response.text)
        self.assertIn('hx-trigger="load"', response.text)
        self.assertIn("Evidence timeline · 2 shown", response.text)

    def test_canonical_partial_reuses_cockpit_loader(self):
        from routes.views import cockpit_panels, news

        self.assertIs(news.load_change_feed, cockpit_panels.load_change_feed)
        client = self.make_client(news.router)
        with (
            patch("routes.views.news.load_config", return_value={}),
            patch(
                "routes.views.news.load_change_feed",
                return_value=change_feed_payload(),
            ) as loader,
        ):
            response = client.get("/partials/news/change-feed")
        self.assertEqual(response.status_code, 200)
        loader.assert_called_once()
        self.assertEqual(loader.call_args.kwargs["limit"], 30)
        self.assertIsNone(loader.call_args.kwargs["before"])
        self.assertIn("Fed holds rates", response.text)

    def test_canonical_partial_bounds_limit_and_validates_before_pre_db(self):
        from routes.views.news import router

        client = self.make_client(router)
        with patch("routes.views.news.load_config", return_value={}):
            self.assertEqual(
                client.get("/partials/news/change-feed?limit=999").status_code, 422
            )
            self.assertEqual(
                client.get("/partials/news/change-feed?limit=0").status_code, 422
            )
        with (
            patch("routes.views.news.load_config", return_value={}),
            patch("routes.views.cockpit_panels.query_many") as query,
        ):
            response = client.get("/partials/news/change-feed?before=garbage")
        self.assertEqual(response.status_code, 422)
        query.assert_not_called()

    def test_load_earlier_appends_rows_without_nested_section(self):
        from urllib.parse import quote

        from routes.views.news import router

        client = self.make_client(router)
        with (
            patch("routes.views.news.load_config", return_value={}),
            patch(
                "routes.views.news.load_change_feed",
                return_value=change_feed_payload(),
            ) as loader,
        ):
            response = client.get(
                "/partials/news/change-feed?before="
                + quote(NOW.isoformat(), safe="")
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<section", response.text)
        self.assertIn("Fed holds rates", response.text)
        self.assertIn('hx-get="/partials/news/change-feed?before=', response.text)
        self.assertIn("&amp;limit=30", response.text)
        self.assertIn('hx-swap="afterend"', response.text)
        self.assertIn("Load earlier", response.text)
        self.assertEqual(loader.call_args.kwargs["before"], NOW.isoformat())
        self.assertEqual(loader.call_args.kwargs["limit"], 30)

    def test_compat_dashboard_change_feed_url_still_serves_partial(self):
        from routes.views.cockpit_panels import router as cockpit_router
        from routes.views.news import router as news_router

        client = self.make_client(news_router, cockpit_router)
        with (
            patch("routes.views.news.load_config", return_value={}),
            patch(
                "routes.views.cockpit_panels.app_config.load_config",
                return_value={},
            ),
            patch(
                "routes.views.news.load_change_feed",
                return_value=change_feed_payload(),
            ),
            patch(
                "routes.views.cockpit_panels.load_change_feed",
                return_value=change_feed_payload(),
            ),
        ):
            canonical = client.get("/partials/news/change-feed")
            compat = client.get("/partials/dashboard/change-feed")
        self.assertEqual(canonical.status_code, 200)
        self.assertEqual(compat.status_code, 200)
        self.assertIn("Fed holds rates", compat.text)
        self.assertIn("Change feed", compat.text)
        self.assertIn("Open story monitor", compat.text)
        self.assertIn('href="/news"', compat.text)
        self.assertIn("Canonical story monitor", canonical.text)
        self.assertIn('href="#story-monitor-title"', canonical.text)

    def test_refresh_is_exclusive_sse_or_market_refresh_no_polling(self):
        from routes.views.news import router

        client = self.make_client(router)
        live_config = {"event_pipeline": {"sse": {"enabled": True}}}
        with (
            patch("routes.views.news.load_config", return_value=live_config),
            patch(
                "routes.views.news.load_change_feed",
                return_value=change_feed_payload(),
            ),
        ):
            live = client.get("/partials/news/change-feed")
        self.assertIn('data-live-section="change_feed"', live.text)
        self.assertIn('data-live-event="section_changed"', live.text)
        self.assertIn('data-live-url="/partials/news/change-feed"', live.text)
        self.assertNotIn("hx-trigger", live.text)
        self.assertNotIn("marketRefresh", live.text)
        self.assertNotIn("every 90s", live.text)
        with (
            patch("routes.views.news.load_config", return_value={}),
            patch(
                "routes.views.news.load_change_feed",
                return_value=change_feed_payload(),
            ),
        ):
            polling = client.get("/partials/news/change-feed")
        self.assertIn('hx-get="/partials/news/change-feed"', polling.text)
        self.assertIn('hx-trigger="marketRefresh from:body"', polling.text)
        self.assertIn('hx-swap="outerHTML"', polling.text)
        self.assertNotIn("data-live-section", polling.text)
        self.assertNotIn("every 90s", polling.text)


if __name__ == "__main__":
    unittest.main()
