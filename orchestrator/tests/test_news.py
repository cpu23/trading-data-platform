import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner


class NewsTests(unittest.TestCase):
    def test_atomic_json_recovers_malformed_and_replaces_file(self):
        from sources.news_storage import atomic_write_json, read_json
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{")
            self.assertEqual(read_json(path, {"ok": True}), {"ok": True})
            atomic_write_json(path, {"value": 2})
            self.assertEqual(json.loads(path.read_text()), {"value": 2})
            self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_reuters_daily_snapshot_is_idempotent_and_state_ordered(self):
        from sources.reuters import run_reuters
        item = {"id": "reuters:one", "url": "https://www.reuters.com/markets/one"}
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"reuters": {"enabled": True, "state_path": f"{tmp}/reuters/state.json", "output_path": f"{tmp}/reuters"}}
            with patch("sources.reuters._fetch_sitemap_index", return_value=["page"]), patch("sources.reuters._parse_sitemap_page", return_value=[item]):
                run_reuters(cfg); run_reuters(cfg)
            daily = next(Path(tmp, "reuters").glob("reuters_*.json"))
            self.assertEqual(json.loads(daily.read_text()), [item])
            state = json.loads(Path(tmp, "reuters/state.json").read_text())
            self.assertEqual(state["last_seen_urls"], [item["url"]])
            self.assertEqual(state["status"], "ok")

    def test_reuters_malformed_page_records_error_and_preserves_valid_page_items(self):
        from sources.reuters import run_reuters

        valid_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
                xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
          <url>
            <loc>https://www.reuters.com/markets/global-markets-test-2026-07-13/</loc>
            <news:news>
              <news:publication_date>2026-07-13T12:00:00Z</news:publication_date>
              <news:title>Global markets test</news:title>
              <news:keywords>stocks, dollar</news:keywords>
            </news:news>
          </url>
        </urlset>"""

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return self.payload

        def urlopen(request, timeout=30):
            payload = (
                b"<urlset><url>"
                if request.full_url == "https://example.test/malformed.xml"
                else valid_xml
            )
            return Response(payload)

        with tempfile.TemporaryDirectory() as tmp:
            cfg = {
                "reuters": {
                    "enabled": True,
                    "state_path": f"{tmp}/reuters/state.json",
                    "output_path": f"{tmp}/reuters",
                }
            }
            with patch(
                "sources.reuters._fetch_sitemap_index",
                return_value=[
                    "https://example.test/malformed.xml",
                    "https://example.test/valid.xml",
                ],
            ), patch("urllib.request.urlopen", side_effect=urlopen):
                items = run_reuters(cfg, max_pages=2)

            state = json.loads(Path(tmp, "reuters/state.json").read_text())

        self.assertEqual([item["id"] for item in items], ["reuters:global-markets-test-2026-07-13"])
        self.assertEqual(items[0]["title"], "Global markets test")
        self.assertEqual(state["status"], "error")
        self.assertIn("malformed", state["error"])

    def test_reuters_mixed_fetch_failure_preserves_items_and_records_error(self):
        from sources.reuters import run_reuters

        valid_xml = b"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
          <url>
            <loc>https://www.reuters.com/markets/valid-item/</loc>
            <news:news>
              <news:publication_date>2026-07-13T12:00:00Z</news:publication_date>
              <news:title>Valid markets item</news:title>
            </news:news>
          </url>
        </urlset>"""

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return valid_xml

        def urlopen(request, timeout=30):
            if request.full_url == "https://example.test/failed.xml":
                raise TimeoutError("private upstream detail")
            return Response()

        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"reuters": {
                "state_path": f"{tmp}/reuters/state.json",
                "output_path": f"{tmp}/reuters",
            }}
            with patch(
                "sources.reuters._fetch_sitemap_index",
                return_value=[
                    "https://example.test/valid.xml",
                    "https://example.test/failed.xml",
                ],
            ), patch("urllib.request.urlopen", side_effect=urlopen):
                items = run_reuters(cfg, max_pages=2)
            state = json.loads(Path(tmp, "reuters/state.json").read_text())

        self.assertEqual([item["id"] for item in items], ["reuters:valid-item"])
        self.assertEqual(state["status"], "error")
        self.assertIn("failed.xml", state["error"])
        self.assertIn("TimeoutError", state["error"])
        self.assertNotIn("private upstream detail", state["error"])

    def test_reuters_all_page_fetches_fail_without_escaping_and_record_error(self):
        from sources.reuters import run_reuters

        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"reuters": {
                "state_path": f"{tmp}/reuters/state.json",
                "output_path": f"{tmp}/reuters",
            }}
            with patch(
                "sources.reuters._fetch_sitemap_index",
                return_value=["https://example.test/failed.xml?token=private"],
            ), patch(
                "urllib.request.urlopen",
                side_effect=ConnectionError("credential=private"),
            ):
                items = run_reuters(cfg, max_pages=1)
            state = json.loads(Path(tmp, "reuters/state.json").read_text())

        self.assertEqual(items, [])
        self.assertEqual(state["status"], "error")
        self.assertIn("failed.xml", state["error"])
        self.assertIn("ConnectionError", state["error"])
        self.assertNotIn("private", state["error"])

    def test_reuters_successful_empty_page_records_ok(self):
        from sources.reuters import run_reuters

        empty_xml = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" />'

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return empty_xml

        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"reuters": {
                "state_path": f"{tmp}/reuters/state.json",
                "output_path": f"{tmp}/reuters",
            }}
            with patch(
                "sources.reuters._fetch_sitemap_index",
                return_value=["https://example.test/empty.xml"],
            ), patch("urllib.request.urlopen", return_value=Response()):
                items = run_reuters(cfg, max_pages=1)
            state = json.loads(Path(tmp, "reuters/state.json").read_text())

        self.assertEqual(items, [])
        self.assertEqual(state["status"], "ok")
        self.assertIsNone(state["error"])

    def test_kobeissi_rejects_empty_api_key_clearly(self):
        from sources.kobeissi import run_kobeissi

        with self.assertRaisesRegex(ValueError, "TWITTERAPI_KEY"):
            run_kobeissi({"kobeissi": {"enabled": True, "api_key": ""}})

    def test_kobeissi_compares_tweet_ids_numerically_and_deduplicates_snapshot(self):
        from sources.kobeissi import run_kobeissi
        payload = {"status": "success", "data": {"tweets": [
            {"id": "10", "text": "new", "createdAt": ""},
            {"id": "9", "text": "old", "createdAt": ""},
        ]}}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self): return json.dumps(payload).encode()
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp, "kobeissi/state.json"); state.parent.mkdir(parents=True); state.write_text(json.dumps({"last_seen_id": "9"}))
            cfg = {"kobeissi": {"enabled": True, "api_key": "key", "state_path": str(state), "output_path": f"{tmp}/kobeissi"}}
            with patch("urllib.request.urlopen", return_value=Response()):
                first = run_kobeissi(cfg); second = run_kobeissi(cfg)
            self.assertEqual([x["id"] for x in first], ["kobeissi:10"])
            self.assertEqual(second, [])
            daily = next(Path(tmp, "kobeissi").glob("kobeissi_*.json"))
            self.assertEqual(len(json.loads(daily.read_text())), 1)

    def test_feed_validates_deduplicates_filters_utc_and_enabled_sources(self):
        from sources.news_feed import build_feed
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime.now(timezone.utc)
            for source in ("reuters", "kobeissi", "disabled"):
                (root/source).mkdir()
            current = {"id":"same", "source":"reuters", "source_label":"Reuters", "title":"A", "summary":"", "url":"https://x", "published":now.isoformat(), "symbols":[], "tags":[], "engagement":{}, "media":[], "meta":{}, "fetched_at":now.isoformat()}
            (root/"reuters/a.json").write_text(json.dumps([current, current, {**current, "id":"old", "published":(now-timedelta(days=8)).isoformat()}]))
            (root/"kobeissi/b.json").write_text("{")
            (root/"disabled/c.json").write_text(json.dumps([{**current, "id":"disabled", "source":"disabled"}]))
            cfg = {"news_feed":{"output_path":tmp}, "reuters":{"enabled":True}, "kobeissi":{"enabled":True}, "disabled":{"enabled":False}}
            feed = build_feed(cfg)
            self.assertEqual(feed["count"], 1)
            self.assertEqual(feed["sources"], ["reuters"])

    def test_news_all_skips_disabled_sources(self):
        from cli import cli
        cfg = {"reuters":{"enabled":False}, "kobeissi":{"enabled":False}, "news_feed":{"output_path":"unused"}}
        with patch("cli.load_config", return_value=cfg), patch("sources.news_feed.build_feed", return_value={"count":0,"sources":[],"items":[]}):
            result = CliRunner().invoke(cli, ["news", "all"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Reuters: disabled", result.output)
        self.assertIn("Kobeissi: disabled", result.output)


if __name__ == "__main__":
    unittest.main()
