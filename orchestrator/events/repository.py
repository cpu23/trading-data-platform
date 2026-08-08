"""Transactional persistence for the market-event ledger and outbox.

All functions in this module use the caller's SQLAlchemy session and never commit.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text

from db import upsert_records_in_session

from .canonicalize import canonical_json
from .contracts import MarketEvent

TOPIC = "market_event"


@dataclass(frozen=True)
class EventInsertResult:
    event: MarketEvent
    inserted: bool
    outbox_inserted: bool = False


@dataclass(frozen=True)
class OutboxClaim:
    id: int
    event_id: UUID
    topic: str
    attempt_count: int
    claimed_by: str
    event: MarketEvent | None = None


def _value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return canonical_json(value)
    if isinstance(value, UUID):
        return str(value)
    return value


def _event_record(event: MarketEvent) -> dict[str, Any]:
    return {
        "id": event.event_id,
        "schema_version": event.schema_version,
        "event_type": event.event_type
        if isinstance(event.event_type, str)
        else event.event_type.value,
        "source": event.source,
        "source_event_id": event.source_event_id,
        "source_payload_id": event.source_payload_id,
        "observed_at": event.observed_at,
        "effective_at": event.effective_at,
        "published_at": event.published_at,
        "ingested_at": event.ingested_at,
        "revision_of_event_id": event.revision_of_event_id,
        "content_hash": event.content_hash,
        "dedupe_key": event.dedupe_key,
        "entities": _value([item.model_dump(mode="json") for item in event.entities]),
        "markets": _value([item.model_dump(mode="json") for item in event.markets]),
        "horizons": [
            item if isinstance(item, str) else item.value for item in event.horizons
        ],
        "importance_hint": event.importance_hint,
        "payload": _value(event.payload),
        "metadata": _value(event.metadata),
        "correlation_id": event.correlation_id,
    }


def _event_from_row(row: Mapping[str, Any]) -> MarketEvent:
    def parse_json(value: Any, default: Any) -> Any:
        if value is None:
            return default
        if isinstance(value, str):
            import json

            return json.loads(value)
        return value

    from .contracts import MarketEventType

    return MarketEvent(
        schema_version=row.get("schema_version", 1),
        event_id=row.get("id", row.get("event_id")),
        event_type=MarketEventType(row["event_type"]),
        source=row["source"],
        source_event_id=row.get("source_event_id"),
        source_payload_id=row.get("source_payload_id"),
        observed_at=row["observed_at"],
        effective_at=row.get("effective_at"),
        published_at=row.get("published_at"),
        ingested_at=row["ingested_at"],
        revision_of_event_id=row.get("revision_of_event_id"),
        content_hash=row["content_hash"],
        dedupe_key=row["dedupe_key"],
        entities=parse_json(row.get("entities"), []),
        markets=parse_json(row.get("markets"), []),
        horizons=parse_json(row.get("horizons"), []),
        importance_hint=row.get("importance_hint"),
        payload=parse_json(row.get("payload"), {}),
        metadata=parse_json(row.get("metadata"), {}),
        correlation_id=row["correlation_id"],
    )


def find_latest_event(
    session: Any, *, source: str, dedupe_key: str
) -> MarketEvent | None:
    """Return the newest event for a stable identity, if one exists."""
    row = (
        session.execute(
            text(
                "SELECT * FROM market_events WHERE source = :source AND dedupe_key = :dedupe_key "
                "ORDER BY ingested_at DESC, created_at DESC, id DESC LIMIT 1"
            ),
            {"source": source, "dedupe_key": dedupe_key},
        )
        .mappings()
        .first()
    )
    return _event_from_row(row) if row is not None else None


def insert_event(
    session: Any,
    event: MarketEvent,
    *,
    topic: str = TOPIC,
    link_revision: bool = True,
) -> EventInsertResult:
    """Insert one immutable event and its unique outbox handoff.

    The identity constraint makes this safe for repeated deliveries. A changed
    content hash under the same stable identity is linked to the prior event.
    """
    previous = None
    if link_revision and event.revision_of_event_id is None:
        previous = find_latest_event(
            session, source=event.source, dedupe_key=event.dedupe_key
        )
        if previous is not None and previous.content_hash != event.content_hash:
            event = event.model_copy(update={"revision_of_event_id": previous.event_id})

    record = _event_record(event)
    columns = ", ".join(record)
    params = {key: value for key, value in record.items()}
    statement = text(
        f"INSERT INTO market_events ({columns}) VALUES ({', '.join(':' + key for key in record)}) "
        "ON CONFLICT (source, dedupe_key, content_hash) DO NOTHING RETURNING id"
    )
    result = session.execute(statement, params)
    inserted_row = result.first()
    inserted = inserted_row is not None
    outbox_inserted = False
    if inserted:
        outbox = session.execute(
            text(
                "INSERT INTO event_outbox (event_id, topic) VALUES (:event_id, :topic) "
                "ON CONFLICT (event_id, topic) DO NOTHING RETURNING id"
            ),
            {"event_id": event.event_id, "topic": topic},
        ).first()
        outbox_inserted = outbox is not None
    return EventInsertResult(
        event=event, inserted=inserted, outbox_inserted=outbox_inserted
    )


def upsert_raw(
    session: Any,
    table_name: str,
    record: Mapping[str, Any],
    conflict_columns: list[str],
) -> int:
    """Upsert one raw observation while retaining the caller's transaction."""
    return upsert_records_in_session(
        session, table_name, [dict(record)], conflict_columns
    )


