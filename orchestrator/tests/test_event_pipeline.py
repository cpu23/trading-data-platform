import sys
import unittest
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector_execution import _run_collector_impl
from events import build_market_event
from events.publisher import (
    PublicationResult,
    publish_collector_records_atomic,
    publish_option_chain_records,
    publish_record,
    publish_records,
)
from events.repository import EventInsertResult, OutboxClaim
from events.worker import OutboxWorker


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.observed_at = datetime(2026, 8, 5, 12, tzinfo=UTC)
        self.record = {
            "series_id": "GDP",
            "observed_at": self.observed_at,
            "value": 3.2,
            "released_at": None,
            "revision_at": None,
            "metadata": {"units": "percent"},
        }

    @patch("events.publisher.insert_event")
    @patch("events.publisher.upsert_raw")
    @patch("events.publisher.find_latest_event", return_value=None)
    def test_fred_raw_write_and_outbox_event_share_the_caller_session(
        self, _find_latest, upsert_raw, insert_event
    ):
        session = MagicMock()
        insert_event.side_effect = lambda active, event, **kwargs: EventInsertResult(
            event=event, inserted=True, outbox_inserted=True
        )

        result = publish_record(session, self.record, source="fred")

        self.assertTrue(result.inserted)
        upsert_raw.assert_called_once()
        insert_event.assert_called_once()
        self.assertIs(upsert_raw.call_args.args[0], session)
        self.assertIs(insert_event.call_args.args[0], session)
        event = insert_event.call_args.args[1]
        self.assertEqual(event.event_type, "macro_release")
        self.assertEqual(event.source_event_id, "GDP:2026-08-05T12:00:00+00:00")

    @patch("events.publisher.insert_event")
    @patch("events.publisher.upsert_raw")
    @patch("events.publisher.find_latest_event")
    def test_changed_fred_observation_creates_revision_link(
        self, find_latest, _upsert_raw, insert_event
    ):
        previous = build_market_event(
            "macro_release",
            "fred",
            self.observed_at,
            {
                "series_id": "GDP",
                "observed_at": self.observed_at,
                "value": 3.1,
                "released_at": None,
                "revision_at": None,
                "metadata": {"units": "percent"},
            },
            source_event_id="GDP:2026-08-05T12:00:00+00:00",
        )
        find_latest.return_value = previous
        insert_event.side_effect = lambda active, event, **kwargs: EventInsertResult(
            event=event, inserted=True, outbox_inserted=True
        )

        publish_record(MagicMock(), self.record, source="fred")

        event = insert_event.call_args.args[1]
        self.assertEqual(event.event_type, "macro_revision")
        self.assertEqual(event.revision_of_event_id, previous.event_id)
        self.assertNotEqual(event.content_hash, previous.content_hash)

    @patch("events.publisher.insert_event")
    @patch("events.publisher.upsert_raw")
    @patch("events.publisher.find_latest_event")
    def test_exact_fred_retry_updates_raw_but_does_not_enqueue_again(
        self, find_latest, upsert_raw, insert_event
    ):
        previous = build_market_event(
            "macro_release",
            "fred",
            self.observed_at,
            {
                "series_id": "GDP",
                "observed_at": self.observed_at,
                "value": 3.2,
                "released_at": None,
                "revision_at": None,
                "metadata": {"units": "percent"},
            },
            source_event_id="GDP:2026-08-05T12:00:00+00:00",
        )
        find_latest.return_value = previous

        result = publish_record(MagicMock(), self.record, source="fred")

        self.assertFalse(result.inserted)
        upsert_raw.assert_called_once()
        insert_event.assert_not_called()

    def test_atomic_adapter_rejects_unsupported_source_table_pair_before_db_use(self):
        with self.assertRaisesRegex(ValueError, "unsupported source/table"):
            publish_collector_records_atomic(
                source_id="fred",
                table_name="market_data",
                records=[self.record],
                conflict_columns=["series_id", "observed_at"],
            )


class CollectorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.collector = MagicMock()
        self.collector.collect.return_value = [
            {
                "series_id": "GDP",
                "observed_at": datetime(2026, 8, 5, tzinfo=UTC),
                "value": 3.2,
                "source": "fred",
            }
        ]
        self.collector.get_target_table.return_value = "macro_series"
        self.collector.get_conflict_columns.return_value = ["series_id", "observed_at"]

    @patch("collector_execution._record_source_freshness")
    @patch("collector_execution._write_collection_log")
    @patch("collector_execution.publish_collector_records_atomic")
    @patch("collector_execution.upsert_records")
    @patch("collector_execution.get_collector")
    def test_enabled_source_uses_atomic_event_publisher(
        self,
        get_collector,
        upsert_records,
        publish_atomic,
        _write_log,
        record_freshness,
    ):
        get_collector.return_value = self.collector
        publish_atomic.return_value = PublicationResult(1, 1, 1, 0, 1)
        config = {
            "event_pipeline": {"enabled": True, "sources": ["fred"]},
            "collectors": {"fred": {}},
        }

        result = _run_collector_impl(
            "fred", config=config, correlation_id=str(uuid4()), manage_lifecycle=False
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["metrics"]["events_inserted"], 1)
        publish_atomic.assert_called_once()
        upsert_records.assert_not_called()
        record_freshness.assert_called_once()

    @patch("collector_execution._record_source_freshness")
    @patch("collector_execution._write_collection_log")
    @patch("collector_execution.publish_collector_records_atomic")
    @patch("collector_execution.upsert_records")
    @patch("collector_execution.get_collector")
    def test_disabled_pipeline_preserves_legacy_raw_writer(
        self, get_collector, upsert_records, publish_atomic, _write_log, _freshness
    ):
        get_collector.return_value = self.collector
        upsert_records.return_value = MagicMock(written=1, status="success")

        result = _run_collector_impl(
            "fred",
            config={"event_pipeline": {"enabled": False}},
            correlation_id=str(uuid4()),
            manage_lifecycle=False,
        )

        self.assertEqual(result["records_written"], 1)
        upsert_records.assert_called_once()
        publish_atomic.assert_not_called()


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.worker = OutboxWorker()
        self.worker._config = {}
        self.claim = OutboxClaim(
            id=7,
            event_id=uuid4(),
            topic="market_event",
            attempt_count=1,
            claimed_by="worker-a",
            event=MagicMock(),
        )
        self.session = MagicMock()

    def _session_context(self, _config):
        @contextmanager
        def context():
            yield self.session

        return context()

    @patch("events.worker.complete_outbox", return_value=True)
    @patch("events.worker.route_event")
    def test_successful_handler_completion_marks_the_claim_done(
        self, route_event, complete_outbox
    ):
        with patch("events.worker.get_session", side_effect=self._session_context):
            self.worker._process(self.claim, 3, 1.0, 60.0)

        route_event.assert_called_once_with(
            self.session, self.claim.event, topic="market_event"
        )
        complete_outbox.assert_called_once_with(self.session, 7, worker_id="worker-a")
        self.assertEqual(self.worker.state["completed"], 1)

    @patch("events.worker.claim_outbox")
    def test_backlog_drains_without_idle_poll_delay(self, claim_outbox):
        claim_outbox.side_effect = [[self.claim], []]
        stop = MagicMock()
        stop.is_set.side_effect = [False, False, True]
        self.worker._stop = stop
        self.worker._config = {"event_pipeline": {"poll_interval_seconds": 9.0}}

        with (
            patch("events.worker.get_session", side_effect=self._session_context),
            patch.object(self.worker, "_process") as process,
        ):
            self.worker._run()

        self.assertEqual(claim_outbox.call_count, 2)
        process.assert_called_once_with(self.claim, 5, 1.0, 60.0)
        stop.wait.assert_called_once_with(9.0)

    @patch("events.worker.retry_outbox", return_value=False)
    @patch("events.worker.complete_outbox", return_value=False)
    @patch("events.worker.route_event")
    def test_lost_lease_rolls_back_handler_transaction(
        self, _route_event, _complete_outbox, _retry_outbox
    ):
        with patch("events.worker.get_session", side_effect=self._session_context):
            self.worker._process(self.claim, 3, 1.0, 60.0)

        self.assertEqual(self.worker.state["completed"], 0)
        self.assertEqual(self.worker.state["last_error"], "LeaseLostError")

    def test_stop_retains_a_worker_thread_that_has_not_exited(self):
        thread = MagicMock()
        thread.is_alive.return_value = True
        self.worker._thread = thread
        self.worker.state["running"] = True

        self.worker.stop()

        self.assertIs(self.worker._thread, thread)
        self.assertTrue(self.worker.state["running"])

    @patch("events.worker.random.uniform", return_value=1.0)
    @patch("events.worker.retry_outbox", return_value=True)
    @patch("events.worker.route_event", side_effect=RuntimeError("secret payload"))
    def test_retryable_handler_failure_releases_claim_with_backoff(
        self, _route_event, retry_outbox, _uniform
    ):
        with patch("events.worker.get_session", side_effect=self._session_context):
            self.worker._process(self.claim, 3, 2.0, 60.0)

        self.assertEqual(self.worker.state["retried"], 1)
        self.assertEqual(self.worker.state["last_error"], "RuntimeError")
        self.assertEqual(
            retry_outbox.call_args.kwargs["error"].args[0], "secret payload"
        )

    @patch("events.worker.terminal_fail_outbox", return_value=True)
    @patch("events.worker.route_event", side_effect=RuntimeError("terminal"))
    def test_last_attempt_preserves_terminal_failure(self, _route_event, terminal_fail):
        claim = OutboxClaim(
            id=9,
            event_id=uuid4(),
            topic="market_event",
            attempt_count=3,
            claimed_by="worker-a",
            event=MagicMock(),
        )
        with patch("events.worker.get_session", side_effect=self._session_context):
            self.worker._process(claim, 3, 1.0, 60.0)

        terminal_fail.assert_called_once()
        self.assertEqual(self.worker.state["failed"], 1)


def _insert_capture(inserted=True, outbox_inserted=True):
    def side_effect(active, event, **kwargs):
        return EventInsertResult(
            event=event, inserted=inserted, outbox_inserted=outbox_inserted
        )

    return side_effect


