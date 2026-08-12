"""Shared resolve-and-pin HTTP transport for arbitrary user-supplied origins.

Single implementation consumed by BOTH the API and the orchestrator so the
SSRF policy (``contracts.outbound_security``) and the transport behavior are
never duplicated:

* every request host is resolved at send time and ALL answers must be
  globally routable (fail-closed on mixed DNS and rebinding);
* the connection is pinned to the first validated address while the original
  hostname is preserved for SNI (``sni_hostname``) and the ``Host`` header;
* HTTPS only; redirect hops re-enter this transport and are re-validated;
* connection pooling is isolated PER ORIGINAL ORIGIN (scheme, host, port):
  distinct hosts that resolve to the same CDN IP can never reuse one TLS
  connection, so a ``Host`` header or credential cannot ride a connection
  established for a different host without a fresh certificate/SNI check;
* the ORIGINAL request is reattached to each response so httpx relative
  redirect resolution and consumers always see the caller's URL;
* ``close()`` releases every child transport.

Arbitrary-origin payloads are bounded (GET document/URL and connection-test
paths today); the body is materialized so the pinned rewrite is a faithful
copy, so callers must keep these payloads size-capped.
"""

from __future__ import annotations

import threading

import httpx

from contracts.outbound_security import (
    OutboundSecurityError,
    require_public_resolution,
)


class PublicOnlyHTTPTransport(httpx.HTTPTransport):
    """Resolve-and-pin transport with per-origin connection pools."""

    _CHILD_LIMITS = httpx.Limits(
        max_connections=16, max_keepalive_connections=4, keepalive_expiry=30.0
    )
    _MAX_CHILD_TRANSPORTS = 32

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._child_transports: dict[
            tuple[str, str, int], httpx.HTTPTransport
        ] = {}
        self._children_lock = threading.Lock()

    def _child_transport(
        self, origin_key: tuple[str, str, int]
    ) -> httpx.HTTPTransport:
        with self._children_lock:
            child = self._child_transports.get(origin_key)
            if child is None:
                child = httpx.HTTPTransport(limits=self._CHILD_LIMITS)
                if len(self._child_transports) >= self._MAX_CHILD_TRANSPORTS:
                    oldest_key, oldest = next(
                        iter(self._child_transports.items())
                    )
                    del self._child_transports[oldest_key]
                    oldest.close()
                self._child_transports[origin_key] = child
            return child

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.scheme != "https":
            raise OutboundSecurityError("outbound URL must use https")
        host = request.url.host
        if not host:
            raise OutboundSecurityError("outbound URL must include a hostname")
        port = request.url.port or 443
        pinned = require_public_resolution(host, port)
        url = httpx.URL(
            scheme=request.url.scheme,
            host=f"[{pinned}]" if ":" in pinned else pinned,
            port=request.url.port,
            path=request.url.path,
            query=request.url.query,
            fragment=request.url.fragment,
        )
        original_netloc = request.url.netloc.decode("ascii")
        new_request = httpx.Request(
            method=request.method,
            url=url,
            headers=request.headers,
            content=request.read(),
            extensions={**request.extensions, "sni_hostname": host},
        )
        new_request.headers["Host"] = original_netloc
        response = self._child_transport(
            (request.url.scheme, host, port)
        ).handle_request(new_request)
        response.request = request
        return response

    def close(self) -> None:
        with self._children_lock:
            children = list(self._child_transports.values())
            self._child_transports.clear()
        for child in children:
            child.close()
        super().close()


__all__ = ["PublicOnlyHTTPTransport"]
