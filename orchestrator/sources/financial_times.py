"""Financial Times RSS feed parsing, URL canonicalisation, and item merging."""

from __future__ import annotations

import hashlib
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from uuid import uuid4

from bs4 import BeautifulSoup

from db import get_session
from logging_config import get_logger
from sources.archive_fo import ArchiveFoClient, validate_archive_capture
from sources.financial_times_repository import (
    create_capture_attempt,
    get_reusable_capture,
    insert_article_version_if_new,
    insert_ft_run,
    mark_capture_status,
    update_ft_run,
    upsert_article_observation,
)

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


# ---------------------------------------------------------------------------
# On-demand collection service
# ---------------------------------------------------------------------------

logger = get_logger("ft")


def _default_fetch(url: str) -> str:
    """Fetch RSS XML using httpx (default fetcher)."""
    import httpx

    response = httpx.get(url, timeout=30)
    response.raise_for_status()
    return response.text


def _extract_body_text(html: str) -> tuple[str, int]:
    """Extract article body text and word count from HTML."""
    soup = BeautifulSoup(html, "html.parser")
    article_el = soup.find("article") or soup.find("main")
    if article_el is None:
        return "", 0
    paragraphs = article_el.find_all("p")
    body_text = "\n\n".join(p.get_text(strip=True) for p in paragraphs)
    word_count = len(body_text.split())
    return body_text, word_count