class IssuerDocumentPublicationTests(unittest.TestCase):
    def _document_record(self, **overrides):
        record = {
            "document_id": "issuer_news:abc123",
            "source": "issuer_news",
            "institution": "Acme Corp",
            "document_type": "issuer_update",
            "title": "Acme Releases Q3 Results",
            "published_at": datetime(2026, 8, 15, 6, 0, tzinfo=UTC),
            "url": "https://ir.example.test/news/q3",
            "content": "bounded body",
            "acquired_at": datetime(2026, 8, 15, 6, 0, 30, tzinfo=UTC),
            "metadata": {"primary": True},
        }
        record.update(overrides)
        return record

    @patch("events.publisher.insert_event")
    @patch("events.publisher.upsert_raw")
    @patch("events.publisher.find_latest_event", return_value=None)
    def test_issuer_news_update_maps_to_headline_published(
        self, _find_latest, upsert_raw, insert_event
    ):
        insert_event.side_effect = _insert_capture()
        record = self._document_record()

        result = publish_record(MagicMock(), record, source="issuer_news")

        self.assertTrue(result.inserted)
        upsert_raw.assert_called_once()
        self.assertEqual(upsert_raw.call_args.args[1], "source_documents")
        self.assertEqual(upsert_raw.call_args.args[3], ["document_id"])
        event = insert_event.call_args.args[1]
        self.assertEqual(event.event_type, "headline_published")
        self.assertEqual(event.source, "issuer_news")
        self.assertEqual(event.source_event_id, "issuer_news:abc123")
        self.assertEqual(event.observed_at, datetime(2026, 8, 15, 6, 0, tzinfo=UTC))
        self.assertEqual(event.entities[0].entity_type, "company")
        self.assertEqual(event.entities[0].canonical_id, "Acme Corp")
        self.assertEqual(event.markets, [])
        self.assertEqual(event.metadata, {"raw_table": "source_documents"})

    @patch("events.publisher.insert_event")
    @patch("events.publisher.upsert_raw")
    @patch("events.publisher.find_latest_event", return_value=None)
    def test_company_expectations_map_to_announced_calendar_event(
        self, _find_latest, _upsert_raw, insert_event
    ):
        insert_event.side_effect = _insert_capture()
        record = self._document_record(
            document_id="expectations:aapl:2026-08-16",
            source="company_expectations",
            document_type="consensus_snapshot",
            metadata={
                "ticker": "AAPL",
                "next_earnings": {"reportDate": "2026-08-20"},
            },
        )
        publish_record(MagicMock(), record, source="company_expectations")
        event = insert_event.call_args.args[1]
        self.assertEqual(event.event_type, "calendar_event_changed")

    @patch("events.publisher.insert_event")
    @patch("events.publisher.upsert_raw")
    @patch("events.publisher.find_latest_event", return_value=None)
    def test_issuer_news_regulatory_document_maps_to_regulatory_filing_published(
        self, _find_latest, _upsert_raw, insert_event
    ):
        insert_event.side_effect = _insert_capture()
        record = self._document_record(
            document_id="issuer_news:sec8k",
            document_type="regulatory_update",
            institution="SEC",
        )

        publish_record(MagicMock(), record, source="issuer_news")

        event = insert_event.call_args.args[1]
        self.assertEqual(event.event_type, "regulatory_filing_published")

    @patch("events.publisher.insert_event")
    @patch("events.publisher.upsert_raw")
    @patch("events.publisher.find_latest_event", return_value=None)
    def test_issuer_transcript_maps_to_transcript_published_with_security_ref(
        self, _find_latest, _upsert_raw, insert_event
    ):
        insert_event.side_effect = _insert_capture()
        record = {
            "document_id": "issuer_transcripts:acme-earnings",
            "source": "issuer_transcripts",
            "institution": "Acme Corp",
            "document_type": "earnings_transcript",
            "title": "Acme Q3 2026 Earnings Call",
            "published_at": datetime(2026, 8, 15, 7, 0, tzinfo=UTC),
            "url": "https://example.test/ir/q3-transcript",
            "content": "Operator: ...",
            "metadata": {"ticker": "ACME"},
        }

        publish_record(MagicMock(), record, source="issuer_transcripts")

        event = insert_event.call_args.args[1]
        self.assertEqual(event.event_type, "transcript_published")
        self.assertEqual(event.source_event_id, "issuer_transcripts:acme-earnings")
        self.assertEqual(event.entities[0].entity_type, "company")
        self.assertEqual(event.entities[1].entity_type, "instrument")
        self.assertEqual(event.entities[1].canonical_id, "ACME")
        self.assertEqual(event.markets[0].canonical_id, "equity:ACME")
        self.assertEqual(event.markets[0].asset_class, "equity")

    @patch("events.publisher.insert_event")
    @patch("events.publisher.upsert_raw")
    @patch("events.publisher.find_latest_event")
    def test_changed_issuer_document_creates_revision_link(
        self, find_latest, _upsert_raw, insert_event
    ):
        insert_event.side_effect = _insert_capture()
        record = self._document_record()
        previous = build_market_event(
            "headline_published",
            "issuer_news",
            record["published_at"],
            {
                "document_id": record["document_id"],
                "source": "issuer_news",
                "institution": record["institution"],
                "document_type": record["document_type"],
                "title": "Old Title",
                "url": record["url"],
                "published_at": record["published_at"],
            },
            source_event_id=record["document_id"],
        )
        find_latest.return_value = previous

        publish_record(MagicMock(), record, source="issuer_news")

        event = insert_event.call_args.args[1]
        self.assertEqual(event.event_type, "headline_published")
        self.assertEqual(event.revision_of_event_id, previous.event_id)

    @patch("events.publisher.insert_event")
    @patch("events.publisher.upsert_raw")
    @patch("events.publisher.find_latest_event", return_value=None)
    def test_identical_issuer_document_retry_is_deduplicated(
        self, find_latest, upsert_raw, insert_event
    ):
        record = self._document_record()
        previous = build_market_event(
            "headline_published",
            "issuer_news",
            record["published_at"],
            {
                "document_id": record["document_id"],
                "source": "issuer_news",
                "institution": record["institution"],
                "document_type": record["document_type"],
                "title": record["title"],
                "url": record["url"],
                "published_at": record["published_at"],
            },
            source_event_id=record["document_id"],
        )
        find_latest.return_value = previous

        result = publish_record(MagicMock(), record, source="issuer_news")

        self.assertFalse(result.inserted)
        upsert_raw.assert_called_once()
        insert_event.assert_not_called()

    @patch("events.publisher.insert_event")
    @patch("events.publisher.upsert_raw")
    @patch("events.publisher.find_latest_event")
    def test_inferred_publication_retry_keeps_first_seen_timestamp(
        self, find_latest, upsert_raw, insert_event
    ):
        first_seen = datetime(2026, 8, 15, 6, 0, tzinfo=UTC)
        retry_seen = datetime(2026, 8, 15, 7, 0, tzinfo=UTC)
        record = self._document_record(
            source="issuer_transcripts",
            document_type="earnings_transcript",
            published_at=retry_seen,
            metadata={"ticker": "ACME", "published_at_inferred": True},
        )
        previous = build_market_event(
            "transcript_published",
            "issuer_transcripts",
            first_seen,
            {
                "document_id": record["document_id"],
                "source": "issuer_transcripts",
                "institution": record["institution"],
                "document_type": record["document_type"],
                "title": record["title"],
                "url": record["url"],
                "published_at": first_seen,
            },
            source_event_id=record["document_id"],
        )
        find_latest.return_value = previous

        result = publish_record(MagicMock(), record, source="issuer_transcripts")

        self.assertFalse(result.inserted)
        persisted = upsert_raw.call_args.args[2]
        self.assertEqual(persisted["published_at"], first_seen)
        self.assertEqual(persisted["metadata"]["published_at"], first_seen.isoformat())
        insert_event.assert_not_called()

    @patch("events.publisher.upsert_raw")
    def test_unavailable_transcript_diagnostic_persists_raw_without_event(
        self, upsert_raw
    ):
        record = {
            "document_id": "issuer_transcripts:acme-audio",
            "source": "issuer_transcripts",
            "institution": "Acme Corp",
            "document_type": "earnings_transcript",
            "title": "Acme Q3 2026 Earnings Call",
            "published_at": datetime(2026, 8, 15, 7, 0, tzinfo=UTC),
            "url": "https://example.test/ir/q3-audio",
            "content": None,
            "metadata": {
                "kind": "audio",
                "state": "setup_required",
                "available": False,
            },
        }
        with patch("events.publisher.insert_event") as insert_event:
            with patch("events.publisher.find_latest_event") as find_latest:
                result = publish_record(
                    MagicMock(), record, source="issuer_transcripts"
                )

        # The raw document row is still persisted atomically...
        self.assertIsNone(result.event)
        self.assertFalse(result.inserted)
        self.assertFalse(result.outbox_inserted)
        upsert_raw.assert_called_once()
        self.assertEqual(upsert_raw.call_args.args[1], "source_documents")
        self.assertEqual(upsert_raw.call_args.args[3], ["document_id"])
        insert_event.assert_not_called()
        find_latest.assert_not_called()

        # ...and batch accounting counts it as written, not as an event.
        with patch("events.publisher.insert_event") as insert_event:
            with patch("events.publisher.find_latest_event") as find_latest:
                batch = publish_records(
                    "issuer_transcripts", [record], session=MagicMock()
                )
        self.assertEqual(batch.raw_written, 1)
        self.assertEqual(batch.events_inserted, 0)
        self.assertEqual(batch.events_deduplicated, 0)
        self.assertEqual(batch.outbox_inserted, 0)
        insert_event.assert_not_called()
        find_latest.assert_not_called()


