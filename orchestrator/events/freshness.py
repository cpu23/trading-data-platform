"""Deterministic source freshness classification and transaction-local persistence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text

_COLUMNS = (
    "state",
    "expected_next_at",
    "last_attempt_at",
    "last_success_at",
    "last_observation_at",
    "last_material_change_at",
    "lag_seconds",
    "reason_code",
    "detail",
    "cache_mode",
    "consecutive_failures",
    "updated_at",
)
_FAILURE_STATES = {"failed", "failure", "error", "exception"}
_RATE_LIMIT_STATES = {
    "rate_limited",
    "rate-limited",
    "ratelimited",
    "throttled",
    "throttle",
}
_CACHE_STATES = {"cached", "cached_fallback", "fallback", "stale_cache"}
_SUCCESS_STATES = {
    "success",
    "succeeded",
    "completed",
    "complete",
    "ok",
    "healthy",
    "no_change",
    "no-change",
    "unchanged",
    "idle",
    "expected_idle",
}


def _utc(value: Any, *, default: datetime | None = None) -> datetime | None:
    if value is None:
        return default
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise TypeError("timestamp must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _json_safe(value: Any) -> Any:
    """Convert diagnostics to JSON primitives without exposing exception text."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return (
            value
            if value == value and value not in (float("inf"), float("-inf"))
            else None
        )
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (UUID, Enum)):
        return _json_safe(value.value if isinstance(value, Enum) else str(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return {"type": type(value).__name__}


def _config_value(config: Any, key: str, default: Any = None) -> Any:
    if isinstance(config, Mapping):
        return config.get(key, default)
    return getattr(config, key, default)


def _status_value(status: Any) -> str | None:
    if status is None:
        return None
    value = getattr(status, "value", status)
    return str(value).strip().lower().replace(" ", "_")


def _cron_next(
    schedule: str | None, anchor: datetime | None, now: datetime
) -> datetime | None:
    if not schedule:
        return None
    # CronTrigger is deliberately used here rather than a hand-written parser.
    trigger = CronTrigger.from_crontab(str(schedule), timezone=UTC)
    if anchor is None:
        return trigger.get_next_fire_time(None, now)
    # Passing the attempt as previous_fire_time yields the next scheduled run,
    # including when the attempt itself happened exactly on a cron boundary.
    return trigger.get_next_fire_time(anchor, anchor)


def calculate_freshness_state(
    *,
    enabled: bool,
    schedule: str | None,
    last_attempt_at: datetime | None,
    last_success_at: datetime | None,
    last_observation_at: datetime | None,
    last_material_change_at: datetime | None,
    last_status: str | None,
    records_fetched: int | None,
    consecutive_failures: int = 0,
    cache_mode: str | None = None,
    reason_code: str | None = None,
    detail: Any = None,
    now: datetime | None = None,
    grace_seconds: float = 300,
) -> dict[str, Any]:
    """Classify a source without I/O or wall-clock dependence when ``now`` is set."""
    current = _utc(now, default=datetime.now(UTC))
    assert current is not None
    attempt = _utc(last_attempt_at)
    success = _utc(last_success_at)
    observation = _utc(last_observation_at)
    material = _utc(last_material_change_at)
    status = _status_value(last_status)
    cache = _status_value(cache_mode)
    failures = max(0, int(consecutive_failures or 0))
    safe_detail = _json_safe(detail) if detail is not None else {}
    if not isinstance(safe_detail, dict):
        safe_detail = {"value": safe_detail}

    expected_next: datetime | None = None
    schedule_error: Exception | None = None
    if enabled and schedule:
        try:
            expected_next = _cron_next(schedule, attempt, current)
        except (
            Exception
        ) as exc:  # schedule diagnostics must not leak the expression/error
            schedule_error = exc

    if not enabled:
        state = "disabled"
        expected_next = None
    elif status in _RATE_LIMIT_STATES:
        state = "rate_limited"
    elif status in _FAILURE_STATES:
        state = "failed"
    elif status in _CACHE_STATES or (
        cache in _CACHE_STATES and status in _SUCCESS_STATES
    ):
        state = "cached_fallback"
    elif (
        attempt is None
        and success is None
        and observation is None
        and status not in _SUCCESS_STATES
    ):
        state = "never_run"
    elif schedule_error is not None or not schedule:
        state = "outside_schedule"
    else:
        is_success = status in _SUCCESS_STATES or (
            status is None and success is not None
        )
        cache_fallback = cache in _CACHE_STATES
        if cache_fallback and is_success:
            state = "cached_fallback"
        elif expected_next is not None and current < expected_next:
            state = (
                "expected_idle" if is_success and records_fetched == 0 else "current"
            )
        elif expected_next is not None:
            overdue = max(0.0, (current - expected_next).total_seconds())
            state = "delayed" if overdue <= max(0.0, float(grace_seconds)) else "stale"
        else:
            state = "outside_schedule"

    lag_seconds = 0.0
    if expected_next is not None and current > expected_next:
        lag_seconds = max(0.0, (current - expected_next).total_seconds())
    elif (
        state in {"failed", "rate_limited", "delayed", "stale"} and attempt is not None
    ):
        lag_seconds = max(0.0, (current - attempt).total_seconds())

    if schedule_error is not None:
        safe_detail = {**safe_detail, "error_type": type(schedule_error).__name__}
    result = {
        "state": state,
        "expected_next_at": expected_next,
        "last_attempt_at": attempt,
        "last_success_at": success,
        "last_observation_at": observation,
        "last_material_change_at": material,
        "lag_seconds": lag_seconds,
        "reason_code": reason_code,
        "detail": safe_detail,
        "cache_mode": cache_mode,
        "consecutive_failures": failures,
        "updated_at": current,
    }
    return {column: result[column] for column in _COLUMNS}


def _row_mapping(result: Any) -> dict[str, Any] | None:
    if result is None:
        return None
    mappings = getattr(result, "mappings", None)
    if callable(mappings):
        row = mappings().first()
        return dict(row) if row is not None else None
    fetchone = getattr(result, "fetchone", None)
    if callable(fetchone):
        row = fetchone()
        if row is None:
            return None
        mapping = getattr(row, "_mapping", None)
        if mapping is not None:
            return dict(mapping)
    return None


def _lock_source(session: Any, source: str) -> None:
    """Serialize freshness read-modify-write, including the absent-row case."""
    try:
        dialect = session.get_bind().dialect.name
    except Exception:
        dialect = None
    if dialect != "postgresql":
        return
    try:
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"source_freshness:{source}"},
        )
    except Exception:
        if dialect == "postgresql":
            raise


