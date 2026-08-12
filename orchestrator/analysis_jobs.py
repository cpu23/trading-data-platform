"""Durable analysis-job repository with caller-owned transactions."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

ACTIVE_STATES = ("queued", "leased", "running", "failed_retryable")
TERMINAL_STATES = (
    "succeeded",
    "failed_terminal",
    "suppressed_duplicate",
    "suppressed_immaterial",
    "suppressed_budget",
    "cancelled",
)


@dataclass(frozen=True)
class AnalysisJob:
    id: Any
    job_type: str
    state: str
    priority: int
    source_event_id: Any
    dedupe_key: str
    input_fingerprint: str
    not_before: datetime
    lease_expires_at: datetime | None
    claimed_by: str | None
    attempt_count: int
    max_attempts: int
    payload: dict[str, Any]
    result_ref: dict[str, Any] | None = None
    last_error: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    correlation_id: Any = None

    @property
    def status(self) -> str:
        return self.state

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> AnalysisJob:
        payload = row.get("payload", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        result_ref = row.get("result_ref")
        if isinstance(result_ref, str):
            try:
                result_ref = json.loads(result_ref)
            except (TypeError, ValueError):
                result_ref = None
        if not isinstance(result_ref, dict):
            result_ref = None
        values = {field: row.get(field) for field in cls.__dataclass_fields__}
        values.update(payload=payload, result_ref=result_ref)
        values["job_type"] = str(values.get("job_type") or "")
        values["state"] = str(values.get("state") or "queued")
        values["priority"] = int(values.get("priority") or 0)
        values["dedupe_key"] = str(values.get("dedupe_key") or "")
        values["input_fingerprint"] = str(values.get("input_fingerprint") or "")
        values["attempt_count"] = int(values.get("attempt_count") or 0)
        values["max_attempts"] = int(values.get("max_attempts") or 1)
        return cls(**values)


@dataclass(frozen=True)
class JobEnqueueResult:
    job: AnalysisJob | None
    inserted: bool
    suppressed: bool = False

    @property
    def duplicate(self) -> bool:
        return not self.inserted

    @property
    def deduplicated(self) -> bool:
        return self.suppressed


_FIELDS = """id, job_type, state, priority, source_event_id, dedupe_key,
input_fingerprint, not_before, lease_expires_at, claimed_by, attempt_count,
max_attempts, payload, result_ref, last_error, created_at, started_at,
completed_at, cancelled_at, correlation_id"""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _supports_skip_locked(session: Any) -> bool:
    try:
        return session.get_bind().dialect.name not in {"sqlite"}
    except Exception:
        return True


def _first(result: Any) -> Mapping[str, Any] | None:
    try:
        result = result.mappings()
    except (AttributeError, TypeError):
        pass
    try:
        return result.first()
    except AttributeError:
        try:
            return next(iter(result), None)
        except TypeError:
            return None


def _rows(result: Any) -> list[Mapping[str, Any]]:
    try:
        result = result.mappings()
    except (AttributeError, TypeError):
        pass
    try:
        return list(result.all())
    except AttributeError:
        try:
            return list(result)
        except TypeError:
            return []


def _canonical_payload(payload: Mapping[str, Any] | Sequence[Any] | None) -> str:
    return json.dumps(
        payload if payload is not None else {},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sanitize_error(error: str | BaseException | None) -> str | None:
    """Return only a safe error type; never persist untrusted exception text."""
    if error is None:
        return None
    name = type(error).__name__ if isinstance(error, BaseException) else "Error"
    name = re.sub(r"[^A-Za-z0-9_.-]", "", name)[:120]
    return name or "Error"


def _identity_select(
    session: Any, identity: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    return _first(
        session.execute(
            text(
                f"SELECT {_FIELDS} FROM analysis_jobs WHERE job_type = :job_type AND dedupe_key = :dedupe_key AND input_fingerprint = :input_fingerprint ORDER BY created_at DESC, id DESC LIMIT 1"
            ),
            dict(identity),
        )
    )


def enqueue_job(
    session: Any,
    *,
    job_type: str,
    dedupe_key: str,
    input_fingerprint: str,
    payload: Mapping[str, Any] | Sequence[Any] | None,
    correlation_id: Any,
    source_event_id: Any = None,
    priority: int = 100,
    max_attempts: int = 5,
    not_before: datetime | None = None,
) -> JobEnqueueResult:
    if (
        not str(job_type).strip()
        or not str(dedupe_key).strip()
        or not str(input_fingerprint).strip()
    ):
        raise ValueError("job identity fields must be nonblank")
    if payload is not None and not isinstance(payload, Mapping):
        raise ValueError("job payload must be a JSON object")
    if correlation_id is None:
        raise ValueError("correlation_id is required")
    identity = {
        "job_type": job_type,
        "dedupe_key": dedupe_key,
        "input_fingerprint": input_fingerprint,
    }
    existing = _identity_select(session, identity)
    if existing is not None:
        return JobEnqueueResult(AnalysisJob.from_row(existing), False, True)
    params = {
        **identity,
        "payload": _canonical_payload(payload),
        "correlation_id": correlation_id,
        "source_event_id": source_event_id,
        "priority": int(priority),
        "max_attempts": max(1, int(max_attempts)),
        "not_before": not_before or _utcnow(),
    }
    row = _first(
        session.execute(
            text(
                "INSERT INTO analysis_jobs (job_type, state, priority, source_event_id, dedupe_key, input_fingerprint, not_before, max_attempts, payload, correlation_id) VALUES (:job_type, 'queued', :priority, :source_event_id, :dedupe_key, :input_fingerprint, :not_before, :max_attempts, CAST(:payload AS JSONB), :correlation_id) ON CONFLICT DO NOTHING RETURNING "
                + _FIELDS
            ),
            params,
        )
    )
    if row is not None:
        return JobEnqueueResult(AnalysisJob.from_row(row), True, False)
    existing = _identity_select(session, identity)
    return JobEnqueueResult(
        AnalysisJob.from_row(existing) if existing else None, False, True
    )


def claim_jobs(
    session: Any,
    worker_id: str,
    limit: int = 25,
    lease_seconds: float = 120,
    job_types: Sequence[str] | None = None,
) -> list[AnalysisJob]:
    now = _utcnow()
    lease_until = now + timedelta(seconds=max(1.0, float(lease_seconds)))
    params: dict[str, Any] = {"now": now, "limit": max(1, int(limit))}
    where = "((state IN ('queued','failed_retryable') AND not_before <= :now) OR (state = 'leased' AND lease_expires_at <= :now)) AND attempt_count < max_attempts"
    if job_types:
        names = []
        for index, value in enumerate(job_types):
            key = f"job_type_{index}"
            names.append(f":{key}")
            params[key] = str(value)
        where += " AND job_type IN (" + ", ".join(names) + ")"
    lock = " FOR UPDATE SKIP LOCKED" if _supports_skip_locked(session) else ""
    rows = _rows(
        session.execute(
            text(
                f"SELECT id FROM analysis_jobs WHERE {where} ORDER BY priority DESC, not_before ASC, created_at ASC, id ASC LIMIT :limit"
                + lock
            ),
            params,
        )
    )
    claims: list[AnalysisJob] = []
    for row in rows:
        updated = _first(
            session.execute(
                text(
                    "UPDATE analysis_jobs SET state = 'leased', claimed_by = :worker_id, lease_expires_at = :lease_until, attempt_count = attempt_count + 1 WHERE id = :id AND attempt_count < max_attempts AND ((state IN ('queued','failed_retryable') AND not_before <= :now) OR (state = 'leased' AND lease_expires_at <= :now)) RETURNING "
                    + _FIELDS
                ),
                {
                    "id": row["id"],
                    "worker_id": worker_id,
                    "now": now,
                    "lease_until": lease_until,
                },
            )
        )
        if updated is not None:
            claims.append(AnalysisJob.from_row(updated))
    return claims


def start_job(session: Any, job_id: Any, worker_id: str) -> bool:
    now = _utcnow()
    result = session.execute(
        text(
            "UPDATE analysis_jobs SET state = 'running', started_at = COALESCE(started_at, :now) WHERE id = :id AND state = 'leased' AND claimed_by = :worker_id AND lease_expires_at > :now"
        ),
        {"id": job_id, "worker_id": worker_id, "now": now},
    )
    return bool(getattr(result, "rowcount", 0))


def succeed_job(
    session: Any,
    job_id: Any,
    worker_id: str,
    result_ref: Mapping[str, Any] | None = None,
) -> bool:
    result = session.execute(
        text(
            "UPDATE analysis_jobs SET state = 'succeeded', completed_at = :now, claimed_by = NULL, lease_expires_at = NULL, result_ref = CAST(:result_ref AS JSONB) WHERE id = :id AND state = 'running' AND claimed_by = :worker_id"
        ),
        {
            "id": job_id,
            "worker_id": worker_id,
            "now": _utcnow(),
            "result_ref": _canonical_payload(result_ref)
            if result_ref is not None
            else None,
        },
    )
    return bool(getattr(result, "rowcount", 0))


def retry_job(
    session: Any,
    job_id: Any,
    worker_id: str,
    not_before: datetime,
    error: str | BaseException | None = None,
    error_state: str = "retryable",
) -> bool:
    result = session.execute(
        text(
            "UPDATE analysis_jobs SET state = 'failed_retryable', not_before = :not_before, claimed_by = NULL, lease_expires_at = NULL, last_error = :last_error WHERE id = :id AND state = 'running' AND claimed_by = :worker_id AND attempt_count < max_attempts"
        ),
        {
            "id": job_id,
            "worker_id": worker_id,
            "not_before": not_before,
            "last_error": sanitize_error(error),
        },
    )
    return bool(getattr(result, "rowcount", 0))


def terminal_fail_job(
    session: Any,
    job_id: Any,
    worker_id: str,
    error: str | BaseException | None = None,
    error_state: str = "terminal",
) -> bool:
    result = session.execute(
        text(
            "UPDATE analysis_jobs SET state = 'failed_terminal', completed_at = :now, claimed_by = NULL, lease_expires_at = NULL, last_error = :last_error WHERE id = :id AND state = 'running' AND claimed_by = :worker_id"
        ),
        {
            "id": job_id,
            "worker_id": worker_id,
            "now": _utcnow(),
            "last_error": sanitize_error(error),
        },
    )
    return bool(getattr(result, "rowcount", 0))


def renew_job_lease(
    session: Any, job_id: Any, worker_id: str, lease_seconds: float = 120
) -> bool:
    now = _utcnow()
    result = session.execute(
        text(
            "UPDATE analysis_jobs SET lease_expires_at = :lease_until "
            "WHERE id = :id AND claimed_by = :worker_id AND state IN ('leased','running') "
            "AND lease_expires_at > :now"
        ),
        {
            "id": job_id,
            "worker_id": worker_id,
            "now": now,
            "lease_until": now
            + timedelta(seconds=max(1.0, float(lease_seconds))),
        },
    )
    return bool(getattr(result, "rowcount", 0))


def suppress_job(
    session: Any,
    job_id: Any,
    worker_id: str | None = None,
    suppression_state: str = "suppressed_duplicate",
) -> bool:
    if suppression_state not in {
        "suppressed_duplicate",
        "suppressed_immaterial",
        "suppressed_budget",
    }:
        raise ValueError("invalid suppression state")
    guard = " AND claimed_by = :worker_id" if worker_id is not None else ""
    params: dict[str, Any] = {
        "id": job_id,
        "now": _utcnow(),
        "suppression_state": suppression_state,
    }
    if worker_id is not None:
        params["worker_id"] = worker_id
    result = session.execute(
        text(
            "UPDATE analysis_jobs SET state = :suppression_state, completed_at = :now, claimed_by = NULL, lease_expires_at = NULL WHERE id = :id AND state IN ('queued','leased','running','failed_retryable')"
            + guard
        ),
        params,
    )
    return bool(getattr(result, "rowcount", 0))


def reconcile_jobs(session: Any, limit: int = 100) -> int:
    result = session.execute(
        text(
            "UPDATE analysis_jobs SET state = CASE WHEN attempt_count >= max_attempts THEN 'failed_terminal' ELSE 'failed_retryable' END, not_before = :now, claimed_by = NULL, lease_expires_at = NULL, completed_at = CASE WHEN attempt_count >= max_attempts THEN :now ELSE completed_at END WHERE id IN (SELECT id FROM analysis_jobs WHERE state IN ('leased','running') AND lease_expires_at <= :now ORDER BY lease_expires_at ASC LIMIT :limit)"
        ),
        {"now": _utcnow(), "limit": max(1, int(limit))},
    )
    return int(getattr(result, "rowcount", 0) or 0)


__all__ = [
    "AnalysisJob",
    "JobEnqueueResult",
    "enqueue_job",
    "claim_jobs",
    "start_job",
    "renew_job_lease",
    "succeed_job",
    "retry_job",
    "terminal_fail_job",
    "suppress_job",
    "reconcile_jobs",
    "sanitize_error",
]