class EquityBarPublicationTests(unittest.TestCase):
    def _bar_record(self, **overrides):
        record = {
            "symbol": "AAPL",
            "timeframe": "1d",
            "timestamp": datetime(2026, 8, 14, tzinfo=UTC),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 52000000.0,
            "source": "public_equities",
            "metadata": {
                "adjusted": False,
                "interval": "1d",
                "range": "1mo",
                "provider_symbol": "AAPL",
                "currency": "USD",
                "exchange_name": "NMS",
                "source_timestamp": "2026-08-14T20:00:00+00:00",
                "available_at": "2026-08-15T07:00:00+00:00",
            },
        }
        record.update(overrides)
        return record

    @patch("events.publisher.insert_event")
    @patch("events.publisher.upsert_raw")
    @patch("events.publisher.find_latest_event", return_value=None)
    def test_daily_bar_maps_to_price_bar_closed(
        self, _find_latest, upsert_raw, insert_event
    ):
        insert_event.side_effect = _insert_capture()
        record = self._bar_record()

        result = publish_record(MagicMock(), record, source="public_equities")

        self.assertTrue(result.inserted)
        self.assertEqual(upsert_raw.call_args.args[1], "market_data")
        self.assertEqual(
            upsert_raw.call_args.args[3], ["symbol", "timeframe", "timestamp"]
        )
        event = insert_event.call_args.args[1]
        self.assertEqual(event.event_type, "price_bar_closed")
        self.assertEqual(event.source_event_id, "AAPL:1d:2026-08-14T00:00:00+00:00")
        self.assertEqual(event.markets[0].canonical_id, "equity:AAPL")
        self.assertEqual(event.payload["close"], 100.5)
        # Acquisition time stays in the raw row, never in the stable event.
        self.assertNotIn("available_at", event.payload["metadata"])

    @patch("events.publisher.insert_event")
    @patch("events.publisher.upsert_raw")
    @patch("events.publisher.find_latest_event")
    def test_changed_bar_at_same_identity_creates_revision_link(
        self, find_latest, _upsert_raw, insert_event
    ):
        insert_event.side_effect = _insert_capture()
        record = self._bar_record()
        previous = build_market_event(
            "price_bar_closed",
            "public_equities",
            record["timestamp"],
            {
                "symbol": "AAPL",
                "timeframe": "1d",
                "timestamp": record["timestamp"],
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 99.5,
                "volume": 52000000.0,
                "metadata": {
                    "adjusted": False,
                    "interval": "1d",
                    "range": "1mo",
                    "provider_symbol": "AAPL",
                    "currency": "USD",
                    "exchange_name": "NMS",
                    "source_timestamp": "2026-08-14T20:00:00+00:00",
                },
            },
            source_event_id="AAPL:1d:2026-08-14T00:00:00+00:00",
        )
        find_latest.return_value = previous

        publish_record(MagicMock(), record, source="public_equities")

        event = insert_event.call_args.args[1]
        self.assertEqual(event.event_type, "price_bar_closed")
        self.assertEqual(event.revision_of_event_id, previous.event_id)

    @patch("events.publisher.insert_event")
    @patch("events.publisher.upsert_raw")
    @patch("events.publisher.find_latest_event", return_value=None)
    def test_insert_only_bar_never_revises_the_stored_row(
        self, _find_latest, upsert_raw, insert_event
    ):
        # insert_only is the public_equities policy: a re-collected bar
        # with the same identity must be a DO NOTHING no-op, not an upsert
        # that would bump updated_at past accepted cutoffs.
        insert_event.side_effect = _insert_capture()
        record = self._bar_record()

        result = publish_record(
            MagicMock(), record, source="public_equities", insert_only=True
        )

        self.assertTrue(result.inserted)
        upsert_raw.assert_not_called()
        event = insert_event.call_args.args[1]
        self.assertEqual(event.event_type, "price_bar_closed")
        self.assertEqual(event.source_event_id, "AAPL:1d:2026-08-14T00:00:00+00:00")

    @patch("events.publisher.insert_event")
    @patch("events.publisher.upsert_raw")
    @patch("events.publisher.find_latest_event")
    def test_insert_only_conflict_noops_event_even_with_changed_payload(
        self, find_latest, upsert_raw, insert_event
    ):
        # A re-collected bar whose identity already exists is a raw no-op.
        # A changed incoming payload must not emit a revision event either:
        # the event would disagree with the first-frozen raw row.
        insert_event.side_effect = _insert_capture()
        record = self._bar_record(close=101.5)  # changed vs the stored bar
        previous = build_market_event(
            "price_bar_closed",
            "public_equities",
            record["timestamp"],
            {
                "symbol": "AAPL",
                "timeframe": "1d",
                "timestamp": record["timestamp"],
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 52000000.0,
                "metadata": {
                    "adjusted": False,
                    "interval": "1d",
                    "range": "1mo",
                    "provider_symbol": "AAPL",
                    "currency": "USD",
                    "exchange_name": "NMS",
                    "source_timestamp": "2026-08-14T20:00:00+00:00",
                },
            },
            source_event_id="AAPL:1d:2026-08-14T00:00:00+00:00",
        )
        find_latest.return_value = previous
        session = MagicMock()
        session.execute.return_value.rowcount = 0  # identity already exists

        result = publish_record(
            session, record, source="public_equities", insert_only=True
        )

        self.assertFalse(result.inserted)
        self.assertFalse(result.outbox_inserted)
        self.assertIsNone(result.event)
        upsert_raw.assert_not_called()
        insert_event.assert_not_called()


