import os
import sys
import unittest
from datetime import UTC, datetime, timedelta
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
    def test_canonical_cluster_api_rejects_bad_filter(self):
        from fastapi.testclient import TestClient
        from main import app

        client = TestClient(app)
        with patch("routes.views.news.query_many", return_value=[story_row()]) as query:
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

    def test_dashboard_partial_uses_shared_polling_heartbeat(self):
        from fastapi import FastAPI
        from fastapi.templating import Jinja2Templates
        from fastapi.testclient import TestClient
        from routes.views.dashboard import router

        context = {"status": "published", "clusters": [story_row()], "lanes": {}}
        app = FastAPI()
        app.state.templates = Jinja2Templates(directory=API_ROOT / "templates")
        app.include_router(router)
        with patch(
            "routes.views.dashboard.load_story_context",
            return_value=context,
        ):
            response = TestClient(app).get("/partials/dashboard/news")
        self.assertIn('hx-get="/partials/dashboard/news"', response.text)
        self.assertIn('hx-trigger="marketRefresh from:body"', response.text)
        self.assertNotIn("data-live-section", response.text)


def change_feed_row():
    return {
        "event_id": "22222222-2222-4222-8222-222222222222",
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
        "oldest_event_id": "22222222-2222-4222-8222-222222222222",
    }


CONFIG = {
    "timezone": {"primary": {"name": "Europe/London", "label": "London"}},
    "event_pipeline": {"sse": {"enabled": True}},
}


def feed_row(
    observed_at,
    *,
    title="Fed holds rates",
    event_type="rate_decision",
    flags=(),
    windows=None,
    markets=None,
    importance=0.8,
    novelty=0.5,
    interpretation=True,
):
    """Raw routed-event DB row as consumed by ``load_change_feed``."""
    if windows is None:
        windows = [
            {
                "timeframe": "PRICE",
                "horizon": "5m",
                "percentage_move": 0.4,
                "reaction_state": "persistence",
            },
            {
                "timeframe": "PRICE",
                "horizon": "30m",
                "percentage_move": -0.2,
                "reaction_state": "reversal",
            },
        ]
    if markets is None:
        markets = [
            {"symbol": "EURUSD"},
            {"symbol": "SP500"},
            {"symbol": "XAUUSD"},
            {"symbol": "DXY"},
            {"symbol": "USDJPY"},
            {"symbol": "GER40"},
        ]
    return {
        "event_id": "22222222-2222-4222-8222-222222222222",
        "observed_at": observed_at,
        "effective_at": observed_at,
        "published_at": observed_at,
        "event_type": event_type,
        "source": "reuters",
        "payload": {"title": title},
        "markets": markets,
        "importance": importance,
        "novelty": novelty,
        "reaction_windows": windows,
        "confirmation_flags": list(flags),
        "interpretation_available": interpretation,
    }


