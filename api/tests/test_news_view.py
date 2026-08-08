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
        self.assertIn('hx-trigger="every 90s"', polling.text)


if __name__ == "__main__":
    unittest.main()
