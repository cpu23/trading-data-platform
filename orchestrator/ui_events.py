"""Caller-owned persistence helpers for UI invalidation events.

UI events are wakeups, not data transport.  Every helper therefore constructs
and validates the small allowlisted payload before touching the database and
never commits, rolls back, or closes the caller's transaction.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from contracts.db_results import result_first, result_rows

REPLAY_RETENTION_HOURS = 48
MAX_REPLAY_BATCH = 100
MAX_CLEANUP_BATCH = 100

EVENT_NAME_BY_SECTION = {
    "watchlist": "watchlist_changed",
    "source_health": "source_health_changed",
    "research_questions": "research_question_changed",
    "research_work_orders": "research_work_order_changed",
    "research_effects": "research_effect_recorded",
    "research_control_plane": "research_control_plane_changed",
    "system_topology": "system_topology_changed",
}
ALLOWED_EVENT_NAMES = frozenset((*EVENT_NAME_BY_SECTION.values(), "section_changed"))


def event_name_for_section(section_key: str) -> str:
    """Return the only event name allowed for a section identity."""
    _require_key(section_key, "section_key")
    return EVENT_NAME_BY_SECTION.get(section_key, "section_changed")


def _require_key(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value


def _require_version(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("section_version must be positive")
    try:
        version = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("section_version must be positive") from exc
    if version <= 0:
        raise ValueError("section_version must be positive")
    if isinstance(value, float) and value != version:
        raise ValueError("section_version must be positive")
    return version


def invalidation_payload(
    *, section_key: str, scope_key: str = "global", section_version: int
) -> dict[str, Any]:
    """Construct the exact, JSON-safe payload exposed to browsers."""
    return {
        "section_key": _require_key(section_key, "section_key"),
        "scope_key": _require_key(scope_key, "scope_key"),
        "version": _require_version(section_version),
    }


def _validate_event_payload(
    *,
    event_name: str,
    section_key: str,
    scope_key: str,
    section_version: int,
    payload: Any = None,
) -> dict[str, Any]:
    expected_name = event_name_for_section(section_key)
    if event_name != expected_name or event_name not in ALLOWED_EVENT_NAMES:
        raise ValueError("event name is not allowlisted for section")
    expected = invalidation_payload(
        section_key=section_key,
        scope_key=scope_key,
        section_version=section_version,
    )
    if payload is not None:
        if not isinstance(payload, Mapping) or dict(payload) != expected:
            raise ValueError(
                "payload contains values outside the invalidation allowlist"
            )
    return expected


def _json_payload(payload: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("payload is not JSON-safe") from exc


def append_ui_invalidation(
    session: Any,
    *,
    section_key: str,
    scope_key: str = "global",
    section_version: int,
    expires_at: datetime | None = None,
) -> Mapping[str, Any] | None:
    """Append one allowlisted invalidation using the caller's transaction."""
    event_name = event_name_for_section(section_key)
    payload = invalidation_payload(
        section_key=section_key,
        scope_key=scope_key,
        section_version=section_version,
    )
    expiry_sql = (
        ":expires_at"
        if expires_at is not None
        else "CURRENT_TIMESTAMP + INTERVAL '48 hours'"
    )
    result = session.execute(
        text(
            f"""INSERT INTO ui_events
               (event_name, section_key, scope_key, section_version, payload, expires_at)
               VALUES (:event_name, :section_key, :scope_key, :section_version,
                       CAST(:payload AS JSONB), {expiry_sql})
               RETURNING id, event_name, section_key, scope_key, section_version,
                         payload, created_at, expires_at"""
        ),
        {
            "event_name": event_name,
            "section_key": payload["section_key"],
            "scope_key": payload["scope_key"],
            "section_version": payload["version"],
            "payload": _json_payload(payload),
            "expires_at": expires_at,
        },
    )
    row = result_first(result)
    return parse_ui_event_row(row) if row is not None else None


def append_ui_invalidations(
    session: Any,
    sections: Iterable[str],
    *,
    scope_key: str = "global",
    expires_at: datetime | None = None,
) -> int:
    """Append at most one wakeup per allowlisted section in caller transaction."""
    bounded = sorted(set(sections) & EVENT_NAME_BY_SECTION.keys())[:8]
    if not bounded:
        return 0
    base_version = int(datetime.now(UTC).timestamp() * 1_000_000)
    published = 0
    for offset, section in enumerate(bounded):
        event = append_ui_invalidation(
            session,
            section_key=section,
            scope_key=scope_key,
            section_version=base_version + offset,
            expires_at=expires_at,
        )
        published += int(event is not None)
    return published


