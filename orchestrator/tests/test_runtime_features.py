import base64
import os
import sys
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DASHBOARD_USER", "internal-user")
os.environ.setdefault("DASHBOARD_PASSWORD", "internal-pass")
INTERNAL_AUTH = {"Authorization": "Basic " + base64.b64encode(b"internal-user:internal-pass").decode()}

from collectors.oanda import OandaCollector
from llm_client import resolve_model
from orchestrator import update_run_progress
from scheduler import scheduler_status, start_scheduler, stop_scheduler


class ComponentIdValidationTests(unittest.TestCase):
    """Task 10: Validate component IDs before accepting background work."""

    def setUp(self):
        from main import app
        from fastapi.testclient import TestClient
        self.client = TestClient(app, headers=INTERNAL_AUTH)

    def test_run_collector_invalid_id_returns_404(self):
        """POST /run_collector/not-real returns 404."""
        resp = self.client.post("/run_collector/not-real")
        self.assertEqual(resp.status_code, 404)

    def test_run_processor_invalid_id_returns_404(self):
        """POST /run_processor/not-real returns 404."""
        resp = self.client.post("/run_processor/not-real")
        self.assertEqual(resp.status_code, 404)

    def test_run_news_validates_before_durable_acceptance(self):
        with patch("main.accept_run") as accept:
            response = self.client.post("/run_news/not-real")
        self.assertEqual(response.status_code, 404)
        accept.assert_not_called()

    def test_run_news_accepts_before_enqueue(self):
        import main

        events = []
        background = Mock()
        background.add_task.side_effect = lambda *args: events.append("enqueue")
        with patch("main._get_config", return_value={}), patch(
            "main.accept_run",
            side_effect=lambda *args, **kwargs: events.append("accept") or datetime.now(timezone.utc),
        ):
            response = main.trigger_news("reuters", background, body={})
        self.assertEqual(events, ["accept", "enqueue"])
        self.assertIn("job_id", response)

    def test_run_news_duplicate_acceptance_returns_409_without_enqueue(self):
        import main
        from fastapi import HTTPException
        from orchestrator import RunAcceptanceConflict

        background = Mock()
        with patch("main._get_config", return_value={}), patch(
            "main.accept_run", side_effect=RunAcceptanceConflict("duplicate")
        ):
            with self.assertRaises(HTTPException) as raised:
                main.trigger_news("reuters", background, body={})
        self.assertEqual(raised.exception.status_code, 409)
        background.add_task.assert_not_called()

    @patch("main.get_session")
    def test_no_cycle_runs_row_created_for_invalid_collector(self, get_session):
        """No cycle_runs row is created for invalid collector IDs."""
        session = Mock()
        get_session.return_value.__enter__.return_value = session

        resp = self.client.post("/run_collector/invalid-collector-id")
        self.assertEqual(resp.status_code, 404)
        # ensure_run should never have been called, so no INSERT into cycle_runs
        insert_calls = [
            call for call in session.execute.call_args_list
            if "INSERT INTO cycle_runs" in str(call)
        ]
        self.assertEqual(len(insert_calls), 0,
                         "No cycle_runs INSERT should occur for invalid collector ID")

    @patch("main.get_session")
    def test_no_cycle_runs_row_created_for_invalid_processor(self, get_session):
        """No cycle_runs row is created for invalid processor IDs."""
        session = Mock()
        get_session.return_value.__enter__.return_value = session

        resp = self.client.post("/run_processor/invalid-processor-id")
        self.assertEqual(resp.status_code, 404)
        insert_calls = [
            call for call in session.execute.call_args_list
            if "INSERT INTO cycle_runs" in str(call)
        ]
        self.assertEqual(len(insert_calls), 0,
                         "No cycle_runs INSERT should occur for invalid processor ID")


