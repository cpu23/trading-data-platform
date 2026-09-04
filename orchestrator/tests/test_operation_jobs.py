"""Integration tests for the durable operation-job lifecycle.

Uses a real SQLite engine (same SQL via the repository's dialect guards) to
exercise: acceptance+enqueue atomicity, lease claim/renew/expiry/reclaim,
crash recovery, poison terminal state, idempotent completion, and duplicate
scheduler suppression.
"""

import sys
import threading
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobs import (
    OperationJob,
    accept_and_enqueue_operation,
    claim_operation_jobs,
    enqueue_operation,
    latest_cycle_status,
    operation_queue_summary,
    reconcile_operation_jobs,
    renew_operation_job_lease,
    retry_operation_job,
    start_operation_job,
    succeed_operation_job,
)
from operation_worker import OperationWorker
from role_heartbeat import (
    heartbeat_is_fresh,
    list_role_heartbeats,
    prune_stale_role_heartbeats,
    read_role_heartbeat,
    role_has_fresh_healthy_instance,
    update_role_heartbeat,
)
from run_lifecycle import RunAcceptanceConflict
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

SCHEMA = """
CREATE TABLE cycle_runs (
    correlation_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    accepted_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    worker_id TEXT,
    idempotency_key TEXT,
    completed_at TIMESTAMPTZ,
    error_message TEXT,
    triggered_by TEXT NOT NULL DEFAULT 'manual',
    run_kind TEXT NOT NULL DEFAULT 'cycle',
    requested_component TEXT,
    result_status TEXT,
    summary TEXT DEFAULT '{}'
);
CREATE UNIQUE INDEX idx_cycle_runs_idempotency_key
    ON cycle_runs (idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    job_type TEXT,
    run_kind TEXT,
    requested_component TEXT,
    correlation_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL DEFAULT 100,
    dedupe_key TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    not_before TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    lease_expires_at TIMESTAMPTZ,
    claimed_by TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    payload TEXT NOT NULL DEFAULT '{}',
    result_ref TEXT,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX idx_jobs_active_identity
    ON jobs (run_kind, dedupe_key, input_fingerprint)
    WHERE state IN ('queued','leased','running','failed_retryable');
CREATE TABLE role_heartbeats (
    role TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    status TEXT NOT NULL,
    last_heartbeat_at TIMESTAMPTZ NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (role, instance_id)
);
CREATE TABLE quote_state (
    symbol TEXT PRIMARY KEY,
    price REAL NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class OperationJobSqliteTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        with self.engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys = ON"))
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(text(statement))
        self.Session = sessionmaker(bind=self.engine)
        self.config = {"database": {"host": "sqlite-test"}}

    @contextmanager
    def session(self):
        session = self.Session()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def _accept_and_enqueue(self, **kwargs):
        defaults = {
            "run_kind": "collector",
            "requested_component": "fred",
            "dedupe_key": f"collector:fred:{uuid4()}",
            "input_fingerprint": "f" * 64,
            "payload": {"mode": "refresh"},
            "triggered_by": "api",
        }
        defaults.update(kwargs)
        defaults["correlation_id"] = kwargs.get("correlation_id") or str(uuid4())
        with patch("orchestrator.get_session", side_effect=lambda cfg: self.session()):
            return accept_and_enqueue_operation(self.config, **defaults)

    def _run_row(self, correlation_id: str) -> dict:
        with self.session() as session:
            row = (
                session.execute(
                    text("SELECT * FROM cycle_runs WHERE correlation_id = :cid"),
                    {"cid": correlation_id},
                )
                .mappings()
                .first()
            )
            return dict(row) if row is not None else None

    def _job_row(self, job_id: str) -> dict:
        with self.session() as session:
            row = (
                session.execute(
                    text("SELECT * FROM jobs WHERE id = :id"),
                    {"id": job_id},
                )
                .mappings()
                .first()
            )
            return dict(row) if row is not None else None

    # ── acceptance atomicity ─────────────────────────────────────────────

    def test_acceptance_and_enqueue_commit_together(self):
        accepted_at, enqueued = self._accept_and_enqueue()
        self.assertTrue(enqueued.inserted)
        run = self._run_row(str(enqueued.job.correlation_id))
        self.assertEqual(run["status"], "accepted")
        self.assertEqual(run["run_kind"], "collector")
        self.assertEqual(run["requested_component"], "fred")
        job = self._job_row(str(enqueued.job.id))
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["run_kind"], "collector")

    def test_enqueue_failure_rolls_back_acceptance(self):
        # Invalid run_kind raises inside the same transaction; the cycle_runs
        # acceptance row must not survive.
        with patch("orchestrator.get_session", side_effect=lambda cfg: self.session()):
            with self.assertRaises(ValueError):
                accept_and_enqueue_operation(
                    self.config,
                    correlation_id=str(uuid4()),
                    triggered_by="api",
                    run_kind="bogus",
                    requested_component=None,
                    dedupe_key="k",
                    input_fingerprint="f" * 64,
                    payload=None,
                )
        # A fresh session sees no leftover rows: the transaction rolled back.
        with self.session() as session:
            count = session.execute(text("SELECT COUNT(*) FROM cycle_runs")).scalar()
        self.assertEqual(count, 0)

    def test_duplicate_correlation_is_a_conflict_without_job(self):
        first = self._accept_and_enqueue()
        correlation_id = str(first[1].job.correlation_id)
        with self.assertRaises(RunAcceptanceConflict):
            self._accept_and_enqueue(correlation_id=correlation_id)
        with self.session() as session:
            jobs = session.execute(text("SELECT COUNT(*) FROM jobs")).scalar()
        self.assertEqual(jobs, 1)

    def test_idempotency_key_replay_returns_original_acceptance(self):
        key = "idem-1"
        first_cid = str(uuid4())
        accepted_at, enqueued = self._accept_and_enqueue(
            correlation_id=first_cid,
            idempotency_key=key,
            run_kind="collector",
            requested_component="fred",
            dedupe_key=None,
        )
        self.assertTrue(enqueued.inserted)
        # Lost-202 retry: same key + same request identity, NEW correlation.
        accepted_at2, enqueued2 = self._accept_and_enqueue(
            idempotency_key=key,
            run_kind="collector",
            requested_component="fred",
            dedupe_key=None,
        )
        self.assertFalse(enqueued2.inserted)
        self.assertFalse(enqueued2.suppressed)
        self.assertEqual(str(enqueued2.job.correlation_id), first_cid)
        self.assertEqual(accepted_at2, accepted_at)
        with self.session() as session:
            runs = session.execute(text("SELECT COUNT(*) FROM cycle_runs")).scalar()
            jobs = session.execute(text("SELECT COUNT(*) FROM jobs")).scalar()
        self.assertEqual((runs, jobs), (1, 1))
        # Same key with a DIFFERENT request identity is a 409 conflict.
        with self.assertRaises(RunAcceptanceConflict):
            self._accept_and_enqueue(
                idempotency_key=key,
                run_kind="processor",
                requested_component="briefing",
                dedupe_key=None,
            )
        with self.session() as session:
            runs = session.execute(text("SELECT COUNT(*) FROM cycle_runs")).scalar()
            jobs = session.execute(text("SELECT COUNT(*) FROM jobs")).scalar()
        self.assertEqual((runs, jobs), (1, 1))

    def test_budget_override_placeholder_row_is_adopted(self):
        # API registers an override placeholder row before dispatch.
        correlation_id = str(uuid4())
        with self.session() as session:
            session.execute(
                text(
                    "INSERT INTO cycle_runs (correlation_id, status, started_at, "
                    "triggered_by, run_kind, requested_component, summary) "
                    "VALUES (:cid, 'running', :now, 'api_manual_override', "
                    "'processor', 'briefing', :summary)"
                ),
                {
                    "cid": correlation_id,
                    "now": datetime.now(UTC),
                    "summary": '{"budget_override": {"requested": true}}',
                },
            )
        accepted_at, enqueued = self._accept_and_enqueue(
            correlation_id=correlation_id,
            run_kind="processor",
            requested_component="briefing",
            request_summary={"mode": "refresh"},
        )
        self.assertTrue(enqueued.inserted)
        run = self._run_row(correlation_id)
        self.assertEqual(run["status"], "accepted")
        self.assertIn("budget_override", run["summary"])
        self.assertIn("mode", run["summary"])

    # ── leases, expiry, reclaim ───────────────────────────────────────────

    def test_claim_is_exclusive_and_lease_renewal_extends_it(self):
        _, enqueued = self._accept_and_enqueue(max_attempts=5)
        job_id = str(enqueued.job.id)
        with self.session() as session:
            claimed = claim_operation_jobs(session, "worker-a", lease_seconds=120)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].state, "leased")
        self.assertEqual(claimed[0].claimed_by, "worker-a")
        self.assertEqual(claimed[0].attempt_count, 1)
        with self.session() as session:
            second = claim_operation_jobs(session, "worker-b", lease_seconds=120)
        self.assertEqual(second, [])
        with self.session() as session:
            renewed = renew_operation_job_lease(
                session, job_id, "worker-a", lease_seconds=120
            )
        self.assertTrue(renewed)

    def test_expired_lease_is_reclaimable_and_start_guards_owner(self):
        _, enqueued = self._accept_and_enqueue(max_attempts=5)
        job_id = str(enqueued.job.id)
        with self.session() as session:
            claim_operation_jobs(session, "worker-a", lease_seconds=1)
        with self.session() as session:
            # Simulate a crashed worker: expire the lease directly.
            session.execute(
                text("UPDATE jobs SET lease_expires_at = :expired WHERE id = :id"),
                {"id": job_id, "expired": datetime.now(UTC) - timedelta(seconds=1)},
            )
        with self.session() as session:
            claimed = claim_operation_jobs(session, "worker-b", lease_seconds=120)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].claimed_by, "worker-b")
        self.assertEqual(claimed[0].attempt_count, 2)
        with self.session() as session:
            # Old owner can no longer start or succeed the job.
            self.assertFalse(start_operation_job(session, job_id, "worker-a"))
            self.assertTrue(start_operation_job(session, job_id, "worker-b"))
            self.assertFalse(succeed_operation_job(session, job_id, "worker-a"))
            self.assertTrue(succeed_operation_job(session, job_id, "worker-b"))

    def test_reconcile_marks_expired_leases_retryable_then_terminal(self):
        _, enqueued = self._accept_and_enqueue(max_attempts=2)
        job_id = str(enqueued.job.id)
        with self.session() as session:
            claim_operation_jobs(session, "worker-a", lease_seconds=120)
        with self.session() as session:
            session.execute(
                text("UPDATE jobs SET lease_expires_at = :expired WHERE id = :id"),
                {"id": job_id, "expired": datetime.now(UTC) - timedelta(seconds=1)},
            )
            repaired = reconcile_operation_jobs(session, limit=10)
        self.assertEqual(repaired, 1)
        row = self._job_row(job_id)
        self.assertEqual(row["state"], "failed_retryable")
        # Second expiry after another claim exhausts attempts -> poison.
        with self.session() as session:
            claim_operation_jobs(session, "worker-b", lease_seconds=120)
        with self.session() as session:
            session.execute(
                text("UPDATE jobs SET lease_expires_at = :expired WHERE id = :id"),
                {"id": job_id, "expired": datetime.now(UTC) - timedelta(seconds=1)},
            )
            repaired = reconcile_operation_jobs(session, limit=10)
        self.assertEqual(repaired, 1)
        row = self._job_row(job_id)
        self.assertEqual(row["state"], "failed_terminal")

    # ── crash recovery ───────────────────────────────────────────────────

    def test_retry_transition_reclaims_run_row_in_same_transaction(self):
        _, enqueued = self._accept_and_enqueue(max_attempts=5, run_kind="collector")
        job_id = str(enqueued.job.id)
        correlation_id = str(enqueued.job.correlation_id)
        with self.session() as session:
            claim_operation_jobs(session, "worker-a", lease_seconds=120)
            self.assertTrue(start_operation_job(session, job_id, "worker-a"))
            session.execute(
                text(
                    "UPDATE cycle_runs SET status = 'running', worker_id = 'worker-a:r1', "
                    "started_at = :n, heartbeat_at = :n WHERE correlation_id = :c"
                ),
                {"n": datetime.now(UTC), "c": correlation_id},
            )
        with self.session() as session:
            retried = retry_operation_job(
                session,
                job_id,
                "worker-a",
                datetime.now(UTC) + timedelta(seconds=1),
                RuntimeError("boom"),
            )
        self.assertTrue(retried)
        run = self._run_row(correlation_id)
        self.assertEqual(run["status"], "accepted")
        self.assertIsNone(run["worker_id"])
        self.assertIsNone(run["heartbeat_at"])
        # The retried attempt can reclaim immediately after not_before without
        # waiting for any stale timeout.
        with self.session() as session:
            session.execute(
                text("UPDATE jobs SET not_before = :n WHERE id = :id"),
                {"n": datetime.now(UTC), "id": job_id},
            )
            claimed = claim_operation_jobs(session, "worker-b", lease_seconds=120)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].claimed_by, "worker-b")
        with self.session() as session:
            self.assertTrue(start_operation_job(session, job_id, "worker-b"))
            self.assertTrue(succeed_operation_job(session, job_id, "worker-b"))

    def test_success_finalize_is_atomic_and_rolls_back_on_lease_lost(self):
        _, enqueued = self._accept_and_enqueue(max_attempts=3, run_kind="collector")
        job_id = str(enqueued.job.id)
        correlation_id = str(enqueued.job.correlation_id)
        fixed = uuid4()
        run_worker_id = f"worker-x:{fixed}"
        with self.session() as session:
            session.execute(
                text(
                    "UPDATE cycle_runs SET status = 'running', worker_id = :w, "
                    "started_at = :n, heartbeat_at = :n WHERE correlation_id = :c"
                ),
                {"w": run_worker_id, "n": datetime.now(UTC), "c": correlation_id},
            )
        worker = OperationWorker(
            {"event_pipeline": {"jobs": {"enabled": True}}},
            worker_id="worker-x",
            session_factory=lambda cfg: self.session(),
        )
        claimed = OperationJob.from_row(self._job_row(job_id))
        with (
            patch("operation_worker.uuid4", return_value=fixed),
            patch("operation_worker.start_run", return_value=True),
            patch("operation_worker.maintain_run_heartbeat") as heartbeat,
            patch.object(
                worker,
                "_dispatch",
                return_value={"status": "success", "error": None},
            ),
            patch("operation_worker.succeed_operation_job", return_value=False),
            # Simulate the reclaimed job's new owner already taking it over, so
            # the retry transition no-ops and the rollback stands alone.
            patch("operation_worker.retry_operation_job", return_value=False),
        ):
            heartbeat.return_value.__enter__ = Mock(return_value=None)
            heartbeat.return_value.__exit__ = Mock(return_value=False)
            worker._handle(claimed, {"retry": {"max_attempts": 3}})

        # Lease loss during the joint finalize rolls BOTH transitions back.
        run = self._run_row(correlation_id)
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["worker_id"], run_worker_id)
        self.assertEqual(self._job_row(job_id)["state"], "running")

    def test_terminal_finalize_rolls_back_when_finish_raises(self):
        _, enqueued = self._accept_and_enqueue(max_attempts=1, run_kind="collector")
        job_id = str(enqueued.job.id)
        correlation_id = str(enqueued.job.correlation_id)
        fixed = uuid4()
        run_worker_id = f"worker-x:{fixed}"
        with self.session() as session:
            session.execute(
                text(
                    "UPDATE cycle_runs SET status = 'running', worker_id = :w, "
                    "started_at = :n, heartbeat_at = :n WHERE correlation_id = :c"
                ),
                {"w": run_worker_id, "n": datetime.now(UTC), "c": correlation_id},
            )
        worker = OperationWorker(
            {"event_pipeline": {"jobs": {"enabled": True}}},
            worker_id="worker-x",
            session_factory=lambda cfg: self.session(),
        )
        claimed = OperationJob.from_row(self._job_row(job_id))
        with (
            patch("operation_worker.uuid4", return_value=fixed),
            patch("operation_worker.start_run", return_value=True),
            patch("operation_worker.maintain_run_heartbeat") as heartbeat,
            patch.object(worker, "_dispatch", side_effect=RuntimeError("boom")),
            patch(
                "operation_worker.finish_run_in_session",
                side_effect=RuntimeError("finalize db failure"),
            ),
        ):
            heartbeat.return_value.__enter__ = Mock(return_value=None)
            heartbeat.return_value.__exit__ = Mock(return_value=False)
            worker._handle(claimed, {"retry": {"max_attempts": 1}})

        # The injected finish failure rolled the whole terminal transition back.
        self.assertEqual(self._job_row(job_id)["state"], "running")
        self.assertEqual(self._run_row(correlation_id)["status"], "running")

    def test_claim_reclaims_stale_running_run_row(self):
        _, enqueued = self._accept_and_enqueue(max_attempts=5)
        correlation_id = str(enqueued.job.correlation_id)
        with self.session() as session:
            # A dead worker left the run row running.
            session.execute(
                text(
                    "UPDATE cycle_runs SET status = 'running', worker_id = 'dead', "
                    "started_at = :now, heartbeat_at = :old "
                    "WHERE correlation_id = :cid"
                ),
                {
                    "cid": correlation_id,
                    "now": datetime.now(UTC),
                    "old": datetime.now(UTC) - timedelta(minutes=30),
                },
            )
        with self.session() as session:
            claimed = claim_operation_jobs(session, "worker-c", lease_seconds=120)
        self.assertEqual(len(claimed), 1)
        run = self._run_row(correlation_id)
        self.assertEqual(run["status"], "accepted")
        self.assertIsNone(run["worker_id"])

    def test_recover_operation_runs_reclaims_active_and_abandons_orphans(self):
        from recovery import recover_operation_runs

        with self.session() as session:
            session.execute(
                text(
                    "INSERT INTO cycle_runs (correlation_id, status, accepted_at, "
                    "started_at, heartbeat_at, triggered_by, run_kind) "
                    "VALUES (:active, 'running', :old, :old, :old, 'api', 'cycle'), "
                    "(:orphan, 'running', :old, :old, :old, 'api', 'cycle'), "
                    "(:stale_accepted, 'accepted', :old, NULL, NULL, 'api', 'cycle')"
                ),
                {
                    "active": "active-run",
                    "orphan": "orphan-run",
                    "stale_accepted": "stale-accepted",
                    "old": datetime.now(UTC) - timedelta(minutes=30),
                },
            )
        with self.session() as session:
            enqueue_operation(
                session,
                run_kind="cycle",
                correlation_id="active-run",
                dedupe_key="active",
                input_fingerprint="f" * 64,
                payload={"mode": "refresh"},
            )
        with patch("recovery._get_session", side_effect=lambda cfg: self.session()):
            result = recover_operation_runs(self.config)
        self.assertEqual(result["reclaimed_ids"], ["active-run"])
        self.assertEqual(result["accepted_ids"], ["stale-accepted"])
        self.assertEqual(result["running_ids"], ["orphan-run"])
        run = self._run_row("active-run")
        self.assertEqual(run["status"], "accepted")
        self.assertEqual(self._run_row("orphan-run")["status"], "abandoned")
        self.assertEqual(self._run_row("stale-accepted")["status"], "abandoned")

    # ── poison job and idempotent completion through the worker ───────────

    def test_returned_retryable_failure_retries_then_succeeds(self):
        _, enqueued = self._accept_and_enqueue(max_attempts=3, run_kind="collector")
        job_id = str(enqueued.job.id)
        correlation_id = str(enqueued.job.correlation_id)
        worker = OperationWorker(
            {"event_pipeline": {"jobs": {"enabled": True}}},
            worker_id="worker-x",
            session_factory=lambda cfg: self.session(),
            random_source=lambda a, b: 0.0,
        )
        fixed = uuid4()
        run_worker_id = f"worker-x:{fixed}"
        results = [
            {"status": "failed", "retryable": True, "error": "source unavailable"},
            {"status": "success", "error": None},
        ]

        def claim_and_handle(expectation):
            with self.session() as session:
                claim_operation_jobs(session, "worker-x", lease_seconds=120)
                session.execute(
                    text(
                        "UPDATE cycle_runs SET status = 'running', worker_id = :w, "
                        "started_at = :n, heartbeat_at = :n WHERE correlation_id = :c"
                    ),
                    {"w": run_worker_id, "n": datetime.now(UTC), "c": correlation_id},
                )
            claimed = OperationJob.from_row(self._job_row(job_id))
            with (
                patch("operation_worker.uuid4", return_value=fixed),
                patch("operation_worker.start_run", return_value=True),
                patch("operation_worker.maintain_run_heartbeat") as heartbeat,
                patch.object(worker, "_dispatch", return_value=expectation),
            ):
                heartbeat.return_value.__enter__ = Mock(return_value=None)
                heartbeat.return_value.__exit__ = Mock(return_value=False)
                worker._handle(claimed, {"retry": {"max_attempts": 3}})

        claim_and_handle(results[0])
        # Retryable failure: job went failed_retryable and released the run row.
        self.assertEqual(self._job_row(job_id)["state"], "failed_retryable")
        self.assertEqual(self._run_row(correlation_id)["status"], "accepted")

        # Second claim: the run executes and finalizes normally.
        claim_and_handle(results[1])
        self.assertEqual(self._job_row(job_id)["state"], "succeeded")
        self.assertEqual(self._run_row(correlation_id)["status"], "completed")

    def test_returned_nonretryable_failure_poisons_job_and_run(self):
        _, enqueued = self._accept_and_enqueue(max_attempts=3, run_kind="collector")
        job_id = str(enqueued.job.id)
        correlation_id = str(enqueued.job.correlation_id)
        worker = OperationWorker(
            {"event_pipeline": {"jobs": {"enabled": True}}},
            worker_id="worker-x",
            session_factory=lambda cfg: self.session(),
            random_source=lambda a, b: 0.0,
        )
        fixed = uuid4()
        run_worker_id = f"worker-x:{fixed}"
        with (
            patch("operation_worker.uuid4", return_value=fixed),
            patch("operation_worker.start_run", return_value=True),
            patch("operation_worker.maintain_run_heartbeat") as heartbeat,
            patch.object(
                worker,
                "_dispatch",
                return_value={"status": "failed", "retryable": False, "error": "bad"},
            ),
        ):
            heartbeat.return_value.__enter__ = Mock(return_value=None)
            heartbeat.return_value.__exit__ = Mock(return_value=False)
            with self.session() as session:
                claim_operation_jobs(session, "worker-x", lease_seconds=120)
                session.execute(
                    text(
                        "UPDATE cycle_runs SET status = 'running', worker_id = :w, "
                        "started_at = :n, heartbeat_at = :n WHERE correlation_id = :c"
                    ),
                    {"w": run_worker_id, "n": datetime.now(UTC), "c": correlation_id},
                )
            claimed = OperationJob.from_row(self._job_row(job_id))
            worker._handle(claimed, {"retry": {"max_attempts": 3}})

        self.assertEqual(self._job_row(job_id)["state"], "failed_terminal")
        run = self._run_row(correlation_id)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["result_status"], "failed")
        self.assertIn("bad", run["error_message"])

    def test_returned_retryable_failure_poisons_at_attempt_exhaustion(self):
        _, enqueued = self._accept_and_enqueue(max_attempts=1, run_kind="collector")
        job_id = str(enqueued.job.id)
        correlation_id = str(enqueued.job.correlation_id)
        worker = OperationWorker(
            {"event_pipeline": {"jobs": {"enabled": True}}},
            worker_id="worker-x",
            session_factory=lambda cfg: self.session(),
            random_source=lambda a, b: 0.0,
        )
        fixed = uuid4()
        run_worker_id = f"worker-x:{fixed}"
        with (
            patch("operation_worker.uuid4", return_value=fixed),
            patch("operation_worker.start_run", return_value=True),
            patch("operation_worker.maintain_run_heartbeat") as heartbeat,
            patch.object(
                worker,
                "_dispatch",
                return_value={"status": "failed", "retryable": True},
            ),
        ):
            heartbeat.return_value.__enter__ = Mock(return_value=None)
            heartbeat.return_value.__exit__ = Mock(return_value=False)
            with self.session() as session:
                claim_operation_jobs(session, "worker-x", lease_seconds=120)
                session.execute(
                    text(
                        "UPDATE cycle_runs SET status = 'running', worker_id = :w, "
                        "started_at = :n, heartbeat_at = :n WHERE correlation_id = :c"
                    ),
                    {"w": run_worker_id, "n": datetime.now(UTC), "c": correlation_id},
                )
            claimed = OperationJob.from_row(self._job_row(job_id))
            worker._handle(claimed, {"retry": {"max_attempts": 1}})

        self.assertEqual(self._job_row(job_id)["state"], "failed_terminal")
        self.assertEqual(self._run_row(correlation_id)["status"], "failed")

    def test_expired_lease_cannot_start_or_renew(self):
        _, enqueued = self._accept_and_enqueue(max_attempts=5)
        job_id = str(enqueued.job.id)
        with self.session() as session:
            claim_operation_jobs(session, "worker-a", lease_seconds=1)
        with self.session() as session:
            session.execute(
                text("UPDATE jobs SET lease_expires_at = :expired WHERE id = :id"),
                {"id": job_id, "expired": datetime.now(UTC) - timedelta(seconds=1)},
            )
        with self.session() as session:
            # Fail closed: a delayed worker must not start or renew a lease
            # that already expired, even before another worker reclaims it.
            self.assertFalse(start_operation_job(session, job_id, "worker-a"))
            self.assertFalse(renew_operation_job_lease(session, job_id, "worker-a", 60))
            # A fresh claim by the same worker restores ownership.
            claimed = claim_operation_jobs(session, "worker-a", lease_seconds=60)
        self.assertEqual(len(claimed), 1)
        with self.session() as session:
            self.assertTrue(start_operation_job(session, job_id, "worker-a"))

    def test_terminal_prior_job_allows_same_identity_enqueue(self):
        _, enqueued = self._accept_and_enqueue(max_attempts=1)
        job_id = str(enqueued.job.id)
        identity = {
            "run_kind": "collector",
            "correlation_id": str(uuid4()),
            "dedupe_key": "dedupe-terminal",
            "input_fingerprint": "f" * 64,
            "payload": {"mode": "refresh"},
            "requested_component": "fred",
        }
        with self.session() as session:
            session.execute(
                text(
                    "UPDATE jobs SET state = 'failed_terminal', "
                    "completed_at = :n WHERE id = :id"
                ),
                {"id": job_id, "n": datetime.now(UTC)},
            )
        # A terminal row must not suppress the same logical identity: the
        # partial unique index only guards active states, so an explicit retry
        # or the next schedule window enqueues a fresh job.
        with self.session() as session:
            fresh = enqueue_operation(session, **identity)
        self.assertTrue(fresh.inserted)
        self.assertNotEqual(str(fresh.job.id), job_id)
        with self.session() as session:
            total = session.execute(text("SELECT COUNT(*) FROM jobs")).scalar()
        self.assertEqual(total, 2)

    def test_terminal_finish_failure_rolls_back_poison(self):
        _, enqueued = self._accept_and_enqueue(max_attempts=1, run_kind="collector")
        job_id = str(enqueued.job.id)
        correlation_id = str(enqueued.job.correlation_id)
        fixed = uuid4()
        run_worker_id = f"worker-x:{fixed}"
        with self.session() as session:
            session.execute(
                text(
                    "UPDATE cycle_runs SET status = 'running', worker_id = :w, "
                    "started_at = :n, heartbeat_at = :n WHERE correlation_id = :c"
                ),
                {"w": run_worker_id, "n": datetime.now(UTC), "c": correlation_id},
            )
        worker = OperationWorker(
            {"event_pipeline": {"jobs": {"enabled": True}}},
            worker_id="worker-x",
            session_factory=lambda cfg: self.session(),
        )
        with (
            patch("operation_worker.uuid4", return_value=fixed),
            patch("operation_worker.start_run", return_value=True),
            patch("operation_worker.maintain_run_heartbeat") as heartbeat,
            patch.object(
                worker,
                "_dispatch",
                return_value={"status": "failed", "retryable": False},
            ),
            patch("operation_worker.finish_run_in_session", return_value=False),
        ):
            heartbeat.return_value.__enter__ = Mock(return_value=None)
            heartbeat.return_value.__exit__ = Mock(return_value=False)
            with self.session() as session:
                claim_operation_jobs(session, "worker-x", lease_seconds=120)
            claimed = OperationJob.from_row(self._job_row(job_id))
            worker._handle(claimed, {"retry": {"max_attempts": 1}})

        # Required run finalization failed: BOTH transitions rolled back, so
        # neither a poison job nor a failed run is committed.
        self.assertEqual(self._job_row(job_id)["state"], "running")
        run = self._run_row(correlation_id)
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["worker_id"], run_worker_id)

    def test_failed_or_abandoned_run_poisons_job(self):
        _, enqueued = self._accept_and_enqueue(max_attempts=3, run_kind="collector")
        job_id = str(enqueued.job.id)
        correlation_id = str(enqueued.job.correlation_id)
        worker = OperationWorker(
            {"event_pipeline": {"jobs": {"enabled": True}}},
            worker_id="worker-x",
            session_factory=lambda cfg: self.session(),
        )
        for terminal_status in ("failed", "abandoned"):
            with self.subTest(terminal_status=terminal_status):
                with self.session() as session:
                    session.execute(
                        text(
                            "UPDATE cycle_runs SET status = :s, "
                            "result_status = :s WHERE correlation_id = :c"
                        ),
                        {"s": terminal_status, "c": correlation_id},
                    )
                with (
                    patch("operation_worker.start_run", return_value=False),
                ):
                    with self.session() as session:
                        claim_operation_jobs(session, "worker-x", lease_seconds=120)
                    claimed = OperationJob.from_row(self._job_row(job_id))
                    worker._handle(claimed, {"retry": {"max_attempts": 3}})
                self.assertEqual(self._job_row(job_id)["state"], "failed_terminal")
                # Reset the job for the next subtest.
                with self.session() as session:
                    session.execute(
                        text(
                            "UPDATE jobs SET state = 'queued', "
                            "attempt_count = 0, claimed_by = NULL, "
                            "lease_expires_at = NULL WHERE id = :id"
                        ),
                        {"id": job_id},
                    )

    def test_worker_id_configured_label_is_unique_per_instance(self):
        config = {
            "event_pipeline": {
                "jobs": {"enabled": True, "worker": {"id": "analysis-jobs"}}
            }
        }
        first = OperationWorker(config, session_factory=lambda cfg: self.session())
        second = OperationWorker(config, session_factory=lambda cfg: self.session())
        self.assertTrue(first.start(config))
        self.assertTrue(second.start(config))
        try:
            self.assertNotEqual(first.worker_id, second.worker_id)
            self.assertTrue(first.worker_id.startswith("analysis-jobs:"))
            self.assertTrue(second.worker_id.startswith("analysis-jobs:"))
        finally:
            first.stop()
            second.stop()

    def test_stop_waits_for_inflight_poll_to_reach_job_boundary(self):
        worker = OperationWorker(
            {"event_pipeline": {"jobs": {"enabled": True}}},
            worker_id="operation-test",
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

    def test_poll_claims_exactly_one_job(self):
        worker = OperationWorker(
            {"event_pipeline": {"jobs": {"enabled": True}}},
            worker_id="worker-x",
            session_factory=lambda cfg: self.session(),
        )
        with (
            patch("operation_worker.recover_operation_runs", return_value={"total": 0}),
            patch("operation_worker.claim_operation_jobs", return_value=[]) as claim,
        ):
            worker.poll_once()
        self.assertEqual(claim.call_args.kwargs["limit"], 1)

    def test_db_snapshot_prefers_healthy_instance_over_newer_stopped(self):
        import price_stream

        now = datetime.now(UTC)
        with (
            patch(
                "price_stream.fresh_role_heartbeats",
                return_value=[
                    # Newer stopped sibling (its last write is more recent).
                    {
                        "role": "quotes",
                        "instance_id": "b:1",
                        "status": "stopped",
                        "last_heartbeat_at": now,
                        "detail": {},
                    },
                    # Older but healthy running replica.
                    {
                        "role": "quotes",
                        "instance_id": "a:1",
                        "status": "connected",
                        "last_heartbeat_at": now - timedelta(seconds=2),
                        "detail": {"error": None},
                    },
                ],
            ),
            patch(
                "price_stream.get_session",
                side_effect=lambda cfg: self.session(),
            ),
        ):
            with self.session() as session:
                session.execute(
                    text(
                        "INSERT INTO quote_state (symbol, price, observed_at) "
                        "VALUES ('EURUSD', 1.08, :n)"
                    ),
                    {"n": datetime.now(UTC)},
                )
            snapshot = price_stream.db_snapshot(self.config)

        self.assertEqual(snapshot["stream"]["status"], "connected")
        self.assertEqual(snapshot["stream"]["healthy_instances"], 1)
        self.assertEqual(snapshot["stream"]["instances"], 2)
        self.assertEqual([quote["symbol"] for quote in snapshot["quotes"]], ["EURUSD"])

    def test_worker_poison_job_finalizes_run_as_failed(self):
        _, enqueued = self._accept_and_enqueue(max_attempts=1, run_kind="processor")
        job_id = str(enqueued.job.id)
        correlation_id = str(enqueued.job.correlation_id)
        fixed = uuid4()
        run_worker_id = f"worker-x:{fixed}"
        with self.session() as session:
            session.execute(
                text(
                    "UPDATE cycle_runs SET status = 'running', worker_id = :w, "
                    "started_at = :n, heartbeat_at = :n WHERE correlation_id = :c"
                ),
                {"w": run_worker_id, "n": datetime.now(UTC), "c": correlation_id},
            )
        worker = OperationWorker(
            {"event_pipeline": {"jobs": {"enabled": True}}},
            worker_id="worker-x",
            session_factory=lambda cfg: self.session(),
            random_source=lambda a, b: 0.0,
        )
        claimed = OperationJob.from_row(self._job_row(job_id))
        with (
            patch("operation_worker.uuid4", return_value=fixed),
            patch("operation_worker.start_run", return_value=True),
            patch.object(worker, "_dispatch", side_effect=RuntimeError("boom")),
            patch("operation_worker.maintain_run_heartbeat") as heartbeat,
        ):
            heartbeat.return_value.__enter__ = Mock(return_value=None)
            heartbeat.return_value.__exit__ = Mock(return_value=False)
            worker._handle(claimed, {"retry": {"max_attempts": 1}})

        row = self._job_row(job_id)
        self.assertEqual(row["state"], "failed_terminal")
        self.assertEqual(row["last_error"], "RuntimeError")
        run = self._run_row(correlation_id)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["worker_id"], run_worker_id)

    def test_worker_idempotent_completion_when_run_already_terminal(self):
        _, enqueued = self._accept_and_enqueue(max_attempts=3)
        job_id = str(enqueued.job.id)
        correlation_id = str(enqueued.job.correlation_id)
        with self.session() as session:
            session.execute(
                text(
                    "UPDATE cycle_runs SET status = 'completed', "
                    "result_status = 'success' WHERE correlation_id = :cid"
                ),
                {"cid": correlation_id},
            )
        worker = OperationWorker(
            {"event_pipeline": {"jobs": {"enabled": True}}},
            worker_id="worker-x",
            session_factory=lambda cfg: self.session(),
        )
        claimed = OperationJob.from_row(self._job_row(job_id))
        with patch("operation_worker.start_run", return_value=False):
            worker._handle(claimed, {"retry": {"max_attempts": 3}})
        row = self._job_row(job_id)
        self.assertEqual(row["state"], "succeeded")
        run = self._run_row(correlation_id)
        self.assertEqual(run["status"], "completed")

    # ── scheduler duplicate suppression ───────────────────────────────────

    def test_duplicate_scheduler_window_enqueues_one_job(self):
        first_correlation = str(uuid4())
        second_correlation = str(uuid4())
        first = self._accept_and_enqueue(
            correlation_id=first_correlation,
            run_kind="collector",
            requested_component="fred",
            dedupe_key="collector:fred:2026-08-11T06:00:00",
            input_fingerprint="1786442400",
            payload={"run_dependents": True},
            triggered_by="scheduler",
        )
        second = self._accept_and_enqueue(
            correlation_id=second_correlation,
            run_kind="collector",
            requested_component="fred",
            dedupe_key="collector:fred:2026-08-11T06:00:00",
            input_fingerprint="1786442400",
            payload={"run_dependents": True},
            triggered_by="scheduler",
        )
        self.assertTrue(first[1].inserted)
        self.assertFalse(second[1].inserted)
        self.assertTrue(second[1].suppressed)
        # Only one operation job exists; the duplicate acceptance row was
        # finalized as already_queued instead of left for a worker.
        with self.session() as session:
            total = session.execute(text("SELECT COUNT(*) FROM jobs")).scalar()
        self.assertEqual(total, 1)
        self.assertEqual(self._run_row(first_correlation)["status"], "accepted")
        self.assertEqual(self._run_row(second_correlation)["status"], "completed")
        self.assertEqual(self._run_row(second_correlation)["result_status"], "skipped")

    def test_distinct_windows_both_enqueue(self):
        for fingerprint in ("1", "2"):
            accepted_at, enqueued = self._accept_and_enqueue(
                run_kind="collector",
                requested_component="fred",
                dedupe_key=f"collector:fred:2026-08-11T06:0{fingerprint}:00",
                input_fingerprint=fingerprint,
                triggered_by="scheduler",
            )
            self.assertTrue(enqueued.inserted)
        with self.session() as session:
            total = session.execute(text("SELECT COUNT(*) FROM jobs")).scalar()
        self.assertEqual(total, 2)

    # ── durable status helpers ────────────────────────────────────────────

    def test_latest_cycle_status_reads_durable_rows(self):
        with patch("orchestrator.get_session", side_effect=lambda cfg: self.session()):
            self.assertEqual(
                latest_cycle_status(self.config),
                {"running": False, "correlation_id": None},
            )
        correlation_id = str(uuid4())
        with patch("orchestrator.get_session", side_effect=lambda cfg: self.session()):
            accept_and_enqueue_operation(
                self.config,
                correlation_id=correlation_id,
                triggered_by="api",
                run_kind="cycle",
                requested_component=None,
                dedupe_key=f"cycle:{correlation_id}",
                input_fingerprint=correlation_id,
                payload={"mode": "refresh"},
            )
        with patch("orchestrator.get_session", side_effect=lambda cfg: self.session()):
            status = latest_cycle_status(self.config)
        self.assertTrue(status["running"])
        self.assertEqual(status["correlation_id"], correlation_id)

    def test_operation_queue_summary_counts_states(self):
        self._accept_and_enqueue()
        with self.session() as session:
            session.execute(text("UPDATE jobs SET state = 'running' WHERE 1=1"))
        with patch("orchestrator.get_session", side_effect=lambda cfg: self.session()):
            summary = operation_queue_summary(self.config)
        self.assertEqual(summary["active"], 1)
        self.assertEqual(summary["counts"].get("running"), 1)

    # ── role heartbeats ───────────────────────────────────────────────────

    def test_role_heartbeat_upsert_is_per_instance(self):
        with patch("orchestrator.get_session", side_effect=lambda cfg: self.session()):
            update_role_heartbeat(
                self.config, "worker", "running", {"pid": 1}, instance_id="host:1"
            )
            update_role_heartbeat(
                self.config, "worker", "running", {"pid": 2}, instance_id="host:2"
            )
            # Same instance upserts in place (no duplicate rows).
            update_role_heartbeat(
                self.config, "worker", "stopped", {"pid": 1}, instance_id="host:1"
            )
            rows = list_role_heartbeats(self.config, "worker")
            by_instance = {row["instance_id"]: row for row in rows}
            heartbeat = read_role_heartbeat(self.config, "worker")
        self.assertEqual(len(rows), 2)
        self.assertEqual(by_instance["host:1"]["status"], "stopped")
        self.assertEqual(by_instance["host:1"]["detail"]["pid"], 1)
        self.assertEqual(by_instance["host:2"]["status"], "running")
        self.assertTrue(heartbeat_is_fresh(heartbeat))
        self.assertFalse(heartbeat_is_fresh(None))
        stale = {"last_heartbeat_at": datetime.now(UTC) - timedelta(seconds=600)}
        self.assertFalse(heartbeat_is_fresh(stale))

    def test_two_replica_heartbeats_healthy_with_stopped_sibling(self):
        with patch("orchestrator.get_session", side_effect=lambda cfg: self.session()):
            update_role_heartbeat(
                self.config, "worker", "running", {}, instance_id="host-a:1"
            )
            update_role_heartbeat(
                self.config, "worker", "stopped", {}, instance_id="host-b:1"
            )
            self.assertTrue(
                role_has_fresh_healthy_instance(self.config, "worker", {"running"})
            )

    def test_prune_stale_role_heartbeats_is_bounded(self):
        with patch("orchestrator.get_session", side_effect=lambda cfg: self.session()):
            update_role_heartbeat(
                self.config, "worker", "running", {}, instance_id="host:1"
            )
            update_role_heartbeat(
                self.config, "worker", "running", {}, instance_id="host:2"
            )
            with self.session() as session:
                session.execute(
                    text(
                        "UPDATE role_heartbeats SET last_heartbeat_at = :old "
                        "WHERE instance_id = 'host:1'"
                    ),
                    {"old": datetime.now(UTC) - timedelta(hours=2)},
                )
            pruned = prune_stale_role_heartbeats(
                self.config, role="worker", older_than=timedelta(minutes=30)
            )
            rows = list_role_heartbeats(self.config, "worker")
        self.assertEqual(pruned, 1)
        self.assertEqual([row["instance_id"] for row in rows], ["host:2"])


class PgSupportGuardTests(unittest.TestCase):
    def test_known_production_name_is_rejected(self):
        from pg_support import assert_safe_database

        for name in ("trading_data", "postgres", "template1"):
            with self.subTest(name=name):
                with self.assertRaises(RuntimeError):
                    assert_safe_database(f"postgresql://ci:ci@localhost:5432/{name}")

    def test_non_test_name_is_rejected(self):
        from pg_support import assert_safe_database

        with self.assertRaises(RuntimeError):
            assert_safe_database("postgresql://ci:ci@localhost:5432/analytics")

    def test_loopback_test_db_is_accepted_without_allow_reset(self):
        from pg_support import assert_safe_database

        assert_safe_database(
            "postgresql://ci:ci@localhost:5432/trading_data_test",
            allow_reset=False,
        )

    def test_non_loopback_test_db_requires_allow_reset(self):
        from pg_support import assert_safe_database

        url = "postgresql://ci:ci@db.internal:5432/trading_data_test"
        with self.assertRaises(RuntimeError):
            assert_safe_database(url, allow_reset=False)
        assert_safe_database(url, allow_reset=True)


class RoleCheckCommandTests(unittest.TestCase):
    WORKER_CONFIG = {}

    def test_check_role_fails_for_missing_heartbeat(self):
        import roles

        with (
            patch("config_loader.load_config", return_value=self.WORKER_CONFIG),
            patch("db.check_connection", return_value=True),
            patch("roles.fresh_role_heartbeats", return_value=[]),
        ):
            self.assertEqual(roles.check_role("worker"), 1)

    def test_check_role_succeeds_for_fresh_heartbeat(self):
        import roles

        with (
            patch("config_loader.load_config", return_value=self.WORKER_CONFIG),
            patch("db.check_connection", return_value=True),
            patch(
                "roles.fresh_role_heartbeats",
                return_value=[
                    {
                        "role": "worker",
                        "instance_id": "worker:1",
                        "status": "running",
                        "last_heartbeat_at": datetime.now(UTC),
                        "detail": {},
                    }
                ],
            ),
        ):
            self.assertEqual(roles.check_role("worker"), 0)

    def test_check_role_required_role_rejects_unhealthy_status(self):
        import roles

        config = {"event_pipeline": {"jobs": {"enabled": True}}}
        with (
            patch("config_loader.load_config", return_value=config),
            patch("db.check_connection", return_value=True),
            patch(
                "roles.fresh_role_heartbeats",
                return_value=[
                    {
                        "role": "worker",
                        "instance_id": "host:1",
                        "status": "stopped",
                        "last_heartbeat_at": datetime.now(UTC),
                        "detail": {},
                    }
                ],
            ),
        ):
            self.assertEqual(roles.check_role("worker"), 1)

    def test_heartbeat_tick_logs_bounded_failure_and_keeps_started_at(self):
        import roles

        with (
            patch(
                "roles.update_role_heartbeat",
                side_effect=RuntimeError("secret db failure"),
            ),
            patch("roles.logger") as logger,
        ):
            roles._heartbeat_tick(
                {}, "worker", "running", None, "2026-08-11T00:00:00+00:00"
            )

        logger.warning.assert_called_once()
        self.assertEqual(
            logger.warning.call_args.args[0], "role_heartbeat_write_failed"
        )
        self.assertEqual(logger.warning.call_args.kwargs["error_type"], "RuntimeError")

    def test_heartbeat_tick_preserves_single_started_at(self):
        import roles

        captured = {}

        def record(config, role, status, detail):
            captured.update(detail)

        with patch("roles.update_role_heartbeat", side_effect=record):
            roles._heartbeat_tick(
                {}, "worker", "running", None, "start-t0", "config-v1"
            )
            roles._heartbeat_tick(
                {}, "worker", "running", None, "start-t0", "config-v1"
            )

        self.assertEqual(captured["started_at"], "start-t0")
        self.assertEqual(captured["config_version"], "config-v1")

    def test_worker_running_flag_resets_when_thread_exits(self):
        import threading as threading_mod

        from operation_worker import OperationWorker

        worker = OperationWorker(
            {"event_pipeline": {"jobs": {"enabled": True}}},
            worker_id="worker-x",
        )
        worker._stop.set()
        thread = threading_mod.Thread(target=worker._run, daemon=True)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(worker.state["running"])
        self.assertFalse(worker._running)

    def test_role_heartbeat_future_timestamp_is_not_fresh(self):
        from role_heartbeat import ALLOWED_CLOCK_SKEW, heartbeat_is_fresh

        now = datetime.now(UTC)
        # Far-future heartbeat (clock skew / corruption) must NOT be fresh.
        future = {"last_heartbeat_at": now + timedelta(hours=1)}
        self.assertFalse(heartbeat_is_fresh(future, now=now))
        # Tiny bounded skew stays fresh.
        skewed = {"last_heartbeat_at": now + ALLOWED_CLOCK_SKEW - timedelta(seconds=1)}
        self.assertTrue(heartbeat_is_fresh(skewed, now=now))

    def test_check_role_rejects_future_heartbeat(self):
        import roles

        with (
            patch("config_loader.load_config", return_value=self.WORKER_CONFIG),
            patch("db.check_connection", return_value=True),
            patch(
                "roles.fresh_role_heartbeats",
                return_value=[
                    {
                        "role": "worker",
                        "instance_id": "worker:1",
                        "status": "running",
                        "last_heartbeat_at": datetime.now(UTC) + timedelta(hours=1),
                        "detail": {},
                    }
                ],
            ),
        ):
            self.assertEqual(roles.check_role("worker"), 1)

    def test_heartbeat_defaults_are_coherent(self):
        import roles

        # Timeout must stay > 2x cadence so a single missed write cannot flap.
        self.assertGreater(
            roles.ROLE_HEARTBEAT_TIMEOUT_SECONDS,
            2 * roles.ROLE_HEARTBEAT_INTERVAL_SECONDS,
        )
        self.assertLessEqual(roles.ROLE_HEARTBEAT_INTERVAL_SECONDS, 5.0)

    def test_write_stopped_records_graceful_shutdown(self):
        import roles

        with patch("roles.update_role_heartbeat") as update:
            roles._write_stopped({}, "worker")
        update.assert_called_once()
        self.assertEqual(update.call_args.args[1], "worker")
        self.assertEqual(update.call_args.args[2], "stopped")

    def test_canonical_job_worker_starts_and_stops_both_handlers(self):
        from jobs import run_job_worker_forever

        stop_event = Mock()
        stop_event.is_set.side_effect = [False, True]
        with (
            patch("job_worker.job_worker") as analysis,
            patch("operation_worker.operation_worker") as operation,
            patch("time.sleep"),
        ):
            run_job_worker_forever({}, stop_event=stop_event)

        analysis.start.assert_called_once_with({})
        operation.start.assert_called_once_with({})
        analysis.stop.assert_called_once_with(timeout=15.0)
        operation.stop.assert_called_once_with(timeout=15.0)

    def test_scheduler_leadership_uses_database_advisory_lock(self):
        import scheduler

        connection = Mock()
        connection.execute.return_value.scalar.return_value = True
        engine = Mock()
        engine.connect.return_value = connection
        with patch("db.get_engine", return_value=engine):
            acquired = scheduler._try_acquire_leader_connection({})

        self.assertIs(acquired, connection)
        statement = connection.execute.call_args.args[0]
        self.assertIn("pg_try_advisory_lock", str(statement))

    def test_config_version_exit_hook_triggers_graceful_stop(self):
        import roles

        calls = {"versions": []}

        def fake_load_config():
            calls["versions"].append("reload")
            return {}

        with (
            patch("config_loader.load_config", side_effect=fake_load_config),
            patch("config_loader.config_version", return_value="v2"),
        ):
            hook = roles._config_version_exit_hook("v1")
            self.assertTrue(hook())
        self.assertEqual(calls["versions"], ["reload"])
        # A rejected reload keeps the old snapshot serving.
        with (
            patch(
                "config_loader.load_config", side_effect=RuntimeError("reload rejected")
            ),
            patch("config_loader.config_version", return_value="v2"),
        ):
            hook = roles._config_version_exit_hook("v1")
            self.assertFalse(hook())
        from config_loader import ConfigError

        # Credential-boundary failures are raised by ConfigStore and require a
        # safe-boundary restart instead of indefinite use of startup secrets.
        with patch(
            "config_loader.load_config",
            side_effect=ConfigError("managed secrets invalid"),
        ):
            hook = roles._config_version_exit_hook("v1")
            self.assertTrue(hook())

    def test_check_role_rejects_unknown_role(self):
        import roles

        self.assertEqual(roles.check_role("nope"), 2)


if __name__ == "__main__":
    unittest.main()
