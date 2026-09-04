"""Bounded Phase 9 research workspace routes (thesis desk).

Provides read models and orchestration dispatch for the bounded thesis desk:
status, groups, opportunities, detail, and manual run triggers.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from api_budgets import enforce_api_budget, get_budget_status
from api_db import get_session
from api_logging import get_logger
from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse

import config as app_config

try:
    from thesis_autonomy import enqueue_thesis_autonomy_job
except ImportError:  # pragma: no cover - deployment wiring reconciles
    enqueue_thesis_autonomy_job = None

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


def _get_reviewer_id(request: Request) -> str:
    session = getattr(request, "session", None)
    if session is None and hasattr(request, "scope"):
        session = request.scope.get("session")
    if isinstance(session, dict) and session.get("authenticated"):
        username = session.get("username")
        if username and str(username).strip():
            return str(username).strip()
        return "admin"
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("basic "):
        try:
            import base64

            decoded = base64.b64decode(auth_header[6:].strip()).decode("utf-8")
            user, _ = decoded.split(":", 1)
            if user and user.strip():
                return user.strip()
        except Exception:
            pass
    return "admin"


def _parse_review_note(body: Any) -> str | None:
    if body is None:
        return None
    if not isinstance(body, Mapping):
        raise ValueError("body must be a JSON object")
    note = body.get("review_note")
    if note is None:
        return None
    if not isinstance(note, str):
        raise ValueError("review_note must be a string")
    note = note.strip()
    if len(note) > 4000:
        raise ValueError("review_note exceeds maximum length of 4000 characters")
    return note or None


def _parse_revision_body(body: Any) -> tuple[str, str | None]:
    if not isinstance(body, Mapping):
        raise ValueError("body must be a JSON object with revision_instructions")
    instructions = body.get("revision_instructions")
    if (
        not instructions
        or not isinstance(instructions, str)
        or not instructions.strip()
    ):
        raise ValueError("revision_instructions is required")
    instructions = instructions.strip()
    if len(instructions) > 4000:
        raise ValueError(
            "revision_instructions exceeds maximum length of 4000 characters"
        )
    note = body.get("review_note")
    if note is not None:
        if not isinstance(note, str):
            raise ValueError("review_note must be a string")
        note = note.strip()
        if len(note) > 4000:
            raise ValueError("review_note exceeds maximum length of 4000 characters")
        note = note or None
    return instructions, note


def _run_body(value: Any) -> bool:
    if value is None:
        return False
    if not isinstance(value, Mapping) or set(value) - {"force"}:
        raise ValueError("body may contain only force")
    force = value.get("force", False)
    if not isinstance(force, bool):
        raise ValueError("force must be boolean")
    return force


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


@router.post("/theses/run", status_code=202)
def run_thesis_desk(request: Request, body: dict | None = Body(default=None)):
    try:
        force = _run_body(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _enforce_research_budget(None)
    if enqueue_thesis_autonomy_job is None:
        raise HTTPException(status_code=503, detail="Thesis run could not be queued")
    config = load_config()
    try:
        result = enqueue_thesis_autonomy_job(
            dict(config), triggered_by="api", force=force
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("thesis_autonomy_enqueue_failed", error_type=type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="Thesis run could not be queued"
        ) from exc
    return _jsonable(result)


@router.get("/theses/proposals")
@router.get("/proposals")
def list_desk_proposals(
    status: str | None = Query(default=None),
    theme_id: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    try:
        helpers = _thesis_desk_helpers()
        parsed_theme = _uuid(theme_id, "theme_id") if theme_id else None
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    config = load_config()
    try:
        with get_session(config) as session:
            proposals = helpers.list_thesis_proposals(
                session,
                status=status,
                theme_id=parsed_theme,
                symbol=symbol,
                limit=limit,
                offset=offset,
            )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {
        "proposals": _jsonable(proposals),
        "limit": limit,
        "offset": offset,
    }


@router.get("/theses/proposals/{proposal_id}")
@router.get("/proposals/{proposal_id}")
def desk_proposal_detail(proposal_id: str):
    try:
        helpers = _thesis_desk_helpers()
        parsed = _uuid(proposal_id, "proposal_id")
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    config = load_config()
    try:
        with get_session(config) as session:
            proposal = helpers.get_thesis_proposal(session, parsed)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return {"proposal": _jsonable(proposal)}


@router.post("/theses/proposals/{proposal_id}/approve")
@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(
    proposal_id: str,
    request: Request,
    body: dict | None = Body(default=None),
):
    try:
        helpers = _thesis_desk_helpers()
        parsed_id = _uuid(proposal_id, "proposal_id")
        review_note = _parse_review_note(body)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    reviewer_id = _get_reviewer_id(request)
    config = load_config()
    try:
        with get_session(config) as session:
            result = helpers.approve_thesis_proposal(
                session,
                parsed_id,
                reviewer_id=reviewer_id,
                review_note=review_note,
            )
    except ValueError as error:
        msg = str(error)
        if "not found" in msg.lower():
            raise HTTPException(status_code=404, detail=msg) from error
        if (
            "status" in msg.lower()
            or "transition" in msg.lower()
            or "only pending_review" in msg.lower()
        ):
            raise HTTPException(status_code=409, detail=msg) from error
        raise HTTPException(status_code=422, detail=msg) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "approved", "proposal": _jsonable(result)}


@router.post("/theses/proposals/{proposal_id}/reject")
@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(
    proposal_id: str,
    request: Request,
    body: dict | None = Body(default=None),
):
    try:
        helpers = _thesis_desk_helpers()
        parsed_id = _uuid(proposal_id, "proposal_id")
        review_note = _parse_review_note(body)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    reviewer_id = _get_reviewer_id(request)
    config = load_config()
    try:
        with get_session(config) as session:
            result = helpers.reject_thesis_proposal(
                session,
                parsed_id,
                reviewer_id=reviewer_id,
                review_note=review_note,
            )
    except ValueError as error:
        msg = str(error)
        if "not found" in msg.lower():
            raise HTTPException(status_code=404, detail=msg) from error
        if (
            "status" in msg.lower()
            or "transition" in msg.lower()
            or "only pending_review" in msg.lower()
        ):
            raise HTTPException(status_code=409, detail=msg) from error
        raise HTTPException(status_code=422, detail=msg) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "rejected", "proposal": _jsonable(result)}


@router.post("/theses/proposals/{proposal_id}/revision", status_code=202)
@router.post("/proposals/{proposal_id}/revision", status_code=202)
def request_proposal_revision(
    proposal_id: str,
    request: Request,
    body: dict = Body(...),
):
    try:
        helpers = _thesis_desk_helpers()
        parsed_id = _uuid(proposal_id, "proposal_id")
        revision_instructions, review_note = _parse_revision_body(body)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    reviewer_id = _get_reviewer_id(request)
    config = load_config()
    try:
        from jobs import enqueue_job
    except ImportError:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    try:
        with get_session(config) as session:
            revision_res = helpers.request_thesis_proposal_revision(
                session,
                parsed_id,
                reviewer_id=reviewer_id,
                revision_instructions=revision_instructions,
                review_note=review_note,
            )
            proposal = revision_res.get("proposal") or {}
            enqueue_payload = revision_res.get("enqueue_payload") or {}
            correlation_id = str(uuid4())
            input_fingerprint = f"thesis-autonomy:revision:{parsed_id}:{correlation_id}"
            dedupe_key = f"thesis-autonomy:revision:{parsed_id}:{correlation_id}"
            enqueued = enqueue_job(
                session,
                job_type="thesis_autonomy_run",
                dedupe_key=dedupe_key,
                input_fingerprint=input_fingerprint,
                payload=enqueue_payload,
                correlation_id=correlation_id,
                priority=90,
                max_attempts=3,
            )
    except ValueError as error:
        msg = str(error)
        if "not found" in msg.lower():
            raise HTTPException(status_code=404, detail=msg) from error
        if (
            "status" in msg.lower()
            or "transition" in msg.lower()
            or "only pending_review" in msg.lower()
        ):
            raise HTTPException(status_code=409, detail=msg) from error
        raise HTTPException(status_code=422, detail=msg) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})

    job = enqueued.job if enqueued is not None else None
    return JSONResponse(
        status_code=202,
        content={
            "status": "revision_requested",
            "proposal": _jsonable(proposal),
            "job_id": str(job.id) if job is not None else None,
            "correlation_id": correlation_id,
        },
    )


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
