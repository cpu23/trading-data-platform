"""Shared, stdlib-only SSRF defense for outbound HTTP origins.

Both the API process (setup connection tests) and the orchestrator
(investment URL ingestion, provider fetches) consume this module so that
arbitrary origins are validated identically:

1. URL shape  - http/https scheme, non-empty hostname, no embedded
   credentials, valid port.
2. DNS policy - every address the hostname resolves to must be globally
   routable. A single private/loopback/link-local/reserved/multicast/
   unspecified answer rejects the whole resolution (fail closed), closing
   the classic mixed-DNS and rebinding split.

The module is intentionally dependency-free (no httpx, no pydantic) so it
can be imported from any process that has the ``contracts`` package.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

PUBLIC_SCHEMES = frozenset({"http", "https"})
DEFAULT_PORTS = {"http": 80, "https": 443}


class OutboundSecurityError(ValueError):
    """A URL or origin failed outbound security validation."""


def _normalized_ip(
    value: str | int | bytes,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Parse an address and collapse IPv4-mapped IPv6 to its IPv4 form.

    ``::ffff:127.0.0.1`` must be classified exactly like ``127.0.0.1``;
    otherwise the mapped form could bypass loopback/private checks.
    """
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError as exc:
        raise OutboundSecurityError(f"invalid IP address {value!r}") from exc
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped
    return parsed


def address_class(value: str | int | bytes | ipaddress._BaseAddress) -> str:
    """Human-readable classification of an address for diagnostics/logging."""
    ip = _normalized_ip(str(value))
    if ip.is_multicast:
        return "multicast"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link_local"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_reserved:
        return "reserved"
    if ip.is_private:
        return "private"
    if not ip.is_global:
        return "non_global"
    return "public"


def is_public_address(value: str | ipaddress._BaseAddress) -> bool:
    """True only for globally routable, unicast addresses.

    ``is_global`` alone is insufficient: IPv4/IPv6 multicast (``224.0.0.1``,
    ``ff02::1``) and a few reserved families report ``is_global=True``, so
    multicast/reserved/unspecified are excluded explicitly. Loopback,
    link-local, private (RFC1918/4193), CGNAT (RFC6598), documentation and
    benchmark ranges all report ``is_global=False`` and are rejected.
    """
    ip = _normalized_ip(str(value))
    if not ip.is_global:
        return False
    return not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def resolve_addresses(host: str, port: int) -> tuple[str, ...]:
    """All unique socket addresses for ``host`` as normalized IP strings.

    Raises :class:`OutboundSecurityError` when the host cannot be resolved.
    IPv4-mapped IPv6 answers are collapsed to their IPv4 form so mixed
    A/AAAA results classify consistently.
    """
    if not isinstance(host, str) or not host or len(host) > 253:
        raise OutboundSecurityError("invalid hostname")
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OutboundSecurityError(f"hostname could not be resolved ({host})") from exc
    addresses: list[str] = []
    for info in infos:
        raw_address = info[4][0]
        ip = _normalized_ip(raw_address)
        addresses.append(str(ip))
    return tuple(dict.fromkeys(addresses))


def require_public_resolution(host: str, port: int) -> str:
    """Resolve ``host`` and require every answer to be globally routable.

    Fail-closed on mixed DNS answers: if the resolver alternates between a
    public and a private address (rebinding), the resolution is rejected as
    a whole. Returns the first public address in presentation format.
    """
    addresses = resolve_addresses(host, port)
    if not addresses:
        raise OutboundSecurityError(f"hostname resolved to no addresses ({host})")
    for address in addresses:
        if not is_public_address(address):
            raise OutboundSecurityError(
                f"hostname resolves to a non-public address ({address})"
            )
    return addresses[0]


@dataclass(frozen=True)
class Origin:
    """A validated outbound origin (scheme, host, port) plus original URL."""

    scheme: str
    host: str
    port: int
    url: str


