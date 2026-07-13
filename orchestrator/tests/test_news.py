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
