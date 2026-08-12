import json
import os
import tempfile
from uuid import UUID

import httpx
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

from config import orchestrator_url
from contracts import InvestmentUrlIngestRequest
from logging_config import get_logger
from routes.json.triggers import _internal_basic_auth

router = APIRouter(prefix="/investment", tags=["investment"])
logger = get_logger("api.investment")
MAX_DOCUMENT_BYTES = 20_000_000
_UPLOAD_CHUNK_BYTES = 65_536


async def _orchestrator_request(
    request: Request, method: str, path: str, **kwargs
) -> httpx.Response:
    """Forward through the shared app client; capability is checked before send.

    There is deliberately no client fallback: a POST that reaches this
    function is sent exactly once, so a missing or unusable client fails
    closed (503) instead of re-sending the request on a second client.
    """
    try:
        auth = _internal_basic_auth()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503, detail="Internal authentication unavailable"
        ) from exc
    client = getattr(request.app.state, "orchestrator_client", None)
    if client is None:
        raise HTTPException(
            status_code=503, detail="Orchestrator client unavailable"
        )
    try:
        return await client.request(
            method,
            f"{orchestrator_url()}{path}",
            auth=auth,
            **kwargs,
        )
    except httpx.TransportError as exc:
        logger.error(
            "investment_orchestrator_unavailable",
            method=method,
            path=path,
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="Orchestrator unavailable") from exc
    except (AttributeError, TypeError) as exc:
        # An unusable client must never trigger a second send of the request.
        logger.error(
            "investment_orchestrator_client_unusable",
            method=method,
            path=path,
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=503, detail="Orchestrator client unavailable"
        ) from exc


def _payload_or_error(response: httpx.Response):
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        payload = None
    if response.is_success and isinstance(payload, (dict, list)):
        return payload
    detail = payload.get("detail") if isinstance(payload, dict) else None
    safe_status = (
        response.status_code
        if response.status_code in {404, 409, 413, 422, 429, 502, 503}
        else 502
    )
    raise HTTPException(
        status_code=safe_status, detail=detail or "Investment service request failed"
    )


@router.get("/dashboard")
async def investment_dashboard(request: Request):
    response = await _orchestrator_request(
        request,
        "GET",
        "/investment/dashboard",
        timeout=30.0,
    )
    return _payload_or_error(response)


@router.get("/analyses/{analysis_id}")
async def investment_analysis(analysis_id: UUID, request: Request):
    response = await _orchestrator_request(
        request,
        "GET",
        f"/investment/analyses/{analysis_id}",
        timeout=30.0,
    )
    return _payload_or_error(response)


def _reject_declared_oversize(request: Request) -> None:
    declared = request.headers.get("content-length")
    if not declared:
        return
    try:
        declared_size = int(declared)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Content-Length")
    if declared_size > MAX_DOCUMENT_BYTES:
        raise HTTPException(status_code=413, detail="Document exceeds 20 MB")


async def _spooled_body(handle):
    """Read the spooled upload in bounded chunks for chunked forwarding."""
    while True:
        chunk = handle.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            return
        yield chunk


@router.post("/documents", status_code=201)
async def ingest_investment_document(request: Request):
    """Stream the inbound upload to temp storage, then forward exactly once.

    The declared Content-Length is rejected early; the running chunk total is
    capped while streaming (so chunked bodies cannot bypass the cap), and the
    spool file is always removed, success or failure.
    """
    _reject_declared_oversize(request)
    spool_path: str | None = None
    try:
        spool = tempfile.NamedTemporaryFile(
            prefix="investment-upload-", suffix=".bin", delete=False
        )
        spool_path = spool.name
        try:
            total = 0
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_DOCUMENT_BYTES:
                    raise HTTPException(status_code=413, detail="Document exceeds 20 MB")
                spool.write(chunk)
        finally:
            spool.close()
        with open(spool_path, "rb") as handle:
            response = await _orchestrator_request(
                request,
                "POST",
                "/investment/documents",
                params=dict(request.query_params),
                content=_spooled_body(handle),
                headers={
                    "Content-Type": request.headers.get(
                        "content-type", "application/octet-stream"
                    )
                },
                timeout=45.0,
            )
        return JSONResponse(status_code=201, content=_payload_or_error(response))
    finally:
        if spool_path is not None:
            try:
                os.unlink(spool_path)
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning(
                    "investment_upload_spool_cleanup_failed",
                    path=spool_path,
                )


@router.post("/urls", status_code=201)
async def ingest_investment_url(
    request: Request, body: InvestmentUrlIngestRequest = Body(...)
):
    response = await _orchestrator_request(
        request,
        "POST",
        "/investment/urls",
        json=body.model_dump(mode="json", exclude_none=True),
        timeout=45.0,
    )
    return JSONResponse(status_code=201, content=_payload_or_error(response))


@router.post("/documents/{document_id}/analyze")
async def run_investment_analysis(
    document_id: UUID,
    request: Request,
    body: dict | None = Body(default=None),
):
    response = await _orchestrator_request(
        request,
        "POST",
        f"/investment/documents/{document_id}/analyze",
        json=body or {},
        timeout=240.0,
    )
    return _payload_or_error(response)


@router.get("/filings/status")
async def investment_filings_status(request: Request):
    response = await _orchestrator_request(
        request,
        "GET",
        "/investment/filings/status",
        timeout=30.0,
    )
    return _payload_or_error(response)


@router.post("/filings/collect", status_code=202)
async def trigger_filing_collection(
    request: Request, body: dict | None = Body(default=None)
):
    response = await _orchestrator_request(
        request,
        "POST",
        "/investment/filings/collect",
        json=body or {},
        timeout=30.0,
    )
    return JSONResponse(status_code=202, content=_payload_or_error(response))
