import socket
import sys
import tempfile
import time
import types
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import transcription
from collectors.base import CollectorNoData, CollectorSetupRequired
from collectors.issuer_transcripts import (
    IssuerTranscriptsCollector,
    _document_id,
    _is_audio_link,
    _parse_html_links,
)
from contracts.runtime_config import CollectorConfig, TranscriptionSettingsConfig
from transcription import (
    TranscriptionFailure,
    TranscriptionResult,
    TranscriptionSegment,
    TranscriptionTimeout,
    TranscriptionUnavailable,
)


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
                "max_audio_bytes": 250_000_000,
                "max_links_per_page": 50,
                "max_records_per_issuer": 25,
                "max_redirects": 5,
                "timeout_seconds": 30,
                "audio_timeout_seconds": 300,
                "transcription": {
                    "model": "small.en",
                    "device": "cpu",
                    "compute_type": "int8",
                },
                "issuers": [
                    {
                        "institution": "Example Corp",
                        "ticker": "EXMP",
                        "url": "https://example.test/ir/events",
                    }
                ],
            }
        }
    }
    section = config["collectors"]["issuer_transcripts"]
    section.update(overrides)
    return config


class RuntimeConfigMappingTests(unittest.TestCase):
    def test_collect_accepts_validated_runtime_config_mapping(self):
        section = CollectorConfig(
            issuers=[
                {
                    "institution": "Example Corp",
                    "ticker": "EXMP",
                    "url": "https://example.test/ir/events",
                }
            ],
            transcription={"model": "tiny.en"},
        )
        collector = IssuerTranscriptsCollector()
        with (
            patch(
                "collectors.issuer_transcripts.transcription_available",
                return_value=False,
            ),
            patch.object(
                collector,
                "_collect_issuer",
                return_value=[{"document_id": "typed", "metadata": {}}],
            ),
        ):
            records = collector.collect(
                {"collectors": {"issuer_transcripts": section}}, "typed-config"
            )

        self.assertEqual(records[0]["document_id"], "typed")

    def test_transcription_normalization_accepts_validated_runtime_mapping(self):
        raw = TranscriptionSettingsConfig(
            model="tiny.en",
            device="cuda",
            beam_size=3,
            timeout_seconds=90,
        )

        settings = transcription.normalize_transcription_config(raw)

        self.assertEqual(settings["model"], "tiny.en")
        self.assertEqual(settings["device"], "cuda")
        self.assertEqual(settings["beam_size"], 3)
        self.assertEqual(settings["timeout_seconds"], 90)


# Writable source_documents columns (migrations 016 + 017): acquired_at is a
# real point-in-time availability column, mirrored in each record's metadata.
SOURCE_DOCUMENTS_COLUMNS = {
    "document_id",
    "source",
    "institution",
    "document_type",
    "title",
    "published_at",
    "url",
    "content",
    "metadata",
    "acquired_at",
}


TRANSCRIPT_PAGE = b"""<html><head><title>Q3 2026 Earnings Call Transcript</title>
<meta property="article:published_time" content="2026-08-07T11:00:00Z"/></head>
<body><article>
<h1>Example Corp Q3 2026 Earnings Call Transcript</h1>
<p><b>Operator:</b> Good morning and welcome to the call.</p>
<p><b>John Smith:</b> Thank you. Revenue grew 12% this quarter.</p>
<p><b>Jane Doe:</b> Our margins expanded as expected.</p>
<p>Questions and Answers</p>
</article></body></html>"""

TEXT_EVENTS_PAGE = b"""<html><body>
<h1>Investor Events</h1>
<a href="/ir/q3-2026-transcript">Q3 2026 Earnings Call Transcript</a>
</body></html>"""

AUDIO_EVENTS_PAGE = b"""<html><body>
<h1>Investor Events</h1>
<a href="/audio/q2-2026-webcast.mp3">Q2 2026 Earnings Webcast Replay</a>
</body></html>"""

