"""Financial Times RSS feed parsing, URL canonicalisation, and item merging."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

# Pattern to extract content ID from FT URLs like /content/abc123
_CONTENT_ID_RE = re.compile(r"/content/([a-zA-Z0-9_-]+)")

# Query params to strip during canonicalisation
_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "igshid")


@dataclass(frozen=True)
class FinancialTimesFeedConfig:
    feed_id: str
    url: str


@dataclass(frozen=True)
class FinancialTimesArticleObservation:
    content_id: str
    canonical_url: str
    title: str
    description: str
    published_at: datetime
    feed_id: str
    rss_payload: dict


@dataclass
class FinancialTimesArticleCandidate:
    content_id: str
    canonical_url: str
    feed_ids: set[str] = field(default_factory=set)
    observations: list[FinancialTimesArticleObservation] = field(default_factory=list)


def canonicalise_ft_url(url: str) -> str:
    """Strip tracking query parameters from FT URLs, keeping the path."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    cleaned = {
        k: v for k, v in params.items()
        if not any(k.startswith(prefix) or k == prefix for prefix in _TRACKING_PREFIXES)
    }
    new_query = urlencode(cleaned, doseq=True) if cleaned else ""
    return urlunparse(parsed._replace(query=new_query))


def _extract_content_id(url: str) -> str | None:
    """Extract /content/<id> from an FT URL."""
    match = _CONTENT_ID_RE.search(url)
    return match.group(1) if match else None


def parse_rss(xml_text: str, feed_id: str) -> list[FinancialTimesArticleObservation]:
    """Parse RSS XML and extract FT article observations."""
    root = ET.fromstring(xml_text)
    observations: list[FinancialTimesArticleObservation] = []

    for item in root.iter("item"):
        link_el = item.find("link")
        title_el = item.find("title")
        desc_el = item.find("description")
        pub_el = item.find("pubDate")

        if link_el is None or link_el.text is None:
            continue

        link = link_el.text.strip()
        content_id = _extract_content_id(link)
        if content_id is None:
            continue  # non-FT link

        canonical_url = canonicalise_ft_url(link)
        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        description = desc_el.text.strip() if desc_el is not None and desc_el.text else ""

        published_at = datetime.now(timezone.utc)
        if pub_el is not None and pub_el.text:
            try:
                published_at = parsedate_to_datetime(pub_el.text.strip())
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass

        observations.append(FinancialTimesArticleObservation(
            content_id=content_id,
            canonical_url=canonical_url,
            title=title,
            description=description,
            published_at=published_at,
            feed_id=feed_id,
            rss_payload={
                "title": title,
                "description": description,
                "link": canonical_url,
            },
        ))

    return observations


def merge_items(
    feed_observations: list[list[FinancialTimesArticleObservation]],
) -> list[FinancialTimesArticleCandidate]:
    """Deduplicate by content_id, merging feed_ids and collecting all observations."""
    by_id: dict[str, FinancialTimesArticleCandidate] = {}

    for observations in feed_observations:
        for obs in observations:
            if obs.content_id in by_id:
                candidate = by_id[obs.content_id]
                candidate.feed_ids.add(obs.feed_id)
                candidate.observations.append(obs)
            else:
                by_id[obs.content_id] = FinancialTimesArticleCandidate(
                    content_id=obs.content_id,
                    canonical_url=obs.canonical_url,
                    feed_ids={obs.feed_id},
                    observations=[obs],
                )

    return list(by_id.values())
