"""Polling worker for durable analysis jobs."""

from __future__ import annotations

import random
import threading
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from analysis_jobs import (
    AnalysisJob,
    claim_jobs,
    reconcile_jobs,
    renew_job_lease,
    retry_job,
    start_job,
    succeed_job,
    terminal_fail_job,
)
from db import get_session


class AnalysisJobWorker:
    """Bounded polling worker with caller-owned handler transactions."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        worker_id: str | None = None,
        session_factory: Callable[..., Any] | None = None,
        random_source: Callable[[float, float], float] | None = None,
    ) -> None:
        self.config = config
        self.worker_id = worker_id or f"analysis:{uuid4()}"
        self._session_factory = session_factory or get_session
        self._random = random_source or random.uniform
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()
        self._last_poll_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._running = False

    @staticmethod
    def _settings(config: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(config, Mapping) or not isinstance(
            config.get("event_pipeline"), Mapping
        ):
            return {}
        settings = config["event_pipeline"].get("jobs")
        return settings if isinstance(settings, Mapping) else {}

    def _cfg(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._settings(config if config is not None else self.config)

    def _worker_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        worker = settings.get("worker")
        return worker if isinstance(worker, Mapping) else {}

    @staticmethod
    def _batch_size(settings: dict[str, Any], worker: dict[str, Any]) -> int:
        try:
            configured = int(worker.get("batch_size", settings.get("batch_size", 1)))
        except (TypeError, ValueError, OverflowError):
            configured = 1
        return max(1, min(configured, 25))

    def enabled(self, config: dict[str, Any] | None = None) -> bool:
        settings = self._cfg(config)
        return bool(settings.get("enabled", False)) and bool(
            self._worker_settings(settings).get("enabled", True)
        )

    def start(self, config: dict[str, Any] | None = None) -> bool:
        if config is not None:
            self.config = config
        if not self.enabled():
            return False
        settings = self._cfg()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            configured_id = self._worker_settings(settings).get("id") or settings.get(
                "worker_id"
            )
            if configured_id:
                # A configured id is a shared LABEL, not an identity: append a
                # process-unique instance uuid so replicas never share
                # claimed_by (a stale replica must never renew or finalize a
                # job another replica reclaimed).
                self.worker_id = f"{configured_id}:{uuid4()}"
            self._stop.clear()
            self._running = True
            self._thread = threading.Thread(
                target=self._run, name=self.worker_id, daemon=True
            )
            self._thread.start()
        return True

    def request_stop(self) -> None:
        """Stop claiming after the current poll/job reaches its boundary."""
        self._stop.set()

    def wait_stopped(self, timeout: float | None = None) -> bool:
        """Wait for an in-flight job to finalize; None means a full drain."""
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            join_timeout = None if timeout is None else max(0.0, float(timeout))
            thread.join(join_timeout)
        stopped = thread is None or not thread.is_alive()
        with self._lock:
            if stopped:
                self._thread = None
                self._running = False
        return stopped

    def stop(self, timeout: float | None = None) -> bool:
        self.request_stop()
        return self.wait_stopped(timeout)

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.poll_once()
                except Exception:
                    self._increment("poll_errors")
                settings = self._cfg()
                worker = self._worker_settings(settings)
                try:
                    interval = max(
                        0.05,
                        float(worker.get("poll_seconds", 1.0)),
                    )
                except (TypeError, ValueError):
                    interval = 1.0
                self._stop.wait(interval)
        finally:
            with self._lock:
                self._running = False

    def _increment(self, key: str, value: int = 1) -> None:
        with self._lock:
            self._counts[key] += value
            if key == "succeeded":
                self._last_success_at = datetime.now(UTC)

    @property
    def state(self) -> dict[str, Any]:
        with self._lock:
            counts, running = dict(self._counts), self._running
            poll_at, success_at = self._last_poll_at, self._last_success_at
        return {
            "running": running,
            "disabled": not self.enabled(),
            "worker_id": self.worker_id,
            "last_poll_at": poll_at.isoformat() if poll_at is not None else None,
            "last_success_at": (
                success_at.isoformat() if success_at is not None else None
            ),
            "claimed": counts.get("claimed", 0),
            "completed": counts.get("succeeded", 0),
            "retried": counts.get("retried", 0),
            "failed": counts.get("failed", 0),
            "poll_errors": counts.get("poll_errors", 0),
            "handler_errors": counts.get("handler_errors", 0),
            "reconciled": counts.get("reconciled", 0),
        }

    def state_counters(self) -> dict[str, int]:
        with self._lock:
            counts = dict(self._counts)
        return {
            "claimed": counts.get("claimed", 0),
            "succeeded": counts.get("succeeded", 0),
            "retried": counts.get("retried", 0),
            "failed": counts.get("failed", 0),
            "suppressed": counts.get("suppressed", 0),
            "poll_errors": counts.get("poll_errors", 0),
            "handler_errors": counts.get("handler_errors", 0),
            "reconciled": counts.get("reconciled", 0),
        }

    @property
    def counters(self) -> dict[str, int]:
        return self.state_counters()

    def _session(self):
        try:
            return self._session_factory(self.config)
        except TypeError:
            return self._session_factory()

    @staticmethod
    def _heartbeat_interval(lease_seconds: float) -> float:
        return max(0.2, min(30.0, lease_seconds / 3.0))

    def _heartbeat(
        self, job_id: Any, lease_seconds: float, stop: threading.Event
    ) -> None:
        interval = self._heartbeat_interval(lease_seconds)
        while not stop.wait(interval):
            try:
                with self._session() as session:
                    if not renew_job_lease(
                        session, job_id, self.worker_id, lease_seconds
                    ):
                        return
            except Exception:
                self._increment("poll_errors")

    @staticmethod
    def _result_ref(value: Any) -> dict[str, Any] | None:
        allowed = (
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
        )
        if isinstance(value, dict):
            candidates = value
        else:
            candidates = {
                key: getattr(value, key) for key in allowed if hasattr(value, key)
            }
        result = {
            key: candidates[key]
            for key in allowed
            if key in candidates
            and isinstance(candidates[key], (str, int, float, bool, type(None)))
        }
        return result or None

    def _handle(self, job: AnalysisJob, settings: dict[str, Any]) -> None:
        import analysis_job_handlers

        lease_seconds = max(
            1.0,
            float(
                settings.get(
                    "lease_seconds",
                    self._worker_settings(settings).get("lease_seconds", 120),
                )
            ),
        )
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
            retry_cfg = (
                settings.get("retry")
                if isinstance(settings.get("retry"), Mapping)
                else {}
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
                base = max(0.0, float(retry_cfg.get("base_seconds", 1.0)))
                cap = max(
                    base,
                    float(retry_cfg.get("max_seconds", 300.0)),
                )
                jitter = max(0.0, float(retry_cfg.get("jitter_seconds", 0.25)))
                delay = min(cap, base * (2 ** max(0, attempts - 1))) + self._random(
                    0.0, jitter
                )
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
                query = (
                    settings.get("query")
                    if isinstance(settings.get("query"), Mapping)
                    else {}
                )
                repaired = reconcile_jobs(
                    session,
                    max(
                        1,
                        min(
                            int(
                                settings.get(
                                    "reconcile_limit",
                                    query.get("max_reconcile_jobs", 100),
                                )
                            ),
                            1000,
                        ),
                    ),
                )
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
