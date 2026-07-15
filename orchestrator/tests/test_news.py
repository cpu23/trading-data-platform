import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner


class NewsTests(unittest.TestCase):
    def test_concurrent_publications_for_same_source_do_not_lose_items(self):
        from sources.news_feed import collect_and_publish
        from sources.news_result import NewsCollectionResult
        from sources.news_storage import merge_items

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "reuters" / "reuters_today.json"
            cfg = {
                "news_feed": {"output_path": tmp, "history_days": 7},
                "reuters": {"enabled": True},
                "kobeissi": {"enabled": False},
            }
            barrier = threading.Barrier(3)

            def publish(item_id):
                item = {
                    "id": item_id, "source": "reuters", "source_label": "Reuters",
                    "title": item_id, "summary": "", "url": f"https://x/{item_id}",
                    "published": datetime.now(timezone.utc).isoformat(), "symbols": [],
                    "tags": [], "engagement": {}, "media": [], "meta": {},
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }

                def collector():
                    merge_items(source_path, [item])
                    return NewsCollectionResult([item], "ok")

                barrier.wait()
                collect_and_publish("reuters", cfg, collector)

            threads = [threading.Thread(target=publish, args=(item_id,)) for item_id in ("one", "two")]
            for thread in threads: thread.start()
            barrier.wait()
            for thread in threads: thread.join(timeout=5)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual({item["id"] for item in json.loads(source_path.read_text())}, {"one", "two"})
            self.assertEqual({item["id"] for item in json.loads((root / "feed.json").read_text())["items"]}, {"one", "two"})

    def test_feed_build_failure_preserves_prior_valid_publication(self):
        from sources.news_feed import collect_and_publish
        from sources.news_result import NewsCollectionResult

        with tempfile.TemporaryDirectory() as tmp:
            feed_path = Path(tmp, "feed.json")
            prior = {"generated_at": "prior", "days": 7, "count": 0, "sources": [], "items": []}
            feed_path.write_text(json.dumps(prior))
            cfg = {"news_feed": {"output_path": tmp}, "reuters": {"enabled": True}}

            with patch("sources.news_feed._build_feed_unlocked", side_effect=ValueError("raw secret")):
                result = collect_and_publish(
                    "reuters", cfg, lambda: NewsCollectionResult([], "ok")
                )

            self.assertEqual(result.status, "error")
            self.assertEqual(result.error, "News feed publication failed: ValueError")
            self.assertEqual(json.loads(feed_path.read_text()), prior)
            self.assertFalse(list(Path(tmp).rglob("*.tmp")))

    def test_atomic_write_uses_fsync_replace_and_restrictive_mode(self):
        from sources.news_storage import atomic_write_json

        with tempfile.TemporaryDirectory() as tmp, patch("sources.news_storage.os.fsync", wraps=__import__("os").fsync) as fsync, patch("sources.news_storage.os.replace", wraps=__import__("os").replace) as replace:
            path = Path(tmp, "feed.json")
            atomic_write_json(path, {"ok": True})

            self.assertGreaterEqual(fsync.call_count, 2)
            replace.assert_called_once()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

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

    def test_reuters_recovers_and_repairs_invalid_last_seen_url_cursors(self):
        from sources.reuters import run_reuters

        cursor_cases = (
            (None, set()),
            (["https://www.reuters.com/markets/seen/", ["nested"], 7, {"raw": "value"}], {
                "https://www.reuters.com/markets/seen/",
            }),
        )

        for stored_cursor, expected_seen in cursor_cases:
            with self.subTest(stored_cursor=stored_cursor), tempfile.TemporaryDirectory() as tmp:
                state_path = Path(tmp, "reuters/state.json")
                state_path.parent.mkdir(parents=True)
                state_path.write_text(json.dumps({"last_seen_urls": stored_cursor}))
                cfg = {"reuters": {
                    "state_path": str(state_path),
                    "output_path": f"{tmp}/reuters",
                }}
                captured_seen = []

                def parse_page(_url, seen_urls, _config):
                    captured_seen.append(set(seen_urls))
                    return []

                with patch("sources.reuters._fetch_sitemap_index", return_value=["https://example.test/page.xml"]), patch("sources.reuters._parse_sitemap_page", side_effect=parse_page):
                    result = run_reuters(cfg, max_pages=1)
                state = json.loads(state_path.read_text())

                self.assertEqual(result.status, "ok")
                self.assertEqual(captured_seen, [expected_seen])
                self.assertEqual(state["last_seen_urls"], sorted(expected_seen))
                self.assertTrue(all(isinstance(url, str) for url in state["last_seen_urls"]))

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
                result = run_reuters(cfg, max_pages=2)

            state = json.loads(Path(tmp, "reuters/state.json").read_text())

        self.assertEqual([item["id"] for item in result.items], ["reuters:global-markets-test-2026-07-13"])
        self.assertEqual(result.items[0]["title"], "Global markets test")
        self.assertEqual(result.status, "error")
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
                result = run_reuters(cfg, max_pages=2)
            state = json.loads(Path(tmp, "reuters/state.json").read_text())

        self.assertEqual([item["id"] for item in result.items], ["reuters:valid-item"])
        self.assertEqual(result.status, "error")
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
                result = run_reuters(cfg, max_pages=1)
            state = json.loads(Path(tmp, "reuters/state.json").read_text())

        self.assertEqual(result.items, [])
        self.assertEqual(result.status, "error")
        self.assertEqual(state["status"], "error")
        self.assertIn("failed.xml", state["error"])
        self.assertIn("ConnectionError", state["error"])
        self.assertNotIn("private", state["error"])

    def test_reuters_index_rejects_well_formed_non_sitemap_xml(self):
        from sources.reuters import run_reuters

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self): return b"<html><body>RAW_CONTENT_SENTINEL</body></html>"

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp, "reuters/state.json")
            cfg = {"reuters": {
                "state_path": str(state_path),
                "output_path": f"{tmp}/reuters",
            }}
            with patch("urllib.request.urlopen", return_value=Response()), patch("sources.reuters.logger") as mocked_logger:
                result = run_reuters(cfg)
            state_text = state_path.read_text()

        self.assertEqual(result.items, [])
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error, "Reuters sitemap index failed: SitemapSchemaError")
        exposed = f"{result.error} {state_text} {mocked_logger.method_calls}"
        self.assertNotIn("RAW_CONTENT_SENTINEL", exposed)

    def test_reuters_pages_reject_well_formed_wrong_roots_and_sanitize_url(self):
        from sources.reuters import run_reuters

        wrong_roots = (
            b"<html><body>RAW_CONTENT_SENTINEL</body></html>",
            b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" />',
            b"<challenge>RAW_CONTENT_SENTINEL</challenge>",
        )
        page_url = "https://example.test/not-a-urlset.xml?token=QUERY_SENTINEL#FRAGMENT_SENTINEL"

        for payload in wrong_roots:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                class Response:
                    def __enter__(self): return self
                    def __exit__(self, *args): return None
                    def read(self): return payload

                state_path = Path(tmp, "reuters/state.json")
                cfg = {"reuters": {
                    "state_path": str(state_path),
                    "output_path": f"{tmp}/reuters",
                }}
                with patch("sources.reuters._fetch_sitemap_index", return_value=[page_url]), patch("urllib.request.urlopen", return_value=Response()), patch("sources.reuters.logger") as mocked_logger:
                    result = run_reuters(cfg)
                state_text = state_path.read_text()

                self.assertEqual(result.items, [])
                self.assertEqual(result.status, "error")
                self.assertIn("https://example.test/not-a-urlset.xml", result.error)
                self.assertIn("SitemapSchemaError", result.error)
                exposed = f"{result.error} {state_text} {mocked_logger.method_calls}"
                self.assertNotIn("QUERY_SENTINEL", exposed)
                self.assertNotIn("FRAGMENT_SENTINEL", exposed)
                self.assertNotIn("RAW_CONTENT_SENTINEL", exposed)

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
                result = run_reuters(cfg, max_pages=1)
            state = json.loads(Path(tmp, "reuters/state.json").read_text())

        self.assertEqual(result.items, [])
        self.assertEqual(result.status, "ok")
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
            self.assertEqual([x["id"] for x in first.items], ["kobeissi:10"])
            self.assertEqual(first.status, "ok")
            self.assertEqual(second.items, [])
            self.assertEqual(second.status, "ok")
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


    def test_kobeissi_failure_replaces_stale_ok_state_with_typed_error(self):
        from sources.kobeissi import run_kobeissi

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp, "kobeissi/state.json")
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({
                "last_seen_id": "42", "last_poll": "2026-01-01T00:00:00+00:00",
                "status": "ok", "error": None,
            }))
            cfg = {"kobeissi": {"api_key": "key", "state_path": str(state_path), "output_path": f"{tmp}/kobeissi"}}
            with patch("urllib.request.urlopen", side_effect=ConnectionError("token=private")), patch("sources.kobeissi.logger") as mocked_logger:
                result = run_kobeissi(cfg)
            state = json.loads(state_path.read_text())

        self.assertEqual(result.items, [])
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error, "Kobeissi fetch failed: ConnectionError")
        self.assertEqual(state["status"], "error")
        self.assertNotEqual(state["last_poll"], "2026-01-01T00:00:00+00:00")
        self.assertNotIn("private", json.dumps(state))
        self.assertNotIn("private", str(mocked_logger.method_calls))

    def test_kobeissi_malformed_tweet_entry_replaces_stale_ok_with_sanitized_typed_error(self):
        from sources.kobeissi import run_kobeissi
        from sources.news_result import NewsCollectionResult

        payload = {
            "status": "success",
            "data": {"tweets": [None, "RAW_PAYLOAD_SENTINEL"]},
        }

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self): return json.dumps(payload).encode()

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp, "kobeissi/state.json")
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({
                "last_seen_id": "42",
                "last_poll": "2026-01-01T00:00:00+00:00",
                "status": "ok",
                "error": None,
            }))
            cfg = {"kobeissi": {
                "api_key": "TOKEN_SENTINEL",
                "state_path": str(state_path),
                "output_path": f"{tmp}/kobeissi",
            }}
            with patch("urllib.request.urlopen", return_value=Response()), patch("sources.kobeissi.logger") as mocked_logger:
                result = run_kobeissi(cfg)
            state = json.loads(state_path.read_text())

        self.assertIsInstance(result, NewsCollectionResult)
        self.assertEqual(result.items, [])
        self.assertEqual(result.status, "error")
        self.assertEqual(state["status"], "error")
        self.assertEqual(state["error"], result.error)
        self.assertIn("invalid response", state["error"])
        self.assertNotEqual(state["last_poll"], "2026-01-01T00:00:00+00:00")
        exposed = json.dumps({"result_error": result.error, "state": state, "logs": str(mocked_logger.method_calls)})
        self.assertNotIn("RAW_PAYLOAD_SENTINEL", exposed)
        self.assertNotIn("TOKEN_SENTINEL", exposed)

    def test_kobeissi_malformed_required_tweet_field_is_sanitized_typed_error(self):
        from sources.kobeissi import run_kobeissi
        from sources.news_result import NewsCollectionResult

        payload = {
            "status": "success",
            "data": {"tweets": [{"id": "43", "text": ["RAW_PAYLOAD_SENTINEL"]}]},
        }

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self): return json.dumps(payload).encode()

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp, "kobeissi/state.json")
            cfg = {"kobeissi": {
                "api_key": "TOKEN_SENTINEL",
                "state_path": str(state_path),
                "output_path": f"{tmp}/kobeissi",
            }}
            with patch("urllib.request.urlopen", return_value=Response()), patch("sources.kobeissi.logger") as mocked_logger:
                result = run_kobeissi(cfg)
            state = json.loads(state_path.read_text())

        self.assertIsInstance(result, NewsCollectionResult)
        self.assertEqual(result.items, [])
        self.assertEqual(result.status, "error")
        self.assertEqual(state["status"], "error")
        self.assertEqual(state["error"], result.error)
        self.assertIn("invalid response", state["error"])
        exposed = json.dumps({"result_error": result.error, "state": state, "logs": str(mocked_logger.method_calls)})
        self.assertNotIn("RAW_PAYLOAD_SENTINEL", exposed)
        self.assertNotIn("TOKEN_SENTINEL", exposed)

    def test_kobeissi_success_requires_explicit_data_and_tweets_schema(self):
        from sources.kobeissi import run_kobeissi

        malformed_payloads = (
            {"status": "success", "raw": "RAW_PAYLOAD_SENTINEL"},
            {"status": "success", "data": {}, "raw": "RAW_PAYLOAD_SENTINEL"},
            {"status": "success", "data": None, "raw": "RAW_PAYLOAD_SENTINEL"},
        )

        for payload in malformed_payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                class Response:
                    def __enter__(self): return self
                    def __exit__(self, *args): return None
                    def read(self): return json.dumps(payload).encode()

                state_path = Path(tmp, "kobeissi/state.json")
                state_path.parent.mkdir(parents=True)
                state_path.write_text(json.dumps({
                    "last_seen_id": "42",
                    "last_poll": "2026-01-01T00:00:00+00:00",
                    "status": "ok",
                    "error": None,
                }))
                cfg = {"kobeissi": {
                    "api_key": "TOKEN_SENTINEL",
                    "state_path": str(state_path),
                    "output_path": f"{tmp}/kobeissi",
                }}
                with patch("urllib.request.urlopen", return_value=Response()), patch("sources.kobeissi.logger") as mocked_logger:
                    result = run_kobeissi(cfg)
                state = json.loads(state_path.read_text())

                self.assertEqual(result.items, [])
                self.assertEqual(result.status, "error")
                self.assertEqual(result.error, "Kobeissi upstream API returned an invalid response")
                self.assertEqual(state["status"], "error")
                self.assertEqual(state["error"], result.error)
                self.assertNotEqual(state["last_poll"], "2026-01-01T00:00:00+00:00")
                exposed = json.dumps({
                    "result_error": result.error,
                    "state": state,
                    "logs": str(mocked_logger.method_calls),
                })
                self.assertNotIn("RAW_PAYLOAD_SENTINEL", exposed)
                self.assertNotIn("TOKEN_SENTINEL", exposed)

    def test_kobeissi_successful_empty_is_typed_ok_and_updates_state(self):
        from sources.kobeissi import run_kobeissi
        payload = {"status": "success", "data": {"tweets": []}}

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self): return json.dumps(payload).encode()

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp, "kobeissi/state.json")
            cfg = {"kobeissi": {"api_key": "key", "state_path": str(state_path), "output_path": f"{tmp}/kobeissi"}}
            with patch("urllib.request.urlopen", return_value=Response()):
                result = run_kobeissi(cfg)
            state = json.loads(state_path.read_text())

        self.assertEqual(result.items, [])
        self.assertEqual(result.status, "ok")
        self.assertIsNone(result.error)
        self.assertEqual(state["status"], "ok")
        self.assertIsNotNone(state["last_poll"])

    def test_kobeissi_upstream_api_error_is_typed_failure(self):
        from sources.kobeissi import run_kobeissi
        payload = {"status": "error", "msg": "api_key=secret"}

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self): return json.dumps(payload).encode()

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp, "kobeissi/state.json")
            cfg = {"kobeissi": {"api_key": "key", "state_path": str(state_path), "output_path": f"{tmp}/kobeissi"}}
            with patch("urllib.request.urlopen", return_value=Response()):
                result = run_kobeissi(cfg)
            state_text = state_path.read_text()

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error, "Kobeissi upstream API returned an error")
        self.assertNotIn("secret", state_text)

    def test_reuters_index_failure_is_sanitized_in_state_result_and_logs(self):
        from sources.reuters import run_reuters

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp, "reuters/state.json")
            cfg = {"reuters": {"state_path": str(state_path), "output_path": f"{tmp}/reuters"}}
            with patch("sources.reuters._fetch_sitemap_index", side_effect=RuntimeError("https://index.test/list.xml?token=private#secret")), patch("sources.reuters.logger") as mocked_logger:
                result = run_reuters(cfg)
            state_text = state_path.read_text()

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error, "Reuters sitemap index failed: RuntimeError")
        self.assertNotIn("private", state_text)
        self.assertNotIn("secret", state_text)
        self.assertNotIn("private", str(mocked_logger.method_calls))
        self.assertNotIn("secret", str(mocked_logger.method_calls))

    def test_reuters_malformed_xml_strips_query_and_parser_details(self):
        import xml.etree.ElementTree as ET
        from sources.reuters import run_reuters

        page_url = "https://example.test/malformed.xml?api_key=secret#token=private"
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp, "reuters/state.json")
            cfg = {"reuters": {"state_path": str(state_path), "output_path": f"{tmp}/reuters"}}
            with patch("sources.reuters._fetch_sitemap_index", return_value=[page_url]), patch("sources.reuters._parse_sitemap_page", side_effect=ET.ParseError("token=private at line 1")), patch("sources.reuters.logger") as mocked_logger:
                result = run_reuters(cfg)
            state_text = state_path.read_text()

        self.assertEqual(result.status, "error")
        self.assertIn("https://example.test/malformed.xml", result.error)
        self.assertIn("ParseError", result.error)
        self.assertNotIn("private", state_text)
        self.assertNotIn("secret", state_text)
        self.assertNotIn("private", str(mocked_logger.method_calls))
        self.assertNotIn("secret", str(mocked_logger.method_calls))

    def test_news_source_cli_exits_nonzero_and_does_not_report_zero_success(self):
        from cli import cli
        from sources.news_result import NewsCollectionResult

        cfg = {"reuters": {"enabled": True}, "news_feed": {"output_path": "unused"}}
        failure = NewsCollectionResult([], "error", "Reuters sitemap index failed: TimeoutError")
        with patch("cli.load_config", return_value=cfg), patch("sources.reuters.run_reuters", return_value=failure), patch("sources.news_feed.collect_and_publish", side_effect=lambda _source, _config, collector, **_kwargs: collector()):
            result = CliRunner().invoke(cli, ["news", "reuters"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Reuters collection failed", result.output)
        self.assertNotIn("Found 0", result.output)

    def test_news_all_exits_nonzero_when_any_source_fails(self):
        from cli import cli
        from sources.news_result import NewsCollectionResult

        cfg = {"reuters": {"enabled": True}, "kobeissi": {"enabled": False}, "news_feed": {"output_path": "unused"}}
        failure = NewsCollectionResult([], "error", "Reuters sitemap index failed: TimeoutError")
        with patch("cli.load_config", return_value=cfg), patch("sources.reuters.run_reuters", return_value=failure), patch("sources.news_feed.build_feed", return_value={"count": 0, "sources": [], "items": []}):
            result = CliRunner().invoke(cli, ["news", "all"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Reuters: failed", result.output)
        self.assertNotIn("Reuters: 0 articles", result.output)

    def test_news_cli_uses_configured_defaults_when_options_are_omitted(self):
        from cli import cli
        from sources.news_result import NewsCollectionResult

        cfg = {"reuters": {"enabled": True, "max_pages": 8}, "kobeissi": {"enabled": True, "count": 35}, "news_feed": {"output_path": "unused", "history_days": 12}}
        success = NewsCollectionResult([], "ok", None)
        with patch("cli.load_config", return_value=cfg), patch("sources.reuters.run_reuters", return_value=success) as reuters, patch("sources.kobeissi.run_kobeissi", return_value=success) as kobeissi, patch("sources.news_feed.collect_and_publish", side_effect=lambda _source, _config, collector, **_kwargs: collector()) as publish:
            result = CliRunner().invoke(cli, ["news", "all"])

        self.assertEqual(result.exit_code, 0, result.output)
        reuters.assert_called_once_with(cfg, max_pages=8)
        kobeissi.assert_called_once_with(cfg, count=35)
        self.assertEqual(publish.call_count, 2)
        self.assertTrue(all(call.kwargs["days"] == 12 for call in publish.call_args_list))


if __name__ == "__main__":
    unittest.main()
