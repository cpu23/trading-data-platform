"""Polling worker for durable operation jobs.

The operation worker claims ``operation_jobs`` rows (leased), claims the
matching accepted ``cycle_runs`` row, executes the run with the durable
cycle_runs heartbeat while renewing the job lease, then finalizes both
atomically.  Lease expiry/reclaim, retry/backoff, and poison terminal state
follow the ``analysis_jobs`` worker contract.
"""

from __future__ import annotations

import random
import threading
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from cycle_planning import VALID_CYCLE_MODES, aggregate_stage_statuses
from db import get_session
from operation_jobs import (
    OperationJob,
    claim_operation_jobs,
    reconcile_operation_jobs,
    renew_operation_job_lease,
    retry_operation_job,
    start_operation_job,
    succeed_operation_job,
    terminal_fail_operation_job,
)
from orchestrator import (
    maintain_run_heartbeat,
    run_collector,
    run_full_cycle,
    run_news_source,
    run_processor,
    start_run,
)
from recovery import recover_operation_runs
from run_lifecycle import finish_run_in_session


class LeaseLostError(RuntimeError):
    """Raised when a worker no longer owns the operation job or its run row."""


def _factory_accepts_config(factory: Callable[..., Any]) -> bool:
    """Determine once whether the session factory takes a config argument."""
    try:
        import inspect

        parameters = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        return True
    return any(
        parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY)
        for parameter in parameters.values()
    ) or any(
        parameter.kind == inspect.Parameter.VAR_POSITIONAL
        for parameter in parameters.values()
    )


