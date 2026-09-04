"""Real-PostgreSQL integration tests for the durable operation lifecycle.

Env-gated by ``TEST_DATABASE_URL`` (see ``pg_support``): skipped locally when
unset, run in CI against a disposable, self-provisioned Timescale/Postgres
database.  Covers the semantics sqlite/mocks cannot prove: transactional
accept+enqueue, SKIP LOCKED concurrent claims, lease expiry/reclaim, crash
recovery after acceptance, duplicate-scheduler prevention, and atomic
run+job finalization rollback.
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
    retry_operation_job,
    start_operation_job,
    succeed_operation_job,
)
from operation_worker import OperationWorker
from pg_support import (
    parse_config,
    provision,
    require_postgres,
    truncate,
)
from run_lifecycle import RunAcceptanceConflict
from sqlalchemy import text


class OperationJobsPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = require_postgres()
        cls.config = parse_config(cls.url)
        provision(cls.config)
        from db import get_engine, get_session

        cls.engine = get_engine(cls.config)
        cls.get_session = staticmethod(get_session)

    def setUp(self):
        truncate(self.config)

    @contextmanager
    def session(self):
        with self.get_session(self.config) as session:
            yield session

    def _enqueue(self, session, **kwargs):
        defaults = {
            "run_kind": "collector",
            "requested_component": "fred",
            "correlation_id": str(uuid4()),
            "dedupe_key": str(uuid4()),
            "input_fingerprint": "f" * 64,
            "payload": {"mode": "refresh"},
        }
        defaults.update(kwargs)
        session.execute(
            text(
                "INSERT INTO cycle_runs "
                "(correlation_id, status, accepted_at, triggered_by, run_kind, "
                "requested_component, summary) "
                "VALUES (:cid, 'accepted', :accepted_at, 'integration_test', "
                ":run_kind, :component, '{}'::jsonb) "
                "ON CONFLICT (correlation_id) DO NOTHING"
            ),
            {
                "cid": defaults["correlation_id"],
                "accepted_at": datetime.now(UTC),
                "run_kind": defaults["run_kind"],
                "component": defaults["requested_component"],
            },
        )
        return enqueue_operation(session, **defaults)

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

    # ── transactional acceptance + enqueue ───────────────────────────────

    def test_accept_and_enqueue_commit_together(self):
        correlation_id = str(uuid4())
        accepted_at, enqueued = accept_and_enqueue_operation(
            self.config,
            correlation_id=correlation_id,
            triggered_by="api",
            run_kind="collector",
            requested_component="fred",
            dedupe_key="accept-1",
            input_fingerprint="f" * 64,
            payload={"mode": "refresh"},
        )
        self.assertTrue(enqueued.inserted)
        run = self._run_row(correlation_id)
        self.assertEqual(run["status"], "accepted")
        self.assertEqual(run["run_kind"], "collector")
        job = self._job_row(str(enqueued.job.id))
        self.assertEqual(job["state"], "queued")

    def test_enqueue_failure_rolls_back_acceptance(self):
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
        with self.session() as session:
            count = session.execute(text("SELECT COUNT(*) FROM cycle_runs")).scalar()
        self.assertEqual(count, 0)

    def test_duplicate_correlation_is_conflict_without_job(self):
        correlation_id = str(uuid4())
        accept_and_enqueue_operation(
            self.config,
            correlation_id=correlation_id,
            triggered_by="api",
            run_kind="collector",
            requested_component="fred",
            dedupe_key="dup-1",
            input_fingerprint="f" * 64,
            payload={},
        )
        with self.assertRaises(RunAcceptanceConflict):
            accept_and_enqueue_operation(
                self.config,
                correlation_id=correlation_id,
                triggered_by="api",
                run_kind="collector",
                requested_component="fred",
                dedupe_key="dup-2",
                input_fingerprint="f" * 64,
                payload={},
            )
        with self.session() as session:
            jobs = session.execute(text("SELECT COUNT(*) FROM jobs")).scalar()
        self.assertEqual(jobs, 1)

    def test_budget_override_placeholder_row_is_adopted(self):
        correlation_id = str(uuid4())
        with self.session() as session:
            session.execute(
                text(
                    "INSERT INTO cycle_runs (correlation_id, status, accepted_at, "
                    "started_at, triggered_by, run_kind, requested_component, summary) "
                    "VALUES (:cid, 'running', :now, :now, 'api_manual_override', "
                    "'processor', 'briefing', :summary)"
                ),
                {
                    "cid": correlation_id,
                    "now": datetime.now(UTC),
                    "summary": '{"budget_override": {"requested": true}}',
                },
            )
        _, enqueued = accept_and_enqueue_operation(
            self.config,
            correlation_id=correlation_id,
            triggered_by="api",
            run_kind="processor",
            requested_component="briefing",
            dedupe_key="adopt-1",
            input_fingerprint="f" * 64,
            payload={"mode": "refresh"},
            request_summary={"mode": "refresh"},
        )
        self.assertTrue(enqueued.inserted)
        run = self._run_row(correlation_id)
        self.assertEqual(run["status"], "accepted")
        self.assertIn("budget_override", run["summary"])
        self.assertIn("mode", run["summary"])

    def test_idempotency_key_lost_response_replay_returns_original(self):
        key = "idem-pg-1"
        first_cid = str(uuid4())
        accepted_at, enqueued = accept_and_enqueue_operation(
            self.config,
            correlation_id=first_cid,
            triggered_by="api",
            run_kind="collector",
            requested_component="fred",
            idempotency_key=key,
            payload={"mode": "refresh"},
        )
        self.assertTrue(enqueued.inserted)
        # Lost-202 retry: same key + same request identity, NEW correlation.
        replay_cid = str(uuid4())
        accepted_at2, enqueued2 = accept_and_enqueue_operation(
            self.config,
            correlation_id=replay_cid,
            triggered_by="api",
            run_kind="collector",
            requested_component="fred",
            idempotency_key=key,
            payload={"mode": "refresh"},
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
            accept_and_enqueue_operation(
                self.config,
                correlation_id=str(uuid4()),
                triggered_by="api",
                run_kind="processor",
                requested_component="briefing",
                idempotency_key=key,
                payload={},
            )
        with self.session() as session:
            runs = session.execute(text("SELECT COUNT(*) FROM cycle_runs")).scalar()
            jobs = session.execute(text("SELECT COUNT(*) FROM jobs")).scalar()
        self.assertEqual((runs, jobs), (1, 1))

    def test_terminal_prior_job_allows_same_identity_enqueue(self):
        with self.session() as session:
            first = self._enqueue(
                session,
                dedupe_key="dedupe-terminal-pg",
                max_attempts=1,
            )
            session.execute(
                text(
                    "UPDATE jobs SET state = 'failed_terminal', "
                    "completed_at = :n WHERE id = :id"
                ),
                {"id": str(first.job.id), "n": datetime.now(UTC)},
            )
        # A terminal row must not suppress the same logical identity: the
        # partial unique index only guards active states.
        with self.session() as session:
            fresh = self._enqueue(session, dedupe_key="dedupe-terminal-pg")
        self.assertTrue(fresh.inserted)
        self.assertNotEqual(str(fresh.job.id), str(first.job.id))
        with self.session() as session:
            total = session.execute(text("SELECT COUNT(*) FROM jobs")).scalar()
        self.assertEqual(total, 2)

    # ── SKIP LOCKED concurrent claims ────────────────────────────────────

    def test_concurrent_claims_are_exclusive(self):
        job_ids = []
        with self.session() as session:
            for index in range(6):
                result = self._enqueue(
                    session, dedupe_key=f"conc-{index}", max_attempts=5
                )
                job_ids.append(str(result.job.id))
            for job_id in job_ids:
                session.execute(
                    text("UPDATE jobs SET state = 'queued' WHERE id = :id"),
                    {"id": job_id},
                )

        barrier = threading.Barrier(3)
        results: dict[str, list[str]] = {"worker-a": [], "worker-b": []}
        errors: list[Exception] = []

        def claim(worker_id: str) -> None:
            try:
                barrier.wait(timeout=10)
                with self.session() as session:
                    claimed = claim_operation_jobs(
                        session, worker_id, limit=10, lease_seconds=60
                    )
                results[worker_id] = [str(job.id) for job in claimed]
            except Exception as exc:  # pragma: no cover - failure diagnostics
                errors.append(exc)

        threads = [
            threading.Thread(target=claim, args=("worker-a",)),
            threading.Thread(target=claim, args=("worker-b",)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=10)
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        claimed_a, claimed_b = set(results["worker-a"]), set(results["worker-b"])
        self.assertEqual(len(claimed_a) + len(claimed_b), len(job_ids))
        self.assertTrue(claimed_a.isdisjoint(claimed_b))
        with self.session() as session:
            owners = session.execute(
                text(
                    "SELECT claimed_by, COUNT(*) FROM jobs "
                    "WHERE state = 'leased' GROUP BY claimed_by"
                )
            ).all()
        self.assertEqual(sum(count for _, count in owners), len(job_ids))

    # ── lease expiry / reclaim / owner guards ─────────────────────────────

    def test_lease_expiry_reclaim_and_owner_guard(self):
        with self.session() as session:
            result = self._enqueue(session, max_attempts=5)
        job_id = str(result.job.id)
        with self.session() as session:
            claimed = claim_operation_jobs(session, "worker-a", lease_seconds=60)
        self.assertEqual(len(claimed), 1)
        with self.session() as session:
            session.execute(
                text("UPDATE jobs SET lease_expires_at = :expired WHERE id = :id"),
                {"id": job_id, "expired": datetime.now(UTC) - timedelta(seconds=1)},
            )
            reclaimed = claim_operation_jobs(session, "worker-b", lease_seconds=60)
        self.assertEqual(len(reclaimed), 1)
        self.assertEqual(reclaimed[0].claimed_by, "worker-b")
        self.assertEqual(reclaimed[0].attempt_count, 2)
        with self.session() as session:
            self.assertFalse(start_operation_job(session, job_id, "worker-a"))
            self.assertTrue(start_operation_job(session, job_id, "worker-b"))
            self.assertFalse(succeed_operation_job(session, job_id, "worker-a"))
            self.assertTrue(succeed_operation_job(session, job_id, "worker-b"))

    def test_retry_transition_reclaims_run_row(self):
        with self.session() as session:
            result = self._enqueue(session, max_attempts=5)
        job_id = str(result.job.id)
        correlation_id = str(result.job.correlation_id)
        with self.session() as session:
            claim_operation_jobs(session, "worker-a", lease_seconds=60)
            self.assertTrue(start_operation_job(session, job_id, "worker-a"))
            session.execute(
                text(
                    "UPDATE cycle_runs SET status = 'running', "
                    "worker_id = 'worker-a:r1', started_at = :n, "
                    "heartbeat_at = :n WHERE correlation_id = :c"
                ),
                {"n": datetime.now(UTC), "c": correlation_id},
            )
        with self.session() as session:
            self.assertTrue(
                retry_operation_job(
                    session,
                    job_id,
                    "worker-a",
                    datetime.now(UTC) + timedelta(seconds=1),
                    RuntimeError("boom"),
                )
            )
        run = self._run_row(correlation_id)
        self.assertEqual(run["status"], "accepted")
        self.assertIsNone(run["worker_id"])

    # ── crash recovery after acceptance ───────────────────────────────────

    def test_crash_after_acceptance_recovery(self):
        from recovery import recover_operation_runs

        now = datetime.now(UTC)
        old = now - timedelta(minutes=30)
        active_job = str(uuid4())
        orphan_run = str(uuid4())
        stale_run = str(uuid4())
        with self.session() as session:
            session.execute(
                text(
                    "INSERT INTO cycle_runs (correlation_id, status, accepted_at, "
                    "started_at, heartbeat_at, triggered_by, run_kind) "
                    "VALUES (:active, 'accepted', :old, NULL, NULL, 'api', 'cycle'), "
                    "(:orphan, 'accepted', :old, NULL, NULL, 'api', 'cycle'), "
                    "(:stale, 'running', :old, :old, :old, 'api', 'cycle')"
                ),
                {
                    "active": active_job,
                    "orphan": orphan_run,
                    "stale": stale_run,
                    "old": old,
                },
            )
            enqueue_operation(
                session,
                run_kind="cycle",
                correlation_id=active_job,
                dedupe_key="crash-active",
                input_fingerprint="f" * 64,
                payload={"mode": "refresh"},
            )
        result = recover_operation_runs(self.config)
        self.assertEqual(result["reclaimed_ids"], [])
        # The accepted row WITH an active job survives untouched (claimable);
        # the orphan acceptance and the stale running row are abandoned.
        self.assertEqual([str(value) for value in result["accepted_ids"]], [orphan_run])
        self.assertEqual([str(value) for value in result["running_ids"]], [stale_run])
        self.assertEqual(self._run_row(active_job)["status"], "accepted")
        self.assertEqual(self._run_row(orphan_run)["status"], "abandoned")
        self.assertEqual(self._run_row(stale_run)["status"], "abandoned")

    def test_claim_reclaims_stale_running_row_after_crash(self):
        with self.session() as session:
            result = self._enqueue(session, max_attempts=5)
        correlation_id = str(result.job.correlation_id)
        with self.session() as session:
            session.execute(
                text(
                    "UPDATE cycle_runs SET status = 'running', "
                    "worker_id = 'dead', started_at = :n, heartbeat_at = :old "
                    "WHERE correlation_id = :c"
                ),
                {
                    "c": correlation_id,
                    "n": datetime.now(UTC),
                    "old": datetime.now(UTC) - timedelta(minutes=30),
                },
            )
        with self.session() as session:
            claimed = claim_operation_jobs(session, "worker-c", lease_seconds=60)
        self.assertEqual(len(claimed), 1)
        run = self._run_row(correlation_id)
        self.assertEqual(run["status"], "accepted")
        self.assertIsNone(run["worker_id"])

    # ── duplicate scheduler prevention ────────────────────────────────────

    def test_duplicate_scheduler_window_enqueues_one_job(self):
        first_correlation = str(uuid4())
        second_correlation = str(uuid4())
        first = accept_and_enqueue_operation(
            self.config,
            correlation_id=first_correlation,
            triggered_by="scheduler",
            run_kind="collector",
            requested_component="fred",
            dedupe_key="collector:fred:2026-08-11T06:00:00",
            input_fingerprint="1786442400",
            payload={"run_dependents": True},
        )
        second = accept_and_enqueue_operation(
            self.config,
            correlation_id=second_correlation,
            triggered_by="scheduler",
            run_kind="collector",
            requested_component="fred",
            dedupe_key="collector:fred:2026-08-11T06:00:00",
            input_fingerprint="1786442400",
            payload={"run_dependents": True},
        )
        self.assertTrue(first[1].inserted)
        self.assertFalse(second[1].inserted)
        self.assertTrue(second[1].suppressed)
        with self.session() as session:
            total = session.execute(text("SELECT COUNT(*) FROM jobs")).scalar()
        self.assertEqual(total, 1)
        self.assertEqual(self._run_row(first_correlation)["status"], "accepted")
        second_run = self._run_row(second_correlation)
        self.assertEqual(second_run["status"], "completed")
        self.assertEqual(second_run["result_status"], "skipped")

    def test_distinct_windows_both_enqueue(self):
        for index in range(2):
            _, enqueued = accept_and_enqueue_operation(
                self.config,
                correlation_id=str(uuid4()),
                triggered_by="scheduler",
                run_kind="collector",
                requested_component="fred",
                dedupe_key=f"collector:fred:win:{index}",
                input_fingerprint=str(index),
                payload={"run_dependents": True},
            )
            self.assertTrue(enqueued.inserted)
        with self.session() as session:
            total = session.execute(text("SELECT COUNT(*) FROM jobs")).scalar()
        self.assertEqual(total, 2)

    # ── atomic run + job finalization rollback ────────────────────────────

    def test_success_finalize_is_atomic_rollback_on_lease_lost(self):
        with self.session() as session:
            result = self._enqueue(session, max_attempts=3)
        job_id = str(result.job.id)
        correlation_id = str(result.job.correlation_id)
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
            self.config,
            worker_id="worker-x",
            session_factory=lambda cfg: self.session(),
        )
        with self.session() as session:
            claim_operation_jobs(session, "worker-x", lease_seconds=60)
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

        run = self._run_row(correlation_id)
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["worker_id"], run_worker_id)
        self.assertEqual(self._job_row(job_id)["state"], "running")


if __name__ == "__main__":
    unittest.main()