class DurableRunLifecycleTests(unittest.TestCase):
    """Phase 5 Task 15: durable acceptance and single-owner lifecycle."""

    def test_accept_run_inserts_accepted_state_and_metadata(self):
        from orchestrator import accept_run

        session = Mock()
        with patch("orchestrator.get_session") as get_session:
            get_session.return_value.__enter__.return_value = session
            accepted_at = accept_run(
                {}, "run-id", "api", "collector", "fred", "request-1"
            )

        sql, params = session.execute.call_args.args
        self.assertIn("INSERT INTO cycle_runs", str(sql))
        self.assertIn("'accepted'", str(sql))
        self.assertEqual(params["cid"], "run-id")
        self.assertEqual(params["component"], "fred")
        self.assertEqual(params["idempotency_key"], "request-1")
        self.assertEqual(params["accepted_at"], accepted_at)

    def test_start_run_is_conditional_and_reports_lost_race(self):
        from orchestrator import start_run

        session = Mock()
        session.execute.return_value.rowcount = 0
        with patch("orchestrator.get_session") as get_session:
            get_session.return_value.__enter__.return_value = session
            started = start_run({}, "run-id", "worker-1")

        self.assertFalse(started)
        sql, params = session.execute.call_args.args
        self.assertIn("status = 'accepted'", str(sql))
        self.assertIn("status = 'running'", str(sql))
        self.assertEqual(params["worker_id"], "worker-1")

    def test_heartbeat_and_progress_are_running_and_owner_conditional(self):
        from orchestrator import heartbeat_run

        session = Mock()
        session.execute.return_value.rowcount = 1
        with patch("orchestrator.get_session") as get_session:
            get_session.return_value.__enter__.return_value = session
            self.assertTrue(heartbeat_run({}, "run-id", "worker-1"))
            update_run_progress("run-id", {"stage": "fred"}, {}, "worker-1")

        heartbeat_sql = str(session.execute.call_args_list[0].args[0])
        progress_sql = str(session.execute.call_args_list[1].args[0])
        self.assertIn("status = 'running'", heartbeat_sql)
        self.assertIn("worker_id = :worker_id", heartbeat_sql)
        self.assertIn("heartbeat_at", progress_sql)
        self.assertIn("status = 'running'", progress_sql)
        self.assertIn("COALESCE(summary", progress_sql)
        self.assertIn("|| CAST(:summary AS JSONB)", progress_sql)

    def test_finish_run_does_not_overwrite_terminal_or_abandoned_state(self):
        from orchestrator import finish_run

        session = Mock()
        session.execute.return_value.rowcount = 0
        with patch("orchestrator.get_session") as get_session:
            get_session.return_value.__enter__.return_value = session
            changed = finish_run("run-id", "success", {}, {}, None)

        self.assertFalse(changed)
        self.assertIn("status IN ('running')", str(session.execute.call_args.args[0]))

    @patch("orchestrator.get_collector")
    @patch("orchestrator.start_run", return_value=False)
    @patch("orchestrator.accept_run")
    def test_lost_start_race_prevents_collector_work(self, accept, start, get_collector):
        from orchestrator import run_collector

        result = run_collector("fred", config={})

        accept.assert_called_once()
        start.assert_called_once()
        get_collector.assert_not_called()
        self.assertIsNone(result)

    def test_direct_collector_start_failure_finalizes_accepted_run_without_owner(self):
        import orchestrator

        original = RuntimeError("secret db failure")
        with patch.object(orchestrator, "accept_run"), patch.object(
            orchestrator, "start_run", side_effect=original
        ), patch.object(orchestrator, "_run_collector_impl") as work, patch.object(
            orchestrator, "finalize_run_safely"
        ) as finalize:
            with self.assertRaises(RuntimeError) as raised:
                orchestrator.run_collector("fred", config={}, correlation_id="run-id")

        self.assertIs(raised.exception, original)
        work.assert_not_called()
        finalize.assert_called_once_with(
            "run-id",
            "failed",
            {"status": "failed", "reason": "run start unavailable"},
            {},
            "run start unavailable",
            worker_id=None,
            run_kind="collector",
            component="fred",
        )

    def test_direct_cycle_start_failure_finalizes_accepted_run_without_owner(self):
        import orchestrator

        original = RuntimeError("secret db failure")
        with patch.object(orchestrator, "accept_run"), patch.object(
            orchestrator, "start_run", side_effect=original
        ), patch.object(orchestrator, "_run_full_cycle_impl") as work, patch.object(
            orchestrator, "finalize_run_safely"
        ) as finalize:
            with self.assertRaises(RuntimeError) as raised:
                orchestrator.run_full_cycle(config={}, correlation_id="run-id")

        self.assertIs(raised.exception, original)
        work.assert_not_called()
        finalize.assert_called_once_with(
            "run-id",
            "failed",
            {"status": "failed", "reason": "run start unavailable"},
            {},
            "run start unavailable",
            worker_id=None,
            run_kind="cycle",
        )

    def test_direct_processor_start_failure_finalizes_accepted_run_without_owner(self):
        import orchestrator

        original = RuntimeError("secret db failure")
        with patch.object(orchestrator, "accept_run"), patch.object(
            orchestrator, "start_run", side_effect=original
        ), patch.object(orchestrator, "_run_processor_impl") as work, patch.object(
            orchestrator, "finalize_run_safely"
        ) as finalize:
            with self.assertRaises(RuntimeError) as raised:
                orchestrator.run_processor("briefing", config={}, correlation_id="run-id")

        self.assertIs(raised.exception, original)
        work.assert_not_called()
        finalize.assert_called_once_with(
            "run-id",
            "failed",
            {"status": "failed", "reason": "run start unavailable"},
            {},
            "run start unavailable",
            worker_id=None,
            run_kind="processor",
            component="briefing",
        )

    @patch("orchestrator.run_collector")
    @patch("orchestrator.get_all_processors", return_value={})
    @patch("orchestrator.get_all_collectors", return_value={"fred": Mock()})
    def test_full_cycle_children_have_lifecycle_disabled(
        self, _collectors, _processors, run_collector
    ):
        from orchestrator import run_full_cycle

        run_collector.return_value = {"status": "success"}
        with patch("orchestrator.accept_run"), patch(
            "orchestrator.start_run", return_value=True
        ), patch("orchestrator.finish_run"), patch(
            "orchestrator.update_run_progress"
        ), patch(
            "orchestrator.advisory_lock", side_effect=lambda *_args: nullcontext()
        ):
            run_full_cycle(config={"collectors": {"fred": {"enabled": True}}})

        self.assertFalse(run_collector.call_args.kwargs["manage_lifecycle"])

    def test_endpoint_accepts_before_enqueue_for_each_run_kind(self):
        import main

        cases = [
            (main.trigger_cycle, (), "cycle"),
            (main.trigger_collector, ("fred",), "collector"),
            (main.trigger_processor, ("briefing",), "processor"),
        ]
        for endpoint, positional, run_kind in cases:
            with self.subTest(run_kind=run_kind):
                events = []
                background = Mock()
                background.add_task.side_effect = lambda *args: events.append("enqueue")
                with patch("main._get_config", return_value={}), patch("main.accept_run", side_effect=lambda *args, **kwargs: events.append("accept") or datetime.now(timezone.utc)), patch(
                    "collectors.get_all_collectors", return_value={"fred": Mock()}
                ), patch("processors.get_all_processors", return_value={"briefing": Mock()}):
                    endpoint(*positional, background, body={})
                self.assertEqual(events, ["accept", "enqueue"])

    def test_process_local_lock_no_longer_decides_cycle_acceptance(self):
        import main

        self.assertFalse(hasattr(main, "_cycle_lock"))
        background = Mock()
        accepted_at = datetime.now(timezone.utc)
        with patch("main._get_config", return_value={}), patch(
            "main.accept_run", return_value=accepted_at
        ) as accept:
            first = main.trigger_cycle(background, body={})
            second = main.trigger_cycle(background, body={})

        self.assertEqual(accept.call_count, 2)
        self.assertEqual(background.add_task.call_count, 2)
        self.assertNotEqual(first["job_id"], second["job_id"])

    def test_duplicate_acceptance_returns_409_without_enqueue(self):
        import main
        from fastapi import HTTPException
        from orchestrator import RunAcceptanceConflict

        background = Mock()
        with patch("main._get_config", return_value={}), patch("main.accept_run", side_effect=RunAcceptanceConflict("duplicate")), patch(
            "collectors.get_all_collectors", return_value={"fred": Mock()}
        ):
            with self.assertRaises(HTTPException) as raised:
                main.trigger_collector("fred", background, body={})
        self.assertEqual(raised.exception.status_code, 409)
        background.add_task.assert_not_called()

    def test_acceptance_failure_returns_controlled_error_without_enqueue(self):
        import main
        from fastapi import HTTPException

        background = Mock()
        with patch("main._get_config", return_value={}), patch("main.accept_run", side_effect=RuntimeError("db unavailable")), patch(
            "processors.get_all_processors", return_value={"briefing": Mock()}
        ):
            with self.assertRaises(HTTPException) as raised:
                main.trigger_processor("briefing", background, body={})
        self.assertEqual(raised.exception.status_code, 503)
        background.add_task.assert_not_called()

    def test_background_wrapper_finalizes_escaped_exception(self):
        import main

        with patch("main._get_config", return_value={}), patch(
            "main.start_run", return_value=True
        ), patch("main.run_collector", side_effect=RuntimeError("boom")), patch(
            "main.finalize_run_safely"
        ) as finish:
            main._run_collector_task("fred", "run-id")

        self.assertEqual(finish.call_args.args[1], "failed")

    def test_cycle_start_failure_finalizes_accepted_run_without_work(self):
        import main

        with patch("main._get_config", return_value={}), patch(
            "main.start_run", side_effect=RuntimeError("secret db failure")
        ), patch("main.run_full_cycle") as work, patch("main.finalize_run_safely") as finish:
            main._run_cycle_task("run-id")

        work.assert_not_called()
        finish.assert_called_once_with(
            "run-id", "failed",
            {"status": "failed", "reason": "run start unavailable"},
            {}, "run start unavailable", run_kind="cycle", component=None,
        )

    def test_collector_start_failure_finalizes_accepted_run_without_work(self):
        import main

        with patch("main._get_config", return_value={}), patch(
            "main.start_run", side_effect=RuntimeError("secret db failure")
        ), patch("main.run_collector") as work, patch("main.finalize_run_safely") as finish:
            main._run_collector_task("fred", "run-id")

        work.assert_not_called()
        finish.assert_called_once_with(
            "run-id", "failed",
            {"status": "failed", "reason": "run start unavailable"},
            {}, "run start unavailable", run_kind="collector", component="fred",
        )

    def test_processor_start_failure_finalizes_accepted_run_without_work(self):
        import main

        with patch("main._get_config", return_value={}), patch(
            "main.start_run", side_effect=RuntimeError("secret db failure")
        ), patch("main.run_processor") as work, patch("main.finalize_run_safely") as finish:
            main._run_processor_task("briefing", "run-id")

        work.assert_not_called()
        finish.assert_called_once_with(
            "run-id", "failed",
            {"status": "failed", "reason": "run start unavailable"},
            {}, "run start unavailable", run_kind="processor", component="briefing",
        )

    def test_lost_start_race_does_not_finish_accepted_run(self):
        import main

        with patch("main._get_config", return_value={}), patch(
            "main.start_run", return_value=False
        ), patch("main.run_collector") as work, patch("main.finalize_run_safely") as finish:
            main._run_collector_task("fred", "run-id")

        work.assert_not_called()
        finish.assert_not_called()

    def test_scheduler_collector_start_failure_finalizes_without_work(self):
        import scheduler

        with patch("scheduler.uuid4", return_value="run-id"), patch(
            "scheduler.accept_run"
        ), patch(
            "scheduler.start_run", side_effect=RuntimeError("secret db failure")
        ), patch("scheduler._run_scheduled_collector_stages") as work, patch(
            "scheduler.finalize_run_safely"
        ) as finish:
            scheduler._scheduled_collector("fred", {})

        work.assert_not_called()
        finish.assert_called_once_with(
            "run-id", "failed",
            {"status": "failed", "reason": "run start unavailable"},
            {}, "run start unavailable", run_kind="collector", component="fred",
        )

    def test_scheduler_processor_start_failure_finalizes_without_work(self):
        import scheduler

        with patch("scheduler.uuid4", return_value="run-id"), patch(
            "scheduler.accept_run"
        ), patch(
            "scheduler.start_run", side_effect=RuntimeError("secret db failure")
        ), patch("scheduler.run_processor") as work, patch(
            "scheduler.finalize_run_safely"
        ) as finish:
            scheduler._scheduled_processor("briefing", {})

        work.assert_not_called()
        finish.assert_called_once_with(
            "run-id", "failed",
            {"status": "failed", "reason": "run start unavailable"},
            {}, "run start unavailable", run_kind="processor", component="briefing",
        )

    def test_background_lock_conflict_finalizes_stable_failed_result(self):
        import main
        from locks import RunConflict

        with patch("main._get_config", return_value={}), patch(
            "main.start_run", return_value=True
        ), patch(
            "main.run_collector", side_effect=RunConflict("collector:fred")
        ), patch("main.finalize_run_safely") as finish:
            main._run_collector_task("fred", "run-id")

        self.assertEqual(
            finish.call_args.args[2],
            {
                "status": "failed",
                "reason": "run conflict: collector:fred",
                "conflict": "collector:fred",
            },
        )
        self.assertEqual(finish.call_args.args[4], "run conflict: collector:fred")

    def test_heartbeat_guard_ticks_with_owner_and_stops_on_success(self):
        import orchestrator

        events = []

        class FakeEvent:
            def __init__(self):
                self.waits = 0

            def wait(self, interval):
                events.append(("wait", interval))
                self.waits += 1
                return self.waits > 1

            def set(self):
                events.append(("stop",))

        class FakeThread:
            def __init__(self, *, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

            def join(self):
                events.append(("join",))

        with patch("orchestrator.heartbeat_run", return_value=True) as heartbeat:
            with orchestrator.maintain_run_heartbeat(
                {"jobs": {"heartbeat_interval_seconds": 7}},
                "run-id",
                "worker-1",
                event_factory=FakeEvent,
                thread_factory=FakeThread,
            ):
                events.append(("work",))

        heartbeat.assert_called_once_with(
            {"jobs": {"heartbeat_interval_seconds": 7}}, "run-id", "worker-1"
        )
        self.assertIn(("wait", 7.0), events)
        self.assertEqual(events[-2:], [("stop",), ("join",)])

    def test_heartbeat_guard_stops_on_protected_exception(self):
        import orchestrator

        event = Mock()
        event.wait.return_value = True
        thread = Mock()

        with self.assertRaisesRegex(RuntimeError, "original failure"):
            with orchestrator.maintain_run_heartbeat(
                {}, "run-id", "worker-1",
                event_factory=Mock(return_value=event),
                thread_factory=Mock(return_value=thread),
            ):
                raise RuntimeError("original failure")

        event.set.assert_called_once_with()
        thread.join.assert_called_once_with()

    def test_direct_lifecycle_starts_heartbeat_only_after_successful_claim(self):
        import orchestrator

        guard = Mock(return_value=nullcontext())
        with patch.object(orchestrator, "accept_run"), patch.object(
            orchestrator, "start_run", return_value=False
        ), patch.object(orchestrator, "maintain_run_heartbeat", guard), patch.object(
            orchestrator, "_run_collector_impl"
        ) as work:
            self.assertIsNone(orchestrator.run_collector("fred", config={}))
        guard.assert_not_called()
        work.assert_not_called()

        guard.reset_mock()
        with patch.object(orchestrator, "accept_run"), patch.object(
            orchestrator, "start_run", return_value=True
        ) as start, patch.object(
            orchestrator, "maintain_run_heartbeat", guard
        ), patch.object(
            orchestrator, "advisory_lock", side_effect=lambda *_args: nullcontext()
        ), patch.object(
            orchestrator, "_run_collector_impl", return_value={"status": "success", "error": None}
        ), patch.object(orchestrator, "finalize_run_safely", return_value=True):
            orchestrator.run_collector("fred", config={}, correlation_id="run-id")

        worker_id = start.call_args.args[2]
        guard.assert_called_once_with({}, "run-id", worker_id)

    def test_full_cycle_progress_uses_parent_worker_and_children_do_not_own_heartbeat(self):
        import orchestrator

        collector_result = {"status": "success"}
        with patch.object(orchestrator, "get_all_collectors", return_value={"fred": Mock()}), patch.object(
            orchestrator, "get_all_processors", return_value={}
        ), patch.object(orchestrator, "run_collector", return_value=collector_result) as child, patch.object(
            orchestrator, "update_run_progress"
        ) as progress:
            orchestrator._run_full_cycle_impl(
                {"collectors": {"fred": {"enabled": True}}},
                "run-id",
                manage_lifecycle=False,
                worker_id="parent-worker",
            )

        self.assertFalse(child.call_args.kwargs["manage_lifecycle"])
        self.assertTrue(progress.call_args_list)
        self.assertTrue(all(call.args[3] == "parent-worker" for call in progress.call_args_list))

    def test_safe_finalization_distinguishes_lost_owner_and_exception(self):
        import orchestrator

        with patch.object(orchestrator, "finish_run", return_value=False), patch.object(
            orchestrator, "logger"
        ) as logger:
            self.assertFalse(orchestrator.finalize_run_safely(
                "run-id", "success", {}, {}, worker_id="worker-1", run_kind="collector"
            ))
        logger.warning.assert_called_once()
        self.assertEqual(logger.warning.call_args.args[0], "run_finalization_lost_ownership")

        with patch.object(orchestrator, "finish_run", side_effect=RuntimeError("secret payload")), patch.object(
            orchestrator, "logger"
        ) as logger:
            self.assertFalse(orchestrator.finalize_run_safely(
                "run-id", "success", {}, {}, worker_id="worker-1", run_kind="collector"
            ))
        logger.error.assert_called_once()
        self.assertNotIn("secret payload", str(logger.error.call_args))

    def test_direct_finalization_failure_preserves_result_and_original_exception(self):
        import orchestrator
        from locks import RunConflict

        result = {"status": "success", "error": None}
        with patch.object(orchestrator, "accept_run"), patch.object(
            orchestrator, "start_run", return_value=True
        ), patch.object(
            orchestrator, "maintain_run_heartbeat", return_value=nullcontext()
        ), patch.object(
            orchestrator, "advisory_lock", side_effect=lambda *_args: nullcontext()
        ), patch.object(orchestrator, "_run_collector_impl", return_value=result), patch.object(
            orchestrator, "finish_run", side_effect=RuntimeError("finalize failed")
        ), patch.object(orchestrator, "logger") as logger:
            self.assertIs(orchestrator.run_collector("fred", config={}), result)
        self.assertFalse(any(call.args[0] == "collector_completed" for call in logger.info.call_args_list))

        with patch.object(orchestrator, "accept_run"), patch.object(
            orchestrator, "start_run", return_value=True
        ), patch.object(
            orchestrator, "maintain_run_heartbeat", return_value=nullcontext()
        ), patch.object(
            orchestrator, "advisory_lock", side_effect=RunConflict("collector:fred")
        ), patch.object(
            orchestrator, "finish_run", side_effect=RuntimeError("finalize failed")
        ):
            with self.assertRaisesRegex(RunConflict, "collector:fred"):
                orchestrator.run_collector("fred", config={})

    def test_http_and_scheduler_use_claim_worker_for_heartbeat_and_safe_finalize(self):
        import main
        import scheduler

        with patch("main._get_config", return_value={}), patch(
            "main.start_run", return_value=True
        ) as start, patch("main.maintain_run_heartbeat", return_value=nullcontext()) as guard, patch(
            "main.run_collector", return_value={"status": "success", "error": None}
        ), patch("main.finalize_run_safely", return_value=False), patch("main.logger") as logger:
            main._run_collector_task("fred", "run-id")
        worker_id = start.call_args.args[2]
        guard.assert_called_once_with({}, "run-id", worker_id)
        self.assertFalse(any(call.args[0] == "collector_trigger_completed" for call in logger.info.call_args_list))

        with patch("scheduler.uuid4", side_effect=["run-id", "worker-id"]), patch(
            "scheduler.accept_run"
        ), patch("scheduler.start_run", return_value=True), patch(
            "scheduler.maintain_run_heartbeat", return_value=nullcontext()
        ) as guard, patch(
            "scheduler.run_processor", side_effect=RuntimeError("original failure")
        ), patch(
            "orchestrator.finish_run", side_effect=RuntimeError("finalize failed")
        ):
            scheduler._scheduled_processor("briefing", {})
        guard.assert_called_once_with({}, "run-id", "scheduler:worker-id")

    def test_scheduled_collector_aggregates_every_stage_status_truthfully(self):
        import scheduler

        cases = [
            ({"fred": {"status": "partial"}}, "partial"),
            ({"fred": {"status": "success"}, "briefing": {"status": "failed"}}, "partial"),
            ({"fred": {"status": "failed"}, "briefing": {"status": "failed"}}, "failed"),
            ({"fred": {"status": "success"}, "briefing": {"status": "success"}}, "success"),
        ]
        for stages, expected in cases:
            with self.subTest(stages=stages), patch(
                "scheduler.uuid4", return_value="run-id"
            ), patch("scheduler.accept_run"), patch(
                "scheduler.start_run", return_value=True
            ), patch(
                "scheduler.maintain_run_heartbeat", return_value=nullcontext()
            ), patch(
                "scheduler._run_scheduled_collector_stages", return_value=stages
            ), patch("scheduler.finalize_run_safely", return_value=True) as finalize:
                scheduler._scheduled_collector("fred", {})

            self.assertEqual(finalize.call_args.args[1], expected)

    def test_full_cycle_returns_and_finalizes_truthful_aggregate_status(self):
        import orchestrator

        cases = [
            (["partial"], "partial"),
            (["success", "failed"], "partial"),
            (["failed", "failed"], "failed"),
            (["success", "success"], "success"),
            (["success", "skipped"], "success"),
            (["skipped", "skipped"], "success"),
        ]
        for statuses, expected in cases:
            collector_ids = [f"collector-{index}" for index in range(len(statuses))]
            config = {
                "collectors": {
                    collector_id: {"enabled": True} for collector_id in collector_ids
                },
                "processors": {},
            }
            results = [{"status": status} for status in statuses]
            with self.subTest(statuses=statuses), patch.object(
                orchestrator,
                "get_all_collectors",
                return_value={collector_id: Mock() for collector_id in collector_ids},
            ), patch.object(
                orchestrator, "get_all_processors", return_value={}
            ), patch.object(
                orchestrator, "run_collector", side_effect=results
            ), patch.object(orchestrator, "accept_run"), patch.object(
                orchestrator, "start_run", return_value=True
            ), patch.object(
                orchestrator, "maintain_run_heartbeat", return_value=nullcontext()
            ), patch.object(
                orchestrator, "advisory_lock", side_effect=lambda *_args: nullcontext()
            ), patch.object(
                orchestrator, "update_run_progress"
            ), patch.object(
                orchestrator, "finalize_run_safely", return_value=True
            ) as finalize:
                result = orchestrator.run_full_cycle(config=config, correlation_id="run-id")

            self.assertEqual(result["status"], expected)
            self.assertEqual(finalize.call_args.args[1], expected)

    def test_empty_full_cycle_is_a_successful_no_op(self):
        import orchestrator

        with patch.object(orchestrator, "get_all_collectors", return_value={}), patch.object(
            orchestrator, "get_all_processors", return_value={}
        ), patch.object(orchestrator, "update_run_progress"):
            result = orchestrator._run_full_cycle_impl(
                config={"collectors": {}, "processors": {}},
                correlation_id="run-id",
                manage_lifecycle=False,
            )

        self.assertEqual(result["status"], "success")


class AbandonedRunRecoveryTests(unittest.TestCase):
    """Phase 5 Task 17: restart reconciliation and explicit-only replay."""

    def test_reconciliation_uses_deterministic_stale_cutoffs_and_preserves_fresh_runs(self):
        from datetime import timedelta
        from orchestrator import reconcile_abandoned_runs

        now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
        session = Mock()
        accepted_result = Mock()
        accepted_result.scalars.return_value.all.return_value = ["stale-accepted"]
        running_result = Mock()
        running_result.scalars.return_value.all.return_value = ["stale-running"]
        session.execute.side_effect = [accepted_result, running_result]
        with patch("orchestrator.get_session") as get_session:
            get_session.return_value.__enter__.return_value = session
            report = reconcile_abandoned_runs(
                {},
                now=now,
                accepted_timeout=timedelta(minutes=10),
                heartbeat_timeout=timedelta(minutes=2),
            )

        accepted_sql, accepted_params = session.execute.call_args_list[0].args
        running_sql, running_params = session.execute.call_args_list[1].args
        self.assertIn("status = 'accepted'", str(accepted_sql))
        self.assertIn("accepted_at < :cutoff", str(accepted_sql))
        self.assertIn("status = 'running'", str(running_sql))
        self.assertIn("COALESCE(heartbeat_at, started_at) < :cutoff", str(running_sql))
        self.assertEqual(accepted_params["cutoff"], now - timedelta(minutes=10))
        self.assertEqual(running_params["cutoff"], now - timedelta(minutes=2))
        self.assertEqual(report["accepted_ids"], ["stale-accepted"])
        self.assertEqual(report["running_ids"], ["stale-running"])
        self.assertEqual(report["total"], 2)

    def test_reconciliation_updates_only_nonterminal_states_with_stable_reason(self):
        from orchestrator import reconcile_abandoned_runs

        session = Mock()
        result = Mock()
        result.scalars.return_value.all.return_value = []
        session.execute.side_effect = [result, result]
        with patch("orchestrator.get_session") as get_session:
            get_session.return_value.__enter__.return_value = session
            reconcile_abandoned_runs({}, now=datetime(2026, 7, 13, tzinfo=timezone.utc))

        for execute_call in session.execute.call_args_list:
            sql, params = execute_call.args
            self.assertIn("status = :abandoned", str(sql))
            self.assertNotIn("completed", str(sql).split("WHERE")[-1])
            self.assertIn("restart reconciliation", params["reason"])

    def test_startup_reconciles_before_scheduler_starts(self):
        import main

        events = []
        with patch("main._get_config", return_value={}), patch(
            "main.setup_logging"
        ), patch("main.check_connection", return_value=True), patch(
            "main.reconcile_abandoned_runs", side_effect=lambda *a, **k: events.append("reconcile") or {}
        ), patch("main.start_scheduler", side_effect=lambda config: events.append("scheduler")), patch.object(
            main.quote_stream, "start"
        ):
            main.on_startup()

        self.assertEqual(events, ["reconcile", "scheduler"])

    def test_retry_abandoned_accepts_new_run_before_enqueue(self):
        import main

        events = []
        background = Mock()
        background.add_task.side_effect = lambda *args: events.append(("enqueue", args[0]))
        old = {
            "correlation_id": "11111111-1111-4111-8111-111111111111",
            "status": "abandoned",
            "run_kind": "collector",
            "requested_component": "fred",
        }
        with patch("main._get_config", return_value={}), patch(
            "main.get_run_for_retry", return_value=old
        ), patch(
            "collectors.get_all_collectors", return_value={"fred": Mock()}
        ), patch(
            "main.accept_run", side_effect=lambda *args, **kwargs: events.append(("accept", args[1])) or datetime.now(timezone.utc)
        ):
            response = main.retry_abandoned_run("11111111-1111-4111-8111-111111111111", background)

        self.assertNotEqual(response["job_id"], "11111111-1111-4111-8111-111111111111")
        self.assertEqual(events[0], ("accept", response["job_id"]))
        self.assertEqual(events[1], ("enqueue", main._run_collector_task))

    def test_retry_removed_component_is_rejected_before_acceptance_or_enqueue(self):
        import main
        from fastapi import HTTPException

        cases = [("collector", "collectors"), ("processor", "processors")]
        for run_kind, registry_module in cases:
            old = {
                "status": "abandoned",
                "run_kind": run_kind,
                "requested_component": "removed-component",
            }
            background = Mock()
            with self.subTest(run_kind=run_kind), patch(
                "main._get_config", return_value={}
            ), patch("main.get_run_for_retry", return_value=old), patch(
                f"{registry_module}.get_all_{registry_module}", return_value={}
            ), patch("main.accept_run") as accept:
                with self.assertRaises(HTTPException) as raised:
                    main.retry_abandoned_run(
                        "11111111-1111-4111-8111-111111111111", background
                    )

            self.assertEqual(raised.exception.status_code, 409)
            self.assertIn("no longer available", raised.exception.detail)
            accept.assert_not_called()
            background.add_task.assert_not_called()

    def test_retry_registered_processor_is_accepted_and_enqueued(self):
        import main

        background = Mock()
        old = {
            "status": "abandoned",
            "run_kind": "processor",
            "requested_component": "briefing",
        }
        with patch("main._get_config", return_value={}), patch(
            "main.get_run_for_retry", return_value=old
        ), patch(
            "processors.get_all_processors", return_value={"briefing": Mock()}
        ) as registry, patch(
            "main.accept_run", return_value=datetime.now(timezone.utc)
        ) as accept:
            main.retry_abandoned_run(
                "11111111-1111-4111-8111-111111111111", background
            )

        registry.assert_called_once_with()
        accept.assert_called_once()
        self.assertIs(background.add_task.call_args.args[0], main._run_processor_task)
        self.assertEqual(background.add_task.call_args.args[1], "briefing")

    def test_retry_registered_news_source_is_accepted_and_enqueued(self):
        import main

        background = Mock()
        old = {
            "status": "abandoned",
            "run_kind": "news",
            "requested_component": "reuters",
        }
        with patch("main._get_config", return_value={}), patch(
            "main.get_run_for_retry", return_value=old
        ), patch(
            "main.accept_run", return_value=datetime.now(timezone.utc)
        ) as accept:
            response = main.retry_abandoned_run(
                "11111111-1111-4111-8111-111111111111", background
            )

        accept.assert_called_once()
        self.assertIn("job_id", response)
        self.assertIs(background.add_task.call_args.args[0], main._run_news_task)
        self.assertEqual(background.add_task.call_args.args[1], "reuters")

    def test_retry_invalid_states_do_not_accept_or_enqueue(self):
        import main
        from fastapi import HTTPException

        cases = [
            (None, 404),
            ({"status": "completed", "run_kind": "cycle", "requested_component": None}, 409),
            ({"status": "abandoned", "run_kind": "mystery", "requested_component": None}, 409),
        ]
        for old, expected in cases:
            background = Mock()
            with self.subTest(old=old), patch("main._get_config", return_value={}), patch(
                "main.get_run_for_retry", return_value=old
            ), patch("main.accept_run") as accept:
                with self.assertRaises(HTTPException) as raised:
                    main.retry_abandoned_run("11111111-1111-4111-8111-111111111111", background)
                self.assertEqual(raised.exception.status_code, expected)
                accept.assert_not_called()
                background.add_task.assert_not_called()

    def test_retry_lookup_failure_returns_503_without_accept_or_enqueue(self):
        import main
        from fastapi import HTTPException

        background = Mock()
        with patch("main._get_config", return_value={}), patch(
            "main.get_run_for_retry", side_effect=RuntimeError("secret db failure")
        ) as lookup, patch("main.accept_run") as accept:
            with self.assertRaises(HTTPException) as raised:
                main.retry_abandoned_run(
                    "11111111-1111-4111-8111-111111111111", background
                )

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail, "Run lookup unavailable")
        lookup.assert_called_once()
        accept.assert_not_called()
        background.add_task.assert_not_called()

    def test_retry_malformed_uuid_returns_422_without_lookup_or_accept(self):
        import main
        from fastapi.testclient import TestClient

        with patch("main._get_config", return_value={}), patch(
            "main.get_run_for_retry"
        ) as lookup, patch("main.accept_run") as accept:
            response = TestClient(main.app, headers=INTERNAL_AUTH).post("/runs/not-a-uuid/retry")

        self.assertEqual(response.status_code, 422)
        lookup.assert_not_called()
        accept.assert_not_called()


