import json
from uuid import UUID

import httpx
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

from logging_config import get_logger
from routes.json.triggers import ORCHESTRATOR_URL, _internal_basic_auth

router = APIRouter(prefix="/investment", tags=["investment"])
logger = get_logger("api.investment")
MAX_DOCUMENT_BYTES = 20_000_000


async def _orchestrator_request(request: Request, method: str, path: str, **kwargs) -> httpx.Response:
    try:
        auth = _internal_basic_auth()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Internal authentication unavailable") from exc
    try:
        client = request.app.state.orchestrator_client
        return await client.request(
            method,
            f"{ORCHESTRATOR_URL}{path}",
            auth=auth,
            **kwargs,
        )
    except (AttributeError, TypeError):
        timeout = kwargs.pop("timeout", 30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.request(
                method,
                f"{ORCHESTRATOR_URL}{path}",
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


def _payload_or_error(response: httpx.Response):
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        payload = None
    if response.is_success and isinstance(payload, (dict, list)):
        return payload
    detail = payload.get("detail") if isinstance(payload, dict) else None
    safe_status = response.status_code if response.status_code in {404, 409, 413, 422, 429, 502, 503} else 502
    raise HTTPException(status_code=safe_status, detail=detail or "Investment service request failed")


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


@router.post("/documents", status_code=201)
async def ingest_investment_document(request: Request):
    declared_size = request.headers.get("content-length")
    if declared_size:
        try:
            if int(declared_size) > MAX_DOCUMENT_BYTES:
                raise HTTPException(status_code=413, detail="Document exceeds 20 MB")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length")

    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_DOCUMENT_BYTES:
            raise HTTPException(status_code=413, detail="Document exceeds 20 MB")
        chunks.append(chunk)
    content = b"".join(chunks)
    response = await _orchestrator_request(
        request,
        "POST",
        "/investment/documents",
        params=dict(request.query_params),
        content=content,
        headers={"Content-Type": request.headers.get("content-type", "application/octet-stream")},
        timeout=45.0,
    )
    return JSONResponse(status_code=201, content=_payload_or_error(response))


@router.post("/urls", status_code=201)
async def ingest_investment_url(request: Request, body: dict = Body(...)):
    response = await _orchestrator_request(
        request,
        "POST",
        "/investment/urls",
        json=body,
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
        timeout=120.0,
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
async def trigger_filing_collection(request: Request, body: dict | None = Body(default=None)):
    response = await _orchestrator_request(
        request,
        "POST",
        "/investment/filings/collect",
        json=body or {},
        timeout=30.0,
    )
    return JSONResponse(status_code=202, content=_payload_or_error(response))
