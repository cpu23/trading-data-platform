"""Source-specific raw observation to market-event publication adapters."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from itertools import islice
from typing import Any
from uuid import UUID

from sqlalchemy import text

from db import get_session

from .canonicalize import build_market_event, canonical_json, content_hash
from .contracts import EntityRef, MarketEvent, MarketEventType, MarketRef
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


def _date(value: Any, name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{name} must be a valid date") from None
    raise ValueError(f"{name} must be a date")


def _raw_params(record: Mapping[str, Any]) -> dict[str, Any]:
    """Prepare raw-row parameters the way the generic upsert helper does."""
    return {
        key: (json.dumps(value) if isinstance(value, (dict, list)) else value)
        for key, value in record.items()
    }


def _insert_raw_do_nothing(
    session: Any,
    table_name: str,
    record: Mapping[str, Any],
    conflict_columns: list[str],
) -> bool:
    """Insert one raw row with ``ON CONFLICT DO NOTHING`` in the caller's transaction.

    Reserved for immutable source tables (``corporate_actions``,
    ``option_chain_snapshots``) whose guard triggers refuse UPDATE/DELETE;
    identical rows are idempotent no-ops and nothing is ever updated.
    Returns True when the row was newly inserted, False when the identity
    already existed (rowcount 0 on PostgreSQL and SQLite).
    """
    columns = list(record)
    statement = text(
        f"INSERT INTO {table_name} ({', '.join(columns)}) "
        f"VALUES ({', '.join(f':{column}' for column in columns)}) "
        f"ON CONFLICT ({', '.join(conflict_columns)}) DO NOTHING"
    )
    result = session.execute(statement, _raw_params(record))
    try:
        return int(result.rowcount or 0) >= 1
    except (TypeError, ValueError):
        # Fakes without a real rowcount report the insert as winning.
        return True


def _insert_raw_do_nothing_batch(
    session: Any,
    table_name: str,
    records: list[Mapping[str, Any]],
    conflict_columns: list[str],
    *,
    chunk_size: int = 1000,
) -> None:
    """Insert immutable raw rows in bounded executemany chunks.

    One ``INSERT ... ON CONFLICT DO NOTHING`` statement per chunk executed
    with a parameter batch, so large snapshots (e.g. 20k contracts per
    symbol) cost a bounded number of round trips. Records must share one
    homogeneous column set, validated before any statement executes; rows
    are never updated. The caller's transaction owns durability.
    """
    if not records:
        return
    canonical_keys = set(records[0])
    for record in records[1:]:
        if set(record) != canonical_keys:
            raise ValueError(f"Inconsistent columns for {table_name}")
    columns = list(records[0])
    statement = text(
        f"INSERT INTO {table_name} ({', '.join(columns)}) "
        f"VALUES ({', '.join(f':{column}' for column in columns)}) "
        f"ON CONFLICT ({', '.join(conflict_columns)}) DO NOTHING"
    )
    chunk_size = max(1, int(chunk_size))
    for start in range(0, len(records), chunk_size):
        chunk = records[start : start + chunk_size]
        session.execute(statement, [_raw_params(record) for record in chunk])


def _equity_markets(symbol: Any) -> list[MarketRef]:
    symbol = str(symbol or "").strip()
    if not symbol:
        return []
    return [
        MarketRef(
            canonical_id=f"equity:{symbol}",
            display_name=symbol,
            asset_class="equity",
            symbol=symbol,
        )
    ]


def _positioning_markets(raw: Mapping[str, Any], source: str) -> list[MarketRef]:
    if source != "cftc":
        return _equity_markets(raw.get("market_id"))
    metadata = raw.get("metadata")
    assets = metadata.get("assets") if isinstance(metadata, Mapping) else None
    if not isinstance(assets, (list, tuple)):
        return []
    markets: list[MarketRef] = []
    for value in assets:
        symbol = str(value or "").strip().upper()
        if not symbol:
            continue
        if symbol.startswith(("XAU", "XPT")):
            asset_class = "commodity"
        elif len(symbol) == 6 and symbol.isalpha():
            asset_class = "fx"
        else:
            asset_class = "futures"
        markets.append(
            MarketRef(
                canonical_id=f"cftc:{symbol}",
                display_name=symbol,
                asset_class=asset_class,
                symbol=symbol,
            )
        )
    return markets


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


def _document_ticker(raw: Mapping[str, Any]) -> str:
    metadata = raw.get("metadata")
    if isinstance(metadata, Mapping):
        return str(metadata.get("ticker") or metadata.get("symbol") or "").strip()
    return ""


def _transcript_available(raw: Mapping[str, Any]) -> bool:
    """False for issuer_transcripts operational diagnostic rows.

    Collector setup/timeout/failure state rows carry ``metadata.available
    is False`` and no content; they are diagnostics, not transcript
    evidence, and mirror the evidence adapter's filter exactly.
    """
    metadata = raw.get("metadata")
    return not (isinstance(metadata, Mapping) and metadata.get("available") is False)


def _issuer_document(
    record: Mapping[str, Any], *, source: str
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    document_id = str(record.get("document_id") or "").strip()
    if not document_id:
        raise ValueError("issuer document record requires document_id")
    published = _utc(record.get("published_at"), "published_at")
    raw = dict(record)
    raw["source"] = source
    payload = {
        "document_id": document_id,
        "source": source,
        "institution": record.get("institution"),
        "document_type": record.get("document_type"),
        "title": record.get("title"),
        "url": record.get("url"),
        "published_at": published,
    }
    return document_id, raw, payload


def _stabilize_inferred_document_publication(
    raw: dict[str, Any],
    payload: dict[str, Any],
    previous: MarketEvent | None,
) -> None:
    """Keep a first-seen fallback timestamp stable across collector retries."""
    metadata = raw.get("metadata")
    if (
        previous is None
        or not isinstance(metadata, Mapping)
        or metadata.get("published_at_inferred") is not True
    ):
        return
    prior_published = previous.payload.get("published_at")
    if prior_published is None:
        return
    published_at = _utc(prior_published, "published_at")
    raw["published_at"] = published_at
    payload["published_at"] = published_at
    stable_metadata = dict(metadata)
    stable_metadata["published_at"] = published_at.isoformat()
    raw["metadata"] = stable_metadata


def _document_entities(raw: Mapping[str, Any]) -> list[EntityRef]:
    entities: list[EntityRef] = []
    institution = str(raw.get("institution") or "").strip()
    if institution:
        entities.append(
            EntityRef(
                entity_type="company",
                canonical_id=institution,
                display_name=institution,
                confidence=1.0,
                mapping_source="source",
            )
        )
    ticker = _document_ticker(raw)
    if ticker:
        entities.append(
            EntityRef(
                entity_type="instrument",
                canonical_id=ticker,
                display_name=ticker,
                confidence=1.0,
                mapping_source="source",
            )
        )
    return entities


def _document_markets(raw: Mapping[str, Any]) -> list[MarketRef]:
    return _equity_markets(_document_ticker(raw))


def _equity_bar(
    record: Mapping[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    symbol = str(record.get("symbol") or "").strip().upper()
    timeframe = str(record.get("timeframe") or "").strip()
    timestamp = _utc(record.get("timestamp"), "timestamp")
    if not symbol or not timeframe:
        raise ValueError("public_equities record requires symbol and timeframe")
    raw = dict(record)
    raw["source"] = "public_equities"
    metadata = (
        record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    )
    payload = {
        "symbol": symbol,
        "timeframe": timeframe,
        "timestamp": timestamp,
        "open": record.get("open"),
        "high": record.get("high"),
        "low": record.get("low"),
        "close": record.get("close"),
        "volume": record.get("volume"),
        "metadata": {
            "adjusted": metadata.get("adjusted"),
            "interval": metadata.get("interval"),
            "range": metadata.get("range"),
            "provider_symbol": metadata.get("provider_symbol"),
            "currency": metadata.get("currency"),
            "exchange_name": metadata.get("exchange_name"),
            "source_timestamp": metadata.get("source_timestamp"),
        },
    }
    return f"{symbol}:{timeframe}:{timestamp.isoformat()}", raw, payload


def _positioning(
    record: Mapping[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    market_id = str(record.get("market_id") or "").strip()
    category = str(record.get("category") or "").strip()
    report_date = _date(record.get("report_date"), "report_date")
    if not market_id or not category:
        raise ValueError("positioning record requires market_id and category")
    raw = dict(record)
    metadata = record.get("metadata")
    positioning_kind = (
        metadata.get("positioning_kind") if isinstance(metadata, Mapping) else None
    )
    payload = {
        "market_id": market_id,
        "report_date": report_date.isoformat(),
        "category": category,
        "long_positions": record.get("long_positions"),
        "short_positions": record.get("short_positions"),
        "net_position": record.get("net_position"),
        "open_interest": record.get("open_interest"),
        "net_pct_open_interest": record.get("net_pct_open_interest"),
        "positioning_kind": positioning_kind,
    }
    return f"{market_id}:{report_date.isoformat()}:{category}", raw, payload


def _positioning_observed(raw: Mapping[str, Any]) -> datetime:
    metadata = raw.get("metadata")
    if isinstance(metadata, Mapping):
        acquired_at = metadata.get("acquired_at")
        if acquired_at is not None:
            return _utc(acquired_at, "acquired_at")
    report_date = _date(raw["report_date"], "report_date")
    return datetime.combine(report_date, time.min, tzinfo=UTC)


def _corporate_action(
    record: Mapping[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    action_id = str(record.get("action_id") or "").strip()
    symbol = str(record.get("symbol") or "").strip().upper()
    action_type = str(record.get("action_type") or "").strip()
    effective_date = _date(record.get("effective_date"), "effective_date")
    if not action_id or not symbol or action_type not in {"split", "dividend"}:
        raise ValueError(
            "corporate action record requires action_id, symbol and a valid action_type"
        )
    raw = dict(record)
    source_timestamp = raw.get("source_timestamp")
    if source_timestamp is not None:
        source_timestamp = _utc(source_timestamp, "source_timestamp")
    payload = {
        "action_id": action_id,
        "symbol": symbol,
        "action_type": action_type,
        "effective_date": effective_date.isoformat(),
        "source_timestamp": source_timestamp,
        "amount": record.get("amount"),
        "ratio_numerator": record.get("ratio_numerator"),
        "ratio_denominator": record.get("ratio_denominator"),
        "description": record.get("description"),
    }
    return action_id, raw, payload


def _corporate_action_observed(raw: Mapping[str, Any]) -> datetime:
    source_timestamp = raw.get("source_timestamp")
    if source_timestamp is not None:
        return _utc(source_timestamp, "source_timestamp")
    effective_date = _date(raw["effective_date"], "effective_date")
    return datetime.combine(effective_date, time.min, tzinfo=UTC)


def map_record(
    record: Mapping[str, Any], *, source: str | None = None
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    src = _source(record, source)
    if src == "fred":
        return _fred(record)
    if src == "oanda":
        return _oanda(record)
    if src in ("issuer_news", "issuer_transcripts", "company_expectations"):
        return _issuer_document(record, source=src)
    if src == "public_equities":
        return _equity_bar(record)
    if src in ("cftc", "sec_form4", "finra_short_volume"):
        return _positioning(record)
    if src == "corporate_actions":
        return _corporate_action(record)
    raise ValueError("unsupported event publication source")


def publish_record(
    session: Any,
    record: Mapping[str, Any],
    *,
    source: str | None = None,
    correlation_id: UUID | str | None = None,
    insert_only: bool = False,
) -> EventInsertResult:
    """Publish one raw record and its event inside the caller's transaction.

    ``insert_only`` keeps the raw row write immutable (``ON CONFLICT DO
    NOTHING``) instead of the usual upsert; event dedup is unchanged.
    """
    src = _source(record, source)
    source_event_id, raw, payload = map_record(record, source=src)
    previous = None
    entities: list[EntityRef] = []
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
    elif src in ("issuer_news", "issuer_transcripts", "company_expectations"):
        if src == "issuer_transcripts" and not _transcript_available(raw):
            # Operational diagnostic row (setup/timeout/failed, no content):
            # persist the raw document atomically but emit no event/outbox.
            upsert_raw(session, "source_documents", raw, ["document_id"])
            return EventInsertResult(event=None, inserted=False, outbox_inserted=False)
        previous = find_latest_event(
            session, source=src, dedupe_key=f"{src}:{source_event_id}"
        )
        _stabilize_inferred_document_publication(raw, payload, previous)
        if src == "company_expectations":
            metadata = raw.get("metadata")
            next_earnings = (
                metadata.get("next_earnings") if isinstance(metadata, Mapping) else None
            )
            event_type = (
                MarketEventType.CALENDAR_EVENT_CHANGED
                if isinstance(next_earnings, Mapping)
                else MarketEventType.MANUAL_RESEARCH_EVENT
            )
        elif src == "issuer_news":
            document_type = str(raw.get("document_type") or "").strip().lower()
            event_type = (
                MarketEventType.REGULATORY_FILING_PUBLISHED
                if "regulatory" in document_type
                else MarketEventType.HEADLINE_PUBLISHED
            )
        else:
            event_type = MarketEventType.TRANSCRIPT_PUBLISHED
        table, conflict, observed = (
            "source_documents",
            ["document_id"],
            _utc(raw["published_at"], "published_at"),
        )
        markets = _document_markets(raw)
        entities = _document_entities(raw)
    elif src == "public_equities":
        previous = find_latest_event(
            session, source=src, dedupe_key=f"{src}:{source_event_id}"
        )
        event_type = MarketEventType.PRICE_BAR_CLOSED
        table, conflict, observed = (
            "market_data",
            ["symbol", "timeframe", "timestamp"],
            _utc(raw["timestamp"], "timestamp"),
        )
        markets = _equity_markets(raw["symbol"])
    elif src in ("cftc", "sec_form4", "finra_short_volume"):
        previous = find_latest_event(
            session, source=src, dedupe_key=f"{src}:{source_event_id}"
        )
        event_type = MarketEventType.POSITIONING_REPORT_PUBLISHED
        table, conflict = (
            "positioning_reports",
            ["source", "market_id", "report_date", "category"],
        )
        observed = _positioning_observed(raw)
        markets = _positioning_markets(raw, src)
    elif src == "oanda":
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
    elif src == "corporate_actions":
        observed = _corporate_action_observed(raw)
        _insert_raw_do_nothing(session, "corporate_actions", raw, ["action_id"])
        event = build_market_event(
            MarketEventType.CORPORATE_ACTION_PUBLISHED,
            src,
            observed,
            payload,
            source_event_id=source_event_id,
            effective_at=observed,
            markets=_equity_markets(raw["symbol"]),
            metadata={"raw_table": "corporate_actions"},
            identity={"source_event_id": source_event_id},
            correlation_id=UUID(str(correlation_id))
            if correlation_id is not None
            else None,
        )
        return insert_event(session, event, topic=TOPIC)
    else:
        raise ValueError("unsupported event publication source")
    if insert_only:
        # Immutable raw rows: identical re-collected rows are idempotent
        # no-ops and a stored row is never revised (its updated_at stays
        # at the original ingestion time).  When the identity already
        # exists the event is a no-op too: the first insert already
        # published it, and a changed incoming payload must never emit a
        # revision event that disagrees with the first-frozen raw row.
        if not _insert_raw_do_nothing(session, table, raw, conflict):
            return EventInsertResult(event=None, inserted=False, outbox_inserted=False)
    else:
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
        entities=entities,
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
    insert_only: bool = False,
) -> PublicationResult:
    values = list(records)
    raw_written = events_inserted = events_deduplicated = outbox_inserted = 0
    errors: list[str] = []

    def run(active_session: Any) -> None:
        nonlocal raw_written, events_inserted, events_deduplicated, outbox_inserted
        for record in values:
            try:
                result = publish_record(
                    active_session,
                    record,
                    source=source,
                    correlation_id=correlation_id,
                    insert_only=insert_only,
                )
            except Exception as exc:
                errors.append(type(exc).__name__)
                raise
            raw_written += 1
            if result.event is None:
                # Diagnostic row persisted without an event (e.g. an
                # unavailable transcript): neither inserted nor deduplicated.
                continue
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


_EXPECTED_SOURCE_TABLES = {
    "fred": "macro_series",
    "oanda": "market_data",
    "issuer_news": "source_documents",
    "issuer_transcripts": "source_documents",
    "company_expectations": "source_documents",
    "public_equities": "market_data",
    "sec_form4": "positioning_reports",
    "finra_short_volume": "positioning_reports",
    "cftc": "positioning_reports",
    "cboe_options": "option_chain_snapshots",
}

_EXPECTED_CONFLICT_COLUMNS = {
    "fred": ["series_id", "observed_at"],
    "oanda": ["symbol", "timeframe", "timestamp"],
    "issuer_news": ["document_id"],
    "issuer_transcripts": ["document_id"],
    "company_expectations": ["document_id"],
    "public_equities": ["symbol", "timeframe", "timestamp"],
    "sec_form4": ["source", "market_id", "report_date", "category"],
    "finra_short_volume": ["source", "market_id", "report_date", "category"],
    "cftc": ["source", "market_id", "report_date", "category"],
    "cboe_options": ["source", "contract_symbol", "captured_at"],
}


def _write_additional_batches(session: Any, batches: list[Any]) -> None:
    """Persist caller-supplied raw-only batches inside the caller's transaction.

    ``db.write_batches_in_session`` owns the batch contract (table, records,
    conflict columns) and runs on the same session as the primary event
    writes, so raw rows, events, and the outbox commit or roll back together.
    Only non-corporate-action batches reach this helper; corporate action
    batches are event-published instead.
    """
    from db import write_batches_in_session

    write_batches_in_session(session, batches)


def _additional_batch_attributes(
    batch: Any, index: int
) -> tuple[str, Any, list[str], bool]:
    """Extract declared batch attributes from Mapping or attribute-shaped
    objects (CollectionWriteBatch), mirroring the db helper's contract."""
    if isinstance(batch, Mapping):
        table_name = batch.get("table_name")
        records = batch.get("records")
        conflict_columns = batch.get("conflict_columns")
        insert_only = batch.get("insert_only", False)
    else:
        table_name = getattr(batch, "table_name", None)
        records = getattr(batch, "records", None)
        conflict_columns = getattr(batch, "conflict_columns", None)
        insert_only = getattr(batch, "insert_only", False)
    if (
        not isinstance(table_name, str)
        or not isinstance(conflict_columns, list)
        or records is None
    ):
        raise ValueError(f"invalid write batch at index {index}")
    return table_name, records, list(conflict_columns), bool(insert_only)


