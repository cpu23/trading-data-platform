"""Transactional, versioned publication of bounded analytical section snapshots.

The functions in this module deliberately do not commit. A caller owns the
transaction so a section publication can be committed with its producer work.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from contracts.db_results import result_first, result_rows

try:
    from .events.canonicalize import canonical_json, content_hash
except ImportError:  # pragma: no cover - legacy imports with orchestrator on sys.path
    from events.canonicalize import canonical_json, content_hash

MAX_SOURCE_EVENT_IDS = 256
MAX_RENDER_CONTEXT_BYTES = 16_384
MAX_HISTORY_LIMIT = 100
MAX_RECONCILE_ROWS = 500


@dataclass(frozen=True)
class SnapshotPublicationResult:
    snapshot_id: Any | None
    section_key: str
    scope_key: str
    version: int
    content_hash: str
    changed: bool


class SnapshotValidationError(ValueError):
    """Raised when a candidate snapshot does not pass its validator."""


def _dialect_name(session: Any) -> str | None:
    bind = getattr(session, "bind", None)
    dialect = getattr(bind, "dialect", None)
    name = getattr(dialect, "name", None)
    return str(name).lower() if name else None


def _execute(
    session: Any, statement: str, params: Mapping[str, Any] | None = None
) -> Any:
    return session.execute(text(statement), dict(params or {}))


def _normalise_payload(payload: Any) -> dict[str, Any] | list[Any]:
    if not isinstance(payload, (Mapping, list)):
        raise TypeError("snapshot payload must be a JSON object or array")
    try:
        normalised = json.loads(canonical_json(payload))
    except (TypeError, ValueError, OverflowError):
        raise TypeError("snapshot payload is not JSON-compatible") from None
    if not isinstance(normalised, (dict, list)):
        raise TypeError("snapshot payload must be a JSON object or array")
    return normalised


def _normalise_context(render_context: Any) -> Any:
    if render_context is None:
        return None
    if not isinstance(render_context, Mapping):
        raise TypeError("render context must be a JSON object")
    try:
        encoded = canonical_json(render_context)
    except (TypeError, ValueError, OverflowError):
        raise TypeError("render context is not JSON-compatible") from None
    if len(encoded.encode("utf-8")) > MAX_RENDER_CONTEXT_BYTES:
        raise ValueError("render context exceeds the supported bound")
    return json.loads(encoded)


def _normalise_event_ids(source_event_ids: Sequence[UUID | str] | None) -> list[UUID]:
    if source_event_ids is None:
        return []
    values = list(source_event_ids)
    if len(values) > MAX_SOURCE_EVENT_IDS:
        values = values[-MAX_SOURCE_EVENT_IDS:]
    result: list[UUID] = []
    for value in values:
        try:
            result.append(value if isinstance(value, UUID) else UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            raise TypeError("source event IDs must be UUID values") from None
    return result


def _normalise_timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("snapshot timestamps must include a timezone")
    return value.astimezone(UTC)


def _lock_section(session: Any, section_key: str, scope_key: str) -> None:
    """Take a transaction-scoped PostgreSQL lock; skip it for SQLite fakes."""
    dialect = _dialect_name(session)
    if dialect not in (None, "postgresql", "postgres"):
        return
    try:
        _execute(
            session,
            "SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))",
            {"lock_key": f"section_snapshot:{section_key}:{scope_key}"},
        )
    except Exception:
        if dialect in ("postgresql", "postgres"):
            raise


def _current_row(
    session: Any, section_key: str, scope_key: str
) -> Mapping[str, Any] | None:
    result = _execute(
        session,
        """SELECT * FROM section_snapshots
           WHERE section_key = :section_key AND scope_key = :scope_key
             AND status = 'published'
           ORDER BY version DESC LIMIT 1""",
        {"section_key": section_key, "scope_key": scope_key},
    )
    return result_first(result)


def _result(
    row: Mapping[str, Any] | None,
    *,
    section_key: str,
    scope_key: str,
    version: int,
    digest: str,
    changed: bool,
) -> SnapshotPublicationResult:
    return SnapshotPublicationResult(
        snapshot_id=row.get("id") if row else None,
        section_key=str(row.get("section_key", section_key)) if row else section_key,
        scope_key=str(row.get("scope_key", scope_key)) if row else scope_key,
        version=int(row.get("version", version)) if row else version,
        content_hash=str(row.get("content_hash", digest)) if row else digest,
        changed=changed,
    )


def _validate(validator: Callable[[Any], Any] | None, payload: Any) -> None:
    if validator is None:
        return
    try:
        valid = validator(payload)
    except Exception:
        raise SnapshotValidationError("snapshot validation failed") from None
    if valid is False:
        raise SnapshotValidationError("snapshot validation failed")


def publish_section_snapshot(
    session: Any,
    *,
    section_key: str,
    payload: Any,
    scope_key: str = "global",
    render_context: Any = None,
    source_event_ids: Sequence[UUID | str] = (),
    data_freshness_at: datetime | None = None,
    analysis_freshness_at: datetime | None = None,
    validator: Callable[[Any], Any] | None = None,
) -> SnapshotPublicationResult:
    """Publish a section version while retaining the caller's transaction."""
    if not isinstance(section_key, str) or not section_key.strip():
        raise ValueError("section key is required")
    if not isinstance(scope_key, str) or not scope_key.strip():
        raise ValueError("scope key is required")

    normalised_payload = _normalise_payload(payload)
    digest = content_hash(normalised_payload)
    normalised_context = _normalise_context(render_context)
    event_ids = _normalise_event_ids(source_event_ids)
    data_at = _normalise_timestamp(data_freshness_at)
    analysis_at = _normalise_timestamp(analysis_freshness_at)

    _lock_section(session, section_key, scope_key)
    current = _current_row(session, section_key, scope_key)
    if current is not None and str(current.get("content_hash", "")) == digest:
        return _result(
            current,
            section_key=section_key,
            scope_key=scope_key,
            version=int(current.get("version", 1)),
            digest=digest,
            changed=False,
        )

    # No database mutation occurs before validation.
    _validate(validator, normalised_payload)

    version_mapping = (
        result_first(
            _execute(
                session,
                """SELECT COALESCE(MAX(version), 0) AS max_version
               FROM section_snapshots
               WHERE section_key = :section_key AND scope_key = :scope_key""",
                {"section_key": section_key, "scope_key": scope_key},
            )
        )
        or {}
    )
    try:
        version = int(version_mapping.get("max_version", 0) or 0) + 1
    except (TypeError, ValueError):
        version = 1

    old_id = current.get("id") if current is not None else None
    if old_id is not None:
        _execute(
            session,
            """UPDATE section_snapshots
               SET status = 'superseded'
               WHERE id = :snapshot_id AND status = 'published'""",
            {"snapshot_id": old_id},
        )

    json_cast = (
        "CAST(:payload AS JSONB)"
        if _dialect_name(session) in (None, "postgresql", "postgres")
        else ":payload"
    )
    context_cast = (
        "CAST(:render_context AS JSONB)"
        if _dialect_name(session) in (None, "postgresql", "postgres")
        else ":render_context"
    )
    row = result_first(
        _execute(
            session,
            f"""INSERT INTO section_snapshots
               (section_key, scope_key, version, status, payload, render_context,
                content_hash, data_freshness_at, analysis_freshness_at,
                source_event_ids, published_at, supersedes_snapshot_id)
               VALUES (:section_key, :scope_key, :version, 'published',
                       {json_cast}, {context_cast}, :content_hash,
                       :data_freshness_at, :analysis_freshness_at,
                       :source_event_ids, CURRENT_TIMESTAMP, :supersedes_snapshot_id)
               RETURNING *""",
            {
                "section_key": section_key,
                "scope_key": scope_key,
                "version": version,
                "payload": canonical_json(normalised_payload),
                "render_context": canonical_json(normalised_context)
                if normalised_context is not None
                else None,
                "content_hash": digest,
                "data_freshness_at": data_at,
                "analysis_freshness_at": analysis_at,
                "source_event_ids": event_ids,
                "supersedes_snapshot_id": old_id,
            },
        )
    )
    inserted = row
    if inserted is None:
        inserted = {
            "id": None,
            "section_key": section_key,
            "scope_key": scope_key,
            "version": version,
            "content_hash": digest,
        }
    return _result(
        inserted,
        section_key=section_key,
        scope_key=scope_key,
        version=version,
        digest=digest,
        changed=True,
    )


