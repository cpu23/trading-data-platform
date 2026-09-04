"""Durable run ownership: acceptance, claims, heartbeats, progress, finalization.

The cycle_runs row is the single source of truth for who owns a run and what
state it is in. Everything here is conditional on ownership so a worker that
lost its row can never overwrite a newer owner's state.
"""

import json
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from errors import classify_error
from logging_config import get_logger
from sqlalchemy import text

logger = get_logger("orchestrator.run_lifecycle")


def _facade_logger():
    """Use the facade logger when available so legacy patch targets remain effective."""
    import orchestrator

    return getattr(orchestrator, "logger", logger)


DEFAULT_ACCEPTED_TIMEOUT = timedelta(minutes=15)
DEFAULT_HEARTBEAT_TIMEOUT = timedelta(minutes=5)
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30.0


class RunAcceptanceConflict(RuntimeError):
    """A correlation or idempotency key already owns a durable run."""


class RunStartConflict(RuntimeError):
    """A synchronously invoked run could not claim its accepted row."""


def _get_session(config: dict):
    import orchestrator

    return orchestrator.get_session(config)


def accept_run(
    config: dict,
    correlation_id: str,
    triggered_by: str,
    run_kind: str,
    requested_component: str | None = None,
    idempotency_key: str | None = None,
    request_summary: dict | None = None,
) -> datetime:
    """Persist durable acceptance, raising a typed conflict on unique-key races."""
    from sqlalchemy.exc import IntegrityError

    accepted_at = datetime.now(UTC)
    try:
        with _get_session(config) as session:
            session.execute(
                text(
                    "INSERT INTO cycle_runs "
                    "(correlation_id, status, accepted_at, triggered_by, run_kind, "
                    "requested_component, idempotency_key, summary) "
                    "VALUES (:cid, 'accepted', :accepted_at, :triggered_by, :run_kind, "
                    ":component, :idempotency_key, CAST(:summary AS JSONB))"
                ),
                {
                    "cid": correlation_id,
                    "accepted_at": accepted_at,
                    "triggered_by": triggered_by,
                    "run_kind": run_kind,
                    "component": requested_component,
                    "idempotency_key": idempotency_key,
                    "summary": json.dumps(request_summary) if request_summary else None,
                },
            )
    except IntegrityError as exc:
        _facade_logger().info(
            "run_acceptance_conflict",
            action="accept_run",
            correlation_id=correlation_id,
            run_kind=run_kind,
        )
        raise RunAcceptanceConflict(
            "run correlation or idempotency key already exists"
        ) from exc
    return accepted_at


def get_run_for_retry(config: dict, correlation_id: str) -> dict | None:
    """Return the immutable dispatch metadata needed for an explicit retry."""
    with _get_session(config) as session:
        row = session.execute(
            text(
                "SELECT correlation_id, status, run_kind, requested_component, triggered_by, summary "
                "FROM cycle_runs WHERE correlation_id = :cid"
            ),
            {"cid": correlation_id},
        ).fetchone()
    return dict(row._mapping) if row is not None else None


def start_run(config: dict, correlation_id: str, worker_id: str) -> bool:
    now = datetime.now(UTC)
    with _get_session(config) as session:
        result = session.execute(
            text(
                "UPDATE cycle_runs SET status = 'running', started_at = :started_at, "
                "heartbeat_at = :heartbeat_at, worker_id = :worker_id "
                "WHERE correlation_id = :cid AND status = 'accepted'"
            ),
            {
                "cid": correlation_id,
                "started_at": now,
                "heartbeat_at": now,
                "worker_id": worker_id,
            },
        )
        return result.rowcount == 1


def heartbeat_run(
    config: dict, correlation_id: str, worker_id: str | None = None
) -> bool:
    owner_clause = " AND worker_id = :worker_id" if worker_id is not None else ""
    with _get_session(config) as session:
        result = session.execute(
            text(
                "UPDATE cycle_runs SET heartbeat_at = :heartbeat_at "
                "WHERE correlation_id = :cid AND status = 'running'" + owner_clause
            ),
            {
                "cid": correlation_id,
                "heartbeat_at": datetime.now(UTC),
                "worker_id": worker_id,
            },
        )
        return result.rowcount == 1


def _heartbeat_interval_seconds(config: dict) -> float:
    """Fixed run-heartbeat cadence.

    There is no ``jobs`` configuration section; the heartbeat interval is a
    process constant.  (Durable run heartbeats must outlive lease-based job
    polling, so they are intentionally not derived from the event-pipeline
    worker lease.)
    """
    return DEFAULT_HEARTBEAT_INTERVAL_SECONDS


