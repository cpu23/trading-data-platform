import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self.rows


class EventRoutingTests(unittest.TestCase):
    def test_event_enqueues_both_stable_section_jobs_per_ingestion_bucket(self):
        from events.routing import initial_handler

        event = SimpleNamespace(
            event_id="event-1",
            source="oanda",
            event_type="price_tick",
            content_hash="a" * 64,
            correlation_id="corr-1",
            ingested_at=datetime(2026, 8, 15, 9, 10, 30, tzinfo=UTC),
        )
        with (
            patch("events.freshness.record_event_observation", return_value={}),
            patch("events.routing._config", return_value={}),
            patch(
                "analysis_jobs.enqueue_job", side_effect=["source", "watchlist"]
            ) as enqueue,
        ):
            initial_handler(MagicMock(), event)

        self.assertEqual(enqueue.call_count, 2)
        calls = [call.kwargs for call in enqueue.call_args_list]
        self.assertEqual(
            [call["job_type"] for call in calls],
            ["publish_source_health_snapshot", "publish_watchlist_snapshot"],
        )
        self.assertEqual(
            [call["dedupe_key"] for call in calls],
            ["source_health:global", "watchlist:global"],
        )
        self.assertEqual(
            {call["input_fingerprint"] for call in calls},
            {"ingestion:2026-08-15T09:10:00:source:oanda"},
        )
        self.assertEqual(
            {call["payload"]["ingestion_bucket"] for call in calls},
            {"2026-08-15T09:10:00"},
        )
        self.assertEqual(
            {call["payload"]["event_content_hash"] for call in calls},
            {"a" * 64},
        )
        self.assertEqual({call["source_event_id"] for call in calls}, {"event-1"})

    def test_historical_backfill_coalesces_section_jobs_per_source_minute(self):
        from events.routing import initial_handler

        first = SimpleNamespace(
            event_id="event-1",
            source="public_equities",
            event_type="price_bar_closed",
            content_hash="a" * 64,
            correlation_id="corr-1",
            observed_at=datetime(2024, 1, 2, tzinfo=UTC),
            ingested_at=datetime(2026, 8, 15, 9, 10, 5, tzinfo=UTC),
        )
        second = SimpleNamespace(
            event_id="event-2",
            source="public_equities",
            event_type="price_bar_closed",
            content_hash="b" * 64,
            correlation_id="corr-1",
            observed_at=datetime(2025, 4, 3, tzinfo=UTC),
            ingested_at=datetime(2026, 8, 15, 9, 10, 50, tzinfo=UTC),
        )
        with (
            patch("events.freshness.record_event_observation", return_value={}),
            patch("events.routing._config", return_value={}),
            patch(
                "analysis_jobs.enqueue_job",
                side_effect=["source-1", "watchlist-1", "source-2", "watchlist-2"],
            ) as enqueue,
        ):
            initial_handler(MagicMock(), first)
            initial_handler(MagicMock(), second)

        self.assertEqual(enqueue.call_count, 4)
        self.assertEqual(
            {call.kwargs["input_fingerprint"] for call in enqueue.call_args_list},
            {"ingestion:2026-08-15T09:10:00:source:public_equities"},
        )


