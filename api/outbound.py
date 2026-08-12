"""API-side alias for the shared resolve-and-pin transport.

The single transport implementation lives in ``contracts.outbound_transport``
(consumed by both API and orchestrator); this module only re-exports it under
the name the API routes import, with no network logic of its own.
"""

from __future__ import annotations

from contracts.outbound_transport import PublicOnlyHTTPTransport as PublicOnlyTransport

__all__ = ["PublicOnlyTransport"]
