"""Normalized market event contracts and deterministic identity helpers."""

from .canonicalize import build_market_event, canonical_json, content_hash, dedupe_key
from .contracts import (
    EntityRef,
    FreshnessState,
    Horizon,
    MarketEvent,
    MarketEventType,
    MarketRef,
)

__all__ = [
    "EntityRef",
    "FreshnessState",
    "Horizon",
    "MarketEvent",
    "MarketEventType",
    "MarketRef",
    "build_market_event",
    "canonical_json",
    "content_hash",
    "dedupe_key",
]
from .publisher import (
    PublicationResult,
    event_pipeline_summary,
    publish_collector_records_atomic,
)
from .repository import (
    claim_outbox,
    complete_outbox,
    insert_event,
    operations_summary,
    retry_outbox,
    terminal_fail_outbox,
)
from .worker import outbox_worker

__all__ += [
    "PublicationResult",
    "claim_outbox",
    "complete_outbox",
    "event_pipeline_summary",
    "insert_event",
    "operations_summary",
    "outbox_worker",
    "publish_collector_records_atomic",
    "retry_outbox",
    "terminal_fail_outbox",
]