def run_financial_times(
    config: dict,
    correlation_id: str,
    sections: tuple[str, ...] = ("homepage", "lex", "unhedged"),
    since: datetime | None = None,
    until: datetime | None = None,
    max_articles: int | None = None,
    ingest: bool = True,
    wait_for_capture: bool = True,
    refresh_existing: bool = False,
    raw_storage_path: str | None = None,
    fetch_fn=None,
    archive_client=None,
) -> dict:
    """Run Financial Times on-demand collection.

    Discovers articles from RSS feeds and optionally ingests them via
    archive.fo.  Returns a provenance bundle with per-article status.

    Parameters
    ----------
    config : dict
        Full application config (must contain ``financial_times`` key).
    correlation_id : str
        Caller-supplied correlation ID for tracing.
    sections : tuple[str, ...]
        Feed sections to discover (keys of ``config["financial_times"]["feeds"]``).
    since / until : datetime | None
        Time window filters on ``published_at``.
    max_articles : int | None
        Cap on articles to process.
    ingest : bool
        If True, submit articles to archive.fo for capture.
    wait_for_capture : bool
        If True, poll archive.fo until capture is ready.
    refresh_existing : bool
        If True, re-capture articles even if a reusable capture exists.
    raw_storage_path : str | None
        Override for on-disk raw HTML storage directory.
    fetch_fn : callable | None
        Injectable RSS fetcher ``(url: str) -> str``.  Defaults to httpx.
    archive_client : ArchiveFoClient | None
        Injectable archive client.  Defaults to constructing one from config.

    Returns
    -------
    dict
        ``run_id``, ``status``, ``correlation_id``, ``articles_discovered``,
        ``articles_captured``, ``articles_failed``, ``articles`` (list of
        per-article provenance dicts).
    """
    ft_config = config.get("financial_times", {})
    feeds = ft_config.get("feeds", {})
    raw_path = raw_storage_path or ft_config.get(
        "raw_storage_path", "/var/lib/trading-data/financial_times"
    )

    run_id = str(uuid4())

    # -- create run record --------------------------------------------------
    with get_session(config) as session:
        insert_ft_run(session, run_id, correlation_id, list(sections), since, until)

    # -- phase 1: RSS discovery ---------------------------------------------
    logger.info("ft_discovery_started", run_id=run_id, sections=list(sections))

    _fetch = fetch_fn or _default_fetch
    all_observations: list[list[FinancialTimesArticleObservation]] = []

    for section in sections:
        feed_url = feeds.get(section)
        if not feed_url:
            logger.warning("ft_feed_not_found", section=section)
            continue
        try:
            xml_text = _fetch(feed_url)
        except Exception as exc:
            logger.error("ft_rss_fetch_failed", section=section, error=str(exc))
            continue
        all_observations.append(parse_rss(xml_text, feed_id=section))

    candidates = merge_items(all_observations)

    # -- filter by time window ----------------------------------------------
    if since or until:
        filtered: list[FinancialTimesArticleCandidate] = []
        for candidate in candidates:
            pub = min(obs.published_at for obs in candidate.observations)
            if since and pub < since:
                continue
            if until and pub > until:
                continue
            filtered.append(candidate)
        candidates = filtered

    if max_articles is not None and len(candidates) > max_articles:
        candidates = candidates[:max_articles]

    logger.info("ft_discovery_complete", run_id=run_id, articles_found=len(candidates))

    # -- upsert observations ------------------------------------------------
    with get_session(config) as session:
        for candidate in candidates:
            obs = candidate.observations[0]
            upsert_article_observation(
                session,
                article_id=candidate.content_id,
                content_id=candidate.content_id,
                canonical_url=candidate.canonical_url,
                title=obs.title,
                description=obs.description,
                published_at=obs.published_at,
                feed_id=obs.feed_id,
                rss_payload=obs.rss_payload,
            )

    # -- phase 2: archive ingestion -----------------------------------------
    captured_count = 0
    failed_count = 0
    article_results: list[dict] = []

    if ingest:
        logger.info("ft_ingestion_started", run_id=run_id, articles=len(candidates))

        if archive_client is None:
            archive_client = ArchiveFoClient(
                archive_host=ft_config.get("archive_host", "https://archive.fo"),
                poll_interval=ft_config.get("poll_interval_seconds", 10),
                max_polls=ft_config.get("max_poll_attempts", 12),
            )

        with get_session(config) as session:
            for candidate in candidates:
                obs = candidate.observations[0]
                article_id = candidate.content_id
                requested_url = candidate.canonical_url

                # -- reuse existing capture? ---------------------------------
                if not refresh_existing:
                    existing = get_reusable_capture(
                        session, article_id, requested_url
                    )
                    if existing:
                        article_results.append(
                            {
                                "content_id": candidate.content_id,
                                "canonical_url": candidate.canonical_url,
                                "status": "reused",
                                "archive_url": existing.get("archive_url"),
                                "content_hash": existing.get("raw_content_hash"),
                            }
                        )
                        captured_count += 1
                        continue

                # -- new capture attempt -------------------------------------
                capture = create_capture_attempt(
                    session, article_id, requested_url
                )
                capture_id = capture["capture_id"]

                try:
                    archive_url = archive_client.submit(requested_url)
                    mark_capture_status(
                        session,
                        capture_id,
                        "submitted",
                        archive_url=archive_url,
                    )

                    if wait_for_capture:
                        archive_url = archive_client.poll(archive_url)

                    html = archive_client.download(archive_url)
                    validation = validate_archive_capture(
                        html, expected_title=obs.title
                    )

                    if not validation.valid:
                        mark_capture_status(
                            session,
                            capture_id,
                            "invalid",
                            error_message=validation.reason,
                        )
                        failed_count += 1
                        article_results.append(
                            {
                                "content_id": candidate.content_id,
                                "canonical_url": candidate.canonical_url,
                                "status": "invalid",
                                "reason": validation.reason,
                            }
                        )
                        continue

                    # -- valid capture ---------------------------------------
                    content_hash = hashlib.sha256(
                        html.encode("utf-8")
                    ).hexdigest()

                    os.makedirs(raw_path, exist_ok=True)
                    raw_file = os.path.join(raw_path, f"{content_hash}.html")
                    with open(raw_file, "w", encoding="utf-8") as fh:
                        fh.write(html)

                    mark_capture_status(
                        session,
                        capture_id,
                        "captured",
                        archive_url=archive_url,
                        raw_capture_path=raw_file,
                        raw_content_hash=content_hash,
                    )

                    body_text, word_count = _extract_body_text(html)

                    version = insert_article_version_if_new(
                        session,
                        article_id=article_id,
                        capture_id=capture_id,
                        archive_url=archive_url,
                        captured_at=datetime.now(timezone.utc),
                        content_hash=content_hash,
                        title=validation.title or obs.title,
                        byline=validation.byline,
                        published_at=obs.published_at,
                        body_text=body_text,
                        word_count=word_count or validation.word_count,
                        raw_capture_path=raw_file,
                        extraction_status="ok",
                        parser_version="1.0",
                    )

                    captured_count += 1
                    article_results.append(
                        {
                            "content_id": candidate.content_id,
                            "canonical_url": candidate.canonical_url,
                            "status": "captured" if version else "duplicate",
                            "archive_url": archive_url,
                            "content_hash": content_hash,
                        }
                    )

                except Exception as exc:
                    logger.error(
                        "ft_capture_failed",
                        article_id=article_id,
                        error=str(exc),
                    )
                    mark_capture_status(
                        session, capture_id, "failed", error_message=str(exc)
                    )
                    failed_count += 1
                    article_results.append(
                        {
                            "content_id": candidate.content_id,
                            "canonical_url": candidate.canonical_url,
                            "status": "failed",
                            "error": str(exc),
                        }
                    )

    # -- finalise run -------------------------------------------------------
    if captured_count == 0 and failed_count > 0:
        overall_status = "failed"
    elif failed_count > 0:
        overall_status = "partial"
    else:
        overall_status = "completed"

    with get_session(config) as session:
        update_ft_run(
            session,
            run_id,
            overall_status,
            articles_discovered=len(candidates),
            articles_captured=captured_count,
            articles_failed=failed_count,
        )

    logger.info(
        "ft_run_complete",
        run_id=run_id,
        status=overall_status,
        discovered=len(candidates),
        captured=captured_count,
        failed=failed_count,
    )

    return {
        "run_id": run_id,
        "status": overall_status,
        "correlation_id": correlation_id,
        "articles_discovered": len(candidates),
        "articles_captured": captured_count,
        "articles_failed": failed_count,
        "articles": article_results,
    }