class PositioningPublicationTests(unittest.TestCase):
    def _positioning_record(self, **overrides):
        record = {
            "source": "sec_form4",
            "market_id": "AAPL",
            "report_date": date(2026, 8, 5),
            "category": "insider_transactions",
            "long_positions": 12000,
            "short_positions": 3000,
            "net_position": 9000,
            "open_interest": None,
            "net_pct_open_interest": None,
            "metadata": {
                "positioning_kind": "insider_activity",
                "assets": [],
                "semantics": "SEC Form 4 open-market insider transactions",
                "acquired_at": "2026-08-15T12:30:00+00:00",
            },
        }
        record.update(overrides)
        return record

    @patch("events.publisher.insert_event")
    @patch("events.publisher.upsert_raw")
    @patch("events.publisher.find_latest_event", return_value=None)
    def test_sec_form4_row_maps_to_positioning_report_published(
        self, _find_latest, upsert_raw, insert_event
    ):
        insert_event.side_effect = _insert_capture()
        record = self._positioning_record()

        result = publish_record(MagicMock(), record, source="sec_form4")

        self.assertTrue(result.inserted)
        self.assertEqual(upsert_raw.call_args.args[1], "positioning_reports")
        self.assertEqual(
            upsert_raw.call_args.args[3],
            ["source", "market_id", "report_date", "category"],
        )
        event = insert_event.call_args.args[1]
        self.assertEqual(event.event_type, "positioning_report_published")
        self.assertEqual(event.source_event_id, "AAPL:2026-08-05:insider_transactions")
        self.assertEqual(event.observed_at, datetime(2026, 8, 15, 12, 30, tzinfo=UTC))
        self.assertEqual(event.markets[0].canonical_id, "equity:AAPL")
        self.assertEqual(event.payload["positioning_kind"], "insider_activity")

    @patch("events.publisher.insert_event")
    @patch("events.publisher.upsert_raw")
    @patch("events.publisher.find_latest_event", return_value=None)
    def test_finra_short_volume_row_maps_to_positioning_report_published(
        self, _find_latest, _upsert_raw, insert_event
    ):
        insert_event.side_effect = _insert_capture()
        record = self._positioning_record(
            source="finra_short_volume",
            category="short_volume",
            long_positions=4000000,
            short_positions=800000,
            net_position=3200000,
            metadata={
                "positioning_kind": "short_volume",
                "assets": [],
                "semantics": "FINRA Reg SHO daily short sale volume",
                "acquired_at": "2026-08-15T20:00:00+00:00",
            },
        )

        publish_record(MagicMock(), record, source="finra_short_volume")

        event = insert_event.call_args.args[1]
        self.assertEqual(event.event_type, "positioning_report_published")
        self.assertEqual(event.source_event_id, "AAPL:2026-08-05:short_volume")
        self.assertEqual(event.observed_at, datetime(2026, 8, 15, 20, 0, tzinfo=UTC))
        self.assertEqual(event.payload["positioning_kind"], "short_volume")

    @patch("events.publisher.insert_event")
    @patch("events.publisher.upsert_raw")
    @patch("events.publisher.find_latest_event", return_value=None)
    def test_cftc_assets_map_to_cross_asset_positioning_event(
        self, _find_latest, _upsert_raw, insert_event
    ):
        insert_event.side_effect = _insert_capture()
        record = self._positioning_record(
            source="cftc",
            market_id="088691",
            category="managed_money",
            metadata={
                "positioning_kind": "futures_positioning",
                "assets": ["EURUSD", "XAUUSD"],
                "semantics": "CFTC futures-only positions; not short interest",
            },
        )

        publish_record(MagicMock(), record, source="cftc")

        event = insert_event.call_args.args[1]
        self.assertEqual(event.event_type, "positioning_report_published")
        self.assertEqual(
            [(market.canonical_id, market.asset_class) for market in event.markets],
            [("cftc:EURUSD", "fx"), ("cftc:XAUUSD", "commodity")],
        )
        self.assertEqual(event.observed_at, datetime(2026, 8, 5, tzinfo=UTC))

    @patch("events.publisher.insert_event")
    @patch("events.publisher.upsert_raw")
    @patch("events.publisher.find_latest_event", return_value=None)
    def test_positioning_observed_falls_back_to_report_date(
        self, _find_latest, _upsert_raw, insert_event
    ):
        insert_event.side_effect = _insert_capture()
        record = self._positioning_record()
        del record["metadata"]["acquired_at"]

        publish_record(MagicMock(), record, source="sec_form4")

        event = insert_event.call_args.args[1]
        self.assertEqual(event.observed_at, datetime(2026, 8, 5, tzinfo=UTC))


