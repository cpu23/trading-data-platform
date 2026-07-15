import copy
import sys
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class CollectorWorkerLimitTests(unittest.TestCase):
    def test_default_config_sets_three_collector_workers(self):
        import yaml

        config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
        config = yaml.safe_load(config_path.read_text())

        self.assertEqual(config["orchestration"]["collector_workers"], 3)

    def test_worker_limit_is_normalized_and_bounded_by_enabled_collectors(self):
        from orchestrator import collector_worker_limit

        cases = [
            ({}, 5, 3),
            ({"orchestration": {"collector_workers": 0}}, 5, 1),
            ({"orchestration": {"collector_workers": -4}}, 5, 1),
            ({"orchestration": {"collector_workers": "invalid"}}, 5, 3),
            ({"orchestration": {"collector_workers": 999}}, 20, 8),
            ({"orchestration": {"collector_workers": 6}}, 2, 2),
            ({"orchestration": {"collector_workers": 1}}, 5, 1),
            ({"orchestration": {"collector_workers": 3}}, 0, 0),
        ]
        for config, enabled_count, expected in cases:
            with self.subTest(config=config, enabled_count=enabled_count):
                self.assertEqual(
                    collector_worker_limit(config, enabled_count), expected
                )


class ConcurrentCollectorCycleTests(unittest.TestCase):
    def test_collectors_overlap_bounded_ordered_and_finish_before_processors(self):
        import orchestrator

        collector_ids = ["alpha", "beta", "gamma"]
        config = {
            "orchestration": {"collector_workers": 3},
            "collectors": {
                source_id: {"enabled": True} for source_id in collector_ids
            },
            "processors": {},
        }
        lock = threading.Lock()
        all_active = threading.Event()
        beta_completed = threading.Event()
        gamma_completed = threading.Event()
        active = 0
        max_active = 0
        completion_order = []
        finished = set()
        worker_threads = set()
        progress_writes = []
        coordinator_thread = threading.get_ident()

        def collect(source_id, **kwargs):
            nonlocal active, max_active
            self.assertEqual(kwargs["correlation_id"], "cycle-id")
            self.assertFalse(kwargs["manage_lifecycle"])
            with lock:
                active += 1
                max_active = max(max_active, active)
                worker_threads.add(threading.get_ident())
                if active == len(collector_ids):
                    all_active.set()
            self.assertTrue(all_active.wait(0.5), "collectors did not overlap")
            if source_id == "alpha":
                self.assertTrue(beta_completed.wait(0.5))
            elif source_id == "beta":
                self.assertTrue(gamma_completed.wait(0.5))
            with lock:
                active -= 1
                completion_order.append(source_id)
                finished.add(source_id)
            if source_id == "gamma":
                gamma_completed.set()
            elif source_id == "beta":
                beta_completed.set()
            return {
                "collector": source_id,
                "status": "success",
                "duration_ms": 1,
            }

        def persist_progress(_cid, progress, _config, _worker_id):
            progress_writes.append((threading.get_ident(), copy.deepcopy(progress)))
            return True

        def processors(**kwargs):
            self.assertEqual(finished, set(collector_ids))
            self.assertEqual(kwargs["successful_collectors"], set(collector_ids))
            return {}

        with patch.object(
            orchestrator,
            "get_all_collectors",
            return_value={source_id: Mock() for source_id in collector_ids},
        ), patch.object(orchestrator, "get_all_processors", return_value={}), patch.object(
            orchestrator, "run_collector", side_effect=collect
        ) as run_collector, patch.object(
            orchestrator, "update_run_progress", side_effect=persist_progress
        ), patch.object(
            orchestrator, "_resolve_and_run_processors", side_effect=processors
        ), patch.object(orchestrator, "logger") as logger:
            result = orchestrator._run_full_cycle_impl(
                config,
                "cycle-id",
                manage_lifecycle=False,
                worker_id="parent-worker",
            )

        self.assertEqual(max_active, 3)
        self.assertEqual(len(worker_threads), 3)
        self.assertNotIn(coordinator_thread, worker_threads)
        self.assertEqual(completion_order, ["gamma", "beta", "alpha"])
        self.assertEqual(list(result["collectors"]), collector_ids)
        self.assertEqual(
            [item["collector"] for item in result["collectors"].values()],
            collector_ids,
        )
        self.assertEqual(run_collector.call_count, 3)
        self.assertTrue(all(thread_id == coordinator_thread for thread_id, _ in progress_writes))
        completed_counts = [
            snapshot["completed_stages"]
            for _, snapshot in progress_writes
            if snapshot["completed_stages"]
        ]
        self.assertEqual(completed_counts, [1, 2, 3])
        completed_snapshots = [
            snapshot
            for _, snapshot in progress_writes
            if snapshot["completed_stages"]
        ]
        self.assertEqual(
            {
                stage["component"]
                for stage in completed_snapshots[-1]["stages"]
                if stage["status"] == "success"
            },
            set(collector_ids),
        )
        layer_log = next(
            call
            for call in logger.info.call_args_list
            if call.args and call.args[0] == "collector_layer_finished"
        )
        self.assertEqual(layer_log.kwargs["worker_limit"], 3)
        self.assertGreaterEqual(layer_log.kwargs["duration_ms"], 0)

    def test_escaped_collector_exceptions_are_isolated_and_safely_aggregated(self):
        import orchestrator

        collector_ids = ["alpha", "beta", "gamma"]
        config = {
            "orchestration": {"collector_workers": 2},
            "collectors": {
                source_id: {"enabled": True} for source_id in collector_ids
            },
            "processors": {},
        }
        cases = [
            ({"alpha"}, "partial"),
            (set(), "failed"),
        ]
        for successful, expected_status in cases:
            attempted = []

            def collect(source_id, **_kwargs):
                attempted.append(source_id)
                if source_id not in successful:
                    raise RuntimeError(f"secret payload for {source_id}")
                return {"collector": source_id, "status": "success", "error": None}

            with self.subTest(successful=successful), patch.object(
                orchestrator,
                "get_all_collectors",
                return_value={source_id: Mock() for source_id in collector_ids},
            ), patch.object(orchestrator, "get_all_processors", return_value={}), patch.object(
                orchestrator, "run_collector", side_effect=collect
            ), patch.object(orchestrator, "update_run_progress"), patch.object(
                orchestrator, "logger"
            ):
                result = orchestrator._run_full_cycle_impl(
                    config, "cycle-id", manage_lifecycle=False
                )

            self.assertEqual(set(attempted), set(collector_ids))
            self.assertEqual(result["status"], expected_status)
            self.assertEqual(list(result["collectors"]), collector_ids)
            for source_id in set(collector_ids) - successful:
                failure = result["collectors"][source_id]
                self.assertEqual(failure["status"], "failed")
                self.assertEqual(failure["collector"], source_id)
                self.assertEqual(failure["correlation_id"], "cycle-id")
                self.assertEqual(failure["error"], "collector execution failed")
                self.assertNotIn("secret payload", str(failure))

    def test_one_worker_is_serial_and_empty_cycle_does_not_create_executor(self):
        import orchestrator

        collector_ids = ["alpha", "beta", "gamma"]
        order = []
        active = 0
        max_active = 0
        lock = threading.Lock()

        def collect(source_id, **_kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            order.append(source_id)
            with lock:
                active -= 1
            return {"collector": source_id, "status": "success"}

        config = {
            "orchestration": {"collector_workers": 1},
            "collectors": {
                source_id: {"enabled": True} for source_id in collector_ids
            },
            "processors": {},
        }
        with patch.object(
            orchestrator,
            "get_all_collectors",
            return_value={source_id: Mock() for source_id in collector_ids},
        ), patch.object(orchestrator, "get_all_processors", return_value={}), patch.object(
            orchestrator, "run_collector", side_effect=collect
        ), patch.object(orchestrator, "update_run_progress"):
            result = orchestrator._run_full_cycle_impl(
                config, "cycle-id", manage_lifecycle=False
            )

        self.assertEqual(max_active, 1)
        self.assertEqual(order, collector_ids)
        self.assertEqual(list(result["collectors"]), collector_ids)

        with patch.object(orchestrator, "get_all_collectors", return_value={}), patch.object(
            orchestrator, "get_all_processors", return_value={}
        ), patch.object(orchestrator, "update_run_progress"), patch.object(
            orchestrator, "ThreadPoolExecutor"
        ) as executor:
            empty_result = orchestrator._run_full_cycle_impl(
                {"collectors": {}, "processors": {}},
                "empty-cycle",
                manage_lifecycle=False,
            )

        executor.assert_not_called()
        self.assertEqual(empty_result["collectors"], {})
        self.assertEqual(empty_result["status"], "success")

    def test_parent_owns_single_heartbeat_and_component_locks_wrap_parallel_children(self):
        import orchestrator

        collector_ids = ["alpha", "beta", "gamma"]
        config = {
            "orchestration": {"collector_workers": 3},
            "collectors": {
                source_id: {"enabled": True} for source_id in collector_ids
            },
            "processors": {},
        }
        events = []
        events_lock = threading.Lock()

        @contextmanager
        def tracked_lock(name, _config):
            with events_lock:
                events.append(("lock-enter", name))
            try:
                yield
            finally:
                with events_lock:
                    events.append(("lock-exit", name))

        @contextmanager
        def heartbeat_guard(_config, correlation_id, worker_id):
            events.append(("heartbeat-enter", correlation_id, worker_id))
            try:
                yield
            finally:
                events.append(("heartbeat-exit", correlation_id, worker_id))

        def collector_work(source_id, _config, correlation_id, manage_lifecycle):
            self.assertFalse(manage_lifecycle)
            self.assertEqual(correlation_id, "cycle-id")
            with events_lock:
                self.assertIn(("lock-enter", "cycle"), events)
                self.assertIn(("lock-enter", f"collector:{source_id}"), events)
                self.assertNotIn(("lock-exit", f"collector:{source_id}"), events)
            return {
                "collector": source_id,
                "status": "success",
                "error": None,
                "correlation_id": correlation_id,
            }

        with patch.object(orchestrator, "accept_run"), patch.object(
            orchestrator, "start_run", return_value=True
        ), patch.object(
            orchestrator, "maintain_run_heartbeat", side_effect=heartbeat_guard
        ) as heartbeat, patch.object(
            orchestrator, "advisory_lock", side_effect=tracked_lock
        ), patch.object(
            orchestrator,
            "get_all_collectors",
            return_value={source_id: Mock() for source_id in collector_ids},
        ), patch.object(orchestrator, "get_all_processors", return_value={}), patch.object(
            orchestrator, "_run_collector_impl", side_effect=collector_work
        ), patch.object(orchestrator, "update_run_progress"), patch.object(
            orchestrator, "finalize_run_safely", return_value=True
        ):
            result = orchestrator.run_full_cycle(config=config, correlation_id="cycle-id")

        heartbeat.assert_called_once()
        self.assertEqual(events[0][0], "heartbeat-enter")
        self.assertEqual(events[1], ("lock-enter", "cycle"))
        self.assertEqual(events[-2], ("lock-exit", "cycle"))
        self.assertEqual(events[-1][0], "heartbeat-exit")
        for source_id in collector_ids:
            enter = events.index(("lock-enter", f"collector:{source_id}"))
            exit_ = events.index(("lock-exit", f"collector:{source_id}"))
            self.assertLess(enter, exit_)
        self.assertEqual(result["status"], "success")

    def test_processors_remain_dependency_ordered_and_sequential(self):
        import orchestrator

        processors = {
            "briefing": Mock(get_depends_on=Mock(return_value=["macro_regime"])),
            "macro_regime": Mock(get_depends_on=Mock(return_value=["fred"])),
            "independent": Mock(get_depends_on=Mock(return_value=["fred"])),
        }
        calls = []
        active = 0
        max_active = 0
        coordinator_thread = threading.get_ident()

        def run(processor_id, **kwargs):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            calls.append((processor_id, threading.get_ident(), kwargs))
            active -= 1
            return {"processor": processor_id, "status": "success"}

        with patch.object(
            orchestrator, "get_all_processors", return_value=processors
        ), patch.object(orchestrator, "run_processor", side_effect=run):
            results = orchestrator._resolve_and_run_processors(
                config={
                    "processors": {
                        processor_id: {"enabled": True}
                        for processor_id in processors
                    }
                },
                correlation_id="cycle-id",
                successful_collectors={"fred"},
            )

        self.assertEqual(
            [processor_id for processor_id, _, _ in calls],
            ["macro_regime", "independent", "briefing"],
        )
        self.assertEqual(list(results), ["macro_regime", "independent", "briefing"])
        self.assertEqual(max_active, 1)
        self.assertTrue(all(thread_id == coordinator_thread for _, thread_id, _ in calls))
        self.assertTrue(
            all(
                kwargs["correlation_id"] == "cycle-id"
                and kwargs["manage_lifecycle"] is False
                for _, _, kwargs in calls
            )
        )


if __name__ == "__main__":
    unittest.main()
