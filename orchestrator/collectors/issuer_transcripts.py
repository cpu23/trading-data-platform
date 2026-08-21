"""Bounded ingestion of publicly accessible issuer earnings transcripts.

Each configured issuer contributes a public page or feed (an IR events page,
an RSS/Atom press feed, a webcast listing). The collector discovers
transcript-text links and public webcast audio links on that page, then:

* text transcripts are fetched with a hard byte bound and normalized into
  source text, keeping speaker sections ("Operator:", "John Smith:") when
  the page structure exposes them;
* webcast audio is fetched with a hard byte bound and transcribed fully
  locally through ``transcription.transcribe_audio`` (lazy faster-whisper,
  in-process, no shelling out and no uploads). When local transcription is
  impossible, an explicit ``setup_required``/``timeout``/``failed`` state
  record is produced instead of fabricated transcript content.

Safety properties:
* every configured issuer origin goes through
  :func:`provider_origins.validate_configured_origin` (HTTPS + globally
  routable DNS, fail closed); every discovered link must be http(s) and
  every redirect hop is shape-validated and re-enters the resolve-and-pin
  public-only transport at send time;
* downloads are bounded (page bytes, audio bytes), redirect chains are
  hop-capped, link/item discovery is capped per page and per issuer, and
  the issuer count itself is capped;
* record identity is deterministic (``document_id`` over source identity +
  provider-supplied source timestamp + URL) and content is deduplicated by
  content hash, so re-runs upsert to the same rows instead of duplicating;
* every record metadata distinguishes the source timestamp
  (``published_at``; feed/enclosure-supplied times are marked
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
      max_audio_bytes: 250000000
      max_links_per_page: 50
      max_records_per_issuer: 25
      max_redirects: 5
      timeout_seconds: 30
      audio_timeout_seconds: 300
      user_agent: "TradingDataTranscriptCollector/1.0 (research@trading-data-platform.local)"
      transcription:
        model: "small.en"  # size name or local model directory path
        device: "cpu"
        compute_type: "int8"
        beam_size: 5
        language: "en"
        max_audio_seconds: 7200
        timeout_seconds: 3600
        model_dir: "/var/lib/trading-data/news/models/whisper"
        vad_filter: true
        condition_on_previous_text: false
      issuers:
        - institution: "Example Corp"
          ticker: "EXMP"
          url: "https://example.com/investor-relations/events"
          document_type: "earnings_transcript"
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from collectors.base import CollectorNoData, CollectorSetupRequired
from contracts.outbound_security import OutboundSecurityError, resolve_redirect_url
from errors import InvalidSourceData
from http_client import make_request
from logging_config import get_logger
from provider_origins import validate_configured_origin
from transcription import (
    TranscriptionFailure,
    TranscriptionTimeout,
    TranscriptionUnavailable,
    audio_sha256,
    transcribe_audio,
    transcription_available,
)

logger = get_logger("collector.issuer_transcripts")

SOURCE_ID = "issuer_transcripts"
DEFAULT_SCHEDULE = "0 7 * * *"
DEFAULT_USER_AGENT = (
    "TradingDataTranscriptCollector/1.0 (research@trading-data-platform.local)"
)
DEFAULT_MAX_ISSUERS = 20
DEFAULT_MAX_PAGE_BYTES = 2_000_000
DEFAULT_MAX_DOCUMENT_BYTES = 25_000_000
DEFAULT_MAX_AUDIO_BYTES = 250_000_000
DEFAULT_MAX_LINKS_PER_PAGE = 50
DEFAULT_MAX_RECORDS_PER_ISSUER = 25
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_AUDIO_TIMEOUT_SECONDS = 300.0

_PAGE_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "application/rss+xml;q=0.9,text/plain;q=0.8,*/*;q=0.8"
)
_AUDIO_ACCEPT = "audio/*,application/octet-stream;q=0.9,*/*;q=0.8"

_AUDIO_EXTENSIONS = frozenset(
    {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac", ".wma", ".webm", ".mp4"}
)
_TRANSCRIPT_KEYWORDS = (
    "transcript",
    "earnings call",
    "conference call",
    "earnings-call",
    "prepared remarks",
)
_AUDIO_KEYWORDS = ("webcast", "audio", "listen", "replay", "podcast", "mp3")
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
_QUARTER_RE = re.compile(r"\bq(?:[1-4]|tr[ .]?[1-4])[^a-z0-9]{0,3}(?:20)?\d{2}\b")
_SPEAKER_LABEL_RE = re.compile(r"^([A-Z][A-Za-z .\-'&]{0,60}?):(\s+|$)")
_STRUCTURED_SPEAKER_RE = re.compile(r"^[A-Z][A-Za-z .\-'&]{0,60}:\s*$")
_NON_SPEAKER_LABELS = frozenset(
    {
        "about",
        "agenda",
        "announcement",
        "attention",
        "cautionary",
        "contact",
        "copyright",
        "disclaimer",
        "forward",
        "highlights",
        "important",
        "introduction",
        "key",
        "note",
        "notes",
        "overview",
        "press",
        "release",
        "summary",
        "transcript",
    }
)
_IGNORED_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "nav",
        "footer",
        "header",
        "form",
        "button",
        "iframe",
        "svg",
        "aside",
        "select",
        "option",
        "template",
    }
)
_BOILERPLATE_HINTS = (
    "menu",
    "nav",
    "cookie",
    "sidebar",
    "share",
    "related",
    "footer",
    "header",
    "advert",
    "promo",
    "disclaimer",
)
_FEED_CONTENT_TYPES = (
    "application/rss",
    "application/atom",
    "application/xml",
    "text/xml",
)
_MAX_TITLE_CHARS = 500
_MAX_ERROR_CHARS = 300


def _section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the issuer_transcripts config section or raise setup state."""
    collectors = config.get("collectors")
    if not isinstance(collectors, Mapping):
        raise CollectorSetupRequired("collector configuration is missing")
    section = collectors.get("issuer_transcripts")
    if not isinstance(section, Mapping):
        raise CollectorSetupRequired("issuer_transcripts is not configured")
    return section