def _supports_skip_locked(session: Any) -> bool:
    try:
        return session.get_bind().dialect.name not in {"sqlite"}
    except Exception:
        return True


def claim_outbox(
    session: Any,
    worker_id: str,
    limit: int = 25,
    lease_seconds: float = 30.0,
) -> list[OutboxClaim]:
    """Atomically lease eligible rows; expired leases are eligible again."""
    now = datetime.now(UTC)
    expired = now - timedelta(seconds=max(1.0, float(lease_seconds)))
    lock = " FOR UPDATE SKIP LOCKED" if _supports_skip_locked(session) else ""
    rows = (
        session.execute(
            text(
                "SELECT id, event_id, topic, attempt_count FROM event_outbox "
                "WHERE completed_at IS NULL AND failed_at IS NULL "
                "AND available_at <= :now AND (claimed_at IS NULL OR claimed_at <= :expired) "
                "ORDER BY id LIMIT :limit" + lock
            ),
            {"now": now, "expired": expired, "limit": max(1, int(limit))},
        )
        .mappings()
        .all()
    )
    claims: list[OutboxClaim] = []
    for row in rows:
        updated = (
            session.execute(
                text(
                    "UPDATE event_outbox SET claimed_at = :now, claimed_by = :worker, "
                    "attempt_count = attempt_count + 1 WHERE id = :id "
                    "AND completed_at IS NULL AND failed_at IS NULL "
                    "AND (claimed_at IS NULL OR claimed_at <= :expired) RETURNING id, event_id, topic, attempt_count"
                ),
                {"now": now, "worker": worker_id, "expired": expired, "id": row["id"]},
            )
            .mappings()
            .first()
        )
        if updated is not None:
            claims.append(
                OutboxClaim(
                    id=updated["id"],
                    event_id=updated["event_id"],
                    topic=updated["topic"],
                    attempt_count=updated["attempt_count"],
                    claimed_by=worker_id,
                )
            )
    return claims


def complete_outbox(session: Any, outbox_id: int, worker_id: str) -> bool:
    result = session.execute(
        text(
            "UPDATE event_outbox SET completed_at = :now, claimed_at = NULL, claimed_by = NULL "
            "WHERE id = :id AND claimed_by = :worker AND completed_at IS NULL AND failed_at IS NULL"
        ),
        {"id": outbox_id, "worker": worker_id, "now": datetime.now(UTC)},
    )
    return bool(result.rowcount)


