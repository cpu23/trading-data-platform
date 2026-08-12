"""Publication and persistence for collector, processor, and news outputs.

These helpers own the durable writes. Callers translate raised exceptions into
the error taxonomy (see ``errors.py``) so retry and reporting policy stays
explicit instead of depending on where a broad catch happens.
"""

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from errors import PersistenceError
from logging_config import get_logger

logger = get_logger("orchestrator.publication")
NEWS_SOURCES = ("reuters", "kobeissi")
NEWS_REQUIRED_FIELDS = {
    "id",
    "source",
    "source_label",
    "title",
    "summary",
    "url",
    "published",
    "symbols",
    "tags",
    "engagement",
    "media",
    "meta",
    "fetched_at",
}


def _news_timestamp(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).timestamp()
    except ValueError:
        return None


def build_news_feed_unlocked(
    config: dict,
    days: int = 7,
    *,
    atomic_write: Callable | None = None,
    read_json: Callable | None = None,
) -> dict:
    """Build and durably write the unified feed without acquiring its lock."""
    from sources.news_storage import atomic_write_json as default_atomic_write
    from sources.news_storage import read_json as default_read_json

    atomic_write = atomic_write or default_atomic_write
    read_json = read_json or default_read_json
    if days < 1:
        raise ValueError("days must be at least 1")
    output_dir = Path(
        config.get("news_feed", {}).get("output_path", "/var/lib/trading-data/news")
    )
    cutoff = (datetime.now(UTC) - timedelta(days=days)).timestamp()
    by_id: dict[str, tuple[float, dict]] = {}
    for source in NEWS_SOURCES:
        if not config.get(source, {}).get("enabled", False):
            continue
        source_dir = output_dir / source
        if not source_dir.is_dir():
            continue
        for path in sorted(source_dir.glob("*.json")):
            if path.name == "state.json":
                continue
            values = read_json(path, [])
            if not isinstance(values, list):
                continue
            for item in values:
                if not isinstance(item, dict) or not NEWS_REQUIRED_FIELDS.issubset(
                    item
                ):
                    logger.warning("news_item_invalid", path=str(path))
                    continue
                if (
                    item["source"] != source
                    or not isinstance(item["id"], str)
                    or not item["id"]
                ):
                    continue
                timestamp = _news_timestamp(item["published"])
                if timestamp is None or timestamp < cutoff:
                    continue
                previous = by_id.get(item["id"])
                if previous is None or timestamp > previous[0]:
                    by_id[item["id"]] = (timestamp, item)
    ordered = sorted(by_id.values(), key=lambda pair: (-pair[0], pair[1]["id"]))
    items = [item for _, item in ordered]
    feed = {
        "generated_at": datetime.now(UTC).isoformat(),
        "days": days,
        "count": len(items),
        "sources": sorted({item["source"] for item in items}),
        "items": items,
    }
    atomic_write(output_dir / "feed.json", feed)
    if config.get("database"):
        from investment_observations import persist_news_observations

        persist_news_observations(config, items)
        pipeline = config.get("event_pipeline", {})
        if isinstance(pipeline, Mapping) and pipeline.get("enabled", False):
            from events.publisher import publish_news_records

            story_settings = config.get("story_clustering", {})
            story_settings = (
                story_settings if isinstance(story_settings, Mapping) else {}
            )
            try:
                publish_limit = max(
                    1, min(1000, int(story_settings.get("publish_limit", 500)))
                )
            except (TypeError, ValueError, OverflowError):
                publish_limit = 500
            publish_news_records(items[:publish_limit], config=config)
    logger.info("feed_built", count=len(items), path=str(output_dir / "feed.json"))
    return feed


def build_news_feed(config: dict, days: int = 7) -> dict:
    """Build the feed while taking the sole news publication lock."""
    from sources.news_storage import publication_lock

    output_dir = Path(
        config.get("news_feed", {}).get("output_path", "/var/lib/trading-data/news")
    )
    with publication_lock(output_dir):
        return build_news_feed_unlocked(config, days)


def publish_news_result_unlocked(
    source_id: str,
    config: dict,
    result,
    *,
    days: int = 7,
    merge_items_fn: Callable | None = None,
    build_feed_fn: Callable | None = None,
    atomic_write_fn: Callable | None = None,
):
    """Publish a successful news collection and commit its candidate state."""
    from sources.news_result import NewsCollectionResult
    from sources.news_storage import atomic_write_json, merge_items

    merge_items_fn = merge_items_fn or merge_items
    build_feed_fn = build_feed_fn or build_news_feed_unlocked
    atomic_write_fn = atomic_write_fn or atomic_write_json
    try:
        if result.publication is not None and result.items:
            merge_items_fn(result.publication.snapshot_path, result.items)
        build_feed_fn(config, days=days)
    except Exception as exc:
        error = f"News feed publication failed: {type(exc).__name__}"
        logger.error("news_feed_publication_failed", source_id=source_id, error=error)
        return NewsCollectionResult(
            result.items,
            "error",
            error,
            publication=result.publication,
            error_class="persistence",
        )
    if result.publication is not None:
        try:
            atomic_write_fn(
                result.publication.state_path, result.publication.candidate_state
            )
        except Exception as exc:
            error = f"News state persistence failed: {type(exc).__name__}"
            logger.error(
                "news_state_persistence_failed", source_id=source_id, error=error
            )
            return NewsCollectionResult(
                result.items,
                "error",
                error,
                publication=result.publication,
                feed_published=True,
                error_class="persistence",
            )
    return NewsCollectionResult(
        result.items, result.status, result.error, feed_published=True
    )


def _get_session(config: dict):
    import orchestrator

    return orchestrator.get_session(config)


