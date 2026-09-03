"""Bounded ingestion of publicly accessible issuer earnings transcripts.

Each configured issuer contributes a public page or feed (an IR events page,
an RSS/Atom press feed, a webcast listing). The collector discovers
transcript-text links on that page, then text transcripts are fetched with a
hard byte bound and normalized into source text, keeping speaker sections
("Operator:", "John Smith:") when the page structure exposes them.

Safety properties:
* every configured issuer origin goes through
  :func:`provider_origins.validate_configured_origin` (HTTPS + globally
  routable DNS, fail closed); every discovered link must be http(s) and
  every redirect hop is shape-validated and re-enters the resolve-and-pin
  public-only transport at send time;
* downloads are bounded (page bytes, document bytes), redirect chains are
  hop-capped, link/item discovery is capped per page and per issuer, and
  the issuer count itself is capped;
* record identity is deterministic (``document_id`` over source identity +
  provider-supplied source timestamp + URL) and content is deduplicated by
  content hash, so re-runs upsert to the same rows instead of duplicating;
* every record metadata distinguishes the source timestamp
  (``published_at``; feed-supplied times are marked
  ``published_at_inferred: false``, page-inferred and acquisition-time
  fallbacks true), the discovery acquisition time (``source_observed_at``)
  and content availability (``fetched_at`` / ``available_at`` /
  ``acquired_at``);
* malformed provider data (unparseable feed, empty transcript page) fails
  explicitly per item/issuer and is never coerced into fake content; a page
  that simply has no transcript items yet is valid empty output.

Configuration lives under ``config["collectors"]["issuer_transcripts"]``::

    issuer_transcripts:
      schedule: "0 7 * * *"
      max_issuers: 20
      max_page_bytes: 2000000
      max_document_bytes: 25000000
      max_links_per_page: 50
      max_records_per_issuer: 25
      max_redirects: 5
      timeout_seconds: 30
      user_agent: "TradingDataTranscriptCollector/1.0 (research@trading-data-platform.local)"
      issuers:
        - institution: "Example Corp"
          ticker: "EXMP"
          url: "https://example.com/investor-relations/events"
          document_type: "earnings_transcript"
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from collectors.base import CollectorNoData, CollectorSetupRequired
from contracts.outbound_security import OutboundSecurityError, resolve_redirect_url
from errors import InvalidSourceData
from http_client import ResponseBodyTooLarge, make_request
from http_errors import safe_error_message
from logging_config import get_logger
from provider_origins import validate_configured_origin

logger = get_logger("collector.issuer_transcripts")

SOURCE_ID = "issuer_transcripts"
DEFAULT_SCHEDULE = "0 7 * * *"
DEFAULT_USER_AGENT = (
    "TradingDataTranscriptCollector/1.0 (research@trading-data-platform.local)"
)
DEFAULT_MAX_ISSUERS = 20
DEFAULT_MAX_PAGE_BYTES = 2_000_000
DEFAULT_MAX_DOCUMENT_BYTES = 25_000_000
DEFAULT_MAX_LINKS_PER_PAGE = 50
DEFAULT_MAX_RECORDS_PER_ISSUER = 25
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_TIMEOUT_SECONDS = 30.0

_PAGE_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "application/rss+xml;q=0.9,text/plain;q=0.8,*/*;q=0.8"
)

_TRANSCRIPT_KEYWORDS = (
    "transcript",
    "earnings call",
    "conference call",
    "earnings-call",
    "prepared remarks",
)
_EARNINGS_CONTEXT_KEYWORDS = ("earnings", "financial results")
_MIN_TEXT_SPEAKERS = 2
_Q4_TIMEZONE_NAMES = {
    "PT": "America/Los_Angeles",
    "PST": "America/Los_Angeles",
    "MT": "America/Denver",
    "MST": "America/Denver",
    "CT": "America/Chicago",
    "CST": "America/Chicago",
    "ET": "America/New_York",
    "EST": "America/New_York",
    "AT": "America/Halifax",
    "AST": "America/Halifax",
    "GMT": "Etc/GMT",
    "BST": "Europe/London",
}

_SPEAKER_RE = re.compile(
    r"^(?:[A-Z][A-Za-z0-9.\'\-\s]{1,40}|(?:Operator|Executive|Analyst|Question-[Aa]nd-[Aa]nswer Session)):\s*(.*)$"
)
_QUARTER_RE = re.compile(r"\b(Q[1-4]|FY\s*\d{2,4}|[12]\d{3})\b", re.IGNORECASE)
_NON_SPEAKER_LABELS = frozenset(
    {
        "http",
        "https",
        "cautionary statement",
        "forward-looking statements",
        "safe harbor",
        "disclosure",
        "source",
        "view source version on",
        "contacts",
        "media contact",
        "investor contact",
        "note",
    }
)
_COLLAPSE_WS_RE = re.compile(r"[ \t\r\f\v]+")


def _safe_error(exc: BaseException) -> str:
    return safe_error_message(exc)


def _clean_title(text: str) -> str:
    collapsed = _COLLAPSE_WS_RE.sub(" ", text).strip()
    return " ".join(line.strip() for line in collapsed.splitlines() if line.strip())


def _bounded_int(val: Any, default: int, min_val: int, max_val: int) -> int:
    try:
        n = int(val)
        return max(min_val, min(n, max_val))
    except (TypeError, ValueError):
        return default


def _bounded_float(val: Any, default: float, min_val: float, max_val: float) -> float:
    try:
        n = float(val)
        return max(min_val, min(n, max_val))
    except (TypeError, ValueError):
        return default


def _section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    collectors = config.get("collectors")
    if not isinstance(collectors, Mapping):
        raise CollectorSetupRequired("collector configuration is missing")
    section = collectors.get("issuer_transcripts")
    if not isinstance(section, Mapping):
        raise CollectorSetupRequired("issuer_transcripts is not configured")
    return section


def _looks_like_feed(lead: bytes, content_type: str) -> bool:
    if "rss" in content_type or "atom" in content_type or "xml" in content_type:
        return True
    return (
        lead.startswith(b"<?xml")
        or b"<rss" in lead
        or b"<feed" in lead
        or b"<rdf:RDF" in lead
    )


def _content_type(response: Any) -> str:
    raw = str(response.headers.get("content-type") or "").strip().lower()
    return raw.split(";")[0].strip()


def _parse_published(raw: str | None) -> datetime | None:
    if not raw:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    try:
        dt = parsedate_to_datetime(cleaned)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None


def _feed_link(item: ElementTree.Element) -> str:
    for child in item:
        tag = child.tag.split("}")[-1].lower() if "}" in child.tag else child.tag.lower()
        if tag == "link":
            href = child.attrib.get("href")
            if href and str(href).strip():
                return str(href).strip()
            text = (child.text or "").strip()
            if text:
                return text
        elif tag == "guid" and child.attrib.get("isPermaLink", "").lower() == "true":
            text = (child.text or "").strip()
            if text.startswith(("http://", "https://")):
                return text
    return ""


def _parse_feed(page_bytes: bytes, page_url: str) -> list[dict]:
    try:
        root = ElementTree.fromstring(page_bytes)
    except ElementTree.ParseError as exc:
        raise InvalidSourceData(f"malformed XML feed: {exc}") from exc
    items: list[ElementTree.Element] = []
    for elem in root.iter():
        tag = elem.tag.split("}")[-1].lower() if "}" in elem.tag else elem.tag.lower()
        if tag in ("item", "entry"):
            items.append(elem)

    candidates: list[dict] = []
    for item in items:
        link = _feed_link(item)
        title = ""
        published_raw = None
        for child in item:
            tag = child.tag.split("}")[-1].lower() if "}" in child.tag else child.tag.lower()
            if tag == "title" and child.text:
                title = child.text.strip()
            elif tag in ("pubdate", "published", "updated", "date") and child.text:
                published_raw = child.text.strip()
        published = _parse_published(published_raw)
        if not link:
            continue
        candidates.append(
            {
                "title": _clean_title(title or link),
                "url": urljoin(page_url, link),
                "published": published,
                "hint": None,
            }
        )
    return candidates


def _q4_timestamp(item: Mapping[str, Any]) -> datetime | None:
    raw = str(item.get("StartDate") or "").strip()
    if not raw:
        return None
    try:
        local = datetime.strptime(raw, "%m/%d/%Y %H:%M:%S")
    except ValueError:
        return None
    timezone_name = _Q4_TIMEZONE_NAMES.get(str(item.get("TimeZone") or "").strip())
    if timezone_name is None:
        return local.replace(tzinfo=UTC)
    return local.replace(tzinfo=ZoneInfo(timezone_name)).astimezone(UTC)


def _parse_q4_events(page_bytes: bytes, page_url: str, *, max_items: int) -> list[dict]:
    try:
        payload = json.loads(page_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidSourceData(f"malformed Q4 event feed: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise InvalidSourceData("Q4 event feed must be an object")
    raw_items = payload.get("GetEventListResult")
    if not isinstance(raw_items, list):
        raise InvalidSourceData("Q4 event feed is missing GetEventListResult")

    candidates: list[dict] = []
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            raise InvalidSourceData("Q4 event feed contains a non-object item")
        title = _clean_title(str(raw.get("Title") or ""))
        webcast = str(raw.get("WebCastLink") or "").strip()
        haystack = f"{title} {webcast}".lower()
        if not title or not any(
            keyword in haystack for keyword in _EARNINGS_CONTEXT_KEYWORDS
        ):
            continue
        url = urljoin(page_url, webcast) if webcast else ""
        if not url:
            continue
        candidates.append(
            {
                "title": title,
                "url": url,
                "published": _q4_timestamp(raw),
                "hint": None,
            }
        )
    candidates.sort(
        key=lambda item: item["published"] or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return candidates[:max_items]


def _link_score(url: str, title: str) -> int:
    haystack = f"{url} {title}".lower()
    transcript_matches = sum(keyword in haystack for keyword in _TRANSCRIPT_KEYWORDS)
    has_earnings_context = any(
        keyword in haystack for keyword in _EARNINGS_CONTEXT_KEYWORDS
    )
    if "press release" in haystack and not transcript_matches:
        return 0
    if not (transcript_matches or has_earnings_context):
        return 0
    score = transcript_matches * 2
    score += 2 if has_earnings_context else 0
    if _QUARTER_RE.search(haystack):
        score += 1
    return score


def _parse_html_links(
    html_bytes: bytes, page_url: str, *, max_links: int
) -> list[dict]:
    try:
        soup = BeautifulSoup(html_bytes, "lxml")
    except Exception as exc:  # noqa: BLE001
        raise InvalidSourceData(f"malformed HTML page: {exc}") from exc
    seen_urls: set[str] = set()
    scored: list[tuple[int, str, str]] = []
    for anchor in soup.find_all("a", href=True):
        raw_href = str(anchor.get("href") or "").strip()
        if not raw_href or raw_href.startswith("#"):
            continue
        absolute = urljoin(page_url, raw_href)
        scheme = urlsplit(absolute).scheme.lower()
        if scheme != "https":
            continue
        dedupe_key = urlsplit(absolute)._replace(fragment="").geturl()
        if dedupe_key in seen_urls:
            continue
        seen_urls.add(dedupe_key)
        title = _clean_title(anchor.get_text(" ", strip=True) or absolute)
        if title.casefold().startswith(("share ", "top of page", "manage cookies")):
            continue
        score = _link_score(absolute, title)
        if score <= 0:
            continue
        scored.append((score, absolute, title))
    scored.sort(key=lambda entry: -entry[0])
    return [
        {"title": title, "url": url, "published": None, "hint": None}
        for _score, url, title in scored[:max_links]
    ]


def _plausible_speaker(label: str) -> bool:
    normalized = label.strip().lower().rstrip(".:")
    return normalized not in _NON_SPEAKER_LABELS and ":" not in normalized


def _extract_text_lines(soup: BeautifulSoup) -> list[str]:
    for tag in soup(["script", "style", "nav", "header", "footer", "form", "noscript"]):
        tag.decompose()
    blocks = soup.find_all(
        ["p", "div", "section", "article", "li", "h1", "h2", "h3", "h4", "h5", "h6"]
    )
    lines: list[str] = []
    for block in blocks:
        text = block.get_text(" ", strip=True)
        if not text:
            continue
        cleaned = _COLLAPSE_WS_RE.sub(" ", text).strip()
        if cleaned:
            lines.append(cleaned)
    return lines


def _normalize_transcript_text(html_bytes: bytes) -> tuple[str, list[str]]:
    try:
        soup = BeautifulSoup(html_bytes, "lxml")
    except Exception as exc:  # noqa: BLE001
        raise InvalidSourceData(f"malformed transcript HTML: {exc}") from exc
    raw_lines = _extract_text_lines(soup)
    if not raw_lines:
        return "", []

    speakers: list[str] = []
    normalized_lines: list[str] = []
    current_speaker: str | None = None

    for line in raw_lines:
        match = _SPEAKER_RE.match(line)
        if match:
            candidate_speaker = line.split(":", 1)[0].strip()
            remainder = match.group(1).strip()
            if _plausible_speaker(candidate_speaker):
                current_speaker = candidate_speaker
                if current_speaker not in speakers:
                    speakers.append(current_speaker)
                if remainder:
                    normalized_lines.append(f"{current_speaker}: {remainder}")
                else:
                    normalized_lines.append(f"{current_speaker}:")
                continue
        if current_speaker:
            normalized_lines.append(line)
        else:
            normalized_lines.append(line)

    content = "\n\n".join(normalized_lines).strip()
    return content, speakers


def _document_id(
    *,
    institution: str,
    document_type: str,
    published_at: datetime | None,
    url: str,
    state: str | None = None,
) -> str:
    inst_slug = re.sub(r"[^a-z0-9]+", "-", institution.lower()).strip("-")
    if state:
        return f"{SOURCE_ID}:{inst_slug}:{state}:{hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]}"
    ts = published_at.strftime("%Y%m%d") if published_at else "undated"
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return f"{SOURCE_ID}:{inst_slug}:{document_type}:{ts}:{url_hash}"


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _iso(when: datetime) -> str:
    return when.isoformat()


def _fetch_bounded(
    url: str,
    *,
    headers: dict[str, str],
    max_bytes: int,
    timeout: float,
    correlation_id: str,
    max_redirects: int,
) -> tuple[Any, str]:
    current_url = url
    for _hop in range(max_redirects + 1):
        try:
            response = make_request(
                "GET",
                current_url,
                headers=headers,
                timeout=timeout,
                max_bytes=max_bytes,
                follow_redirects=False,
            )
        except ResponseBodyTooLarge as exc:
            raise InvalidSourceData(
                f"payload from {current_url} exceeded {max_bytes} bytes"
            ) from exc
        except Exception as exc:
            raise InvalidSourceData(f"failed to fetch {current_url}: {_safe_error(exc)}") from exc

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            if not location:
                raise InvalidSourceData(f"redirect without Location header from {current_url}")
            try:
                current_url = resolve_redirect_url(current_url, location)
            except OutboundSecurityError as exc:
                raise InvalidSourceData(f"redirect target rejected: {_safe_error(exc)}") from exc
            continue

        if response.status_code != 200:
            raise InvalidSourceData(
                f"HTTP {response.status_code} fetching {current_url}"
            )
        return response, current_url

    raise InvalidSourceData(f"too many redirects fetching {url}")


class IssuerTranscriptsCollector:
    source_id = SOURCE_ID

    def collect(self, config: Mapping[str, Any], correlation_id: str) -> list[dict]:
        section = _section(config)
        issuers = section.get("issuers") or []
        if not issuers:
            raise CollectorSetupRequired("No issuer transcript sources are configured")
        max_issuers = _bounded_int(
            section.get("max_issuers"), DEFAULT_MAX_ISSUERS, 1, 200
        )
        acquired_at = datetime.now(UTC)
        seen_ids: set[str] = set()
        seen_hashes: set[str] = set()
        records: list[dict] = []
        failures: list[dict] = []

        for issuer in issuers[:max_issuers]:
            try:
                records.extend(
                    self._collect_issuer(
                        issuer,
                        section,
                        correlation_id,
                        acquired_at,
                        seen_ids,
                        seen_hashes,
                        failures,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    {
                        "institution": str(issuer.get("institution") or issuer.get("ticker") or "unknown"),
                        "error": _safe_error(exc),
                    }
                )
                logger.error(
                    "issuer_transcripts_issuer_failed",
                    institution=issuer.get("institution"),
                    error=_safe_error(exc),
                    correlation_id=correlation_id,
                )

        if not records and failures:
            raise CollectorNoData(
                f"Failed to ingest transcripts for {len(failures)} configured issuers: {failures[0]['error']}"
            )
        return records

    def _collect_issuer(
        self,
        issuer: Mapping[str, Any],
        section: Mapping[str, Any],
        correlation_id: str,
        acquired_at: datetime,
        seen_ids: set[str],
        seen_hashes: set[str],
        failures: list[dict],
    ) -> list[dict]:
        institution = _clean_title(
            str(issuer.get("institution") or issuer.get("ticker") or "unknown")
        )
        page_url = validate_configured_origin(
            issuer.get("url"), section, label="issuer_transcripts page"
        )
        document_type = _clean_title(
            str(issuer.get("document_type") or "earnings_transcript")
        )
        headers = self._headers(section, issuer)
        page_timeout = _bounded_float(
            section.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS, 5.0, 600.0
        )
        max_page_bytes = _bounded_int(
            section.get("max_page_bytes"),
            DEFAULT_MAX_PAGE_BYTES,
            64 * 1024,
            50 * 1024 * 1024,
        )
        max_document_bytes = _bounded_int(
            section.get("max_document_bytes"),
            DEFAULT_MAX_DOCUMENT_BYTES,
            64 * 1024,
            50 * 1024 * 1024,
        )
        max_redirects = _bounded_int(
            section.get("max_redirects"), DEFAULT_MAX_REDIRECTS, 0, 10
        )
        max_links = _bounded_int(
            section.get("max_links_per_page"), DEFAULT_MAX_LINKS_PER_PAGE, 1, 500
        )
        max_records = _bounded_int(
            section.get("max_records_per_issuer"),
            DEFAULT_MAX_RECORDS_PER_ISSUER,
            1,
            500,
        )
        observed_at = datetime.now(UTC)

        response, final_page_url = _fetch_bounded(
            page_url,
            headers=headers,
            max_bytes=max_page_bytes,
            timeout=page_timeout,
            correlation_id=correlation_id,
            max_redirects=max_redirects,
        )
        kind = str(issuer.get("kind") or "").strip().lower()
        if kind == "q4_events":
            items = _parse_q4_events(
                response.content, final_page_url, max_items=max_links
            )
        elif _looks_like_feed(
            response.content.lstrip(b"\xef\xbb\xbf")[:512].lstrip().lower(),
            _content_type(response),
        ):
            items = _parse_feed(response.content, final_page_url)
        else:
            items = _parse_html_links(
                response.content, final_page_url, max_links=max_links
            )
        items = items[:max_records]

        issuer_records: list[dict] = []
        for item in items:
            try:
                record = self._build_record(
                    item,
                    issuer,
                    institution,
                    document_type,
                    headers,
                    page_timeout,
                    max_document_bytes,
                    max_redirects,
                    correlation_id,
                    observed_at,
                    acquired_at,
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    {
                        "institution": institution,
                        "item": item.get("url", ""),
                        "error": _safe_error(exc),
                    }
                )
                logger.error(
                    "issuer_transcript_item_failed",
                    institution=institution,
                    item=item.get("url"),
                    error_type=type(exc).__name__,
                    correlation_id=correlation_id,
                )
                continue
            if record is None:
                continue
            content_hash = record["metadata"].get("content_hash")
            if record["document_id"] in seen_ids or (
                content_hash and content_hash in seen_hashes
            ):
                continue
            seen_ids.add(record["document_id"])
            if content_hash:
                seen_hashes.add(content_hash)
            issuer_records.append(record)
        return issuer_records

    @staticmethod
    def _headers(
        section: Mapping[str, Any], issuer: Mapping[str, Any]
    ) -> dict[str, str]:
        headers = {
            "User-Agent": str(
                section.get("user_agent")
                or issuer.get("user_agent")
                or DEFAULT_USER_AGENT
            ),
            "Accept": _PAGE_ACCEPT,
        }
        section_headers = section.get("headers")
        if isinstance(section_headers, Mapping):
            headers.update({str(k): str(v) for k, v in section_headers.items()})
        issuer_headers = issuer.get("headers")
        if isinstance(issuer_headers, Mapping):
            headers.update({str(k): str(v) for k, v in issuer_headers.items()})
        return headers

    def _build_record(
        self,
        item: dict,
        issuer: Mapping[str, Any],
        institution: str,
        document_type: str,
        headers: dict,
        page_timeout: float,
        max_document_bytes: int,
        max_redirects: int,
        correlation_id: str,
        observed_at: datetime,
        acquired_at: datetime,
    ) -> dict | None:
        url = item["url"]
        title = _clean_title(item["title"] or url)
        published = item.get("published")
        return self._text_record(
            url,
            title,
            published,
            issuer,
            institution,
            document_type,
            headers,
            page_timeout,
            max_document_bytes,
            max_redirects,
            correlation_id,
            observed_at,
            acquired_at,
        )

    def _text_record(
        self,
        url: str,
        title: str,
        published: datetime | None,
        issuer: Mapping[str, Any],
        institution: str,
        document_type: str,
        headers: dict,
        timeout: float,
        max_bytes: int,
        max_redirects: int,
        correlation_id: str,
        observed_at: datetime,
        acquired_at: datetime,
    ) -> dict:
        fetch_headers = dict(headers)
        fetch_headers["Accept"] = _PAGE_ACCEPT
        response, final_url = _fetch_bounded(
            url,
            headers=fetch_headers,
            max_bytes=max_bytes,
            timeout=timeout,
            correlation_id=correlation_id,
            max_redirects=max_redirects,
        )
        content, speakers = _normalize_transcript_text(response.content)
        if not content:
            raise InvalidSourceData(f"empty transcript text from {final_url}")

        published_at = published or observed_at
        c_hash = _content_hash(content)
        metadata = {
            "kind": "text",
            "ticker": issuer.get("ticker"),
            "source_url": url,
            "final_url": final_url,
            "content_hash": c_hash,
            "content_length": len(content),
            "speakers": speakers,
            "speaker_count": len(speakers),
            "published_at": _iso(published_at),
            "published_at_inferred": published is None,
            "source_observed_at": _iso(observed_at),
            "fetched_at": _iso(datetime.now(UTC)),
            "available_at": _iso(datetime.now(UTC)),
            "acquired_at": _iso(acquired_at),
        }
        return {
            "document_id": _document_id(
                institution=institution,
                document_type=document_type,
                published_at=published,
                url=url,
            ),
            "source": SOURCE_ID,
            "institution": institution,
            "document_type": document_type,
            "title": title,
            "published_at": published_at,
            "url": final_url,
            "content": content,
            "metadata": metadata,
            "acquired_at": acquired_at,
        }

    def health_check(self, config: Mapping[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        try:
            section = _section(config)
            issuers = section.get("issuers") or []
            if not issuers:
                return {
                    "healthy": False,
                    "state": "setup_required",
                    "message": "No issuer transcript sources configured",
                    "latency_ms": 0,
                }
            page_url = validate_configured_origin(
                issuers[0].get("url"), section, label="issuer_transcripts page"
            )
            response = make_request(
                "GET",
                page_url,
                headers=self._headers(section, issuers[0]),
                timeout=10.0,
                max_bytes=100000,
            )
            return {
                "healthy": response.status_code == 200,
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "healthy": False,
                "error": _safe_error(exc),
                "latency_ms": int((time.monotonic() - started) * 1000),
            }

    @staticmethod
    def fingerprint_inputs(section: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "issuers": [
                {
                    "institution": str(i.get("institution") or ""),
                    "ticker": str(i.get("ticker") or ""),
                    "url": str(i.get("url") or ""),
                }
                for i in (section.get("issuers") or [])
                if isinstance(i, Mapping)
            ]
        }

    @staticmethod
    def document_identity_fields() -> list[str]:
        return ["document_id"]
