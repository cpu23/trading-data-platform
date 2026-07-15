"""Strict registry for supported news collection sources."""
from __future__ import annotations

NEWS_SOURCE_IDS = frozenset({"reuters", "kobeissi"})


def get_news_source_ids() -> frozenset[str]:
    return NEWS_SOURCE_IDS


def get_news_collector(source_id: str, config: dict):
    if source_id == "reuters":
        from sources.reuters import run_reuters

        pages = config.get("reuters", {}).get("max_pages", 3)
        return lambda: run_reuters(config, max_pages=pages)
    if source_id == "kobeissi":
        from sources.kobeissi import run_kobeissi

        count = config.get("kobeissi", {}).get("count", 20)
        return lambda: run_kobeissi(config, count=count)
    raise KeyError(source_id)
