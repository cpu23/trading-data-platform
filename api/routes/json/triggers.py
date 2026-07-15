import os
import uuid
from datetime import datetime, timezone
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, StrictBool

from logging_config import get_logger

router = APIRouter()
logger = get_logger("api.triggers")

ORCHESTRATOR_URL = "http://orchestrator:8000"

# Component ID registries — keep in sync with orchestrator/collectors/__init__.py
# and orchestrator/processors/__init__.py
_VALID_COLLECTORS = frozenset({"fred", "forex_factory", "oanda"})
_VALID_PROCESSORS = frozenset({"macro_regime", "event_impact", "briefing"})
_VALID_NEWS_SOURCES = frozenset({"reuters", "kobeissi"})


class CycleRequest(BaseModel):
    mode: Literal["refresh", "analyze", "force_full"] = "refresh"
    budget_confirmed: StrictBool = False


def _internal_basic_auth() -> httpx.BasicAuth:
    username = os.environ.get("DASHBOARD_USER", "")
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("internal credentials unavailable")
    return httpx.BasicAuth(username, password)


def _orchestrator_job_payload(response: httpx.Response, fallback_id: str, now: str) -> dict:
    payload = response.json()
    job_id = payload.get("job_id", fallback_id)
    return {
        "job_id": job_id,
        "accepted_at": payload.get("accepted_at", now),
    }


@router.post("/collect/{source_id}", status_code=202)
async def trigger_collect(source_id: str, request: Request):
    if source_id not in _VALID_COLLECTORS:
        raise HTTPException(status_code=404, detail=f"Unknown collector: {source_id}")

    correlation_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    try:
        response = await request.app.state.orchestrator_client.post(
            f"{ORCHESTRATOR_URL}/run_collector/{source_id}",
            json={"correlation_id": correlation_id}, timeout=10.0,
        )
    except httpx.TransportError as exc:
        logger.error("orchestrator_connect_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="Orchestrator unavailable")

    if response.status_code != 202:
        logger.error("orchestrator_unexpected_response", status=response.status_code)
        raise HTTPException(status_code=502, detail="Orchestrator returned unexpected response")

    payload = _orchestrator_job_payload(response, correlation_id, now)
    payload["status_url"] = f"/api/system/logs?component={source_id}"
    return payload


@router.post("/process/{processor_id}", status_code=202)
async def trigger_process(processor_id: str, request: Request):
    if processor_id not in _VALID_PROCESSORS:
        raise HTTPException(status_code=404, detail=f"Unknown processor: {processor_id}")

    correlation_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    try:
        response = await request.app.state.orchestrator_client.post(
            f"{ORCHESTRATOR_URL}/run_processor/{processor_id}",
            json={"correlation_id": correlation_id}, timeout=10.0,
        )
    except httpx.TransportError as exc:
        logger.error("orchestrator_connect_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="Orchestrator unavailable")

    if response.status_code != 202:
        logger.error("orchestrator_unexpected_response", status=response.status_code)
        raise HTTPException(status_code=502, detail="Orchestrator returned unexpected response")

    payload = _orchestrator_job_payload(response, correlation_id, now)
    payload["status_url"] = f"/api/system/logs?component={processor_id}"
    return payload


@router.post("/triggers/news/{source_id}", status_code=202)
async def trigger_news(source_id: str, request: Request):
    if source_id not in _VALID_NEWS_SOURCES:
        raise HTTPException(status_code=404, detail=f"Unknown news source: {source_id}")

    correlation_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    try:
        response = await request.app.state.orchestrator_client.post(
            f"{ORCHESTRATOR_URL}/run_news/{source_id}",
            json={"correlation_id": correlation_id}, timeout=10.0,
        )
    except httpx.TransportError as exc:
        logger.error("orchestrator_connect_failed", error=type(exc).__name__)
        raise HTTPException(status_code=503, detail="Orchestrator unavailable") from exc

    if response.status_code in {409, 503}:
        try:
            detail = response.json().get("detail", "News request rejected")
        except (TypeError, ValueError):
            detail = "News request rejected"
        raise HTTPException(status_code=response.status_code, detail=detail)
    if response.status_code != 202:
        logger.error("orchestrator_unexpected_response", status=response.status_code)
        raise HTTPException(status_code=503, detail="Orchestrator unavailable")

    payload = _orchestrator_job_payload(response, correlation_id, now)
    payload["status_url"] = f"/api/system/logs?correlation_id={payload['job_id']}"
    return payload


@router.post("/cycle", status_code=202, include_in_schema=False)
@router.post("/triggers/cycle", status_code=202)
async def trigger_cycle(request: Request, body: CycleRequest | None = None):
    body = body or CycleRequest()
    correlation_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    if body.mode == "force_full" and body.budget_confirmed is not True:
        raise HTTPException(
            status_code=422,
            detail="force_full requires explicit budget_confirmed=true",
        )

    auth = None
    if body.mode == "force_full":
        try:
            auth = _internal_basic_auth()
        except RuntimeError as exc:
            raise HTTPException(
                status_code=503, detail="Internal authentication unavailable"
            ) from exc

    request_kwargs = {
        "json": {
            "correlation_id": correlation_id,
            "mode": body.mode,
            "budget_confirmed": body.budget_confirmed,
        },
        "timeout": 10.0,
    }
    if auth is not None:
        request_kwargs["auth"] = auth

    try:
        response = await request.app.state.orchestrator_client.post(
            f"{ORCHESTRATOR_URL}/run_cycle",
            **request_kwargs,
        )
    except httpx.TransportError as exc:
        logger.error("orchestrator_connect_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="Orchestrator unavailable")

    if response.status_code in {409, 422, 503}:
        fallback = "Cycle already running" if response.status_code == 409 else "Cycle request rejected"
        raise HTTPException(
            status_code=response.status_code,
            detail=response.json().get("detail", fallback),
        )

    if response.status_code != 202:
        logger.error("orchestrator_unexpected_response", status=response.status_code)
        raise HTTPException(status_code=502, detail="Orchestrator returned unexpected response")

    payload = _orchestrator_job_payload(response, correlation_id, now)
    payload["status_url"] = f"/api/system/logs?correlation_id={payload['job_id']}"
    return payload