def get_current_snapshot(
    session: Any, *, section_key: str, scope_key: str = "global"
) -> Mapping[str, Any] | None:
    """Return the current published snapshot without changing the transaction."""
    return _current_row(session, section_key, scope_key)


def list_snapshot_history(
    session: Any,
    *,
    section_key: str,
    scope_key: str = "global",
    limit: int = MAX_HISTORY_LIMIT,
    before_version: int | None = None,
) -> list[Mapping[str, Any]]:
    """Return newest-first bounded history for one section and scope."""
    try:
        bounded_limit = max(1, min(int(limit), MAX_HISTORY_LIMIT))
    except (TypeError, ValueError):
        bounded_limit = MAX_HISTORY_LIMIT
    clauses = ["section_key = :section_key", "scope_key = :scope_key"]
    params: dict[str, Any] = {
        "section_key": section_key,
        "scope_key": scope_key,
        "limit": bounded_limit,
    }
    if before_version is not None:
        clauses.append("version < :before_version")
        params["before_version"] = int(before_version)
    result = _execute(
        session,
        f"""SELECT * FROM section_snapshots WHERE {" AND ".join(clauses)}
            ORDER BY version DESC LIMIT :limit""",
        params,
    )
    return result_rows(result)


def reconcile_snapshots(
    session: Any,
    *,
    section_key: str | None = None,
    scope_key: str | None = None,
    limit: int = MAX_RECONCILE_ROWS,
) -> dict[str, int]:
    """Repair derivable metadata only; payloads are never fabricated."""
    try:
        bounded_limit = max(1, min(int(limit), MAX_RECONCILE_ROWS))
    except (TypeError, ValueError):
        bounded_limit = MAX_RECONCILE_ROWS
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": bounded_limit}
    if section_key is not None:
        clauses.append("section_key = :section_key")
        params["section_key"] = section_key
    if scope_key is not None:
        clauses.append("scope_key = :scope_key")
        params["scope_key"] = scope_key
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    result = _execute(
        session,
        f"""SELECT * FROM section_snapshots {where}
            ORDER BY section_key, scope_key, version DESC LIMIT :limit""",
        params,
    )
    rows = result_rows(result)
    repaired = 0
    skipped = 0
    published: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in rows:
        key = (str(row.get("section_key", "")), str(row.get("scope_key", "global")))
        if row.get("status") == "published":
            if key in published and row.get("id") is not None:
                try:
                    _execute(
                        session,
                        """UPDATE section_snapshots SET status = 'superseded'
                           WHERE id = :snapshot_id AND status = 'published'""",
                        {"snapshot_id": row["id"]},
                    )
                    repaired += 1
                except Exception:
                    skipped += 1
            else:
                published[key] = row
        payload = row.get("payload")
        if payload is not None:
            try:
                digest = content_hash(_normalise_payload(payload))
            except (TypeError, ValueError, OverflowError):
                skipped += 1
            else:
                if digest != row.get("content_hash") and row.get("id") is not None:
                    _execute(
                        session,
                        "UPDATE section_snapshots SET content_hash = :content_hash WHERE id = :snapshot_id",
                        {"content_hash": digest, "snapshot_id": row["id"]},
                    )
                    repaired += 1
        if (
            row.get("status") in ("published", "superseded")
            and row.get("published_at") is None
            and row.get("id") is not None
        ):
            _execute(
                session,
                """UPDATE section_snapshots SET published_at = COALESCE(created_at, CURRENT_TIMESTAMP)
                   WHERE id = :snapshot_id AND status IN ('published', 'superseded')""",
                {"snapshot_id": row["id"]},
            )
            repaired += 1
        elif (
            row.get("status") in ("draft", "failed")
            and row.get("published_at") is not None
            and row.get("id") is not None
        ):
            _execute(
                session,
                """UPDATE section_snapshots SET published_at = NULL
                   WHERE id = :snapshot_id AND status IN ('draft', 'failed')""",
                {"snapshot_id": row["id"]},
            )
            repaired += 1
    return {"checked": len(rows), "repaired": repaired, "skipped": skipped}


__all__ = [
    "SnapshotPublicationResult",
    "SnapshotValidationError",
    "publish_section_snapshot",
    "get_current_snapshot",
    "list_snapshot_history",
    "reconcile_snapshots",
]
