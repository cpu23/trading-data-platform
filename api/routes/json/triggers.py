import os
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

# Component ID registries — keep in sync with orchestrator/collectors/__init__.py
# and orchestrator/processors/__init__.py
_VALID_COLLECTORS = frozenset({"fred", "forex_factory", "oanda"})
_VALID_PROCESSORS = frozenset({"macro_regime", "event_impact", "briefing"})
_VALID_NEWS_SOURCES = frozenset({"reuters", "kobeissi"})

_VALID_MODES = frozenset({"refresh", "analyze", "force_full"})


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
    if not budget.get("paid_calls_allowed", True) and override is None:
        logger.warning(
            "paid_trigger_budget_denied",
            today_cost_usd=budget.get("today_cost_usd"),
            budget_cap_usd=budget.get("budget_cap_usd"),
        )
        raise HTTPException(
            status_code=429,
            detail={
                "message": "Daily LLM budget reached",
                "budget": budget,
                "override": {
                    "supported": True,
                    "body_fields": {
                        "budget_override": True,
                        "override_reason": "required, non-empty string",
                    },
                },
            },
        )
    return budget


async def _post_to_orchestrator(request: Request, url: str, **kwargs) -> httpx.Response:
    """Use the shared app client when available; fall back to a new client for direct calls."""
    try:
        client = request.app.state.orchestrator_client
        return await client.post(url, **kwargs)
    except (AttributeError, TypeError):
        # AttributeError: no shared client (direct endpoint calls)
        # TypeError: shared client is a plain Mock (budget tests)
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await client.post(url, **kwargs)


@router.post("/collect/{source_id}", status_code=202)
async def trigger_collect(source_id: str, request: Request):
    if source_id not in _VALID_COLLECTORS:
        raise HTTPException(status_code=404, detail=f"Unknown collector: {source_id}")

    correlation_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    try:
        response = await _post_to_orchestrator(
            request,
            f"{ORCHESTRATOR_URL}/run_collector/{source_id}",
            json={"correlation_id": correlation_id}, timeout=10.0, auth=_internal_basic_auth(),
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
async def trigger_process(
    processor_id: str,
    request: Request,
    body: dict | None = Body(default=None),
):
    if processor_id not in _VALID_PROCESSORS:
        raise HTTPException(status_code=404, detail=f"Unknown processor: {processor_id}")

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
        response = await _post_to_orchestrator(
            request,
            f"{ORCHESTRATOR_URL}/run_processor/{processor_id}",
            json={"correlation_id": correlation_id}, timeout=10.0, auth=_internal_basic_auth(),
        )
    except httpx.TransportError as exc:
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
        logger.error("orchestrator_unexpected_response", status=response.status_code)
        raise HTTPException(status_code=502, detail="Orchestrator returned unexpected response")

    payload = _orchestrator_job_payload(response, correlation_id, now)
    payload["status_url"] = f"/api/system/logs?component={processor_id}"
    payload["budget"] = budget
    payload["budget_override"] = override
    return payload


@router.post("/triggers/news/{source_id}", status_code=202)
async def trigger_news(source_id: str, request: Request):
    if source_id not in _VALID_NEWS_SOURCES:
        raise HTTPException(status_code=404, detail=f"Unknown news source: {source_id}")

    correlation_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    try:
        response = await _post_to_orchestrator(
            request,
            f"{ORCHESTRATOR_URL}/run_news/{source_id}",
            json={"correlation_id": correlation_id}, timeout=10.0, auth=_internal_basic_auth(),
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
async def trigger_cycle(request: Request, body: dict | None = Body(default=None)):
    correlation_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # Validate and extract mode / budget_confirmed from the raw body dict.
    mode = "refresh"
    budget_confirmed = False
    if body is not None:
        if "mode" in body:
            mode = body["mode"]
            if not isinstance(mode, str) or mode not in _VALID_MODES:
                raise HTTPException(status_code=422, detail=f"Invalid mode: {mode!r}")
        if "budget_confirmed" in body:
            budget_confirmed = body["budget_confirmed"]
            if not isinstance(budget_confirmed, bool):
                raise HTTPException(status_code=422, detail="budget_confirmed must be a boolean")

    if mode == "force_full" and budget_confirmed is not True:
        raise HTTPException(
            status_code=422,
            detail="force_full requires explicit budget_confirmed=true",
        )

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
        auth = _internal_basic_auth()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Internal authentication unavailable") from exc

    try:
        response = await _post_to_orchestrator(
            request,
            f"{ORCHESTRATOR_URL}/run_cycle",
            json={
                "correlation_id": correlation_id,
                "mode": mode,
                "budget_confirmed": budget_confirmed,
            },
            timeout=10.0,
            auth=auth,
        )
    except httpx.TransportError as exc:
        if override:
            mark_override_dispatch_failed(correlation_id, str(exc))
        logger.error("orchestrator_connect_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="Orchestrator unavailable")

    if response.status_code in {409, 422, 503}:
        if override:
            mark_override_dispatch_failed(correlation_id, f"orchestrator returned HTTP {response.status_code}")
        fallback = "Cycle already running" if response.status_code == 409 else "Cycle request rejected"
        raise HTTPException(
            status_code=response.status_code,
            detail=response.json().get("detail", fallback),
        )

    if response.status_code != 202:
        if override:
            mark_override_dispatch_failed(correlation_id, f"orchestrator returned HTTP {response.status_code}")
        logger.error("orchestrator_unexpected_response", status=response.status_code)
        raise HTTPException(status_code=502, detail="Orchestrator returned unexpected response")

    payload = _orchestrator_job_payload(response, correlation_id, now)
    payload["status_url"] = f"/api/system/logs?correlation_id={payload['job_id']}"
    payload["budget"] = budget
    payload["budget_override"] = override
    return payload