def retry_outbox(
    session: Any,
    outbox_id: int,
    worker_id: str,
    available_at: datetime,
    error: str | Exception | None = None,
) -> bool:
    result = session.execute(
        text(
            "UPDATE event_outbox SET available_at = :available_at, claimed_at = NULL, claimed_by = NULL, "
            "last_error = :error WHERE id = :id AND claimed_by = :worker "
            "AND completed_at IS NULL AND failed_at IS NULL"
        ),
        {
            "id": outbox_id,
            "worker": worker_id,
            "available_at": available_at,
            "error": sanitize_error(error),
        },
    )
    return bool(result.rowcount)


def terminal_fail_outbox(
    session: Any,
    outbox_id: int,
    worker_id: str,
    error: str | Exception | None = None,
) -> bool:
    result = session.execute(
        text(
            "UPDATE event_outbox SET failed_at = :now, claimed_at = NULL, claimed_by = NULL, "
            "last_error = :error WHERE id = :id AND claimed_by = :worker "
            "AND completed_at IS NULL AND failed_at IS NULL"
        ),
        {
            "id": outbox_id,
            "worker": worker_id,
            "error": sanitize_error(error),
            "now": datetime.now(UTC),
        },
    )
    return bool(result.rowcount)


def sanitize_error(error: str | Exception | None) -> str | None:
    if error is None:
        return None
    if isinstance(error, BaseException):
        return type(error).__name__[:200]
    text_value = " ".join(str(error).split())
    return text_value[:500] or "error"


def operations_summary(
    session: Any,
    *,
    recent_limit: int = 20,
    lease_seconds: float = 120.0,
) -> dict[str, Any]:
    expired = datetime.now(UTC) - timedelta(seconds=max(1.0, float(lease_seconds)))
    row = (
        session.execute(
            text(
                "SELECT COUNT(*) FILTER (WHERE completed_at IS NULL AND failed_at IS NULL "
                "AND (claimed_at IS NULL OR claimed_at <= :expired)) AS pending, "
                "COUNT(*) FILTER (WHERE completed_at IS NULL AND failed_at IS NULL "
                "AND claimed_at > :expired) AS claimed, "
                "COUNT(*) FILTER (WHERE completed_at IS NULL AND failed_at IS NULL "
                "AND claimed_at IS NOT NULL AND claimed_at <= :expired) AS expired, "
                "COUNT(*) FILTER (WHERE failed_at IS NOT NULL) AS failed, "
                "MIN(created_at) FILTER (WHERE completed_at IS NULL AND failed_at IS NULL) AS oldest, "
                "COALESCE(SUM(attempt_count), 0) AS attempts FROM event_outbox"
            ),
            {"expired": expired},
        )
        .mappings()
        .one()
    )
    recent = (
        session.execute(
            text(
                "SELECT id, event_type, source, observed_at, ingested_at FROM market_events ORDER BY ingested_at DESC LIMIT :limit"
            ),
            {"limit": max(1, int(recent_limit))},
        )
        .mappings()
        .all()
    )
    return {
        "pending": int(row["pending"] or 0),
        "claimed": int(row["claimed"] or 0),
        "expired": int(row["expired"] or 0),
        "failed": int(row["failed"] or 0),
        "oldest": row["oldest"],
        "attempts": int(row["attempts"] or 0),
        "events_recent": [dict(item) for item in recent],
    }


# Concise aliases used by integrations.
insert_market_event = insert_event
claim_outbox_rows = claim_outbox
complete_outbox_row = complete_outbox
retry_outbox_row = retry_outbox
terminal_fail_outbox_row = terminal_fail_outbox

__all__ = [
    "TOPIC",
    "EventInsertResult",
    "OutboxClaim",
    "claim_outbox",
    "claim_outbox_rows",
    "complete_outbox",
    "complete_outbox_row",
    "find_latest_event",
    "insert_event",
    "insert_market_event",
    "operations_summary",
    "retry_outbox",
    "retry_outbox_row",
    "terminal_fail_outbox",
    "terminal_fail_outbox_row",
    "sanitize_error",
    "upsert_raw",
]
