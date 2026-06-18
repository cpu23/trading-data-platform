import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Body, HTTPException, Request

from budgets import (
    get_budget_status,
    mark_override_dispatch_failed,
    register_manual_override,
)
from logging_config import get_logger

router = APIRouter()
logger = get_logger("api.triggers")

ORCHESTRATOR_URL = "http://orchestrator:8000"


def _orchestrator_job_payload(response: httpx.Response, fallback_id: str, now: str) -> dict:
    payload = response.json()
    job_id = payload.get("job_id", fallback_id)
    return {
        "job_id": job_id,
        "accepted_at": payload.get("accepted_at", now),
    }


def _manual_override(body: dict | None, request: Request) -> dict | None:
    if not isinstance(body, dict) or body.get("budget_override") is not True:
        return None

    reason = str(body.get("override_reason", "")).strip()
    if not reason:
        raise HTTPException(
            status_code=422,
            detail="override_reason is required when budget_override is true",
        )

    client_host = request.client.host if request.client else "unknown"
    return {
        "reason": reason,
        "requested_by": f"authenticated_api_user@{client_host}",
    }


def _enforce_api_budget(override: dict | None) -> dict:
    budget = get_budget_status()
    if not budget["paid_calls_allowed"] and override is None:
        logger.warning(
            "paid_trigger_budget_denied",
            today_cost_usd=budget["today_cost_usd"],
            budget_cap_usd=budget["budget_cap_usd"],
        )
        raise HTTPException(
            status_code=429,
            detail={
                "message": "Daily LLM budget reached",
                "budget": budget,
                "override": {
                    "available": True,
                    "required_fields": ["budget_override", "override_reason"],
                },
            },
        )
    return budget


@router.post("/collect/{source_id}", status_code=202)
async def trigger_collect(source_id: str):
    correlation_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{ORCHESTRATOR_URL}/run_collector/{source_id}",
                json={"correlation_id": correlation_id},
            )
    except httpx.ConnectError as exc:
        logger.error("orchestrator_connect_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="Orchestrator unavailable")

    if response.status_code != 202:
        logger.error("orchestrator_unexpected_response", status=response.status_code, body=response.text)
        raise HTTPException(status_code=502, detail="Orchestrator returned unexpected response")

    payload = _orchestrator_job_payload(response, correlation_id, now)
    payload["status_url"] = f"/api/system/logs?component={source_id}"
    return payload


@router.post("/process/{processor_id}", status_code=202)
async def trigger_process(
    processor_id: str,
    request: Request,
    body: dict | None = Body(default=None),
):
    correlation_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    override_request = _manual_override(body, request)
    budget = _enforce_api_budget(override_request)
    override = None

    if override_request:
        override = register_manual_override(
            correlation_id=correlation_id,
            run_kind="processor",
            requested_component=processor_id,
            **override_request,
        )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{ORCHESTRATOR_URL}/run_processor/{processor_id}",
                json={"correlation_id": correlation_id},
            )
    except httpx.ConnectError as exc:
        if override:
            mark_override_dispatch_failed(correlation_id, str(exc))
        logger.error("orchestrator_connect_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="Orchestrator unavailable")

    if response.status_code != 202:
        if override:
            mark_override_dispatch_failed(
                correlation_id,
                f"orchestrator returned HTTP {response.status_code}",
            )
        logger.error("orchestrator_unexpected_response", status=response.status_code, body=response.text)
        raise HTTPException(status_code=502, detail="Orchestrator returned unexpected response")

    payload = _orchestrator_job_payload(response, correlation_id, now)
    payload["status_url"] = f"/api/system/logs?component={processor_id}"
    payload["budget"] = budget
    payload["budget_override"] = override
    return payload


@router.post("/cycle", status_code=202)
async def trigger_cycle(
    request: Request,
    body: dict | None = Body(default=None),
):
    correlation_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    override_request = _manual_override(body, request)
    budget = get_budget_status()
    override = None

    if override_request:
        override = register_manual_override(
            correlation_id=correlation_id,
            run_kind="cycle",
            requested_component=None,
            **override_request,
        )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{ORCHESTRATOR_URL}/run_cycle",
                json={"correlation_id": correlation_id},
            )
    except httpx.ConnectError as exc:
        if override:
            mark_override_dispatch_failed(correlation_id, str(exc))
        logger.error("orchestrator_connect_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="Orchestrator unavailable")

    if response.status_code == 409:
        if override:
            mark_override_dispatch_failed(correlation_id, "Cycle already running")
        raise HTTPException(status_code=409, detail=response.json().get("detail", "Cycle already running"))

    if response.status_code != 202:
        if override:
            mark_override_dispatch_failed(
                correlation_id,
                f"orchestrator returned HTTP {response.status_code}",
            )
        logger.error("orchestrator_unexpected_response", status=response.status_code, body=response.text)
        raise HTTPException(status_code=502, detail="Orchestrator returned unexpected response")

    payload = _orchestrator_job_payload(response, correlation_id, now)
    payload["status_url"] = f"/api/system/logs?correlation_id={payload['job_id']}"
    payload["budget"] = budget
    payload["budget_override"] = override
    return payload