class HandlerBoundTests(unittest.TestCase):
    def test_source_health_is_allowlisted_bounded_and_keeps_aware_freshness(self):
        import analysis_job_handlers as handlers

        now = datetime(2026, 8, 6, 7, 0, tzinfo=UTC)
        session = MagicMock()
        session.execute.return_value = _Result(
            [
                {
                    "source": "oanda",
                    "state": "current",
                    "updated_at": now,
                    "detail": "never selected",
                }
            ]
        )
        published = MagicMock(return_value=SimpleNamespace(changed=True))
        snapshot_module = SimpleNamespace(publish_section_snapshot=published)
        with (
            patch.object(
                handlers,
                "load_config",
                return_value={
                    "event_pipeline": {"jobs": {"query": {"max_source_rows": 3}}}
                },
            ),
            patch.dict(sys.modules, {"section_snapshots": snapshot_module}),
        ):
            handlers.publish_source_health_snapshot(
                session, SimpleNamespace(source_event_id="event-1")
            )

        sql = str(session.execute.call_args.args[0])
        self.assertIn("LIMIT", sql)
        self.assertIn("source", sql)
        self.assertNotIn("detail", sql)
        self.assertIs(published.call_args.kwargs["data_freshness_at"].tzinfo, UTC)
        self.assertEqual(
            published.call_args.kwargs["payload"]["sources"][0]["updated_at"],
            now.isoformat(),
        )

    def test_watchlist_uses_one_bounded_query_and_allows_empty_rows(self):
        import analysis_job_handlers as handlers

        session = MagicMock()
        session.execute.return_value = _Result([])
        published = MagicMock(return_value=SimpleNamespace(changed=False))
        snapshot_module = SimpleNamespace(publish_section_snapshot=published)
        config = {
            "event_pipeline": {"jobs": {"query": {"max_watchlist_rows": 2}}},
            "collectors": {
                "oanda": {"instruments": [{"symbol": "EURUSD", "enabled": True}]}
            },
        }
        with (
            patch.object(handlers, "load_config", return_value=config),
            patch.dict(sys.modules, {"section_snapshots": snapshot_module}),
        ):
            handlers.publish_watchlist_snapshot(
                session, SimpleNamespace(source_event_id=None)
            )

        self.assertEqual(session.execute.call_count, 1)
        sql = str(session.execute.call_args.args[0])
        self.assertIn("DISTINCT ON", sql)
        self.assertIn("LIMIT", sql)
        self.assertEqual(published.call_args.kwargs["payload"], {"instruments": []})


class LifecycleAndReconciliationTests(unittest.TestCase):
    def test_api_lifecycle_owns_no_worker_scheduler_or_stream_singletons(self):
        import main

        config = {"logging": {"level": "INFO"}}
        with (
            patch.object(main, "_get_config", return_value=config),
            patch.object(main, "check_connection", return_value=True),
            patch.object(main, "setup_logging"),
            patch.object(main, "close_shared_client") as close,
            patch.object(main, "threading") as threading,
        ):
            threading.Thread = MagicMock()
            main.on_startup()
            main.on_shutdown()

        close.assert_called_once_with()
        # The API only records its own durable heartbeat; it never starts the
        # analysis worker, outbox worker, scheduler, or quote stream.
        self.assertEqual(main.on_startup.__name__, "on_startup")
        for name in (
            "outbox_worker",
            "job_worker",
            "start_scheduler",
            "quote_stream",
        ):
            self.assertFalse(
                hasattr(main, name),
                f"main must not own the {name} singleton",
            )

    def test_reconciliation_repairs_are_isolated_by_class(self):
        import reconciliation

        class SessionContext:
            def __enter__(self):
                return MagicMock()

            def __exit__(self, *args):
                return False

        with (
            patch.object(reconciliation, "get_session", return_value=SessionContext()),
            patch(
                "analysis_jobs.reconcile_jobs",
                side_effect=RuntimeError("private payload"),
            ),
            patch(
                "section_snapshots.reconcile_snapshots", return_value={"repaired": 2}
            ),
            patch("ui_events.delete_expired_ui_events", return_value=4),
            patch(
                "events.freshness.refresh_freshness_states", return_value={"changed": 3}
            ),
        ):
            result = reconciliation.reconcile_event_pipeline(
                {"event_pipeline": {"jobs": {}}}
            )
        self.assertEqual(result["jobs_reconciled"], 0)
        self.assertEqual(result["snapshots_reconciled"], 2)
        self.assertEqual(result["ui_events_expired"], 4)
        self.assertEqual(result["freshness_reclassified"], 3)
        self.assertEqual(result["error_count"], 1)
        self.assertNotIn("private payload", str(result))