class CollectionFailureStatusTests(unittest.TestCase):
    """Task 9: Propagate collection and persistence failures into run status."""

    def setUp(self):
        from main import app
        from fastapi.testclient import TestClient
        self.client = TestClient(app, headers=INTERNAL_AUTH)
        self.lock_patcher = patch(
            "orchestrator.advisory_lock", side_effect=lambda *_args: nullcontext()
        )
        self.lock_patcher.start()
        self.addCleanup(self.lock_patcher.stop)
        self.config = {
            "database": {"host": "localhost", "port": 5432, "name": "test",
                         "user": "test", "password": "test"},
            "collectors": {"fred": {"enabled": True, "schedule": "0 6 * * *",
                                     "api_key": "test", "series": [
                                         {"id": "GDP", "frequency": "quarterly"},
                                         {"id": "CPI", "frequency": "monthly"},
                                     ]}},
            "processors": {},
        }

    @patch("orchestrator.get_collector")
    @patch("orchestrator.get_session")
    def test_all_series_fail_yields_failed_status(self, get_session, get_collector):
        """All FRED series fail → collector status 'failed'."""
        from collectors.base import CollectionResult

        session = Mock()
        get_session.return_value.__enter__.return_value = session
        session.execute.return_value = None

        mock_collector = Mock()
        mock_collector.get_target_table.return_value = "macro_series"
        mock_collector.get_conflict_columns.return_value = ["series_id", "observed_at"]
        # Return CollectionResult with all series failed (Task 9)
        mock_collector.collect.return_value = CollectionResult(
            records=[],
            errors=[
                {"series_id": "GDP", "error": "Connection refused", "frequency": "quarterly"},
                {"series_id": "CPI", "error": "Connection refused", "frequency": "monthly"},
            ],
            total_series=2,
            successful_series=0,
        )
        get_collector.return_value = mock_collector

        from orchestrator import run_collector
        result = run_collector(
            "fred", config=self.config, correlation_id="test-cid", manage_lifecycle=False
        )

        self.assertEqual(result["status"], "failed",
                         "All series failed → status should be 'failed'")

    @patch("orchestrator.get_collector")
    @patch("orchestrator.upsert_records")
    @patch("orchestrator.get_session")
    def test_some_series_fail_yields_partial_status(self, get_session,
                                                     upsert_records,
                                                     get_collector):
        """Some FRED series fail → collector status 'partial'."""
        from collectors.base import CollectionResult
        from db import WriteResult

        session = Mock()
        get_session.return_value.__enter__.return_value = session

        mock_collector = Mock()
        mock_collector.get_target_table.return_value = "macro_series"
        mock_collector.get_conflict_columns.return_value = ["series_id", "observed_at"]
        # Return CollectionResult with partial failure (GDP succeeded, CPI failed)
        mock_collector.collect.return_value = CollectionResult(
            records=[{"series_id": "GDP", "value": 1.0}],
            errors=[{"series_id": "CPI", "error": "Connection refused", "frequency": "monthly"}],
            total_series=2,
            successful_series=1,
        )
        get_collector.return_value = mock_collector

        # DB writes succeed for the fetched records
        upsert_records.return_value = WriteResult(
            attempted=1, written=1, failed=0, errors=()
        )

        from orchestrator import run_collector
        result = run_collector(
            "fred", config=self.config, correlation_id="test-cid", manage_lifecycle=False
        )
        self.assertEqual(result["status"], "partial",
                         "Partial collection failure → status should be 'partial'")

    @patch("orchestrator.get_collector")
    @patch("orchestrator.upsert_records")
    @patch("orchestrator.get_session")
    def test_records_fetched_but_all_writes_fail_yields_failed(self, get_session,
                                                                upsert_records,
                                                                get_collector):
        """Records fetched but every DB write fails → status 'failed'."""
        from db import WriteResult

        session = Mock()
        get_session.return_value.__enter__.return_value = session

        mock_collector = Mock()
        mock_collector.get_target_table.return_value = "macro_series"
        mock_collector.get_conflict_columns.return_value = ["series_id", "observed_at"]
        mock_collector.collect.return_value = [
            {"series_id": "GDP", "observed_at": "2024-01-01", "value": 1.0}
        ]
        get_collector.return_value = mock_collector

        upsert_records.return_value = WriteResult(
            attempted=1, written=0, failed=1, errors=("write error",)
        )

        from orchestrator import run_collector
        result = run_collector(
            "fred", config=self.config, correlation_id="test-cid", manage_lifecycle=False
        )
        self.assertEqual(result["status"], "failed",
                         "Status should be 'failed' when records fetched but none written")

    @patch("orchestrator.get_collector")
    @patch("orchestrator.upsert_records")
    @patch("orchestrator.get_session")
    def test_some_writes_fail_yields_partial_status(self, get_session,
                                                     upsert_records,
                                                     get_collector):
        """Some DB writes fail → status 'partial'."""
        from db import WriteResult

        session = Mock()
        get_session.return_value.__enter__.return_value = session

        mock_collector = Mock()
        mock_collector.get_target_table.return_value = "macro_series"
        mock_collector.get_conflict_columns.return_value = ["series_id", "observed_at"]
        mock_collector.collect.return_value = [
            {"series_id": "GDP", "observed_at": "2024-01-01", "value": 1.0},
            {"series_id": "CPI", "observed_at": "2024-01-01", "value": 2.0},
        ]
        get_collector.return_value = mock_collector

        upsert_records.return_value = WriteResult(
            attempted=2, written=1, failed=1, errors=("write error",)
        )

        from orchestrator import run_collector
        result = run_collector(
            "fred", config=self.config, correlation_id="test-cid", manage_lifecycle=False
        )
        self.assertEqual(result["status"], "partial",
                         "Status should be 'partial' when some but not all records written")


