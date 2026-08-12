import os
import uuid
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Body, HTTPException, Request

from budgets import (
    get_budget_status,
    mark_override_dispatch_failed,
    register_manual_override,
)
from config import orchestrator_url
from contracts import RunAcceptanceRequest, RunAcceptedResponse
from contracts.runtime_config import (
    KNOWN_COLLECTORS,
    KNOWN_NEWS_SOURCES,
    KNOWN_PROCESSORS,
)
from logging_config import get_logger

router = APIRouter()
logger = get_logger("api.triggers")

# Executable component registries — single source of truth shared with the
# configuration models (contracts.runtime_config).
_VALID_COLLECTORS = KNOWN_COLLECTORS
_VALID_PROCESSORS = KNOWN_PROCESSORS
_VALID_NEWS_SOURCES = KNOWN_NEWS_SOURCES


def _internal_basic_auth() -> httpx.BasicAuth:
    username = os.environ.get("DASHBOARD_USER", "")
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("internal credentials unavailable")
    return httpx.BasicAuth(username, password)


def _orchestrator_job_payload(
    response: httpx.Response, fallback_id: str, now: str
) -> dict:
    try:
        accepted = RunAcceptedResponse.model_validate(response.json())
    except Exception as exc:
        logger.error("orchestrator_contract_invalid", error=type(exc).__name__)
        raise HTTPException(
            status_code=502, detail="Orchestrator returned invalid response"
        ) from exc
    return accepted.model_dump(mode="json", exclude_none=True)


def _definitively_unspent_error(exc: BaseException) -> bool:
    """True only when the orchestrator provably never accepted the dispatch.

    Connect failures (pre-send) are definitive; read/write/protocol/deadline
    errors can occur after the orchestrator durably accepted the run, so they
    must not revoke a registered override — the worker will consume it (or it
    expires) and the operator can recover via the returned correlation id.
    """
    if isinstance(exc, httpx.ConnectError):
        return True
    return isinstance(exc, httpx.ConnectTimeout)


_DEFINITIVE_REJECTION_STATUSES = frozenset({400, 404, 409, 422})


def _definitively_rejected_status(status_code: int) -> bool:
    """Validated client-error statuses whose endpoint contract guarantees no enqueue.

    A 4xx the orchestrator validates BEFORE durable acceptance (unknown
    component, malformed body, duplicate/conflicting correlation) proves the
    override was never consumed, so revoking it is safe. 5xx responses and
    502 response-contract failures can follow a committed acceptance, so they
    must leave the override active for worker consumption.
    """
    return status_code in _DEFINITIVE_REJECTION_STATUSES


def _manual_override(
    body: dict | RunAcceptanceRequest | None, request: Request
) -> dict | None:
    if hasattr(body, "model_dump"):
        body = body.model_dump()
    if not isinstance(body, dict) or "budget_override" not in body:
        return None
    if not isinstance(body["budget_override"], bool):
        raise HTTPException(
            status_code=422, detail="budget_override must be a boolean"
        )
    if body["budget_override"] is not True:
        return None

    reason = body.get("override_reason")
    if not isinstance(reason, str):
        raise HTTPException(
            status_code=422, detail="override_reason must be a string"
        )
    reason = reason.strip()
    if not 1 <= len(reason) <= 500:
        raise HTTPException(
            status_code=422,
            detail="override_reason must be between 1 and 500 characters",
        )

    client_host = request.client.host if request.client else "unknown"
    return {
        "reason": reason,
        "requested_by": f"authenticated_api_user@{client_host}",
    }


def _enforce_api_budget(override: dict | None) -> dict:
    budget = get_budget_status()
    # Fail closed: a missing, malformed, or unavailable budget status never
    # defaults to "allowed". Only an explicit manual override may proceed when
    # the budget cannot be determined (the orchestrator honors it at claim).
    if not isinstance(budget, dict) or budget.get("available") is not True:
        if override is None:
            logger.warning(
                "paid_trigger_budget_unavailable",
                status=budget.get("status") if isinstance(budget, dict) else None,
            )
            raise HTTPException(
                status_code=503,
                detail="Daily LLM budget status unavailable",
            )
        return budget
    if not budget.get("paid_calls_allowed", False) and override is None:
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
    """Send through the shared app client; capability is checked before send.

    A POST is sent at most once: if the shared client is missing or unusable
    the call fails closed with 503 instead of re-sending on a fallback client.
    """
    client = getattr(request.app.state, "orchestrator_client", None)
    if client is None:
        logger.error("orchestrator_client_unavailable", action="orchestrator_post")
        raise HTTPException(status_code=503, detail="Orchestrator client unavailable")
    try:
        return await client.post(url, **kwargs)
    except (AttributeError, TypeError):
        logger.error("orchestrator_client_unusable", action="orchestrator_post")
        raise HTTPException(status_code=503, detail="Orchestrator client unavailable")


