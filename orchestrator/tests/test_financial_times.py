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


if __name__ == "__main__":
    unittest.main()