class RuntimeFeatureTests(unittest.TestCase):
    def tearDown(self):
        stop_scheduler()

    def test_model_resolution_is_provider_model_agnostic(self):
        config = {
            "llm": {
                "default_model": "deepseek/deepseek-v4-flash",
                "models": {"briefing": "provider/custom-model"},
            }
        }
        self.assertEqual(resolve_model(config), "deepseek/deepseek-v4-flash")
        self.assertEqual(
            resolve_model(config, processor_id="briefing"),
            "provider/custom-model",
        )
        self.assertEqual(
            resolve_model(config, processor_id="briefing", model="explicit/model"),
            "explicit/model",
        )

    def test_config_env_substitution_supports_defaults_and_explicit_empty_values(self):
        from config_loader import reload_config

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(
                "required: ${REQUIRED_VALUE}\n"
                "defaulted: ${ABSENT_VALUE:-fallback}\n"
                "explicit_empty: ${EMPTY_VALUE:-fallback}\n"
            )
            with patch.dict(
                os.environ,
                {"REQUIRED_VALUE": "configured", "EMPTY_VALUE": ""},
                clear=True,
            ):
                config = reload_config(str(config_path))

        self.assertEqual(config["required"], "configured")
        self.assertEqual(config["defaulted"], "fallback")
        self.assertEqual(config["explicit_empty"], "")

    def test_config_env_substitution_names_absent_required_variable(self):
        from config_loader import reload_config

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text("required: ${TRULY_REQUIRED}\n")
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "TRULY_REQUIRED"):
                    reload_config(str(config_path))

    def test_config_env_substitution_rejects_blank_required_variable(self):
        from config_loader import reload_config

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text("required: ${BLANK_REQUIRED}\n")
            with patch.dict(os.environ, {"BLANK_REQUIRED": ""}, clear=True):
                with self.assertRaisesRegex(ValueError, "BLANK_REQUIRED"):
                    reload_config(str(config_path))

    def test_demo_config_loads_without_twitter_credential(self):
        from config_loader import reload_config

        config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
        demo_env = {
            "DEMO_MODE": "true",
        }
        with patch.dict(os.environ, demo_env, clear=True):
            config = reload_config(str(config_path))

        self.assertTrue(config["demo"]["enabled"])
        self.assertEqual(config["kobeissi"]["api_key"], "")
        self.assertEqual(config["database"]["name"], "trading_data")

    def test_enabled_production_source_missing_credential_fails_closed(self):
        from config_loader import reload_config

        config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
        production_env = {
            "DB_USER": "trading",
            "DB_PASSWORD": "password",
            "OPENROUTER_API_KEY": "key",
            "OPENROUTER_MODEL": "provider/model",
            "OANDA_API_KEY": "key",
            "TWITTERAPI_KEY": "",
            "DASHBOARD_USER": "admin",
            "DASHBOARD_PASSWORD": "password",
        }
        with patch.dict(os.environ, production_env, clear=True):
            with self.assertRaisesRegex(ValueError, "FRED_API_KEY"):
                reload_config(str(config_path))

    def test_enabled_production_sources_blank_credentials_fail_closed(self):
        from config_loader import reload_config

        config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
        base_env = {
            "DB_USER": "trading",
            "DB_PASSWORD": "password",
            "FRED_API_KEY": "configured-fred",
            "OPENROUTER_API_KEY": "configured-openrouter",
            "OPENROUTER_MODEL": "provider/model",
            "OANDA_API_KEY": "configured-oanda",
            "TWITTERAPI_KEY": "",
            "DASHBOARD_USER": "admin",
            "DASHBOARD_PASSWORD": "password",
        }
        for variable in ("FRED_API_KEY", "OANDA_API_KEY", "OPENROUTER_API_KEY"):
            with self.subTest(variable=variable):
                env = {**base_env, variable: ""}
                with patch.dict(os.environ, env, clear=True):
                    with self.assertRaisesRegex(ValueError, variable):
                        reload_config(str(config_path))

    @patch("collectors.oanda.make_request")
    def test_oanda_filters_unsupported_instruments(self, make_request):
        response = Mock()
        response.json.return_value = {"instruments": [{"name": "EUR_USD"}]}
        response.raise_for_status.return_value = None
        make_request.return_value = response

        result = OandaCollector()._filter_supported_instruments(
            "https://api-fxpractice.oanda.com",
            "token",
            "account",
            [
                {"symbol": "EURUSD", "oanda_instrument": "EUR_USD"},
                {"symbol": "OLD", "oanda_instrument": "OLD_NAME"},
            ],
            "correlation",
        )

        self.assertEqual([item["symbol"] for item in result], ["EURUSD"])
        self.assertTrue(make_request.call_args.kwargs["follow_redirects"])

    def test_named_sunday_schedule_fires_on_sunday_utc(self):
        from scheduler import _build_cron_trigger

        trigger = _build_cron_trigger("0 20 * * sun")
        monday = datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc)

        self.assertEqual(
            trigger.get_next_fire_time(None, monday),
            datetime(2026, 7, 19, 20, 0, tzinfo=timezone.utc),
        )

    def test_legacy_numeric_sunday_schedules_fire_on_sunday_utc(self):
        from scheduler import _build_cron_trigger

        monday = datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc)
        expected = datetime(2026, 7, 19, 20, 0, tzinfo=timezone.utc)
        for weekday in ("0", "7"):
            with self.subTest(weekday=weekday):
                trigger = _build_cron_trigger(f"0 20 * * {weekday}")
                self.assertEqual(trigger.get_next_fire_time(None, monday), expected)

    def test_simple_posix_numeric_weekday_is_mapped_explicitly(self):
        from scheduler import _build_cron_trigger

        monday = datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc)
        trigger = _build_cron_trigger("0 20 * * 1")

        self.assertEqual(
            trigger.get_next_fire_time(None, monday),
            datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc),
        )

    def test_legacy_posix_numeric_weekday_range_maps_to_monday_through_friday(self):
        from scheduler import _build_cron_trigger

        monday = datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc)
        trigger = _build_cron_trigger("0 6,12,18 * * 1-5")

        self.assertEqual(
            trigger.get_next_fire_time(None, monday),
            datetime(2026, 7, 13, 6, 0, tzinfo=timezone.utc),
        )

    def test_wildcard_and_named_weekdays_are_preserved(self):
        from scheduler import _build_cron_trigger

        monday = datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            _build_cron_trigger("0 20 * * *").get_next_fire_time(None, monday),
            datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc),
        )
        for weekday in ("mon", "mon-fri"):
            with self.subTest(weekday=weekday):
                self.assertEqual(
                    _build_cron_trigger(
                        f"0 20 * * {weekday}"
                    ).get_next_fire_time(None, monday),
                    datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc),
                )

    def test_comma_separated_posix_numeric_weekdays_are_mapped_explicitly(self):
        from scheduler import _build_cron_trigger

        monday = datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc)
        trigger = _build_cron_trigger("0 20 * * 2,4,6")

        self.assertEqual(
            trigger.get_next_fire_time(None, monday),
            datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc),
        )

    def test_unsafe_numeric_weekday_expressions_are_rejected_actionably(self):
        from scheduler import _build_cron_trigger

        for weekday in ("*/2", "1-5/2", "5-1", "1-", "mon,1"):
            with self.subTest(weekday=weekday):
                with self.assertRaisesRegex(ValueError, "named weekdays|named weekday"):
                    _build_cron_trigger(f"0 20 * * {weekday}")

    def test_active_enabled_config_schedules_build_and_oanda_uses_named_weekdays(self):
        import yaml

        from scheduler import _build_cron_trigger

        config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
        config = yaml.safe_load(config_path.read_text())

        self.assertEqual(
            config["collectors"]["oanda"]["schedule"],
            "0 6,12,18 * * mon-fri",
        )
        for section in ("collectors", "processors"):
            for component, component_config in config.get(section, {}).items():
                schedule = component_config.get("schedule")
                enabled = component_config.get("enabled", section == "collectors")
                if enabled and schedule and schedule != "after_dependency":
                    with self.subTest(
                        section=section, component=component, schedule=schedule
                    ):
                        _build_cron_trigger(schedule)

    def test_scheduler_registers_configured_cron_jobs(self):
        config = {
            "collectors": {"fred": {"enabled": True, "schedule": "0 6 * * *"}},
            "processors": {
                "briefing": {"enabled": True, "schedule": "0 7 * * *"},
                "macro_regime": {"enabled": True, "schedule": "after_dependency"},
            },
        }

        start_scheduler(config)
        ids = {job["id"] for job in scheduler_status()["jobs"]}

        self.assertEqual(ids, {"collector:fred", "processor:briefing"})

    @patch("scheduler.BackgroundScheduler")
    def test_filings_job_runs_immediately_on_scheduler_start(self, scheduler_factory):
        scheduler = Mock()
        scheduler.running = False
        scheduler.get_jobs.return_value = []
        scheduler_factory.return_value = scheduler
        before = datetime.now(timezone.utc)

        start_scheduler({
            "collectors": {},
            "processors": {},
            "investment_filings": {
                "enabled": True,
                "schedule": "0 8 * * 1-5",
                "run_on_startup": True,
            },
        })

        next_run_time = scheduler.add_job.call_args.kwargs["next_run_time"]
        self.assertGreaterEqual(next_run_time, before)
        self.assertLessEqual(next_run_time, datetime.now(timezone.utc))

    def test_scheduler_registers_enabled_news_but_not_disabled_paid_source(self):
        config = {
            "collectors": {}, "processors": {},
            "reuters": {"enabled": True, "schedule_enabled": True, "schedule": "15 */6 * * *"},
            "kobeissi": {"enabled": True, "schedule_enabled": False, "schedule": "20 */6 * * *"},
        }
        start_scheduler(config)
        ids = {job["id"] for job in scheduler_status()["jobs"]}
        self.assertEqual(ids, {"news:reuters"})

    def test_demo_mode_registers_no_news_jobs(self):
        config = {
            "demo": {"enabled": True}, "collectors": {}, "processors": {},
            "reuters": {"enabled": True, "schedule_enabled": True, "schedule": "15 */2 * * *"},
            "kobeissi": {"enabled": True, "schedule_enabled": True, "schedule": "20 */6 * * *"},
        }
        start_scheduler(config)
        ids = {job["id"] for job in scheduler_status()["jobs"]}
        self.assertEqual(ids, set())

    def test_run_news_source_returns_truthful_safe_summary(self):
        from sources.news_result import NewsCollectionResult
        from orchestrator import run_news_source

        config = {"news_feed": {"output_path": "unused"}, "reuters": {"enabled": True}}
        with patch("orchestrator.advisory_lock", return_value=nullcontext()) as lock, patch(
            "sources.news_feed.collect_and_publish",
            return_value=NewsCollectionResult([{"id": "one"}], "ok"),
        ):
            result = run_news_source("reuters", "cid-1", config, manage_lifecycle=False)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["new_item_count"], 1)
        self.assertEqual(result["correlation_id"], "cid-1")
        self.assertEqual(result["state"], "published")
        self.assertTrue(result["feed_published"])
        lock.assert_called_once_with("news:reuters", config)

    def test_state_failure_after_feed_publication_is_truthfully_reported(self):
        from sources.news_result import NewsCollectionResult, NewsPublication
        from orchestrator import run_news_source

        publication = NewsPublication(Path("snapshot"), Path("state"), {"cursor": "advanced"})
        outcome = NewsCollectionResult(
            [{"id": "one"}], "error", "News state persistence failed: OSError",
            publication, True,
        )
        config = {"news_feed": {"output_path": "unused"}, "reuters": {"enabled": True}}
        with patch("orchestrator.advisory_lock", return_value=nullcontext()), patch(
            "sources.news_feed.collect_and_publish", return_value=outcome
        ):
            result = run_news_source("reuters", "cid-state", config, manage_lifecycle=False)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["state"], "publication_failed")
        self.assertEqual(result["code"], "news_publication_failed")
        self.assertTrue(result["feed_published"])
        self.assertEqual(result["new_item_count"], 1)

    def test_background_news_lock_conflict_finalizes_failed(self):
        import main
        from locks import RunConflict

        with patch("main._get_config", return_value={}), patch(
            "main.start_run", return_value=True
        ), patch("main.maintain_run_heartbeat", return_value=nullcontext()), patch(
            "main.run_news_source", side_effect=RunConflict("news:reuters")
        ), patch("main.finalize_run_safely") as finalize:
            main._run_news_task("reuters", "run-id")

        self.assertEqual(finalize.call_args.args[1], "failed")
        self.assertEqual(finalize.call_args.args[2]["code"], "news_run_conflict")
        self.assertEqual(finalize.call_args.kwargs["run_kind"], "news")

    def test_demo_fixture_is_public_safe_and_deterministic(self):
        seed_path = (
            Path(__file__).resolve().parents[2]
            / "db"
            / "demo"
            / "900_demo_seed.sql"
        )
        if not seed_path.exists():
            self.skipTest("Demo fixtures are intentionally public-repository only")
        seed = seed_path.read_text()

        self.assertIn("Fictional deterministic fixtures", seed)
        self.assertIn("77777777-7777-4777-8777-777777777777", seed)
        self.assertNotIn("OPENROUTER_API_KEY", seed)

    @patch("orchestrator.get_session")
    def test_cycle_progress_is_persisted_while_run_is_active(self, get_session):
        session = Mock()
        get_session.return_value.__enter__.return_value = session

        update_run_progress(
            "run-id",
            {"current_stage": "fred", "completed_stages": 0, "total_stages": 3},
            {},
        )

        params = session.execute.call_args.args[1]
        self.assertEqual(params["cid"], "run-id")
        self.assertIn('"current_stage": "fred"', params["summary"])


