import os
from typing import Any, cast

import httpx
from fastapi import HTTPException, Request

from config import orchestrator_url
from logging_config import get_logger

logger = get_logger("api.orchestrator")


def internal_basic_auth() -> httpx.BasicAuth:
    """Return BasicAuth credentials for internal orchestrator dispatch."""
    username = os.environ.get("DASHBOARD_USER", "")
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("internal credentials unavailable")
    return httpx.BasicAuth(username, password)



async def orchestrator_request(
    request: Request,
    method: str,
    path: str,
    *,
    auth: httpx.Auth | None = None,
    raise_for_transport: bool = True,
    **kwargs: Any,
) -> httpx.Response:
    """Forward a request through the shared app orchestrator client.

    Handles internal basic authentication, URL resolution against orchestrator_url(),
    client capability verification, and standard transport failure translation.
    """
    if auth is None:
        try:
            auth = internal_basic_auth()
        except RuntimeError as exc:
            raise HTTPException(
                status_code=503, detail="Internal authentication unavailable"
            ) from exc

    app_state = getattr(getattr(request, "app", None), "state", None)
    client = (
        getattr(app_state, "orchestrator_client", None)
        if app_state is not None
        else None
    )
    if client is None:
        logger.error("orchestrator_client_unavailable", method=method, path=path)
        raise HTTPException(
            status_code=503, detail="Orchestrator client unavailable"
        )

    clean_path = path if path.startswith("/") else f"/{path}"
    url = f"{orchestrator_url()}{clean_path}"
    try:
        http_method = getattr(client, method.lower())
        return cast(httpx.Response, await http_method(url, auth=auth, **kwargs))
    except httpx.TransportError as exc:
        if raise_for_transport:
            logger.error(
                "orchestrator_connect_failed",
                method=method,
                path=path,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise HTTPException(
                status_code=503, detail="Orchestrator unavailable"
            ) from exc
        raise
    except (AttributeError, TypeError) as exc:
        logger.error(
            "orchestrator_client_unusable",
            method=method,
            path=path,
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=503, detail="Orchestrator client unavailable"
        ) from exc


async def orchestrator_post(
    request: Request,
    path: str,
    *,
    auth: httpx.Auth | None = None,
    raise_for_transport: bool = True,
    **kwargs: Any,
) -> httpx.Response:
    """Convenience helper for POST requests to the internal orchestrator."""
    return await orchestrator_request(
        request,
        "POST",
        path,
        auth=auth,
        raise_for_transport=raise_for_transport,
        **kwargs,
    )