class CorporateActionPublicationTests(unittest.TestCase):
    def _action_record(self, **overrides):
        record = {
            "action_id": "9" * 64,
            "symbol": "AAPL",
            "action_type": "dividend",
            "effective_date": date(2024, 5, 8),
            "source": "public_equities",
            "source_timestamp": datetime(2024, 5, 8, 12, 0, tzinfo=UTC),
            "available_at": datetime(2026, 8, 15, 7, 0, tzinfo=UTC),
            "amount": 0.25,
            "ratio_numerator": None,
            "ratio_denominator": None,
            "description": None,
            "metadata": {"provider_event_key": "1715174400"},
        }
        record.update(overrides)
        return record

    @patch("events.publisher.insert_event")
    @patch("events.publisher.find_latest_event")
    def test_corporate_action_uses_do_nothing_raw_insert_and_never_updates(
        self, find_latest, insert_event
    ):
        insert_event.side_effect = _insert_capture()
        session = MagicMock()
        record = self._action_record()

        result = publish_record(session, record, source="corporate_actions")

        self.assertTrue(result.inserted)
        find_latest.assert_not_called()
        session.execute.assert_called_once()
        statement = str(session.execute.call_args.args[0]).lower()
        self.assertIn("insert into corporate_actions", statement)
        self.assertIn("on conflict (action_id) do nothing", statement)
        self.assertNotIn("do update", statement)
        params = session.execute.call_args.args[1]
        self.assertEqual(params["action_id"], "9" * 64)
        self.assertEqual(params["amount"], 0.25)
        event = insert_event.call_args.args[1]
        self.assertEqual(event.event_type, "corporate_action_published")
        self.assertEqual(event.source_event_id, "9" * 64)
        self.assertEqual(event.observed_at, datetime(2024, 5, 8, 12, 0, tzinfo=UTC))
        self.assertEqual(event.markets[0].canonical_id, "equity:AAPL")

    @patch("events.publisher.insert_event")
    @patch("events.publisher.find_latest_event")
    def test_corporate_action_replay_is_immutable_no_op(
        self, find_latest, insert_event
    ):
        insert_event.side_effect = _insert_capture(
            inserted=False, outbox_inserted=False
        )
        session = MagicMock()
        record = self._action_record()

        result = publish_record(session, record, source="corporate_actions")

        self.assertFalse(result.inserted)
        self.assertFalse(result.outbox_inserted)
        find_latest.assert_not_called()
        statement = str(session.execute.call_args.args[0]).lower()
        self.assertIn("do nothing", statement)
        self.assertNotIn("do update", statement)

    @patch("events.publisher.insert_event")
    @patch("events.publisher.find_latest_event")
    def test_corporate_action_without_action_id_rejects_before_db_use(
        self, find_latest, insert_event
    ):
        session = MagicMock()
        record = self._action_record()
        del record["action_id"]

        with self.assertRaisesRegex(ValueError, "action_id"):
            publish_record(session, record, source="corporate_actions")

        session.execute.assert_not_called()
        find_latest.assert_not_called()
        insert_event.assert_not_called()