# ═════════════════════════════════════════════════════════════════════════════
# Task 12: Orchestrator health contract tests
# ═════════════════════════════════════════════════════════════════════════════

class HealthContractTests(unittest.TestCase):
    """Task 12: Orchestrator /health and /quality endpoints."""

    def setUp(self):
        from main import app
        from fastapi.testclient import TestClient
        self.client = TestClient(app, headers=INTERNAL_AUTH)

    @patch("main.run_quality_checks", return_value={})
    @patch("main.check_connection", return_value=True)
    @patch("main.get_last_collection_runs", return_value=[])
    @patch("main._get_config")
    def test_health_returns_separated_healthy_contract(
        self, mock_get_config, _mock_runs, _mock_db, _mock_quality
    ):
        mock_get_config.return_value = {"logging": {"level": "INFO"}}
        resp = self.client.get("/health")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["liveness"], "ok")
        self.assertEqual(data["readiness"], "ready")
        self.assertEqual(data["data_health"], "healthy")
        self.assertEqual(data["status"], "healthy")
        self.assertIsInstance(data["components"], list)
        self.assertIn("scheduler", data)
        self.assertIn("stream", data)
        self.assertIn("collectors", data)

    @patch("main.check_connection", return_value=False)
    @patch("main._get_config", return_value={})
    def test_health_returns_503_when_database_is_unavailable(self, _config, _db):
        resp = self.client.get("/health")

        self.assertEqual(resp.status_code, 503)
        data = resp.json()
        self.assertEqual(data["liveness"], "ok")
        self.assertEqual(data["readiness"], "unready")
        self.assertEqual(data["data_health"], "degraded")
        db_component = next(c for c in data["components"] if c["name"] == "database")
        self.assertEqual(db_component["status"], "unavailable")
        self.assertTrue(db_component["critical"])

    @patch("main.run_quality_checks", return_value={
        "fred_DGS10_freshness": {"healthy": False, "detail": "stale", "source_id": "fred"}
    })
    @patch("main.check_connection", return_value=True)
    @patch("main.get_last_collection_runs", return_value=[])
    @patch("main._get_config", return_value={})
    def test_stale_data_keeps_readiness_ready_and_http_200(
        self, _config, _runs, _db, _quality
    ):
        resp = self.client.get("/health")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["liveness"], "ok")
        self.assertEqual(data["readiness"], "ready")
        self.assertEqual(data["data_health"], "degraded")
        self.assertEqual(data["status"], "degraded")

    @patch("main._get_config")
    @patch("main.run_quality_checks", return_value={})
    def test_quality_returns_overall_and_checks_with_empty_registry(self, _runner, mock_get_config):
        """GET /quality returns {overall, checks} even with no checks registered."""
        mock_get_config.return_value = {"logging": {"level": "INFO"}}
        resp = self.client.get("/quality")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("overall", data)
        self.assertIn("checks", data)
        self.assertEqual(data["overall"], "healthy")
        self.assertIsInstance(data["checks"], dict)

    @patch("main._get_config")
    @patch("main.run_quality_checks")
    def test_quality_endpoint_uses_production_runner(self, runner, mock_get_config):
        config = {"collectors": {"fred": {"series": [{"id": "DGS10", "frequency": "daily"}]}}}
        mock_get_config.return_value = config
        runner.return_value = {
            "fred_DGS10_freshness": {"healthy": True, "source_id": "fred"}
        }

        resp = self.client.get("/quality")

        self.assertEqual(resp.status_code, 200)
        runner.assert_called_once_with(config)
        self.assertIn("fred_DGS10_freshness", resp.json()["checks"])

    @patch("main._get_config")
    @patch("main.run_quality_checks", return_value={})
    def test_quality_checks_is_dict(self, _runner, mock_get_config):
        """GET /quality checks key is always a dict."""
        mock_get_config.return_value = {"logging": {"level": "INFO"}}
        resp = self.client.get("/quality")
        data = resp.json()
        self.assertIsInstance(data["checks"], dict,
                              "Quality checks must be a dict for consumers to iterate with .items()")


if __name__ == "__main__":
    unittest.main()