def _partition_additional_writes(
    batches: list[Any],
) -> tuple[list[list[Mapping[str, Any]]], list[Any]]:
    """Split additional writes into event-published and raw-only batches.

    ``corporate_actions`` batches are immutable and event-published (one
    CORPORATE_ACTION_PUBLISHED event per action row, raw rows inserted with
    ``ON CONFLICT DO NOTHING``); every other table stays raw-only for
    ``db.write_batches_in_session``. Batch-level validation runs before any
    write so a malformed declaration rejects the whole transaction.
    """
    event_batches: list[list[Mapping[str, Any]]] = []
    raw_batches: list[Any] = []
    for index, batch in enumerate(batches):
        table_name, records, conflict_columns, insert_only = (
            _additional_batch_attributes(batch, index)
        )
        if table_name == "corporate_actions":
            if conflict_columns != ["action_id"] or not insert_only:
                raise ValueError(
                    "corporate_actions additional writes require conflict "
                    "['action_id'] and insert_only semantics"
                )
            event_batches.append(list(records))
        else:
            raw_batches.append(batch)
    return event_batches, raw_batches


def _merge_publication_results(
    *results: PublicationResult,
) -> PublicationResult:
    """Combine publication results from batches sharing one transaction."""
    return PublicationResult(
        attempted=sum(result.attempted for result in results),
        raw_written=sum(result.raw_written for result in results),
        events_inserted=sum(result.events_inserted for result in results),
        events_deduplicated=sum(result.events_deduplicated for result in results),
        outbox_inserted=sum(result.outbox_inserted for result in results),
        errors=tuple(error for result in results for error in result.errors),
    )


