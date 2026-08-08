import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self.rows


class EventRoutingTests(unittest.TestCase):
    def test_event_enqueues_both_stable_section_jobs_with_content_fingerprint(self):
        from events.routing import initial_handler

        event = SimpleNamespace(
            event_id="event-1",
            source="oanda",
            event_type="price_tick",
            content_hash="a" * 64,
            correlation_id="corr-1",
        )
        with (
            patch("events.freshness.record_event_observation", return_value={}),
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
        self.assertEqual({call["input_fingerprint"] for call in calls}, {"a" * 64})


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
    def test_api_lifecycle_starts_and_stops_analysis_worker(self):
        import main

        config = {"logging": {"level": "INFO"}}
        worker = MagicMock()
        with (
            patch.object(main, "job_worker", worker),
            patch.object(main, "_get_config", return_value=config),
            patch.object(main, "check_connection", return_value=True),
            patch.object(main, "setup_logging"),
            patch.object(
                main,
                "reconcile_abandoned_runs",
                return_value={"accepted_ids": [], "running_ids": [], "total": 0},
            ),
            patch.object(main.outbox_worker, "start"),
            patch.object(main.outbox_worker, "stop"),
            patch.object(main, "start_scheduler"),
            patch.object(main, "stop_scheduler"),
            patch.object(main, "close_shared_client"),
            patch.object(main.quote_stream, "start"),
            patch.object(main.quote_stream, "stop"),
        ):
            main.on_startup()
            main.on_shutdown()
        worker.start.assert_called_once_with(config)
        worker.stop.assert_called_once_with()

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


if __name__ == "__main__":
    unittest.main()
