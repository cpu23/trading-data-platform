"""Bounded primary-source ingestion for issuer and regulatory updates.

Shared engine for the ``issuer_news`` collector: fetches configured RSS/Atom
feeds and HTML/JSON-LD discovery pages through the pinned public-only
transport, normalises items into ``source_documents`` records, and collapses
syndicated aliases of the same release into one deterministic document
identity.

Safety posture
--------------
* Every configured origin is operator-allowlisted (validated by
  ``provider_origins.validate_configured_origin`` before any fetch); each
  send re-resolves DNS through :class:`PublicOnlyHTTPTransport` and requires
  every answer to be globally routable.
* Redirects are followed manually and each hop must stay on the configured
  origin (scheme/host/port); HTTPS downgrades, credential smuggling, and
  cross-origin hops fail closed. Hop count and body size are bounded.
* XML bodies declaring a DTD or entities are refused (entity-expansion
  protection); malformed XML/JSON-LD fails explicitly.
* Item URLs are only shape-validated references -- they are never fetched,
  so arbitrary linked articles are never downloaded.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from contracts.outbound_security import (
    DEFAULT_PORTS,
    OutboundSecurityError,
    parse_origin,
    resolve_redirect_url,
)
from http_client import get_shared_client
from logging_config import get_logger

logger = get_logger("issuer_feed")

DEFAULT_USER_AGENT = "TradingDataPlatform/1.0 (issuer news collector; +local research)"

# Bounded defaults; operators may tune per feed.
DEFAULT_MAX_FEED_BYTES = 5_000_000
DEFAULT_MAX_ITEMS_PER_FEED = 100
DEFAULT_MAX_TITLE_CHARS = 500
DEFAULT_MAX_CONTENT_CHARS = 4_000
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_JSONLD_BLOCKS = 200

_ATOM_NS = "http://www.w3.org/2005/Atom"
_RSS1_NS = "http://purl.org/rss/1.0/"
_RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"

# Direct child text lookup namespaces, in preference order (plain first).
_TEXT_NAMESPACES = ("", _ATOM_NS, _RSS1_NS, _DC_NS, _CONTENT_NS)

# Query parameters that are tracking noise, never part of a document identity.
_TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "gclsrc",
        "igshid",
        "mc_cid",
        "mc_eid",
        "twclid",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_source",
        "utm_term",
    }
)

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})

# SEC EDGAR "current events" Atom entries are titled
# "<form> - <company> (<10-digit CIK>) (<role>)", e.g.
# "8-K - Microsoft Corp (0000789019) (Filer)".
_SEC_EDGAR_TITLE_RE = re.compile(r"(.+?)\s+\((\d{10})\)\s+\(([A-Za-z]+)\)")

# Upper bound for a configured CIK -> ticker map (mirrors the strict config
# model bound; lookups stay trivial).
MAX_CIK_SYMBOLS = 500

# schema.org types treated as primary-source documents.
_JSONLD_DOCUMENT_TYPES = frozenset(
    {"Article", "BlogPosting", "NewsArticle", "Report", "WebPage"}
)

_TAG_RE = re.compile(r"<[^>]+>")
_PUNCTUATION_SPACING_RE = re.compile(r"\s+([,.;:!?])")
_WHITESPACE_RE = re.compile(r"\s+")


class IssuerFeedError(Exception):
    """Base for explicit issuer-feed failures.

    ``code`` and ``error_class`` drive deterministic per-feed error entries
    (``error_class`` follows the platform taxonomy: transient_source vs
    invalid_source_data).
    """

    code = "feed_failed"
    error_class = "invalid_source_data"


class FeedOversizeError(IssuerFeedError):
    code = "feed_oversize"


class FeedUnsafeOriginError(IssuerFeedError):
    code = "unsafe_origin"


class FeedRedirectError(IssuerFeedError):
    code = "redirect_limit"


class FeedMalformedError(IssuerFeedError):
    code = "malformed_feed"


class FeedUnsupportedKindError(IssuerFeedError):
    code = "unsupported_kind"


class FeedReadTimeoutError(IssuerFeedError):
    code = "feed_read_timeout"
    error_class = "transient_source"


class FeedHTTPError(IssuerFeedError):
    code = "http_status"

    def __init__(self, status_code: int, detail: str = ""):
        self.status_code = status_code
        message = f"HTTP {status_code}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)
        self.error_class = (
            "transient_source"
            if status_code in _RETRYABLE_HTTP_STATUSES
            else "invalid_source_data"
        )


@dataclass(frozen=True)
class FeedFetch:
    """Outcome of one bounded feed/page fetch."""

    status_code: int
    final_url: str
    body: bytes | None  # None for 304 Not Modified
    etag: str | None = None
    last_modified: str | None = None
    hops: int = 0


# ---------------------------------------------------------------------------
# URL identity
# ---------------------------------------------------------------------------


def canonicalize_url(url: str) -> str:
    """Deterministic identity URL for a primary-source document.

    Normalises scheme/host case, strips fragments, tracking parameters, and
    default ports, sorts remaining query parameters, and fills an empty path
    with ``/``. Two aliases of the same release (e.g. one feed linking with
    ``?utm_source=...``) collapse to the same canonical URL.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"invalid port in URL {url!r}") from exc
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if port is not None and port != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS
    ]
    query.sort(key=lambda pair: (pair[0].lower(), pair[1]))
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def document_id(source: str, canonical_url: str) -> str:
    """Deterministic document identity.

    Derived only from the canonical source URL, so the same release found in
    several syndicated feeds shares one identity.
    """
    identity = f"{source}|{canonical_url}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _validated_item_url(url: str | None) -> str | None:
    """Shape-validate an item URL (a reference, never fetched).

    Requires http(s), a hostname, and no embedded credentials. DNS is not
    resolved: item URLs are stored, not requested.
    """
    if not url:
        return None
    try:
        origin = parse_origin(url)
    except OutboundSecurityError:
        return None
    return origin.url