def append_ui_event(
    session: Any,
    *,
    event_name: str,
    section_key: str,
    scope_key: str = "global",
    section_version: int,
    payload: Mapping[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> Mapping[str, Any] | None:
    """Compatibility spelling that still permits only canonical invalidations."""
    _validate_event_payload(
        event_name=event_name,
        section_key=section_key,
        scope_key=scope_key,
        section_version=section_version,
        payload=payload,
    )
    return append_ui_invalidation(
        session,
        section_key=section_key,
        scope_key=scope_key,
        section_version=section_version,
        expires_at=expires_at,
    )



def parse_ui_event_row(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Parse only canonical fields; malformed or arbitrary rows are rejected."""
    if row is None:
        return None
    try:
        section_key = _require_key(row.get("section_key"), "section_key")
        scope_key = _require_key(row.get("scope_key", "global"), "scope_key")
        section_version = _require_version(row.get("section_version"))
        event_name = _require_key(row.get("event_name"), "event_name")
        raw_id = row.get("id")
        if isinstance(raw_id, bool):
            raise ValueError("id must be numeric")
        event_id = int(raw_id) if raw_id is not None else None
        if event_id is not None and event_id <= 0:
            raise ValueError("id must be positive")
        expected_payload = _validate_event_payload(
            event_name=event_name,
            section_key=section_key,
            scope_key=scope_key,
            section_version=section_version,
            payload=None,
        )
    except (TypeError, ValueError, OverflowError):
        return None
    parsed: dict[str, Any] = {
        "id": event_id,
        "event_name": event_name,
        "section_key": section_key,
        "scope_key": scope_key,
        "section_version": section_version,
        "payload": expected_payload,
    }
    for key in ("created_at", "expires_at"):
        if key in row:
            parsed[key] = row[key]
    return parsed


def list_ui_event_replay(
    session: Any, *, after_id: int | None = None, limit: int = MAX_REPLAY_BATCH
) -> list[dict[str, Any]]:
    """List retained replay rows in ascending id order, bounded to 100."""
    try:
        cursor = int(after_id or 0)
    except (TypeError, ValueError):
        cursor = 0
    bounded = max(1, min(MAX_REPLAY_BATCH, int(limit)))
    result = session.execute(
        text(
            """SELECT id, event_name, section_key, scope_key, section_version,
                      payload, created_at, expires_at
               FROM ui_events
               WHERE id > :after_id AND expires_at > CURRENT_TIMESTAMP
               ORDER BY id ASC
               LIMIT :limit"""
        ),
        {"after_id": cursor, "limit": bounded},
    )
    return [
        parsed
        for row in result_rows(result)
        if (parsed := parse_ui_event_row(row)) is not None
    ]


def get_ui_event_bounds(session: Any) -> tuple[int | None, int | None]:
    """Return retained replay min/max ids without exposing row payloads."""
    result = session.execute(
        text(
            """SELECT MIN(id) AS min_id, MAX(id) AS max_id
               FROM ui_events
               WHERE expires_at > CURRENT_TIMESTAMP"""
        )
    )
    row = result_first(result) or {}

    def _id(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    return _id(row.get("min_id")), _id(row.get("max_id"))


def delete_expired_ui_events(session: Any, *, limit: int = MAX_CLEANUP_BATCH) -> int:
    """Delete at most one bounded batch of expired rows; retain transaction ownership."""
    bounded = max(1, min(MAX_CLEANUP_BATCH, int(limit)))
    result = session.execute(
        text(
            """DELETE FROM ui_events
               WHERE id IN (
                   SELECT id FROM ui_events
                   WHERE expires_at <= CURRENT_TIMESTAMP
                   ORDER BY expires_at ASC, id ASC
                   LIMIT :limit
               )"""
        ),
        {"limit": bounded},
    )
    rowcount = getattr(result, "rowcount", 0)
    try:
        return max(0, int(rowcount))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "ALLOWED_EVENT_NAMES",
    "EVENT_NAME_BY_SECTION",
    "MAX_REPLAY_BATCH",
    "append_ui_event",
    "append_ui_invalidation",
    "append_ui_invalidations",
    "delete_expired_ui_events",
    "event_name_for_section",
    "get_ui_event_bounds",
    "invalidation_payload",
    "list_ui_event_replay",
    "parse_ui_event_row",
]