def publish_collector_records_atomic(
    *,
    source_id: str,
    table_name: str,
    records: Iterable[Mapping[str, Any]],
    conflict_columns: list[str],
    correlation_id: UUID | str | None = None,
    config: Any = None,
    additional_writes: Iterable[Mapping[str, Any]] = (),
    insert_only: bool = False,
) -> PublicationResult:
    source = str(source_id).strip().lower()
    if _EXPECTED_SOURCE_TABLES.get(source) != table_name:
        raise ValueError("unsupported source/table event publication pair")
    if list(conflict_columns) != _EXPECTED_CONFLICT_COLUMNS[source]:
        raise ValueError("unsupported source conflict columns")
    extra = list(additional_writes)
    if not extra:
        if source == "cboe_options":
            return publish_option_chain_records(
                records, config=config, correlation_id=correlation_id
            )
        if insert_only:
            return publish_records(
                source,
                records,
                config=config,
                correlation_id=correlation_id,
                insert_only=True,
            )
        return publish_records(
            source, records, config=config, correlation_id=correlation_id
        )
    event_batches, raw_batches = _partition_additional_writes(extra)
    # Multi-table collectors share one transaction: primary records, their
    # events/outbox, event-published additions (corporate actions), and raw
    # additional batches commit or roll back together.
    with get_session(config) as active_session:
        if source == "cboe_options":
            result = publish_option_chain_records(
                records, session=active_session, correlation_id=correlation_id
            )
        elif insert_only:
            result = publish_records(
                source,
                records,
                session=active_session,
                correlation_id=correlation_id,
                insert_only=True,
            )
        else:
            result = publish_records(
                source,
                records,
                session=active_session,
                correlation_id=correlation_id,
            )
        for action_records in event_batches:
            addition = publish_records(
                "corporate_actions",
                action_records,
                session=active_session,
                correlation_id=correlation_id,
            )
            result = _merge_publication_results(result, addition)
        if raw_batches:
            _write_additional_batches(active_session, raw_batches)
        return result


