"""Credential-safe diagnostics for outbound HTTP failures.

Single reusable sanitizer for every place a provider failure is logged,
persisted, or surfaced in health output.  Guarantees that no produced field
ever contains a URL query string, URL userinfo, header values, payload text,
or a credential-shaped value, while preserving the exception class, the HTTP
status when the failure carried a response, and the provider origin and safe
URL path so operators keep actionable context.

The full request URL (including query strings and any embedded credentials)
is NEVER part of any field: URLs inside arbitrary exception messages are
scrubbed down to ``scheme://host/path`` and named credential pairs are
redacted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

DEFAULT_MESSAGE_LIMIT = 500

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_AUTH_SCHEME_RE = re.compile(r"(?i)\b(bearer|basic)\s+[^\s,;]+")
_NAMED_SECRET_RE = re.compile(
    r"(?i)(?<![\w-])"
    r"(api[_-]?key|apikey|crtfc[_-]?key|access[_-]?token|refresh[_-]?token"
    r"|password|passwd|client[_-]?secret|authorization|secret)"
    r"(\s*[:=]\s*)([^\s,;&#}\]\"']+)"
)
_TRAILING_PUNCTUATION = ".,;!)]}'\""


def scrub_url(url: str) -> str:
    """Return ``scheme://host/path`` for one URL, dropping userinfo/query."""
    trailing = ""
    while url and url[-1] in _TRAILING_PUNCTUATION:
        trailing = url[-1] + trailing
        url = url[:-1]
    try:
        parts = urlsplit(url)
        netloc = parts.netloc
        if "@" in netloc:
            _userinfo, _separator, hostinfo = netloc.rpartition("@")
            netloc = hostinfo
        return urlunsplit((parts.scheme, netloc, parts.path, "", "")) + trailing
    except ValueError:
        return "[REDACTED URL]" + trailing


def _scrub_text(text: str) -> str:
    text = _URL_RE.sub(lambda match: scrub_url(match.group(0)), text)
    text = _AUTH_SCHEME_RE.sub(
        lambda match: f"{match.group(1)} [REDACTED]", text
    )
    text = _NAMED_SECRET_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text
    )
    return text


def _safe_attr(exc: object, name: str) -> object | None:
    """Read an attribute, treating raising properties as missing.

    httpx exposes ``request`` as a property that raises ``RuntimeError``
    when no request was attached (e.g. a bare ``ConnectError`` or
    ``TimeoutException``); a diagnostics helper must never raise while
    extracting context, so that is treated like a missing attribute.
    """
    try:
        return getattr(exc, name, None)
    except RuntimeError:
        return None


def _request_url(exc: BaseException) -> str | None:
    request = _safe_attr(exc, "request")
    url = getattr(request, "url", None)
    if url is None:
        response = _safe_attr(exc, "response")
        request = _safe_attr(response, "request")
        url = getattr(request, "url", None)
    return None if url is None else str(url)


def _origin_path(exc: BaseException, *, provider: str | None) -> tuple[str | None, str | None]:
    url = _request_url(exc)
    if url is None:
        return provider, None
    try:
        parts = urlsplit(url)
        return parts.hostname or provider, parts.path or None
    except ValueError:
        return provider, None


@dataclass(frozen=True)
class SafeHTTPError:
    """Structured, credential-free description of one external HTTP failure.

    ``error_type`` is the exception class name, ``status_code`` the HTTP
    status when the failure carried a response, ``origin`` the provider
    hostname (or caller-supplied provider id), and ``path`` the safe URL
    path.  ``message`` is a bounded human-readable line; no field contains a
    query string, userinfo, headers, or payload content.
    """

    error_type: str
    message: str
    status_code: int | None = None
    origin: str | None = None
    path: str | None = None

    def to_dict(self) -> dict:
        return {
            "error_type": self.error_type,
            "message": self.message,
            "status_code": self.status_code,
            "origin": self.origin,
            "path": self.path,
        }


def safe_http_error(
    exc: BaseException, *, provider: str | None = None
) -> SafeHTTPError:
    """Build the credential-free representation for one exception."""
    status_code: int | None = None
    response = _safe_attr(exc, "response")
    if response is not None:
        try:
            status_code = int(response.status_code)
        except (TypeError, ValueError):
            status_code = None
    origin, path = _origin_path(exc, provider=provider)
    return SafeHTTPError(
        error_type=type(exc).__name__,
        message=safe_error_message(exc, provider=provider),
        status_code=status_code,
        origin=origin,
        path=path,
    )


def safe_error_message(
    exc: BaseException,
    *,
    provider: str | None = None,
    limit: int = DEFAULT_MESSAGE_LIMIT,
) -> str:
    """Return a bounded, credential-free diagnostic line for an exception.

    The provider's own message text is preserved (it usually carries the
    actionable detail, e.g. ``timed out``) with every URL stripped of
    userinfo and query strings and named credential values redacted.  When
    the failure carried a request URL that the message did not already
    mention, the safe ``host/path`` context is appended so operators retain
    origin information.
    """
    raw = str(exc) or type(exc).__name__
    message = " ".join(_scrub_text(raw).split())
    origin, path = _origin_path(exc, provider=provider)
    if origin:
        context = f"{origin}{path or ''}"
        if context not in message:
            message = f"{message} ({context})"
    if limit and len(message) > limit:
        message = message[:limit].rstrip()
    return message or type(exc).__name__