def _write_collection_log(
    collector_id: str,
    started_at: datetime,
    completed_at: datetime,
    status: str,
    records_fetched: int,
    records_written: int,
    error_message: str | None,
    error_traceback: str | None,
    duration_ms: int,
    api_calls_made: int,
    config: dict,
    correlation_id: str,
):
    config_snapshot = {}
    collector_config = config.get("collectors", {}).get(collector_id, {})
    safe_keys = ["schedule", "enabled"]
    for key in safe_keys:
        if key in collector_config:
            config_snapshot[key] = collector_config[key]
    if "series" in collector_config:
        config_snapshot["series_count"] = len(collector_config["series"])
    if "instruments" in collector_config:
        config_snapshot["instruments_count"] = len(
            [
                item
                for item in collector_config["instruments"]
                if item.get("enabled", True)
            ]
        )
    if "snapshot_timeframe" in collector_config:
        config_snapshot["snapshot_timeframe"] = collector_config["snapshot_timeframe"]

    log_record = {
        "started_at": started_at,
        "completed_at": completed_at,
        "collector": collector_id,
        "status": status,
        "records_fetched": records_fetched,
        "records_written": records_written,
        "error_message": error_message,
        "error_traceback": error_traceback,
        "duration_ms": duration_ms,
        "api_calls_made": api_calls_made,
        "config_snapshot": json.dumps(config_snapshot),
        "correlation_id": correlation_id,
    }

    try:
        with _get_session(config) as session:
            columns = ", ".join(log_record.keys())
            placeholders = ", ".join(f":{k}" for k in log_record)
            sql = text(
                f"INSERT INTO collection_log ({columns}) VALUES ({placeholders})"
            )
            session.execute(sql, log_record)
    except Exception as exc:
        logger.error(
            "collection_log_write_failed",
            action="write_collection_log",
            error=str(exc),
            correlation_id=correlation_id,
        )
        raise PersistenceError("collection log write failed") from exc


def _write_processing_log(
    processor_id: str,
    started_at: datetime,
    completed_at: datetime,
    status: str,
    input_summary: dict | None,
    output_id: str | None,
    prompt_text: str | None,
    raw_response: str | None,
    model_used: str | None,
    tokens_input: int | None,
    tokens_output: int | None,
    cost_usd: float | None,
    duration_ms: int,
    error_message: str | None,
    input_fingerprint: str | None,
    skip_reason: str | None,
    forced: bool,
    config: dict,
    correlation_id: str,
):
    log_record = {
        "started_at": started_at,
        "completed_at": completed_at,
        "processor": processor_id,
        "status": status,
        "input_summary": json.dumps(input_summary) if input_summary else None,
        "output_id": output_id,
        "prompt_text": None,
        "raw_response": None,
        "model_used": model_used,
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "cost_usd": cost_usd,
        "duration_ms": duration_ms,
        "error_message": error_message,
        "input_fingerprint": input_fingerprint,
        "skip_reason": skip_reason,
        "forced": forced,
        "correlation_id": correlation_id,
    }

    try:
        with _get_session(config) as session:
            columns = ", ".join(log_record.keys())
            placeholders = ", ".join(f":{k}" for k in log_record)
            sql = text(
                f"INSERT INTO processing_log ({columns}) VALUES ({placeholders})"
            )
            session.execute(sql, log_record)
    except Exception as exc:
        logger.error(
            "processing_log_write_failed",
            action="write_processing_log",
            error=str(exc),
            correlation_id=correlation_id,
        )
        raise PersistenceError("processing log write failed") from exc


def _persist_processor_result(
    opinions: list[dict],
    extra_records: dict[str, list[dict]],
    processing_log: dict,
    processor_id: str,
    started_at: datetime,
    completed_at: datetime,
    duration_ms: int,
    correlation_id: str,
    config: dict,
) -> None:
    """Persist one processor result as a single transaction."""
    from db import insert_records_in_session, upsert_records_in_session

    try:
        with _get_session(config) as session:
            insert_records_in_session(session, "structured_opinions", opinions)
            for table_name, records in extra_records.items():
                if table_name == "daily_briefings":
                    upsert_records_in_session(
                        session,
                        table_name,
                        records,
                        ["briefing_date", "correlation_id"],
                    )
                else:
                    insert_records_in_session(session, table_name, records)

            log_record = {
                "started_at": started_at,
                "completed_at": completed_at,
                "processor": processor_id,
                "status": "success",
                "input_summary": processing_log.get("input_summary"),
                "output_id": processing_log.get("output_id"),
                "output_ids": processing_log.get("output_ids", []),
                "prompt_text": processing_log.get("prompt_text"),
                "raw_response": processing_log.get("raw_response"),
                "model_used": processing_log.get("model_used"),
                "tokens_input": processing_log.get("tokens_input"),
                "tokens_output": processing_log.get("tokens_output"),
                "cost_usd": processing_log.get("cost_usd"),
                "duration_ms": duration_ms,
                "error_message": None,
                "request_metadata": processing_log.get("request_metadata"),
                "correlation_id": correlation_id,
            }
            insert_records_in_session(session, "processing_log", [log_record])
    except PersistenceError:
        raise
    except Exception as exc:
        logger.error(
            "processor_persistence_failed",
            action="persist_processor_result",
            processor=processor_id,
            error=str(exc),
            correlation_id=correlation_id,
        )
        raise PersistenceError("processor result persistence failed") from exc

    logger.info(
        "processor_records_written",
        action="run_processor",
        processor=processor_id,
        opinion_count=len(opinions),
        extra_tables=list(extra_records),
        correlation_id=correlation_id,
    )


__all__ = [
    "_write_collection_log",
    "_write_processing_log",
    "_persist_processor_result",
    "build_news_feed_unlocked",
    "build_news_feed",
    "publish_news_result_unlocked",
]
