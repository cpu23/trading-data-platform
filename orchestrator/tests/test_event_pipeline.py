import sys
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector_execution import _run_collector_impl
from events import build_market_event
from events.publisher import (
    PublicationResult,
    publish_collector_records_atomic,
    publish_record,
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


if __name__ == "__main__":
    unittest.main()
