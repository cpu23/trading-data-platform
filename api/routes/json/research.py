"""Bounded Phase 9 research workspace routes (thesis desk).

Provides read models and orchestration dispatch for the bounded thesis desk:
status, groups, opportunities, detail, and manual run triggers.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse

import config as app_config
import orchestrator_imports  # noqa: F401
from budgets import enforce_api_budget, get_budget_status
from db import get_session
from logging_config import get_logger
from orchestrator_client import orchestrator_post

try:
    import thesis_fusion as _thesis_fusion

    THESIS_GROUP_STATUSES = _thesis_fusion.GROUP_STATUSES
except ImportError:  # pragma: no cover - deployment wiring reconciles
    _thesis_fusion = None
    THESIS_GROUP_STATUSES = ()

logger = get_logger("api.research")


def load_config() -> Mapping[str, Any]:
    return app_config.load_config()


def _enforce_research_budget(
    override: dict[str, str] | None = None,
) -> dict[str, Any]:
    return enforce_api_budget(get_budget_status(), override)


def _thesis_desk_helpers() -> Any:
    """Return the thesis-desk repository module or fail soft with 503."""
    if _thesis_fusion is None:
        raise RuntimeError("thesis desk helpers unavailable")
    return _thesis_fusion


router = APIRouter(prefix="/research", tags=["research"])

_LIST_LIMIT = 100


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return str(value)


def _uuid(value: Any, field: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"invalid {field}") from None


def _run_body(value: Any) -> bool:
    if value is None:
        return False
    if not isinstance(value, Mapping) or set(value) - {"force"}:
        raise ValueError("body may contain only force")
    force = value.get("force", False)
    if not isinstance(force, bool):
        raise ValueError("force must be boolean")
    return force


async def _research_orchestrator_post(
    request: Request, path: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    response = await orchestrator_post(
        request,
        path,
        json=payload or {},
        timeout=10.0,
    )
    if response.status_code != 202:
        status = (
            response.status_code
            if response.status_code in {404, 409, 422, 429, 503}
            else 502
        )
        raise HTTPException(
            status_code=status,
            detail="Research job could not be queued",
        )
    try:
        body = response.json()
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail="Orchestrator returned invalid response"
        ) from exc
    if not isinstance(body, dict) or not body.get("job_id"):
        raise HTTPException(
            status_code=502, detail="Orchestrator returned invalid response"
        )
    return _jsonable(body)


@router.get("/theses/opportunities")
def list_desk_opportunities(
    limit: int = Query(default=50, ge=1, le=_LIST_LIMIT),
    minimum_score: float = Query(default=0.0, ge=0.0, le=1.0),
    group_id: str | None = Query(default=None),
    include_ineligible: bool = Query(default=False),
):
    try:
        helpers = _thesis_desk_helpers()
        parsed_group = _uuid(group_id, "group_id") if group_id is not None else None
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    config = load_config()
    try:
        with get_session(config) as session:
            rows = helpers.list_ranked_opportunities(
                session,
                limit=limit,
                minimum_score=minimum_score,
                group_id=parsed_group,
                include_ineligible=include_ineligible,
            )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {
        "opportunities": _jsonable(rows),
        "limit": limit,
        "minimum_score": minimum_score,
        "include_ineligible": include_ineligible,
    }


@router.get("/theses/groups")
def list_desk_groups(
    limit: int = Query(default=50, ge=1, le=_LIST_LIMIT),
    status: str | None = Query(default=None),
):
    try:
        helpers = _thesis_desk_helpers()
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    if status is not None and status not in THESIS_GROUP_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported group status:{str(status)[:32]}",
        )
    config = load_config()
    try:
        with get_session(config) as session:
            rows = helpers.list_thesis_groups(session, limit=limit, status=status)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"groups": _jsonable(rows), "limit": limit}


@router.get("/theses/status")
def desk_status(limit: int = Query(default=20, ge=1, le=100)):
    try:
        helpers = _thesis_desk_helpers()
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    config = load_config()
    try:
        with get_session(config) as session:
            status = helpers.thesis_desk_status(session, limit=limit)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return _jsonable(status)


@router.get("/theses/{thesis_id}")
def desk_thesis_detail(thesis_id: str):
    try:
        helpers = _thesis_desk_helpers()
        parsed = _uuid(thesis_id, "thesis_id")
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    config = load_config()
    try:
        with get_session(config) as session:
            detail = helpers.load_thesis_detail(session, parsed)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    if detail is None:
        raise HTTPException(status_code=404, detail="Thesis not found")
    return {"thesis": _jsonable(detail)}


@router.post("/theses/run", status_code=202)
async def run_thesis_desk(request: Request, body: dict | None = Body(default=None)):
    try:
        force = _run_body(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _enforce_research_budget(None)
    return await _research_orchestrator_post(
        request, "/research/theses/run", {"force": force}
    )
