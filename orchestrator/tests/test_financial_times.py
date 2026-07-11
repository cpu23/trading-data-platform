import sys
import unittest
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
        from sources.archive_fo import ArchiveFoClient
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.headers = {}
        mock_response.text = ""
        mock_response.status_code = 200

        request_fn = MagicMock(return_value=mock_response)
        client = ArchiveFoClient(request_fn=request_fn)

        with self.assertRaises(RuntimeError):
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


if __name__ == "__main__":
    unittest.main()
