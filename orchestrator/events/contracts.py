"""Strict domain contracts for normalized market events.

The event envelope is deliberately independent of persistence.  It is safe to
serialize with ``model_dump(mode="json")`` and keeps source observations
append-only by making revisions explicit links to earlier event IDs.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
MappingSource = Literal["source", "rule", "alias", "model", "manual"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        use_enum_values=True,
    )


class MarketEventType(StrEnum):
    PRICE_TICK = "price_tick"
    PRICE_BAR_CLOSED = "price_bar_closed"
    OPTION_CHAIN_PUBLISHED = "option_chain_published"
    CORPORATE_ACTION_PUBLISHED = "corporate_action_published"
    VOLATILITY_STATE_CHANGED = "volatility_state_changed"
    CORRELATION_STATE_CHANGED = "correlation_state_changed"
    MACRO_RELEASE = "macro_release"
    MACRO_REVISION = "macro_revision"
    CALENDAR_EVENT_CHANGED = "calendar_event_changed"
    HEADLINE_PUBLISHED = "headline_published"
    STORY_UPDATED = "story_updated"
    REGULATORY_FILING_PUBLISHED = "regulatory_filing_published"
    TRANSCRIPT_PUBLISHED = "transcript_published"
    FILING_INGESTED = "filing_ingested"
    CENTRAL_BANK_COMMUNICATION = "central_bank_communication"
    POSITIONING_REPORT_PUBLISHED = "positioning_report_published"
    SOURCE_FRESHNESS_CHANGED = "source_freshness_changed"
    MANUAL_RESEARCH_EVENT = "manual_research_event"


class Horizon(StrEnum):
    INTRADAY = "intraday"
    SWING = "swing"
    MEDIUM = "medium"
    LONG_TERM = "long_term"


class FreshnessState(StrEnum):
    CURRENT = "current"
    EXPECTED_IDLE = "expected_idle"
    OUTSIDE_SCHEDULE = "outside_schedule"
    STALE = "stale"
    CACHED_FALLBACK = "cached_fallback"
    RATE_LIMITED = "rate_limited"
    DELAYED = "delayed"
    FAILED = "failed"
    NEVER_RUN = "never_run"
    DISABLED = "disabled"


def _json_compatible(value: Any, *, path: str = "value") -> None:
    """Validate values accepted by our canonical JSON representation."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite float")
        return
    if isinstance(value, UUID):
        return
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{path} contains a naive datetime")
        return
    if isinstance(value, date):
        return
    if isinstance(value, StrEnum):
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _json_compatible(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} has a non-string key")
            _json_compatible(item, path=f"{path}.{key}")
        return
    raise ValueError(
        f"{path} contains non-JSON-compatible value {type(value).__name__}"
    )


class EntityRef(_StrictModel):
    entity_type: Literal[
        "instrument",
        "company",
        "country",
        "currency",
        "commodity",
        "sector",
        "industry",
        "central_bank",
        "government_body",
        "theme",
    ]
    canonical_id: NonBlankText
    display_name: NonBlankText
    confidence: float = Field(ge=0.0, le=1.0)
    mapping_source: MappingSource

    @field_validator("confidence", mode="before")
    @classmethod
    def _strict_confidence(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("confidence must be a finite number between 0 and 1")
        if not math.isfinite(float(value)):
            raise ValueError("confidence must be finite")
        return value


class MarketRef(_StrictModel):
    """A deterministic reference to a traded or otherwise monitored market."""

    canonical_id: NonBlankText
    display_name: NonBlankText
    asset_class: NonBlankText | None = None
    symbol: NonBlankText | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    mapping_source: MappingSource = "source"

    @field_validator("confidence", mode="before")
    @classmethod
    def _strict_confidence(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("confidence must be a finite number between 0 and 1")
        if not math.isfinite(float(value)):
            raise ValueError("confidence must be finite")
        return value


class MarketEvent(_StrictModel):
    schema_version: Literal[1] = 1
    event_id: UUID
    event_type: MarketEventType
    source: NonBlankText
    source_event_id: NonBlankText | None
    source_payload_id: UUID | None
    observed_at: datetime
    effective_at: datetime | None
    published_at: datetime | None
    ingested_at: datetime
    revision_of_event_id: UUID | None
    content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    dedupe_key: NonBlankText
    entities: list[EntityRef]
    markets: list[MarketRef]
    horizons: list[Horizon]
    importance_hint: float | None = Field(ge=0.0, le=1.0)
    payload: dict[str, Any]
    metadata: dict[str, Any]
    correlation_id: UUID

    @field_validator(
        "observed_at", "effective_at", "published_at", "ingested_at", mode="before"
    )
    @classmethod
    def _utc_datetime(cls, value: Any) -> Any:
        if value is None:
            return value
        if not isinstance(value, datetime):
            # Let Pydantic parse ISO input, then the after validator below checks awareness.
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("observed_at", "effective_at", "published_at", "ingested_at")
    @classmethod
    def _aware_datetime(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("timestamps must be timezone-aware")
            return value.astimezone(UTC)
        return value

    @field_validator("importance_hint", mode="before")
    @classmethod
    def _strict_importance(cls, value: Any) -> Any:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("importance_hint must be a finite number between 0 and 1")
        if not math.isfinite(float(value)):
            raise ValueError("importance_hint must be finite")
        return value

    @model_validator(mode="after")
    def _validate_json_fields(self) -> MarketEvent:
        _json_compatible(self.payload, path="payload")
        _json_compatible(self.metadata, path="metadata")
        return self


__all__ = [
    "EntityRef",
    "FreshnessState",
    "Horizon",
    "MarketEvent",
    "MarketEventType",
    "MarketRef",
    "MappingSource",
]
