"""Bounded Phase 9 research workspace routes.

All POST bodies are validated before a session is opened; helpers own the
queries and the route owns the transaction (``get_session`` commits once).
Responses serialise datetimes as ISO strings and UUIDs as strings; unknown
resources are 404, invalid input 422, and infrastructure failures fail soft
as 503 without leaking details.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import JSONResponse

from config import load_config
from db import get_session

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


def _helpers() -> Any:
    """Return the orchestrator research module or fail soft with 503."""
    if _research is None:
        raise RuntimeError("research helpers unavailable")
    return _research


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
