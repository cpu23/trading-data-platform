"""Operator-config provider origin validation.

Known providers use canonical fixed origins (audited public HTTPS); any
custom configured origin must be HTTPS and resolve to globally routable
addresses. There is deliberately no private/local-provider escape hatch at
runtime: every configured provider endpoint is external. The validation
policy itself lives in ``contracts.outbound_security`` (shared, stdlib-only)
so API and orchestrator classify origins identically.
"""

from __future__ import annotations

from contracts.outbound_security import (
    OutboundSecurityError,
    validate_provider_origin,
)


def validate_configured_origin(
    url: object,
    section: dict,
    *,
    label: str,
    canonical: set[str] | frozenset[str] | tuple[str, ...] = (),
) -> str:
    """Validate one operator-configured provider origin.

    Known canonical defaults (audited public HTTPS constants) are accepted
    without DNS; any custom value must be HTTPS and resolve to globally
    routable addresses. Returns the normalized URL or raises ``ValueError``
    with a deterministic message.
    """
    value = str(url).strip() if url is not None else ""
    if not value:
        raise ValueError(f"{label} URL is required")
    if value in set(canonical):
        return value
    try:
        return validate_provider_origin(value)
    except OutboundSecurityError as exc:
        raise ValueError(f"invalid {label} URL ({exc})") from exc


__all__ = ["validate_configured_origin"]