def _group_option_records(
    records: Iterable[Mapping[str, Any]],
) -> list[tuple[str, datetime, list[dict[str, Any]]]]:
    """Validate and group option contract rows by (symbol, captured_at).

    Every contract is validated before any database work so a malformed
    record anywhere in the batch rejects the whole snapshot group
    atomically. Rows are immutable ``option_chain_snapshots`` rows; the
    group key is the deterministic point-in-time snapshot identity.
    """
    groups: dict[tuple[str, datetime], list[dict[str, Any]]] = {}
    order: list[tuple[str, datetime]] = []
    canonical_keys: set[str] | None = None
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("option chain record must be a mapping")
        source = str(record.get("source") or "").strip()
        symbol = str(record.get("symbol") or "").strip()
        contract_symbol = str(record.get("contract_symbol") or "").strip()
        option_type = str(record.get("option_type") or "").strip()
        if not source or not symbol or not contract_symbol:
            raise ValueError(
                "option chain record requires source, symbol and contract_symbol"
            )
        if option_type not in ("call", "put"):
            raise ValueError(f"invalid option_type {option_type!r}")
        strike = record.get("strike")
        if (
            isinstance(strike, bool)
            or not isinstance(strike, (int, float))
            or not math.isfinite(float(strike))
            or float(strike) <= 0
        ):
            raise ValueError(f"invalid option strike {strike!r}")
        _date(record.get("expiration"), "expiration")
        captured_at = _utc(record.get("captured_at"), "captured_at")
        keys = set(record)
        if canonical_keys is None:
            canonical_keys = keys
        elif keys != canonical_keys:
            raise ValueError("option chain records must share one column set")
        key = (symbol, captured_at)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(dict(record))
    return [
        (symbol, captured_at, groups[(symbol, captured_at)])
        for symbol, captured_at in order
    ]