RSS_FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Example Corp IR</title>
<item>
<title>Q2 2026 Earnings Call Transcript</title>
<link>https://example.test/ir/q2-2026-transcript</link>
<pubDate>Fri, 07 Aug 2026 12:00:00 +0000</pubDate>
</item>
<item>
<title>Q2 2026 Earnings Webcast</title>
<enclosure url="https://example.test/audio/q2-2026.mp3" type="audio/mpeg" length="123456"/>
<pubDate>Fri, 07 Aug 2026 13:00:00 +0000</pubDate>
</item>
</channel></rss>"""

Q4_EVENTS_FEED = b"""{
  "GetEventListResult": [
    {
      "Title": "Q2 2026 Example Corp Earnings Conference Call",
      "WebCastLink": "/audio/q2-2026-full-call.mp3",
      "StartDate": "08/06/2026 14:00:00",
      "TimeZone": "PT"
    },
    {
      "Title": "Example Corp Annual Meeting",
      "WebCastLink": "/audio/annual-meeting.mp3",
      "StartDate": "06/01/2026 10:00:00",
      "TimeZone": "PT"
    },
    {
      "Title": "Q1 2026 Example Corp Financial Results",
      "WebCastLink": "https://events.example.test/attendee/123",
      "StartDate": "05/01/2026 14:00:00",
      "TimeZone": "PT"
    }
  ]
}"""


def _transcription_result():
    return TranscriptionResult(
        text="Good morning and welcome. Revenue grew strongly.",
        language="en",
        language_probability=0.97,
        duration_seconds=42.5,
        segments=(
            TranscriptionSegment(0.0, 3.2, "Good morning and welcome."),
            TranscriptionSegment(3.2, 6.0, "Revenue grew strongly."),
        ),
        model="small.en",
        device="cpu",
        compute_type="int8",
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
        elapsed_ms=900,
        transcribed_at="2026-08-15T12:00:00+00:00",
    )


class IssuerTranscriptDiscoveryTests(unittest.TestCase):
    @patch("collectors.issuer_transcripts.transcribe_audio")
    @patch("collectors.issuer_transcripts.transcription_available", return_value=True)
    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_html_page_yields_deterministic_text_transcript(
        self, _dns, request, _available, transcribe
    ):
        """A configured issuer page yields a deterministic text record with speakers."""
        # Each re-run consumes exactly the two intended mocked calls: the
        # issuer events page, then the transcript page itself.
        request.side_effect = [
            _response(TEXT_EVENTS_PAGE),
            _response(TRANSCRIPT_PAGE),
        ] * 2

        first = IssuerTranscriptsCollector().collect(_base_config(), "corr")
        second = IssuerTranscriptsCollector().collect(_base_config(), "corr")

        self.assertEqual(len(first), 1)
        record = first[0]
        self.assertEqual(record["document_type"], "earnings_transcript")
        self.assertEqual(record["institution"], "Example Corp")
        self.assertEqual(record["source"], "issuer_transcripts")
        self.assertEqual(record["url"], "https://example.test/ir/q3-2026-transcript")
        self.assertIn(
            "Operator: Good morning and welcome to the call.", record["content"]
        )
        self.assertIn(
            "John Smith: Thank you. Revenue grew 12% this quarter.",
            record["content"],
        )
        self.assertTrue(record["metadata"]["speaker_sections"])
        self.assertGreaterEqual(record["metadata"]["speakers"], 2)
        self.assertEqual(
            record["published_at"], datetime(2026, 8, 7, 11, 0, tzinfo=UTC)
        )
        # The timestamp is scraped from the page itself, not supplied by a
        # feed/enclosure, so it is provenance-marked as inferred.
        self.assertTrue(record["metadata"]["published_at_inferred"])
        self.assertEqual(record["metadata"]["kind"], "text")
        self.assertIn("content_hash", record["metadata"])
        for key in (
            "published_at",
            "source_observed_at",
            "fetched_at",
            "available_at",
            "acquired_at",
        ):
            self.assertIn(key, record["metadata"])
        # acquired_at is a real source_documents column (migration 017) and
        # mirrors the metadata acquisition time; every record matches the
        # table's columns.
        self.assertEqual(
            record["acquired_at"].isoformat(), record["metadata"]["acquired_at"]
        )
        self.assertEqual(set(record), SOURCE_DOCUMENTS_COLUMNS)
        self.assertEqual(request.call_args.kwargs["correlation_id"], "corr")
        # Deterministic identity and content across runs.
        self.assertEqual(second[0]["document_id"], record["document_id"])
        self.assertEqual(
            second[0]["metadata"]["content_hash"], record["metadata"]["content_hash"]
        )
        transcribe.assert_not_called()

    @patch("collectors.issuer_transcripts.transcribe_audio")
    @patch("collectors.issuer_transcripts.transcription_available", return_value=True)
    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_feed_discovery_parses_rss_items_and_enclosures(
        self, _dns, request, _available, transcribe
    ):
        """RSS items become text records; audio enclosures feed the transcriber."""
        transcribe.return_value = _transcription_result()
        request.side_effect = [
            _response(RSS_FEED, content_type="application/rss+xml"),
            _response(TRANSCRIPT_PAGE),
            _response(b"fake-audio", content_type="audio/mpeg"),
        ]

        records = IssuerTranscriptsCollector().collect(_base_config(), "corr")

        self.assertEqual(len(records), 2)
        text_record = next(r for r in records if r["metadata"]["kind"] == "text")
        audio_record = next(r for r in records if r["metadata"]["kind"] == "audio")
        self.assertEqual(
            text_record["published_at"], datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
        )
        self.assertEqual(
            text_record["url"], "https://example.test/ir/q2-2026-transcript"
        )
        self.assertEqual(audio_record["url"], "https://example.test/audio/q2-2026.mp3")
        self.assertEqual(
            audio_record["published_at"], datetime(2026, 8, 7, 13, 0, tzinfo=UTC)
        )
        # Feed/enclosure publication times are provider-supplied, never
        # inferred, for both text items and audio enclosures.
        self.assertFalse(text_record["metadata"]["published_at_inferred"])
        self.assertFalse(audio_record["metadata"]["published_at_inferred"])
        transcribe.assert_called_once()
        self.assertEqual(transcribe.call_args.args[0], b"fake-audio")

    @patch("collectors.issuer_transcripts.transcribe_audio")
    @patch("collectors.issuer_transcripts.transcription_available", return_value=True)
    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_q4_event_feed_ingests_only_direct_earnings_audio(
        self, _dns, request, _available, transcribe
    ):
        transcribe.return_value = _transcription_result()
        request.side_effect = [
            _response(Q4_EVENTS_FEED, content_type="application/json"),
            _response(b"official-call-audio", content_type="audio/mpeg"),
        ]
        config = _base_config(
            issuers=[
                {
                    "institution": "Example Corp",
                    "ticker": "EXMP",
                    "kind": "q4_events",
                    "url": "https://example.test/feed/Event.svc/GetEventList",
                }
            ]
        )

        records = IssuerTranscriptsCollector().collect(config, "corr")

        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0]["url"], "https://example.test/audio/q2-2026-full-call.mp3"
        )
        self.assertEqual(
            records[0]["published_at"], datetime(2026, 8, 6, 21, 0, tzinfo=UTC)
        )
        transcribe.assert_called_once()

    @patch("collectors.issuer_transcripts.transcribe_audio")
    @patch("collectors.issuer_transcripts.transcription_available", return_value=True)
    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_successful_audio_transcript_is_reused_from_durable_cache(
        self, _dns, request, _available, transcribe
    ):
        transcribe.return_value = _transcription_result()
        request.side_effect = [
            _response(Q4_EVENTS_FEED, content_type="application/json"),
            _response(b"official-call-audio", content_type="audio/mpeg"),
            _response(Q4_EVENTS_FEED, content_type="application/json"),
        ]
        config = _base_config(
            issuers=[
                {
                    "institution": "Example Corp",
                    "ticker": "EXMP",
                    "kind": "q4_events",
                    "url": "https://example.test/feed/Event.svc/GetEventList",
                }
            ]
        )
        with tempfile.TemporaryDirectory() as model_dir:
            config["collectors"]["issuer_transcripts"]["transcription"]["model_dir"] = (
                model_dir
            )
            first = IssuerTranscriptsCollector().collect(config, "first")
            second = IssuerTranscriptsCollector().collect(config, "second")

        self.assertFalse(first[0]["metadata"]["cache_hit"])
        self.assertTrue(second[0]["metadata"]["cache_hit"])
        self.assertEqual(second[0]["content"], first[0]["content"])
        self.assertEqual(request.call_count, 3)
        transcribe.assert_called_once()

    def test_webcast_landing_page_is_not_classified_as_audio(self):
        self.assertFalse(
            _is_audio_link(
                {
                    "url": "https://example.test/investor/latestearnings",
                    "title": "Press Release & Webcast",
                    "hint": None,
                }
            )
        )

    def test_html_discovery_ignores_navigation_and_non_transcript_earnings_links(self):
        page = b"""<html><body>
        <a href="#main">Skip to main content</a>
        <a href="http://social.example/share?url=/earnings/webcast">Share call</a>
        <a href="/earnings/fy-2026-q4/metrics">Metrics</a>
        <a href="/earnings/fy-2026-q4/press-release-webcast">Press Release &amp; Webcast</a>
        <a href="/events/fy-2026/earnings-fy-2026-q4">Webcast</a>
        </body></html>"""

        items = _parse_html_links(
            page,
            "https://example.test/earnings/fy-2026-q4/press-release-webcast",
            max_links=10,
        )

        self.assertEqual(
            [item["url"] for item in items],
            ["https://example.test/events/fy-2026/earnings-fy-2026-q4"],
        )

    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_press_release_is_not_mislabeled_as_transcript(self, _dns, request):
        request.side_effect = [
            _response(
                b'<html><body><a href="/earnings/q2">Q2 2026 Earnings Call</a></body></html>'
            ),
            _response(
                b"<html><main><h1>Q2 Earnings Release</h1>"
                b"<p>Revenue increased during the quarter.</p></main></html>"
            ),
        ]

        with self.assertRaises(CollectorNoData) as raised:
            IssuerTranscriptsCollector().collect(_base_config(), "corr")

        failures = raised.exception.metadata["failed_issuers"]
        self.assertIn("speaker-labelled", failures[0]["error"])

    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_page_without_transcript_links_is_valid_empty(self, _dns, request):
        """A healthy page with no transcript links is valid empty output."""
        request.return_value = _response(
            b"<html><body><h1>No events yet</h1><a href='/pr'>Press</a></body></html>"
        )
        with self.assertRaises(CollectorNoData) as raised:
            IssuerTranscriptsCollector().collect(_base_config(), "corr")
        self.assertIn("no transcript items", str(raised.exception))

    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_plain_text_transcript_preserves_speaker_lines(self, _dns, request):
        request.side_effect = [
            _response(TEXT_EVENTS_PAGE),
            _response(
                b"Operator: Hello everyone.\nJohn Smith: Hi, thanks for joining.",
                content_type="text/plain",
            ),
        ]
        records = IssuerTranscriptsCollector().collect(_base_config(), "corr")
        self.assertEqual(len(records), 1)
        self.assertIn("Operator: Hello everyone.", records[0]["content"])
        self.assertGreaterEqual(records[0]["metadata"]["speakers"], 2)

    @patch(
        "investment_service.extract_document_text",
        return_value=(
            "HD - Q1 Earnings Call\n"
            "Operator\n"
            "Greetings and welcome to the earnings call.\n"
            "Jane Smith - Example Corp - Chief Financial Officer\n"
            "Thank you. Revenue increased during the quarter."
        ),
    )
    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_pdf_transcript_uses_document_bound_and_preserves_speakers(
        self, _dns, request, extract
    ):
        request.side_effect = [
            _response(TEXT_EVENTS_PAGE),
            _response(b"%PDF-" + (b"x" * 199_995), content_type="application/pdf"),
        ]

        records = IssuerTranscriptsCollector().collect(
            _base_config(max_page_bytes=100_000, max_document_bytes=500_000),
            "corr",
        )

        self.assertEqual(len(records), 1)
        self.assertIn("Jane Smith - Example Corp", records[0]["content"])
        self.assertGreaterEqual(records[0]["metadata"]["speakers"], 2)
        self.assertEqual(extract.call_args.kwargs["max_bytes"], 500_000)

    def test_missing_configuration_raises_setup(self):
        with self.assertRaises(CollectorSetupRequired):
            IssuerTranscriptsCollector().collect({}, "corr")
        with self.assertRaises(CollectorSetupRequired):
            IssuerTranscriptsCollector().collect(
                {"collectors": {"issuer_transcripts": {"issuers": []}}}, "corr"
            )

    def test_contract_methods(self):
        collector = IssuerTranscriptsCollector()
        self.assertEqual(collector.get_target_table(), "source_documents")
        self.assertEqual(collector.get_conflict_columns(), ["document_id"])
        self.assertEqual(collector.get_schedule(_base_config()), "0 7 * * *")
        self.assertEqual(collector.get_schedule({"collectors": {}}), "0 7 * * *")


class IssuerTranscriptAudioTests(unittest.TestCase):
    @patch("collectors.issuer_transcripts.transcribe_audio")
    @patch("collectors.issuer_transcripts.transcription_available", return_value=True)
    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_audio_is_transcribed_locally_with_provenance(
        self, _dns, request, _available, transcribe
    ):
        """Public audio produces a transcript record with full provenance."""
        transcribe.return_value = _transcription_result()
        request.side_effect = [
            _response(AUDIO_EVENTS_PAGE),
            _response(b"fake-audio-bytes", content_type="audio/mpeg"),
        ]

        records = IssuerTranscriptsCollector().collect(_base_config(), "corr")

        self.assertEqual(len(records), 1)
        audio_record = records[0]
        self.assertEqual(
            audio_record["content"], "Good morning and welcome. Revenue grew strongly."
        )
        transcribe.assert_called_once()
        self.assertEqual(transcribe.call_args.args[0], b"fake-audio-bytes")
        self.assertEqual(transcribe.call_args.args[1]["model"], "small.en")
        self.assertEqual(
            transcribe.call_args.kwargs["source_url"],
            "https://example.test/audio/q2-2026-webcast.mp3",
        )
        metadata = audio_record["metadata"]
        self.assertEqual(
            metadata["audio_sha256"], transcription.audio_sha256(b"fake-audio-bytes")
        )
        self.assertEqual(metadata["content_hash"], metadata["audio_sha256"])
        self.assertEqual(metadata["audio_bytes"], 16)
        self.assertEqual(metadata["audio_content_type"], "audio/mpeg")
        self.assertEqual(metadata["transcription"]["model"], "small.en")
        self.assertEqual(metadata["transcription"]["language"], "en")
        self.assertEqual(metadata["transcription"]["segments"], 2)
        self.assertEqual(metadata["transcription"]["beam_size"], 5)
        self.assertEqual(metadata["transcription"]["vad_filter"], True)
        self.assertEqual(metadata["transcription"]["condition_on_previous_text"], False)
        self.assertEqual(
            metadata["transcription"]["transcribed_at"], "2026-08-15T12:00:00+00:00"
        )
        self.assertEqual(metadata["available_at"], "2026-08-15T12:00:00+00:00")
        self.assertNotIn("state", metadata)
        # The HTML events page supplied no publication time, so the recorded
        # timestamp is the acquisition-time fallback, marked inferred.
        self.assertTrue(metadata["published_at_inferred"])
        self.assertEqual(set(audio_record), SOURCE_DOCUMENTS_COLUMNS)

    @patch("collectors.issuer_transcripts.transcribe_audio")
    @patch("collectors.issuer_transcripts.transcription_available", return_value=False)
    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_audio_without_transcriber_yields_setup_state(
        self, _dns, request, _available, transcribe
    ):
        """When faster-whisper is missing, audio yields an explicit setup state."""
        request.side_effect = [
            _response(AUDIO_EVENTS_PAGE),
            _response(b"fake-audio-bytes", content_type="audio/mpeg"),
        ]

        records = IssuerTranscriptsCollector().collect(_base_config(), "corr")

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertIsNone(record["content"])
        self.assertEqual(record["metadata"]["state"], "setup_required")
        self.assertEqual(record["metadata"]["available"], False)
        self.assertIn("faster-whisper", record["metadata"]["error"])
        transcribe.assert_not_called()
        # The state record lives in its own namespace: it never collides with
        # (or clobbers) the identity a successful transcript would use.
        base_id = _document_id(
            institution="Example Corp",
            document_type="earnings_transcript",
            published_at=None,
            url="https://example.test/audio/q2-2026-webcast.mp3",
        )
        self.assertEqual(
            record["document_id"],
            _document_id(
                institution="Example Corp",
                document_type="earnings_transcript",
                published_at=None,
                url="https://example.test/audio/q2-2026-webcast.mp3",
                state="setup_required",
            ),
        )
        self.assertNotEqual(record["document_id"], base_id)
        self.assertEqual(set(record), SOURCE_DOCUMENTS_COLUMNS)

    @patch("collectors.issuer_transcripts.transcribe_audio")
    @patch("collectors.issuer_transcripts.transcription_available", return_value=True)
    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_audio_timeout_yields_timeout_state(
        self, _dns, request, _available, transcribe
    ):
        transcribe.side_effect = TranscriptionTimeout(
            "transcription exceeded 1800s deadline"
        )
        request.side_effect = [
            _response(AUDIO_EVENTS_PAGE),
            _response(b"fake-audio-bytes", content_type="audio/mpeg"),
        ]

        records = IssuerTranscriptsCollector().collect(_base_config(), "corr")

        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0]["content"])
        self.assertEqual(records[0]["metadata"]["state"], "timeout")
        self.assertIn("deadline", records[0]["metadata"]["error"])

    @patch("collectors.issuer_transcripts.transcribe_audio")
    @patch("collectors.issuer_transcripts.transcription_available", return_value=True)
    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_audio_failure_yields_failed_state_without_invented_content(
        self, _dns, request, _available, transcribe
    ):
        transcribe.side_effect = TranscriptionFailure("no speech was transcribed")
        request.side_effect = [
            _response(AUDIO_EVENTS_PAGE),
            _response(b"fake-audio-bytes", content_type="audio/mpeg"),
        ]

        records = IssuerTranscriptsCollector().collect(_base_config(), "corr")

        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0]["content"])
        self.assertEqual(records[0]["metadata"]["state"], "failed")
        self.assertIn("no speech", records[0]["metadata"]["error"])

    @patch("collectors.issuer_transcripts.transcription_available", return_value=False)
    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_failed_state_record_identity_is_deterministic(
        self, _dns, request, _available
    ):
        request.side_effect = [
            _response(AUDIO_EVENTS_PAGE),
            _response(b"fake-audio-bytes", content_type="audio/mpeg"),
        ] * 2
        first = IssuerTranscriptsCollector().collect(_base_config(), "corr")
        second = IssuerTranscriptsCollector().collect(_base_config(), "corr")
        self.assertEqual(first[0]["document_id"], second[0]["document_id"])


class IssuerTranscriptBoundTests(unittest.TestCase):
    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_page_bytes_are_bounded(self, _dns, request):
        """A page whose declared size exceeds the bound fails explicitly."""
        request.return_value = _response(
            TEXT_EVENTS_PAGE, headers={"content-length": "5000000"}
        )
        config = _base_config(max_page_bytes=100_000)

        with self.assertRaises(CollectorNoData) as raised:
            IssuerTranscriptsCollector().collect(config, "corr")

        self.assertEqual(len(raised.exception.metadata["failed_issuers"]), 1)
        self.assertIn(
            "declared content",
            raised.exception.metadata["failed_issuers"][0]["error"],
        )
        self.assertEqual(request.call_count, 1)

    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_downloaded_page_bytes_are_bounded(self, _dns, request):
        request.return_value = _response(b"x" * 200_000)
        config = _base_config(max_page_bytes=100_000)
        with self.assertRaises(CollectorNoData) as raised:
            IssuerTranscriptsCollector().collect(config, "corr")
        self.assertIn(
            "downloaded content",
            raised.exception.metadata["failed_issuers"][0]["error"],
        )

    @patch("collectors.issuer_transcripts.transcribe_audio")
    @patch("collectors.issuer_transcripts.transcription_available", return_value=True)
    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_audio_bytes_are_bounded(self, _dns, request, _available, transcribe):
        request.side_effect = [
            _response(AUDIO_EVENTS_PAGE),
            _response(
                b"audio",
                headers={"content-length": "99999999"},
                content_type="audio/mpeg",
            ),
        ]
        config = _base_config(max_audio_bytes=2_000_000)

        with self.assertRaises(CollectorNoData) as raised:
            IssuerTranscriptsCollector().collect(config, "corr")

        self.assertIn(
            "declared content",
            raised.exception.metadata["failed_issuers"][0]["error"],
        )
        transcribe.assert_not_called()

    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_redirect_hop_limit_is_enforced(self, _dns, request):
        request.return_value = _response(
            b"", status=302, is_redirect=True, headers={"location": "/next"}
        )
        config = _base_config(max_redirects=2)

        with self.assertRaises(CollectorNoData) as raised:
            IssuerTranscriptsCollector().collect(config, "corr")

        self.assertIn(
            "too many redirects",
            raised.exception.metadata["failed_issuers"][0]["error"],
        )
        self.assertEqual(request.call_count, 3)  # initial + 2 hops

    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_redirect_to_http_private_target_is_rejected(self, _dns, request):
        """A redirect downgrade/SSRF target fails closed before any fetch."""
        request.return_value = _response(
            b"",
            status=301,
            is_redirect=True,
            headers={"location": "http://192.168.1.1/x"},
        )

        with self.assertRaises(CollectorNoData) as raised:
            IssuerTranscriptsCollector().collect(_base_config(), "corr")

        self.assertIn(
            "redirect target rejected",
            raised.exception.metadata["failed_issuers"][0]["error"],
        )
        self.assertEqual(request.call_count, 1)

    @patch("collectors.issuer_transcripts.make_request")
    def test_private_configured_origin_is_rejected_without_network(self, request):
        config = _base_config()
        config["collectors"]["issuer_transcripts"]["issuers"][0]["url"] = (
            "http://127.0.0.1/ir/events"
        )

        with self.assertRaises(CollectorNoData) as raised:
            IssuerTranscriptsCollector().collect(config, "corr")

        self.assertIn(
            "invalid issuer_transcripts page URL",
            raised.exception.metadata["failed_issuers"][0]["error"],
        )
        request.assert_not_called()

    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_private_dns)
    def test_origin_resolving_to_private_address_is_rejected(self, _dns, request):
        with self.assertRaises(CollectorNoData) as raised:
            IssuerTranscriptsCollector().collect(_base_config(), "corr")
        self.assertIn(
            "non-public address",
            raised.exception.metadata["failed_issuers"][0]["error"],
        )
        request.assert_not_called()

    @patch("collectors.issuer_transcripts.transcribe_audio")
    @patch("collectors.issuer_transcripts.transcription_available", return_value=True)
    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_issuer_and_link_counts_are_bounded(
        self, _dns, request, _available, transcribe
    ):
        transcribe.return_value = _transcription_result()
        events = b"""<html><body>
        <a href="/ir/q3-2026-transcript">Q3 2026 Earnings Call Transcript</a>
        <a href="/ir/q2-2026-transcript">Q2 2026 Earnings Call Transcript</a>
        <a href="/audio/q2-2026-webcast.mp3">Q2 2026 Earnings Webcast Replay</a>
        </body></html>"""
        request.side_effect = [
            _response(events),
            _response(b"audio", content_type="audio/mpeg"),
            _response(TRANSCRIPT_PAGE),
        ]
        config = _base_config(
            max_issuers=1,
            max_links_per_page=2,
            issuers=[
                {"institution": "First", "url": "https://example.test/ir/events"},
                {"institution": "Second", "url": "https://other.test/ir"},
            ],
        )

        records = IssuerTranscriptsCollector().collect(config, "corr")

        # One issuer; the two highest-scoring candidates (audio webcast, then
        # the first transcript) are processed, everything else is untouched.
        self.assertEqual(len(records), 2)
        fetched = [call.args[1] for call in request.call_args_list]
        self.assertNotIn("https://other.test/ir", fetched)
        self.assertNotIn("https://example.test/ir/q2-2026-transcript", fetched)

    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_malformed_feed_fails_explicitly(self, _dns, request):
        request.return_value = _response(
            b"<rss><channel><item>", content_type="application/rss+xml"
        )

        with self.assertRaises(CollectorNoData) as raised:
            IssuerTranscriptsCollector().collect(_base_config(), "corr")

        self.assertIn(
            "malformed feed", raised.exception.metadata["failed_issuers"][0]["error"]
        )

    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_well_formed_empty_feed_is_valid_empty_output(self, _dns, request):
        request.return_value = _response(
            b"<rss version='2.0'><channel><title>IR</title></channel></rss>",
            content_type="application/rss+xml",
        )
        with self.assertRaises(CollectorNoData) as raised:
            IssuerTranscriptsCollector().collect(_base_config(), "corr")
        self.assertIn("no transcript items", str(raised.exception))
        self.assertEqual(raised.exception.metadata, {"state": "no_data"})

    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_transcript_page_without_content_fails_explicitly(self, _dns, request):
        request.side_effect = [
            _response(TEXT_EVENTS_PAGE),
            _response(b"<html><body><nav>menu</nav></body></html>"),
        ]
        with self.assertRaises(CollectorNoData) as raised:
            IssuerTranscriptsCollector().collect(_base_config(), "corr")
        self.assertIn(
            "no transcript text",
            raised.exception.metadata["failed_issuers"][0]["error"],
        )


class IssuerTranscriptDedupeTests(unittest.TestCase):
    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_duplicate_items_dedupe_by_document_id(self, _dns, request):
        """The same feed item twice yields one record."""
        duplicated = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Example Corp IR</title>
<item>
<title>Q2 2026 Earnings Call Transcript</title>
<link>https://example.test/ir/q2-2026-transcript</link>
<pubDate>Fri, 07 Aug 2026 12:00:00 +0000</pubDate>
</item>
<item>
<title>Q2 2026 Earnings Call Transcript</title>
<link>https://example.test/ir/q2-2026-transcript</link>
<pubDate>Fri, 07 Aug 2026 12:00:00 +0000</pubDate>
</item>
</channel></rss>"""
        request.side_effect = [
            _response(duplicated, content_type="application/rss+xml"),
            _response(TRANSCRIPT_PAGE),
            _response(TRANSCRIPT_PAGE),
        ]

        records = IssuerTranscriptsCollector().collect(_base_config(), "corr")

        self.assertEqual(len(records), 1)
        self.assertEqual(request.call_count, 3)  # feed + both fetches

    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_identical_content_different_urls_dedupe_by_content_hash(
        self, _dns, request
    ):
        page = b"""<html><body>
        <a href="/a/transcript">Q1 2026 Earnings Call Transcript</a>
        <a href="/b/transcript">Q1 2026 Earnings Call Transcript</a>
        </body></html>"""
        request.side_effect = [
            _response(page),
            _response(TRANSCRIPT_PAGE),
            _response(TRANSCRIPT_PAGE),
        ]

        records = IssuerTranscriptsCollector().collect(_base_config(), "corr")

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["url"], "https://example.test/a/transcript")

    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_same_link_listed_twice_on_html_page_is_discovered_once(
        self, _dns, request
    ):
        page = b"""<html><body>
        <a href="/ir/q3-2026-transcript">Q3 2026 Earnings Call Transcript</a>
        <a href="/ir/q3-2026-transcript#top">Q3 2026 Earnings Call Transcript</a>
        </body></html>"""
        request.side_effect = [
            _response(page),
            _response(TRANSCRIPT_PAGE),
        ]

        records = IssuerTranscriptsCollector().collect(_base_config(), "corr")

        self.assertEqual(len(records), 1)
        self.assertEqual(request.call_count, 2)


