"""Build the validated Reuters + Kobeissi unified news feed."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from logging_config import get_logger
from sources.news_result import NewsCollectionResult
from sources.news_storage import (
    atomic_write_json,
    merge_items,
    publication_lock,
    read_json,
)

logger = get_logger("news_feed")


def _timestamp(value: Any) -> float | None:
    from publication import _news_timestamp

    return _news_timestamp(value)


def _build_feed_unlocked(config: dict, days: int = 7) -> dict[str, Any]:
    from publication import build_news_feed_unlocked

    return build_news_feed_unlocked(
        config,
        days,
        atomic_write=atomic_write_json,
        read_json=read_json,
    )


def build_feed(config: dict, days: int = 7) -> dict[str, Any]:
    """Atomically rebuild the feed as the sole public lock owner."""
    output_dir = Path(
        config.get("news_feed", {}).get("output_path", "/var/lib/trading-data/news")
    )
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
    output_dir = Path(
        config.get("news_feed", {}).get("output_path", "/var/lib/trading-data/news")
    )
    history_days = (
        days if days is not None else config.get("news_feed", {}).get("history_days", 7)
    )
    with publication_lock(output_dir):
        result = collector()
        if not result.succeeded:
            return result
        from publication import publish_news_result_unlocked

        return publish_news_result_unlocked(
            source_id,
            config,
            result,
            days=history_days,
            merge_items_fn=merge_items,
            build_feed_fn=_build_feed_unlocked,
            atomic_write_fn=atomic_write_json,
        )
