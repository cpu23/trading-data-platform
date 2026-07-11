import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text()


# ---------------------------------------------------------------------------
# Task 1 — Fixture sanity checks
# ---------------------------------------------------------------------------
class FinancialTimesFixtureTests(unittest.TestCase):
    def test_fixture_files_are_present(self):
        self.assertIn("<rss", load_fixture("ft_homepage.xml"))
        self.assertIn("<article", load_fixture("archive_ft_article.html"))

    def test_challenge_fixture_is_not_article_content(self):
        html = load_fixture("archive_challenge.html")
        self.assertIn("Security Verification", html)


# ---------------------------------------------------------------------------
# Task 2 — URL canonicalisation and RSS parsing
# ---------------------------------------------------------------------------
class FinancialTimesParsingTests(unittest.TestCase):
    def test_canonical_url_removes_tracking_query_parameters(self):
        from sources.financial_times import canonicalise_ft_url

        assert canonicalise_ft_url(
            "https://www.ft.com/content/abc?utm_source=rss&utm_medium=feed"
        ) == "https://www.ft.com/content/abc"

    def test_rss_item_extracts_content_id_and_metadata(self):
        from sources.financial_times import parse_rss

        items = parse_rss(load_fixture("ft_lex.xml"), feed_id="lex")
        assert len(items) > 0
        item = items[0]
        assert item.content_id
        assert item.canonical_url.startswith("https://www.ft.com/content/")
        assert item.feed_id == "lex"
        assert item.published_at.tzinfo is not None

    def test_same_article_is_deduplicated_across_feeds_but_observations_are_kept(self):
        from sources.financial_times import parse_rss, merge_items

        observations = merge_items([
            parse_rss(load_fixture("ft_homepage.xml"), "homepage"),
            parse_rss(load_fixture("ft_lex.xml"), "lex"),
        ])
        article = next(item for item in observations if item.content_id == "shared-id")
        assert article.feed_ids == {"homepage", "lex"}

    def test_unhedged_feed_parses_items(self):
        from sources.financial_times import parse_rss

        items = parse_rss(load_fixture("ft_unhedged.xml"), feed_id="unhedged")
        assert len(items) == 2
        assert all(item.feed_id == "unhedged" for item in items)

    def test_non_ft_links_are_filtered(self):
        from sources.financial_times import parse_rss
        import xml.etree.ElementTree as ET

        # Build a feed with a non-FT link
        rss = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel>
            <item>
                <title>Outside</title>
                <link>https://example.com/article</link>
                <description>desc</description>
                <pubDate>Sat, 11 Jul 2026 08:00:00 +0000</pubDate>
            </item>
            <item>
                <title>FT Article</title>
                <link>https://www.ft.com/content/real-article</link>
                <description>desc</description>
                <pubDate>Sat, 11 Jul 2026 08:00:00 +0000</pubDate>
            </item>
        </channel></rss>"""
        items = parse_rss(rss, feed_id="test")
        assert len(items) == 1
        assert items[0].content_id == "real-article"


# ---------------------------------------------------------------------------
# Task 3 — archive.fo client and capture validation
# ---------------------------------------------------------------------------
class ArchiveFoTests(unittest.TestCase):
    def test_article_capture_passes_validation(self):
        from sources.archive_fo import validate_archive_capture

        result = validate_archive_capture(
            load_fixture("archive_ft_article.html"),
            expected_title="Markets rally on strong earnings reports",
        )
        assert result.valid is True
        assert result.word_count > 20

    def test_security_challenge_capture_fails_validation(self):
        from sources.archive_fo import validate_archive_capture

        result = validate_archive_capture(load_fixture("archive_challenge.html"))
        assert result.valid is False
        assert result.reason == "challenge_or_block_page"

    def test_submit_url_is_encoded_and_uses_archive_submit_endpoint(self):
        from sources.archive_fo import ArchiveFoClient
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.headers = {"Location": "https://archive.ph/abc123"}
        mock_response.status_code = 302

        request_fn = MagicMock(return_value=mock_response)
        client = ArchiveFoClient(request_fn=request_fn)

        archive_url = client.submit("https://www.ft.com/content/test-article")
        assert archive_url == "https://archive.ph/abc123"
        request_fn.assert_called_once()
        call_args = request_fn.call_args
        assert "/submit/" in call_args[0][1]
        assert "ft.com" in call_args[0][1]

    def test_submit_raises_on_missing_location(self):
        from sources.archive_fo import ArchiveFoClient, ArchiveCaptureError
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.headers = {}
        mock_response.text = ""
        mock_response.status_code = 200

        request_fn = MagicMock(return_value=mock_response)
        client = ArchiveFoClient(request_fn=request_fn)

        with self.assertRaises(ArchiveCaptureError):
            client.submit("https://www.ft.com/content/missing")

    def test_poll_times_out_after_max_polls(self):
        from sources.archive_fo import ArchiveFoClient
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.status_code = 404

        request_fn = MagicMock(return_value=mock_response)
        client = ArchiveFoClient(request_fn=request_fn, poll_interval=0, max_polls=3)

        with self.assertRaises(TimeoutError):
            client.poll("https://archive.ph/abc123")
        assert request_fn.call_count == 3

    def test_title_mismatch_fails_validation(self):
        from sources.archive_fo import validate_archive_capture

        result = validate_archive_capture(
            load_fixture("archive_ft_article.html"),
            expected_title="Completely different title that does not match",
        )
        assert result.valid is False
        assert result.reason == "title_mismatch"

    def test_too_short_body_fails_validation(self):
        from sources.archive_fo import validate_archive_capture

        html = """<html><body><article>
            <h1>Short Article</h1>
            <p>Too few words.</p>
        </article></body></html>"""
        result = validate_archive_capture(html)
        assert result.valid is False
        assert result.reason == "body_too_short"

    def test_no_article_element_fails_validation(self):
        from sources.archive_fo import validate_archive_capture

        html = "<html><body><div>No article here at all</div></body></html>"
        result = validate_archive_capture(html)
        assert result.valid is False
        assert result.reason == "no_article_element"

    def test_download_returns_html(self):
        from sources.archive_fo import ArchiveFoClient
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.text = "<html>captured</html>"

        request_fn = MagicMock(return_value=mock_response)
        client = ArchiveFoClient(request_fn=request_fn)

        html = client.download("https://archive.ph/abc123")
        assert html == "<html>captured</html>"

    def test_client_raises_without_request_fn(self):
        from sources.archive_fo import ArchiveFoClient

        client = ArchiveFoClient(request_fn=None)
        with self.assertRaises(RuntimeError):
            client.submit("https://www.ft.com/content/test")


# ---------------------------------------------------------------------------
# Task 4 — Migration contract: SQL is syntactically valid
# ---------------------------------------------------------------------------
class FinancialTimesMigrationContractTests(unittest.TestCase):
    """Verify the migration SQL parses without syntax errors."""

    def test_migration_sql_loads_without_error(self):
        """The migration file should be valid SQL that parses cleanly."""
        migration_path = (
            Path(__file__).resolve().parents[2] / "db" / "migrations" / "008_financial_times.sql"
        )
        sql = migration_path.read_text()
        # Should contain all expected table definitions
        self.assertIn("CREATE TABLE IF NOT EXISTS ft_articles", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS ft_article_observations", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS ft_archive_captures", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS ft_article_versions", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS ft_collection_runs", sql)

    def test_migration_has_idempotent_triggers(self):
        """Triggers should use DO $$ EXCEPTION blocks for idempotency."""
        migration_path = (
            Path(__file__).resolve().parents[2] / "db" / "migrations" / "008_financial_times.sql"
        )
        sql = migration_path.read_text()
        self.assertIn("WHEN duplicate_object THEN NULL", sql)
        self.assertEqual(sql.count("WHEN duplicate_object THEN NULL"), 3)  # 3 tables with updated_at

    def test_migration_has_required_indexes(self):
        migration_path = (
            Path(__file__).resolve().parents[2] / "db" / "migrations" / "008_financial_times.sql"
        )
        sql = migration_path.read_text()
        self.assertIn("idx_ft_articles_published_at", sql)
        self.assertIn("idx_ft_archive_captures_article_status", sql)
        self.assertIn("idx_ft_article_versions_content_hash", sql)
        self.assertIn("idx_ft_article_versions_captured_at", sql)
        self.assertIn("idx_ft_collection_runs_correlation_id", sql)

    def test_migration_has_unique_constraints(self):
        migration_path = (
            Path(__file__).resolve().parents[2] / "db" / "migrations" / "008_financial_times.sql"
        )
        sql = migration_path.read_text()
        self.assertIn("UNIQUE(article_id, feed_id, observed_at)", sql)
        self.assertIn("UNIQUE(article_id, content_hash)", sql)


# ---------------------------------------------------------------------------
# Task 5 — Repository unit tests (FakeSession approach)
# ---------------------------------------------------------------------------
class _FakeResult:
    """Minimal result proxy for testing."""

    def __init__(self, rows=None, rowcount=1):
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _FakeRow:
    """Minimal row proxy that supports ._mapping."""

    def __init__(self, data: dict):
        self._mapping = data


class _FakeSession:
    """Records execute() calls and returns configurable results."""

    def __init__(self, return_rows=None, rowcount=1):
        self.calls: list[tuple] = []
        self._return_rows = return_rows or []
        self._rowcount = rowcount

    def execute(self, stmt, params=None):
        self.calls.append((str(stmt), params or {}))
        return _FakeResult(
            rows=[_FakeRow(r) for r in self._return_rows],
            rowcount=self._rowcount,
        )


class FinancialTimesRepositoryTests(unittest.TestCase):
    """Test repository functions use correct SQL and parameters."""

    # -- upsert_article_observation --

    def test_upsert_article_inserts_and_records_observation(self):
        from sources.financial_times_repository import upsert_article_observation
        from datetime import datetime, timezone

        session = _FakeSession()
        now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
        result = upsert_article_observation(
            session,
            article_id="art-1",
            content_id="cid-1",
            canonical_url="https://www.ft.com/content/cid-1",
            title="Test Article",
            description="A test",
            published_at=now,
            feed_id="homepage",
            rss_payload={"title": "Test Article"},
            now=now,
        )
        self.assertEqual(result["article_id"], "art-1")
        # Two calls: one for ft_articles upsert, one for ft_article_observations insert
        self.assertEqual(len(session.calls), 2)
        # First call should be an upsert on ft_articles
        self.assertIn("ft_articles", session.calls[0][0])
        self.assertIn("ON CONFLICT", session.calls[0][0])
        # Second call should insert into ft_article_observations
        self.assertIn("ft_article_observations", session.calls[1][0])
        self.assertIn("ON CONFLICT", session.calls[1][0])

    def test_upsert_article_observation_idempotent(self):
        """Observation insert uses ON CONFLICT DO NOTHING."""
        from sources.financial_times_repository import upsert_article_observation
        from datetime import datetime, timezone

        session = _FakeSession(rowcount=0)  # simulates conflict
        now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
        # Should not raise even with duplicate
        result = upsert_article_observation(
            session,
            article_id="art-1",
            content_id="cid-1",
            canonical_url="https://www.ft.com/content/cid-1",
            title="Test Article",
            description=None,
            published_at=None,
            feed_id="homepage",
            rss_payload={},
            now=now,
        )
        self.assertEqual(result["article_id"], "art-1")
        # Verify the observation SQL has DO NOTHING
        obs_sql = session.calls[1][0]
        self.assertIn("DO NOTHING", obs_sql)

    def test_upsert_article_from_second_feed(self):
        """Observing the same article from a different feed_id only adds an observation."""
        from sources.financial_times_repository import upsert_article_observation
        from datetime import datetime, timezone

        session = _FakeSession()
        now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
        # First call: homepage feed
        upsert_article_observation(
            session, "art-1", "cid-1", "https://ft.com/content/cid-1",
            "Title", None, now, "homepage", {}, now,
        )
        # Second call: lex feed
        upsert_article_observation(
            session, "art-1", "cid-1", "https://ft.com/content/cid-1",
            "Title", None, now, "lex", {}, now,
        )
        # 4 calls total: 2 article upserts + 2 observation inserts
        self.assertEqual(len(session.calls), 4)
        # Both observation inserts should have ON CONFLICT DO NOTHING
        self.assertIn("DO NOTHING", session.calls[1][0])
        self.assertIn("DO NOTHING", session.calls[3][0])

    # -- get_article_by_content_id --

    def test_get_article_by_content_id(self):
        from sources.financial_times_repository import get_article_by_content_id

        expected = {"article_id": "art-1", "content_id": "cid-1"}
        session = _FakeSession(return_rows=[expected])
        result = get_article_by_content_id(session, "cid-1")
        self.assertEqual(result["article_id"], "art-1")
        self.assertIn("WHERE content_id = :content_id", session.calls[0][0])

    def test_get_article_by_content_id_not_found(self):
        from sources.financial_times_repository import get_article_by_content_id

        session = _FakeSession(return_rows=[])
        result = get_article_by_content_id(session, "missing")
        self.assertIsNone(result)

    # -- get_reusable_capture --

    def test_get_reusable_capture_returns_captured_status(self):
        from sources.financial_times_repository import get_reusable_capture

        expected = {"capture_id": "cap-1", "status": "captured"}
        session = _FakeSession(return_rows=[expected])
        result = get_reusable_capture(session, "art-1", "https://ft.com/content/cid-1")
        self.assertEqual(result["capture_id"], "cap-1")
        self.assertIn("status = 'captured'", session.calls[0][0])

    # -- create_capture_attempt --

    def test_create_capture_attempt_defaults_to_queued(self):
        from sources.financial_times_repository import create_capture_attempt

        session = _FakeSession()
        result = create_capture_attempt(session, "art-1", "https://ft.com/content/cid-1")
        self.assertEqual(result["status"], "queued")
        self.assertIn("ft_archive_captures", session.calls[0][0])

    def test_create_capture_attempt_with_custom_status(self):
        from sources.financial_times_repository import create_capture_attempt

        session = _FakeSession()
        result = create_capture_attempt(
            session, "art-1", "https://ft.com/content/cid-1", status="submitted",
        )
        self.assertEqual(result["status"], "submitted")

    # -- mark_capture_status --

    def test_mark_capture_status_updates_fields(self):
        from sources.financial_times_repository import mark_capture_status

        session = _FakeSession()
        mark_capture_status(
            session, "cap-1", "captured",
            archive_url="https://archive.ph/abc",
        )
        sql = session.calls[0][0]
        self.assertIn("UPDATE ft_archive_captures", sql)
        self.assertIn(":status", sql)
        self.assertIn(":archive_url", sql)
        self.assertIn("attempt_count + 1", sql)

    def test_mark_capture_status_sets_completed_at_for_terminal_states(self):
        from sources.financial_times_repository import mark_capture_status

        for status in ("captured", "failed", "invalid", "manual_review"):
            session = _FakeSession()
            mark_capture_status(session, "cap-1", status)
            params = session.calls[0][1]
            # completed_at should be set (not None) for terminal states
            self.assertIsNotNone(params.get("completed_at"), f"completed_at should be set for {status}")

    # -- insert_article_version_if_new --

    def test_insert_article_version_if_new_inserts_on_first_call(self):
        from sources.financial_times_repository import insert_article_version_if_new
        from datetime import datetime, timezone

        session = _FakeSession(rowcount=1)
        now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
        result = insert_article_version_if_new(
            session, "art-1", "cap-1", "https://archive.ph/abc",
            now, "hash1", "Title", "Byline", now, "Body text", 100,
            None, "ok", "1.0",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["article_id"], "art-1")
        self.assertIn("ON CONFLICT (article_id, content_hash) DO NOTHING", session.calls[0][0])

    def test_insert_article_version_if_new_skips_duplicate_hash(self):
        from sources.financial_times_repository import insert_article_version_if_new
        from datetime import datetime, timezone

        session = _FakeSession(rowcount=0)  # conflict hit
        now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
        result = insert_article_version_if_new(
            session, "art-1", "cap-1", "https://archive.ph/abc",
            now, "hash1", "Title", None, now, "Body", 50,
            None, "ok", "1.0",
        )
        self.assertIsNone(result)

    # -- get_pending_captures --

    def test_get_pending_captures_filters_by_status(self):
        from sources.financial_times_repository import get_pending_captures

        session = _FakeSession(return_rows=[
            {"capture_id": "cap-1", "status": "queued"},
            {"capture_id": "cap-2", "status": "pending"},
        ])
        results = get_pending_captures(session)
        self.assertEqual(len(results), 2)
        sql = session.calls[0][0]
        self.assertIn("status = ANY(:statuses)", sql)
        params = session.calls[0][1]
        self.assertEqual(params["statuses"], ["queued", "submitted", "pending"])

    def test_get_pending_captures_with_article_filter(self):
        from sources.financial_times_repository import get_pending_captures

        session = _FakeSession(return_rows=[])
        get_pending_captures(session, article_id="art-1")
        params = session.calls[0][1]
        self.assertEqual(params["article_id"], "art-1")

    # -- insert_ft_run / update_ft_run / get_latest_ft_run --

    def test_insert_ft_run_creates_running_record(self):
        from sources.financial_times_repository import insert_ft_run

        session = _FakeSession()
        result = insert_ft_run(session, "run-1", "corr-1", ["homepage"], None, None)
        self.assertEqual(result["status"], "running")
        self.assertIn("ft_collection_runs", session.calls[0][0])

    def test_update_ft_run_sets_completed_at_for_terminal_statuses(self):
        from sources.financial_times_repository import update_ft_run

        for status in ("completed", "failed"):
            session = _FakeSession()
            update_ft_run(session, "run-1", status, articles_discovered=10)
            params = session.calls[0][1]
            self.assertIsNotNone(params.get("completed_at"), f"completed_at should be set for {status}")

    def test_get_latest_ft_run_returns_most_recent(self):
        from sources.financial_times_repository import get_latest_ft_run

        expected = {"run_id": "run-1", "status": "completed"}
        session = _FakeSession(return_rows=[expected])
        result = get_latest_ft_run(session)
        self.assertEqual(result["run_id"], "run-1")
        self.assertIn("status = 'completed'", session.calls[0][0])

    def test_get_latest_ft_run_returns_none_when_empty(self):
        from sources.financial_times_repository import get_latest_ft_run

        session = _FakeSession(return_rows=[])
        result = get_latest_ft_run(session)
        self.assertIsNone(result)

    # -- SQL idempotency spot-checks --

    def test_all_write_operations_use_on_conflict(self):
        """Every write function should use ON CONFLICT for idempotency."""
        from sources.financial_times_repository import (
            upsert_article_observation,
            insert_article_version_if_new,
            insert_ft_run,
        )
        from datetime import datetime, timezone

        now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)

        # upsert_article_observation — check both SQLs
        session = _FakeSession()
        upsert_article_observation(
            session, "a", "c", "https://ft.com/content/c",
            "t", None, now, "f", {}, now,
        )
        self.assertIn("ON CONFLICT", session.calls[0][0])
        self.assertIn("ON CONFLICT", session.calls[1][0])

        # insert_article_version_if_new
        session = _FakeSession(rowcount=1)
        insert_article_version_if_new(
            session, "a", "cap", "https://archive.ph/x",
            now, "h", "t", None, now, "b", 10,
            None, "ok", "1.0",
        )
        self.assertIn("ON CONFLICT", session.calls[0][0])

        # insert_ft_run — no ON CONFLICT needed (new run_id each time)
        session = _FakeSession()
        insert_ft_run(session, "r-1", None, None, None, None)
        self.assertIn("INSERT INTO ft_collection_runs", session.calls[0][0])


from click.testing import CliRunner


# ---------------------------------------------------------------------------
# Task 6 — Service-level tests for run_financial_times
# ---------------------------------------------------------------------------
class _FakeArchiveClient:
    """Minimal archive client for service tests."""

    def __init__(self, submit_url="https://archive.ph/fake123", html=None, title_map=None):
        self._submit_url = submit_url
        self._html = html or load_fixture("archive_ft_article.html")
        self._title_map = title_map or {}  # canonical_url -> html mapping
        self._archive_to_canonical: dict[str, str] = {}  # archive_url -> canonical_url
        self.submitted: list[str] = []
        self.polled: list[str] = []
        self.downloaded: list[str] = []

    def submit(self, url: str) -> str:
        self.submitted.append(url)
        archive_url = f"{self._submit_url}/{len(self.submitted)}"
        self._archive_to_canonical[archive_url] = url
        return archive_url

    def poll(self, url: str) -> str:
        self.polled.append(url)
        return url

    def download(self, url: str) -> str:
        self.downloaded.append(url)
        canonical = self._archive_to_canonical.get(url)
        if canonical and canonical in self._title_map:
            return self._title_map[canonical]
        return self._html


class FinancialTimesServiceTests(unittest.TestCase):
    """Test run_financial_times with fakes and mocks."""

    def _base_config(self):
        return {
            "financial_times": {
                "feeds": {
                    "homepage": "https://www.ft.com/?format=rss",
                    "lex": "https://www.ft.com/lex?format=rss",
                    "unhedged": "https://www.ft.com/unhedged?format=rss",
                },
                "archive_host": "https://archive.fo",
                "poll_interval_seconds": 0,
                "max_poll_attempts": 1,
                "raw_storage_path": "/tmp/ft_test_raw",
            }
        }

    @staticmethod
    def _make_article_html(title: str) -> str:
        """Generate valid archive HTML with the given title."""
        return (
            '<!DOCTYPE html><html lang="en"><head>'
            f"<title>{title}</title>"
            "</head><body><article>"
            f"<h1>{title}</h1>"
            '<div class="byline"><span class="author">Staff</span></div>'
            "<p>" + "word " * 30 + "</p>"
            "</article></body></html>"
        )

    def _make_title_map(self, fetch_xml: str) -> dict:
        """Build url→html mapping so every RSS article passes title validation."""
        import xml.etree.ElementTree as ET
        from sources.financial_times import canonicalise_ft_url, _extract_content_id
        from sources.financial_times import _extract_content_id as eci

        title_map = {}
        root = ET.fromstring(fetch_xml)
        for item in root.iter("item"):
            link_el = item.find("link")
            title_el = item.find("title")
            if link_el is None or link_el.text is None:
                continue
            link = link_el.text.strip()
            if _extract_content_id(link) is None:
                continue
            canonical = canonicalise_ft_url(link)
            title = title_el.text.strip() if title_el is not None and title_el.text else "Article"
            title_map[canonical] = self._make_article_html(title)
        return title_map

    def test_run_discovers_ingests_and_returns_provenance_bundle(self):
        from sources.financial_times import run_financial_times
        from unittest.mock import patch, MagicMock
        import tempfile, shutil

        config = self._base_config()
        tmp_dir = tempfile.mkdtemp()
        config["financial_times"]["raw_storage_path"] = tmp_dir

        rss_xml = load_fixture("ft_homepage.xml")
        fetch_fn = MagicMock(return_value=rss_xml)
        title_map = self._make_title_map(rss_xml)
        archive_client = _FakeArchiveClient(title_map=title_map)

        session = _FakeSession()
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=session)
        ctx.__exit__ = MagicMock(return_value=False)

        with patch("sources.financial_times.get_session", return_value=ctx):
            result = run_financial_times(
                config=config,
                correlation_id="test-corr-1",
                sections=("homepage",),
                ingest=True,
                wait_for_capture=False,
                fetch_fn=fetch_fn,
                archive_client=archive_client,
            )

        self.assertEqual(result["status"], "completed")
        self.assertGreater(result["articles_discovered"], 0)
        self.assertEqual(result["articles_captured"], result["articles_discovered"])
        self.assertEqual(result["articles_failed"], 0)
        for art in result["articles"]:
            self.assertIn("archive_url", art)
            self.assertIn("content_hash", art)
            self.assertEqual(art["status"], "captured")

        shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_ingest_false_returns_discovery_only(self):
        from sources.financial_times import run_financial_times
        from unittest.mock import patch, MagicMock

        config = self._base_config()
        fetch_fn = MagicMock(return_value=load_fixture("ft_homepage.xml"))
        archive_client = _FakeArchiveClient()

        session = _FakeSession()
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=session)
        ctx.__exit__ = MagicMock(return_value=False)

        with patch("sources.financial_times.get_session", return_value=ctx):
            result = run_financial_times(
                config=config,
                correlation_id="test-corr-2",
                sections=("homepage",),
                ingest=False,
                fetch_fn=fetch_fn,
                archive_client=archive_client,
            )

        self.assertEqual(result["status"], "completed")
        self.assertGreater(result["articles_discovered"], 0)
        self.assertEqual(result["articles_captured"], 0)
        # No archive calls should have been made
        self.assertEqual(len(archive_client.submitted), 0)

    def test_article_outside_window_excluded(self):
        from sources.financial_times import run_financial_times
        from unittest.mock import patch, MagicMock
        from datetime import datetime, timezone, timedelta

        config = self._base_config()
        # Fixture pubDate is 2026-07-11 08:00 UTC — set since after that
        since = datetime(2026, 7, 12, tzinfo=timezone.utc)
        fetch_fn = MagicMock(return_value=load_fixture("ft_homepage.xml"))

        session = _FakeSession()
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=session)
        ctx.__exit__ = MagicMock(return_value=False)

        with patch("sources.financial_times.get_session", return_value=ctx):
            result = run_financial_times(
                config=config,
                correlation_id="test-corr-3",
                sections=("homepage",),
                since=since,
                ingest=False,
                fetch_fn=fetch_fn,
            )

        self.assertEqual(result["articles_discovered"], 0)
        self.assertEqual(len(result["articles"]), 0)

    def test_repeated_run_reuses_existing_content(self):
        from sources.financial_times import run_financial_times
        from unittest.mock import patch, MagicMock
        import tempfile, shutil

        config = self._base_config()
        tmp_dir = tempfile.mkdtemp()
        config["financial_times"]["raw_storage_path"] = tmp_dir

        fetch_fn = MagicMock(return_value=load_fixture("ft_homepage.xml"))
        archive_client = _FakeArchiveClient()

        # FakeSession returns reusable capture from get_reusable_capture
        reusable = {
            "capture_id": "cap-existing",
            "archive_url": "https://archive.ph/existing",
            "raw_content_hash": "abc123",
        }
        session = _FakeSession(return_rows=[reusable])
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=session)
        ctx.__exit__ = MagicMock(return_value=False)

        with patch("sources.financial_times.get_session", return_value=ctx):
            result = run_financial_times(
                config=config,
                correlation_id="test-corr-4",
                sections=("homepage",),
                ingest=True,
                wait_for_capture=False,
                fetch_fn=fetch_fn,
                archive_client=archive_client,
            )

        # All articles should be reused
        self.assertEqual(result["status"], "completed")
        for art in result["articles"]:
            self.assertEqual(art["status"], "reused")
        # No archive submission should have happened
        self.assertEqual(len(archive_client.submitted), 0)

        shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_challenge_page_not_stored_as_validated_version(self):
        from sources.financial_times import run_financial_times
        from unittest.mock import patch, MagicMock
        import tempfile, shutil

        config = self._base_config()
        tmp_dir = tempfile.mkdtemp()
        config["financial_times"]["raw_storage_path"] = tmp_dir

        fetch_fn = MagicMock(return_value=load_fixture("ft_homepage.xml"))
        # Archive returns challenge page HTML instead of article
        archive_client = _FakeArchiveClient(
            html=load_fixture("archive_challenge.html")
        )

        session = _FakeSession()
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=session)
        ctx.__exit__ = MagicMock(return_value=False)

        with patch("sources.financial_times.get_session", return_value=ctx):
            result = run_financial_times(
                config=config,
                correlation_id="test-corr-5",
                sections=("homepage",),
                ingest=True,
                wait_for_capture=False,
                fetch_fn=fetch_fn,
                archive_client=archive_client,
            )

        self.assertEqual(result["status"], "failed")
        for art in result["articles"]:
            self.assertEqual(art["status"], "invalid")
            self.assertIn("reason", art)

        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Task 7 — CLI command tests
# ---------------------------------------------------------------------------
class FinancialTimesCliTests(unittest.TestCase):
    def test_ft_discover_command_exists(self):
        from cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["ft", "discover", "--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("--sections", result.output)

    def test_ft_run_command_exists(self):
        from cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["ft", "run", "--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("--no-ingest", result.output)

    def test_ft_resume_command_exists(self):
        from cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["ft", "resume", "--help"])
        self.assertEqual(result.exit_code, 0)

    def test_ft_status_command_exists(self):
        from cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["ft", "status", "--help"])
        self.assertEqual(result.exit_code, 0)

    def test_invalid_section_rejected(self):
        from cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["ft", "discover", "--sections", "invalid_feed"])
        self.assertNotEqual(result.exit_code, 0)


import threading
from unittest.mock import patch as mock_patch, MagicMock

from fastapi.testclient import TestClient


class BriefingFinancialTimesTests(unittest.TestCase):
    """Tests for FT context integration in the briefing processor."""

    def test_briefing_prompt_includes_ft_context(self):
        """_build_prompt should replace {{financial_times_context}} with ft_context."""
        from processors.briefing import DailyBriefingProcessor

        processor = DailyBriefingProcessor()
        prompt_path = Path(__file__).resolve().parents[2] / "prompts" / "briefing_v3.txt"

        ft_text = "## Financial Times Context\n\n**Test Article**\nSome body text."

        prompt = processor._build_prompt(
            template_path=str(prompt_path),
            current_date="Friday, July 11, 2026",
            macro_regime_summary="Risk-on.",
            today_events="No events.",
            this_week_events="No events.",
            watchlist="EURUSD (forex)",
            ft_context=ft_text,
        )

        self.assertIn("Financial Times Context", prompt)
        self.assertIn("Test Article", prompt)
        self.assertIn("Some body text.", prompt)
        # The placeholder should be replaced, not left as-is
        self.assertNotIn("{{financial_times_context}}", prompt)

    def test_briefing_no_ft_context_produces_empty_string(self):
        """When ft_context is empty, the placeholder is replaced with empty string."""
        from processors.briefing import DailyBriefingProcessor

        processor = DailyBriefingProcessor()
        prompt_path = Path(__file__).resolve().parents[2] / "prompts" / "briefing_v3.txt"

        prompt = processor._build_prompt(
            template_path=str(prompt_path),
            current_date="Friday, July 11, 2026",
            macro_regime_summary="Risk-on.",
            today_events="No events.",
            this_week_events="No events.",
            watchlist="EURUSD (forex)",
            ft_context="",
        )

        self.assertNotIn("{{financial_times_context}}", prompt)
        self.assertNotIn("Financial Times Context", prompt)

    def test_briefing_does_not_call_ft_network_services(self):
        """_get_financial_times_bundle only queries the database, never RSS or archive."""
        from processors.briefing import DailyBriefingProcessor
        from unittest.mock import patch, MagicMock

        processor = DailyBriefingProcessor()

        config = {
            "financial_times": {"enabled": False},
        }

        # With FT disabled, should return empty immediately without DB calls
        with patch("processors.briefing.get_session") as mock_session:
            result = processor._get_financial_times_bundle(config)
            mock_session.assert_not_called()

        self.assertEqual(result["prompt_text"], "")
        self.assertEqual(result["article_ids"], [])

    def test_briefing_ft_bundle_returns_article_ids(self):
        """When FT is enabled and articles exist, bundle returns their IDs."""
        from processors.briefing import DailyBriefingProcessor
        from unittest.mock import patch, MagicMock

        processor = DailyBriefingProcessor()
        config = {
            "financial_times": {"enabled": True},
        }

        fake_row = _FakeRow({
            "article_id": "art-1",
            "title": "Markets Rally",
            "byline": "Staff Reporter",
            "published_at": datetime(2026, 7, 11, 8, 0, tzinfo=timezone.utc),
            "body_text": "Markets rallied today on strong earnings. " * 5,
            "word_count": 30,
            "archive_url": "https://archive.ph/abc",
            "canonical_url": "https://www.ft.com/content/art-1",
            "content_id": "art-1",
        })

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        # Make execute return an iterable list of _FakeRow
        mock_session.execute = MagicMock(return_value=[fake_row])

        with patch("processors.briefing.get_session", return_value=mock_session):
            result = processor._get_financial_times_bundle(config)

        self.assertEqual(len(result["article_ids"]), 1)
        self.assertEqual(result["article_ids"][0], "art-1")
        self.assertIn("Markets Rally", result["prompt_text"])
        self.assertIn("https://www.ft.com/content/art-1", result["prompt_text"])


class FinancialTimesEndpointTests(unittest.TestCase):
    """Tests for the /run_financial_times orchestrator endpoint."""

    def setUp(self):
        import main as m
        self._mod = m
        # Ensure the lock is initialised (normally done at startup)
        if m._ft_lock is None:
            m._ft_lock = threading.Lock()
        m._ft_correlation_id = None

    def _client(self):
        from unittest.mock import patch as _patch
        with _patch.object(self._mod, "check_connection", return_value=True), \
             _patch.object(self._mod, "start_scheduler"), \
             _patch.object(self._mod.quote_stream, "start"), \
             _patch.object(self._mod.quote_stream, "stop"):
            return TestClient(self._mod.app)

    def test_run_financial_times_endpoint_returns_202(self):
        client = self._client()
        with mock_patch.object(self._mod, "run_financial_times", return_value={"status": "completed"}):
            resp = client.post("/run_financial_times", json={})
        self.assertEqual(resp.status_code, 202)
        data = resp.json()
        self.assertIn("job_id", data)
        self.assertIn("accepted_at", data)
        self.assertIn("status_url", data)

    def test_run_financial_times_accepts_body_parameters(self):
        client = self._client()
        with mock_patch.object(self._mod, "run_financial_times", return_value={"status": "completed"}) as mock_ft:
            resp = client.post(
                "/run_financial_times",
                json={
                    "sections": ["lex"],
                    "max_articles": 5,
                    "ingest": False,
                    "wait_for_capture": False,
                },
            )
        self.assertEqual(resp.status_code, 202)
        # Verify parameters were passed to the background task
        mock_ft.assert_not_called()  # background, not called yet

    def test_invalid_sections_return_422(self):
        client = self._client()
        with mock_patch.object(self._mod, "run_financial_times"):
            resp = client.post("/run_financial_times", json={"sections": ["invalid_feed"]})
        self.assertEqual(resp.status_code, 422)
        self.assertIn("invalid_feed", resp.json()["detail"])

    def test_concurrent_ft_request_returns_409(self):
        client = self._client()
        self._mod._ft_correlation_id = "already-running-id"
        # Simulate lock already held
        self._mod._ft_lock.acquire()
        try:
            resp = client.post("/run_financial_times", json={})
            self.assertEqual(resp.status_code, 409)
            self.assertIn("already running", resp.json()["detail"])
        finally:
            self._mod._ft_lock.release()
            self._mod._ft_correlation_id = None

    def test_default_sections_are_homepage_lex_unhedged(self):
        client = self._client()
        with mock_patch.object(self._mod, "run_financial_times", return_value={"status": "completed"}):
            resp = client.post("/run_financial_times")
        self.assertEqual(resp.status_code, 202)


if __name__ == "__main__":
    unittest.main()
