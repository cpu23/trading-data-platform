"""Build the validated Reuters + Kobeissi unified news feed."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from logging_config import get_logger
from sources.news_result import NewsCollectionResult
from sources.news_storage import atomic_write_json, merge_items, publication_lock, read_json

logger = get_logger("news_feed")
SOURCES = ("reuters", "kobeissi")
REQUIRED_FIELDS = {
    "id", "source", "source_label", "title", "summary", "url", "published",
    "symbols", "tags", "engagement", "media", "meta", "fetched_at",
}


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def _build_feed_unlocked(config: dict, days: int = 7) -> dict[str, Any]:
    if days < 1:
        raise ValueError("days must be at least 1")
    output_dir = Path(config.get("news_feed", {}).get("output_path", "/var/lib/trading-data/news"))
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).timestamp()
    by_id: dict[str, tuple[float, dict]] = {}

    for source in SOURCES:
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
                if not isinstance(item, dict) or not REQUIRED_FIELDS.issubset(item):
                    logger.warning("news_item_invalid", path=str(path))
                    continue
                if item["source"] != source or not isinstance(item["id"], str) or not item["id"]:
                    continue
                timestamp = _timestamp(item["published"])
                if timestamp is None or timestamp < cutoff:
                    continue
                previous = by_id.get(item["id"])
                if previous is None or timestamp > previous[0]:
                    by_id[item["id"]] = (timestamp, item)

    ordered = sorted(by_id.values(), key=lambda pair: (-pair[0], pair[1]["id"]))
    items = [item for _, item in ordered]
    feed = {
        "generated_at": now.isoformat(), "days": days, "count": len(items),
        "sources": sorted({item["source"] for item in items}), "items": items,
    }
    atomic_write_json(output_dir / "feed.json", feed)
    logger.info("feed_built", count=len(items), path=str(output_dir / "feed.json"))
    return feed


def build_feed(config: dict, days: int = 7) -> dict[str, Any]:
    """Atomically rebuild the feed as the sole public lock owner."""
    output_dir = Path(config.get("news_feed", {}).get("output_path", "/var/lib/trading-data/news"))
    with publication_lock(output_dir):
        return _build_feed_unlocked(config, days)


def collect_and_publish(
    source_id: str,
    config: dict,
    collector: Callable[[], NewsCollectionResult],
    *,
    days: int | None = None,
) -> NewsCollectionResult:
    """Publish snapshot/feed before committing a collector's advanced cursor."""
    output_dir = Path(config.get("news_feed", {}).get("output_path", "/var/lib/trading-data/news"))
    history_days = days if days is not None else config.get("news_feed", {}).get("history_days", 7)
    with publication_lock(output_dir):
        result = collector()
        if not result.succeeded:
            return result
        try:
            if result.publication is not None and result.items:
                merge_items(result.publication.snapshot_path, result.items)
            _build_feed_unlocked(config, days=history_days)
        except Exception as exc:
            error = f"News feed publication failed: {type(exc).__name__}"
            logger.error("news_feed_publication_failed", source_id=source_id, error=error)
            return NewsCollectionResult(result.items, "error", error, result.publication)
        if result.publication is not None:
            try:
                atomic_write_json(
                    result.publication.state_path,
                    result.publication.candidate_state,
                )
            except Exception as exc:
                error = f"News state persistence failed: {type(exc).__name__}"
                logger.error("news_state_persistence_failed", source_id=source_id, error=error)
                return NewsCollectionResult(
                    result.items, "error", error, result.publication, True
                )
        return NewsCollectionResult(result.items, result.status, result.error, feed_published=True)
