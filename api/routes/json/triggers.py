from __future__ import annotations

import uuid
from datetime import datetime

from api_budgets import (
    enforce_api_budget,
    extract_manual_override,
    get_budget_status,
    mark_override_dispatch_failed,
    register_manual_override,
)
from api_logging import get_logger
from fastapi import APIRouter, Body, HTTPException, Request
from jobs import accept_and_enqueue_operation
from run_lifecycle import RunAcceptanceConflict

import config as app_config
from contracts import RunAcceptanceRequest, RunAcceptedResponse
from contracts.runtime_config import (
    KNOWN_COLLECTORS,
    KNOWN_NEWS_SOURCES,
    KNOWN_PROCESSORS,
)

router = APIRouter()
logger = get_logger("api.triggers")

# Executable component registries — single source of truth shared with the
# configuration models (contracts.runtime_config).
_VALID_COLLECTORS = KNOWN_COLLECTORS
_VALID_PROCESSORS = KNOWN_PROCESSORS
_VALID_NEWS_SOURCES = KNOWN_NEWS_SOURCES


@router.post(
    "/collect/{source_id}", status_code=202, response_model=RunAcceptedResponse
)
async def trigger_collect(
    source_id: str,
    request: Request,
    body: dict | None = Body(default=None),
):
    if source_id not in _VALID_COLLECTORS:
        raise HTTPException(status_code=404, detail=f"Unknown collector: {source_id}")

    correlation_id = (
        str(body.get("correlation_id"))
        if isinstance(body, dict) and body.get("correlation_id")
        else str(uuid.uuid4())
    )
    idempotency_key = body.get("idempotency_key") if isinstance(body, dict) else None
    config = app_config.load_config()

    try:
        accepted_at, enqueued = accept_and_enqueue_operation(
            config,
            correlation_id=correlation_id,
            triggered_by="api",
            run_kind="collector",
            requested_component=source_id,
            idempotency_key=idempotency_key,
            request_summary={"mode": "refresh", "run_dependents": False},
            payload={"mode": "refresh", "run_dependents": False},
            max_attempts=3,
        )
    except RunAcceptanceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "run_acceptance_failed",
            correlation_id=correlation_id,
            run_kind="collector",
            component=source_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=503, detail="Run acceptance unavailable"
        ) from exc

    job_id = (
        str(enqueued.job.correlation_id)
        if enqueued and enqueued.job and enqueued.job.correlation_id
        else correlation_id
    )
    return {
        "job_id": job_id,
        "accepted_at": (
            accepted_at.isoformat()
            if isinstance(accepted_at, datetime)
            else str(accepted_at)
        ),
        "status_url": f"/api/system/logs?component={source_id}",
    }


@router.post(
    "/process/{processor_id}", status_code=202, response_model=RunAcceptedResponse
)
async def trigger_process(
    processor_id: str,
    request: Request,
    body: dict | None = Body(default=None),
):
    if processor_id not in _VALID_PROCESSORS:
        raise HTTPException(
            status_code=404, detail=f"Unknown processor: {processor_id}"
        )

    correlation_id = (
        str(body.get("correlation_id"))
        if isinstance(body, dict) and body.get("correlation_id")
        else str(uuid.uuid4())
    )
    idempotency_key = body.get("idempotency_key") if isinstance(body, dict) else None

    config = app_config.load_config()
    override_request = extract_manual_override(body, request)
    budget = enforce_api_budget(get_budget_status(config), override_request)
    override = None

    if override_request:
        override = register_manual_override(
            correlation_id=correlation_id,
            run_kind="processor",
            requested_component=processor_id,
            config=config,
            **override_request,
        )

    try:
        accepted_at, enqueued = accept_and_enqueue_operation(
            config,
            correlation_id=correlation_id,
            triggered_by="api",
            run_kind="processor",
            requested_component=processor_id,
            idempotency_key=idempotency_key,
            request_summary={"mode": "refresh"},
            payload={"mode": "refresh"},
            max_attempts=3,
        )
    except RunAcceptanceConflict as exc:
        if override:
            mark_override_dispatch_failed(correlation_id, str(exc), config=config)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        if override:
            mark_override_dispatch_failed(correlation_id, str(exc), config=config)
        logger.error(
            "run_acceptance_failed",
            correlation_id=correlation_id,
            run_kind="processor",
            component=processor_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Run acceptance unavailable",
                "correlation_id": correlation_id,
            },
        ) from exc

    job_id = (
        str(enqueued.job.correlation_id)
        if enqueued and enqueued.job and enqueued.job.correlation_id
        else correlation_id
    )
    return {
        "job_id": job_id,
        "accepted_at": (
            accepted_at.isoformat()
            if isinstance(accepted_at, datetime)
            else str(accepted_at)
        ),
        "status_url": f"/api/system/logs?component={processor_id}",
        "budget": budget,
        "budget_override": override,
    }