class OptionChainPublicationTests(unittest.TestCase):
    def _contract(
        self,
        index,
        captured_at,
        *,
        symbol="AAPL",
        source_timestamp=None,
        expiration=None,
    ):
        expiration = expiration or date(2026, 9, 18)
        return {
            "source": "cboe_options",
            "symbol": symbol,
            "contract_symbol": f"{symbol}260918C{index:08d}",
            "captured_at": captured_at,
            "source_timestamp": source_timestamp
            or datetime(2026, 8, 15, 12, 59, tzinfo=UTC),
            "expiration": expiration,
            "strike": 100.0 + index,
            "option_type": "call" if index % 2 == 0 else "put",
            "bid": 1.0,
            "ask": 1.1,
            "last": 1.05,
            "volume": 10,
            "open_interest": 5,
            "implied_volatility": 0.25,
            "underlying_price": 232.0,
            "metadata": {
                "delayed": True,
                "delay_minutes": 15,
                "truncated": {
                    "symbols": False,
                    "expiries": False,
                    "contracts": False,
                },
            },
        }

    @patch("events.publisher.insert_event")
    def test_ten_thousand_contracts_yield_one_compact_event_per_snapshot(
        self, insert_event
    ):
        insert_event.side_effect = _insert_capture()
        session = MagicMock()
        captured_at = datetime(2026, 8, 15, 13, 0, tzinfo=UTC)
        base_time = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
        records = [
            self._contract(
                index,
                captured_at,
                source_timestamp=base_time + timedelta(seconds=index),
                expiration=(date(2026, 9, 18) if index < 5000 else date(2026, 12, 18)),
            )
            for index in range(10000)
        ]

        result = publish_option_chain_records(records, session=session)

        self.assertEqual(result.attempted, 10000)
        self.assertEqual(result.raw_written, 10000)
        self.assertEqual(result.events_inserted, 1)
        self.assertEqual(result.outbox_inserted, 1)
        self.assertEqual(insert_event.call_count, 1)
        event = insert_event.call_args.args[1]
        self.assertEqual(event.event_type, "option_chain_published")
        self.assertEqual(event.source, "cboe_options")
        self.assertEqual(event.source_event_id, "AAPL:2026-08-15T13:00:00+00:00")
        payload = event.payload
        self.assertEqual(payload["symbol"], "AAPL")
        self.assertEqual(payload["contract_count"], 10000)
        self.assertEqual(payload["contracts_by_type"], {"call": 5000, "put": 5000})
        self.assertEqual(payload["expiration_count"], 2)
        self.assertEqual(payload["source_timestamp_min"], "2026-08-15T12:00:00+00:00")
        self.assertEqual(
            payload["source_timestamp_max"],
            (base_time + timedelta(seconds=9999)).isoformat(),
        )
        self.assertEqual(payload["expiration_min"], "2026-09-18")
        self.assertEqual(payload["expiration_max"], "2026-12-18")
        self.assertEqual(payload["underlying_price"], 232.0)
        self.assertTrue(payload["delayed"])
        self.assertEqual(payload["delay_minutes"], 15)
        self.assertEqual(
            payload["truncated"],
            {"symbols": False, "expiries": False, "contracts": False},
        )
        self.assertEqual(event.markets[0].canonical_id, "equity:AAPL")
        # Every contract is persisted immutably in bounded executemany
        # chunks: DO NOTHING, never UPDATE, one statement per 1000 rows.
        self.assertEqual(session.execute.call_count, 10)
        parameter_batches = [call.args[1] for call in session.execute.call_args_list]
        self.assertEqual({len(batch) for batch in parameter_batches}, {1000})
        self.assertEqual(sum(len(batch) for batch in parameter_batches), 10000)
        contract_symbols = {
            params["contract_symbol"] for batch in parameter_batches for params in batch
        }
        self.assertEqual(len(contract_symbols), 10000)
        for call in session.execute.call_args_list:
            statement = str(call.args[0]).lower()
            self.assertIn("insert into option_chain_snapshots", statement)
            self.assertIn(
                "on conflict (source, contract_symbol, captured_at) do nothing",
                statement,
            )
            self.assertNotIn("do update", statement)

    @patch("events.publisher.insert_event")
    def test_one_event_per_symbol_snapshot(self, insert_event):
        insert_event.side_effect = _insert_capture()
        session = MagicMock()
        captured_at = datetime(2026, 8, 15, 13, 0, tzinfo=UTC)
        records = [
            self._contract(index, captured_at, symbol="AAPL") for index in range(100)
        ]
        records.extend(
            self._contract(index, captured_at, symbol="MSFT") for index in range(50)
        )
        records.extend(
            self._contract(index, captured_at + timedelta(hours=1), symbol="AAPL")
            for index in range(10)
        )

        result = publish_option_chain_records(records, session=session)

        self.assertEqual(result.attempted, 160)
        self.assertEqual(result.events_inserted, 3)
        self.assertEqual(insert_event.call_count, 3)
        symbols = {
            insert_event.call_args_list[call][0][1].payload["symbol"]
            for call in range(3)
        }
        self.assertEqual(symbols, {"AAPL", "MSFT"})

    @patch("events.publisher.insert_event")
    def test_identical_snapshot_replay_is_no_op(self, insert_event):
        insert_event.side_effect = _insert_capture(inserted=False)
        session = MagicMock()
        captured_at = datetime(2026, 8, 15, 13, 0, tzinfo=UTC)
        records = [self._contract(index, captured_at) for index in range(100)]

        result = publish_option_chain_records(records, session=session)

        self.assertEqual(result.events_inserted, 0)
        self.assertEqual(result.events_deduplicated, 1)
        self.assertEqual(result.outbox_inserted, 0)
        # Raw rows are re-asserted as immutable no-ops in one chunk.
        self.assertEqual(session.execute.call_count, 1)
        parameter_batch = session.execute.call_args.args[1]
        self.assertEqual(len(parameter_batch), 100)
        statement = str(session.execute.call_args.args[0]).lower()
        self.assertIn("do nothing", statement)
        self.assertNotIn("do update", statement)

    @patch("events.publisher.insert_event")
    def test_malformed_mixed_snapshot_groups_reject_atomically(self, insert_event):
        session = MagicMock()
        captured_at = datetime(2026, 8, 15, 13, 0, tzinfo=UTC)
        records = [self._contract(index, captured_at) for index in range(3)]
        records.extend(
            self._contract(index, captured_at, symbol="MSFT") for index in range(3)
        )
        malformed = self._contract(0, captured_at, symbol="MSFT")
        malformed["contract_symbol"] = ""
        records.append(malformed)

        with self.assertRaisesRegex(ValueError, "contract_symbol"):
            publish_option_chain_records(records, session=session)

        # Validation precedes any write: the whole batch is rejected, even
        # the otherwise-valid AAPL snapshot group.
        session.execute.assert_not_called()
        insert_event.assert_not_called()

    @patch("events.publisher.insert_event")
    def test_heterogeneous_contract_keys_reject_before_any_db_write(self, insert_event):
        session = MagicMock()
        captured_at = datetime(2026, 8, 15, 13, 0, tzinfo=UTC)
        records = [self._contract(0, captured_at)]
        malformed = self._contract(1, captured_at)
        del malformed["open_interest"]
        records.append(malformed)

        with self.assertRaisesRegex(ValueError, "column set"):
            publish_option_chain_records(records, session=session)

        session.execute.assert_not_called()
        insert_event.assert_not_called()


