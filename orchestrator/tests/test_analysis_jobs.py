import sys
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis_jobs import (
    AnalysisJob,
    enqueue_job,
    retry_job,
    sanitize_error,
    succeed_job,
    terminal_fail_job,
)
from job_worker import AnalysisJobWorker


class Result:
    def __init__(self, rows=(), first=None, rowcount=0):
        self._rows = list(rows)
        self._first = first
        self.rowcount = rowcount

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._first


class FakeSession:
    def __init__(self, existing=None):
        self.existing = existing
        self.calls = []
        self.inserted = None

    def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, params or {}))
        if sql.startswith("SELECT") and "ORDER BY created_at DESC" in sql:
            return Result(first=self.existing)
        if sql.startswith("INSERT"):
            if self.existing is not None:
                return Result()
            self.inserted = {
                "id": 7,
                "job_type": params["job_type"],
                "dedupe_key": params["dedupe_key"],
                "input_fingerprint": params["input_fingerprint"],
                "payload": params["payload"],
                "state": "queued",
                "priority": params["priority"],
                "max_attempts": params["max_attempts"],
                "attempt_count": 0,
            }
            return Result(first=self.inserted)
        return Result(rowcount=1)


JOB_ROW = {
    "id": 3,
    "job_type": "watchlist",
    "dedupe_key": "global",
    "input_fingerprint": "f" * 64,
    "payload": {"b": 2, "a": 1},
    "state": "running",
    "claimed_by": "worker-a",
    "attempt_count": 1,
    "max_attempts": 3,
}


class AnalysisJobRepositoryTests(unittest.TestCase):
    def test_enqueue_serializes_payload_and_suppresses_exact_duplicate(self):
        session = FakeSession()
        result = enqueue_job(
            session,
            job_type="watchlist",
            dedupe_key="global",
            input_fingerprint="f" * 64,
            payload={"b": 2, "a": 1},
            correlation_id=uuid4(),
        )
        self.assertTrue(result.inserted)
        self.assertEqual(session.inserted["payload"], '{"a":1,"b":2}')

        duplicate = FakeSession(existing=session.inserted)
        result = enqueue_job(
            duplicate,
            job_type="watchlist",
            dedupe_key="global",
            input_fingerprint="f" * 64,
            payload={"a": 1, "b": 2},
            correlation_id=uuid4(),
        )
        self.assertFalse(result.inserted)
        self.assertTrue(result.suppressed)
        self.assertEqual(
            len([call for call in duplicate.calls if call[0].startswith("INSERT")]), 0
        )

    def test_transitions_require_current_owner(self):
        session = FakeSession()
        self.assertTrue(succeed_job(session, 3, "worker-a"))
        sql, params = session.calls[-1]
        self.assertIn("state = 'running'", sql)
        self.assertEqual(params["worker_id"], "worker-a")
        self.assertTrue(
            retry_job(session, 3, "worker-a", datetime.now(UTC), ValueError("secret"))
        )
        self.assertEqual(session.calls[-1][1]["last_error"], "ValueError")
        self.assertTrue(terminal_fail_job(session, 3, "worker-a", "private payload"))
        self.assertEqual(session.calls[-1][1]["last_error"], "Error")

    def test_error_sanitization_never_returns_exception_text(self):
        self.assertEqual(sanitize_error(RuntimeError("token=secret")), "RuntimeError")
        self.assertEqual(sanitize_error("password=secret"), "Error")


class AnalysisJobWorkerTests(unittest.TestCase):
    def test_disabled_configuration_does_not_open_a_session(self):
        factory = unittest.mock.Mock()
        worker = AnalysisJobWorker({}, session_factory=factory)
        self.assertFalse(worker.start())
        self.assertEqual(worker.poll_once()["claimed"], 0)
        factory.assert_not_called()

    def test_poll_exception_is_bounded_and_does_not_escape(self):
        @contextmanager
        def failing_session(*args):
            raise RuntimeError("database unavailable")
            yield

        worker = AnalysisJobWorker(
            {"event_pipeline": {"jobs": {"enabled": True}}},
            session_factory=failing_session,
        )
        counters = worker.poll_once()
        self.assertEqual(counters["poll_errors"], 1)
        self.assertNotIn("database unavailable", str(counters))

    def test_heartbeat_renews_the_running_job_lease(self):
        sessions = []

        @contextmanager
        def session_factory(*args):
            session = SimpleNamespace()
            sessions.append(session)
            yield session

        stop = unittest.mock.Mock()
        stop.wait.side_effect = [False, True]
        worker = AnalysisJobWorker(
            {"event_pipeline": {"jobs": {"enabled": True}}},
            worker_id="worker-a",
            session_factory=session_factory,
        )
        with patch("job_worker.renew_job_lease", return_value=True) as renew:
            worker._heartbeat(3, 6.0, stop)

        renew.assert_called_once_with(sessions[0], 3, "worker-a", 6.0)
        self.assertEqual(stop.wait.call_args_list[0].args, (2.0,))

    def test_result_reference_accepts_snapshot_result_objects(self):
        result = SimpleNamespace(
            snapshot_id="snapshot-1",
            section_key="watchlist",
            scope_key="global",
            version=2,
            changed=True,
            private_value="not exposed",
        )

        self.assertEqual(
            AnalysisJobWorker._result_ref(result),
            {
                "snapshot_id": "snapshot-1",
                "section_key": "watchlist",
                "scope_key": "global",
                "version": 2,
                "changed": True,
            },
        )

    def test_claimed_job_handler_failure_is_retried_without_sleeping(self):
        claimed = AnalysisJob.from_row(JOB_ROW)
        calls = []

        @contextmanager
        def sessions(*args):
            session = SimpleNamespace()
            calls.append(session)
            yield session

        worker = AnalysisJobWorker(
            {
                "event_pipeline": {
                    "jobs": {
                        "enabled": True,
                        "retry": {"base_seconds": 1, "jitter_seconds": 0},
                    }
                }
            },
            worker_id="worker-a",
            session_factory=sessions,
        )
        with (
            patch("job_worker.reconcile_jobs", return_value=0),
            patch("job_worker.claim_jobs", return_value=[claimed]),
            patch("job_worker.start_job", return_value=True),
            patch(
                "analysis_job_handlers.route_job", side_effect=RuntimeError("secret")
            ),
            patch("job_worker.retry_job", return_value=True),
        ):
            counters = worker.poll_once()
        self.assertEqual(counters["retried"], 1)
        self.assertEqual(counters["handler_errors"], 1)
        self.assertEqual(len(calls), 4)


if __name__ == "__main__":
    unittest.main()