@contextmanager
def maintain_run_heartbeat(
    config: dict,
    correlation_id: str,
    worker_id: str,
    *,
    event_factory=threading.Event,
    thread_factory=threading.Thread,
):
    """Maintain an owned running row without sharing caller DB sessions."""
    stop_event = event_factory()
    interval = _heartbeat_interval_seconds(config)

    import orchestrator

    def heartbeat_loop() -> None:
        while not stop_event.wait(interval):
            try:
                owned = orchestrator.heartbeat_run(config, correlation_id, worker_id)
            except Exception as exc:
                policy = classify_error(exc)
                _facade_logger().error(
                    "run_heartbeat_failed",
                    action="heartbeat_run",
                    correlation_id=correlation_id,
                    worker_id=worker_id,
                    error_class=policy.error_class,
                    retryable=policy.retryable,
                )
                continue
            if not owned:
                _facade_logger().warning(
                    "run_heartbeat_lost_ownership",
                    action="heartbeat_run",
                    correlation_id=correlation_id,
                    worker_id=worker_id,
                )
                return

    thread = thread_factory(
        target=heartbeat_loop,
        name=f"run-heartbeat-{correlation_id}",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stop_event.set()
        thread.join()


def ensure_run(
    correlation_id: str,
    config: dict,
    run_kind: str = "cycle",
    requested_component: str | None = None,
    triggered_by: str = "internal",
) -> None:
    """Compatibility helper for callers that need an immediate accepted→running run."""
    accept_run(
        config,
        correlation_id,
        triggered_by,
        run_kind,
        requested_component=requested_component,
    )
    if not start_run(config, correlation_id, f"sync:{uuid4()}"):
        raise RunStartConflict("accepted run could not be started")


def _jsonb_expr(session, column: str = "summary") -> str:
    try:
        dialect = session.get_bind().dialect.name
    except Exception:
        dialect = "postgresql"
    return f"CAST(:{column} AS JSONB)" if dialect != "sqlite" else f":{column}"


def finish_run_in_session(
    session,
    correlation_id: str,
    result_status: str,
    summary: dict,
    error_message: str | None = None,
    worker_id: str | None = None,
) -> bool:
    """Finalize a run row in the caller's transaction (no commit)."""
    lifecycle_status = "failed" if result_status == "failed" else "completed"
    allowed_from = (
        "('running', 'accepted')" if lifecycle_status == "failed" else "('running')"
    )
    owner_clause = " AND worker_id = :worker_id" if worker_id is not None else ""
    result = session.execute(
        text(
            "UPDATE cycle_runs SET status = :status, result_status = :result_status, "
            "summary = " + _jsonb_expr(session) + ", completed_at = :completed_at, "
            "heartbeat_at = :completed_at, error_message = :error_message "
            f"WHERE correlation_id = :cid AND status IN {allowed_from}" + owner_clause
        ),
        {
            "cid": correlation_id,
            "status": lifecycle_status,
            "result_status": result_status,
            "summary": json.dumps(summary),
            "completed_at": datetime.now(UTC),
            "error_message": error_message,
            "worker_id": worker_id,
        },
    )
    return result.rowcount == 1


def finish_run(
    correlation_id: str,
    result_status: str,
    summary: dict,
    config: dict,
    error_message: str | None = None,
    worker_id: str | None = None,
) -> bool:
    lifecycle_status = "failed" if result_status == "failed" else "completed"
    allowed_from = (
        "('running', 'accepted')" if lifecycle_status == "failed" else "('running')"
    )
    owner_clause = " AND worker_id = :worker_id" if worker_id is not None else ""
    with _get_session(config) as session:
        result = session.execute(
            text(
                "UPDATE cycle_runs SET status = :status, result_status = :result_status, "
                "summary = CAST(:summary AS JSONB), completed_at = :completed_at, "
                "heartbeat_at = :completed_at, error_message = :error_message "
                f"WHERE correlation_id = :cid AND status IN {allowed_from}"
                + owner_clause
            ),
            {
                "cid": correlation_id,
                "status": lifecycle_status,
                "result_status": result_status,
                "summary": json.dumps(summary),
                "completed_at": datetime.now(UTC),
                "error_message": error_message,
                "worker_id": worker_id,
            },
        )
        return result.rowcount == 1


def finalize_run_safely(
    correlation_id: str,
    result_status: str,
    summary: dict,
    config: dict,
    error_message: str | None = None,
    *,
    worker_id: str | None = None,
    run_kind: str,
    component: str | None = None,
) -> bool:
    """Finalize durably without masking the work outcome or exception."""
    import orchestrator

    try:
        finalized = orchestrator.finish_run(
            correlation_id,
            result_status,
            summary,
            config,
            error_message,
            worker_id,
        )
    except Exception as exc:
        policy = classify_error(exc)
        _facade_logger().error(
            "run_finalization_failed",
            action="finish_run",
            correlation_id=correlation_id,
            run_kind=run_kind,
            component=component,
            worker_id=worker_id,
            error_class=policy.error_class,
            retryable=policy.retryable,
        )
        return False
    if not finalized:
        _facade_logger().warning(
            "run_finalization_lost_ownership",
            action="finish_run",
            correlation_id=correlation_id,
            run_kind=run_kind,
            component=component,
            worker_id=worker_id,
        )
        return False
    return True


def update_run_progress(
    correlation_id: str,
    progress: dict,
    config: dict,
    worker_id: str | None = None,
) -> bool:
    owner_clause = " AND worker_id = :worker_id" if worker_id is not None else ""
    try:
        with _get_session(config) as session:
            result = session.execute(
                text(
                    "UPDATE cycle_runs SET summary = "
                    "COALESCE(summary, CAST('{}' AS JSONB)) || CAST(:summary AS JSONB), "
                    "heartbeat_at = :heartbeat_at "
                    "WHERE correlation_id = :cid AND status = 'running'" + owner_clause
                ),
                {
                    "cid": correlation_id,
                    "summary": json.dumps({"progress": progress}),
                    "heartbeat_at": datetime.now(UTC),
                    "worker_id": worker_id,
                },
            )
            return result.rowcount == 1
    except Exception as exc:
        policy = classify_error(exc)
        _facade_logger().error(
            "cycle_progress_write_failed",
            action="update_run_progress",
            correlation_id=correlation_id,
            error=str(exc),
            error_class=policy.error_class,
            retryable=policy.retryable,
        )
        return False
