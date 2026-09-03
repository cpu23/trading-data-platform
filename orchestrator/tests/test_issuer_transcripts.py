import socket
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.issuer_transcripts import IssuerTranscriptsCollector
from contracts.runtime_config import CollectorConfig


def _public_dns(host, port, *args, **kwargs):
    """Reserved test hosts must resolve publicly for origin validation."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]


def _private_dns(host, port, *args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", port or 443))]


def _response(
    content=b"",
    status=200,
    headers=None,
    content_type="text/html; charset=utf-8",
    is_redirect=False,
    text=None,
):
    response = Mock()
    response.status_code = status
    response.is_redirect = is_redirect
    response.content = content
    response.headers = {"content-type": content_type}
    if headers:
        response.headers.update(headers)
    response.text = text if text is not None else content.decode("utf-8", "replace")
    response.raise_for_status.return_value = None
    return response


def _base_config(**overrides):
    config = {
        "collectors": {
            "issuer_transcripts": {
                "schedule": "0 7 * * *",
                "max_issuers": 20,
                "max_page_bytes": 2_000_000,
                "max_document_bytes": 25_000_000,
                "max_links_per_page": 50,
                "max_records_per_issuer": 25,
                "max_redirects": 5,
                "timeout_seconds": 30,
                "user_agent": "TradingDataTranscriptCollector/1.0",
                "issuers": [
                    {
                        "institution": "Example Corp",
                        "ticker": "EXMP",
                        "url": "https://example.test/ir/events",
                        "document_type": "earnings_transcript",
                    }
                ],
            }
        }
    }
    section = config["collectors"]["issuer_transcripts"]
    section.update(overrides)
    return config


HTML_PAGE = b"""<!DOCTYPE html>
<html>
<head><title>Example Corp - IR Events</title></head>
<body>
  <h1>Events and Presentations</h1>
  <a href="/ir/q3-2026-transcript">Q3 2026 Earnings Call Transcript</a>
  <a href="/ir/q2-2026-transcript">Q2 2026 Earnings Call Transcript</a>
  <a href="/ir/press-release-q3">Q3 2026 Financial Results Press Release</a>
</body>
</html>"""

TRANSCRIPT_PAGE = b"""<!DOCTYPE html>
<html>
<head><title>Q3 2026 Earnings Call Transcript</title></head>
<body>
  <p><strong>Operator:</strong> Good morning and welcome to the call.</p>
  <p><strong>Jane Doe:</strong> Revenue grew 15% year-over-year.</p>
</body>
</html>"""

RSS_FEED = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>Example Corp Transcripts</title>
    <link>https://example.test/ir</link>
    <item>
      <title>Q3 2026 Earnings Call</title>
      <link>https://example.test/ir/q3-2026-transcript</link>
      <pubDate>Thu, 29 Oct 2026 14:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>"""


class RuntimeConfigMappingTests(unittest.TestCase):
    def test_collect_accepts_validated_runtime_config_mapping(self):
        section = CollectorConfig(
            issuers=[
                {
                    "institution": "Typed Corp",
                    "ticker": "TYPD",
                    "url": "https://example.test/ir",
                    "document_type": "earnings_transcript",
                }
            ],
            timeout_seconds=15.0,
        )
        collector = IssuerTranscriptsCollector()

        with (
            patch("collectors.issuer_transcripts.make_request") as request,
            patch("socket.getaddrinfo", side_effect=_public_dns),
        ):
            request.side_effect = [
                _response(HTML_PAGE),
                _response(TRANSCRIPT_PAGE),
                _response(TRANSCRIPT_PAGE),
            ]
            records = collector.collect(
                {"collectors": {"issuer_transcripts": section}}, "typed-config"
            )

        self.assertEqual(records[0]["institution"], "Typed Corp")


class IssuerTranscriptDiscoveryTests(unittest.TestCase):
    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_html_page_yields_deterministic_text_transcript(
        self, _dns, request
    ):
        request.side_effect = [
            _response(HTML_PAGE),
            _response(TRANSCRIPT_PAGE),
            _response(TRANSCRIPT_PAGE),
        ]
        config = _base_config()
        collector = IssuerTranscriptsCollector()
        records = collector.collect(config, "corr-1")

        self.assertGreaterEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["institution"], "Example Corp")
        self.assertEqual(record["source"], "issuer_transcripts")
        self.assertEqual(record["url"], "https://example.test/ir/q3-2026-transcript")
        self.assertIn(
            "Operator: Good morning and welcome to the call.", record["content"]
        )
        self.assertEqual(record["metadata"]["kind"], "text")
        self.assertEqual(record["metadata"]["speakers"], ["Operator", "Jane Doe"])

    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_feed_discovery_parses_rss_items(
        self, _dns, request
    ):
        request.side_effect = [
            _response(RSS_FEED, content_type="application/rss+xml"),
            _response(TRANSCRIPT_PAGE),
        ]
        config = _base_config()
        collector = IssuerTranscriptsCollector()
        records = collector.collect(config, "corr-2")

        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0]["published_at"],
            datetime(2026, 10, 29, 14, 0, 0, tzinfo=UTC),
        )

    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_press_release_is_not_mislabeled_as_transcript(self, _dns, request):
        html = b"""<!DOCTYPE html>
        <html><body>
          <a href="/ir/press-release-q3">Press Release: Q3 Financial Results</a>
        </body></html>"""
        request.side_effect = [_response(html)]
        config = _base_config()

        records = IssuerTranscriptsCollector().collect(config, "corr-pr")
        self.assertEqual(records, [])


class IssuerTranscriptBoundTests(unittest.TestCase):
    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_page_bytes_are_bounded(self, _dns, request):
        request.side_effect = [
            _response(HTML_PAGE),
            _response(TRANSCRIPT_PAGE),
            _response(TRANSCRIPT_PAGE),
        ]
        config = _base_config(max_page_bytes=1000)
        IssuerTranscriptsCollector().collect(config, "corr-bound")
        self.assertEqual(request.call_args_list[0].kwargs["max_bytes"], 64 * 1024)

    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_max_records_per_issuer_limits_items(self, _dns, request):
        request.side_effect = [
            _response(HTML_PAGE),
            _response(TRANSCRIPT_PAGE),
        ]
        config = _base_config(max_records_per_issuer=1)
        records = IssuerTranscriptsCollector().collect(config, "corr-limit")
        self.assertEqual(len(records), 1)


class IssuerTranscriptDedupeTests(unittest.TestCase):
    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_duplicate_items_dedupe_by_document_id(self, _dns, request):
        request.side_effect = [
            _response(HTML_PAGE),
            _response(TRANSCRIPT_PAGE),
            _response(TRANSCRIPT_PAGE),
        ]
        config = _base_config()
        records = IssuerTranscriptsCollector().collect(config, "corr-dedupe")
        doc_ids = [r["document_id"] for r in records]
        self.assertEqual(len(doc_ids), len(set(doc_ids)))


class IssuerTranscriptHealthTests(unittest.TestCase):
    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_health_check_ok(self, _dns, request):
        request.return_value = _response(status=200)
        config = _base_config()
        health = IssuerTranscriptsCollector().health_check(config)
        self.assertTrue(health["healthy"])

    def test_health_check_fails_without_issuers(self):
        config = {"collectors": {"issuer_transcripts": {"issuers": []}}}
        health = IssuerTranscriptsCollector().health_check(config)
        self.assertFalse(health["healthy"])
        self.assertEqual(health["state"], "setup_required")


if __name__ == "__main__":
    unittest.main()