class ThesisAutonomyRoutingTests(unittest.TestCase):
    def _config(self, enabled=True, debounce=60):
        return {
            "thesis_autonomy": {
                "enabled": enabled,
                "event_debounce_minutes": debounce,
            }
        }

    def _event(
        self,
        event_type,
        source_at,
        source="oanda",
        event_id="event-1",
        *,
        ingested_at=None,
    ):
        return SimpleNamespace(
            event_id=event_id,
            source=source,
            event_type=event_type,
            content_hash="a" * 64,
            correlation_id="corr-1",
            ingested_at=ingested_at or source_at,
            payload={"released_at": source_at.isoformat()},
        )

    def test_debounce_bucket_is_deterministic_utc_and_config_driven(self):
        from events.routing import _thesis_autonomy_bucket

        config = self._config()
        first = self._event("macro_release", datetime(2026, 8, 15, 9, 10, tzinfo=UTC))
        later_same_bucket = self._event(
            "macro_revision", datetime(2026, 8, 15, 9, 45, tzinfo=UTC)
        )
        next_bucket = self._event(
            "macro_release", datetime(2026, 8, 15, 10, 5, tzinfo=UTC)
        )
        self.assertEqual(_thesis_autonomy_bucket(first, config), "2026-08-15T09:00:00")
        self.assertEqual(
            _thesis_autonomy_bucket(later_same_bucket, config),
            _thesis_autonomy_bucket(first, config),
        )
        self.assertEqual(
            _thesis_autonomy_bucket(next_bucket, config), "2026-08-15T10:00:00"
        )
        # The debounce window is config-driven.
        self.assertEqual(
            _thesis_autonomy_bucket(first, self._config(debounce=30)),
            "2026-08-15T09:00:00",
        )
        self.assertEqual(
            _thesis_autonomy_bucket(
                self._event("macro_release", datetime(2026, 8, 15, 9, 25, tzinfo=UTC)),
                self._config(debounce=30),
            ),
            "2026-08-15T09:00:00",
        )
        self.assertEqual(
            _thesis_autonomy_bucket(
                self._event("macro_release", datetime(2026, 8, 15, 9, 35, tzinfo=UTC)),
                self._config(debounce=30),
            ),
            "2026-08-15T09:30:00",
        )

    def test_historical_backfill_events_coalesce_by_ingestion_time(self):
        from events.routing import _thesis_autonomy_bucket

        first = self._event(
            "price_bar_closed",
            datetime(2024, 1, 2, tzinfo=UTC),
            ingested_at=datetime(2026, 8, 15, 9, 10, tzinfo=UTC),
        )
        later_ingestion = self._event(
            "macro_release",
            datetime(2025, 4, 3, tzinfo=UTC),
            ingested_at=datetime(2026, 8, 15, 9, 45, tzinfo=UTC),
        )
        next_bucket = self._event(
            "positioning_report_published",
            datetime(2026, 8, 14, tzinfo=UTC),
            ingested_at=datetime(2026, 8, 15, 10, 5, tzinfo=UTC),
        )

        self.assertEqual(
            _thesis_autonomy_bucket(first, self._config()),
            "2026-08-15T09:00:00",
        )
        self.assertEqual(
            _thesis_autonomy_bucket(later_ingestion, self._config()),
            "2026-08-15T09:00:00",
        )
        self.assertEqual(
            _thesis_autonomy_bucket(next_bucket, self._config()),
            "2026-08-15T10:00:00",
        )

    def test_enqueue_one_job_per_bucket_with_bounded_payload(self):
        from events.routing import _enqueue_thesis_autonomy_job

        event = self._event("price_tick", datetime(2026, 8, 15, 9, 10, tzinfo=UTC))
        with patch("analysis_jobs.enqueue_job") as enqueue:
            enqueue.return_value = SimpleNamespace(inserted=True, suppressed=False)
            jobs = _enqueue_thesis_autonomy_job(
                MagicMock(),
                event,
                self._config(),
                event_hash="a" * 64,
                correlation_id="corr-1",
                source_event_id="event-1",
            )
        self.assertEqual(len(jobs), 1)
        call = enqueue.call_args
        self.assertEqual(call.kwargs["job_type"], "thesis_autonomy_run")
        self.assertEqual(
            call.kwargs["dedupe_key"], "thesis-autonomy:event:2026-08-15T09:00:00"
        )
        self.assertEqual(call.kwargs["input_fingerprint"], "bucket:2026-08-15T09:00:00")
        self.assertEqual(
            call.kwargs["payload"],
            {
                "source": "oanda",
                "event_type": "price_tick",
                "event_content_hash": "a" * 64,
                "bucket": "2026-08-15T09:00:00",
                "as_of": "2026-08-15T09:10:00+00:00",
            },
        )
        self.assertEqual(call.kwargs["source_event_id"], "event-1")

    def test_daily_event_quota_blocks_enqueue_at_limit_under_advisory_lock(self):
        from events.routing import _enqueue_thesis_autonomy_job

        event = self._event("macro_release", datetime(2026, 8, 15, 23, 10, tzinfo=UTC))
        session = MagicMock()
        count_result = MagicMock()
        count_result.mappings.return_value.first.return_value = {"count": 4}
        session.execute.side_effect = [MagicMock(), count_result]
        config = self._config()
        config["thesis_autonomy"]["maximum_event_runs_per_day"] = 4

        with patch("analysis_jobs.enqueue_job") as enqueue:
            jobs = _enqueue_thesis_autonomy_job(
                session,
                event,
                config,
                event_hash="a" * 64,
                correlation_id="corr-1",
                source_event_id="event-1",
            )

        self.assertEqual(jobs, [])
        enqueue.assert_not_called()
        self.assertEqual(session.execute.call_count, 2)
        lock_sql = str(session.execute.call_args_list[0].args[0])
        self.assertIn("pg_advisory_xact_lock", lock_sql)
        self.assertEqual(
            session.execute.call_args_list[0].args[1]["quota_key"],
            "thesis-autonomy:event:2026-08-15",
        )
        count_params = session.execute.call_args_list[1].args[1]
        self.assertEqual(count_params["day_start"], datetime(2026, 8, 15, tzinfo=UTC))
        self.assertEqual(count_params["day_end"], datetime(2026, 8, 16, tzinfo=UTC))

    def test_daily_event_quota_allows_one_remaining_slot(self):
        from events.routing import _enqueue_thesis_autonomy_job

        event = self._event("macro_release", datetime(2026, 8, 15, 20, 10, tzinfo=UTC))
        session = MagicMock()
        count_result = MagicMock()
        count_result.mappings.return_value.first.return_value = {"count": 3}
        session.execute.side_effect = [MagicMock(), count_result]
        config = self._config()
        config["thesis_autonomy"]["maximum_event_runs_per_day"] = 4

        with patch(
            "analysis_jobs.enqueue_job",
            return_value=SimpleNamespace(inserted=True, suppressed=False),
        ) as enqueue:
            jobs = _enqueue_thesis_autonomy_job(
                session,
                event,
                config,
                event_hash="a" * 64,
                correlation_id="corr-1",
                source_event_id="event-1",
            )

        self.assertEqual(len(jobs), 1)
        enqueue.assert_called_once()

    def test_disabled_config_enqueues_nothing(self):
        from events.routing import _enqueue_thesis_autonomy_job

        event = self._event("price_tick", datetime(2026, 8, 15, 9, 10, tzinfo=UTC))
        with patch("analysis_jobs.enqueue_job") as enqueue:
            jobs = _enqueue_thesis_autonomy_job(
                MagicMock(),
                event,
                {"thesis_autonomy": {"enabled": False}},
                event_hash="a" * 64,
                correlation_id="corr-1",
                source_event_id="event-1",
            )
        self.assertEqual(jobs, [])
        enqueue.assert_not_called()

    def test_initial_handler_enqueues_autonomy_before_early_return(self):
        from events.routing import initial_handler

        event = self._event("macro_release", datetime(2026, 8, 15, 9, 10, tzinfo=UTC))
        with (
            patch("events.freshness.record_event_observation", return_value={}),
            patch("events.routing._config", return_value=self._config()),
            patch(
                "analysis_jobs.enqueue_job",
                return_value=SimpleNamespace(inserted=True, suppressed=False),
            ) as enqueue,
        ):
            initial_handler(MagicMock(), event)
        job_types = [call.kwargs["job_type"] for call in enqueue.call_args_list]
        self.assertIn("thesis_autonomy_run", job_types)
        self.assertIn("publish_source_health_snapshot", job_types)
        self.assertIn("publish_watchlist_snapshot", job_types)
        self.assertEqual(
            len(
                [
                    job_type
                    for job_type in job_types
                    if job_type == "thesis_autonomy_run"
                ]
            ),
            1,
        )

    def test_non_material_event_types_do_not_enqueue_autonomy(self):
        from events.routing import initial_handler

        event = self._event(
            "source_freshness_changed", datetime(2026, 8, 15, 9, 10, tzinfo=UTC)
        )
        with (
            patch("events.freshness.record_event_observation", return_value={}),
            patch("events.routing._config", return_value=self._config()),
            patch(
                "analysis_jobs.enqueue_job",
                return_value=SimpleNamespace(inserted=True, suppressed=False),
            ) as enqueue,
        ):
            initial_handler(MagicMock(), event)
        job_types = [call.kwargs["job_type"] for call in enqueue.call_args_list]
        self.assertNotIn("thesis_autonomy_run", job_types)
        # The two stable section jobs are preserved.
        self.assertEqual(len(job_types), 2)

    def test_price_tick_control_plane_propagates_once_per_target_bucket(self):
        from events.routing import initial_handler

        config = {
            "thesis_autonomy": {"enabled": False},
            "research_control_plane": {
                "enabled": True,
                "event_debounce_seconds": 120,
                "priority_policy_version": "v1",
            },
        }

        def event(symbol, at, event_id):
            payload = {
                "event_id": event_id,
                "source": "oanda",
                "event_type": "price_tick",
                "entities": [{"symbol": symbol}],
                "markets": [],
                "importance_hint": "0.5",
            }
            return SimpleNamespace(
                **payload,
                content_hash="a" * 64,
                correlation_id="corr-1",
                ingested_at=at,
                payload={},
                model_dump=lambda mode: dict(payload),
            )

        seen = set()
        enqueued = []

        def enqueue(_session, **kwargs):
            identity = (
                kwargs["job_type"],
                kwargs["dedupe_key"],
                kwargs["input_fingerprint"],
            )
            inserted = identity not in seen
            seen.add(identity)
            enqueued.append(kwargs)
            return SimpleNamespace(
                inserted=inserted,
                job=SimpleNamespace(id=f"job-{len(enqueued)}"),
            )

        start = datetime(2026, 8, 15, 9, 10, tzinfo=UTC)
        events = (
            event("AAPL", start, "tick-1"),
            event("AAPL", start.replace(minute=11), "tick-2"),
            event("MSFT", start.replace(minute=11), "tick-3"),
            event("AAPL", start.replace(minute=12), "tick-4"),
        )
        session = MagicMock()
        with (
            patch("events.freshness.record_event_observation", return_value={}),
            patch("events.routing._config", return_value=config),
            patch("events.routing._enqueue_section_jobs", return_value=[]),
            patch("analysis_jobs.enqueue_job", side_effect=enqueue),
            patch(
                "research_control_plane.repository.propagate_event_dependencies",
                return_value={
                    "nodes_touched": 1,
                    "edges_touched": 1,
                    "theses_affected": 1,
                },
            ) as propagate,
            patch(
                "research_control_plane.repository.upsert_question",
                return_value={"id": "question"},
            ),
            patch("ui_events.append_ui_invalidations"),
        ):
            results = [initial_handler(session, item) for item in events]

        self.assertEqual(propagate.call_count, 3)
        self.assertTrue(results[0]["research_control_plane"]["planner_job_created"])
        self.assertTrue(results[1]["research_control_plane"]["planner_job_coalesced"])
        self.assertEqual(
            results[1]["research_control_plane"]["nodes_touched"],
            0,
        )
        self.assertNotEqual(
            enqueued[0]["dedupe_key"],
            enqueued[2]["dedupe_key"],
        )
        self.assertEqual(enqueued[0]["dedupe_key"], enqueued[1]["dedupe_key"])
        self.assertNotEqual(
            enqueued[0]["input_fingerprint"],
            enqueued[3]["input_fingerprint"],
        )