@router.post(
    "/triggers/news/{source_id}",
    status_code=202,
    response_model=RunAcceptedResponse,
)
async def trigger_news(
    source_id: str,
    request: Request,
    body: dict | None = Body(default=None),
):
    if source_id not in _VALID_NEWS_SOURCES:
        raise HTTPException(status_code=404, detail=f"Unknown news source: {source_id}")

    correlation_id = (
        str(body.get("correlation_id"))
        if isinstance(body, dict) and body.get("correlation_id")
        else str(uuid.uuid4())
    )
    idempotency_key = body.get("idempotency_key") if isinstance(body, dict) else None
    config = app_config.load_config()

    try:
        accepted_at, enqueued = accept_and_enqueue_operation(
            config,
            correlation_id=correlation_id,
            triggered_by="api",
            run_kind="news",
            requested_component=source_id,
            idempotency_key=idempotency_key,
            request_summary={"mode": "refresh"},
            payload={"mode": "refresh"},
            max_attempts=3,
        )
    except RunAcceptanceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "run_acceptance_failed",
            correlation_id=correlation_id,
            run_kind="news",
            component=source_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=503, detail="Run acceptance unavailable"
        ) from exc

    job_id = (
        str(enqueued.job.correlation_id)
        if enqueued and enqueued.job and enqueued.job.correlation_id
        else correlation_id
    )
    return {
        "job_id": job_id,
        "accepted_at": (
            accepted_at.isoformat()
            if isinstance(accepted_at, datetime)
            else str(accepted_at)
        ),
        "status_url": f"/api/system/logs?correlation_id={job_id}",
    }


@router.post("/cycle", status_code=202, include_in_schema=False)
@router.post(
    "/triggers/cycle",
    status_code=202,
    response_model=RunAcceptedResponse,
)
async def trigger_cycle(
    request: Request,
    body: RunAcceptanceRequest | None = Body(default=None),
):
    request_body = (
        body
        if isinstance(body, RunAcceptanceRequest)
        else RunAcceptanceRequest.model_validate(body or {})
    )
    mode = (
        request_body.mode.value
        if hasattr(request_body.mode, "value")
        else request_body.mode
    )
    budget_confirmed = request_body.budget_confirmed

    if mode == "force_full" and budget_confirmed is not True:
        raise HTTPException(
            status_code=422,
            detail="force_full requires explicit budget_confirmed=true",
        )

    correlation_id = (
        str(request_body.correlation_id)
        if request_body.correlation_id
        else str(uuid.uuid4())
    )
    idempotency_key = getattr(request_body, "idempotency_key", None)

    config = app_config.load_config()
    override_request = extract_manual_override(request_body, request)
    budget = get_budget_status(config)
    override = None

    if override_request:
        override = register_manual_override(
            correlation_id=correlation_id,
            run_kind="cycle",
            requested_component=None,
            config=config,
            **override_request,
        )

    request_summary = {
        "mode": mode,
        "budget_confirmed": mode == "force_full",
    }
    payload = {"mode": mode}

    try:
        accepted_at, enqueued = accept_and_enqueue_operation(
            config,
            correlation_id=correlation_id,
            triggered_by="api",
            run_kind="cycle",
            requested_component=None,
            idempotency_key=idempotency_key,
            request_summary=request_summary,
            payload=payload,
            max_attempts=2,
        )
    except RunAcceptanceConflict as exc:
        if override:
            mark_override_dispatch_failed(correlation_id, str(exc), config=config)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        if override:
            mark_override_dispatch_failed(correlation_id, str(exc), config=config)
        logger.error(
            "run_acceptance_failed",
            correlation_id=correlation_id,
            run_kind="cycle",
            error=str(exc),
        )
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Run acceptance unavailable",
                "correlation_id": correlation_id,
            },
        ) from exc

    job_id = (
        str(enqueued.job.correlation_id)
        if enqueued and enqueued.job and enqueued.job.correlation_id
        else correlation_id
    )
    return {
        "job_id": job_id,
        "accepted_at": (
            accepted_at.isoformat()
            if isinstance(accepted_at, datetime)
            else str(accepted_at)
        ),
        "status_url": f"/api/system/logs?correlation_id={job_id}",
        "budget": budget,
        "budget_override": override,
    }