class NewsChangeFeedQueryTests(unittest.TestCase):
    """Loader-level contract for the news-owned change feed loader."""

    def test_rejects_bad_before_before_database_access(self):
        from fastapi import HTTPException
        from routes.views.news import load_change_feed

        with patch("routes.views.news.query_many") as query:
            with self.assertRaises(HTTPException) as caught:
                load_change_feed(CONFIG, before="not-a-timestamp")
        self.assertEqual(caught.exception.status_code, 422)
        query.assert_not_called()

        with patch("routes.views.news.query_many") as query:
            with self.assertRaises(HTTPException) as caught:
                load_change_feed(CONFIG, before="2026-08-06T12:00:00")
        self.assertEqual(caught.exception.status_code, 422)
        query.assert_not_called()

    def test_validates_and_binds_full_ordering_cursor(self):
        from fastapi import HTTPException
        from routes.views.news import load_change_feed

        cursor_id = UUID("22222222-2222-4222-8222-222222222222")
        with patch("routes.views.news.query_many") as query:
            with self.assertRaises(HTTPException) as caught:
                load_change_feed(CONFIG, before_id=str(cursor_id))
        self.assertEqual(caught.exception.status_code, 422)
        query.assert_not_called()

        with patch("routes.views.news.query_many") as query:
            with self.assertRaises(HTTPException) as caught:
                load_change_feed(CONFIG, before=NOW.isoformat(), before_id="not-a-uuid")
        self.assertEqual(caught.exception.status_code, 422)
        query.assert_not_called()

        with patch("routes.views.news.query_many", return_value=[]) as query:
            load_change_feed(
                CONFIG,
                before=NOW.isoformat(),
                before_id=str(cursor_id),
            )
        self.assertEqual(query.call_args.kwargs["params"]["before"], NOW)
        self.assertEqual(query.call_args.kwargs["params"]["before_id"], cursor_id)
        self.assertIn(
            "routed.event_id < :before_id",
            query.call_args.args[0],
        )

    def test_bounds_limit_and_signals_has_earlier(self):
        from routes.views.news import load_change_feed

        rows = [feed_row(NOW - timedelta(minutes=index)) for index in range(31)]
        with patch("routes.views.news.query_many", return_value=rows) as query:
            feed = load_change_feed(CONFIG, limit=30)
        self.assertTrue(feed["available"])
        self.assertTrue(feed["has_earlier"])
        self.assertEqual(len(feed["rows"]), 30)
        self.assertEqual(query.call_args.kwargs["params"]["limit"], 31)

        with patch("routes.views.news.query_many", return_value=rows[:30]):
            feed = load_change_feed(CONFIG, limit=30)
        self.assertFalse(feed["has_earlier"])
        self.assertEqual(len(feed["rows"]), 30)

        many_rows = [feed_row(NOW - timedelta(minutes=index)) for index in range(60)]
        with patch("routes.views.news.query_many", return_value=many_rows) as query:
            feed = load_change_feed(CONFIG, limit=999)
        self.assertEqual(feed["limit"], 50)
        self.assertTrue(feed["has_earlier"])
        self.assertEqual(len(feed["rows"]), 50)
        self.assertEqual(query.call_args.kwargs["params"]["limit"], 51)

    def test_row_processing_caps_markets_and_windows(self):
        from routes.views.news import load_change_feed

        windows = [
            {
                "timeframe": "PRICE" if index % 2 == 0 else "5m",
                "horizon": f"{index}m",
                "percentage_move": 0.1 * index,
                "reaction_state": "persistence",
            }
            for index in range(6)
        ]
        row = feed_row(
            NOW,
            flags=("confirmed_by_market", "market_moved_before_headline"),
            windows=windows,
        )
        with patch("routes.views.news.query_many", return_value=[row]):
            feed = load_change_feed(CONFIG, limit=30)
        processed = feed["rows"][0]
        self.assertEqual(processed["title"], "Fed holds rates")
        self.assertEqual(processed["source"], "reuters")
        self.assertEqual(processed["markets"], ["EURUSD", "SP500", "XAUUSD", "DXY"])
        self.assertEqual(len(processed["reaction_windows"]), 4)
        self.assertEqual(processed["reaction_windows"][0]["horizon"], "0m")
        self.assertEqual(processed["reaction_windows"][0]["timeframe"], "PRICE")
        self.assertEqual(processed["state"], "confirmed")
        self.assertEqual(processed["state_class"], "bullish")
        self.assertTrue(processed["interpretation_available"])
        self.assertEqual(processed["importance_label"], "High")
        self.assertEqual(processed["novelty_display"], "0.50")
        self.assertEqual(processed["observed_at"], NOW.isoformat())

    def test_title_falls_back_to_event_type(self):
        from routes.views.news import load_change_feed

        raw = feed_row(NOW)
        raw["payload"] = {}
        raw["event_type"] = "central_bank_rate"
        with patch("routes.views.news.query_many", return_value=[raw]):
            feed = load_change_feed(CONFIG)
        self.assertEqual(feed["rows"][0]["title"], "central_bank_rate")

    def test_state_mapping_from_confirmation_flags(self):
        from routes.views.news import load_change_feed

        cases = {
            "developing": (),
            "confirmed": ("confirmed_by_market",),
            "contradicted": ("initial_move_reversed",),
            "completed": ("no_material_reaction",),
        }
        for expected, flags in cases.items():
            with patch(
                "routes.views.news.query_many",
                return_value=[feed_row(NOW, flags=flags)],
            ):
                feed = load_change_feed(CONFIG)
            self.assertEqual(feed["rows"][0]["state"], expected)

    def test_query_failure_is_fail_soft(self):
        from routes.views.news import load_change_feed

        with patch(
            "routes.views.news.query_many",
            side_effect=RuntimeError("secret sql"),
        ):
            feed = load_change_feed(CONFIG)
        self.assertFalse(feed["available"])
        self.assertEqual(feed["rows"], [])
        self.assertNotIn("secret sql", str(feed))


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

    def test_canonical_partial_uses_news_owned_loader(self):
        import routes.views.cockpit_panels as cockpit_panels
        from routes.views import news

        # The feed loader moved into News; cockpit_panels no longer owns it.
        self.assertTrue(hasattr(news, "load_change_feed"))
        self.assertFalse(hasattr(cockpit_panels, "load_change_feed"))
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
        self.assertIsNone(loader.call_args.kwargs["before_id"])
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
            patch("routes.views.news.query_many") as query,
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
                + "&before_id=22222222-2222-4222-8222-222222222222"
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<section", response.text)
        self.assertIn("Fed holds rates", response.text)
        self.assertIn('hx-get="/partials/news/change-feed?before=', response.text)
        self.assertIn(
            "&amp;before_id=22222222-2222-4222-8222-222222222222", response.text
        )
        self.assertIn("&amp;limit=30", response.text)
        self.assertIn('hx-swap="outerHTML"', response.text)
        self.assertIn("Load earlier", response.text)
        self.assertEqual(loader.call_args.kwargs["before"], NOW.isoformat())
        self.assertEqual(
            loader.call_args.kwargs["before_id"],
            "22222222-2222-4222-8222-222222222222",
        )
        self.assertEqual(loader.call_args.kwargs["limit"], 30)

    def test_change_feed_renders_reaction_windows_and_load_earlier(self):
        from urllib.parse import quote

        from routes.views.news import router

        client = self.make_client(router)
        feed = change_feed_payload(has_earlier=True)
        with (
            patch("routes.views.news.load_config", return_value={}),
            patch("routes.views.news.load_change_feed", return_value=feed),
        ):
            response = client.get("/partials/news/change-feed")
        self.assertIn("Fed holds rates", response.text)
        self.assertIn("5m", response.text)
        self.assertIn("+0.40%", response.text)
        self.assertIn("confirmed", response.text)
        self.assertIn("interpretation", response.text)
        self.assertIn("Load earlier", response.text)
        self.assertIn('hx-swap="outerHTML"', response.text)
        self.assertIn("before=" + quote(NOW.isoformat(), safe=""), response.text)
        self.assertIn(
            "before_id=22222222-2222-4222-8222-222222222222",
            response.text,
        )

        feed["has_earlier"] = False
        with (
            patch("routes.views.news.load_config", return_value={}),
            patch("routes.views.news.load_change_feed", return_value=feed),
        ):
            response = client.get("/partials/news/change-feed")
        self.assertNotIn("Load earlier", response.text)

    def test_change_feed_uses_shared_polling_heartbeat(self):
        from routes.views.news import router

        client = self.make_client(router)
        with (
            patch("routes.views.news.load_config", return_value={}),
            patch(
                "routes.views.news.load_change_feed",
                return_value=change_feed_payload(),
            ),
        ):
            response = client.get("/partials/news/change-feed")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'hx-get="/partials/news/change-feed"',
            response.text,
        )
        self.assertIn(
            'hx-trigger="marketRefresh from:body"',
            response.text,
        )
        self.assertNotIn("data-live-section", response.text)


if __name__ == "__main__":
    unittest.main()
