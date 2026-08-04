"""Recovery and reconciliation for runs orphaned by process restarts."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from errors import PersistenceError
from logging_config import get_logger
from run_lifecycle import DEFAULT_ACCEPTED_TIMEOUT, DEFAULT_HEARTBEAT_TIMEOUT

logger = get_logger("orchestrator.recovery")


def _get_session(config: dict):
    import orchestrator

    return orchestrator.get_session(config)


def reconcile_abandoned_runs(
    config: dict,
    now: datetime | None = None,
    accepted_timeout: timedelta = DEFAULT_ACCEPTED_TIMEOUT,
    heartbeat_timeout: timedelta = DEFAULT_HEARTBEAT_TIMEOUT,
) -> dict:
    """Mark jobs that could not have survived a process restart as abandoned."""
    completed_at = now or datetime.now(UTC)
    accepted_reason = "abandoned by restart reconciliation: acceptance timeout exceeded"
    running_reason = "abandoned by restart reconciliation: heartbeat timeout exceeded"
    try:
        with _get_session(config) as session:
            accepted_result = session.execute(
                text(
                    "UPDATE cycle_runs SET status = :abandoned, result_status = :abandoned, "
                    "completed_at = :completed_at, error_message = :reason "
                    "WHERE status = 'accepted' AND accepted_at < :cutoff "
                    "RETURNING correlation_id"
                ),
                {
                    "abandoned": "abandoned",
                    "completed_at": completed_at,
                    "reason": accepted_reason,
                    "cutoff": completed_at - accepted_timeout,
                },
            )
            running_result = session.execute(
                text(
                    "UPDATE cycle_runs SET status = :abandoned, result_status = :abandoned, "
                    "completed_at = :completed_at, error_message = :reason "
                    "WHERE status = 'running' "
                    "AND COALESCE(heartbeat_at, started_at) < :cutoff "
                    "RETURNING correlation_id"
                ),
                {
                    "abandoned": "abandoned",
                    "completed_at": completed_at,
                    "reason": running_reason,
                    "cutoff": completed_at - heartbeat_timeout,
                },
            )
            accepted_ids = list(accepted_result.scalars().all())
            running_ids = list(running_result.scalars().all())
    except Exception as exc:
        logger.error(
            "abandoned_run_reconciliation_failed",
            action="reconcile_abandoned_runs",
            error=str(exc),
        )
        raise PersistenceError("abandoned run reconciliation failed") from exc

    return {
        "accepted_ids": accepted_ids,
        "running_ids": running_ids,
        "total": len(accepted_ids) + len(running_ids),
    }


__all__ = ["reconcile_abandoned_runs"]
