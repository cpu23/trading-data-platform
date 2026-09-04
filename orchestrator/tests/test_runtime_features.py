import os
import sys
import tempfile
import unittest
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from collectors.oanda import OandaCollector
from llm_client import resolve_model
from scheduler import scheduler_status, start_scheduler, stop_scheduler

from orchestrator import update_run_progress


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
    def test_lost_start_race_prevents_collector_work(
        self, accept, start, get_collector
    ):
        from orchestrator import run_collector

        result = run_collector("fred", config={})

        accept.assert_called_once()
        start.assert_called_once()
        get_collector.assert_not_called()
        self.assertIsNone(result)

    def test_direct_collector_start_failure_finalizes_accepted_run_without_owner(self):
        import orchestrator

        original = RuntimeError("secret db failure")
        with (
            patch.object(orchestrator, "accept_run"),
            patch.object(orchestrator, "start_run", side_effect=original),
            patch.object(orchestrator, "_run_collector_impl") as work,
            patch.object(orchestrator, "finalize_run_safely") as finalize,
        ):
            with self.assertRaises(RuntimeError) as raised:
                orchestrator.run_collector("fred", config={}, correlation_id="run-id")

        self.assertIs(raised.exception, original)
        work.assert_not_called()
        finalize.assert_called_once_with(
            "run-id",
            "failed",
            {
                "status": "failed",
                "reason": "run start unavailable",
                "error_class": "unknown",
                "retryable": False,
            },
            {},
            "run start unavailable",
            worker_id=None,
            run_kind="collector",
            component="fred",
        )

    def test_direct_cycle_start_failure_finalizes_accepted_run_without_owner(self):
        import orchestrator

        original = RuntimeError("secret db failure")
        with (
            patch.object(orchestrator, "accept_run"),
            patch.object(orchestrator, "start_run", side_effect=original),
            patch.object(orchestrator, "_run_full_cycle_impl") as work,
            patch.object(orchestrator, "finalize_run_safely") as finalize,
        ):
            with self.assertRaises(RuntimeError) as raised:
                orchestrator.run_full_cycle(config={}, correlation_id="run-id")

        self.assertIs(raised.exception, original)
        work.assert_not_called()
        finalize.assert_called_once_with(
            "run-id",
            "failed",
            {
                "status": "failed",
                "reason": "run start unavailable",
                "error_class": "unknown",
                "retryable": False,
            },
            {},
            "run start unavailable",
            worker_id=None,
            run_kind="cycle",
        )

    def test_direct_processor_start_failure_finalizes_accepted_run_without_owner(self):
        import orchestrator

        original = RuntimeError("secret db failure")
        with (
            patch.object(orchestrator, "accept_run"),
            patch.object(orchestrator, "start_run", side_effect=original),
            patch.object(orchestrator, "_run_processor_impl") as work,
            patch.object(orchestrator, "finalize_run_safely") as finalize,
        ):
            with self.assertRaises(RuntimeError) as raised:
                orchestrator.run_processor(
                    "briefing", config={}, correlation_id="run-id"
                )

        self.assertIs(raised.exception, original)
        work.assert_not_called()
        finalize.assert_called_once_with(
            "run-id",
            "failed",
            {
                "status": "failed",
                "reason": "run start unavailable",
                "error_class": "unknown",
                "retryable": False,
            },
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
        with (
            patch("orchestrator.accept_run"),
            patch("orchestrator.start_run", return_value=True),
            patch("orchestrator.finish_run"),
            patch("orchestrator.update_run_progress"),
            patch(
                "orchestrator.advisory_lock", side_effect=lambda *_args: nullcontext()
            ),
        ):
            run_full_cycle(config={"collectors": {"fred": {"enabled": True}}})

        self.assertFalse(run_collector.call_args.kwargs["manage_lifecycle"])

    def test_scheduler_collector_enqueues_without_inline_execution(self):
        import scheduler

        with (
            patch("scheduler.uuid4", return_value="run-id"),
            patch("scheduler.accept_and_enqueue_operation") as accept,
            patch("scheduler.datetime") as scheduler_datetime,
        ):
            scheduler_datetime.now.return_value = datetime(
                2026, 8, 11, 6, 0, tzinfo=UTC
            )
            scheduler._scheduled_collector("fred", {"processors": {}})

        accept.assert_called_once()
        self.assertEqual(accept.call_args.kwargs["run_kind"], "collector")
        self.assertEqual(accept.call_args.kwargs["requested_component"], "fred")
        self.assertEqual(accept.call_args.kwargs["triggered_by"], "scheduler")
        self.assertEqual(
            accept.call_args.kwargs["payload"],
            {"run_dependents": True, "mode": "refresh"},
        )
        self.assertIn("collector:fred:", accept.call_args.kwargs["dedupe_key"])

    def test_scheduler_processor_enqueues_without_inline_execution(self):
        import scheduler

        with (
            patch("scheduler.uuid4", return_value="run-id"),
            patch("scheduler.accept_and_enqueue_operation") as accept,
            patch("scheduler.datetime") as scheduler_datetime,
        ):
            scheduler_datetime.now.return_value = datetime(
                2026, 8, 11, 6, 0, tzinfo=UTC
            )
            scheduler._scheduled_processor("briefing", {})

        accept.assert_called_once()
        self.assertEqual(accept.call_args.kwargs["run_kind"], "processor")
        self.assertEqual(accept.call_args.kwargs["requested_component"], "briefing")
        self.assertEqual(accept.call_args.kwargs["payload"], {"mode": "refresh"})

    def test_scheduler_enqueue_failure_is_bounded_and_does_not_raise(self):
        import scheduler

        with (
            patch("scheduler.uuid4", return_value="run-id"),
            patch(
                "scheduler.accept_and_enqueue_operation",
                side_effect=RuntimeError("db unavailable"),
            ),
            patch("scheduler.datetime") as scheduler_datetime,
        ):
            scheduler_datetime.now.return_value = datetime(
                2026, 8, 11, 6, 0, tzinfo=UTC
            )
            scheduler._scheduled_collector("fred", {})

    def test_duplicate_scheduled_enqueue_is_suppressed_not_double_run(self):
        import scheduler

        first = (
            datetime(2026, 8, 11, 6, 0, tzinfo=UTC),
            Mock(inserted=True, suppressed=False),
        )
        second = (
            datetime(2026, 8, 11, 6, 1, tzinfo=UTC),
            Mock(inserted=False, suppressed=True),
        )
        with (
            patch("scheduler.uuid4", side_effect=["run-1", "run-2"]),
            patch(
                "scheduler.accept_and_enqueue_operation",
                side_effect=[first, second],
            ) as accept,
            patch("scheduler.datetime") as scheduler_datetime,
        ):
            scheduler_datetime.now.return_value = datetime(
                2026, 8, 11, 6, 0, tzinfo=UTC
            )
            scheduler._scheduled_collector("fred", {})
            scheduler._scheduled_collector("fred", {})

        self.assertEqual(accept.call_count, 2)
        # Both fires went through the durable enqueue path; no inline work.
        self.assertEqual(
            accept.call_args_list[0].kwargs["dedupe_key"],
            accept.call_args_list[1].kwargs["dedupe_key"],
        )

    def test_heartbeat_guard_ticks_with_owner_and_stops_on_success(self):
        import orchestrator

        events = []
        config = {"event_pipeline": {"jobs": {"enabled": True}}}

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
                config,
                "run-id",
                "worker-1",
                event_factory=FakeEvent,
                thread_factory=FakeThread,
            ):
                events.append(("work",))

        heartbeat.assert_called_once_with(config, "run-id", "worker-1")
        # Durable run heartbeats use a fixed process cadence so they outlive
        # lease-based job polling; the interval is not configurable.
        self.assertIn(("wait", orchestrator.DEFAULT_HEARTBEAT_INTERVAL_SECONDS), events)
        self.assertEqual(events[-2:], [("stop",), ("join",)])

    def test_heartbeat_guard_stops_on_protected_exception(self):
        import orchestrator

        event = Mock()
        event.wait.return_value = True
        thread = Mock()

        with self.assertRaisesRegex(RuntimeError, "original failure"):
            with orchestrator.maintain_run_heartbeat(
                {},
                "run-id",
                "worker-1",
                event_factory=Mock(return_value=event),
                thread_factory=Mock(return_value=thread),
            ):
                raise RuntimeError("original failure")

        event.set.assert_called_once_with()
        thread.join.assert_called_once_with()

    def test_direct_lifecycle_starts_heartbeat_only_after_successful_claim(self):
        import orchestrator

        guard = Mock(return_value=nullcontext())
        with (
            patch.object(orchestrator, "accept_run"),
            patch.object(orchestrator, "start_run", return_value=False),
            patch.object(orchestrator, "maintain_run_heartbeat", guard),
            patch.object(orchestrator, "_run_collector_impl") as work,
        ):
            self.assertIsNone(orchestrator.run_collector("fred", config={}))
        guard.assert_not_called()
        work.assert_not_called()

        guard.reset_mock()
        with (
            patch.object(orchestrator, "accept_run"),
            patch.object(orchestrator, "start_run", return_value=True) as start,
            patch.object(orchestrator, "maintain_run_heartbeat", guard),
            patch.object(
                orchestrator, "advisory_lock", side_effect=lambda *_args: nullcontext()
            ),
            patch.object(
                orchestrator,
                "_run_collector_impl",
                return_value={"status": "success", "error": None},
            ),
            patch.object(orchestrator, "finalize_run_safely", return_value=True),
        ):
            orchestrator.run_collector("fred", config={}, correlation_id="run-id")

        worker_id = start.call_args.args[2]
        guard.assert_called_once_with({}, "run-id", worker_id)

    def test_full_cycle_progress_uses_parent_worker_and_children_do_not_own_heartbeat(
        self,
    ):
        import orchestrator

        collector_result = {"status": "success"}
        with (
            patch.object(
                orchestrator, "get_all_collectors", return_value={"fred": Mock()}
            ),
            patch.object(orchestrator, "get_all_processors", return_value={}),
            patch.object(
                orchestrator, "run_collector", return_value=collector_result
            ) as child,
            patch.object(orchestrator, "update_run_progress") as progress,
        ):
            orchestrator._run_full_cycle_impl(
                {"collectors": {"fred": {"enabled": True}}},
                "run-id",
                manage_lifecycle=False,
                worker_id="parent-worker",
            )

        self.assertFalse(child.call_args.kwargs["manage_lifecycle"])
        self.assertTrue(progress.call_args_list)
        self.assertTrue(
            all(call.args[3] == "parent-worker" for call in progress.call_args_list)
        )

    def test_safe_finalization_distinguishes_lost_owner_and_exception(self):
        import orchestrator

        with (
            patch.object(orchestrator, "finish_run", return_value=False),
            patch.object(orchestrator, "logger") as logger,
        ):
            self.assertFalse(
                orchestrator.finalize_run_safely(
                    "run-id",
                    "success",
                    {},
                    {},
                    worker_id="worker-1",
                    run_kind="collector",
                )
            )
        logger.warning.assert_called_once()
        self.assertEqual(
            logger.warning.call_args.args[0], "run_finalization_lost_ownership"
        )

        with (
            patch.object(
                orchestrator, "finish_run", side_effect=RuntimeError("secret payload")
            ),
            patch.object(orchestrator, "logger") as logger,
        ):
            self.assertFalse(
                orchestrator.finalize_run_safely(
                    "run-id",
                    "success",
                    {},
                    {},
                    worker_id="worker-1",
                    run_kind="collector",
                )
            )
        logger.error.assert_called_once()
        self.assertNotIn("secret payload", str(logger.error.call_args))

    def test_direct_finalization_failure_preserves_result_and_original_exception(self):
        from locks import RunConflict

        import orchestrator

        result = {"status": "success", "error": None}
        with (
            patch.object(orchestrator, "accept_run"),
            patch.object(orchestrator, "start_run", return_value=True),
            patch.object(
                orchestrator, "maintain_run_heartbeat", return_value=nullcontext()
            ),
            patch.object(
                orchestrator, "advisory_lock", side_effect=lambda *_args: nullcontext()
            ),
            patch.object(orchestrator, "_run_collector_impl", return_value=result),
            patch.object(
                orchestrator, "finish_run", side_effect=RuntimeError("finalize failed")
            ),
            patch.object(orchestrator, "logger") as logger,
        ):
            self.assertIs(orchestrator.run_collector("fred", config={}), result)
        self.assertFalse(
            any(
                call.args[0] == "collector_completed"
                for call in logger.info.call_args_list
            )
        )

        with (
            patch.object(orchestrator, "accept_run"),
            patch.object(orchestrator, "start_run", return_value=True),
            patch.object(
                orchestrator, "maintain_run_heartbeat", return_value=nullcontext()
            ),
            patch.object(
                orchestrator, "advisory_lock", side_effect=RunConflict("collector:fred")
            ),
            patch.object(
                orchestrator, "finish_run", side_effect=RuntimeError("finalize failed")
            ),
        ):
            with self.assertRaisesRegex(RunConflict, "collector:fred"):
                orchestrator.run_collector("fred", config={})

    def test_operation_worker_claims_run_and_uses_heartbeat_guard(self):
        from contextlib import contextmanager

        import operation_worker

        @contextmanager
        def session_factory(config):
            yield SimpleNamespace()

        worker = operation_worker.OperationWorker(
            {"event_pipeline": {"jobs": {"enabled": True}}},
            worker_id="worker-x",
            session_factory=session_factory,
        )
        job = SimpleNamespace(
            id="job-1",
            run_kind="collector",
            requested_component="fred",
            correlation_id="run-id",
            attempt_count=1,
            max_attempts=3,
            payload={"mode": "refresh"},
        )
        with (
            patch("operation_worker.start_operation_job", return_value=True),
            patch("operation_worker.start_run", return_value=True) as start,
            patch(
                "operation_worker.maintain_run_heartbeat", return_value=nullcontext()
            ) as guard,
            patch(
                "operation_worker.run_collector",
                return_value={"status": "success", "error": None},
            ),
            patch("operation_worker.finish_run_in_session", return_value=True),
            patch("operation_worker.succeed_operation_job", return_value=True),
        ):
            worker._handle(job, {"retry": {"max_attempts": 3}})

        worker_id = start.call_args.args[2]
        self.assertTrue(worker_id.startswith("worker-x:"))
        guard.assert_called_once_with(
            {"event_pipeline": {"jobs": {"enabled": True}}}, "run-id", worker_id
        )

    def test_operation_worker_collector_with_dependents_aggregates_statuses(self):
        import operation_worker

        cases = [
            (
                {"fred": {"status": "partial"}},
                "partial",
                {"fred": {"status": "partial"}, "briefing": {"status": "success"}},
            ),
            (
                {"fred": {"status": "success"}, "briefing": {"status": "failed"}},
                "partial",
                {"fred": {"status": "success"}, "briefing": {"status": "failed"}},
            ),
            (
                {"fred": {"status": "failed"}, "briefing": {"status": "failed"}},
                "failed",
                {"fred": {"status": "failed"}},
            ),
            (
                {"fred": {"status": "success"}, "briefing": {"status": "success"}},
                "success",
                {"fred": {"status": "success"}, "briefing": {"status": "success"}},
            ),
        ]
        for stages, expected, expected_stages in cases:
            briefing_status = stages.get("briefing", {}).get("status", "success")
            with (
                self.subTest(stages=stages),
                patch(
                    "operation_worker.run_processor",
                    return_value={"status": briefing_status},
                ),
                patch(
                    "processors.get_processor",
                    return_value=Mock(get_depends_on=Mock(return_value=["fred"])),
                ),
            ):
                result = (
                    operation_worker.OperationWorker._run_collector_with_dependents(
                        "fred",
                        {
                            "processors": {
                                "briefing": {
                                    "enabled": True,
                                    "schedule": "after_dependency",
                                }
                            }
                        },
                        "cid",
                        stages["fred"],
                    )
                )
            self.assertEqual(result["status"], expected)
            # Every stage that ran is recorded truthfully: the collector
            # result plus each after_dependency processor actually executed.
            # A failed collector runs no dependents.
            self.assertEqual(result["stages"], expected_stages)

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
            with (
                self.subTest(statuses=statuses),
                patch.object(
                    orchestrator,
                    "get_all_collectors",
                    return_value={
                        collector_id: Mock() for collector_id in collector_ids
                    },
                ),
                patch.object(orchestrator, "get_all_processors", return_value={}),
                patch.object(orchestrator, "run_collector", side_effect=results),
                patch.object(orchestrator, "accept_run"),
                patch.object(orchestrator, "start_run", return_value=True),
                patch.object(
                    orchestrator, "maintain_run_heartbeat", return_value=nullcontext()
                ),
                patch.object(
                    orchestrator,
                    "advisory_lock",
                    side_effect=lambda *_args: nullcontext(),
                ),
                patch.object(orchestrator, "update_run_progress"),
                patch.object(
                    orchestrator, "finalize_run_safely", return_value=True
                ) as finalize,
            ):
                result = orchestrator.run_full_cycle(
                    config=config, correlation_id="run-id"
                )

            self.assertEqual(result["status"], expected)
            self.assertEqual(finalize.call_args.args[1], expected)

    def test_empty_full_cycle_is_a_successful_no_op(self):
        import orchestrator

        with (
            patch.object(orchestrator, "get_all_collectors", return_value={}),
            patch.object(orchestrator, "get_all_processors", return_value={}),
            patch.object(orchestrator, "update_run_progress"),
        ):
            result = orchestrator._run_full_cycle_impl(
                config={"collectors": {}, "processors": {}},
                correlation_id="run-id",
                manage_lifecycle=False,
            )

        self.assertEqual(result["status"], "success")


