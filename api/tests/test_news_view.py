import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import mock_open, patch

os.environ.setdefault("DB_USER", "test")
os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("FRED_API_KEY", "test")
os.environ.setdefault("OPENROUTER_API_KEY", "test")
os.environ.setdefault("OPENROUTER_MODEL", "test/model")
os.environ.setdefault("OANDA_API_KEY", "test")
os.environ.setdefault("DASHBOARD_USER", "test")
os.environ.setdefault("DASHBOARD_PASSWORD", "test")

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))


class NewsViewTests(unittest.TestCase):
    def test_bounded_reader_caps_the_actual_read(self):
        from routes.views.news import _read_json_bounded

        reader = mock_open(read_data=b"{}")
        with patch("routes.views.news.open", reader):
            self.assertEqual(_read_json_bounded(Path("feed.json"), 16), {})
        reader().read.assert_called_once_with(17)

    def test_loader_rejects_malformed_utf8_and_oversized_feed(self):
        from routes.views.news import MAX_NEWS_FEED_BYTES, load_news_context

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.json"
            path.write_bytes(b"\xff\xfe")
            self.assertEqual(load_news_context({"news_feed": {"output_path": directory}})["status"], "invalid")
            path.write_bytes(b" " * (MAX_NEWS_FEED_BYTES + 1))
            self.assertEqual(load_news_context({"news_feed": {"output_path": directory}})["status"], "invalid")

    def test_loader_bounds_rendered_fields_and_items(self):
        from routes.views.news import load_news_context

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.json"
            path.write_text(json.dumps({
                "generated_at": "g" * 500,
                "items": [{
                    "title": "t" * 500,
                    "source_label": "s" * 500,
                    "published": "p" * 500,
                    "summary": "x" * 500,
                    "symbols": ["AUDJPY"] * 20,
                    "tags": ["macro"] * 20,
                    "url": "javascript:alert(1)",
                } for _ in range(10)],
            }))
            context = load_news_context({"news_feed": {"output_path": directory}}, limit=5)
        self.assertEqual(len(context["items"]), 5)
        self.assertLessEqual(len(context["items"][0]["title"]), 240)
        self.assertLessEqual(len(context["items"][0]["source"]), 64)
        self.assertIsNone(context["items"][0]["url"])
        self.assertLessEqual(len(context["generated_at"]), 64)

    @patch("config.load_config")
    def test_json_feed_uses_bounded_sanitized_contract(self, config):
        from fastapi.testclient import TestClient
        from main import app

        with tempfile.TemporaryDirectory() as directory:
            config.return_value = {"news_feed": {"output_path": directory}}
            items = [
                {"title": "x" * 400, "source": "reuters", "summary": "y" * 800}
                for _ in range(520)
            ]
            (Path(directory) / "feed.json").write_text(
                json.dumps({"generated_at": "now", "items": items})
            )
            response = TestClient(app).get(
                "/api/news/feed",
                headers={"Authorization": "Basic dGVzdDp0ZXN0"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["items"]), 500)
        self.assertEqual(len(payload["items"][0]["title"]), 240)
        self.assertEqual(len(payload["items"][0]["summary"]), 500)

    def test_full_page_filters_source_and_symbol(self):
        from fastapi import FastAPI
        from fastapi.templating import Jinja2Templates
        from fastapi.testclient import TestClient
        from routes.views.news import router

        app = FastAPI()
        app.state.templates = Jinja2Templates(directory=API_ROOT / "templates")
        app.include_router(router)
        payload = {
            "status": "published", "generated_at": "now",
            "items": [
                {"title": "A", "source": "Reuters", "source_id": "reuters", "published": "now", "summary": "", "symbols": ["AUDJPY"], "tags": ["macro"], "url": None},
                {"title": "B", "source": "Kobeissi", "source_id": "kobeissi", "published": "now", "summary": "", "symbols": ["XAUUSD"], "tags": ["metals"], "url": None},
            ],
        }
        with patch("routes.views.news.load_config", return_value={}), patch("routes.views.news.load_news_context", return_value=payload), patch("routes.views.news.load_source_states", return_value=[]):
            response = TestClient(app).get("/news?source=reuters&symbol=AUDJPY")
        self.assertEqual(response.status_code, 200)
        self.assertIn("A", response.text)
        self.assertNotIn(">B<", response.text)


if __name__ == "__main__":
    unittest.main()
