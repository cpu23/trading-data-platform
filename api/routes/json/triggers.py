import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException

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
async def trigger_process(processor_id: str):
    correlation_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{ORCHESTRATOR_URL}/run_processor/{processor_id}",
                json={"correlation_id": correlation_id},
            )
    except httpx.ConnectError as exc:
        logger.error("orchestrator_connect_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="Orchestrator unavailable")

    if response.status_code != 202:
        logger.error("orchestrator_unexpected_response", status=response.status_code, body=response.text)
        raise HTTPException(status_code=502, detail="Orchestrator returned unexpected response")

    payload = _orchestrator_job_payload(response, correlation_id, now)
    payload["status_url"] = f"/api/system/logs?component={processor_id}"
    return payload


@router.post("/cycle", status_code=202)
async def trigger_cycle():
    correlation_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{ORCHESTRATOR_URL}/run_cycle",
                json={"correlation_id": correlation_id},
            )
    except httpx.ConnectError as exc:
        logger.error("orchestrator_connect_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="Orchestrator unavailable")

    if response.status_code == 409:
        raise HTTPException(status_code=409, detail=response.json().get("detail", "Cycle already running"))

    if response.status_code != 202:
        logger.error("orchestrator_unexpected_response", status=response.status_code, body=response.text)
        raise HTTPException(status_code=502, detail="Orchestrator returned unexpected response")

    payload = _orchestrator_job_payload(response, correlation_id, now)
    payload["status_url"] = f"/api/system/logs?correlation_id={payload['job_id']}"
    return payload


@router.post("/financial-times", status_code=202)
async def trigger_financial_times(body: dict | None = None):
    correlation_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{ORCHESTRATOR_URL}/run_financial_times",
                json={**(body or {}), "correlation_id": correlation_id},
            )
    except httpx.ConnectError as exc:
        logger.error("orchestrator_connect_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="Orchestrator unavailable")

    if response.status_code == 409:
        raise HTTPException(
            status_code=409,
            detail=response.json().get("detail", "FT collection already running"),
        )

    if response.status_code != 202:
        logger.error(
            "orchestrator_unexpected_response",
            status=response.status_code,
            body=response.text,
        )
        raise HTTPException(
            status_code=502,
            detail="Orchestrator returned unexpected response",
        )

    payload = _orchestrator_job_payload(response, correlation_id, now)
    payload["status_url"] = f"/api/system/logs?correlation_id={payload['job_id']}"
    return payload
