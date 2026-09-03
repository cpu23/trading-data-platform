"""Reuters news sitemap poller — discovers and normalises market-relevant articles."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from http_client import get_shared_client, make_request
from logging_config import get_logger
from sources.news_result import NewsCollectionResult, NewsPublication
from sources.news_storage import atomic_write_json, read_json

logger = get_logger("reuters")

REUTERS_SITEMAP_INDEX = (
    "https://www.reuters.com/arc/outboundfeeds/news-sitemap-index/?outputType=xml"
)
# Provider contract: child sitemap URLs come from the (upstream-controlled)
# index payload, so they are pinned to the canonical Reuters host and their
# bodies are size-bounded.
REUTERS_SITEMAP_HOST = "www.reuters.com"
MAX_SITEMAP_BYTES = 10_000_000

_NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "news": "http://www.google.com/schemas/sitemap-news/0.9",
    "image": "http://www.google.com/schemas/sitemap-image/1.1",
}

_LANG_PREFIXES = ("es", "fr", "pt", "de", "it", "ja", "zh", "ar", "ko")


class _SitemapPageFetchError(Exception):
    def __init__(self, cause: Exception):
        super().__init__(type(cause).__name__)
        self.error_type = type(cause).__name__


class SitemapSchemaError(Exception):
    """Raised when well-formed XML is not the expected sitemap document type."""


def _require_sitemap_root(root: ET.Element, expected_local_name: str) -> None:
    expected_tag = f"{{{_NS['sm']}}}{expected_local_name}"
    if root.tag != expected_tag:
        raise SitemapSchemaError("unexpected sitemap root")


def _extract_section(url: str) -> str:
    m = re.match(r"https://www\.reuters\.com/([^/]+)/", url)
    return m.group(1) if m else ""


def _is_markets_relevant(
    url: str, title: str, keywords_xml: str, config: dict
) -> tuple[bool, list[str]]:
    section = _extract_section(url)
    markets_sections = set(
        config.get(
            "markets_sections",
            {
                "markets",
                "business",
                "finance",
                "economy",
                "companies",
                "breakingviews",
                "wealth",
            },
        )
    )
    if section in markets_sections:
        return True, [f"section:{section}"]

    combined = f"{title} {keywords_xml}".lower()
    keywords = config.get("markets_keywords", [])
    matched = [kw for kw in keywords if kw in combined]
    return bool(matched), matched


def _fetch_bytes(
    url: str,
    *,
    headers: dict | None = None,
    timeout: float = 30.0,
    cap: int = MAX_SITEMAP_BYTES,
) -> bytes:
    """Fetch a sitemap body through the shared resolve-and-pin transport.

    Every real send re-resolves the host and requires all DNS answers to be
    globally routable (fail-closed on rebinding), pins the connection to a
    validated address, and re-validates each redirect hop; the body is hard
    size-bounded. Tests inject fake fetchers here to exercise parsing.
    """
    resp = make_request(
        "GET",
        url,
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
        client=get_shared_client(),
        max_response_bytes=cap,
    )
    resp.raise_for_status()
    return resp.content


def _validated_sitemap_url(url: str) -> str:
    """Validate a child sitemap URL: public HTTPS and the canonical Reuters
    host (the provider contract for index payloads).

    Shape checks (scheme, embedded credentials, port) and the host pin run
    here; DNS/rebinding validation is enforced at send time by the pinned
    public-only transport (every send re-resolves and requires all answers
    to be globally routable, and redirect hops re-enter the transport).
    IP-literal child URLs are classified here so a private address is
    rejected before any fetch.
    """
    import ipaddress

    from contracts.outbound_security import (
        OutboundSecurityError,
        is_public_address,
        parse_origin,
        validate_public_url,
    )

    try:
        normalized = validate_public_url(url, resolve=False)
    except OutboundSecurityError as exc:
        raise ValueError(f"invalid sitemap URL ({exc})") from exc
    origin = parse_origin(normalized)
    try:
        literal = ipaddress.ip_address(origin.host)
    except ValueError:
        literal = None
    if literal is not None and not is_public_address(literal):
        raise ValueError(
            f"invalid sitemap URL (hostname resolves to a non-public address ({literal}))"
        )
    if origin.host != REUTERS_SITEMAP_HOST:
        raise ValueError(
            f"sitemap URL must be on {REUTERS_SITEMAP_HOST}, got {origin.host}"
        )
    return normalized


def _fetch_sitemap_index() -> list[str]:
    """Fetch the canonical Reuters sitemap index through the pinned
    public-only transport (per-hop validation, bounded body)."""
    xml_data = _fetch_bytes(
        REUTERS_SITEMAP_INDEX,
        headers={"User-Agent": "TradingResearchSystem/1.0"},
    )
    root = ET.fromstring(xml_data)
    _require_sitemap_root(root, "sitemapindex")
    return [
        sm.findtext("sm:loc", "", _NS)
        for sm in root.findall("sm:sitemap", _NS)
        if sm.findtext("sm:loc", "", _NS)
    ]


def _parse_sitemap_page(
    url: str, seen_urls: set[str], config: dict
) -> list[dict[str, Any]]:
    """Fetch a sitemap page and return normalised feed items.

    The child URL is upstream-controlled (came from the index payload), so it
    is validated against the public-origin policy AND bound to the canonical
    Reuters host before the pinned transport fetches it with a body cap.
    """
    try:
        normalized_url = _validated_sitemap_url(url)
    except ValueError as exc:
        raise _SitemapPageFetchError(exc) from exc
    try:
        xml_data = _fetch_bytes(
            normalized_url,
            headers={"User-Agent": "TradingResearchSystem/1.0"},
        )
    except Exception as exc:
        raise _SitemapPageFetchError(exc) from exc

    root = ET.fromstring(xml_data)
    _require_sitemap_root(root, "urlset")
    items = []
    now = datetime.now(UTC).isoformat()

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
        display_tags = [
            t.replace("section:", "") for t in matched if not t.startswith("section:")
        ]
        slug = loc.rstrip("/").split("/")[-1]
        items.append(
            {
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
            }
        )

    return items


def _page_error_context(url: str, error_type: str) -> str:
    """Describe a page failure without persisting query strings or exception details."""
    parsed = urlsplit(url)
    safe_url = urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", ""))
    return f"Reuters sitemap page failed at {safe_url}: {error_type}"


def run_reuters(config: dict, max_pages: int = 3) -> NewsCollectionResult:
    """
    Poll Reuters news sitemap for new market-relevant articles.

    Returns a typed outcome; failed pages may coexist with successful-page items.
    """
    reuters_config = config.get("reuters", {})
    state_path = Path(reuters_config.get("state_path", "var/news/reuters/state.json"))
    output_dir = Path(reuters_config.get("output_path", "var/news/reuters"))

    state = read_json(state_path, {"last_seen_urls": [], "last_poll": None})
    if not isinstance(state, dict):
        state = {"last_seen_urls": [], "last_poll": None}
    stored_urls = state.get("last_seen_urls", [])
    if not isinstance(stored_urls, list):
        stored_urls = []
    seen_urls = {url for url in stored_urls if isinstance(url, str)}

    logger.info("reuters_poll_started", max_pages=max_pages)

    try:
        sitemap_urls = _fetch_sitemap_index()
    except Exception as exc:
        error = f"Reuters sitemap index failed: {type(exc).__name__}"
        logger.error("reuters_index_failed", error=error)
        state.update(
            {
                "last_poll": datetime.now(UTC).isoformat(),
                "status": "error",
                "error": error,
            }
        )
        atomic_write_json(state_path, state)
        error_class = (
            "invalid_source_data"
            if isinstance(exc, (ET.ParseError, SitemapSchemaError))
            else "transient_source"
        )
        return NewsCollectionResult([], "error", error, error_class=error_class)

    pages_to_scan = min(max_pages, len(sitemap_urls))
    logger.info("reuters_scanning", pages=pages_to_scan)

    all_items: list[dict[str, Any]] = []
    page_errors: list[tuple[str, str]] = []
    for page_url in sitemap_urls[:pages_to_scan]:
        try:
            items = _parse_sitemap_page(page_url, seen_urls, reuters_config)
        except (ET.ParseError, SitemapSchemaError) as exc:
            error = _page_error_context(page_url, type(exc).__name__)
            logger.warning("reuters_sitemap_parse_failed", error=error)
            page_errors.append((error, "invalid_source_data"))
            continue
        except _SitemapPageFetchError as exc:
            error = _page_error_context(page_url, exc.error_type)
            logger.warning("reuters_sitemap_fetch_failed", error=error)
            page_errors.append((error, "transient_source"))
            continue
        all_items.extend(items)
        seen_urls.update(i["url"] for i in items)

    if page_errors:
        failure_state = dict(state)
        failure_state["last_seen_urls"] = sorted(
            url for url in stored_urls if isinstance(url, str)
        )
        failure_state["last_poll"] = datetime.now(UTC).isoformat()
        failure_state["status"] = "error"
        failure_state["error"] = "; ".join(error for error, _class in page_errors)
        atomic_write_json(state_path, failure_state)
        logger.info("reuters_poll_complete", new_items=len(all_items))
        error_class = (
            "invalid_source_data"
            if any(
                error_class == "invalid_source_data"
                for _error, error_class in page_errors
            )
            else "transient_source"
        )
        return NewsCollectionResult(
            all_items,
            "error",
            failure_state["error"],
            error_class=error_class,
        )

    candidate_state = dict(state)
    candidate_state["last_seen_urls"] = sorted(seen_urls)[-5000:]
    candidate_state["last_poll"] = datetime.now(UTC).isoformat()
    candidate_state["status"] = "ok"
    candidate_state["error"] = None
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    publication = NewsPublication(
        snapshot_path=output_dir / f"reuters_{today}.json",
        state_path=state_path,
        candidate_state=candidate_state,
    )

    if all_items:
        logger.info("reuters_poll_complete", new_items=len(all_items))
    else:
        logger.info("reuters_poll_complete", new_items=0)

    error = candidate_state["error"]
    return NewsCollectionResult(
        all_items, candidate_state["status"], error, publication
    )