class AbandonedRunRecoveryTests(unittest.TestCase):
    """Phase 5 Task 17: restart reconciliation and explicit-only replay."""

    def test_reconciliation_uses_deterministic_stale_cutoffs_and_preserves_fresh_runs(
        self,
    ):
        from datetime import timedelta

        from orchestrator import reconcile_abandoned_runs

        now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
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
            reconcile_abandoned_runs({}, now=datetime(2026, 7, 13, tzinfo=UTC))

        for execute_call in session.execute.call_args_list:
            sql, params = execute_call.args
            self.assertIn("status = :abandoned", str(sql))
            self.assertNotIn("completed", str(sql).split("WHERE")[-1])
            self.assertIn("restart reconciliation", params["reason"])


class RuntimeFeatureTests(unittest.TestCase):
    def tearDown(self):
        stop_scheduler()

    def test_model_resolution_is_provider_model_agnostic(self):
        config = {
            "llm": {
                "models": {"default": "deepseek/deepseek-v4-flash"},
                "briefing": "provider/custom-model",
            }
        }
        self.assertEqual(resolve_model(config), "deepseek/deepseek-v4-flash")
        self.assertEqual(
            resolve_model(config, processor_id="briefing"),
            "deepseek/deepseek-v4-flash",
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
                "database:\n"
                "  host: localhost\n"
                "  name: ${ABSENT_VALUE:-fallback}\n"
                "  user: ${REQUIRED_VALUE}\n"
                "  password: ${DB_PASSWORD}\n"
                "kobeissi:\n"
                "  api_key: ${EMPTY_VALUE:-fallback}\n"
            )
            with patch.dict(
                os.environ,
                {
                    "REQUIRED_VALUE": "configured",
                    "EMPTY_VALUE": "",
                    "DB_PASSWORD": "correct-horse-battery-staple",
                },
                clear=True,
            ):
                config = reload_config(str(config_path))

        self.assertEqual(config["database"]["user"], "configured")
        self.assertEqual(config["database"]["name"], "fallback")
        self.assertEqual(config["kobeissi"]["api_key"], "")

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

    def test_disabled_credentialed_sources_load_without_unused_keys(self):
        from config_loader import reload_config

        config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
        production_env = {
            "DB_USER": "trading",
            "DB_PASSWORD": "correct-horse-battery-staple",
            "OPENROUTER_API_KEY": "configured-openrouter",
            "TWITTERAPI_KEY": "",
        }
        with patch.dict(os.environ, production_env, clear=True):
            config = reload_config(str(config_path))

        self.assertFalse(config.collectors["fred"].enabled)
        self.assertFalse(config.collectors["oanda"].enabled)
        self.assertEqual(config.collectors["fred"].api_key, "")
        self.assertEqual(config.collectors["oanda"].api_key, "")

    def test_enabled_production_sources_blank_credentials_fail_closed(self):
        from config_loader import reload_config

        config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
        base_env = {
            "DB_USER": "trading",
            "DB_PASSWORD": "correct-horse-battery-staple",
            "OPENROUTER_API_KEY": "configured-openrouter",
            "OANDA_API_KEY": "",
            "TWITTERAPI_KEY": "",
        }
        with tempfile.TemporaryDirectory() as tmp:
            candidate_path = Path(tmp) / "config.yaml"
            candidate_path.write_text(config_path.read_text())
            operator_path = Path(tmp) / "operator.yaml"
            secrets_path = Path(tmp) / "secrets.env"
            secrets_path.write_text(
                "OPENROUTER_API_KEY=configured-openrouter\n"
                "FRED_API_KEY=\n"
                "OANDA_API_KEY=\n"
            )
            for source_id, variable in (
                ("fred", "FRED_API_KEY"),
                ("oanda", "OANDA_API_KEY"),
            ):
                with self.subTest(variable=variable):
                    operator_path.write_text(
                        f"collectors:\n  {source_id}:\n    enabled: true\n"
                    )
                    env = {
                        **base_env,
                        "OPERATOR_CONFIG": str(operator_path),
                        "SECRETS_FILE": str(secrets_path),
                    }
                    with patch.dict(os.environ, env, clear=True):
                        with self.assertRaisesRegex(
                            ValueError, rf"collectors\.{source_id}\.api_key"
                        ):
                            reload_config(str(candidate_path))

        with patch.dict(
            os.environ,
            {**base_env, "OPENROUTER_API_KEY": ""},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "OPENROUTER_API_KEY"):
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
        monday = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)

        self.assertEqual(
            trigger.get_next_fire_time(None, monday),
            datetime(2026, 7, 19, 20, 0, tzinfo=UTC),
        )

    def test_legacy_numeric_sunday_schedules_fire_on_sunday_utc(self):
        from scheduler import _build_cron_trigger

        monday = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
        expected = datetime(2026, 7, 19, 20, 0, tzinfo=UTC)
        for weekday in ("0", "7"):
            with self.subTest(weekday=weekday):
                trigger = _build_cron_trigger(f"0 20 * * {weekday}")
                self.assertEqual(trigger.get_next_fire_time(None, monday), expected)

    def test_simple_posix_numeric_weekday_is_mapped_explicitly(self):
        from scheduler import _build_cron_trigger

        monday = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
        trigger = _build_cron_trigger("0 20 * * 1")

        self.assertEqual(
            trigger.get_next_fire_time(None, monday),
            datetime(2026, 7, 13, 20, 0, tzinfo=UTC),
        )

    def test_legacy_posix_numeric_weekday_range_maps_to_monday_through_friday(self):
        from scheduler import _build_cron_trigger

        monday = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
        trigger = _build_cron_trigger("0 6,12,18 * * 1-5")

        self.assertEqual(
            trigger.get_next_fire_time(None, monday),
            datetime(2026, 7, 13, 6, 0, tzinfo=UTC),
        )

    def test_wildcard_and_named_weekdays_are_preserved(self):
        from scheduler import _build_cron_trigger

        monday = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
        self.assertEqual(
            _build_cron_trigger("0 20 * * *").get_next_fire_time(None, monday),
            datetime(2026, 7, 13, 20, 0, tzinfo=UTC),
        )
        for weekday in ("mon", "mon-fri"):
            with self.subTest(weekday=weekday):
                self.assertEqual(
                    _build_cron_trigger(f"0 20 * * {weekday}").get_next_fire_time(
                        None, monday
                    ),
                    datetime(2026, 7, 13, 20, 0, tzinfo=UTC),
                )

    def test_comma_separated_posix_numeric_weekdays_are_mapped_explicitly(self):
        from scheduler import _build_cron_trigger

        monday = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
        trigger = _build_cron_trigger("0 20 * * 2,4,6")

        self.assertEqual(
            trigger.get_next_fire_time(None, monday),
            datetime(2026, 7, 14, 20, 0, tzinfo=UTC),
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
        research = config["research_intelligence"]
        self.assertTrue(research["enabled"])
        self.assertTrue(research["schedule_enabled"])
        _build_cron_trigger(research["schedule"])

    def test_scheduler_registers_configured_cron_jobs(self):
        config = {
            "collectors": {"fred": {"enabled": True, "schedule": "0 6 * * *"}},
            "processors": {
                "briefing": {"enabled": True, "schedule": "0 7 * * *"},
                "macro_regime": {"enabled": True, "schedule": "after_dependency"},
            },
            "research_intelligence": {
                "enabled": True,
                "schedule_enabled": True,
                "schedule": "30 5 * * *",
            },
            "thesis_autonomy": {
                "enabled": True,
                "schedule_enabled": True,
                "schedule": "0 2,8,14,20 * * 1-5",
            },
        }

        start_scheduler(config)
        ids = {job["id"] for job in scheduler_status()["jobs"]}

        self.assertEqual(
            ids,
            {
                "collector:fred",
                "processor:briefing",
                "research:discovery",
                "thesis-autonomy:run",
            },
        )

    @patch("scheduler.BackgroundScheduler")
    def test_thesis_autonomy_cron_job_coalesces_with_one_instance(
        self, scheduler_factory
    ):
        scheduler = Mock()
        scheduler.running = False
        scheduler.get_jobs.return_value = []
        scheduler_factory.return_value = scheduler

        start_scheduler(
            {
                "collectors": {},
                "processors": {},
                "thesis_autonomy": {
                    "enabled": True,
                    "schedule_enabled": True,
                    "schedule": "0 2,8,14,20 * * 1-5",
                },
            }
        )

        autonomy_calls = [
            call
            for call in scheduler.add_job.call_args_list
            if call.kwargs.get("id") == "thesis-autonomy:run"
        ]
        self.assertEqual(len(autonomy_calls), 1)
        autonomy_job = autonomy_calls[0]
        self.assertTrue(autonomy_job.kwargs["coalesce"])
        self.assertEqual(autonomy_job.kwargs["max_instances"], 1)
        self.assertEqual(
            autonomy_job.args[1].get_next_fire_time(
                None, datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
            ),
            datetime(2026, 8, 10, 2, 0, tzinfo=UTC),
        )

    @patch("scheduler.BackgroundScheduler")
    def test_disabled_thesis_autonomy_registers_no_cron_job(self, scheduler_factory):
        scheduler = Mock()
        scheduler.running = False
        scheduler.get_jobs.return_value = []
        scheduler_factory.return_value = scheduler

        start_scheduler(
            {
                "collectors": {},
                "processors": {},
                "thesis_autonomy": {
                    "enabled": False,
                    "schedule_enabled": True,
                    "schedule": "0 2,8,14,20 * * 1-5",
                },
            }
        )
        autonomy_jobs = [
            call.kwargs
            for call in scheduler.add_job.call_args_list
            if call.kwargs.get("id") == "thesis-autonomy:run"
        ]
        self.assertEqual(autonomy_jobs, [])

    @patch("scheduler.BackgroundScheduler")
    def test_filings_job_runs_immediately_on_scheduler_start(self, scheduler_factory):
        scheduler = Mock()
        scheduler.running = False
        scheduler.get_jobs.return_value = []
        scheduler_factory.return_value = scheduler
        before = datetime.now(UTC)

        start_scheduler(
            {
                "collectors": {},
                "processors": {},
                "investment_filings": {
                    "enabled": True,
                    "schedule": "0 8 * * 1-5",
                    "run_on_startup": True,
                },
            }
        )

        next_run_time = scheduler.add_job.call_args.kwargs["next_run_time"]
        self.assertGreaterEqual(next_run_time, before)
        self.assertLessEqual(next_run_time, datetime.now(UTC))

    def test_scheduler_registers_enabled_news_but_not_disabled_paid_source(self):
        config = {
            "collectors": {},
            "processors": {},
            "reuters": {
                "enabled": True,
                "schedule_enabled": True,
                "schedule": "15 */6 * * *",
            },
            "kobeissi": {
                "enabled": True,
                "schedule_enabled": False,
                "schedule": "20 */6 * * *",
            },
        }
        start_scheduler(config)
        ids = {job["id"] for job in scheduler_status()["jobs"]}
        self.assertEqual(ids, {"news:reuters"})

    def test_demo_mode_registers_no_news_jobs(self):
        config = {
            "demo": {"enabled": True},
            "collectors": {},
            "processors": {},
            "reuters": {
                "enabled": True,
                "schedule_enabled": True,
                "schedule": "15 */2 * * *",
            },
            "kobeissi": {
                "enabled": True,
                "schedule_enabled": True,
                "schedule": "20 */6 * * *",
            },
        }
        start_scheduler(config)
        ids = {job["id"] for job in scheduler_status()["jobs"]}
        self.assertEqual(ids, set())

    def test_run_news_source_returns_truthful_safe_summary(self):
        from sources.news_result import NewsCollectionResult

        from orchestrator import run_news_source

        config = {"news_feed": {"output_path": "unused"}, "reuters": {"enabled": True}}
        with (
            patch("orchestrator.advisory_lock", return_value=nullcontext()) as lock,
            patch(
                "sources.news_feed.collect_and_publish",
                return_value=NewsCollectionResult([{"id": "one"}], "ok"),
            ),
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

        publication = NewsPublication(
            Path("snapshot"), Path("state"), {"cursor": "advanced"}
        )
        outcome = NewsCollectionResult(
            [{"id": "one"}],
            "error",
            "News state persistence failed: OSError",
            publication,
            True,
        )
        config = {"news_feed": {"output_path": "unused"}, "reuters": {"enabled": True}}
        with (
            patch("orchestrator.advisory_lock", return_value=nullcontext()),
            patch("sources.news_feed.collect_and_publish", return_value=outcome),
        ):
            result = run_news_source(
                "reuters", "cid-state", config, manage_lifecycle=False
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["state"], "publication_failed")
        self.assertEqual(result["code"], "news_publication_failed")
        self.assertEqual(result["error_class"], "persistence")
        self.assertTrue(result["retryable"])
        self.assertTrue(result["feed_published"])
        self.assertEqual(result["new_item_count"], 1)

    def test_worker_lock_conflict_poisons_job_and_finalizes_run_failed(self):
        from contextlib import contextmanager

        import operation_worker
        from locks import RunConflict

        @contextmanager
        def session_factory(config):
            yield SimpleNamespace()

        with (
            patch("operation_worker.start_run", return_value=True),
            patch(
                "operation_worker.maintain_run_heartbeat", return_value=nullcontext()
            ),
            patch(
                "operation_worker.run_news_source",
                side_effect=RunConflict("news:reuters"),
            ),
            patch(
                "operation_worker.finish_run_in_session", return_value=True
            ) as finalize,
            patch("operation_worker.terminal_fail_operation_job", return_value=True),
            patch("operation_worker.start_operation_job", return_value=True),
        ):
            job = SimpleNamespace(
                id="job-1",
                run_kind="news",
                requested_component="reuters",
                correlation_id="run-id",
                attempt_count=3,
                max_attempts=3,
                payload={"mode": "refresh"},
            )
            worker = operation_worker.OperationWorker(
                {"event_pipeline": {"jobs": {"enabled": True}}},
                worker_id="worker-x",
                session_factory=session_factory,
            )
            worker._handle(job, {"retry": {"max_attempts": 3}})

        self.assertEqual(finalize.call_args.args[2], "failed")
        self.assertEqual(finalize.call_args.args[1], "run-id")
        self.assertEqual(
            finalize.call_args.kwargs["worker_id"].split(":")[0], "worker-x"
        )

    def test_demo_fixture_is_public_safe_and_deterministic(self):
        seed_path = (
            Path(__file__).resolve().parents[2] / "db" / "demo" / "900_demo_seed.sql"
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


if __name__ == "__main__":
    unittest.main()
