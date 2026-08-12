"""Durable operation-job repository with caller-owned transactions.

Operation jobs are the durable hand-off between run acceptance and worker
execution.  ``accept_and_enqueue_operation`` inserts the ``cycle_runs``
acceptance row and its ``operation_jobs`` entry in ONE caller-owned
transaction, so acceptance + enqueue are atomic: either both persist or
neither does.  Lease claim, heartbeat renewal, retry/backoff, poison terminal
state, and expired-lease reclaim mirror the ``analysis_jobs`` contract.

Duplicate logical runs are prevented by the partial unique index
``idx_operation_jobs_active_identity`` plus an advisory transaction lock keyed
by the logical identity; a suppressed duplicate finalizes its acceptance row
inside the same transaction instead of leaving an unclaimed accepted run.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from analysis_jobs import (
    _canonical_payload,
    _first,
    _rows,
    _supports_skip_locked,
    _utcnow,
    sanitize_error,
)
from run_lifecycle import DEFAULT_HEARTBEAT_TIMEOUT, RunAcceptanceConflict

ACTIVE_STATES = ("queued", "leased", "running", "failed_retryable")
TERMINAL_STATES = ("succeeded", "failed_terminal", "suppressed_duplicate", "cancelled")
RUN_KINDS = frozenset({"cycle", "collector", "processor", "news", "filings"})


@dataclass(frozen=True)
class OperationJob:
    id: Any
    run_kind: str
    requested_component: str | None
    correlation_id: Any
    state: str
    dedupe_key: str
    input_fingerprint: str
    not_before: datetime
    lease_expires_at: datetime | None
    claimed_by: str | None
    attempt_count: int
    max_attempts: int
    payload: dict[str, Any]
    priority: int = 100
    result_ref: dict[str, Any] | None = None
    last_error: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None

    @property
    def status(self) -> str:
        return self.state

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> OperationJob:
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
        values["run_kind"] = str(values.get("run_kind") or "")
        values["state"] = str(values.get("state") or "queued")
        values["priority"] = int(values.get("priority") or 0)
        values["dedupe_key"] = str(values.get("dedupe_key") or "")
        values["input_fingerprint"] = str(values.get("input_fingerprint") or "")
        values["attempt_count"] = int(values.get("attempt_count") or 0)
        values["max_attempts"] = int(values.get("max_attempts") or 1)
        values["requested_component"] = values.get("requested_component")
        return cls(**values)


@dataclass(frozen=True)
class OperationJobEnqueueResult:
    job: OperationJob | None
    inserted: bool
    suppressed: bool = False

    @property
    def duplicate(self) -> bool:
        return not self.inserted


_FIELDS = """id, run_kind, requested_component, correlation_id, state, priority,
dedupe_key, input_fingerprint, not_before, lease_expires_at, claimed_by,
attempt_count, max_attempts, payload, result_ref, last_error, created_at,
started_at, completed_at, cancelled_at"""


def _jsonb_expr(column: str, session: Any) -> str:
    try:
        dialect = session.get_bind().dialect.name
    except Exception:
        dialect = "postgresql"
    return f"CAST(:{column} AS JSONB)" if dialect != "sqlite" else f":{column}"


def _identity_select(
    session: Any, identity: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    """Find an ACTIVE job with the same logical identity.

    Terminal rows are deliberately ignored: the partial unique index only
    guards active states, so an explicit retry or the next schedule window
    must be able to enqueue a fresh job after a terminal outcome.
    """
    return _first(
        session.execute(
            text(
                f"SELECT {_FIELDS} FROM operation_jobs "
                "WHERE run_kind = :run_kind AND dedupe_key = :dedupe_key "
                "AND input_fingerprint = :input_fingerprint "
                f"AND state IN {tuple(ACTIVE_STATES)} "
                "ORDER BY created_at DESC, id DESC LIMIT 1"
            ),
            dict(identity),
        )
    )


def enqueue_operation(
    session: Any,
    *,
    run_kind: str,
    correlation_id: Any,
    dedupe_key: str,
    input_fingerprint: str,
    payload: Mapping[str, Any] | Sequence[Any] | None,
    requested_component: str | None = None,
    priority: int = 100,
    max_attempts: int = 5,
    not_before: datetime | None = None,
) -> OperationJobEnqueueResult:
    """Enqueue one operation job in the caller's transaction (no commit).

    Raises ``ValueError`` for invalid identities or payloads.  A job whose
    logical identity already has an active row is suppressed and the existing
    active job is returned.
    """
    if run_kind not in RUN_KINDS:
        raise ValueError("unsupported operation run kind")
    if (
        not str(dedupe_key).strip()
        or not str(input_fingerprint).strip()
    ):
        raise ValueError("job identity fields must be nonblank")
    if payload is not None and not isinstance(payload, Mapping):
        raise ValueError("job payload must be a JSON object")
    if correlation_id is None:
        raise ValueError("correlation_id is required")
    identity = {
        "run_kind": run_kind,
        "dedupe_key": dedupe_key,
        "input_fingerprint": input_fingerprint,
    }
    existing = _identity_select(session, identity)
    if existing is not None:
        return OperationJobEnqueueResult(
            OperationJob.from_row(existing), False, True
        )
    params = {
        "id": str(uuid4()),
        **identity,
        "requested_component": requested_component,
        "correlation_id": correlation_id,
        "payload": _canonical_payload(payload),
        "priority": int(priority),
        "max_attempts": max(1, int(max_attempts)),
        "not_before": not_before or _utcnow(),
    }
    row = _first(
        session.execute(
            text(
                "INSERT INTO operation_jobs (id, run_kind, requested_component, "
                "correlation_id, state, priority, dedupe_key, input_fingerprint, "
                "not_before, max_attempts, payload) "
                "VALUES (:id, :run_kind, :requested_component, :correlation_id, "
                "'queued', :priority, :dedupe_key, :input_fingerprint, :not_before, "
                ":max_attempts, "
                + _jsonb_expr("payload", session)
                + ") ON CONFLICT DO NOTHING RETURNING "
                + _FIELDS
            ),
            params,
        )
    )
    if row is not None:
        return OperationJobEnqueueResult(OperationJob.from_row(row), True, False)
    existing = _identity_select(session, identity)
    return OperationJobEnqueueResult(
        OperationJob.from_row(existing) if existing else None, False, True
    )


def _advisory_lock_key(session: Any, run_kind: str, dedupe_key: str) -> None:
    try:
        dialect = session.get_bind().dialect.name
    except Exception:
        dialect = "postgresql"
    if dialect == "sqlite":
        return
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": f"operation:{run_kind}:{dedupe_key}"},
    )


def _identity_for(
    triggered_by: str,
    run_kind: str,
    requested_component: str | None,
    correlation_id: str,
    idempotency_key: str | None,
    dedupe_key: str | None,
    input_fingerprint: str | None,
) -> tuple[str, str]:
    """Derive the stable logical identity of one acceptance.

    A caller-supplied idempotency key makes the identity request-derived (so
    a lost-202 retry maps to the original job); otherwise it is
    correlation-derived and unique per request.
    """
    if idempotency_key is not None:
        identity_key = dedupe_key or (
            f"{triggered_by}:{run_kind}:{requested_component or 'none'}:"
            f"{idempotency_key}"
        )
        identity_fingerprint = input_fingerprint or f"v1:{idempotency_key}"
        return identity_key, identity_fingerprint
    identity_key = dedupe_key or f"{triggered_by}:{run_kind}:{correlation_id}"
    identity_fingerprint = input_fingerprint or correlation_id
    return identity_key, identity_fingerprint


def _replay_acceptance(
    session: Any,
    *,
    idempotency_key: str | None,
    run_kind: str,
    requested_component: str | None,
    identity_key: str,
    identity_fingerprint: str,
) -> tuple[datetime, OperationJobEnqueueResult] | None:
    """Return the ORIGINAL acceptance for a matching idempotent retry.

    Only an unkeyed caller is excluded; a keyed retry whose run kind,
    component, and logical identity all match the original job replays it.
    Any other reuse (or a legacy acceptance without a job to verify against)
    returns None so the caller raises a 409 conflict.
    """
    if idempotency_key is None:
        return None
    row = _first(
        session.execute(
            text(
                "SELECT correlation_id, accepted_at FROM cycle_runs "
                "WHERE idempotency_key = :key LIMIT 1"
            ),
            {"key": idempotency_key},
        )
    )
    if row is None:
        return None
    job = _first(
        session.execute(
            text(
                f"SELECT {_FIELDS} FROM operation_jobs "
                "WHERE correlation_id = :cid LIMIT 1"
            ),
            {"cid": row["correlation_id"]},
        )
    )
    if job is None:
        return None
    normalized_component = (requested_component or None) == (
        job.get("requested_component") or None
    )
    if (
        str(job.get("run_kind") or "") != run_kind
        or not normalized_component
        or str(job.get("dedupe_key") or "") != identity_key
        or str(job.get("input_fingerprint") or "") != identity_fingerprint
    ):
        return None
    accepted_at = row["accepted_at"]
    if isinstance(accepted_at, str):
        try:
            accepted_at = datetime.fromisoformat(
                accepted_at.replace("Z", "+00:00")
            )
        except ValueError:
            return None
    return (
        accepted_at,
        OperationJobEnqueueResult(OperationJob.from_row(job), False, False),
    )


def accept_and_enqueue_operation(
    config: dict,
    *,
    correlation_id: str,
    triggered_by: str,
    run_kind: str,
    requested_component: str | None = None,
    idempotency_key: str | None = None,
    request_summary: dict | None = None,
    dedupe_key: str | None = None,
    input_fingerprint: str | None = None,
    payload: Mapping[str, Any] | None = None,
    priority: int = 100,
    max_attempts: int = 5,
    not_before: datetime | None = None,
) -> tuple[datetime, OperationJobEnqueueResult]:
    """Accept a durable run and enqueue its operation job atomically.

    Returns ``(accepted_at, enqueued)``; ``enqueued.job`` always carries the
    effective job (the original one on an idempotent replay).

    Idempotency contract: a caller that lost the 202 may retry with the SAME
    idempotency key, run kind, component, and request identity.  The retry
    returns the ORIGINAL acceptance (accepted_at + job correlation) and never
    creates a second run or job (``enqueued.inserted is False`` and
    ``enqueued.suppressed is False``).  Reusing the key with a DIFFERENT
    request raises ``RunAcceptanceConflict`` (409), as does any other
    correlation/idempotency-key race.

    A duplicate logical run under a fresh key (scheduler windows) finalizes
    its acceptance row inside the same transaction with an ``already_queued``
    summary and returns ``enqueued.suppressed is True``.
    """
    if run_kind not in RUN_KINDS:
        raise ValueError("unsupported operation run kind")
    import orchestrator

    accepted_at = _utcnow()
    identity_key, identity_fingerprint = _identity_for(
        triggered_by,
        run_kind,
        requested_component,
        correlation_id,
        idempotency_key,
        dedupe_key,
        input_fingerprint,
    )
    with orchestrator.get_session(config) as session:
        # Targetless ON CONFLICT DO NOTHING: a unique conflict on EITHER the
        # correlation primary key or the idempotency_key index is swallowed
        # without aborting the transaction, so the replay queries below can
        # run on PostgreSQL (a targeted conflict would poison the txn).
        inserted = session.execute(
            text(
                "INSERT INTO cycle_runs "
                "(correlation_id, status, accepted_at, triggered_by, run_kind, "
                "requested_component, idempotency_key, summary) "
                "VALUES (:cid, 'accepted', :accepted_at, :triggered_by, :run_kind, "
                ":component, :idempotency_key, "
                + _jsonb_expr("summary", session)
                + ") ON CONFLICT DO NOTHING"
            ),
            {
                "cid": correlation_id,
                "accepted_at": accepted_at,
                "triggered_by": triggered_by,
                "run_kind": run_kind,
                "component": requested_component,
                "idempotency_key": idempotency_key,
                "summary": _canonical_payload(request_summary or {}),
            },
        )
        if getattr(inserted, "rowcount", 0) == 0:
            # A conflict happened.  Distinguish the two unique keys by query:
            # if the correlation row exists it is a correlation conflict
            # (override adoption, same-correlation replay, or genuine
            # conflict); otherwise it is an idempotency-key conflict (replay
            # or key-reuse conflict).
            existing = _first(
                session.execute(
                    text(
                        "SELECT status, triggered_by, idempotency_key FROM cycle_runs "
                        "WHERE correlation_id = :cid"
                    ),
                    {"cid": correlation_id},
                )
            )
            if existing is not None:
                # The API may pre-register a budget-override placeholder row
                # (triggered_by='api_manual_override'); adopt it as this run's
                # acceptance.
                if (
                    str(existing.get("triggered_by") or "") == "api_manual_override"
                ):
                    session.execute(
                        text(
                            "UPDATE cycle_runs SET status = 'accepted', "
                            "accepted_at = :accepted_at, started_at = NULL, "
                            "worker_id = NULL, heartbeat_at = NULL, "
                            "idempotency_key = COALESCE(idempotency_key, :idempotency_key), "
                            "summary = COALESCE(summary, " + _jsonb_expr("empty_summary", session)
                            + ") || " + _jsonb_expr("summary", session)
                            + " WHERE correlation_id = :cid"
                        ),
                        {
                            "cid": correlation_id,
                            "accepted_at": accepted_at,
                            "idempotency_key": idempotency_key,
                            "empty_summary": _canonical_payload({}),
                            "summary": _canonical_payload(request_summary or {}),
                        },
                    )
                else:
                    # Same-correlation resubmission: replay only when the
                    # stored idempotency key matches the request (otherwise the
                    # request collides with two different rows -> conflict).
                    if (
                        idempotency_key is not None
                        and existing.get("idempotency_key") == idempotency_key
                    ):
                        replay = _replay_acceptance(
                            session,
                            idempotency_key=idempotency_key,
                            run_kind=run_kind,
                            requested_component=requested_component,
                            identity_key=identity_key,
                            identity_fingerprint=identity_fingerprint,
                        )
                        if replay is not None:
                            return replay
                    raise RunAcceptanceConflict(
                        "run correlation or idempotency key already exists"
                    )
            else:
                # The correlation is new, so this is an idempotency-key
                # conflict.  A matching retry replays the original acceptance;
                # anything else is a genuine key-reuse conflict.
                replay = _replay_acceptance(
                    session,
                    idempotency_key=idempotency_key,
                    run_kind=run_kind,
                    requested_component=requested_component,
                    identity_key=identity_key,
                    identity_fingerprint=identity_fingerprint,
                )
                if replay is not None:
                    return replay
                raise RunAcceptanceConflict(
                    "run correlation or idempotency key already exists"
                )
        _advisory_lock_key(session, run_kind, identity_key)
        enqueued = enqueue_operation(
            session,
            run_kind=run_kind,
            correlation_id=correlation_id,
            dedupe_key=identity_key,
            input_fingerprint=identity_fingerprint,
            payload=payload,
            requested_component=requested_component,
            priority=priority,
            max_attempts=max_attempts,
            not_before=not_before,
        )
        if not enqueued.inserted:
            # Duplicate logical run: close the acceptance row now so no worker
            # ever claims an orphaned accepted run for work that already exists.
            prior = enqueued.job
            summary = {
                "status": "already_queued",
                "inserted": False,
                "prior_job_id": str(getattr(prior, "id", "") or ""),
                "prior_correlation_id": str(
                    getattr(prior, "correlation_id", "") or ""
                ),
            }
            session.execute(
                text(
                    "UPDATE cycle_runs SET status = 'completed', "
                    "result_status = 'skipped', summary = "
                    + _jsonb_expr("summary", session)
                    + ", completed_at = :completed_at, heartbeat_at = :completed_at "
                    "WHERE correlation_id = :cid AND status = 'accepted'"
                ),
                {
                    "cid": correlation_id,
                    "completed_at": accepted_at,
                    "summary": _canonical_payload(summary),
                },
            )
    return accepted_at, enqueued


def claim_operation_jobs(
    session: Any,
    worker_id: str,
    limit: int = 25,
    lease_seconds: float = 120,
    run_kinds: Sequence[str] | None = None,
) -> list[OperationJob]:
    """Atomically lease eligible operation jobs; expired leases are eligible."""
    now = _utcnow()
    lease_until = now + timedelta(seconds=max(1.0, float(lease_seconds)))
    params: dict[str, Any] = {"now": now, "limit": max(1, int(limit))}
    where = (
        "((state IN ('queued','failed_retryable') AND not_before <= :now) "
        "OR (state = 'leased' AND lease_expires_at <= :now)) "
        "AND attempt_count < max_attempts"
    )
    if run_kinds:
        names = []
        for index, value in enumerate(run_kinds):
            key = f"run_kind_{index}"
            names.append(f":{key}")
            params[key] = str(value)
        where += " AND run_kind IN (" + ", ".join(names) + ")"
    lock = " FOR UPDATE SKIP LOCKED" if _supports_skip_locked(session) else ""
    rows = _rows(
        session.execute(
            text(
                f"SELECT id, correlation_id FROM operation_jobs WHERE {where} "
                "ORDER BY priority DESC, not_before ASC, created_at ASC, id ASC "
                "LIMIT :limit" + lock
            ),
            params,
        )
    )
    claims: list[OperationJob] = []
    for row in rows:
        # A claimable operation job means no worker holds its lease, so a
        # still-'running' cycle_runs row whose heartbeat has aged past the
        # timeout belongs to a dead or retired attempt; reset it to 'accepted'
        # so this claim can start execution.  A FRESH running row is either
        # this worker's own attempt or another live worker's and must never be
        # clobbered (that would silently break the other owner's finalize).
        # Rows already terminal stay untouched and fail the claim below.
        session.execute(
            text(
                "UPDATE cycle_runs SET status = 'accepted', worker_id = NULL, "
                "started_at = NULL WHERE correlation_id = :cid AND status = 'running' "
                "AND COALESCE(heartbeat_at, started_at) < :stale_cutoff"
            ),
            {
                "cid": row["correlation_id"],
                "stale_cutoff": now - DEFAULT_HEARTBEAT_TIMEOUT,
            },
        )
        updated = _first(
            session.execute(
                text(
                    "UPDATE operation_jobs SET state = 'leased', "
                    "claimed_by = :worker_id, lease_expires_at = :lease_until, "
                    "attempt_count = attempt_count + 1 WHERE id = :id "
                    "AND attempt_count < max_attempts "
                    "AND ((state IN ('queued','failed_retryable') AND not_before <= :now) "
                    "OR (state = 'leased' AND lease_expires_at <= :now)) RETURNING "
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
            claims.append(OperationJob.from_row(updated))
    return claims


def start_operation_job(
    session: Any, job_id: Any, worker_id: str, lease_seconds: float = 120
) -> bool:
    """Transition one job to ``running`` in the caller's transaction.

    - An owned unexpired lease starts normally (fail closed: an expired lease
      or another worker's lease never starts).
    - A job handed to the worker without a lease (``queued`` or
      ``failed_retryable``) is claimed and started in the SAME conditional
      update, so the transition is atomic and the attempt is counted once.
      ``not_before`` is deliberately not re-checked here: the caller's claim
      already gated eligibility, and starting a job that was explicitly handed
      over must not dead-letter it over a backoff boundary.
    """
    now = _utcnow()
    result = session.execute(
        text(
            "UPDATE operation_jobs SET state = 'running', "
            "claimed_by = :worker_id, lease_expires_at = :lease_until, "
            "started_at = COALESCE(started_at, :now), "
            "attempt_count = attempt_count + CASE WHEN state = 'leased' THEN 0 ELSE 1 END "
            "WHERE id = :id AND ("
            "  (state IN ('queued', 'failed_retryable') AND attempt_count < max_attempts) "
            "  OR (state = 'leased' AND claimed_by = :worker_id "
            "     AND lease_expires_at > :now)"
            ") "
        ),
        {
            "id": job_id,
            "worker_id": worker_id,
            "now": now,
            "lease_until": now + timedelta(seconds=max(1.0, float(lease_seconds))),
        },
    )
    return bool(getattr(result, "rowcount", 0))


def succeed_operation_job(
    session: Any,
    job_id: Any,
    worker_id: str,
    result_ref: Mapping[str, Any] | None = None,
) -> bool:
    result = session.execute(
        text(
            "UPDATE operation_jobs SET state = 'succeeded', completed_at = :now, "
            "claimed_by = NULL, lease_expires_at = NULL, result_ref = "
            + _jsonb_expr("result_ref", session)
            + " WHERE id = :id AND state = 'running' AND claimed_by = :worker_id"
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


def retry_operation_job(
    session: Any,
    job_id: Any,
    worker_id: str,
    not_before: datetime,
    error: str | BaseException | None = None,
) -> bool:
    """Move an owned running job to retryable AND release its run row.

    The cycle_runs row is reset to ``accepted`` (clearing the dead attempt's
    worker ownership) in the SAME transaction, so the retried attempt can
    claim it immediately after ``not_before`` without waiting for any stale
    timeout.  Only a job the caller still owns is touched.
    """
    updated = _first(
        session.execute(
            text(
                "UPDATE operation_jobs SET state = 'failed_retryable', "
                "not_before = :not_before, claimed_by = NULL, lease_expires_at = NULL, "
                "last_error = :last_error WHERE id = :id AND state = 'running' "
                "AND claimed_by = :worker_id AND attempt_count < max_attempts "
                "RETURNING correlation_id"
            ),
            {
                "id": job_id,
                "worker_id": worker_id,
                "not_before": not_before,
                "last_error": sanitize_error(error),
            },
        )
    )
    if updated is None:
        return False
    session.execute(
        text(
            "UPDATE cycle_runs SET status = 'accepted', worker_id = NULL, "
            "started_at = NULL, heartbeat_at = NULL "
            "WHERE correlation_id = :cid AND status = 'running'"
        ),
        {"cid": updated["correlation_id"]},
    )
    return True


def terminal_fail_operation_job(
    session: Any,
    job_id: Any,
    worker_id: str,
    error: str | BaseException | None = None,
) -> bool:
    result = session.execute(
        text(
            "UPDATE operation_jobs SET state = 'failed_terminal', completed_at = :now, "
            "claimed_by = NULL, lease_expires_at = NULL, last_error = :last_error "
            "WHERE id = :id AND state = 'running' AND claimed_by = :worker_id"
        ),
        {
            "id": job_id,
            "worker_id": worker_id,
            "now": _utcnow(),
            "last_error": sanitize_error(error),
        },
    )
    return bool(getattr(result, "rowcount", 0))


def renew_operation_job_lease(
    session: Any, job_id: Any, worker_id: str, lease_seconds: float = 120
) -> bool:
    now = _utcnow()
    result = session.execute(
        text(
            "UPDATE operation_jobs SET lease_expires_at = :lease_until "
            "WHERE id = :id AND claimed_by = :worker_id "
            "AND state IN ('leased','running') AND lease_expires_at > :now"
        ),
        {
            "id": job_id,
            "worker_id": worker_id,
            "now": now,
            "lease_until": now + timedelta(seconds=max(1.0, float(lease_seconds))),
        },
    )
    return bool(getattr(result, "rowcount", 0))


def reconcile_operation_jobs(session: Any, limit: int = 100) -> int:
    """Recover expired-leased jobs AND their run rows in one transaction.

    - Terminal (attempts exhausted): job -> failed_terminal AND the matching
      cycle_runs row -> failed (sanitized reason).  Any failure raises so the
      whole reconcile transaction rolls back (job and run stay coherent).
    - Retryable: job -> failed_retryable AND the matching running cycle_runs
      row -> accepted (worker/heartbeat cleared) immediately, so the run row
      never falsely reports running/owned during the backoff window.
    """
    now = _utcnow()
    reason = "lease expired without completion"
    params: dict[str, Any] = {"now": now, "limit": max(1, int(limit))}
    lock = " FOR UPDATE SKIP LOCKED" if _supports_skip_locked(session) else ""
    candidates = _rows(
        session.execute(
            text(
                "SELECT id, correlation_id, attempt_count, max_attempts "
                "FROM operation_jobs "
                "WHERE state IN ('leased','running') AND lease_expires_at <= :now "
                "ORDER BY lease_expires_at ASC, id ASC LIMIT :limit" + lock
            ),
            params,
        )
    )
    repaired = 0
    for candidate in candidates:
        job_id = candidate["id"]
        attempt_count = int(candidate["attempt_count"] or 0)
        max_attempts = int(candidate["max_attempts"] or 1)
        if attempt_count >= max_attempts:
            updated = _first(
                session.execute(
                    text(
                        "UPDATE operation_jobs SET state = 'failed_terminal', "
                        "completed_at = :now, claimed_by = NULL, "
                        "lease_expires_at = NULL, last_error = :reason "
                        "WHERE id = :id AND state IN ('leased','running') "
                        "AND lease_expires_at <= :now RETURNING correlation_id"
                    ),
                    {"id": job_id, "now": now, "reason": reason},
                )
            )
            if updated is not None:
                session.execute(
                    text(
                        "UPDATE cycle_runs SET status = 'failed', "
                        "result_status = 'failed', completed_at = :now, "
                        "heartbeat_at = :now, error_message = :reason, "
                        "summary = " + _jsonb_expr("summary", session)
                        + " WHERE correlation_id = :cid "
                        "AND status IN ('running','accepted')"
                    ),
                    {
                        "cid": updated["correlation_id"],
                        "now": now,
                        "reason": reason,
                        "summary": _canonical_payload(
                            {"status": "failed", "reason": reason}
                        ),
                    },
                )
                repaired += 1
        else:
            updated = _first(
                session.execute(
                    text(
                        "UPDATE operation_jobs SET state = 'failed_retryable', "
                        "not_before = :now, claimed_by = NULL, "
                        "lease_expires_at = NULL, last_error = :reason "
                        "WHERE id = :id AND state IN ('leased','running') "
                        "AND lease_expires_at <= :now RETURNING correlation_id"
                    ),
                    {"id": job_id, "now": now, "reason": reason},
                )
            )
            if updated is not None:
                session.execute(
                    text(
                        "UPDATE cycle_runs SET status = 'accepted', "
                        "worker_id = NULL, started_at = NULL, heartbeat_at = NULL "
                        "WHERE correlation_id = :cid AND status = 'running'"
                    ),
                    {"cid": updated["correlation_id"]},
                )
                repaired += 1
    return repaired


def operation_queue_summary(config: dict) -> dict[str, Any]:
    """Bounded durable queue state for status endpoints (no payloads/errors)."""
    import orchestrator

    with orchestrator.get_session(config) as session:
        rows = session.execute(
            text(
                "SELECT state, COUNT(*) AS count, MIN(created_at) AS oldest_created_at "
                "FROM operation_jobs GROUP BY state ORDER BY state"
            )
        ).mappings()
        counts = {str(row["state"]): int(row["count"]) for row in rows}
        active_rows = [state for state in ACTIVE_STATES if counts.get(state, 0)]
    oldest = None
    if active_rows:
        with orchestrator.get_session(config) as session:
            row = session.execute(
                text(
                    "SELECT MIN(created_at) AS oldest FROM operation_jobs "
                    "WHERE state IN ('queued','leased','running','failed_retryable')"
                )
            ).mappings().first()
            oldest = row["oldest"] if row is not None else None
    return {
        "counts": counts,
        "active": sum(counts.get(state, 0) for state in ACTIVE_STATES),
        "oldest_pending_at": oldest,
    }


def latest_cycle_status(config: dict) -> dict[str, Any]:
    """Durable replacement for the in-process cycle correlation global."""
    import orchestrator

    with orchestrator.get_session(config) as session:
        row = (
            session.execute(
                text(
                    "SELECT correlation_id, status, accepted_at, started_at, "
                    "heartbeat_at FROM cycle_runs "
                    "WHERE run_kind = 'cycle' AND status IN ('accepted','running') "
                    "ORDER BY accepted_at DESC, correlation_id DESC LIMIT 1"
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        return {"running": False, "correlation_id": None}
    return {
        "running": True,
        "correlation_id": str(row["correlation_id"]),
        "status": row["status"],
        "accepted_at": row["accepted_at"],
        "started_at": row["started_at"],
        "heartbeat_at": row["heartbeat_at"],
    }


__all__ = [
    "ACTIVE_STATES",
    "RUN_KINDS",
    "TERMINAL_STATES",
    "OperationJob",
    "OperationJobEnqueueResult",
    "accept_and_enqueue_operation",
    "claim_operation_jobs",
    "enqueue_operation",
    "latest_cycle_status",
    "operation_queue_summary",
    "reconcile_operation_jobs",
    "renew_operation_job_lease",
    "retry_operation_job",
    "start_operation_job",
    "succeed_operation_job",
    "terminal_fail_operation_job",
]
