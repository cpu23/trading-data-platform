"""Polling worker for durable analysis jobs."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from jobs import (
    AnalysisJob,
    claim_jobs,
    reconcile_jobs,
    renew_job_lease,
    retry_job,
    start_job,
    succeed_job,
    terminal_fail_job,
)
from polling_worker import BasePollingWorker, filter_result_ref


class AnalysisJobWorker(BasePollingWorker):
    """Bounded polling worker with caller-owned handler transactions."""

    _default_id_prefix = "analysis"
    _enabled_key = "enabled"
    _default_poll_seconds = 1.0
    _poll_error_interval = 1.0

    _ALLOWED_RESULT_KEYS = (
        "snapshot_id",
        "section_key",
        "scope_key",
        "version",
        "changed",
        "status",
        "id",
        "case_id",
        "case_count",
        "driver_count",
        "lifecycle_transition_count",
        "error_count",
        "cost_usd",
        "promoted_count",
        "falsification_runs",
    )

    def _heartbeat(
        self, job_id: Any, lease_seconds: float, stop: threading.Event
    ) -> None:
        self._run_heartbeat(job_id, lease_seconds, stop, renew_job_lease)

    @classmethod
    def _result_ref(cls, value: Any) -> dict[str, Any] | None:
        return filter_result_ref(value, cls._ALLOWED_RESULT_KEYS)

    def _handle(self, job: AnalysisJob, settings: dict[str, Any]) -> None:
        import analysis_job_handlers

        lease_seconds = self._lease_seconds(settings)
        with self._session() as session:
            if not start_job(session, job.id, self.worker_id):
                return
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(job.id, lease_seconds, heartbeat_stop),
            daemon=True,
        )
        heartbeat.start()
        try:
            with self._session() as session:
                handler_result = analysis_job_handlers.route_job(session, job)
                if not succeed_job(
                    session,
                    job.id,
                    self.worker_id,
                    result_ref=self._result_ref(handler_result),
                ):
                    raise RuntimeError("job lease ownership lost")
            self._increment("succeeded")
        except Exception as exc:
            self._increment("handler_errors")
            attempts = max(1, int(job.attempt_count))
            retry_raw = settings.get("retry")
            retry_cfg: Mapping[str, Any] = (
                retry_raw if isinstance(retry_raw, Mapping) else {}
            )
            max_attempts = max(1, int(retry_cfg.get("max_attempts", job.max_attempts)))
            if attempts >= max_attempts or attempts >= job.max_attempts:
                try:
                    with self._session() as session:
                        if terminal_fail_job(session, job.id, self.worker_id, exc):
                            self._increment("failed")
                except Exception:
                    self._increment("poll_errors")
            else:
                delay = self._compute_retry_delay(attempts, retry_cfg)
                try:
                    with self._session() as session:
                        if retry_job(
                            session,
                            job.id,
                            self.worker_id,
                            datetime.now(UTC) + timedelta(seconds=delay),
                            exc,
                        ):
                            self._increment("retried")
                except Exception:
                    self._increment("poll_errors")
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=self._heartbeat_interval(lease_seconds) + 1.0)

    def poll_once(self) -> dict[str, int]:
        if not self.enabled():
            return self.state_counters()
        settings = self._cfg()
        worker = self._worker_settings(settings)
        try:
            with self._session() as session:
                query_raw = settings.get("query")
                query: Mapping[str, Any] = (
                    query_raw if isinstance(query_raw, Mapping) else {}
                )
                reconcile_limit = max(
                    1,
                    min(
                        int(
                            settings.get("reconcile_limit")
                            or query.get("max_reconcile_jobs")
                            or 100
                        ),
                        1000,
                    ),
                )
                repaired = reconcile_jobs(session, reconcile_limit)
                if repaired:
                    self._increment("reconciled", repaired)
                lease = float(
                    settings.get("lease_seconds", worker.get("lease_seconds", 120))
                )
                # Sequential worker: claim exactly ONE job per poll so no
                # unhandled in-memory claim can expire and be reclaimed by
                # another replica while a long first handler runs.
                jobs = claim_jobs(
                    session,
                    self.worker_id,
                    limit=1,
                    lease_seconds=max(1.0, lease),
                    job_types=settings.get("job_types")
                    if isinstance(settings.get("job_types"), (list, tuple))
                    else None,
                )
            self._increment("claimed", len(jobs))
        except Exception:
            self._increment("poll_errors")
            return self.state_counters()
        for job in jobs:
            try:
                self._handle(job, settings)
            except Exception:
                self._increment("handler_errors")
        with self._lock:
            self._last_poll_at = datetime.now(UTC)
        return self.state_counters()


job_worker = AnalysisJobWorker()
__all__ = ["AnalysisJobWorker", "job_worker"]