@router.post(
    "/collect/{source_id}", status_code=202, response_model=RunAcceptedResponse
)
async def trigger_collect(source_id: str, request: Request):
    if source_id not in _VALID_COLLECTORS:
        raise HTTPException(status_code=404, detail=f"Unknown collector: {source_id}")

    correlation_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()

    try:
        response = await _post_to_orchestrator(
            request,
            f"{orchestrator_url()}/run_collector/{source_id}",
            json={"correlation_id": correlation_id},
            timeout=10.0,
            auth=_internal_basic_auth(),
        )
    except httpx.TransportError as exc:
        logger.error("orchestrator_connect_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="Orchestrator unavailable")

    if response.status_code != 202:
        logger.error("orchestrator_unexpected_response", status=response.status_code)
        raise HTTPException(
            status_code=502, detail="Orchestrator returned unexpected response"
        )

    payload = _orchestrator_job_payload(response, correlation_id, now)
    payload["status_url"] = f"/api/system/logs?component={source_id}"
    return payload


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

    correlation_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
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
            f"{orchestrator_url()}/run_processor/{processor_id}",
            json={"correlation_id": correlation_id},
            timeout=10.0,
            auth=_internal_basic_auth(),
        )
    except httpx.TransportError as exc:
        if override and _definitively_unspent_error(exc):
            mark_override_dispatch_failed(correlation_id, str(exc))
        logger.error("orchestrator_connect_failed", error=str(exc))
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Orchestrator unavailable",
                "correlation_id": correlation_id,
            },
        )

    if response.status_code != 202:
        if override and _definitively_rejected_status(response.status_code):
            mark_override_dispatch_failed(
                correlation_id,
                f"orchestrator returned HTTP {response.status_code}",
            )
        logger.error("orchestrator_unexpected_response", status=response.status_code)
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Orchestrator returned unexpected response",
                "correlation_id": correlation_id,
            },
        )

    payload = _orchestrator_job_payload(response, correlation_id, now)
    payload["status_url"] = f"/api/system/logs?component={processor_id}"
    payload["budget"] = budget
    payload["budget_override"] = override
    return payload


@router.post(
    "/triggers/news/{source_id}",
    status_code=202,
    response_model=RunAcceptedResponse,
)
async def trigger_news(source_id: str, request: Request):
    if source_id not in _VALID_NEWS_SOURCES:
        raise HTTPException(status_code=404, detail=f"Unknown news source: {source_id}")

    correlation_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    try:
        response = await _post_to_orchestrator(
            request,
            f"{orchestrator_url()}/run_news/{source_id}",
            json={"correlation_id": correlation_id},
            timeout=10.0,
            auth=_internal_basic_auth(),
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
@router.post(
    "/triggers/cycle",
    status_code=202,
    response_model=RunAcceptedResponse,
)
async def trigger_cycle(
    request: Request,
    body: RunAcceptanceRequest | None = Body(default=None),
):
    correlation_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()

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

    override_request = _manual_override(request_body, request)
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
        raise HTTPException(
            status_code=503, detail="Internal authentication unavailable"
        ) from exc

    try:
        response = await _post_to_orchestrator(
            request,
            f"{orchestrator_url()}/run_cycle",
            json={
                "correlation_id": correlation_id,
                "mode": mode,
                "budget_confirmed": budget_confirmed,
            },
            timeout=10.0,
            auth=auth,
        )
    except httpx.TransportError as exc:
        if override and _definitively_unspent_error(exc):
            mark_override_dispatch_failed(correlation_id, str(exc))
        logger.error("orchestrator_connect_failed", error=str(exc))
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Orchestrator unavailable",
                "correlation_id": correlation_id,
            },
        )

    if response.status_code in {409, 422, 503}:
        if override and _definitively_rejected_status(response.status_code):
            mark_override_dispatch_failed(
                correlation_id, f"orchestrator returned HTTP {response.status_code}"
            )
        if response.status_code == 503:
            # Ambiguous: the orchestrator may have committed acceptance before
            # failing; keep the override live and give the operator recovery.
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "Cycle request rejected by orchestrator",
                    "correlation_id": correlation_id,
                },
            )
        fallback = (
            "Cycle already running"
            if response.status_code == 409
            else "Cycle request rejected"
        )
        raise HTTPException(
            status_code=response.status_code,
            detail=response.json().get("detail", fallback),
        )

    if response.status_code != 202:
        if override and _definitively_rejected_status(response.status_code):
            mark_override_dispatch_failed(
                correlation_id, f"orchestrator returned HTTP {response.status_code}"
            )
        logger.error("orchestrator_unexpected_response", status=response.status_code)
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Orchestrator returned unexpected response",
                "correlation_id": correlation_id,
            },
        )

    payload = _orchestrator_job_payload(response, correlation_id, now)
    payload["status_url"] = f"/api/system/logs?correlation_id={payload['job_id']}"
    payload["budget"] = budget
    payload["budget_override"] = override
    return payload