def _bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return default
    return max(minimum, min(int(value), maximum))


def _bounded_float(value, default: float, minimum: float, maximum: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return default
    return max(minimum, min(float(value), maximum))


def _safe_error(exc: BaseException) -> str:
    """Bounded diagnostic text; never raw provider secrets."""
    return str(exc)[:_MAX_ERROR_CHARS] or type(exc).__name__


def _collapse_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _clean_title(value: str) -> str:
    title = _collapse_whitespace(value)
    return title[:_MAX_TITLE_CHARS] if len(title) > _MAX_TITLE_CHARS else title


def _content_length(response) -> int | None:
    raw = response.headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _fetch_bounded(
    url: str,
    *,
    headers: dict,
    max_bytes: int,
    timeout: float,
    correlation_id: str,
    max_redirects: int,
) -> tuple[object, str]:
    """Fetch one URL with byte, redirect and deadline bounds.

    Redirect hops are followed manually so the chain is hop-capped and every
    hop is shape-validated (https-only) here, then re-validated against DNS
    and pinned by the shared public-only transport at send time. Returns
    ``(response, final_url)``.
    """
    current = url
    for _hop in range(max_redirects + 1):
        response = make_request(
            "GET",
            current,
            headers=headers,
            timeout=timeout,
            correlation_id=correlation_id,
            follow_redirects=False,
        )
        if response.is_redirect:
            location = response.headers.get("location")
            response.close()
            if not location or not str(location).strip():
                raise InvalidSourceData(
                    f"redirect response without a Location header: {current}"
                )
            try:
                current = resolve_redirect_url(current, str(location))
            except OutboundSecurityError as exc:
                raise InvalidSourceData(
                    f"redirect target rejected by outbound policy: {exc}"
                ) from exc
            continue
        response.raise_for_status()
        declared = _content_length(response)
        if declared is not None and declared > max_bytes:
            response.close()
            raise InvalidSourceData(
                f"declared content of {declared} bytes exceeds the "
                f"{max_bytes}-byte limit: {current}"
            )
        content = response.content
        if len(content) > max_bytes:
            response.close()
            raise InvalidSourceData(
                f"downloaded content of {len(content)} bytes exceeds the "
                f"{max_bytes}-byte limit: {current}"
            )
        return response, current
    raise InvalidSourceData(f"too many redirects fetching {url}")


def _content_type(response) -> str:
    return str(response.headers.get("content-type", "") or "").split(";", 1)[0].lower()


def _looks_like_feed(sniff: bytes, content_type: str) -> bool:
    if any(content_type.startswith(prefix) for prefix in _FEED_CONTENT_TYPES):
        return True
    return (
        sniff.startswith(b"<?xml")
        or sniff.startswith(b"<rss")
        or sniff.startswith(b"<feed")
    )


def _feed_text(item, name: str) -> str | None:
    for tag in (name, f"{{http://www.w3.org/2005/Atom}}{name}"):
        node = item.find(tag)
        if node is not None and (node.text or "").strip():
            return (node.text or "").strip()
    return None


def _feed_link(item) -> str | None:
    plain = item.find("link")
    if plain is not None and (plain.text or "").strip():
        return (plain.text or "").strip()
    for node in item.findall("{http://www.w3.org/2005/Atom}link"):
        href = node.get("href")
        if href:
            return href
    return None


def _feed_date(item, names: tuple[str, ...]) -> datetime | None:
    for name in names:
        raw = _feed_text(item, name)
        if not raw:
            continue
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            try:
                when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return when
    return None


def _parse_feed(page_bytes: bytes, page_url: str) -> list[dict]:
    """Parse an RSS/Atom feed into bounded candidate items."""
    try:
        root = ElementTree.fromstring(page_bytes.lstrip(b"\xef\xbb\xbf"))
    except ElementTree.ParseError as exc:
        raise InvalidSourceData(f"malformed feed: {exc}") from exc
    items = root.findall(".//item") or root.findall(
        ".//{http://www.w3.org/2005/Atom}entry"
    )
    candidates: list[dict] = []
    for item in items:
        link = _feed_link(item)
        published = _feed_date(item, ("pubDate", "updated", "published"))
        title = _feed_text(item, "title")
        enclosures = item.findall("enclosure")
        if enclosures:
            for enclosure in enclosures:
                enc_url = (enclosure.get("url") or "").strip()
                if not enc_url:
                    continue
                enc_type = (enclosure.get("type") or "").lower()
                hint = (
                    "audio"
                    if enc_type.startswith("audio/") or _is_audio_url(enc_url)
                    else "text"
                )
                candidates.append(
                    {
                        "title": _clean_title(title or enc_url),
                        "url": urljoin(page_url, enc_url),
                        "published": published,
                        "hint": hint,
                    }
                )
            continue
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
    """Parse a Q4 public event feed into direct earnings-call audio items."""
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
        url = urljoin(page_url, webcast)
        if not webcast or not _is_audio_url(url):
            continue
        candidates.append(
            {
                "title": title,
                "url": url,
                "published": _q4_timestamp(raw),
                "hint": "audio",
            }
        )
    candidates.sort(
        key=lambda item: item["published"] or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return candidates[:max_items]


def _is_audio_url(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return any(path.endswith(ext) for ext in _AUDIO_EXTENSIONS)


def _link_score(url: str, title: str) -> int:
    """Score a discovered link as an earnings-transcript/webcast candidate."""
    haystack = f"{url} {title}".lower()
    is_audio = _is_audio_url(url)
    transcript_matches = sum(keyword in haystack for keyword in _TRANSCRIPT_KEYWORDS)
    has_earnings_context = any(
        keyword in haystack for keyword in _EARNINGS_CONTEXT_KEYWORDS
    )
    audio_matches = sum(keyword in haystack for keyword in _AUDIO_KEYWORDS)
    if "press release" in haystack and not (is_audio or transcript_matches):
        return 0
    if not (is_audio or transcript_matches or (has_earnings_context and audio_matches)):
        return 0
    score = (3 if is_audio else 0) + transcript_matches * 2
    score += 2 if has_earnings_context else 0
    if audio_matches:
        score += 2
    if _QUARTER_RE.search(haystack):
        score += 1
    return score


def _parse_html_links(
    html_bytes: bytes, page_url: str, *, max_links: int
) -> list[dict]:
    """Discover bounded transcript/audio candidates from an HTML issuer page."""
    try:
        soup = BeautifulSoup(html_bytes, "lxml")
    except Exception as exc:  # noqa: BLE001 - malformed page is an explicit failure
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


def _is_audio_link(item: dict) -> bool:
    return item.get("hint") == "audio" or _is_audio_url(item["url"])


def _plausible_speaker(label: str) -> bool:
    normalized = label.strip().lower().rstrip(".:")
    return normalized not in _NON_SPEAKER_LABELS and ":" not in normalized


def _detect_speaker(block, text: str) -> str | None:
    match = _SPEAKER_LABEL_RE.match(text)
    if match and _plausible_speaker(match.group(1)):
        return match.group(1)
    for element in block.find_all(["b", "strong", "span", "i"], limit=3):
        inner = element.get_text(" ", strip=True)
        if _STRUCTURED_SPEAKER_RE.fullmatch(inner) and _plausible_speaker(inner[:-1]):
            return inner[:-1]
    return None


def _is_boilerplate(block) -> bool:
    element_id = str(block.get("id") or "").lower()
    classes = " ".join(block.get("class") or []).lower()
    haystack = f"{element_id} {classes}"
    return any(hint in haystack for hint in _BOILERPLATE_HINTS)


def _extract_html_transcript(html_bytes: bytes, page_url: str) -> tuple[str, int, str]:
    """Normalize transcript HTML into text, preserving speaker sections."""
    try:
        soup = BeautifulSoup(html_bytes, "lxml")
    except Exception as exc:  # noqa: BLE001 - malformed page is an explicit failure
        raise InvalidSourceData(f"malformed transcript page: {exc}") from exc
    page_title = (
        _clean_title(soup.title.get_text(" ", strip=True)) if soup.title else ""
    )
    for tag in soup(_IGNORED_TAGS):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    block_names = ("p", "div", "li", "h1", "h2", "h3", "h4", "blockquote")
    lines: list[str] = []
    speakers: dict[str, int] = {}
    for block in root.find_all(block_names):
        if _is_boilerplate(block) or block.find(block_names):
            continue
        text = _collapse_whitespace(block.get_text(" ", strip=True))
        if not text:
            continue
        speaker = _detect_speaker(block, text)
        if speaker:
            speakers[speaker] = speakers.get(speaker, 0) + 1
        if not lines or lines[-1] != text:
            lines.append(text)
    if not lines:
        raise InvalidSourceData(f"no transcript text found on {page_url}")
    return "\n\n".join(lines), len(speakers), page_title


def _plain_speaker_label(line: str) -> str | None:
    match = _SPEAKER_LABEL_RE.match(line)
    if match and _plausible_speaker(match.group(1)):
        return match.group(1)
    if line.casefold() == "operator":
        return line
    parts = re.split(r"\s+[–—-]\s+", line, maxsplit=1)
    label = parts[0].strip()
    words = label.split()
    if len(parts) == 2 and 2 <= len(words) <= 8 and len(label) <= 80:
        if _plausible_speaker(label):
            return label
    return None


def _extract_plain_transcript(text: str) -> tuple[str, int, str]:
    """Normalize plain/PDF text while retaining structural speaker labels."""
    lines: list[str] = []
    speakers: dict[str, int] = {}
    for raw in text.splitlines():
        line = _collapse_whitespace(raw)
        if not line:
            continue
        speaker = _plain_speaker_label(line)
        if speaker:
            speakers[speaker] = speakers.get(speaker, 0) + 1
        lines.append(line)
    if not lines:
        raise InvalidSourceData("empty plain-text transcript")
    return "\n".join(lines), len(speakers), ""


def _extract_transcript(
    response, page_url: str, *, max_bytes: int
) -> tuple[str, int, str]:
    content_type = _content_type(response)
    if (
        content_type == "application/pdf"
        or urlsplit(page_url).path.lower().endswith(".pdf")
        or response.content.startswith(b"%PDF-")
    ):
        from investment_service import extract_document_text

        try:
            text = extract_document_text(
                response.content,
                Path(urlsplit(page_url).path).name or "transcript.pdf",
                content_type,
                max_bytes=max_bytes,
                ocr_page_budget=100,
                ocr_wall_seconds=120,
            )
        except ValueError as exc:
            raise InvalidSourceData(f"unextractable transcript PDF: {exc}") from exc
        return _extract_plain_transcript(text)
    if content_type.startswith("text/plain"):
        return _extract_plain_transcript(response.text)
    return _extract_html_transcript(response.content, page_url)


def _published_from_page(response) -> datetime | None:
    """Best-effort source timestamp from a transcript page itself."""
    try:
        soup = BeautifulSoup(response.content, "lxml")
    except Exception:  # noqa: BLE001 - probing is best effort
        return None
    for selector in (
        {"property": "article:published_time"},
        {"property": "og:published_time"},
        {"name": "date"},
        {"itemprop": "datePublished"},
    ):
        node = soup.find("meta", attrs=selector)
        if node and node.get("content"):
            raw = str(node.get("content")).strip()
            try:
                when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            return when
    node = soup.find("time")
    if node is not None:
        raw = (node.get("datetime") or node.get_text(" ", strip=True) or "").strip()
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return when
    return None


def _document_id(
    *,
    institution: str,
    document_type: str,
    published_at: datetime | None,
    url: str,
    state: str | None = None,
) -> str:
    """Deterministic identity; stable across runs for the same source item.

    Only provider-supplied publication times (RSS/audio enclosures) enter
    the identity; timestamps inferred from the page or from acquisition
    time are excluded so re-runs do not mint new rows, and failed-state
    records get their own namespace so they never clobber a real
    transcript.
    """
    published_part = published_at.isoformat() if published_at is not None else ""
    identity = "|".join((SOURCE_ID, institution, document_type, published_part, url))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    if state is not None:
        digest = hashlib.sha256(f"{digest}|state:{state}".encode()).hexdigest()
    return digest


def _iso(when: datetime) -> str:
    return when.isoformat()


def _audio_cache_path(
    config: Mapping[str, Any], url: str, published: datetime | None
) -> Path | None:
    model_dir = str(config.get("model_dir") or "").strip()
    if not model_dir:
        return None
    identity = f"{url}|{published.isoformat() if published else ''}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return Path(model_dir) / "transcript-cache" / f"{digest}.json"


def _read_audio_cache(path: Path | None, source_url: str) -> dict | None:
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning(
            "issuer_transcript_cache_read_failed",
            cache_path=str(path),
            error_type=type(exc).__name__,
        )
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or payload.get("source_url") != source_url
        or not isinstance(payload.get("content"), str)
        or not payload["content"].strip()
        or not isinstance(payload.get("transcription"), dict)
        or not isinstance(payload.get("audio_sha256"), str)
        or not isinstance(payload.get("audio_bytes"), int)
        or not isinstance(payload.get("final_url"), str)
        or urlsplit(payload["final_url"]).scheme not in ("http", "https")
        or not isinstance(payload.get("fetched_at"), str)
        or not isinstance(payload.get("available_at"), str)
    ):
        logger.warning(
            "issuer_transcript_cache_invalid",
            cache_path=str(path),
        )
        return None
    return payload


def _write_audio_cache(path: Path | None, payload: Mapping[str, Any]) -> None:
    if path is None:
        return
    temporary_path: str | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.stem}-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = handle.name
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning(
            "issuer_transcript_cache_write_failed",
            cache_path=str(path),
            error_type=type(exc).__name__,
        )
        if temporary_path:
            try:
                Path(temporary_path).unlink(missing_ok=True)
            except OSError:
                pass


class IssuerTranscriptsCollector:
    source_id = SOURCE_ID

    def collect(self, config, correlation_id):
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
        transcriber_ready = transcription_available()

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
                        transcriber_ready,
                        failures,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - per-issuer isolation
                failures.append(
                    {
                        "institution": str(
                            issuer.get("institution") or issuer.get("ticker") or ""
                        ),
                        "error": _safe_error(exc),
                    }
                )
                logger.error(
                    "issuer_transcript_issuer_failed",
                    issuer=issuer.get("institution") or issuer.get("ticker"),
                    error_type=type(exc).__name__,
                    correlation_id=correlation_id,
                )

        if not records and not failures:
            raise CollectorNoData(
                "Issuer transcript pages returned no transcript items"
            )
        if not records:
            raise CollectorNoData(
                "Issuer transcript collection produced no records",
                failed_issuers=failures,
            )
        logger.info(
            "issuer_transcripts_collection_completed",
            state="partial" if failures else "success",
            issuers_configured=len(issuers),
            issuers_processed=min(len(issuers), max_issuers),
            failed_items=failures,
            records=len(records),
            acquired_at=acquired_at.isoformat(),
            correlation_id=correlation_id,
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
        transcriber_ready: bool,
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
        audio_timeout = _bounded_float(
            section.get("audio_timeout_seconds"),
            DEFAULT_AUDIO_TIMEOUT_SECONDS,
            10.0,
            3600.0,
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
        max_audio_bytes = _bounded_int(
            section.get("max_audio_bytes"),
            DEFAULT_MAX_AUDIO_BYTES,
            1024 * 1024,
            2 * 1024 * 1024 * 1024,
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
        transcription_config = section.get("transcription") or {}
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
                    audio_timeout,
                    max_document_bytes,
                    max_audio_bytes,
                    max_redirects,
                    correlation_id,
                    observed_at,
                    acquired_at,
                    transcription_config,
                    transcriber_ready,
                )
            except Exception as exc:  # noqa: BLE001 - per-item isolation
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
        audio_timeout: float,
        max_document_bytes: int,
        max_audio_bytes: int,
        max_redirects: int,
        correlation_id: str,
        observed_at: datetime,
        acquired_at: datetime,
        transcription_config: dict,
        transcriber_ready: bool,
    ) -> dict | None:
        url = item["url"]
        title = _clean_title(item["title"] or url)
        published = item.get("published")
        if _is_audio_link(item):
            return self._audio_record(
                url,
                title,
                published,
                issuer,
                institution,
                document_type,
                headers,
                audio_timeout,
                max_audio_bytes,
                max_redirects,
                correlation_id,
                observed_at,
                acquired_at,
                transcription_config,
                transcriber_ready,
            )
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
        issuer: dict,
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
        fetch_headers["Accept"] = (
            "text/html,application/xhtml+xml,application/pdf,"
            "application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.8"
        )
        response, final_url = _fetch_bounded(
            url,
            headers=fetch_headers,
            max_bytes=max_bytes,
            timeout=timeout,
            correlation_id=correlation_id,
            max_redirects=max_redirects,
        )
        fetched_at = datetime.now(UTC)
        content, speaker_count, page_title = _extract_transcript(
            response, final_url, max_bytes=max_bytes
        )
        marker = f"{title} {url} {final_url}".lower()
        page_marker = page_title.lower()
        if speaker_count < _MIN_TEXT_SPEAKERS or not (
            "transcript" in marker
            or "transcript" in page_marker
            or "earnings conference call" in page_marker
        ):
            raise InvalidSourceData(
                "page does not contain a speaker-labelled earnings transcript"
            )
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        # Only provider-supplied publication times (RSS/audio enclosures)
        # are authoritative. A timestamp scraped from the page itself is
        # best-effort (inferred), and the acquisition-time fallback is the
        # last resort; inferred times never enter the document identity, so
        # re-runs do not mint new rows when page metadata changes.
        published_at = published
        inferred = published_at is None
        if published_at is None:
            published_at = _published_from_page(response) or observed_at

        metadata = {
            "kind": "text",
            "ticker": issuer.get("ticker"),
            "source_url": url,
            "content_hash": content_hash,
            "speaker_sections": speaker_count > 0,
            "speakers": speaker_count,
            "published_at": _iso(published_at),
            "published_at_inferred": inferred,
            "source_observed_at": _iso(observed_at),
            "fetched_at": _iso(fetched_at),
            "available_at": _iso(fetched_at),
            "acquired_at": _iso(acquired_at),
        }
        return {
            "document_id": _document_id(
                institution=institution,
                document_type=document_type,
                published_at=None if inferred else published_at,
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

    def _audio_record(
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
        transcription_config: Mapping[str, Any],
        transcriber_ready: bool,
    ) -> dict:
        cache_path = _audio_cache_path(transcription_config, url, published)
        cached = _read_audio_cache(cache_path, url)
        if cached is not None:
            published_at = published or observed_at
            audio_hash = cached["audio_sha256"]
            audio_metadata = {
                "kind": "audio",
                "ticker": issuer.get("ticker"),
                "source_url": url,
                "audio_url": cached["final_url"],
                "audio_content_type": str(cached.get("audio_content_type") or ""),
                "audio_bytes": cached["audio_bytes"],
                "audio_sha256": audio_hash,
                "content_hash": audio_hash,
                "published_at": _iso(published_at),
                "published_at_inferred": published is None,
                "source_observed_at": _iso(observed_at),
                "fetched_at": cached["fetched_at"],
                "available_at": cached["available_at"],
                "acquired_at": _iso(acquired_at),
                "transcription": dict(cached["transcription"]),
                "cache_hit": True,
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
                "url": cached["final_url"],
                "content": cached["content"],
                "metadata": audio_metadata,
                "acquired_at": acquired_at,
            }
        fetch_headers = dict(headers)
        fetch_headers["Accept"] = _AUDIO_ACCEPT
        response, final_url = _fetch_bounded(
            url,
            headers=fetch_headers,
            max_bytes=max_bytes,
            timeout=timeout,
            correlation_id=correlation_id,
            max_redirects=max_redirects,
        )
        fetched_at = datetime.now(UTC)
        audio = response.content
        audio_hash = audio_sha256(audio)
        audio_metadata = {
            "kind": "audio",
            "ticker": issuer.get("ticker"),
            "source_url": url,
            "audio_url": final_url,
            "audio_content_type": _content_type(response),
            "audio_bytes": len(audio),
            "audio_sha256": audio_hash,
            "content_hash": audio_hash,
            "published_at": _iso(published or observed_at),
            "published_at_inferred": published is None,
            "source_observed_at": _iso(observed_at),
            "fetched_at": _iso(fetched_at),
            "acquired_at": _iso(acquired_at),
            "cache_hit": False,
        }
        state = None
        try:
            if not transcriber_ready:
                raise TranscriptionUnavailable(
                    "faster-whisper is not installed; cannot transcribe audio locally"
                )
            result = transcribe_audio(
                audio,
                transcription_config,
                correlation_id,
                source_url=final_url,
            )
        except TranscriptionUnavailable as exc:
            state = "setup_required"
            error = _safe_error(exc)
            logger.warning(
                "issuer_transcript_transcription_unavailable",
                institution=institution,
                item=url,
                correlation_id=correlation_id,
            )
        except TranscriptionTimeout as exc:
            state = "timeout"
            error = _safe_error(exc)
            logger.warning(
                "issuer_transcript_transcription_timeout",
                institution=institution,
                item=url,
                correlation_id=correlation_id,
            )
        except TranscriptionFailure as exc:
            state = "failed"
            error = _safe_error(exc)
            logger.warning(
                "issuer_transcript_transcription_failed",
                institution=institution,
                item=url,
                error_type=type(exc).__name__,
                correlation_id=correlation_id,
            )
        else:
            audio_metadata["available_at"] = result.transcribed_at
            audio_metadata["transcription"] = {
                "model": result.model,
                "device": result.device,
                "compute_type": result.compute_type,
                "beam_size": result.beam_size,
                "language": result.language,
                "language_probability": result.language_probability,
                "duration_seconds": result.duration_seconds,
                "segments": len(result.segments),
                "vad_filter": result.vad_filter,
                "condition_on_previous_text": result.condition_on_previous_text,
                "elapsed_ms": result.elapsed_ms,
                "transcribed_at": result.transcribed_at,
            }
            published_at = published or observed_at
            record = {
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
                "content": result.text,
                "metadata": audio_metadata,
                "acquired_at": acquired_at,
            }
            _write_audio_cache(
                cache_path,
                {
                    "version": 1,
                    "source_url": url,
                    "final_url": final_url,
                    "content": result.text,
                    "audio_content_type": audio_metadata["audio_content_type"],
                    "audio_bytes": audio_metadata["audio_bytes"],
                    "audio_sha256": audio_hash,
                    "fetched_at": audio_metadata["fetched_at"],
                    "available_at": audio_metadata["available_at"],
                    "transcription": audio_metadata["transcription"],
                },
            )
            return record

        audio_metadata["state"] = state
        audio_metadata["error"] = error
        audio_metadata["available"] = False
        return {
            "document_id": _document_id(
                institution=institution,
                document_type=document_type,
                published_at=published,
                url=url,
                state=state,
            ),
            "source": SOURCE_ID,
            "institution": institution,
            "document_type": document_type,
            "title": title,
            "published_at": published or observed_at,
            "url": final_url,
            "content": None,
            "metadata": audio_metadata,
            "acquired_at": acquired_at,
        }

    def health_check(self, config):
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
                timeout=15,
                follow_redirects=False,
            )
            healthy = response.status_code < 400
            return {
                "healthy": healthy,
                "state": "success" if healthy else "failed",
                "message": f"HTTP {response.status_code}",
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as exc:  # noqa: BLE001 - health checks never raise
            state = (
                "setup_required"
                if isinstance(exc, CollectorSetupRequired)
                else "failed"
            )
            return {
                "healthy": False,
                "state": state,
                "message": _safe_error(exc),
                "latency_ms": int((time.monotonic() - started) * 1000),
            }

    def get_schedule(self, config):
        try:
            return _section(config).get("schedule") or DEFAULT_SCHEDULE
        except CollectorSetupRequired:
            return DEFAULT_SCHEDULE

    def get_target_table(self):
        return "source_documents"

    def get_conflict_columns(self):
        return ["document_id"]


__all__ = [
    "DEFAULT_SCHEDULE",
    "DEFAULT_USER_AGENT",
    "IssuerTranscriptsCollector",
    "SOURCE_ID",
]
