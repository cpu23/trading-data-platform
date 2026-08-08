"""Source-specific raw observation to market-event publication adapters."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import islice
from typing import Any
from uuid import UUID

from db import get_session

from .canonicalize import build_market_event, canonical_json, content_hash
from .contracts import MarketEventType, MarketRef
from .repository import (
    TOPIC,
    EventInsertResult,
    find_latest_event,
    insert_event,
    operations_summary,
    upsert_raw,
)


@dataclass(frozen=True)
class PublicationResult:
    attempted: int
    raw_written: int
    events_inserted: int
    events_deduplicated: int
    outbox_inserted: int
    errors: tuple[str, ...] = ()

    @property
    def written(self) -> int:
        return self.raw_written

    @property
    def status(self) -> str:
        return (
            "success"
            if self.raw_written == self.attempted
            else ("failed" if self.raw_written == 0 else "partial")
        )

    @property
    def success(self) -> bool:
        return self.status == "success"


def _utc(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return result.astimezone(UTC)


def _source(record: Mapping[str, Any], source: str | None) -> str:
    return str(source or record.get("source") or "").strip().lower()


def _fred(record: Mapping[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    series_id = str(record.get("series_id") or "").strip()
    observed = _utc(record.get("observed_at"), "observed_at")
    if not series_id:
        raise ValueError("FRED record requires series_id")
    raw = dict(record)
    raw["source"] = "fred"
    payload = {
        "series_id": series_id,
        "observed_at": observed,
        "value": record.get("value"),
        "released_at": record.get("released_at"),
        "revision_at": record.get("revision_at"),
        "metadata": record.get("metadata") or {},
    }
    return f"{series_id}:{observed.isoformat()}", raw, payload


def _oanda(record: Mapping[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    symbol = str(record.get("symbol") or "").strip()
    timeframe = str(record.get("timeframe") or "").strip()
    timestamp = _utc(record.get("timestamp"), "timestamp")
    if not symbol or not timeframe:
        raise ValueError("OANDA record requires symbol and timeframe")
    raw = dict(record)
    raw["source"] = "oanda"
    payload = {
        key: record.get(key)
        for key in (
            "symbol",
            "timeframe",
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
        )
    }
    return f"{symbol}:{timeframe}:{timestamp.isoformat()}", raw, payload


def map_record(
    record: Mapping[str, Any], *, source: str | None = None
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    src = _source(record, source)
    if src == "fred":
        return _fred(record)
    if src == "oanda":
        return _oanda(record)
    raise ValueError("unsupported event publication source")


def publish_record(
    session: Any,
    record: Mapping[str, Any],
    *,
    source: str | None = None,
    correlation_id: UUID | str | None = None,
) -> EventInsertResult:
    src = _source(record, source)
    source_event_id, raw, payload = map_record(record, source=src)
    previous = None
    if src == "fred":
        previous = find_latest_event(
            session, source=src, dedupe_key=f"{src}:{source_event_id}"
        )
        event_type = (
            MarketEventType.MACRO_REVISION
            if previous is not None
            else MarketEventType.MACRO_RELEASE
        )
        table, conflict, observed, markets = (
            "macro_series",
            ["series_id", "observed_at"],
            _utc(raw["observed_at"], "observed_at"),
            [],
        )
    else:
        event_type = MarketEventType.PRICE_TICK
        table, conflict, observed = (
            "market_data",
            ["symbol", "timeframe", "timestamp"],
            _utc(raw["timestamp"], "timestamp"),
        )
        markets = [
            MarketRef(
                canonical_id=f"oanda:{raw['symbol']}",
                display_name=str(raw["symbol"]),
                asset_class="fx",
                symbol=str(raw["symbol"]),
            )
        ]
    upsert_raw(session, table, raw, conflict)
    if previous is not None and canonical_json(previous.payload) == canonical_json(
        payload
    ):
        return EventInsertResult(event=previous, inserted=False, outbox_inserted=False)
    event = build_market_event(
        event_type,
        src,
        observed,
        payload,
        source_event_id=source_event_id,
        effective_at=observed,
        entities=(),
        markets=markets,
        metadata={"raw_table": table},
        identity={"source_event_id": source_event_id},
        revision_of_event_id=previous.event_id if previous is not None else None,
        correlation_id=UUID(str(correlation_id))
        if correlation_id is not None
        else None,
    )
    return insert_event(session, event, topic=TOPIC)


def publish_records(
    source: str,
    records: Iterable[Mapping[str, Any]],
    *,
    config: Any = None,
    session: Any = None,
    correlation_id: UUID | str | None = None,
) -> PublicationResult:
    values = list(records)
    raw_written = events_inserted = events_deduplicated = outbox_inserted = 0
    errors: list[str] = []

    def run(active_session: Any) -> None:
        nonlocal raw_written, events_inserted, events_deduplicated, outbox_inserted
        for record in values:
            try:
                result = publish_record(
                    active_session, record, source=source, correlation_id=correlation_id
                )
            except Exception as exc:
                errors.append(type(exc).__name__)
                raise
            raw_written += 1
            if result.inserted:
                events_inserted += 1
                outbox_inserted += int(result.outbox_inserted)
            else:
                events_deduplicated += 1

    if session is not None:
        run(session)
    else:
        with get_session(config) as active_session:
            run(active_session)
    return PublicationResult(
        len(values),
        raw_written,
        events_inserted,
        events_deduplicated,
        outbox_inserted,
        tuple(errors),
    )


def publish_collector_records_atomic(
    *,
    source_id: str,
    table_name: str,
    records: Iterable[Mapping[str, Any]],
    conflict_columns: list[str],
    correlation_id: UUID | str | None = None,
    config: Any = None,
) -> PublicationResult:
    source = str(source_id).strip().lower()
    expected = {"fred": "macro_series", "oanda": "market_data"}
    if expected.get(source) != table_name:
        raise ValueError("unsupported source/table event publication pair")
    expected_conflicts = (
        ["series_id", "observed_at"]
        if source == "fred"
        else ["symbol", "timeframe", "timestamp"]
    )
    if list(conflict_columns) != expected_conflicts:
        raise ValueError("unsupported source conflict columns")
    return publish_records(
        source, records, config=config, correlation_id=correlation_id
    )


def _bounded_news_text(value: Any, maximum: int) -> str:
    return str(value or "").strip()[:maximum]


def publish_news_record(
    session: Any,
    record: Mapping[str, Any],
    *,
    correlation_id: UUID | str | None = None,
) -> EventInsertResult:
    """Publish one durable-feed item without duplicating its raw payload."""
    source = _bounded_news_text(record.get("source"), 64).lower()
    source_event_id = _bounded_news_text(record.get("id"), 500)
    title = _bounded_news_text(record.get("title"), 500)
    if source not in {"reuters", "kobeissi"}:
        raise ValueError("unsupported news publication source")
    if not source_event_id or not title:
        raise ValueError("news item id and title are required")
    published = _utc(record.get("published"), "published")
    symbols = []
    for raw in (
        record.get("symbols", []) if isinstance(record.get("symbols"), list) else []
    ):
        symbol = _bounded_news_text(raw, 32).upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    tags = []
    for raw in record.get("tags", []) if isinstance(record.get("tags"), list) else []:
        tag = _bounded_news_text(raw, 80).lower()
        if tag and tag not in tags:
            tags.append(tag)
    payload = {
        "id": source_event_id,
        "source": source,
        "source_label": _bounded_news_text(record.get("source_label") or source, 100),
        "title": title,
        "summary": _bounded_news_text(record.get("summary"), 2000) or None,
        "url": _bounded_news_text(record.get("url"), 2000) or None,
        "published": published.isoformat(),
        "symbols": symbols[:100],
        "tags": tags[:50],
    }
    iso_year, iso_week, _ = published.isocalendar()
    cache_key = f"news:{source}:{content_hash({'id': source_event_id})}"
    upsert_raw(
        session,
        "source_payload_cache",
        {
            "cache_key": cache_key,
            "source": source,
            "target_week": f"{iso_year}-W{iso_week:02d}",
            "raw_payload": payload,
            "payload_hash": content_hash(payload),
            "fetched_at": datetime.now(UTC),
            "period_start": published,
            "period_end": published,
            "metadata": {"payload_contract": "news_headline_v1"},
        },
        ["cache_key"],
    )
    prior = find_latest_event(
        session, source=source, dedupe_key=f"{source}:{source_event_id}"
    )
    if prior is not None and canonical_json(prior.payload) == canonical_json(payload):
        return EventInsertResult(event=prior, inserted=False, outbox_inserted=False)
    markets = [
        MarketRef(
            canonical_id=f"news:{symbol}",
            display_name=symbol,
            asset_class="news-linked",
            symbol=symbol,
        )
        for symbol in symbols[:100]
    ]
    importance = record.get("importance_hint", record.get("importance"))
    if isinstance(importance, bool) or not isinstance(importance, (int, float)):
        importance = None
    elif not 0.0 <= float(importance) <= 1.0:
        importance = None
    event = build_market_event(
        MarketEventType.HEADLINE_PUBLISHED,
        source,
        published,
        payload,
        source_event_id=source_event_id,
        effective_at=published,
        published_at=published,
        markets=markets,
        importance_hint=float(importance) if importance is not None else None,
        metadata={
            "raw_store": "durable_news_snapshot",
            "raw_record_committed": True,
        },
        identity={"source_event_id": source_event_id},
        revision_of_event_id=prior.event_id if prior is not None else None,
        correlation_id=UUID(str(correlation_id))
        if correlation_id is not None
        else None,
    )
    return insert_event(session, event, topic=TOPIC)


def publish_news_records(
    records: Iterable[Mapping[str, Any]],
    *,
    config: Any = None,
    session: Any = None,
    correlation_id: UUID | str | None = None,
) -> PublicationResult:
    values = list(islice(records, 1001))
    if len(values) > 1000:
        raise ValueError("news publication batch exceeds 1000 records")
    raw_written = inserted = deduplicated = outbox = 0
    errors: list[str] = []

    def run(active_session: Any) -> None:
        nonlocal raw_written, inserted, deduplicated, outbox
        for record in values:
            try:
                result = publish_news_record(
                    active_session,
                    record,
                    correlation_id=correlation_id,
                )
            except Exception as exc:
                errors.append(type(exc).__name__)
                raise
            raw_written += 1
            if result.inserted:
                inserted += 1
                outbox += int(result.outbox_inserted)
            else:
                deduplicated += 1

    if session is not None:
        run(session)
    else:
        with get_session(config) as active_session:
            run(active_session)
    return PublicationResult(
        len(values),
        raw_written,
        inserted,
        deduplicated,
        outbox,
        tuple(errors),
    )


def event_pipeline_summary(config: Any = None) -> dict[str, Any]:
    pipeline = config.get("event_pipeline", {}) if isinstance(config, Mapping) else {}
    lease_seconds = (
        pipeline.get("lease_seconds", 120.0) if isinstance(pipeline, Mapping) else 120.0
    )
    with get_session(config) as session:
        return operations_summary(session, lease_seconds=lease_seconds)


def publish_fred(
    records: Iterable[Mapping[str, Any]], *, config: Any = None, session: Any = None
) -> PublicationResult:
    return publish_records("fred", records, config=config, session=session)


def publish_oanda(
    records: Iterable[Mapping[str, Any]], *, config: Any = None, session: Any = None
) -> PublicationResult:
    return publish_records("oanda", records, config=config, session=session)


publish = publish_records
__all__ = [
    "PublicationResult",
    "event_pipeline_summary",
    "map_record",
    "publish",
    "publish_collector_records_atomic",
    "publish_fred",
    "publish_news_record",
    "publish_news_records",
    "publish_oanda",
    "publish_record",
    "publish_records",
]