# ---------------------------------------------------------------------------
# Timestamps and text
# ---------------------------------------------------------------------------


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse RFC 822 (RSS pubDate) or ISO 8601 (Atom/JSON-LD) timestamps.

    Returns a timezone-aware UTC datetime, or None when unparseable.
    """
    if not value:
        return None
    value = value.strip()
    parsed = None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _plain_text(value: str | None, max_chars: int) -> str:
    """Strip markup and entities, collapse whitespace, bound length."""
    if not value:
        return ""
    text = html.unescape(value)
    text = _TAG_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    text = _PUNCTUATION_SPACING_RE.sub(r"\1", text)
    return text[:max_chars]


def extract_primary_page_text(body: bytes, max_chars: int) -> str:
    """Extract bounded article text from one same-origin primary-source page.

    Prefer schema.org ``articleBody`` because it excludes navigation and related
    links. Fall back to semantic ``article``/``main`` content after removing
    executable, navigational, and form elements.
    """
    if max_chars <= 0 or not body:
        return ""
    try:
        soup = BeautifulSoup(body, "lxml")
    except Exception as exc:
        raise FeedMalformedError("linked primary page is not parseable HTML") from exc

    jsonld_bodies: list[str] = []
    for node in soup.find_all(
        "script", attrs={"type": "application/ld+json"}, limit=50
    ):
        raw = node.string or node.get_text()
        if not raw or len(raw) > 2_000_000:
            continue
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            continue
        pending = [value]
        visited = 0
        while pending and visited < 500:
            visited += 1
            current = pending.pop()
            if isinstance(current, dict):
                article_body = current.get("articleBody")
                if isinstance(article_body, str):
                    cleaned = _plain_text(article_body, max_chars)
                    if cleaned:
                        jsonld_bodies.append(cleaned)
                pending.extend(current.values())
            elif isinstance(current, list):
                pending.extend(current[:200])
    if jsonld_bodies:
        return max(jsonld_bodies, key=len)[:max_chars]

    for node in soup(
        [
            "script",
            "style",
            "noscript",
            "svg",
            "nav",
            "header",
            "footer",
            "form",
            "aside",
        ]
    ):
        node.decompose()
    root = soup.find("article") or soup.find("main") or soup.body
    if root is None:
        return ""
    paragraphs = []
    for value in root.stripped_strings:
        cleaned = _WHITESPACE_RE.sub(" ", str(value)).strip()
        if cleaned:
            paragraphs.append(cleaned)
    return "\n".join(paragraphs)[:max_chars]


def _child_text(node: ElementTree.Element, name: str) -> str | None:
    """First non-empty direct child text for ``name`` across feed namespaces."""
    for namespace in _TEXT_NAMESPACES:
        tag = f"{{{namespace}}}{name}" if namespace else name
        child = node.find(tag)
        if child is not None:
            text = (child.text or "").strip()
            if text:
                return text
    return None


def _local_name(tag: str) -> str:
    return tag.split("}", 1)[-1] if tag.startswith("{") else tag


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def _read_bounded(response, cap: int, deadline_seconds: float) -> bytes:
    """Read a streaming response with hard byte and wall-clock caps."""
    started = time.monotonic()
    chunks = []
    total = 0
    for chunk in response.iter_bytes():
        if time.monotonic() - started >= deadline_seconds:
            raise FeedReadTimeoutError("feed body read exceeded the deadline")
        total += len(chunk)
        if total > cap:
            raise FeedOversizeError(f"feed body exceeds {cap} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_feed(
    url: str,
    *,
    headers: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    cap: int = DEFAULT_MAX_FEED_BYTES,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    conditional: dict[str, str] | None = None,
    client=None,
) -> FeedFetch:
    """Fetch one validated feed/page origin through the pinned transport.

    The caller must have validated ``url`` as an allowlisted origin
    (``provider_origins.validate_configured_origin``); every send
    re-resolves DNS and pins the connection through
    ``PublicOnlyHTTPTransport``, so a rebound host cannot reach private
    networks. Redirects are followed manually: each hop must keep the
    configured origin (scheme/host/port) and stay HTTPS; the hop count and
    the body are bounded. ``conditional`` (previously observed ETag /
    Last-Modified) is sent as ``If-None-Match`` / ``If-Modified-Since``;
    304 yields ``body=None``.
    """
    try:
        configured = parse_origin(url)
    except OutboundSecurityError as exc:
        raise FeedUnsafeOriginError("feed URL is not a valid http(s) origin") from exc
    request_headers = dict(headers or {})
    request_headers.setdefault("User-Agent", DEFAULT_USER_AGENT)
    if conditional:
        etag = conditional.get("etag")
        if etag:
            request_headers.setdefault("If-None-Match", str(etag))
        last_modified = conditional.get("last_modified")
        if last_modified:
            request_headers.setdefault("If-Modified-Since", str(last_modified))
    client = client or get_shared_client()
    current = url
    hops = 0
    for _attempt in range(max_redirects + 1):
        with client.stream(
            "GET",
            current,
            headers=request_headers,
            follow_redirects=False,
            timeout=timeout,
        ) as response:
            status = response.status_code
            etag_header = response.headers.get("etag")
            last_modified_header = response.headers.get("last-modified")
            if status in _REDIRECT_STATUSES:
                location = response.headers.get("location")
                if not location:
                    raise FeedHTTPError(
                        status, "redirect response has no Location header"
                    )
                try:
                    target = resolve_redirect_url(current, location)
                    target_origin = parse_origin(target)
                except OutboundSecurityError as exc:
                    raise FeedUnsafeOriginError(
                        "redirect target failed origin validation"
                    ) from exc
                if (
                    target_origin.scheme,
                    target_origin.host,
                    target_origin.port,
                ) != (configured.scheme, configured.host, configured.port):
                    raise FeedUnsafeOriginError("redirect leaves the configured origin")
                current = target
                hops += 1
                continue
            if status == 304:
                return FeedFetch(
                    status, current, None, etag_header, last_modified_header, hops
                )
            if status < 200 or status >= 300:
                raise FeedHTTPError(status)
            body = _read_bounded(response, cap, deadline_seconds=timeout + 10.0)
            return FeedFetch(
                status, current, body, etag_header, last_modified_header, hops
            )
    raise FeedRedirectError(f"redirect chain exceeded {max_redirects} hops")


# ---------------------------------------------------------------------------
# RSS / Atom parsing
# ---------------------------------------------------------------------------


def _reject_doctype(body: bytes) -> None:
    """Refuse documents declaring a DTD or entities (expansion protection)."""
    head = body[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        raise FeedMalformedError("feed declares a DTD or entities; refusing to parse")


def _rss_root(tag: str) -> bool:
    if not isinstance(tag, str):
        return False
    if _local_name(tag) not in {"rss", "RDF"}:
        return False
    if tag.startswith("{"):
        namespace = tag[1:].split("}", 1)[0]
        return namespace in {
            "http://backend.userland.com/rss2",
            _RSS1_NS,
            _RDF_NS,
        }
    return True


def _parse_xml_feed(
    body: bytes,
    *,
    max_items: int,
    max_title_chars: int,
    max_content_chars: int,
) -> list[dict[str, Any]]:
    _reject_doctype(body)
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise FeedMalformedError("feed body is not well-formed XML") from exc
    tag = root.tag if isinstance(root.tag, str) else ""
    if tag.startswith(f"{{{_ATOM_NS}}}"):
        return _parse_atom(
            root,
            max_items=max_items,
            max_title_chars=max_title_chars,
            max_content_chars=max_content_chars,
        )
    if _rss_root(tag):
        return _parse_rss(
            root,
            max_items=max_items,
            max_title_chars=max_title_chars,
            max_content_chars=max_content_chars,
        )
    raise FeedMalformedError(f"unexpected feed root element {_local_name(tag)}")


def _parse_rss(
    root: ElementTree.Element,
    *,
    max_items: int,
    max_title_chars: int,
    max_content_chars: int,
) -> list[dict[str, Any]]:
    nodes = root.findall(".//item") + root.findall(f".//{{{_RSS1_NS}}}item")
    raw_items = []
    for node in nodes[:max_items]:
        item = _rss_item(node, max_title_chars, max_content_chars)
        if item is not None:
            raw_items.append(item)
    return raw_items


def _rss_item(
    node: ElementTree.Element, max_title_chars: int, max_content_chars: int
) -> dict[str, Any] | None:
    title = _plain_text(_child_text(node, "title"), max_title_chars)
    if not title:
        return None
    link = _child_text(node, "link")
    guid = _child_text(node, "guid") or _child_text(node, "id")
    rdf_about = node.get(f"{{{_RDF_NS}}}about")
    url = _validated_item_url(link or guid or rdf_about)
    if url is None:
        return None
    published_raw, published_field = _first_present(
        node, ("pubDate", "date", "updated", "published")
    )
    published = parse_timestamp(published_raw)
    updated = parse_timestamp(_child_text(node, "updated"))
    content = (
        _child_text(node, "encoded")
        or _child_text(node, "description")
        or _child_text(node, "summary")
    )
    return {
        "title": title,
        "url": url,
        "canonical_url": canonicalize_url(url),
        "guid": guid,
        "published": published,
        "updated": updated,
        "raw_published": published_raw,
        "published_fallback": None
        if published_field in {"pubDate", "date"}
        else published_field,
        "content": _plain_text(content, max_content_chars),
        "source_kind": "rss",
        "publisher": None,
        "author": None,
    }


def _first_present(
    node: ElementTree.Element, names: tuple[str, ...]
) -> tuple[str | None, str | None]:
    for name in names:
        value = _child_text(node, name)
        if value:
            return value, name
    return None, None


def _parse_atom(
    root: ElementTree.Element,
    *,
    max_items: int,
    max_title_chars: int,
    max_content_chars: int,
) -> list[dict[str, Any]]:
    nodes = root.findall(f".//{{{_ATOM_NS}}}entry")
    raw_items = []
    for node in nodes[:max_items]:
        item = _atom_item(node, max_title_chars, max_content_chars)
        if item is not None:
            raw_items.append(item)
    return raw_items


def _atom_link(node: ElementTree.Element) -> str | None:
    best = None
    for link in node.findall(f"{{{_ATOM_NS}}}link"):
        href = (link.get("href") or "").strip()
        if not href:
            continue
        rel = (link.get("rel") or "alternate").strip()
        if rel == "alternate":
            return href
        if best is None:
            best = href
    return best


def _atom_item(
    node: ElementTree.Element, max_title_chars: int, max_content_chars: int
) -> dict[str, Any] | None:
    title = _plain_text(_child_text(node, "title"), max_title_chars)
    if not title:
        return None
    url = _validated_item_url(_atom_link(node))
    if url is None:
        return None
    published_raw, published_field = _first_present(
        node, ("published", "updated", "date")
    )
    published = parse_timestamp(published_raw)
    updated = parse_timestamp(_child_text(node, "updated"))
    content = _child_text(node, "summary") or _child_text(node, "content")
    return {
        "title": title,
        "url": url,
        "canonical_url": canonicalize_url(url),
        "guid": _child_text(node, "id"),
        "published": published,
        "updated": updated,
        "raw_published": published_raw,
        "published_fallback": None
        if published_field in {"published"}
        else published_field,
        "content": _plain_text(content, max_content_chars),
        "source_kind": "atom",
        "publisher": None,
        "author": None,
    }


# ---------------------------------------------------------------------------
# JSON-LD parsing (direct payload or HTML script discovery)
# ---------------------------------------------------------------------------


class _JsonLdBlockExtractor(HTMLParser):
    """Collect ``<script type="application/ld+json">`` blocks from an HTML page."""

    def __init__(self, cap: int):
        super().__init__(convert_charrefs=True)
        self._cap = cap
        self.blocks: list[str] = []
        self._active = False
        self._buffer: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and len(self.blocks) < self._cap:
            attributes = dict(attrs)
            content_type = (attributes.get("type") or "text/javascript").lower()
            if content_type == "application/ld+json" or content_type.startswith(
                "application/ld+json;"
            ):
                self._active = True
                self._buffer = []

    def handle_endtag(self, tag):
        if self._active and tag == "script":
            self.blocks.append("".join(self._buffer))
            self._active = False
            self._buffer = []

    def handle_data(self, data):
        if self._active:
            self._buffer.append(data)


def _jsonld_types(value: dict[str, Any]) -> set[str]:
    raw = value.get("@type") or value.get("type")
    if isinstance(raw, str):
        return {raw.split("/")[-1]}
    if isinstance(raw, list):
        return {str(item).split("/")[-1] for item in raw if isinstance(item, str)}
    return set()


def _jsonld_items(value: Any) -> list[dict[str, Any]]:
    """Flatten a JSON-LD value into document objects (dicts)."""
    if isinstance(value, list):
        items = []
        for element in value:
            items.extend(_jsonld_items(element))
        return items
    if not isinstance(value, dict):
        return []
    graph = value.get("@graph")
    if isinstance(graph, (list, dict)):
        return _jsonld_items(graph)
    types = _jsonld_types(value)
    if "ItemList" in types or "itemListElement" in value:
        return _jsonld_items(value.get("itemListElement", []))
    if "item" in value and isinstance(value.get("item"), (list, dict)):
        return _jsonld_items(value.get("item"))
    if types & _JSONLD_DOCUMENT_TYPES:
        return [value]
    return []


def _jsonld_scalar(document: dict[str, Any], key: str) -> str | None:
    value = document.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _jsonld_first(document: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _jsonld_scalar(document, key)
        if value:
            return value
    return None


def _jsonld_url(document: dict[str, Any]) -> str | None:
    for key in ("url", "mainEntityOfPage", "@id"):
        value = document.get(key)
        if isinstance(value, str):
            validated = _validated_item_url(value)
            if validated:
                return validated
        elif isinstance(value, dict):
            for sub in ("@id", "url"):
                nested = value.get(sub)
                if isinstance(nested, str):
                    validated = _validated_item_url(nested)
                    if validated:
                        return validated
    return None


def _jsonld_name(document: dict[str, Any], key: str) -> str | None:
    value = document.get(key)
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        name = value.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _jsonld_item(
    document: dict[str, Any], max_title_chars: int, max_content_chars: int
) -> dict[str, Any] | None:
    title = _plain_text(
        _jsonld_first(document, "headline", "name", "title"), max_title_chars
    )
    if not title:
        return None
    url = _jsonld_url(document)
    if url is None:
        return None
    published_raw = None
    published_fallback = None
    for field in ("datePublished", "dateCreated", "dateModified"):
        candidate = _jsonld_scalar(document, field)
        if candidate:
            published_raw = candidate
            published_fallback = None if field == "datePublished" else field
            break
    published = parse_timestamp(published_raw)
    updated = parse_timestamp(_jsonld_scalar(document, "dateModified"))
    content = _plain_text(
        _jsonld_first(document, "description", "articleBody", "text"),
        max_content_chars,
    )
    guid = document.get("@id") if isinstance(document.get("@id"), str) else None
    return {
        "title": title,
        "url": url,
        "canonical_url": canonicalize_url(url),
        "guid": guid,
        "published": published,
        "updated": updated,
        "raw_published": published_raw,
        "published_fallback": published_fallback,
        "content": content,
        "source_kind": "jsonld",
        "publisher": _jsonld_name(document, "publisher"),
        "author": _jsonld_name(document, "author"),
    }


def _parse_jsonld(
    body: bytes,
    feed: dict,
    *,
    max_items: int,
    max_title_chars: int,
    max_content_chars: int,
) -> list[dict[str, Any]]:
    kind = str(feed.get("kind") or "jsonld").strip().lower()
    documents: list[dict[str, Any]] = []
    if kind in {"html", "html_jsonld"}:
        extractor = _JsonLdBlockExtractor(MAX_JSONLD_BLOCKS)
        extractor.feed(body.decode("utf-8", errors="replace"))
        for block in extractor.blocks:
            if not block.strip():
                continue
            try:
                value = json.loads(block)
            except json.JSONDecodeError as exc:
                raise FeedMalformedError(
                    "JSON-LD script block is not valid JSON"
                ) from exc
            documents.extend(_jsonld_items(value))
    else:
        try:
            value = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise FeedMalformedError("JSON-LD payload is not valid JSON") from exc
        documents = _jsonld_items(value)
    raw_items = []
    for document in documents[:max_items]:
        item = _jsonld_item(document, max_title_chars, max_content_chars)
        if item is not None:
            raw_items.append(item)
    return raw_items


# ---------------------------------------------------------------------------
# Public parsing entry point
# ---------------------------------------------------------------------------


def parse_feed_items(
    body: bytes,
    feed: dict,
    *,
    max_items: int = DEFAULT_MAX_ITEMS_PER_FEED,
    max_title_chars: int = DEFAULT_MAX_TITLE_CHARS,
    max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
) -> list[dict[str, Any]]:
    """Parse one bounded feed/page body into normalised raw items.

    ``kind`` selects the parser: ``feed``/``rss``/``atom`` (root-driven XML),
    ``html`` (JSON-LD script discovery), or ``jsonld`` (direct JSON-LD
    payload). Malformed provider data raises ``IssuerFeedError``.
    """
    kind = str(feed.get("kind") or "feed").strip().lower()
    if kind in {"html", "html_jsonld", "jsonld"}:
        return _parse_jsonld(
            body,
            feed,
            max_items=max_items,
            max_title_chars=max_title_chars,
            max_content_chars=max_content_chars,
        )
    if kind in {"feed", "rss", "atom", "xml"}:
        return _parse_xml_feed(
            body,
            max_items=max_items,
            max_title_chars=max_title_chars,
            max_content_chars=max_content_chars,
        )
    raise FeedUnsupportedKindError(f"unsupported feed kind {kind!r}")


# ---------------------------------------------------------------------------
# SEC EDGAR entity parsing (opt-in per feed)
# ---------------------------------------------------------------------------


def parse_sec_edgar_title(title: str) -> dict[str, str] | None:
    """Parse a standard SEC EDGAR Atom entry title.

    EDGAR "current events" Atom feeds title entries as
    ``<form> - <company> (<10-digit CIK>) (<role>)``, e.g.
    ``8-K - Microsoft Corp (0000789019) (Filer)``. Returns the form type,
    company name, 10-digit CIK, and filer role -- or ``None`` when the title
    does not match that shape, in which case the caller keeps the feed-level
    institution and must not invent an entity.
    """
    head, separator, tail = title.partition(" - ")
    if not separator:
        return None
    match = _SEC_EDGAR_TITLE_RE.fullmatch(tail.strip())
    if match is None:
        return None
    company = match.group(1).strip()
    if not company:
        return None
    return {
        "form": head.strip(),
        "company": company,
        "cik": match.group(2),
        "role": match.group(3),
    }


# ---------------------------------------------------------------------------
# Record normalisation and alias deduplication
# ---------------------------------------------------------------------------


def _record_from_raw(
    raw: dict[str, Any],
    *,
    source: str,
    institution: str | None,
    document_type: str,
    role: str,
    label: str,
    feed_url: str,
    acquired_at: datetime,
    observed_at: datetime,
    fetch: FeedFetch,
    entity_parser: str | None = None,
    cik_symbols: dict[str, str] | None = None,
) -> tuple[dict | None, str | None]:
    title = raw.get("title") or ""
    if not title:
        return None, "missing_title"
    url = raw.get("canonical_url")
    if not url:
        return None, "invalid_url"
    published = raw.get("published")
    if published is None:
        return None, "missing_date"

    metadata: dict[str, Any] = {
        "origin": {
            "feed": label,
            "feed_url": feed_url,
            "kind": raw.get("source_kind"),
            "role": role,
        },
        "canonical_url": url,
        "source_time": {
            "published_at": published.isoformat(),
            "updated_at": raw.get("updated").isoformat()
            if raw.get("updated")
            else None,
            "raw_published": raw.get("raw_published"),
            "published_fallback": raw.get("published_fallback"),
        },
        "acquisition": {
            "acquired_at": acquired_at.isoformat(),
            "observed_at": observed_at.isoformat(),
            "status_code": fetch.status_code,
            "etag": fetch.etag,
            "last_modified": fetch.last_modified,
        },
        "content_role": role,
        "primary": role == "primary",
    }
    if raw.get("url"):
        metadata["raw_url"] = raw["url"]
    if raw.get("guid"):
        metadata["guid"] = raw["guid"]
    if raw.get("publisher"):
        metadata["publisher"] = raw["publisher"]
    if raw.get("author"):
        metadata["author"] = raw["author"]

    resolved_institution = institution
    if entity_parser == "sec_edgar":
        entity = parse_sec_edgar_title(title)
        if entity is not None:
            resolved_institution = entity["company"]
            metadata["cik"] = entity["cik"]
            ticker = (cik_symbols or {}).get(entity["cik"])
            if ticker:
                metadata["ticker"] = ticker

    return {
        "document_id": document_id(source, url),
        "source": source,
        "institution": resolved_institution,
        "document_type": document_type,
        "title": title,
        "published_at": published,
        "url": url,
        "content": raw.get("content") or None,
        "acquired_at": acquired_at,
        "metadata": metadata,
    }, None


def normalize_feed_records(
    raw_items: list[dict[str, Any]],
    feed: dict,
    *,
    source: str,
    acquired_at: datetime,
    observed_at: datetime,
    fetch: FeedFetch,
    feed_url: str,
) -> tuple[list[dict], dict[str, int]]:
    """Turn raw feed items into ``source_documents`` records.

    Records carry the canonical source URL, publication (source) time and
    acquisition/observation timestamps, bounded primary-source text, and
    metadata identifying the originating feed and primary vs derivative
    syndication. Items missing a title, a usable URL, or a publication time
    are skipped with explicit reasons; nothing is fabricated.

    A feed may opt into ``entity_parser: sec_edgar``: standard EDGAR titles
    (``<form> - <company> (<10-digit CIK>) (<role>)``) then override the
    record institution with the filing company and add ``metadata.cik`` plus
    an optional ``metadata.ticker`` from the bounded ``cik_symbols`` map.
    Non-matching titles keep the feed-level institution with no entity
    metadata.
    """
    role = str(feed.get("content_role") or "primary").strip().lower()
    if role not in {"primary", "derivative"}:
        role = "primary"
    document_type = str(feed.get("document_type") or "issuer_update").strip()
    if not document_type:
        document_type = "issuer_update"
    institution = str(feed.get("institution") or "").strip() or None
    label = str(feed.get("name") or "").strip() or feed_url

    entity_parser = str(feed.get("entity_parser") or "").strip().lower() or None
    if entity_parser not in {"sec_edgar"}:
        entity_parser = None
    raw_cik_symbols = feed.get("cik_symbols")
    if entity_parser == "sec_edgar" and isinstance(raw_cik_symbols, dict):
        cik_symbols = {
            str(key): str(value)
            for key, value in list(raw_cik_symbols.items())[:MAX_CIK_SYMBOLS]
        }
    else:
        cik_symbols = {}

    records: list[dict] = []
    skipped: dict[str, int] = {}
    for raw in raw_items:
        record, reason = _record_from_raw(
            raw,
            source=source,
            institution=institution,
            document_type=document_type,
            role=role,
            label=label,
            feed_url=feed_url,
            acquired_at=acquired_at,
            observed_at=observed_at,
            fetch=fetch,
            entity_parser=entity_parser,
            cik_symbols=cik_symbols,
        )
        if record is None:
            skipped[reason or "invalid_item"] = (
                skipped.get(reason or "invalid_item", 0) + 1
            )
            continue
        records.append(record)
    return records, skipped


def _alias_entry(record: dict) -> dict[str, Any]:
    metadata = record.get("metadata") or {}
    origin = metadata.get("origin") or {}
    entry: dict[str, Any] = {"feed": origin.get("feed"), "role": origin.get("role")}
    if metadata.get("raw_url"):
        entry["raw_url"] = metadata["raw_url"]
    if metadata.get("guid"):
        entry["guid"] = metadata["guid"]
    return entry


def dedupe_records(records: list[dict]) -> list[dict]:
    """Collapse records sharing one document identity across feed aliases.

    Primary sources win over derivative aliases regardless of feed order;
    otherwise the first configured occurrence is kept. Losers are recorded
    in the winner's ``metadata.aliases``. Deterministic.
    """
    seen: dict[str, dict] = {}
    for record in records:
        identity = record["document_id"]
        previous = seen.get(identity)
        if previous is None:
            seen[identity] = record
            continue
        previous_primary = bool((previous.get("metadata") or {}).get("primary"))
        current_primary = bool((record.get("metadata") or {}).get("primary"))
        if current_primary and not previous_primary:
            record.setdefault("metadata", {}).setdefault("aliases", []).append(
                _alias_entry(previous)
            )
            seen[identity] = record
        else:
            previous.setdefault("metadata", {}).setdefault("aliases", []).append(
                _alias_entry(record)
            )
    return list(seen.values())


__all__ = [
    "DEFAULT_MAX_CONTENT_CHARS",
    "DEFAULT_MAX_FEED_BYTES",
    "DEFAULT_MAX_ITEMS_PER_FEED",
    "DEFAULT_MAX_REDIRECTS",
    "DEFAULT_MAX_TITLE_CHARS",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_USER_AGENT",
    "FeedFetch",
    "FeedHTTPError",
    "FeedMalformedError",
    "FeedOversizeError",
    "FeedReadTimeoutError",
    "FeedRedirectError",
    "FeedUnsafeOriginError",
    "FeedUnsupportedKindError",
    "IssuerFeedError",
    "MAX_CIK_SYMBOLS",
    "canonicalize_url",
    "dedupe_records",
    "document_id",
    "fetch_feed",
    "normalize_feed_records",
    "parse_feed_items",
    "parse_sec_edgar_title",
    "parse_timestamp",
]
