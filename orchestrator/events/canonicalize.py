"""Deterministic event serialization, identity, and construction helpers."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, TypeAdapter

from .contracts import EntityRef, Horizon, MarketEvent, MarketEventType, MarketRef

_DATETIME_ADAPTER = TypeAdapter(datetime)


def canonical_json_value(value: Any, *, path: str = "value") -> Any:
    """Convert supported values to JSON primitives without lossy coercion."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"{path} contains a non-finite float")
        # JSON has one representation for zero; normalize signed zero as well.
        return 0.0 if value == 0 else value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise TypeError(f"{path} contains non-finite Decimal")
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise TypeError(f"{path} contains a naive datetime")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return canonical_json_value(value.value, path=path)
    if isinstance(value, BaseModel):
        return canonical_json_value(value.model_dump(mode="python"), path=path)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: canonical_json_value(
                getattr(value, field.name), path=f"{path}.{field.name}"
            )
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} has a non-string key")
            result[key] = canonical_json_value(item, path=f"{path}.{key}")
        return result
    if isinstance(value, Sequence) and not isinstance(
        value, (bytes, bytearray, memoryview)
    ):
        return [
            canonical_json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise TypeError(f"{path} contains non-JSON-compatible value {type(value).__name__}")


_json_value = canonical_json_value


def canonical_json(value: Any) -> str:
    """Return compact, sorted-key JSON with stable UUID and UTC datetime values."""
    return json.dumps(
        canonical_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def content_hash(value: Any) -> str:
    """Return the SHA-256 digest of a canonical JSON representation."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


canonical_fingerprint = content_hash


def dedupe_key(
    source: str,
    source_event_id: str | None = None,
    *,
    event_type: MarketEventType | str | None = None,
    identity: Mapping[str, Any] | None = None,
) -> str:
    """Build a stable source identity for the event ledger.

    A stable upstream ID is preferred.  Sources without one must provide
    explicit canonical identity fields; silently using an empty payload would
    collapse unrelated observations into one dedupe key.
    """
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source must be non-empty")
    source = source.strip()
    if source_event_id is not None:
        if not isinstance(source_event_id, str) or not source_event_id.strip():
            raise ValueError("source_event_id must be non-empty when provided")
        return f"{source}:{source_event_id.strip()}"
    if event_type is None:
        raise ValueError("event_type is required when source_event_id is absent")
    event_name = event_type.value if isinstance(event_type, Enum) else event_type
    if not isinstance(event_name, str) or not event_name.strip():
        raise ValueError("event_type must be non-empty")
    if not isinstance(identity, Mapping) or not identity:
        raise ValueError("identity must contain at least one canonical field")
    return f"{source}:{event_name.strip()}:{canonical_json(identity)}"


def _timestamp(value: datetime, name: str) -> datetime:
    try:
        parsed = _DATETIME_ADAPTER.validate_python(value)
    except (
        Exception
    ) as exc:  # pydantic gives a useful error only after model construction
        raise TypeError(f"{name} must be a datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def build_market_event(
    event_type: MarketEventType | str,
    source: str,
    observed_at: datetime,
    payload: dict[str, Any],
    *,
    source_event_id: str | None = None,
    source_payload_id: UUID | None = None,
    effective_at: datetime | None = None,
    published_at: datetime | None = None,
    ingested_at: datetime | None = None,
    revision_of_event_id: UUID | None = None,
    entities: Sequence[EntityRef | Mapping[str, Any]] = (),
    markets: Sequence[MarketRef | Mapping[str, Any]] = (),
    horizons: Sequence[Horizon | str] = (),
    importance_hint: float | None = None,
    metadata: dict[str, Any] | None = None,
    identity: Mapping[str, Any] | None = None,
    event_id: UUID | None = None,
    correlation_id: UUID | None = None,
) -> MarketEvent:
    """Construct a normalized event with UTC timestamps and derived identity."""
    observed = _timestamp(observed_at, "observed_at")
    effective = (
        _timestamp(effective_at, "effective_at") if effective_at is not None else None
    )
    published = (
        _timestamp(published_at, "published_at") if published_at is not None else None
    )
    ingested = (
        _timestamp(ingested_at, "ingested_at")
        if ingested_at is not None
        else datetime.now(UTC)
    )
    if not isinstance(payload, dict):
        raise TypeError("payload must be a mapping")
    metadata_value = {} if metadata is None else metadata
    if not isinstance(metadata_value, dict):
        raise TypeError("metadata must be a mapping")

    event_key = dedupe_key(
        source, source_event_id, event_type=event_type, identity=identity
    )
    # Volatile delivery identifiers are intentionally excluded from content
    # identity: the source observation, not its ingestion attempt, is hashed.
    hash_input = {
        "source": source,
        "source_event_id": source_event_id,
        "observed_at": observed,
        "effective_at": effective,
        "published_at": published,
        "entities": list(entities),
        "markets": list(markets),
        "horizons": list(horizons),
        "importance_hint": importance_hint,
        "payload": payload,
        "metadata": metadata_value,
    }
    digest = content_hash(hash_input)
    return MarketEvent(
        event_id=event_id or uuid4(),
        event_type=event_type,
        source=source,
        source_event_id=source_event_id,
        source_payload_id=source_payload_id,
        observed_at=observed,
        effective_at=effective,
        published_at=published,
        ingested_at=ingested,
        revision_of_event_id=revision_of_event_id,
        content_hash=digest,
        dedupe_key=event_key,
        entities=list(entities),
        markets=list(markets),
        horizons=list(horizons),
        importance_hint=importance_hint,
        payload=payload,
        metadata=metadata_value,
        correlation_id=correlation_id or uuid4(),
    )


__all__ = [
    "build_market_event",
    "canonical_fingerprint",
    "canonical_json",
    "canonical_json_value",
    "content_hash",
    "dedupe_key",
]
