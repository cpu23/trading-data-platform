import sys
import threading
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis_job_handlers import route_job
from errors import sanitize_error
from job_worker import AnalysisJobWorker
from jobs import (
    AnalysisJob,
    enqueue_job,
    retry_job,
    succeed_job,
    terminal_fail_job,
)
from research_intelligence.operations import (
    enqueue_research_job,
    retry_research_job,
)

from contracts.runtime_config import (
    AppConfig,
    DatabaseConfig,
    LlmConfig,
    ResearchIntelligenceConfig,
)


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
        self.assertEqual(session.calls[-1][1]["last_error"], "private payload")

    def test_error_sanitization_never_returns_exception_text(self):
        self.assertEqual(sanitize_error(RuntimeError("token=secret")), "RuntimeError")
        self.assertEqual(sanitize_error("password=secret"), "password=[REDACTED]")
        self.assertIsNone(sanitize_error(None))


class AnalysisJobWorkerTests(unittest.TestCase):
    def test_disabled_configuration_does_not_open_a_session(self):
        factory = unittest.mock.Mock()
        worker = AnalysisJobWorker({}, session_factory=factory)
        self.assertFalse(worker.start())
        self.assertEqual(worker.poll_once()["claimed"], 0)
        factory.assert_not_called()

    def test_stop_waits_for_inflight_poll_to_reach_job_boundary(self):
        worker = AnalysisJobWorker(
            {"event_pipeline": {"jobs": {"enabled": True}}},
            worker_id="analysis-test",
        )
        entered = threading.Event()
        release = threading.Event()

        def blocked_poll():
            entered.set()
            release.wait()
            return {}

        with patch.object(worker, "poll_once", side_effect=blocked_poll):
            self.assertTrue(worker.start())
            self.assertTrue(entered.wait(1))
            stopper = threading.Thread(target=worker.stop)
            stopper.start()
            self.assertTrue(worker._stop.wait(1))
            stopper.join(timeout=0.05)
            self.assertTrue(stopper.is_alive())
            release.set()
            stopper.join(timeout=1)

        self.assertFalse(stopper.is_alive())
        self.assertFalse(worker.state["running"])

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

    def test_poll_claims_exactly_one_job(self):
        @contextmanager
        def sessions(*args):
            yield SimpleNamespace()

        worker = AnalysisJobWorker(
            {
                "event_pipeline": {
                    "jobs": {
                        "enabled": True,
                        "worker": {"batch_size": 99},
                    }
                }
            },
            worker_id="worker-a",
            session_factory=sessions,
        )
        with (
            patch("job_worker.reconcile_jobs", return_value=0),
            patch("job_worker.claim_jobs", return_value=[]) as claim,
        ):
            worker.poll_once()

        # A sequential worker claims exactly one job per poll so an unhandled
        # in-memory claim can never expire and be reclaimed by a replica.
        self.assertEqual(claim.call_args.kwargs["limit"], 1)

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

    def test_result_reference_keeps_autonomy_outcome_counts(self):
        self.assertEqual(
            AnalysisJobWorker._result_ref(
                {
                    "status": "completed",
                    "cost_usd": 0.07,
                    "promoted_count": 12,
                    "falsification_runs": 4,
                    "private_value": "not exposed",
                }
            ),
            {
                "status": "completed",
                "cost_usd": 0.07,
                "promoted_count": 12,
                "falsification_runs": 4,
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


class ResearchOperationsTests(unittest.TestCase):
    CONFIG = AppConfig(
        database=DatabaseConfig(
            host="localhost",
            port=5432,
            name="test",
            user="research_runner_ci",
            password="s9V!q2K#x7Lm4P@t",
        ),
        llm=LlmConfig(api_key="rI8nW3qY5vT2mK7pL9sF4dH6"),
        research_intelligence=ResearchIntelligenceConfig(enabled=True),
    )

    @staticmethod
    @contextmanager
    def _session(*args):
        yield object()

    def test_normal_refreshes_coalesce_but_forced_rebuilds_have_unique_identity(self):
        def enqueue(session, **kwargs):
            return SimpleNamespace(
                inserted=True,
                job=SimpleNamespace(
                    id=uuid4(),
                    correlation_id=kwargs["correlation_id"],
                ),
            )

        accepted = datetime(2026, 8, 8, 12, tzinfo=UTC)
        with (
            patch(
                "research_intelligence.operations.accept_run",
                return_value=accepted,
            ),
            patch("research_intelligence.operations.start_run", return_value=True),
            patch(
                "research_intelligence.operations.get_session",
                side_effect=self._session,
            ),
            patch(
                "research_intelligence.operations.enqueue_job",
                side_effect=enqueue,
            ) as enqueue_call,
            patch(
                "research_intelligence.operations.finalize_run_safely",
                return_value=True,
            ),
        ):
            enqueue_research_job(
                self.CONFIG,
                job_type="research_discovery",
            )
            enqueue_research_job(
                self.CONFIG,
                job_type="research_discovery",
            )
            enqueue_research_job(
                self.CONFIG,
                job_type="research_discovery",
                force=True,
            )
            enqueue_research_job(
                self.CONFIG,
                job_type="research_discovery",
                force=True,
            )

        fingerprints = [
            call.kwargs["input_fingerprint"] for call in enqueue_call.call_args_list
        ]
        self.assertEqual(fingerprints[0], fingerprints[1])
        self.assertNotEqual(fingerprints[1], fingerprints[2])
        self.assertNotEqual(fingerprints[2], fingerprints[3])

    def test_start_failure_finalizes_accepted_run_without_enqueuing(self):
        accepted = datetime(2026, 8, 8, 12, tzinfo=UTC)
        with (
            patch(
                "research_intelligence.operations.accept_run",
                return_value=accepted,
            ),
            patch(
                "research_intelligence.operations.start_run",
                side_effect=RuntimeError("database unavailable"),
            ),
            patch("research_intelligence.operations.enqueue_job") as enqueue_call,
            patch(
                "research_intelligence.operations.finalize_run_safely",
                return_value=True,
            ) as finalize,
        ):
            with self.assertRaises(RuntimeError):
                enqueue_research_job(
                    self.CONFIG,
                    job_type="research_discovery",
                )

        enqueue_call.assert_not_called()
        self.assertEqual(finalize.call_args.args[1], "failed")
        self.assertEqual(finalize.call_args.kwargs["run_kind"], "research")
        self.assertNotIn("database unavailable", str(finalize.call_args))

    def test_terminal_retry_enqueues_new_identity_without_mutating_prior_job(self):
        job_id = str(uuid4())
        case_id = str(uuid4())
        session = SimpleNamespace()
        session.execute = unittest.mock.Mock(
            return_value=Result(
                first={
                    "id": job_id,
                    "job_type": "research_case_update",
                    "state": "terminal_failed",
                    "payload": {"case_id": case_id, "force": False},
                    "correlation_id": uuid4(),
                }
            )
        )

        @contextmanager
        def sessions(*args):
            yield session

        with (
            patch(
                "research_intelligence.operations.get_session",
                side_effect=sessions,
            ),
            patch(
                "research_intelligence.operations.enqueue_research_job",
                return_value={"status": "queued"},
            ) as enqueue,
        ):
            result = retry_research_job(self.CONFIG, job_id)

        self.assertEqual(result, {"status": "queued"})
        self.assertEqual(session.execute.call_count, 1)
        kwargs = enqueue.call_args.kwargs
        self.assertEqual(kwargs["case_id"], case_id)
        self.assertEqual(kwargs["triggered_by"], "retry")
        self.assertIn(job_id, kwargs["request_nonce"])


class ResearchIntelligenceJobTests(unittest.TestCase):
    def test_discovery_job_runs_macro_and_case_stages_and_returns_observable_counts(
        self,
    ):
        session = object()
        config = object()
        job = SimpleNamespace(
            job_type="research_discovery",
            payload={"force": True},
            correlation_id="correlation",
        )
        with (
            patch("analysis_job_handlers.load_config", return_value=config),
            patch(
                "research_intelligence.service.run_macro_transmission",
                return_value={"driver_count": 3, "model_cost_usd": 0.25},
            ) as macro,
            patch(
                "research_intelligence.service.run_discovery",
                return_value={
                    "cases": [
                        {"case_id": "case-1", "status": "created"},
                        {"case_id": "case-2", "status": "updated"},
                    ],
                    "candidate_count": 3,
                    "errors": [],
                    "model_cost_usd": 0.5,
                },
            ) as discovery,
        ):
            result = route_job(session, job)

        self.assertEqual(
            result,
            {
                "status": "completed",
                "case_count": 2,
                "candidate_count": 3,
                "abstention_count": 0,
                "driver_count": 3,
                "lifecycle_transition_count": 0,
                "error_count": 0,
                "cost_usd": 0.75,
            },
        )
        macro.assert_called_once_with(
            session,
            config,
            correlation_id="correlation",
            force=True,
        )
        discovery.assert_called_once_with(
            session,
            config,
            correlation_id="correlation",
            force=True,
        )

    def test_one_macro_stage_failure_is_inspectable_without_losing_case_work(self):
        session = object()
        config = object()
        job = SimpleNamespace(
            job_type="research_discovery", payload={}, correlation_id=None
        )
        with (
            patch("analysis_job_handlers.load_config", return_value=config),
            patch(
                "research_intelligence.service.run_macro_transmission",
                side_effect=RuntimeError("unavailable"),
            ),
            patch(
                "research_intelligence.service.run_discovery",
                return_value={
                    "cases": [
                        {"case_id": "case-1", "status": "updated"},
                        {"case_id": None, "status": "abstained"},
                    ],
                    "candidate_count": 2,
                    "errors": [{"stage": "value_capture"}],
                    "model_cost_usd": 0.1,
                },
            ),
        ):
            result = route_job(session, job)

        self.assertEqual(result["status"], "completed_with_errors")
        self.assertEqual(result["case_count"], 1)
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["abstention_count"], 1)
        self.assertEqual(result["driver_count"], 0)
        self.assertEqual(result["error_count"], 2)
        self.assertEqual(result["cost_usd"], 0.1)

    def test_case_update_job_rejects_missing_case_identity(self):
        job = SimpleNamespace(
            job_type="research_case_update", payload={}, correlation_id=None
        )
        with patch("analysis_job_handlers._config", return_value={}):
            with self.assertRaisesRegex(ValueError, "requires a case id"):
                route_job(object(), job)


class ThesisAutonomyJobTests(unittest.TestCase):
    def test_route_job_dispatches_thesis_autonomy_run_with_bounded_result(self):
        job = SimpleNamespace(
            job_type="thesis_autonomy_run",
            payload={"as_of": "2026-08-15T09:30:00+00:00"},
            correlation_id="corr-1",
        )
        with (
            patch(
                "analysis_job_handlers._config",
                return_value={"thesis_autonomy": {"enabled": True}},
            ),
            patch(
                "thesis_autonomy.run_autonomous_thesis_cycle",
                return_value={
                    "status": "completed",
                    "error_count": 1,
                    "cost_usd": 0.4,
                    "promoted_count": 3,
                    "falsification_runs": 2,
                },
            ) as cycle,
        ):
            result = route_job(FakeSession(), job)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(result["cost_usd"], 0.4)
        self.assertEqual(result["promoted_count"], 3)
        self.assertEqual(result["falsification_runs"], 2)
        cycle.assert_called_once()
        self.assertEqual(cycle.call_args.kwargs["correlation_id"], "corr-1")
        self.assertEqual(cycle.call_args.kwargs["as_of"], "2026-08-15T09:30:00+00:00")

    def test_unsupported_job_type_is_rejected_without_leaking(self):
        job = SimpleNamespace(job_type="mystery_job", payload={})
        with self.assertRaisesRegex(ValueError, "unsupported analysis job type"):
            route_job(FakeSession(), job)


if __name__ == "__main__":
    unittest.main()