class PlaybookMatchLedgerTests(unittest.TestCase):
    """Event-driven context-match ledger (events/routing.py)."""

    EVENT_ID = "99999999-9999-4999-8999-999999999999"

    def _event(self, event_type="headline_published", symbol="ACME"):
        return SimpleNamespace(
            event_id=UUID(self.EVENT_ID),
            source="issuer_news",
            event_type=event_type,
            content_hash="a" * 64,
            correlation_id="corr-1",
            observed_at=datetime(2026, 8, 15, 9, 10, tzinfo=UTC),
            markets=[{"symbol": symbol, "canonical_id": symbol.casefold()}],
            entities=[],
            payload={"released_at": "2026-08-15T09:10:00+00:00"},
        )

    def _config(self, enabled=True):
        return {
            "thesis_autonomy": {
                "enabled": enabled,
                "event_debounce_minutes": 60,
            }
        }

    def _session(self, *, symbol="ACME", company=None):
        from thesis_autonomy_support import (
            EXISTING_ID,
            MemorySession,
        )

        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID, status="active", symbol=symbol, company=company
        )
        playbook = session.seed_playbook(
            EXISTING_ID,
            event_types=["headline_published", "story_updated"],
        )
        session.market_events.add(self.EVENT_ID)
        return session, playbook

    def test_context_match_recorded_once_per_event(self):
        from thesis_autonomy_support import EXISTING_ID

        from events.routing import _match_due_playbooks

        session, playbook = self._session()
        event = self._event()
        first = _match_due_playbooks(session, event, self._config())
        self.assertEqual(first["playbooks_loaded"], 1)
        self.assertEqual(first["matches_recorded"], 1)
        self.assertEqual(first["thesis_ids"], [EXISTING_ID])
        second = _match_due_playbooks(session, event, self._config())
        self.assertEqual(second["matches_recorded"], 0)
        self.assertEqual(len(session.event_matches), 1)
        self.assertEqual(
            session.event_matches, {(playbook["id"], self.EVENT_ID, "context")}
        )

    def test_mismatched_entities_record_nothing(self):
        from events.routing import _match_due_playbooks

        session, _playbook = self._session(symbol="MSFT")
        result = _match_due_playbooks(
            session, self._event(symbol="ACME"), self._config()
        )
        self.assertEqual(result["matches_recorded"], 0)
        self.assertEqual(session.event_matches, set())

    def test_no_due_playbooks_record_nothing(self):
        from thesis_autonomy_support import MemorySession

        from events.routing import _match_due_playbooks

        session = MemorySession()
        session.market_events.add(self.EVENT_ID)
        result = _match_due_playbooks(session, self._event(), self._config())
        self.assertEqual(result["playbooks_loaded"], 0)
        self.assertEqual(result["matches_recorded"], 0)

    def test_disabled_config_skips_the_ledger(self):
        from events.routing import _match_due_playbooks

        session, _playbook = self._session()
        result = _match_due_playbooks(session, self._event(), self._config(False))
        self.assertEqual(result["matches_recorded"], 0)
        self.assertEqual(session.event_matches, set())

    def test_initial_handler_records_ledger_before_early_return(self):
        from thesis_autonomy_support import EXISTING_ID

        from events.routing import initial_handler

        session, _playbook = self._session()
        event = self._event()
        with (
            patch("events.freshness.record_event_observation", return_value={}),
            patch("events.routing._config", return_value=self._config()),
            patch(
                "analysis_jobs.enqueue_job",
                return_value=SimpleNamespace(inserted=True, suppressed=False),
            ) as enqueue,
        ):
            result = initial_handler(session, event)
        self.assertEqual(result["playbook_matches"]["matches_recorded"], 1)
        self.assertEqual(result["playbook_matches"]["thesis_ids"], [EXISTING_ID])
        job_types = [call.kwargs["job_type"] for call in enqueue.call_args_list]
        self.assertIn("thesis_autonomy_run", job_types)
        self.assertEqual(len(session.event_matches), 1)


if __name__ == "__main__":
    unittest.main()
