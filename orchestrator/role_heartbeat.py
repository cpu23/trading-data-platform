"""Durable role liveness: per-process heartbeat rows replace process globals.

Each role process (api, scheduler, worker, outbox, quotes) upserts its OWN
``role_heartbeats`` row keyed by ``(role, instance_id)`` while it is alive, so
replicas of the same role never overwrite each other's liveness and an exiting
instance's ``stopped`` write cannot clobber a healthy sibling.  Health
endpoints and the ``roles check <ROLE>`` command aggregate fresh instances:
a role is ready when it has at least one fresh healthy instance (the scheduler
additionally requires a fresh ``running`` leader).  Stale rows are ignored by
readers and can be pruned with :func:`prune_stale_role_heartbeats`.
"""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

DEFAULT_HEARTBEAT_TIMEOUT = timedelta(seconds=12)
ROLES = ("api", "scheduler", "worker", "outbox", "quotes")

# Small bounded allowance for writer clocks running slightly ahead; anything
# further in the future is a clock/row corruption and must NOT count as fresh.
ALLOWED_CLOCK_SKEW = timedelta(seconds=5)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _get_session(config: dict):
    import orchestrator

    return orchestrator.get_session(config)


def default_instance_id() -> str:
    """Per-process identity: hostname + pid, unique across replicas/restarts."""
    return f"{socket.gethostname() or 'host'}:{os.getpid()}"


def _jsonb_expr(session: Any) -> str:
    try:
        dialect = session.get_bind().dialect.name
    except Exception:
        dialect = "postgresql"
    return "CAST(:detail AS JSONB)" if dialect != "sqlite" else ":detail"


def update_role_heartbeat(
    config: dict,
    role: str,
    status: str,
    detail: Mapping[str, Any] | None = None,
    instance_id: str | None = None,
) -> None:
    """Record liveness for ONE process instance in its own transaction."""
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    if not str(status).strip():
        raise ValueError("role heartbeat status must be nonblank")
    resolved_instance = instance_id or default_instance_id()
    if not str(resolved_instance).strip():
        raise ValueError("role heartbeat instance_id must be nonblank")
    with _get_session(config) as session:
        session.execute(
            text(
                "INSERT INTO role_heartbeats (role, instance_id, status, "
                "last_heartbeat_at, detail) "
                "VALUES (:role, :instance_id, :status, :now, "
                + _jsonb_expr(session)
                + ") ON CONFLICT (role, instance_id) DO UPDATE SET "
                "status = EXCLUDED.status, "
                "last_heartbeat_at = EXCLUDED.last_heartbeat_at, "
                "detail = EXCLUDED.detail"
            ),
            {
                "role": role,
                "instance_id": str(resolved_instance),
                "status": str(status).strip(),
                "now": _utcnow(),
                "detail": json.dumps(
                    dict(detail or {}), sort_keys=True, separators=(",", ":")
                ),
            },
        )


def _row_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    detail = row.get("detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except (TypeError, ValueError):
            detail = {}
    if not isinstance(detail, dict):
        detail = {}
    return {
        "role": row["role"],
        "instance_id": row["instance_id"],
        "status": row["status"],
        "last_heartbeat_at": row["last_heartbeat_at"],
        "detail": detail,
    }


def list_role_heartbeats(config: dict, role: str) -> list[dict[str, Any]]:
    """Return every recorded instance row for one role (bounded)."""
    with _get_session(config) as session:
        rows = session.execute(
            text(
                "SELECT role, instance_id, status, last_heartbeat_at, detail "
                "FROM role_heartbeats WHERE role = :role "
                "ORDER BY last_heartbeat_at DESC LIMIT 100"
            ),
            {"role": role},
        ).mappings()
        return [_row_to_dict(row) for row in rows]


def read_role_heartbeat(config: dict, role: str) -> dict[str, Any] | None:
    """Return the freshest row for a role, or None when none exists."""
    rows = list_role_heartbeats(config, role)
    return rows[0] if rows else None


def fresh_role_heartbeats(
    config: dict,
    role: str,
    now: datetime | None = None,
    timeout: timedelta = DEFAULT_HEARTBEAT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Return the fresh (non-stale, non-future) instance rows for one role."""
    current = now or _utcnow()
    return [
        heartbeat
        for heartbeat in list_role_heartbeats(config, role)
        if heartbeat_is_fresh(heartbeat, now=current, timeout=timeout)
    ]


def heartbeat_is_fresh(
    heartbeat: dict[str, Any] | None,
    now: datetime | None = None,
    timeout: timedelta = DEFAULT_HEARTBEAT_TIMEOUT,
) -> bool:
    if heartbeat is None:
        return False
    last = heartbeat.get("last_heartbeat_at")
    if last is None:
        return False
    if isinstance(last, str):
        try:
            last = datetime.fromisoformat(last.replace("Z", "+00:00"))
        except ValueError:
            return False
    current = now or _utcnow()
    if getattr(last, "tzinfo", None) is None:
        last = last.replace(tzinfo=UTC)
    age = current - last
    # A future timestamp (beyond the tiny documented skew) is stale data and
    # must never keep a dead role healthy until the clock catches up.
    return -ALLOWED_CLOCK_SKEW <= age <= timeout


def prune_stale_role_heartbeats(
    config: dict,
    role: str | None = None,
    older_than: timedelta = DEFAULT_HEARTBEAT_TIMEOUT * 10,
) -> int:
    """Delete rows far past the stale window (bounded; operator-facing)."""
    params: dict[str, Any] = {
        "cutoff": _utcnow() - max(older_than, DEFAULT_HEARTBEAT_TIMEOUT)
    }
    where = "last_heartbeat_at < :cutoff"
    if role is not None:
        if role not in ROLES:
            raise ValueError(f"unknown role: {role}")
        where += " AND role = :role"
        params["role"] = role
    with _get_session(config) as session:
        result = session.execute(
            text(f"DELETE FROM role_heartbeats WHERE {where}"),
            params,
        )
        return int(getattr(result, "rowcount", 0) or 0)


def role_has_fresh_healthy_instance(
    config: dict,
    role: str,
    healthy_statuses: set[str],
    now: datetime | None = None,
    timeout: timedelta = DEFAULT_HEARTBEAT_TIMEOUT,
) -> bool:
    """True when at least one fresh instance reports a healthy status."""
    for heartbeat in fresh_role_heartbeats(config, role, now=now, timeout=timeout):
        if str(heartbeat.get("status") or "") in healthy_statuses:
            return True
    return False


__all__ = [
    "ALLOWED_CLOCK_SKEW",
    "DEFAULT_HEARTBEAT_TIMEOUT",
    "ROLES",
    "default_instance_id",
    "fresh_role_heartbeats",
    "heartbeat_is_fresh",
    "list_role_heartbeats",
    "prune_stale_role_heartbeats",
    "read_role_heartbeat",
    "role_has_fresh_healthy_instance",
    "update_role_heartbeat",
]
