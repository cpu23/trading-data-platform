"""Reuters news sitemap poller — discovers and normalises market-relevant articles."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from logging_config import get_logger
from sources.news_storage import atomic_write_json, merge_items, read_json

logger = get_logger("reuters")

REUTERS_SITEMAP_INDEX = (
    "https://www.reuters.com/arc/outboundfeeds/news-sitemap-index/?outputType=xml"
)

_NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "news": "http://www.google.com/schemas/sitemap-news/0.9",
    "image": "http://www.google.com/schemas/sitemap-image/1.1",
}

_LANG_PREFIXES = ("es", "fr", "pt", "de", "it", "ja", "zh", "ar", "ko")


def _extract_section(url: str) -> str:
    m = re.match(r"https://www\.reuters\.com/([^/]+)/", url)
    return m.group(1) if m else ""


def _is_markets_relevant(
    url: str, title: str, keywords_xml: str, config: dict
) -> tuple[bool, list[str]]:
    section = _extract_section(url)
    markets_sections = set(config.get("markets_sections", {
        "markets", "business", "finance", "economy", "companies",
        "breakingviews", "wealth",
    }))
    if section in markets_sections:
        return True, [f"section:{section}"]

    combined = f"{title} {keywords_xml}".lower()
    keywords = config.get("markets_keywords", [])
    matched = [kw for kw in keywords if kw in combined]
    return bool(matched), matched


def _fetch_sitemap_index() -> list[str]:
    import urllib.request
    req = urllib.request.Request(
        REUTERS_SITEMAP_INDEX,
        headers={"User-Agent": "TradingResearchSystem/1.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        xml_data = resp.read()
    root = ET.fromstring(xml_data)
    return [
        sm.findtext("sm:loc", "", _NS)
        for sm in root.findall("sm:sitemap", _NS)
        if sm.findtext("sm:loc", "", _NS)
    ]


def _parse_sitemap_page(
    url: str, seen_urls: set[str], config: dict
) -> list[dict[str, Any]]:
    """Fetch a sitemap page and return normalised feed items."""
    import urllib.request

    req = urllib.request.Request(
        url, headers={"User-Agent": "TradingResearchSystem/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            xml_data = resp.read()
    except Exception as e:
        logger.warning("reuters_sitemap_fetch_failed", url=url, error=str(e))
        return []

    root = ET.fromstring(xml_data)
    items = []
    now = datetime.now(timezone.utc).isoformat()

    for url_el in root.findall("sm:url", _NS):
        loc = url_el.findtext("sm:loc", "", _NS)
        if not loc or loc in seen_urls:
            continue
        if re.search(rf"\.com/({'|'.join(_LANG_PREFIXES)})/", loc):
            continue

        news_el = url_el.find("news:news", _NS)
        if news_el is None:
            continue

        title = news_el.findtext("news:title", "", _NS)
        pub_date = news_el.findtext("news:publication_date", "", _NS)
        keywords = news_el.findtext("news:keywords", "", _NS)

        is_relevant, matched = _is_markets_relevant(loc, title, keywords, config)
        if not is_relevant:
            continue

        img_el = url_el.find("image:image", _NS)
        image_url = img_el.findtext("image:loc", "", _NS) if img_el is not None else ""

        # Normalised feed item
        display_tags = [t.replace("section:", "") for t in matched if not t.startswith("section:")]
        slug = loc.rstrip("/").split("/")[-1]
        items.append({
            "id": f"reuters:{slug}",
            "source": "reuters",
            "source_label": "Reuters",
            "title": title,
            "summary": "",
            "url": loc,
            "published": pub_date,
            "symbols": [],
            "tags": display_tags,
            "engagement": {},
            "media": [{"type": "photo", "url": image_url}] if image_url else [],
            "meta": {"lastmod": url_el.findtext("sm:lastmod", "", _NS)},
            "fetched_at": now,
        })

    return items


def run_reuters(config: dict, max_pages: int = 3) -> list[dict[str, Any]]:
    """
    Poll Reuters news sitemap for new market-relevant articles.

    Returns normalised feed items.
    """
    reuters_config = config.get("reuters", {})
    state_path = Path(reuters_config.get("state_path", "var/news/reuters/state.json"))
    output_dir = Path(reuters_config.get("output_path", "var/news/reuters"))

    state = read_json(state_path, {"last_seen_urls": [], "last_poll": None})
    if not isinstance(state, dict):
        state = {"last_seen_urls": [], "last_poll": None}
    seen_urls = set(state.get("last_seen_urls", []))

    logger.info("reuters_poll_started", max_pages=max_pages)

    try:
        sitemap_urls = _fetch_sitemap_index()
    except Exception as e:
        logger.error("reuters_index_failed", error=str(e))
        state.update({"last_poll": datetime.now(timezone.utc).isoformat(), "status": "error", "error": str(e)})
        atomic_write_json(state_path, state)
        return []

    pages_to_scan = min(max_pages, len(sitemap_urls))
    logger.info("reuters_scanning", pages=pages_to_scan)

    all_items: list[dict[str, Any]] = []
    for page_url in sitemap_urls[:pages_to_scan]:
        items = _parse_sitemap_page(page_url, seen_urls, reuters_config)
        all_items.extend(items)
        seen_urls.update(i["url"] for i in items)

    state["last_seen_urls"] = sorted(seen_urls)[-5000:]
    state["last_poll"] = datetime.now(timezone.utc).isoformat()
    state["status"] = "ok"
    state["error"] = None
    atomic_write_json(state_path, state)

    if all_items:
        output_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_file = output_dir / f"reuters_{today}.json"
        merge_items(daily_file, all_items)
        logger.info("reuters_poll_complete", new_items=len(all_items))
    else:
        logger.info("reuters_poll_complete", new_items=0)

    return all_items
