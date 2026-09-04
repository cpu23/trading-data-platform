"""Polling executor for durable operation jobs.

The executor claims leased rows from ``jobs``, claims the matching accepted
``cycle_runs`` row, executes the run while renewing both heartbeats, and
finalizes both atomically. Lease expiry, retry/backoff, and poison terminal
state use the same queue contract as every other job type.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from cycle_planning import VALID_CYCLE_MODES, aggregate_stage_statuses
from jobs import (
    OperationJob,
    claim_operation_jobs,
    reconcile_operation_jobs,
    renew_operation_job_lease,
    retry_operation_job,
    start_operation_job,
    succeed_operation_job,
    terminal_fail_operation_job,
)
from polling_worker import BasePollingWorker, filter_result_ref
from recovery import recover_operation_runs
from run_lifecycle import finish_run_in_session
from sqlalchemy import text

from orchestrator import (
    maintain_run_heartbeat,
    run_collector,
    run_full_cycle,
    run_news_source,
    run_processor,
    start_run,
)


class LeaseLostError(RuntimeError):
    """Raised when a worker no longer owns the operation job or its run row."""


class OperationWorker(BasePollingWorker):
    """Bounded polling worker with caller-owned handler transactions."""

    _default_id_prefix = "operation"
    _enabled_key = "operation_enabled"
    _default_poll_seconds = 1.0
    _poll_error_interval = 2.0

    _ALLOWED_RESULT_KEYS = (
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

    def _heartbeat(
        self, job_id: Any, lease_seconds: float, stop: threading.Event
    ) -> None:
        self._run_heartbeat(job_id, lease_seconds, stop, renew_operation_job_lease)

    @classmethod
    def _result_ref(cls, value: Any) -> dict[str, Any] | None:
        return filter_result_ref(value, cls._ALLOWED_RESULT_KEYS)

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
        delay = self._compute_retry_delay(attempts, retry_cfg)
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
        lease_seconds = self._lease_seconds(settings)
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
