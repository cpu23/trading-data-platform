"""Durable operator controls for research discovery and case updates."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from jobs import enqueue_job
from sqlalchemy import text

from contracts.runtime_config import AppConfig
from db import get_session
from orchestrator import accept_run, finalize_run_safely, start_run
from research_intelligence.config import ResearchSettings
from research_intelligence.contracts import canonical_fingerprint

_JOB_TYPES = frozenset({"research_discovery", "research_case_update"})


def _case_id(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("invalid research case id") from None


def enqueue_research_job(
    config: AppConfig,
    *,
    job_type: str,
    case_id: str | None = None,
    force: bool = False,
    triggered_by: str = "manual",
    request_nonce: str | None = None,
) -> dict[str, Any]:
    """Accept a durable run and enqueue one bounded research job."""
    if job_type not in _JOB_TYPES:
        raise ValueError("unsupported research job type")
    settings = ResearchSettings.from_config(config.research_intelligence)
    if not settings.enabled:
        raise ValueError("research intelligence is disabled")
    parsed_case = _case_id(case_id)
    if job_type == "research_case_update" and parsed_case is None:
        raise ValueError("research case update requires a case id")
    correlation_id = str(uuid4())
    component = parsed_case or "discovery"
    accepted_at = accept_run(
        config,
        correlation_id,
        triggered_by,
        "research",
        component,
        request_summary={
            "job_type": job_type,
            "case_id": parsed_case,
            "force": bool(force),
        },
    )
    worker_id = f"research-enqueue:{uuid4()}"
    try:
        started = start_run(config, correlation_id, worker_id)
    except Exception:
        finalize_run_safely(
            correlation_id,
            "failed",
            {"status": "failed", "reason": "research run start unavailable"},
            config,
            "research run start unavailable",
            run_kind="research",
            component=component,
        )
        raise
    if not started:
        raise RuntimeError("accepted research run could not be claimed")
    try:
        identity = {
            "job_type": job_type,
            "case_id": parsed_case,
            "config": {
                "rolling_window_days": settings.rolling_window_days,
                "hot_market_universe": settings.hot_market_universe,
                "region_universe": settings.region_universe,
                "stage_enabled": dict(settings.stage_enabled),
            },
            "request_date": datetime.now(UTC).date().isoformat(),
            # Normal refreshes coalesce. Explicit rebuilds and retries receive a
            # unique identity so a completed prior job cannot suppress new work.
            "request_nonce": request_nonce or (correlation_id if force else None),
        }
        input_fingerprint = canonical_fingerprint(identity)
        dedupe_key = (
            f"research-case:{parsed_case}"
            if parsed_case is not None
            else "research-discovery:global"
        )
        payload = {"force": bool(force)}
        if parsed_case is not None:
            payload["case_id"] = parsed_case
        with get_session(config) as session:
            enqueued = enqueue_job(
                session,
                job_type=job_type,
                dedupe_key=dedupe_key,
                input_fingerprint=input_fingerprint,
                payload=payload,
                correlation_id=correlation_id,
                priority=90 if force else 80,
                max_attempts=3,
            )
        job = enqueued.job
        result = {
            "status": "queued" if enqueued.inserted else "already_queued",
            "job_id": str(job.id) if job is not None else None,
            "correlation_id": (
                str(job.correlation_id) if job is not None else correlation_id
            ),
            "accepted_at": accepted_at.isoformat(),
            "inserted": enqueued.inserted,
            "force": bool(force),
        }
        finalize_run_safely(
            correlation_id,
            "success",
            result,
            config,
            None,
            worker_id=worker_id,
            run_kind="research",
            component=component,
        )
        return result
    except Exception:
        finalize_run_safely(
            correlation_id,
            "failed",
            {},
            config,
            "research enqueue failed",
            worker_id=worker_id,
            run_kind="research",
            component=component,
        )
        raise


def retry_research_job(config: AppConfig, job_id: str) -> dict[str, Any]:
    try:
        parsed_job = str(UUID(str(job_id)))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("invalid research job id") from None
    with get_session(config) as session:
        result = session.execute(
            text(
                """
                SELECT id, job_type, state, payload, correlation_id
                FROM jobs WHERE id = :job_id LIMIT 1
                """
            ),
            {"job_id": parsed_job},
        )
        row = result.mappings().first()
        job = dict(row) if row is not None else None
        if job is None or job.get("job_type") not in _JOB_TYPES:
            raise ValueError("research job not found")
        if job.get("state") == "failed_retryable":
            session.execute(
                text("UPDATE jobs SET not_before = NOW() WHERE id = :job_id"),
                {"job_id": parsed_job},
            )
            return {
                "status": "retry_queued",
                "job_id": parsed_job,
                "correlation_id": str(job["correlation_id"]),
            }
        if job.get("state") in {"queued", "leased", "running"}:
            return {
                "status": "already_queued",
                "job_id": parsed_job,
                "correlation_id": str(job["correlation_id"]),
            }
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    return enqueue_research_job(
        config,
        job_type=str(job["job_type"]),
        case_id=payload.get("case_id"),
        force=bool(payload.get("force", False)),
        triggered_by="retry",
        request_nonce=f"retry:{parsed_job}:{uuid4()}",
    )


__all__ = ["enqueue_research_job", "retry_research_job"]