def resume_ft_captures(
    config: dict,
    correlation_id: str,
    archive_client=None,
) -> dict:
    """Resume all pending archive captures.

    Picks up queued / submitted / pending capture attempts and drives them
    through the archive.fo submit → poll → download → validate pipeline.

    Returns
    -------
    dict
        ``status``, ``captures_resumed``, ``captures_succeeded``,
        ``captures_failed``, ``results`` (per-capture list).
    """
    from sources.financial_times_repository import get_pending_captures

    ft_config = config.get("financial_times", {})
    raw_path = ft_config.get(
        "raw_storage_path", "/var/lib/trading-data/financial_times"
    )

    if archive_client is None:
        archive_client = ArchiveFoClient(
            archive_host=ft_config.get("archive_host", "https://archive.fo"),
            poll_interval=ft_config.get("poll_interval_seconds", 10),
            max_polls=ft_config.get("max_poll_attempts", 12),
        )

    with get_session(config) as session:
        pending = get_pending_captures(session)

    if not pending:
        return {
            "status": "completed",
            "captures_resumed": 0,
            "captures_succeeded": 0,
            "captures_failed": 0,
            "results": [],
        }

    succeeded = 0
    failed = 0
    results: list[dict] = []

    with get_session(config) as session:
        for capture in pending:
            capture_id = capture["capture_id"]
            requested_url = capture["requested_url"]
            article_id = capture["article_id"]

            try:
                archive_url = archive_client.submit(requested_url)
                mark_capture_status(
                    session, capture_id, "submitted", archive_url=archive_url
                )

                archive_url = archive_client.poll(archive_url)
                html = archive_client.download(archive_url)

                validation = validate_archive_capture(html)

                if not validation.valid:
                    mark_capture_status(
                        session,
                        capture_id,
                        "invalid",
                        error_message=validation.reason,
                    )
                    failed += 1
                    results.append(
                        {
                            "capture_id": capture_id,
                            "status": "invalid",
                            "reason": validation.reason,
                        }
                    )
                    continue

                content_hash = hashlib.sha256(
                    html.encode("utf-8")
                ).hexdigest()

                os.makedirs(raw_path, exist_ok=True)
                raw_file = os.path.join(raw_path, f"{content_hash}.html")
                with open(raw_file, "w", encoding="utf-8") as fh:
                    fh.write(html)

                mark_capture_status(
                    session,
                    capture_id,
                    "captured",
                    archive_url=archive_url,
                    raw_capture_path=raw_file,
                    raw_content_hash=content_hash,
                )

                body_text, word_count = _extract_body_text(html)

                version = insert_article_version_if_new(
                    session,
                    article_id=article_id,
                    capture_id=capture_id,
                    archive_url=archive_url,
                    captured_at=datetime.now(timezone.utc),
                    content_hash=content_hash,
                    title=validation.title,
                    byline=validation.byline,
                    published_at=None,
                    body_text=body_text,
                    word_count=word_count or validation.word_count,
                    raw_capture_path=raw_file,
                    extraction_status="ok",
                    parser_version="1.0",
                )

                succeeded += 1
                results.append(
                    {
                        "capture_id": capture_id,
                        "status": "captured" if version else "duplicate",
                        "archive_url": archive_url,
                        "content_hash": content_hash,
                    }
                )

            except Exception as exc:
                logger.error(
                    "ft_resume_capture_failed",
                    capture_id=capture_id,
                    error=str(exc),
                )
                mark_capture_status(
                    session, capture_id, "failed", error_message=str(exc)
                )
                failed += 1
                results.append(
                    {
                        "capture_id": capture_id,
                        "status": "failed",
                        "error": str(exc),
                    }
                )

    overall = (
        "completed"
        if failed == 0
        else ("partial" if succeeded > 0 else "failed")
    )
    return {
        "status": overall,
        "captures_resumed": len(pending),
        "captures_succeeded": succeeded,
        "captures_failed": failed,
        "results": results,
    }