class OperationWorker:
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
        self.worker_id = worker_id or f"operation:{uuid4()}"
        self._session_factory = session_factory or get_session
        self._session_takes_config = _factory_accepts_config(self._session_factory)
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

    @staticmethod
    def _worker_settings(settings: dict[str, Any]) -> dict[str, Any]:
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
        worker = self._worker_settings(settings)
        if not bool(settings.get("enabled", False)):
            return False
        return bool(worker.get("operation_enabled", True))

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
                    interval = 2.0
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
        if self._session_takes_config:
            return self._session_factory(self.config)
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
                    if not renew_operation_job_lease(
                        session, job_id, self.worker_id, lease_seconds
                    ):
                        return
            except Exception:
                self._increment("poll_errors")

    @staticmethod
    def _result_ref(value: Any) -> dict[str, Any] | None:
        allowed = (
            "status",
            "mode",
            "records_fetched",
            "records_written",
            "duration_ms",
            "ingested",
            "skipped",
            "failed",
            "inserted",
            "job_id",
            "stage_count",
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

    def _dispatch(self, job: OperationJob, correlation_id: str) -> dict[str, Any]:
        """Execute one claimed operation job; the run owns no lifecycle."""
        from orchestrator import _authorize_claimed_run_budget

        config = self.config
        run_kind = job.run_kind
        component = job.requested_component
        payload = dict(job.payload or {})
        if run_kind == "cycle":
            mode = payload.get("mode", "refresh")
            if mode not in VALID_CYCLE_MODES:
                mode = "refresh"
            budget_context = _authorize_claimed_run_budget(
                config, correlation_id, run_kind="cycle", component=None
            )
            result = run_full_cycle(
                config=config,
                correlation_id=correlation_id,
                manage_lifecycle=False,
                mode=mode,
                budget_context=budget_context,
            )
        elif run_kind == "collector":
            if component is None:
                raise ValueError("collector operation requires a component")
            result = run_collector(
                component,
                config=config,
                correlation_id=correlation_id,
                manage_lifecycle=False,
            )
            if payload.get("run_dependents") is True:
                result = self._run_collector_with_dependents(
                    component, config, correlation_id, result
                )
        elif run_kind == "processor":
            if component is None:
                raise ValueError("processor operation requires a component")
            budget_context = _authorize_claimed_run_budget(
                config, correlation_id, run_kind="processor", component=component
            )
            result = run_processor(
                component,
                config=config,
                correlation_id=correlation_id,
                manage_lifecycle=False,
                budget_context=budget_context,
            )
        elif run_kind == "news":
            if component is None:
                raise ValueError("news operation requires a component")
            result = run_news_source(
                component, correlation_id, config, manage_lifecycle=False
            )
        elif run_kind == "filings":
            from investment_filings import run_filing_collection

            result = run_filing_collection(
                config,
                correlation_id=correlation_id,
                auto_analyze=payload.get("auto_analyze", False),
            )
        else:
            raise ValueError(f"unsupported operation run kind: {run_kind}")
        if result is None:
            raise RuntimeError("run returned no result after ownership was claimed")
        return result

    @staticmethod
    def _run_collector_with_dependents(
        source_id: str,
        config: dict[str, Any],
        correlation_id: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Run a scheduled collector and its after_dependency processors."""
        stages = {source_id: result}
        if result.get("status") in ("success", "partial"):
            from processors import get_processor

            for processor_id, processor_config in config.get("processors", {}).items():
                if not processor_config.get("enabled", False):
                    continue
                if processor_config.get("schedule") != "after_dependency":
                    continue
                if source_id in get_processor(processor_id).get_depends_on():
                    stages[processor_id] = run_processor(
                        processor_id,
                        config=config,
                        correlation_id=correlation_id,
                        manage_lifecycle=False,
                    )
        return {
            "stages": stages,
            "status": aggregate_stage_statuses(
                item["status"] for item in stages.values()
            ),
        }

    def _cycle_run_status(self, correlation_id: str) -> str | None:
        try:
            with self._session() as session:
                row = (
                    session.execute(
                        text(
                            "SELECT status FROM cycle_runs WHERE correlation_id = :cid"
                        ),
                        {"cid": correlation_id},
                    )
                    .mappings()
                    .first()
                )
            return str(row["status"]) if row is not None else None
        except Exception:
            return None

    def _terminal_fail(
        self,
        job: OperationJob,
        correlation_id: str,
        run_worker_id: str | None,
        summary: dict[str, Any],
        error: Any,
    ) -> None:
        """Poison the job and fail the run row in ONE transaction.

        The job's ``last_error`` is sanitized to the error TYPE (never
        untrusted exception text); the run row keeps the caller's message so
        dashboards can show why the run failed.

        When the run row was claimed, its finalization is REQUIRED: a failure
        to finalize raises so the whole terminal transition (job poison +
        run fail) rolls back together instead of committing a poison job over
        an owned, still-'running' row.
        """
        error_message = error if isinstance(error, str) else type(error).__name__
        try:
            with self._session() as session:
                if terminal_fail_operation_job(session, job.id, self.worker_id, error):
                    if run_worker_id:
                        finalized = finish_run_in_session(
                            session,
                            correlation_id,
                            "failed",
                            summary,
                            error_message=error_message,
                            worker_id=run_worker_id,
                        )
                        if not finalized:
                            raise LeaseLostError(
                                "run finalization lost ownership during terminal failure"
                            )
                    self._increment("failed")
        except Exception:
            self._increment("poll_errors")

    def _retry_or_poison(
        self,
        job: OperationJob,
        correlation_id: str,
        run_worker_id: str | None,
        error: Any,
        settings: dict[str, Any],
        summary: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        """Retry with backoff or poison at attempt exhaustion.

        ``retry_operation_job`` releases the run row (back to ``accepted``)
        in the same transaction, so the next attempt claims it immediately.
        """
        attempts = max(1, int(job.attempt_count))
        retry_cfg = (
            settings.get("retry") if isinstance(settings.get("retry"), Mapping) else {}
        )
        max_attempts = max(1, int(retry_cfg.get("max_attempts", job.max_attempts)))
        if attempts >= max_attempts or attempts >= job.max_attempts:
            self._terminal_fail(
                job,
                correlation_id,
                run_worker_id,
                summary or {"status": "failed", "reason": type(error).__name__},
                error,
            )
            return
        base = max(0.0, float(retry_cfg.get("base_seconds", 1.0)))
        cap = max(base, float(retry_cfg.get("max_seconds", 300.0)))
        jitter = max(0.0, float(retry_cfg.get("jitter_seconds", 0.25)))
        delay = min(cap, base * (2 ** max(0, attempts - 1))) + self._random(0.0, jitter)
        try:
            with self._session() as session:
                if retry_operation_job(
                    session,
                    job.id,
                    self.worker_id,
                    datetime.now(UTC) + timedelta(seconds=delay),
                    error,
                ):
                    self._increment("retried")
        except Exception:
            self._increment("poll_errors")

    def _handle(self, job: OperationJob, settings: dict[str, Any]) -> None:
        lease_seconds = max(
            1.0,
            float(
                settings.get(
                    "lease_seconds",
                    self._worker_settings(settings).get("lease_seconds", 120),
                )
            ),
        )
        correlation_id = str(job.correlation_id)
        run_worker_id = f"{self.worker_id}:{uuid4()}"
        with self._session() as session:
            if not start_operation_job(session, job.id, self.worker_id):
                return
        job_heartbeat_stop = threading.Event()
        job_heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(job.id, lease_seconds, job_heartbeat_stop),
            daemon=True,
        )
        job_heartbeat.start()
        run_claimed = False
        try:
            try:
                run_claimed = start_run(self.config, correlation_id, run_worker_id)
            except Exception:
                run_claimed = False
            if not run_claimed:
                # Idempotent completion: only this worker executes a
                # correlation's single operation job, so a terminal run row
                # means a prior attempt already finalized the outcome.  The
                # run row is the source of truth: a completed run completes
                # the job; a failed or abandoned run poisons it.
                row_status = self._cycle_run_status(correlation_id)
                if row_status == "completed":
                    with self._session() as session:
                        if succeed_operation_job(
                            session,
                            job.id,
                            self.worker_id,
                            result_ref={
                                "status": "skipped",
                                "reason": "run already finalized",
                            },
                        ):
                            self._increment("succeeded")
                    return
                if row_status in ("failed", "abandoned"):
                    with self._session() as session:
                        if terminal_fail_operation_job(
                            session,
                            job.id,
                            self.worker_id,
                            f"run {row_status} before execution",
                        ):
                            self._increment("failed")
                    return
                raise LeaseLostError("cycle_runs ownership could not be claimed")
            with maintain_run_heartbeat(self.config, correlation_id, run_worker_id):
                result = self._dispatch(job, correlation_id)
            if result.get("status") == "failed":
                # Implementations return failures instead of raising.  Respect
                # their retryable flag exactly like a raised exception:
                # retryable failures go through backoff (or poison at attempt
                # exhaustion); non-retryable failures poison immediately.
                self._increment("handler_errors")
                if result.get("retryable") is True:
                    self._retry_or_poison(
                        job,
                        correlation_id,
                        run_worker_id,
                        result.get("error") or "retryable run failure",
                        settings,
                        summary=result,
                        error_message=str(
                            result.get("error") or "retryable run failure"
                        ),
                    )
                    return
                self._terminal_fail(
                    job,
                    correlation_id,
                    run_worker_id,
                    result,
                    str(result.get("error") or "run failed"),
                )
                return
            # Finalize the run row and the operation job in ONE caller-owned
            # transaction: a crash or lease loss between the two would leave
            # an inconsistent pair, so either both persist or neither does.
            with self._session() as session:
                finalized = finish_run_in_session(
                    session,
                    correlation_id,
                    result["status"],
                    result,
                    error_message=result.get("error"),
                    worker_id=run_worker_id,
                )
                if not finalized:
                    raise LeaseLostError("run finalization lost ownership")
                if not succeed_operation_job(
                    session,
                    job.id,
                    self.worker_id,
                    result_ref=self._result_ref(result),
                ):
                    raise LeaseLostError("operation job lease ownership lost")
            self._increment("succeeded")
        except Exception as exc:
            self._increment("handler_errors")
            self._retry_or_poison(job, correlation_id, run_worker_id, exc, settings)
        finally:
            job_heartbeat_stop.set()
            job_heartbeat.join(timeout=self._heartbeat_interval(lease_seconds) + 1.0)

    def poll_once(self) -> dict[str, int]:
        if not self.enabled():
            return self.state_counters()
        settings = self._cfg()
        worker = self._worker_settings(settings)
        try:
            recovery_result = recover_operation_runs(self.config)
            recovered = recovery_result.get("total", 0)
            if recovered:
                self._increment("reconciled", recovered)
            with self._session() as session:
                query = (
                    settings.get("query")
                    if isinstance(settings.get("query"), Mapping)
                    else {}
                )
                repaired = reconcile_operation_jobs(
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
                # Sequential worker: claim exactly ONE job per poll.  Anything
                # more would sit in memory without a heartbeat while the first
                # job runs, letting its lease expire and another replica
                # reclaim it -> duplicate execution.
                jobs = claim_operation_jobs(
                    session,
                    self.worker_id,
                    limit=1,
                    lease_seconds=max(1.0, lease),
                    run_kinds=settings.get("run_kinds")
                    if isinstance(settings.get("run_kinds"), (list, tuple))
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


operation_worker = OperationWorker()
__all__ = ["LeaseLostError", "OperationWorker", "operation_worker"]
