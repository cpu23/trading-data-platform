"""Recovery and reconciliation for runs orphaned by process restarts."""

from datetime import UTC, datetime, timedelta
from typing import Any

from errors import PersistenceError
from logging_config import get_logger
from run_lifecycle import DEFAULT_ACCEPTED_TIMEOUT, DEFAULT_HEARTBEAT_TIMEOUT
from sqlalchemy import text

logger = get_logger("orchestrator.recovery")


_ACTIVE_JOB_STATES = "('queued', 'leased', 'running', 'failed_retryable')"


def _active_job_exists_sql() -> str:
    """A correlation has active durable work in the canonical job queue."""
    return (
        "EXISTS (SELECT 1 FROM jobs "
        "WHERE jobs.correlation_id = cycle_runs.correlation_id "
        f"AND jobs.state IN {_ACTIVE_JOB_STATES})"
    )


def _fresh_job_lease_exists_sql() -> str:
    """A worker currently owns durable work for this correlation."""
    return (
        "EXISTS (SELECT 1 FROM jobs AS protected_jobs "
        "WHERE protected_jobs.correlation_id = cycle_runs.correlation_id "
        "AND protected_jobs.state IN ('leased', 'running') "
        "AND protected_jobs.lease_expires_at > :now)"
    )


def recover_operation_runs(
    session: Any,
    now: datetime | None = None,
    accepted_timeout: timedelta = DEFAULT_ACCEPTED_TIMEOUT,
    heartbeat_timeout: timedelta = DEFAULT_HEARTBEAT_TIMEOUT,
) -> dict[str, list[Any] | int]:
    """Recover orphaned operation runs in the caller's transaction.

    Accepted and running rows are abandoned only when no active job references
    their correlation. Stale running rows with active work are reclaimed unless
    another worker still holds a fresh job lease for that correlation.
    """
    completed_at = now or datetime.now(UTC)
    active = _active_job_exists_sql()
    fresh_lease = _fresh_job_lease_exists_sql()
    accepted_reason = "abandoned by worker recovery: acceptance timeout exceeded"
    running_reason = "abandoned by worker recovery: heartbeat timeout exceeded"
    try:
        reclaimed = session.execute(
            text(
                "UPDATE cycle_runs SET status = 'accepted', worker_id = NULL, "
                "started_at = NULL "
                "WHERE status = 'running' "
                "AND COALESCE(heartbeat_at, started_at) < :cutoff "
                "AND (" + active + ") "
                "AND NOT (" + fresh_lease + ") RETURNING correlation_id"
            ),
            {
                "cutoff": completed_at - heartbeat_timeout,
                "now": completed_at,
            },
        )
        reclaimed_ids = list(reclaimed.scalars().all())
        accepted_result = session.execute(
            text(
                "UPDATE cycle_runs SET status = :abandoned, result_status = :abandoned, "
                "completed_at = :completed_at, error_message = :reason "
                "WHERE status = 'accepted' AND accepted_at < :cutoff "
                "AND NOT (" + active + ") "
                "RETURNING correlation_id"
            ),
            {
                "abandoned": "abandoned",
                "completed_at": completed_at,
                "reason": accepted_reason,
                "cutoff": completed_at - accepted_timeout,
            },
        )
        accepted_ids = list(accepted_result.scalars().all())
        running_result = session.execute(
            text(
                "UPDATE cycle_runs SET status = :abandoned, result_status = :abandoned, "
                "completed_at = :completed_at, error_message = :reason "
                "WHERE status = 'running' "
                "AND COALESCE(heartbeat_at, started_at) < :cutoff "
                "AND NOT (" + active + ") "
                "RETURNING correlation_id"
            ),
            {
                "abandoned": "abandoned",
                "completed_at": completed_at,
                "reason": running_reason,
                "cutoff": completed_at - heartbeat_timeout,
            },
        )
        running_ids = list(running_result.scalars().all())
    except Exception as exc:
        logger.error(
            "operation_run_recovery_failed",
            action="recover_operation_runs",
            error=str(exc),
        )
        raise PersistenceError("operation run recovery failed") from exc

    return {
        "reclaimed_ids": reclaimed_ids,
        "accepted_ids": accepted_ids,
        "running_ids": running_ids,
        "total": len(reclaimed_ids) + len(accepted_ids) + len(running_ids),
    }


__all__ = ["recover_operation_runs"]
