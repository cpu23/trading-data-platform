"""Bounded Phase 9 research workspace routes.

All POST bodies are validated before a session is opened; helpers own the
queries and the route owns the transaction (``get_session`` commits once).
Responses serialise datetimes as ISO strings and UUIDs as strings; unknown
resources are 404, invalid input 422, and infrastructure failures fail soft
as 503 without leaking details.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from config import load_config, orchestrator_url
from db import get_session
from routes.json.triggers import (
    _enforce_api_budget,
    _internal_basic_auth,
    _post_to_orchestrator,
)

_ORCHESTRATOR_DIR = Path(__file__).resolve().parents[3] / "orchestrator"
_orchestrator_path = str(_ORCHESTRATOR_DIR)
_orchestrator_path_added = (
    _ORCHESTRATOR_DIR.is_dir() and _orchestrator_path not in sys.path
)
if _orchestrator_path_added:
    sys.path.insert(0, _orchestrator_path)

try:
    from research_intelligence import queries as _research_queries
    from research_intelligence.benchmarks import list_benchmarks as _list_benchmarks

    _live_case_cohorts = _research_queries.live_case_cohorts
except ImportError:  # pragma: no cover - deployment wiring
    _research_queries = None
    _list_benchmarks = None
    _live_case_cohorts = None

try:
    from research_intelligence.scorecards import (
        annotate_benchmark_scorecard as _annotate_benchmark_scorecard,
    )
except ImportError:  # pragma: no cover - deployment wiring
    _annotate_benchmark_scorecard = None


try:  # orchestrator directory on PYTHONPATH (deployment wiring)
    import research as _research

    ENTITY_TYPES = _research.ENTITY_TYPES
    EVIDENCE_TYPES = _research.EVIDENCE_TYPES
    RELATIONSHIPS = _research.RELATIONSHIPS
    CATALYST_STATES = _research.CATALYST_STATES
    RISK_KINDS = _research.RISK_KINDS
    RISK_SEVERITIES = _research.RISK_SEVERITIES
    HOLDING_SOURCES = _research.HOLDING_SOURCES
except ImportError:  # pragma: no cover - api-only environment
    try:
        from orchestrator import research as _research

        ENTITY_TYPES = _research.ENTITY_TYPES
        EVIDENCE_TYPES = _research.EVIDENCE_TYPES
        RELATIONSHIPS = _research.RELATIONSHIPS
        CATALYST_STATES = _research.CATALYST_STATES
        RISK_KINDS = _research.RISK_KINDS
        RISK_SEVERITIES = _research.RISK_SEVERITIES
        HOLDING_SOURCES = _research.HOLDING_SOURCES
    except ImportError:  # pragma: no cover - deployment wiring reconciles
        _research = None
        ENTITY_TYPES = EVIDENCE_TYPES = RELATIONSHIPS = ()
        CATALYST_STATES = RISK_KINDS = RISK_SEVERITIES = HOLDING_SOURCES = ()
finally:
    if _orchestrator_path_added:
        sys.path.remove(_orchestrator_path)


def _helpers() -> Any:
    """Return the orchestrator research module or fail soft with 503."""
    if _research is None:
        raise RuntimeError("research helpers unavailable")
    return _research


def _annotation_helper() -> Any:
    """Resolve the optional write helper after API imports have settled."""
    global _annotate_benchmark_scorecard
    if _annotate_benchmark_scorecard is None:
        try:
            from research_intelligence.scorecards import (
                annotate_benchmark_scorecard,
            )
        except ImportError as exc:  # pragma: no cover - api-only environment
            raise RuntimeError("research annotation helper unavailable") from exc
        _annotate_benchmark_scorecard = annotate_benchmark_scorecard
    return _annotate_benchmark_scorecard


router = APIRouter(prefix="/research", tags=["research"])

_LIST_LIMIT = 100
_TEXT_MAX = 5000
_ITEM_MAX = 50


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


def _mapping(value: Any, field: str = "body") -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return dict(value)


def _required_text(value: Any, field: str, maximum: int = _TEXT_MAX) -> str:
    if value is None:
        raise ValueError(f"{field} is required")
    text_value = str(value).strip()
    if not text_value:
        raise ValueError(f"{field} is required")
    return text_value[:maximum]


def _bounded_text(value: Any, maximum: int) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value[:maximum] if text_value else None


def _string_list(value: Any, field: str, maximum: int = 200) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be a list")
    if len(value) > _ITEM_MAX:
        raise ValueError(f"{field} has too many items")
    result: list[str] = []
    for item in value:
        entry = _bounded_text(item, maximum)
        if entry:
            result.append(entry)
    return result


def _object_list(value: Any, field: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be a list")
    if len(value) > _ITEM_MAX:
        raise ValueError(f"{field} has too many items")
    return list(value)


def _float_bounded(value: Any, field: str, low: float, high: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"invalid {field}")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"invalid {field}") from None
    if not math.isfinite(result) or not (low <= result <= high):
        raise ValueError(f"invalid {field}")
    return result


def _timestamp(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"invalid {field}") from None
    raise ValueError(f"invalid {field}")


def _uuid(value: Any, field: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"invalid {field}") from None


def _value_error_http(error: ValueError) -> HTTPException:
    message = str(error)
    if message.startswith("unknown "):
        return HTTPException(status_code=404, detail=message)
    if message.startswith("duplicate "):
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=422, detail=message)


def _validate_theme_payload(body: Any) -> dict[str, Any]:
    payload = _mapping(body)
    theme = {
        "name": _required_text(payload.get("name"), "name", 200),
        "definition": _required_text(payload.get("definition"), "definition"),
    }
    if payload.get("horizon") is not None:
        theme["horizon"] = _bounded_text(payload.get("horizon"), 50) or "multi_year"
    if payload.get("macro_drivers") is not None:
        theme["macro_drivers"] = _string_list(payload.get("macro_drivers"))
    if payload.get("key_indicators") is not None:
        theme["key_indicators"] = _string_list(payload.get("key_indicators"))
    if payload.get("invalidation_conditions") is not None:
        items = _object_list(
            payload.get("invalidation_conditions"), "invalidation_conditions"
        )
        theme["invalidation_conditions"] = items
    if payload.get("confidence") is not None:
        theme["confidence"] = _float_bounded(
            payload.get("confidence"), "confidence", 0.0, 1.0
        )
    entities: list[dict[str, Any]] = []
    for item in _object_list(payload.get("entities"), "entities"):
        if not isinstance(item, Mapping):
            raise ValueError("invalid entity")
        entity_type = str(item.get("entity_type") or "").strip().lower()
        if entity_type not in ENTITY_TYPES:
            raise ValueError(f"unsupported entity_type:{entity_type[:32]}")
        entities.append(
            {
                "entity_type": entity_type,
                "entity_id": _required_text(item.get("entity_id"), "entity_id", 200),
                "display_name": _bounded_text(item.get("display_name"), 200),
            }
        )
    return {"theme": theme, "entities": entities}


def _validate_thesis_payload(body: Any) -> dict[str, Any]:
    payload = _mapping(body)
    thesis: dict[str, Any] = {"claim": _required_text(payload.get("claim"), "claim")}
    for key, maximum in (
        ("company", 200),
        ("symbol", 20),
        ("variant_perception", 2000),
        ("horizon", 50),
        ("rationale", _TEXT_MAX),
    ):
        if payload.get(key) is not None:
            thesis[key] = _bounded_text(payload.get(key), maximum)
    if payload.get("confidence") is not None:
        thesis["confidence"] = _float_bounded(
            payload.get("confidence"), "confidence", 0.0, 1.0
        )
    if payload.get("invalidation_conditions") is not None:
        thesis["invalidation_conditions"] = _object_list(
            payload.get("invalidation_conditions"), "invalidation_conditions"
        )
    return thesis


def _validate_revision_payload(body: Any) -> dict[str, Any]:
    payload = _mapping(body)
    revision: dict[str, Any] = {
        "claim": _required_text(payload.get("claim"), "claim"),
        "rationale": _required_text(payload.get("rationale"), "rationale"),
    }
    if payload.get("variant_perception") is not None:
        revision["variant_perception"] = _bounded_text(
            payload.get("variant_perception"), 2000
        )
    if payload.get("confidence") is not None:
        revision["confidence"] = _float_bounded(
            payload.get("confidence"), "confidence", 0.0, 1.0
        )
    if payload.get("changed_by") is not None:
        revision["changed_by"] = _bounded_text(payload.get("changed_by"), 200)
    return revision


def _validate_evidence_payload(body: Any) -> list[dict[str, Any]]:
    payload = _mapping(body)
    evidence: list[dict[str, Any]] = []
    for item in _object_list(payload.get("evidence"), "evidence"):
        if not isinstance(item, Mapping):
            raise ValueError("invalid evidence row")
        evidence_type = str(item.get("evidence_type") or "").strip().lower()
        relationship = str(item.get("relationship") or "").strip().lower()
        if evidence_type not in EVIDENCE_TYPES:
            raise ValueError(f"unsupported evidence_type:{evidence_type[:32]}")
        if relationship not in RELATIONSHIPS:
            raise ValueError(f"unsupported relationship:{relationship[:32]}")
        evidence_id = _required_text(item.get("evidence_id"), "evidence_id", 200)
        if evidence_type == "atom":
            _uuid(evidence_id, "evidence_id")
        evidence.append(
            {
                "evidence_type": evidence_type,
                "evidence_id": evidence_id,
                "relationship": relationship,
                "excerpt": _bounded_text(item.get("excerpt"), 500),
            }
        )
    if not evidence:
        raise ValueError("evidence is required")
    return evidence


def _validate_catalyst_payload(body: Any) -> dict[str, Any]:
    payload = _mapping(body)
    state = str(payload.get("state") or "pending").strip().lower()
    if state not in CATALYST_STATES:
        raise ValueError(f"unsupported catalyst state:{state[:32]}")
    catalyst: dict[str, Any] = {
        "description": _required_text(payload.get("description"), "description", 2000),
        "state": state,
    }
    if payload.get("expected_at") is not None:
        catalyst["expected_at"] = _timestamp(payload.get("expected_at"), "expected_at")
    return catalyst


def _validate_risk_payload(body: Any) -> dict[str, Any]:
    payload = _mapping(body)
    kind = str(payload.get("kind") or "counter_thesis").strip().lower()
    severity = str(payload.get("severity") or "moderate").strip().lower()
    if kind not in RISK_KINDS:
        raise ValueError(f"unsupported risk kind:{kind[:32]}")
    if severity not in RISK_SEVERITIES:
        raise ValueError(f"unsupported risk severity:{severity[:32]}")
    return {
        "description": _required_text(payload.get("description"), "description", 2000),
        "kind": kind,
        "severity": severity,
    }


def _validate_holdings_payload(body: Any) -> list[dict[str, Any]]:
    payload = _mapping(body)
    holdings: list[dict[str, Any]] = []
    for item in _object_list(payload.get("holdings"), "holdings"):
        if not isinstance(item, Mapping):
            raise ValueError("invalid holding")
        source = str(item.get("source") or "manual").strip().lower()
        if source not in HOLDING_SOURCES:
            raise ValueError(f"unsupported source:{source[:32]}")
        holding: dict[str, Any] = {
            "symbol": _required_text(item.get("symbol"), "symbol", 20),
            "source": source,
        }
        for key, maximum in (
            ("company", 200),
            ("sector", 100),
            ("country", 100),
            ("currency", 10),
            ("rate_sensitivity", 100),
            ("commodity_sensitivity", 100),
        ):
            if item.get(key) is not None:
                holding[key] = _bounded_text(item.get(key), maximum)
        if item.get("weight") is not None:
            holding["weight"] = _float_bounded(item.get("weight"), "weight", 0.0, 1.0)
        if item.get("theme_tags") is not None:
            holding["theme_tags"] = _string_list(item.get("theme_tags"))
        holdings.append(holding)
    if not holdings:
        raise ValueError("holdings is required")
    return holdings


@router.get("/themes")
def get_themes(limit: int = Query(default=50, ge=1, le=_LIST_LIMIT)):
    config = load_config()
    try:
        helpers = _helpers()
        with get_session(config) as session:
            rows = helpers.list_themes(session, limit=limit)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"themes": [_jsonable(row) for row in rows], "limit": limit}


@router.get("/themes/{theme_id}")
def get_theme(theme_id: str):
    try:
        helpers = _helpers()
        parsed = _uuid(theme_id, "theme_id")
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    config = load_config()
    try:
        with get_session(config) as session:
            theme = helpers.get_theme(session, parsed)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    if theme is None:
        raise HTTPException(status_code=404, detail="Unknown theme")
    return {"theme": _jsonable(theme)}


@router.get("/companies/{company}")
def get_company(company: str):
    try:
        helpers = _helpers()
        company_name = _required_text(company, "company", 200)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    config = load_config()
    try:
        with get_session(config) as session:
            dossier = helpers.get_dossier(session, company_name)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    if dossier is None:
        raise HTTPException(status_code=404, detail="Unknown company")
    return {"dossier": _jsonable(dossier)}


@router.get("/portfolio")
def get_portfolio():
    config = load_config()
    try:
        helpers = _helpers()
        with get_session(config) as session:
            context = helpers.portfolio_context(session)
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"portfolio": _jsonable(context)}


@router.post("/themes", status_code=201)
def create_theme(body: dict = Body(...)):
    try:
        helpers = _helpers()
        payload = _validate_theme_payload(body)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    config = load_config()
    try:
        with get_session(config) as session:
            theme_id = helpers.create_theme(session, **payload["theme"])
            if payload["entities"]:
                helpers.attach_theme_entities(session, theme_id, payload["entities"])
    except ValueError as error:
        raise _value_error_http(error) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"theme_id": str(theme_id)}


@router.post("/themes/{theme_id}/theses", status_code=201)
def create_thesis(theme_id: str, body: dict = Body(...)):
    try:
        helpers = _helpers()
        parsed = _uuid(theme_id, "theme_id")
        payload = _validate_thesis_payload(body)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    config = load_config()
    try:
        with get_session(config) as session:
            thesis_id = helpers.create_thesis(session, theme_id=parsed, **payload)
    except ValueError as error:
        raise _value_error_http(error) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"thesis_id": str(thesis_id), "version": 1}


@router.post("/theses/{thesis_id}/revisions", status_code=201)
def revise_thesis(thesis_id: str, body: dict = Body(...)):
    try:
        helpers = _helpers()
        parsed = _uuid(thesis_id, "thesis_id")
        payload = _validate_revision_payload(body)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    config = load_config()
    try:
        with get_session(config) as session:
            version = helpers.revise_thesis(session, parsed, **payload)
    except ValueError as error:
        raise _value_error_http(error) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"thesis_id": str(parsed), "version": version}


@router.post("/theses/{thesis_id}/evidence", status_code=201)
def add_evidence(thesis_id: str, body: dict = Body(...)):
    try:
        helpers = _helpers()
        parsed = _uuid(thesis_id, "thesis_id")
        evidence = _validate_evidence_payload(body)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    config = load_config()
    try:
        with get_session(config) as session:
            count = helpers.add_thesis_evidence(session, parsed, evidence=evidence)
    except ValueError as error:
        raise _value_error_http(error) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"thesis_id": str(parsed), "evidence": count}


@router.post("/theses/{thesis_id}/catalysts", status_code=201)
def add_catalyst(thesis_id: str, body: dict = Body(...)):
    try:
        helpers = _helpers()
        parsed = _uuid(thesis_id, "thesis_id")
        payload = _validate_catalyst_payload(body)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    config = load_config()
    try:
        with get_session(config) as session:
            catalyst_id = helpers.add_catalyst(session, parsed, **payload)
    except ValueError as error:
        raise _value_error_http(error) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"thesis_id": str(parsed), "catalyst_id": str(catalyst_id)}


@router.post("/theses/{thesis_id}/risks", status_code=201)
def add_risk(thesis_id: str, body: dict = Body(...)):
    try:
        helpers = _helpers()
        parsed = _uuid(thesis_id, "thesis_id")
        payload = _validate_risk_payload(body)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    config = load_config()
    try:
        with get_session(config) as session:
            risk_id = helpers.add_risk(session, parsed, **payload)
    except ValueError as error:
        raise _value_error_http(error) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"thesis_id": str(parsed), "risk_id": str(risk_id)}


@router.post("/portfolio/holdings", status_code=201)
def upsert_holdings(body: dict = Body(...)):
    try:
        helpers = _helpers()
        holdings = _validate_holdings_payload(body)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    config = load_config()
    try:
        with get_session(config) as session:
            count = helpers.upsert_holdings(session, holdings=holdings)
    except ValueError as error:
        raise _value_error_http(error) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"holdings": count}


def _intelligence_helpers():
    if _research_queries is None:
        raise RuntimeError("research intelligence helpers unavailable")
    return _research_queries


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
    try:
        response = await _post_to_orchestrator(
            request,
            f"{orchestrator_url()}{path}",
            json=payload or {},
            timeout=10.0,
            auth=_internal_basic_auth(),
        )
    except (httpx.TransportError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="Orchestrator unavailable") from exc
    if response.status_code != 202:
        status = response.status_code if response.status_code in {404, 409, 422, 429, 503} else 502
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


@router.get("/cases")
def list_intelligence_cases(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1000),
    lifecycle_state: str | None = Query(default=None),
    changed_only: bool = Query(default=False),
):
    try:
        helpers = _intelligence_helpers()
        with get_session(load_config()) as session:
            rows = helpers.list_cases(
                session,
                limit=limit,
                offset=offset,
                lifecycle_state=lifecycle_state,
                changed_only=changed_only,
            )
        return {"cases": _jsonable(rows), "limit": limit, "offset": offset}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})


@router.get("/cases/{case_id}")
def intelligence_case_detail(
    case_id: str,
    detail_limit: int = Query(default=100, ge=1, le=200),
):
    try:
        helpers = _intelligence_helpers()
        parsed = _uuid(case_id, "case_id")
        with get_session(load_config()) as session:
            result = helpers.get_case(session, parsed, detail_limit=detail_limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    if result is None:
        raise HTTPException(status_code=404, detail="Research case not found")
    return _jsonable(result)


@router.get("/cases/{case_id}/history")
def intelligence_case_history(
    case_id: str,
    limit: int = Query(default=50, ge=1, le=100),
):
    try:
        helpers = _intelligence_helpers()
        parsed = _uuid(case_id, "case_id")
        with get_session(load_config()) as session:
            rows = helpers.case_history(session, parsed, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"case_id": parsed, "history": _jsonable(rows), "limit": limit}


@router.get("/drivers")
def intelligence_market_drivers(
    changed_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=100),
):
    try:
        helpers = _intelligence_helpers()
        with get_session(load_config()) as session:
            rows = helpers.current_market_drivers(
                session, changed_only=changed_only, limit=limit
            )
        return {"drivers": _jsonable(rows), "limit": limit}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})


@router.get("/benchmarks")
def intelligence_benchmarks():
    if _list_benchmarks is None:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    try:
        episodes = _list_benchmarks()
        return {
            "benchmarks": [
                {
                    "id": item.episode_id,
                    "version": item.version,
                    "synthetic": item.synthetic,
                    "episode_kind": item.episode_kind,
                    "description": item.description,
                    "replay_dates": [
                        value.isoformat() for value in item.replay_dates
                    ],
                    "evidence_count": len(item.evidence),
                }
                for item in episodes
            ]
        }
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})


@router.get("/replays")
def intelligence_replays(
    benchmark_id: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1000),
):
    try:
        helpers = _intelligence_helpers()
        with get_session(load_config()) as session:
            rows = helpers.list_replay_runs(
                session,
                benchmark_id=benchmark_id,
                limit=limit,
                offset=offset,
            )
        return {"replays": _jsonable(rows), "limit": limit, "offset": offset}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})


@router.get("/replays/{replay_run_id}")
def intelligence_replay_detail(
    replay_run_id: str,
    detail_limit: int = Query(default=100, ge=1, le=200),
):
    try:
        helpers = _intelligence_helpers()
        parsed = _uuid(replay_run_id, "replay_run_id")
        with get_session(load_config()) as session:
            result = helpers.get_replay_run(
                session, parsed, detail_limit=detail_limit
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    if result is None:
        raise HTTPException(status_code=404, detail="Research replay not found")
    return _jsonable(result)


@router.post("/replays/{replay_run_id}/annotations")
def annotate_intelligence_replay(
    replay_run_id: str,
    body: dict = Body(...),
):
    try:
        annotator = _annotation_helper()
    except RuntimeError:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    unknown = set(body) - {
        "overall_label",
        "dimension_labels",
        "notes",
        "expected_version",
    }
    if unknown:
        raise HTTPException(
            status_code=422, detail="body contains unknown annotation fields"
        )
    annotations = {
        key: body[key]
        for key in ("overall_label", "dimension_labels", "notes")
        if key in body
    }
    expected_version = body.get("expected_version")
    try:
        with get_session(load_config()) as session:
            return _jsonable(
                annotator(
                    session,
                    replay_run_id,
                    annotations,
                    annotated_by="authenticated_operator",
                    expected_version=expected_version,
                )
            )
    except ValueError as exc:
        detail = str(exc)
        if detail == "benchmark scorecard not found":
            raise HTTPException(status_code=404, detail=detail) from exc
        if detail == "human annotation version conflict":
            raise HTTPException(status_code=409, detail=detail) from exc
        raise HTTPException(status_code=422, detail=detail) from exc
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})




@router.get("/metrics")
def intelligence_quality_metrics(
    metric_scope: str | None = Query(default=None),
    benchmark_id: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=100, ge=1, le=200),
):
    try:
        helpers = _intelligence_helpers()
        with get_session(load_config()) as session:
            rows = helpers.list_quality_metrics(
                session,
                metric_scope=metric_scope,
                benchmark_id=benchmark_id,
                limit=limit,
            )
        return {"metrics": _jsonable(rows), "limit": limit}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})


@router.get("/cohorts")
def intelligence_case_cohorts(
    since: datetime | None = Query(default=None),
):
    if _live_case_cohorts is None:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    try:
        with get_session(load_config()) as session:
            rows = _live_case_cohorts(session, since=since)
        return {"cohorts": _jsonable(rows)}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})


@router.get("/status")
def intelligence_status(limit: int = Query(default=20, ge=1, le=100)):
    try:
        helpers = _intelligence_helpers()
        with get_session(load_config()) as session:
            return _jsonable(helpers.research_status(session, limit=limit))
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})


@router.post("/run", status_code=202)
async def run_intelligence(
    request: Request, body: dict | None = Body(default=None)
):
    try:
        force = _run_body(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _enforce_api_budget(None)
    return await _research_orchestrator_post(
        request, "/research/run", {"force": force}
    )


@router.post("/cases/{case_id}/run", status_code=202)
async def run_intelligence_case(
    case_id: str,
    request: Request,
    body: dict | None = Body(default=None),
):
    try:
        helpers = _intelligence_helpers()
        parsed = _uuid(case_id, "case_id")
        force = _run_body(body)
        with get_session(load_config()) as session:
            if helpers.get_case(session, parsed, detail_limit=1) is None:
                raise HTTPException(status_code=404, detail="Research case not found")
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    _enforce_api_budget(None)
    return await _research_orchestrator_post(
        request, f"/research/cases/{parsed}/run", {"force": force}
    )


@router.post("/jobs/{job_id}/retry", status_code=202)
async def retry_intelligence_job(job_id: str, request: Request):
    try:
        parsed = _uuid(job_id, "job_id")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _enforce_api_budget(None)
    return await _research_orchestrator_post(
        request, f"/research/jobs/{parsed}/retry"
    )
