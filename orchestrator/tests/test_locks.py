import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class StableLockKeyTests(unittest.TestCase):
    def test_key_is_deterministic_signed_64_bit_and_names_are_distinct(self):
        from locks import stable_lock_key

        cycle_key = stable_lock_key("cycle")
        self.assertEqual(cycle_key, stable_lock_key("cycle"))
        self.assertGreaterEqual(cycle_key, -(2**63))
        self.assertLessEqual(cycle_key, 2**63 - 1)
        self.assertNotEqual(cycle_key, stable_lock_key("collector:fred"))


class AdvisoryLockTests(unittest.TestCase):
    @staticmethod
    def _session(acquired=True, unlocked=True):
        session = Mock()
        acquire_result = Mock()
        acquire_result.scalar_one.return_value = acquired
        unlock_result = Mock()
        unlock_result.scalar_one.return_value = unlocked
        session.execute.side_effect = [acquire_result, unlock_result]
        return session

    def test_successful_acquire_yields_and_releases_before_session_returns(self):
        from locks import advisory_lock, stable_lock_key

        session = self._session()
        events = []

        @contextmanager
        def session_scope(_config):
            events.append("session-open")
            yield session
            events.append("session-returned")

        with patch("locks.get_session", side_effect=session_scope):
            with advisory_lock("collector:fred", {"database": {}}):
                events.append("protected-work")

        self.assertEqual(events, ["session-open", "protected-work", "session-returned"])
        self.assertEqual(session.execute.call_count, 2)
        acquire_sql, acquire_params = session.execute.call_args_list[0].args
        unlock_sql, unlock_params = session.execute.call_args_list[1].args
        self.assertIn("pg_try_advisory_lock", str(acquire_sql))
        self.assertIn("pg_advisory_unlock", str(unlock_sql))
        self.assertEqual(acquire_params, {"key": stable_lock_key("collector:fred")})
        self.assertEqual(unlock_params, acquire_params)

    def test_conflict_raises_typed_exception_without_yield_or_unlock(self):
        from locks import RunConflict, advisory_lock

        session = self._session(acquired=False)
        protected_work = Mock()
        with patch("locks.get_session") as get_session:
            get_session.return_value.__enter__.return_value = session
            with self.assertRaises(RunConflict) as raised:
                with advisory_lock("processor:briefing", {}):
                    protected_work()

        self.assertEqual(raised.exception.lock_name, "processor:briefing")
        self.assertEqual(str(raised.exception), "run conflict: processor:briefing")
        protected_work.assert_not_called()
        self.assertEqual(session.execute.call_count, 1)

    def test_protected_exception_still_releases_and_is_preserved(self):
        from locks import advisory_lock

        session = self._session()
        original = ValueError("protected failure")
        with patch("locks.get_session") as get_session:
            get_session.return_value.__enter__.return_value = session
            with self.assertRaises(ValueError) as raised:
                with advisory_lock("cycle", {}):
                    raise original

        self.assertIs(raised.exception, original)
        self.assertIn(
            "pg_advisory_unlock", str(session.execute.call_args_list[1].args[0])
        )

    def test_false_unlock_logs_loudly_without_failing_successful_work(self):
        from locks import advisory_lock

        session = self._session(unlocked=False)
        with patch("locks.get_session") as get_session, patch("locks.logger") as logger:
            get_session.return_value.__enter__.return_value = session
            with advisory_lock("cycle", {}):
                pass

        logger.error.assert_called_once()
        self.assertIn("advisory_lock_release_failed", logger.error.call_args.args)
        self.assertEqual(logger.error.call_args.kwargs["lock_name"], "cycle")

    def test_false_unlock_does_not_mask_protected_exception(self):
        from locks import advisory_lock

        session = self._session(unlocked=False)
        original = RuntimeError("original")
        with patch("locks.get_session") as get_session, patch("locks.logger") as logger:
            get_session.return_value.__enter__.return_value = session
            with self.assertRaises(RuntimeError) as raised:
                with advisory_lock("cycle", {}):
                    raise original

        self.assertIs(raised.exception, original)
        logger.error.assert_called_once()


class CommonExecutionLockTests(unittest.TestCase):
    @staticmethod
    @contextmanager
    def _record_lock(name, _config, names):
        names.append(name)
        yield

    def test_collector_processor_and_cycle_use_common_lock_wrapper(self):
        import orchestrator

        calls = []

        def lock(name, config):
            return self._record_lock(name, config, calls)

        with (
            patch.object(orchestrator, "advisory_lock", side_effect=lock),
            patch.object(
                orchestrator, "_run_collector_impl", return_value={"status": "success"}
            ),
            patch.object(
                orchestrator, "_run_processor_impl", return_value={"status": "success"}
            ),
            patch.object(
                orchestrator, "_run_full_cycle_impl", return_value={"status": "success"}
            ),
        ):
            orchestrator.run_collector("fred", config={}, manage_lifecycle=False)
            orchestrator.run_processor("briefing", config={}, manage_lifecycle=False)
            orchestrator.run_full_cycle(config={}, manage_lifecycle=False)

        self.assertEqual(calls, ["collector:fred", "processor:briefing", "cycle"])

    def test_full_cycle_holds_cycle_then_child_component_lock_with_lifecycle_disabled(
        self,
    ):
        import orchestrator

        held = []
        entries = []

        @contextmanager
        def lock(name, _config):
            entries.append(("enter", name, tuple(held)))
            held.append(name)
            try:
                yield
            finally:
                held.remove(name)
                entries.append(("exit", name, tuple(held)))

        collector = Mock()
        collector.collect.return_value = []
        with (
            patch.object(orchestrator, "advisory_lock", side_effect=lock),
            patch.object(
                orchestrator, "get_all_collectors", return_value={"fred": collector}
            ),
            patch.object(orchestrator, "get_collector", return_value=collector),
            patch.object(orchestrator, "get_all_processors", return_value={}),
            patch.object(orchestrator, "update_run_progress"),
            patch.object(orchestrator, "_write_collection_log"),
        ):
            orchestrator.run_full_cycle(
                config={"collectors": {"fred": {"enabled": True}}},
                manage_lifecycle=False,
            )

        self.assertIn(("enter", "collector:fred", ("cycle",)), entries)

    def test_direct_conflict_finalizes_owned_lifecycle_and_surfaces(self):
        from locks import RunConflict

        import orchestrator

        with (
            patch.object(orchestrator, "accept_run"),
            patch.object(orchestrator, "start_run", return_value=True),
            patch.object(
                orchestrator, "advisory_lock", side_effect=RunConflict("collector:fred")
            ),
            patch.object(orchestrator, "finish_run") as finish,
        ):
            with self.assertRaises(RunConflict):
                orchestrator.run_collector("fred", config={}, correlation_id="run-id")

        finish.assert_called_once()
        self.assertEqual(finish.call_args.args[1], "failed")
        self.assertEqual(finish.call_args.args[4], "run conflict: collector:fred")


if __name__ == "__main__":
    unittest.main()