class IssuerTranscriptHealthTests(unittest.TestCase):
    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_health_check_ok(self, _dns, request):
        request.return_value = _response(TEXT_EVENTS_PAGE, status=200)
        result = IssuerTranscriptsCollector().health_check(_base_config())
        self.assertTrue(result["healthy"])
        self.assertEqual(result["state"], "success")
        self.assertIn("200", result["message"])
        self.assertGreaterEqual(result["latency_ms"], 0)

    @patch("collectors.issuer_transcripts.make_request")
    @patch("socket.getaddrinfo", side_effect=_public_dns)
    def test_health_check_reports_unreachable(self, _dns, request):
        request.side_effect = ConnectionError("refused")
        result = IssuerTranscriptsCollector().health_check(_base_config())
        self.assertFalse(result["healthy"])
        self.assertEqual(result["state"], "failed")
        self.assertIn("refused", result["message"])

    def test_health_check_setup_required_without_issuers(self):
        result = IssuerTranscriptsCollector().health_check(
            {"collectors": {"issuer_transcripts": {}}}
        )
        self.assertFalse(result["healthy"])
        self.assertEqual(result["state"], "setup_required")


class LocalTranscriptionTests(unittest.TestCase):
    def setUp(self):
        # Models are cached per settings key; fake-model tests must not leak
        # one test's model into the next, or the wrong fake gets reused.
        transcription._MODEL_CACHE.clear()

    def test_unavailable_without_faster_whisper(self):
        """Missing faster-whisper surfaces an explicit setup error, never content."""
        with patch(
            "transcription._import_faster_whisper",
            side_effect=ImportError("no module named faster_whisper"),
        ):
            self.assertFalse(transcription.transcription_available())
            with self.assertRaises(TranscriptionUnavailable):
                transcription.transcribe_audio(b"audio-bytes")

    @patch("transcription._import_faster_whisper")
    def test_empty_and_oversized_payloads_fail_explicitly(self, _fw):
        with self.assertRaises(TranscriptionFailure):
            transcription.transcribe_audio(b"")
        with self.assertRaises(TranscriptionFailure):
            transcription.transcribe_audio(b"x" * 100, {"max_audio_bytes": 10})

    def test_normalization_clamps_config(self):
        settings = transcription.normalize_transcription_config(
            {
                "beam_size": 100,
                "timeout_seconds": 0,
                "max_audio_seconds": 99_999,
                "model": "  ",
            }
        )
        self.assertEqual(settings["beam_size"], 10)
        self.assertEqual(
            settings["timeout_seconds"], transcription.DEFAULT_TIMEOUT_SECONDS
        )
        self.assertEqual(settings["max_audio_seconds"], 24 * 3600)
        self.assertEqual(settings["model"], transcription.DEFAULT_MODEL)
        defaults = transcription.normalize_transcription_config(None)
        self.assertEqual(defaults["model"], "small.en")
        self.assertEqual(defaults["device"], "cpu")
        self.assertEqual(defaults["compute_type"], "int8")
        self.assertEqual(defaults["language"], "en")
        self.assertEqual(defaults["beam_size"], 5)
        self.assertEqual(defaults["max_audio_seconds"], 7200)
        self.assertEqual(defaults["timeout_seconds"], 3600)
        self.assertEqual(
            defaults["model_dir"], "/var/lib/trading-data/news/models/whisper"
        )
        # Strict anti-hallucination switches: on unless explicitly overridden
        # with a bool; junk values fall back to the strict defaults.
        self.assertTrue(defaults["vad_filter"])
        self.assertFalse(defaults["condition_on_previous_text"])
        strict = transcription.normalize_transcription_config(
            {"vad_filter": False, "condition_on_previous_text": True}
        )
        self.assertFalse(strict["vad_filter"])
        self.assertTrue(strict["condition_on_previous_text"])
        junk = transcription.normalize_transcription_config({"vad_filter": "yes"})
        self.assertTrue(junk["vad_filter"])

    @patch("transcription._probe_duration_seconds", return_value=7200.0)
    @patch("transcription._import_faster_whisper")
    def test_duration_bound_rejects_overlong_audio(self, fw, probe):
        with self.assertRaises(TranscriptionFailure) as raised:
            transcription.transcribe_audio(b"x" * 100, {"max_audio_seconds": 3600})
        self.assertIn("exceeds", str(raised.exception))
        # The availability probe imports the package, but the model itself is
        # never constructed: the duration bound short-circuits before load.
        fw.WhisperModel.assert_not_called()

    def test_transcribes_locally_with_fake_model_no_network(self):
        """A fake whisper model exercises the local pipeline end to end."""

        class FakeSegment:
            def __init__(self, start, end, text):
                self.start = start
                self.end = end
                self.text = text

        class FakeInfo:
            language = "en"
            language_probability = 0.99
            duration = 12.5

        class FakeModel:
            captured_init_kwargs = None
            captured_transcribe_kwargs = None

            def __init__(self, *args, **kwargs):
                self.kwargs = kwargs
                FakeModel.captured_init_kwargs = kwargs

            def transcribe(self, path, **kwargs):
                FakeModel.captured_transcribe_kwargs = kwargs
                return iter(
                    [
                        FakeSegment(0.0, 3.0, "Hello world."),
                        FakeSegment(3.0, 6.0, "Second segment."),
                    ]
                ), FakeInfo()

        fake = types.SimpleNamespace(WhisperModel=FakeModel)
        with (
            patch("transcription._probe_duration_seconds", return_value=6.0),
            patch("transcription._import_faster_whisper", return_value=fake),
            patch("transcription._write_audio_tempfile") as write_temp,
            patch("transcription.Path") as path_class,
        ):
            temp_path = "/tmp/issuer_audio_test"
            write_temp.return_value = temp_path
            result = transcription.transcribe_audio(
                b"fake-audio",
                {
                    "model": "tiny",
                    "device": "cpu",
                    "compute_type": "int8",
                    "beam_size": 1,
                    "timeout_seconds": 60,
                },
                source_url="https://example.test/audio/call.mp3",
            )

        self.assertEqual(result.text, "Hello world.\nSecond segment.")
        self.assertEqual(result.language, "en")
        self.assertEqual(result.language_probability, 0.99)
        self.assertEqual(result.duration_seconds, 12.5)
        self.assertEqual(len(result.segments), 2)
        self.assertEqual(result.segments[0].start, 0.0)
        self.assertEqual(result.segments[1].text, "Second segment.")
        self.assertEqual(result.model, "tiny")
        self.assertEqual(result.device, "cpu")
        self.assertEqual(result.compute_type, "int8")
        self.assertEqual(result.beam_size, 1)
        self.assertTrue(result.vad_filter)
        self.assertFalse(result.condition_on_previous_text)
        self.assertGreaterEqual(result.elapsed_ms, 0)
        self.assertTrue(result.transcribed_at)
        write_temp.assert_called_once_with(
            b"fake-audio", "https://example.test/audio/call.mp3"
        )
        # The model is instantiated through the real constructor contract:
        # size name positionally, device/compute_type as keywords, weights
        # pinned to the production model_dir.
        self.assertEqual(
            FakeModel.captured_init_kwargs["download_root"],
            "/var/lib/trading-data/news/models/whisper",
        )
        self.assertEqual(FakeModel.captured_init_kwargs["device"], "cpu")
        self.assertEqual(FakeModel.captured_init_kwargs["compute_type"], "int8")
        # Strict settings reach faster-whisper at transcribe time.
        self.assertEqual(FakeModel.captured_transcribe_kwargs["language"], "en")
        self.assertEqual(FakeModel.captured_transcribe_kwargs["beam_size"], 1)
        self.assertEqual(FakeModel.captured_transcribe_kwargs["task"], "transcribe")
        self.assertEqual(FakeModel.captured_transcribe_kwargs["vad_filter"], True)
        self.assertEqual(
            FakeModel.captured_transcribe_kwargs["condition_on_previous_text"], False
        )
        # The temp file is always cleaned up.
        path_class.assert_any_call(temp_path)
        path_class.return_value.unlink.assert_called_once_with(missing_ok=True)

    def test_timeout_raises_without_partial_output(self):
        class SlowModel:
            def __init__(self, *args, **kwargs):
                pass

            def transcribe(self, path, **kwargs):
                time.sleep(2.0)
                return iter(()), types.SimpleNamespace()

        fake = types.SimpleNamespace(WhisperModel=SlowModel)
        with (
            patch("transcription._probe_duration_seconds", return_value=5.0),
            patch("transcription._import_faster_whisper", return_value=fake),
        ):
            with self.assertRaises(TranscriptionTimeout) as raised:
                transcription.transcribe_audio(
                    b"x" * 100,
                    {"timeout_seconds": 1, "max_audio_seconds": 3600},
                )
        self.assertIn("deadline", str(raised.exception))

    def test_segment_deadline_check_raises_timeout(self):
        """A segment iterator that outlives the deadline also raises timeout."""

        class SlowSegments:
            def __init__(self, *args, **kwargs):
                pass

            def transcribe(self, path, **kwargs):
                def gen():
                    for i in range(1000):
                        time.sleep(0.01)
                        yield types.SimpleNamespace(start=i, end=i + 1, text=f"seg {i}")

                return gen(), types.SimpleNamespace()

        fake = types.SimpleNamespace(WhisperModel=SlowSegments)
        with (
            patch("transcription._probe_duration_seconds", return_value=1000.0),
            patch("transcription._import_faster_whisper", return_value=fake),
        ):
            with self.assertRaises(TranscriptionTimeout):
                transcription.transcribe_audio(
                    b"x" * 100,
                    {"timeout_seconds": 1, "max_audio_seconds": 3600},
                )

    def test_no_speech_raises_failure(self):
        class EmptyModel:
            def __init__(self, *args, **kwargs):
                pass

            def transcribe(self, path, **kwargs):
                return iter(()), types.SimpleNamespace(language="en")

        fake = types.SimpleNamespace(WhisperModel=EmptyModel)
        with (
            patch("transcription._probe_duration_seconds", return_value=3.0),
            patch("transcription._import_faster_whisper", return_value=fake),
        ):
            with self.assertRaises(TranscriptionFailure) as raised:
                transcription.transcribe_audio(b"x" * 100, {"timeout_seconds": 60})
        self.assertIn("no speech", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