def _existing(session: Any, source: str) -> dict[str, Any] | None:
    result = session.execute(
        text(
            "SELECT source, state, expected_next_at, last_attempt_at, last_success_at, "
            "last_observation_at, last_material_change_at, lag_seconds, reason_code, detail, "
            "cache_mode, consecutive_failures, updated_at "
            "FROM source_freshness_state WHERE source = :source"
        ),
        {"source": source},
    )
    return _row_mapping(result)


def _upsert(session: Any, source: str, row: Mapping[str, Any]) -> dict[str, Any]:
    values = {"source": source, **{column: row.get(column) for column in _COLUMNS}}
    values["detail"] = json.dumps(
        _json_safe(values["detail"] or {}), separators=(",", ":")
    )
    statement = text(
        "INSERT INTO source_freshness_state "
        "(source, state, expected_next_at, last_attempt_at, last_success_at, "
        "last_observation_at, last_material_change_at, lag_seconds, reason_code, detail, "
        "cache_mode, consecutive_failures, updated_at) "
        "VALUES (:source, :state, :expected_next_at, :last_attempt_at, :last_success_at, "
        ":last_observation_at, :last_material_change_at, :lag_seconds, :reason_code, "
        "CAST(:detail AS JSONB), :cache_mode, :consecutive_failures, :updated_at) "
        "ON CONFLICT (source) DO UPDATE SET "
        "state = EXCLUDED.state, expected_next_at = EXCLUDED.expected_next_at, "
        "last_attempt_at = EXCLUDED.last_attempt_at, last_success_at = EXCLUDED.last_success_at, "
        "last_observation_at = EXCLUDED.last_observation_at, "
        "last_material_change_at = EXCLUDED.last_material_change_at, lag_seconds = EXCLUDED.lag_seconds, "
        "reason_code = EXCLUDED.reason_code, detail = EXCLUDED.detail, cache_mode = EXCLUDED.cache_mode, "
        "consecutive_failures = EXCLUDED.consecutive_failures, updated_at = EXCLUDED.updated_at"
    )
    session.execute(statement, values)
    return {"source": source, **{column: row.get(column) for column in _COLUMNS}}