class AtomicAdapterMappingTests(unittest.TestCase):
    def setUp(self):
        self.record = {
            "series_id": "GDP",
            "observed_at": datetime(2026, 8, 5, tzinfo=UTC),
            "value": 3.2,
        }

    @patch("events.publisher.publish_records")
    def test_atomic_adapter_accepts_new_source_table_pairs(self, publish_records):
        publish_records.return_value = PublicationResult(1, 1, 1, 0, 1)
        for source_id, table_name, conflicts in (
            ("issuer_news", "source_documents", ["document_id"]),
            ("issuer_transcripts", "source_documents", ["document_id"]),
            (
                "public_equities",
                "market_data",
                ["symbol", "timeframe", "timestamp"],
            ),
            (
                "sec_form4",
                "positioning_reports",
                ["source", "market_id", "report_date", "category"],
            ),
            (
                "finra_short_volume",
                "positioning_reports",
                ["source", "market_id", "report_date", "category"],
            ),
        ):
            result = publish_collector_records_atomic(
                source_id=source_id,
                table_name=table_name,
                records=[{}],
                conflict_columns=conflicts,
                config={},
            )
            self.assertEqual(result.status, "success")
            self.assertEqual(publish_records.call_args.args[0], source_id)

    def test_atomic_adapter_rejects_unknown_source_table_pairs(self):
        with self.assertRaisesRegex(ValueError, "unsupported source/table"):
            publish_collector_records_atomic(
                source_id="issuer_news",
                table_name="market_data",
                records=[{}],
                conflict_columns=["document_id"],
                config={},
            )
        with self.assertRaisesRegex(ValueError, "unsupported source conflict"):
            publish_collector_records_atomic(
                source_id="issuer_news",
                table_name="source_documents",
                records=[{}],
                conflict_columns=["document_id", "extra"],
                config={},
            )

    @patch("events.publisher.publish_option_chain_records")
    def test_atomic_adapter_routes_cboe_options_to_grouped_publisher(self, grouped):
        grouped.return_value = PublicationResult(2, 2, 1, 0, 1)
        records = [{"a": 1}, {"a": 2}]

        result = publish_collector_records_atomic(
            source_id="cboe_options",
            table_name="option_chain_snapshots",
            records=records,
            conflict_columns=["source", "contract_symbol", "captured_at"],
            config={},
        )

        grouped.assert_called_once()
        self.assertEqual(grouped.call_args.args[0], records)
        self.assertEqual(result.events_inserted, 1)

    @patch("events.publisher.get_session")
    def test_additional_writes_share_the_publisher_transaction(self, get_session):
        session = MagicMock()

        def _session_context(_config):
            @contextmanager
            def context():
                yield session

            return context()

        get_session.side_effect = _session_context
        action = {
            "action_id": "a" * 64,
            "symbol": "AAPL",
            "action_type": "dividend",
            "effective_date": date(2024, 5, 8),
            "source": "public_equities",
            "source_timestamp": datetime(2024, 5, 8, 12, 0, tzinfo=UTC),
            "amount": 0.25,
            "ratio_numerator": None,
            "ratio_denominator": None,
            "description": None,
            "metadata": {},
        }
        batches = [
            {
                "table_name": "corporate_actions",
                "records": [action],
                "conflict_columns": ["action_id"],
                "insert_only": True,
            }
        ]
        with patch("db.write_batches_in_session", create=True) as write_batches:
            with patch("events.publisher.publish_records") as publish_records:
                publish_records.return_value = PublicationResult(1, 1, 1, 0, 1)
                result = publish_collector_records_atomic(
                    source_id="fred",
                    table_name="macro_series",
                    records=[self.record],
                    conflict_columns=["series_id", "observed_at"],
                    config={},
                    additional_writes=batches,
                )

        # Primary records and the corporate action batch share one session;
        # the action batch is event-published, never a raw-only helper write.
        self.assertEqual(publish_records.call_count, 2)
        self.assertIs(publish_records.call_args_list[0].kwargs["session"], session)
        self.assertEqual(publish_records.call_args_list[1].args[0], "corporate_actions")
        self.assertIs(publish_records.call_args_list[1].kwargs["session"], session)
        write_batches.assert_not_called()
        # Aggregated counts include the event-published addition.
        self.assertEqual(result.attempted, 2)
        self.assertEqual(result.raw_written, 2)
        self.assertEqual(result.events_inserted, 2)
        self.assertEqual(result.outbox_inserted, 2)
        self.assertEqual(result.status, "success")

    @patch("events.publisher.get_session")
    def test_generic_additional_writes_still_use_the_db_helper(self, get_session):
        session = MagicMock()

        def _session_context(_config):
            @contextmanager
            def context():
                yield session

            return context()

        get_session.side_effect = _session_context
        batches = [
            {
                "table_name": "other_table",
                "records": [{"id": 1}],
                "conflict_columns": ["id"],
            }
        ]
        with patch("db.write_batches_in_session", create=True) as write_batches:
            with patch("events.publisher.publish_records") as publish_records:
                publish_records.return_value = PublicationResult(1, 1, 1, 0, 1)
                result = publish_collector_records_atomic(
                    source_id="fred",
                    table_name="macro_series",
                    records=[self.record],
                    conflict_columns=["series_id", "observed_at"],
                    config={},
                    additional_writes=batches,
                )

        publish_records.assert_called_once()
        write_batches.assert_called_once_with(session, batches)
        self.assertEqual(result.events_inserted, 1)

    def test_corporate_action_additional_write_requires_insert_only_semantics(self):
        batches = [
            {
                "table_name": "corporate_actions",
                "records": [],
                "conflict_columns": ["action_id"],
            }
        ]

        with self.assertRaisesRegex(ValueError, "insert_only"):
            publish_collector_records_atomic(
                source_id="fred",
                table_name="macro_series",
                records=[self.record],
                conflict_columns=["series_id", "observed_at"],
                config={},
                additional_writes=batches,
            )

    @patch("events.publisher.publish_records")
    def test_atomic_adapter_without_additional_writes_keeps_legacy_call(
        self, publish_records
    ):
        publish_records.return_value = PublicationResult(1, 1, 1, 0, 1)
        config = {"database": {}}

        publish_collector_records_atomic(
            source_id="fred",
            table_name="macro_series",
            records=[self.record],
            conflict_columns=["series_id", "observed_at"],
            config=config,
        )

        publish_records.assert_called_once_with(
            "fred", [self.record], config=config, correlation_id=None
        )

    @patch("events.publisher.publish_records")
    def test_atomic_adapter_forwards_insert_only_for_immutable_sources(
        self, publish_records
    ):
        # An insert-only source (public_equities bars) must reach the
        # publisher with the DO NOTHING flag; every other source keeps the
        # preexisting call contract (asserted above).
        publish_records.return_value = PublicationResult(1, 1, 1, 0, 1)
        config = {"database": {}}

        publish_collector_records_atomic(
            source_id="public_equities",
            table_name="market_data",
            records=[self.record],
            conflict_columns=["symbol", "timeframe", "timestamp"],
            config=config,
            insert_only=True,
        )

        publish_records.assert_called_once_with(
            "public_equities",
            [self.record],
            config=config,
            correlation_id=None,
            insert_only=True,
        )


if __name__ == "__main__":
    unittest.main()