def _enforce_scheme_policy(scheme: str, allow_http: bool) -> None:
    """Arbitrary origins default to HTTPS; HTTP is an explicit opt-in only.

    Known/internal providers that legitimately use plain HTTP (operator
    config, not user input) pass ``allow_http=True``; every user-supplied
    origin must fail closed on a plain-HTTP URL.
    """
    if scheme == "http" and not allow_http:
        raise OutboundSecurityError("outbound URL must use https")


def parse_origin(url: str) -> Origin:
    """Parse and validate the shape of an outbound HTTP(S) URL.

    Rejects non-http(s) schemes, empty hostnames, embedded userinfo
    (credential smuggling / host confusion) and malformed ports. Does not
    touch the network. Scheme policy (HTTPS by default) is enforced by
    :func:`validate_public_url` / :func:`resolve_redirect_url`.
    """
    if not isinstance(url, str) or not url.strip():
        raise OutboundSecurityError("outbound URL is required")
    try:
        parsed = urlsplit(url.strip())
    except ValueError as exc:
        raise OutboundSecurityError("outbound URL is malformed") from exc
    scheme = (parsed.scheme or "").lower()
    if scheme not in PUBLIC_SCHEMES:
        raise OutboundSecurityError("outbound URL must use http or https")
    host = parsed.hostname
    if not host:
        raise OutboundSecurityError("outbound URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise OutboundSecurityError("outbound URL must not embed credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise OutboundSecurityError("outbound URL has an invalid port") from exc
    if port is None:
        port = DEFAULT_PORTS[scheme]
    return Origin(scheme=scheme, host=host, port=port, url=url.strip())


def validate_public_url(
    url: str, *, resolve: bool = True, allow_http: bool = False
) -> str:
    """Validate an arbitrary outbound URL against the SSRF policy.

    Plain-HTTP origins are rejected unless ``allow_http=True`` (explicit
    opt-in for known/internal providers). With ``resolve=True`` (default)
    the hostname is resolved and every answer must be a globally routable
    address; mixed answers fail closed. With ``resolve=False`` only the URL
    shape is checked (for callers that resolve later, e.g. during a manual
    redirect chain).
    """
    origin = parse_origin(url)
    _enforce_scheme_policy(origin.scheme, allow_http)
    if resolve:
        require_public_resolution(origin.host, origin.port)
    return origin.url


def validate_provider_origin(url: str, *, allow_http: bool = False) -> str:
    """Validate an operator-configured provider origin.

    Applies the same HTTPS-by-default and public-only DNS policy as
    :func:`validate_public_url`; there is deliberately no private-origin
    escape hatch at runtime (every configured provider is external).
    """
    origin = parse_origin(url)
    _enforce_scheme_policy(origin.scheme, allow_http)
    require_public_resolution(origin.host, origin.port)
    return origin.url


def resolve_redirect_url(
    current: str, location: str, *, allow_http: bool = False
) -> str:
    """Join a redirect ``location`` against ``current`` and validate its shape.

    Plain-HTTP targets are rejected unless ``allow_http=True``, so an
    HTTPS-to-HTTP downgrade in a redirect chain fails closed. Resolution
    (DNS) is left to the caller so each hop can be re-validated against the
    full policy at send time.
    """
    if not isinstance(location, str) or not location.strip():
        raise OutboundSecurityError("redirect location is missing")
    joined = urljoin(current, location.strip())
    origin = parse_origin(joined)
    _enforce_scheme_policy(origin.scheme, allow_http)
    return origin.url


__all__ = [
    "DEFAULT_PORTS",
    "Origin",
    "OutboundSecurityError",
    "PUBLIC_SCHEMES",
    "address_class",
    "is_public_address",
    "parse_origin",
    "require_public_resolution",
    "resolve_addresses",
    "resolve_redirect_url",
    "validate_provider_origin",
    "validate_public_url",
]