def record_collection_freshness(
    session: Any,
    *,
    source: str,
    source_config: Any,
    status: str,
    attempted_at: datetime,
    completed_at: datetime | None,
    records_fetched: int,
    cache_mode: str | None = None,
    reason_code: str | None = None,
    detail: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record one collection attempt using the caller's transaction."""
    _lock_source(session, source)
    old = _existing(session, source) or {}
    attempted = _utc(attempted_at)
    completed = _utc(completed_at)
    normalized_status = _status_value(status)
    cache_value = _status_value(cache_mode)
    is_failure = (
        normalized_status in _FAILURE_STATES | _RATE_LIMIT_STATES | _CACHE_STATES
        or cache_value in _CACHE_STATES
    )
    previous_failures = int(old.get("consecutive_failures") or 0)
    duplicate_attempt = _utc(old.get("last_attempt_at")) == attempted
    failures = (
        previous_failures
        if duplicate_attempt
        else (previous_failures + 1 if is_failure else 0)
    )
    success_at = old.get("last_success_at")
    if not is_failure and completed is not None:
        success_at = completed
    config_enabled = bool(_config_value(source_config, "enabled", True))
    schedule = _config_value(
        source_config,
        "schedule",
        _config_value(
            source_config, "cron", _config_value(source_config, "cron_schedule", None)
        ),
    )
    grace = float(_config_value(source_config, "freshness_grace_seconds", 300) or 0)
    classified = calculate_freshness_state(
        enabled=config_enabled,
        schedule=schedule,
        last_attempt_at=attempted,
        last_success_at=success_at,
        last_observation_at=old.get("last_observation_at"),
        last_material_change_at=old.get("last_material_change_at"),
        last_status=normalized_status,
        records_fetched=records_fetched,
        consecutive_failures=failures,
        cache_mode=cache_mode,
        reason_code=reason_code,
        detail=detail if detail is not None else old.get("detail") or {},
        now=now,
        grace_seconds=grace,
    )
    return _upsert(session, source, classified)


def _event_value(event: Any, name: str, default: Any = None) -> Any:
    if isinstance(event, Mapping):
        return event.get(name, default)
    return getattr(event, name, default)


def record_event_observation(session: Any, event: Any) -> dict[str, Any]:
    """Advance observation/material-change timestamps for a normalized event."""
    source = _event_value(event, "source")
    observed_at = _utc(_event_value(event, "observed_at"))
    if not isinstance(source, str) or not source.strip() or observed_at is None:
        raise ValueError("event source and observed_at are required")
    source = source.strip()
    _lock_source(session, source)
    old = _existing(session, source) or {}
    old_observation = _utc(old.get("last_observation_at"))
    old_material = _utc(old.get("last_material_change_at"))
    observation = max(filter(None, (old_observation, observed_at)), default=observed_at)
    metadata = _event_value(event, "metadata", {}) or {}
    marker = _event_value(event, "material_change", None)
    if marker is None and isinstance(metadata, Mapping):
        marker = metadata.get("material_change")
    material = old_material
    if marker is not False:
        material = max(filter(None, (old_material, observed_at)), default=observed_at)
    now = datetime.now(UTC)
    row = {column: old.get(column) for column in _COLUMNS}
    row.update(
        state=old.get("state") or "current",
        detail=old.get("detail") or {},
        consecutive_failures=int(old.get("consecutive_failures") or 0),
        last_observation_at=observation,
        last_material_change_at=material,
        updated_at=now,
    )
    if row["state"] == "never_run":
        row["state"] = "current"
    return _upsert(session, source, row)


def refresh_freshness_states(
    session: Any,
    source_configs: Mapping[str, Any],
    *,
    now: datetime | None = None,
    default_grace_seconds: float = 300.0,
    limit: int = 500,
) -> dict[str, int]:
    """Reclassify persisted rows as schedules advance without a collection."""
    current = _utc(now, default=datetime.now(UTC))
    assert current is not None
    result = session.execute(
        text("SELECT source FROM source_freshness_state ORDER BY source LIMIT :limit"),
        {"limit": max(1, min(500, int(limit)))},
    )
    mappings = getattr(result, "mappings", None)
    rows = mappings().all() if callable(mappings) else []
    checked = changed = 0
    for item in rows:
        source = str(item["source"])
        _lock_source(session, source)
        old = _existing(session, source)
        if old is None:
            continue
        checked += 1
        source_config = source_configs.get(source, {})
        enabled = bool(_config_value(source_config, "enabled", True))
        schedule = _config_value(
            source_config,
            "schedule",
            _config_value(
                source_config,
                "cron",
                _config_value(source_config, "cron_schedule", None),
            ),
        )
        grace = float(
            _config_value(
                source_config,
                "freshness_grace_seconds",
                default_grace_seconds,
            )
            or 0
        )
        old_state = _status_value(old.get("state"))
        last_status = (
            old_state
            if old_state in _FAILURE_STATES | _RATE_LIMIT_STATES | _CACHE_STATES
            else "success"
        )
        records_fetched = (
            0
            if old_state in {"expected_idle", "outside_schedule"}
            else int(old.get("last_observation_at") is not None)
        )
        classified = calculate_freshness_state(
            enabled=enabled,
            schedule=schedule,
            last_attempt_at=old.get("last_attempt_at"),
            last_success_at=old.get("last_success_at"),
            last_observation_at=old.get("last_observation_at"),
            last_material_change_at=old.get("last_material_change_at"),
            last_status=last_status,
            records_fetched=records_fetched,
            consecutive_failures=int(old.get("consecutive_failures") or 0),
            cache_mode=old.get("cache_mode"),
            reason_code=old.get("reason_code"),
            detail=old.get("detail") or {},
            now=current,
            grace_seconds=grace,
        )
        if (
            classified["state"] != old.get("state")
            or classified["expected_next_at"] != old.get("expected_next_at")
            or classified["lag_seconds"] != old.get("lag_seconds")
        ):
            _upsert(session, source, classified)
            changed += 1
    return {"checked": checked, "changed": changed}


def freshness_summary(session: Any) -> list[dict[str, Any]]:
    """Return source freshness rows without opening, committing, or closing a transaction."""
    result = session.execute(
        text("SELECT * FROM source_freshness_state ORDER BY source")
    )
    mappings = getattr(result, "mappings", None)
    if callable(mappings):
        return [dict(row) for row in mappings().all()]
    rows = getattr(result, "fetchall", lambda: [])()
    return [dict(getattr(row, "_mapping", row)) for row in rows]


__all__ = [
    "calculate_freshness_state",
    "record_collection_freshness",
    "record_event_observation",
    "refresh_freshness_states",
    "freshness_summary",
]