def _publish_option_snapshot(
    session: Any,
    symbol: str,
    captured_at: datetime,
    chain: list[dict[str, Any]],
    *,
    correlation_id: UUID | str | None = None,
) -> EventInsertResult:
    """Persist one immutable snapshot and emit one compact event for it."""
    _insert_raw_do_nothing_batch(
        session,
        "option_chain_snapshots",
        chain,
        ["source", "contract_symbol", "captured_at"],
    )
    source_times = sorted(
        _utc(record["source_timestamp"], "source_timestamp")
        for record in chain
        if record.get("source_timestamp") is not None
    )
    expirations = sorted(_date(record["expiration"], "expiration") for record in chain)
    calls = sum(1 for record in chain if record["option_type"] == "call")
    first_metadata = chain[0].get("metadata")
    metadata = first_metadata if isinstance(first_metadata, Mapping) else {}
    payload = {
        "symbol": symbol,
        "captured_at": captured_at.isoformat(),
        "contract_count": len(chain),
        "contracts_by_type": {"call": calls, "put": len(chain) - calls},
        "expiration_count": len({expiration for expiration in expirations}),
        "source_timestamp_min": (source_times[0].isoformat() if source_times else None),
        "source_timestamp_max": (
            source_times[-1].isoformat() if source_times else None
        ),
        "expiration_min": expirations[0].isoformat(),
        "expiration_max": expirations[-1].isoformat(),
        "underlying_price": chain[0].get("underlying_price"),
        "delayed": bool(metadata.get("delayed")),
        "delay_minutes": metadata.get("delay_minutes"),
        "truncated": metadata.get("truncated") or {},
    }
    event = build_market_event(
        MarketEventType.OPTION_CHAIN_PUBLISHED,
        "cboe_options",
        captured_at,
        payload,
        source_event_id=f"{symbol}:{captured_at.isoformat()}",
        effective_at=captured_at,
        markets=_equity_markets(symbol),
        metadata={"raw_table": "option_chain_snapshots"},
        identity={"symbol": symbol, "captured_at": captured_at.isoformat()},
        correlation_id=UUID(str(correlation_id))
        if correlation_id is not None
        else None,
    )
    return insert_event(session, event, topic=TOPIC)


def publish_option_chain_records(
    records: Iterable[Mapping[str, Any]],
    *,
    config: Any = None,
    session: Any = None,
    correlation_id: UUID | str | None = None,
) -> PublicationResult:
    """Publish immutable option chain snapshots as one compact event each.

    Contract rows are persisted with ``INSERT ... ON CONFLICT DO NOTHING``
    (never UPDATE); exactly one ``OPTION_CHAIN_PUBLISHED`` event is emitted
    per (symbol, captured_at) group with contract count, source quote-time
    bounds, expiry bounds, and availability metadata. Malformed records
    reject the whole batch before any write.
    """
    values = list(records)
    groups = _group_option_records(values)
    raw_written = inserted = deduplicated = outbox = 0
    errors: list[str] = []

    def run(active_session: Any) -> None:
        nonlocal raw_written, inserted, deduplicated, outbox
        for symbol, captured_at, chain in groups:
            try:
                result = _publish_option_snapshot(
                    active_session,
                    symbol,
                    captured_at,
                    chain,
                    correlation_id=correlation_id,
                )
            except Exception as exc:
                errors.append(type(exc).__name__)
                raise
            raw_written += len(chain)
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
    "publish_option_chain_records",
    "publish_record",
    "publish_records",
]
