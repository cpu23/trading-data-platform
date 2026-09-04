"""Three-service operational topology backed by bounded local database reads."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from api_db import query_one

from contracts import SystemTopologyEdge, SystemTopologyNode, SystemTopologyResponse

_WORKER_STALE_AFTER = timedelta(seconds=30)


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def unavailable_system_topology(
    component: str = "system_topology",
) -> SystemTopologyResponse:
    return SystemTopologyResponse(
        generated_at=datetime.now(UTC),
        status="unavailable",
        nodes=[],
        edges=[],
        unavailable_components=[component],
        summary="Operational topology is unavailable.",
    )


def build_system_topology() -> SystemTopologyResponse:
    now = datetime.now(UTC)
    unavailable: list[str] = []
    active_jobs: int | None = None
    last_job_at: datetime | None = None
    try:
        jobs = (
            query_one(
                """SELECT COUNT(*) FILTER (
                     WHERE state IN ('queued','leased','running','failed_retryable')
                   ) AS active_jobs,
                      MAX(created_at) AS last_job_at
                 FROM jobs"""
            )
            or {}
        )
        active_jobs = max(0, int(jobs.get("active_jobs") or 0))
        last_job_at = _time(jobs.get("last_job_at"))
    except Exception:
        unavailable.append("postgres")

    worker_status = "unavailable"
    worker_state = "heartbeat unavailable"
    worker_at: datetime | None = None
    try:
        heartbeat = query_one(
            """SELECT status, last_heartbeat_at
                 FROM role_heartbeats
                WHERE role = 'worker'
                ORDER BY last_heartbeat_at DESC
                LIMIT 1"""
        )
        if heartbeat:
            worker_at = _time(heartbeat.get("last_heartbeat_at"))
            reported = str(heartbeat.get("status") or "unknown")
            if worker_at is not None and now - worker_at <= _WORKER_STALE_AFTER:
                worker_status = "healthy" if reported == "running" else "degraded"
                worker_state = reported
            else:
                worker_status = "stale"
                worker_state = "heartbeat stale"
        else:
            worker_status = "idle"
            worker_state = "not started"
    except Exception:
        unavailable.append("worker")

    postgres_status = "unavailable" if "postgres" in unavailable else "healthy"
    nodes = [
        SystemTopologyNode(
            id="web",
            label="Web",
            group="Runtime",
            kind="service",
            status="healthy",
            activity_state="serving",
            safe_detail="FastAPI application and polling UI.",
            navigation_target="/operations",
        ),
        SystemTopologyNode(
            id="worker",
            label="Worker",
            group="Runtime",
            kind="service",
            status=worker_status,
            activity_state=worker_state,
            bounded_count=active_jobs,
            last_activity_at=worker_at or last_job_at,
            staleness_reason="worker heartbeat is stale"
            if worker_status == "stale"
            else None,
            safe_detail="Scheduler, durable jobs, outbox, and quote ingestion.",
            navigation_target="/operations",
        ),
        SystemTopologyNode(
            id="postgres",
            label="PostgreSQL",
            group="Storage",
            kind="database",
            status=postgres_status,
            activity_state="available"
            if postgres_status == "healthy"
            else "unavailable",
            bounded_count=active_jobs,
            last_activity_at=last_job_at,
            staleness_reason="database query failed"
            if postgres_status == "unavailable"
            else None,
            safe_detail="Authoritative application state and durable job queue.",
        ),
    ]
    edges = [
        SystemTopologyEdge(
            source="web",
            target="postgres",
            kind="reads-writes",
            status=postgres_status,
            recent_activity_count=active_jobs,
            last_activity_at=last_job_at,
            safe_detail="Web reads state and enqueues durable work.",
        ),
        SystemTopologyEdge(
            source="worker",
            target="postgres",
            kind="claims-publishes",
            status=worker_status,
            recent_activity_count=active_jobs,
            last_activity_at=worker_at or last_job_at,
            safe_detail="Worker claims jobs and publishes results transactionally.",
        ),
    ]
    status = "available" if not unavailable else "partial"
    return SystemTopologyResponse(
        generated_at=now,
        status=status,
        nodes=nodes,
        edges=edges,
        unavailable_components=unavailable,
        summary="Web, worker, and PostgreSQL operational state.",
    )


__all__ = ["build_system_topology", "unavailable_system_topology"]
