"""Phase 9 long-horizon research workspace: deterministic, fail-soft pages.

Themes, theses, evidence, and research cases render statically on save.
Every section is bounded and fail-soft; nothing here calls an LLM or streams
live data.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from uuid import UUID

from api_db import get_session, query_many, query_one
from fastapi import APIRouter, HTTPException, Request

from config import load_config

try:
    from research_intelligence import queries as _research_queries
    from research_intelligence.benchmarks import list_benchmarks as _list_benchmarks

    _live_case_cohorts = _research_queries.live_case_cohorts
except ImportError:  # pragma: no cover - deployment wiring
    _research_queries = None
    _list_benchmarks = None
    _live_case_cohorts = None

router = APIRouter()

MAX_THEMES = 200
MAX_ENTITIES = 500
MAX_THESES = 100
MAX_ATOMS = 20
MAX_INDICATORS = 10
MAX_EVENTS = 10
MAX_THESIS_DETAIL = 10  # catalysts / risks per thesis
EXCERPT_LIMIT = 500

# Ordered idea-funnel outline rendered as nav on the research index page.
FUNNEL_STEPS = (
    {
        "key": "structural_trend",
        "title": "1. Structural Trend",
        "question": "What secular shift creates an asymmetric demand/supply imbalance?",
        "deliverable": "Market driver analysis with observable indicators",
    },
    {
        "key": "company_exposure",
        "title": "2. Company Exposure",
        "question": "Which specific companies capture or suffer from this trend?",
        "deliverable": "Theme entity universe with supply-chain role mapping",
    },
    {
        "key": "variant_perception",
        "title": "3. Variant Perception",
        "question": "Where does consensus misprice the magnitude, duration, or timing?",
        "deliverable": "Core thesis statement with explicit consensus contrast",
    },
    {
        "key": "falsification_framework",
        "title": "4. Falsification Framework",
        "question": "What observable, non-trivial facts would prove this thesis wrong?",
        "deliverable": "Pre-committed invalidation conditions with test schedule",
    },
    {
        "key": "catalyst_pathway",
        "title": "5. Catalyst Pathway",
        "question": "What discrete events force the market to re-rate this position?",
        "deliverable": "Chronological catalyst calendar with leading indicators",
    },
    {
        "key": "risk_matrix",
        "title": "6. Risk Matrix",
        "question": "What could permanently impair capital despite the thesis holding?",
        "deliverable": "Categorised risk register with severity classifications",
    },
    {
        "key": "portfolio_fit",
        "title": "7. Portfolio Fit",
        "question": "How does this thesis interact with existing exposures and liquidity?",
        "deliverable": "Factor overlap, correlation analysis, and sizing envelope",
    },
    {
        "key": "review_cadence",
        "title": "8. Review Cadence",
        "question": "When and on what triggers is this thesis formally re-evaluated?",
        "deliverable": "Scheduled review dates and automated alert thresholds",
    },
)

THEMES_SQL = """
SELECT t.id, t.name, t.definition, t.horizon, t.status, t.confidence, t.review_at,
       (SELECT COUNT(*) FROM investment_theme_entities e WHERE e.theme_id = t.id)
           AS entity_count,
       (SELECT COUNT(*) FROM investment_theses th WHERE th.theme_id = t.id)
           AS thesis_count
FROM investment_themes t
ORDER BY t.updated_at DESC, t.id DESC
LIMIT :limit
"""

THEME_SQL = """
SELECT t.id, t.name, t.definition, t.horizon, t.macro_drivers, t.key_indicators,
       t.status, t.review_at, t.invalidation_conditions, t.confidence,
       t.confidence_components, t.updated_at
FROM investment_themes t
WHERE t.id = CAST(:theme_id AS UUID)
"""

ENTITIES_SQL = """
SELECT entity_type, entity_id, display_name
FROM investment_theme_entities
WHERE theme_id = CAST(:theme_id AS UUID)
ORDER BY entity_type ASC, display_name ASC
LIMIT :limit
"""

THESES_SQL = """
SELECT th.id, th.company, th.symbol, th.claim, th.variant_perception, th.status,
       th.horizon, th.review_at, th.confidence, th.invalidation_conditions,
       th.updated_at,
       v.version, v.claim AS latest_claim, v.rationale, v.created_at AS version_created_at,
       (SELECT COUNT(*) FROM investment_thesis_evidence e
        WHERE e.thesis_id = th.id AND e.relationship = 'supports') AS supports,
       (SELECT COUNT(*) FROM investment_thesis_evidence e
        WHERE e.thesis_id = th.id AND e.relationship = 'contradicts') AS contradicts,
       (SELECT COUNT(*) FROM investment_thesis_evidence e
        WHERE e.thesis_id = th.id AND e.relationship = 'context') AS context
FROM investment_theses th
LEFT JOIN LATERAL (
    SELECT version, claim, rationale, created_at
    FROM investment_thesis_versions
    WHERE thesis_id = th.id
    ORDER BY version DESC
    LIMIT 1
) v ON TRUE
WHERE th.theme_id = CAST(:theme_id AS UUID)
ORDER BY th.updated_at DESC, th.id DESC
LIMIT :limit
"""

CATALYSTS_SQL = """
SELECT id, description, expected_at, state
FROM investment_catalysts
WHERE thesis_id = CAST(:thesis_id AS UUID)
ORDER BY expected_at ASC NULLS LAST, id ASC
LIMIT :limit
"""

RISKS_SQL = """
SELECT id, description, kind, severity
FROM investment_risks
WHERE thesis_id = CAST(:thesis_id AS UUID)
ORDER BY severity ASC, id ASC
LIMIT :limit
"""

ATOMS_SQL = """
SELECT e.relationship, a.id, a.claim, a.confidence, a.status, a.published_at
FROM investment_thesis_evidence e
JOIN analysis_atoms a ON a.id::text = e.evidence_id
WHERE e.thesis_id = CAST(:thesis_id AS UUID)
  AND e.evidence_type = 'analysis_atom'
ORDER BY a.published_at DESC NULLS LAST, a.id DESC
LIMIT :limit
"""

INDICATORS_SQL = """
SELECT DISTINCT ON (m.series_id) m.series_id, m.observed_at, m.value,
       md.title, md.units
FROM macro_series m
LEFT JOIN macro_series_metadata md ON md.series_id = m.series_id
WHERE m.series_id = ANY(:series_ids)
ORDER BY m.series_id, m.observed_at DESC
LIMIT :limit
"""

EVENTS_SQL = """
SELECT event_id, event_name, country, scheduled_at, impact_level,
       consensus, previous, actual
FROM econ_events
WHERE scheduled_at >= NOW() - INTERVAL '7 days'
ORDER BY scheduled_at ASC
LIMIT :limit
"""


# ---------------------------------------------------------------------------
# Small deterministic helpers
# ---------------------------------------------------------------------------


def _text(value, limit: int) -> str:
    if value is None:
        return ""
    return str(value)[:limit]


def _iso(value) -> str | None:
    if isinstance(value, datetime):
        aware = (
            value.replace(tzinfo=UTC)
            if value.tzinfo is None or value.utcoffset() is None
            else value.astimezone(UTC)
        )
        return aware.isoformat()
    return _text(value, 64) or None


def _finite(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _json_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _json_list(value, limit: int) -> list:
    return value[:limit] if isinstance(value, list) else []


def _confidence_components(value) -> list[dict]:
    components = _json_dict(value)
    items = []
    for name, component in list(components.items())[:12]:
        if isinstance(component, dict):
            score = _finite(component.get("score", component.get("value")))
            label = _text(component.get("label") or component.get("note"), 120) or None
        else:
            score = _finite(component)
            label = None
        items.append({"name": _text(name, 64), "score": score, "label": label})
    return items


def _validate_theme_id(value: str) -> str:
    try:
        parsed = UUID(str(value).strip())
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="Invalid theme id")
    return str(parsed)


# ---------------------------------------------------------------------------
# Research index
# ---------------------------------------------------------------------------


def load_research_index(config: dict) -> dict:
    """Themes with entity/thesis counts plus the ordered idea-funnel outline."""
    try:
        rows = query_many(THEMES_SQL, {"limit": MAX_THEMES}, config=config)
    except Exception:
        return {"status": "unavailable", "themes": []}
    themes = [
        {
            "id": str(row["id"]),
            "name": _text(row.get("name"), 120),
            "definition": _text(row.get("definition"), 500),
            "horizon": _text(row.get("horizon"), 24),
            "status": _text(row.get("status"), 16),
            "confidence": _finite(row.get("confidence")),
            "review_at": _iso(row.get("review_at")),
            "entity_count": int(row.get("entity_count") or 0),
            "thesis_count": int(row.get("thesis_count") or 0),
        }
        for row in rows
    ]
    return {
        "status": "published" if themes else "empty",
        "themes": themes,
        "funnel": [dict(step) for step in FUNNEL_STEPS],
    }


# ---------------------------------------------------------------------------
# Theme page
# ---------------------------------------------------------------------------


def load_theme_page(config: dict, theme_id: str) -> dict | None:
    """Theme row plus bounded per-section detail; None when the theme is unknown."""
    try:
        row = query_one(THEME_SQL, {"theme_id": theme_id}, config=config)
    except Exception:
        return {"status": "unavailable", "theme": None}
    if row is None:
        return None
    theme = {
        "id": str(row["id"]),
        "name": _text(row.get("name"), 120),
        "definition": _text(row.get("definition"), 1000),
        "horizon": _text(row.get("horizon"), 24),
        "status": _text(row.get("status"), 16),
        "confidence": _finite(row.get("confidence")),
        "review_at": _iso(row.get("review_at")),
        "updated_at": _iso(row.get("updated_at")),
        "macro_drivers": [
            _text(item, 200) for item in _json_list(row.get("macro_drivers"), 20)
        ],
        "key_indicators": [
            _text(item, 64) for item in _json_list(row.get("key_indicators"), 20)
        ],
        "invalidation_conditions": [
            _text(item, EXCERPT_LIMIT)
            for item in _json_list(row.get("invalidation_conditions"), 20)
        ],
        "confidence_components": _confidence_components(
            row.get("confidence_components")
        ),
        "entities": _theme_entities(config, theme_id),
        "theses": _theme_theses(config, theme_id),
        "indicators": _theme_indicators(config, row.get("key_indicators")),
        "events": _theme_events(config),
    }
    return {"status": "published", "theme": theme}


def _theme_entities(config: dict, theme_id: str) -> dict:
    try:
        rows = query_many(
            ENTITIES_SQL,
            {"theme_id": theme_id, "limit": MAX_ENTITIES},
            config=config,
        )
    except Exception:
        return {"available": False, "groups": {}}
    groups: dict[str, list[dict]] = {}
    for row in rows:
        entity_type = _text(row.get("entity_type"), 24) or "other"
        groups.setdefault(entity_type, []).append(
            {
                "entity_id": _text(row.get("entity_id"), 200),
                "display_name": _text(
                    row.get("display_name") or row.get("entity_id"), 120
                ),
            }
        )
    return {"available": True, "groups": groups}


def _theme_theses(config: dict, theme_id: str) -> dict:
    try:
        rows = query_many(
            THESES_SQL, {"theme_id": theme_id, "limit": MAX_THESES}, config=config
        )
    except Exception:
        return {"available": False, "rows": []}
    items = []
    for row in rows:
        thesis_id = str(row["id"])
        items.append(
            {
                "id": thesis_id,
                "company": _text(row.get("company"), 64) or None,
                "symbol": _text(row.get("symbol"), 16) or None,
                "claim": _text(row.get("claim"), 1000),
                "variant_perception": _text(row.get("variant_perception"), 1000)
                or None,
                "status": _text(row.get("status"), 16),
                "horizon": _text(row.get("horizon"), 24) or None,
                "review_at": _iso(row.get("review_at")),
                "confidence": _finite(row.get("confidence")),
                "invalidation_conditions": [
                    _text(item, EXCERPT_LIMIT)
                    for item in _json_list(row.get("invalidation_conditions"), 20)
                ],
                "version": row.get("version"),
                "latest_claim": _text(row.get("latest_claim"), 1000) or None,
                "rationale": _text(row.get("rationale"), 2000) or None,
                "version_created_at": _iso(row.get("version_created_at")),
                "evidence_counts": {
                    "supports": int(row.get("supports") or 0),
                    "contradicts": int(row.get("contradicts") or 0),
                    "context": int(row.get("context") or 0),
                },
                "catalysts": _thesis_catalysts(config, thesis_id),
                "risks": _thesis_risks(config, thesis_id),
                "atoms": _thesis_atoms(config, thesis_id),
            }
        )
    return {"available": True, "rows": items}


def _thesis_catalysts(config: dict, thesis_id: str) -> dict:
    try:
        rows = query_many(
            CATALYSTS_SQL,
            {"thesis_id": thesis_id, "limit": MAX_THESIS_DETAIL},
            config=config,
        )
    except Exception:
        return {"available": False, "rows": []}
    return {
        "available": True,
        "rows": [
            {
                "id": str(row["id"]),
                "description": _text(row.get("description"), 500),
                "expected_at": _iso(row.get("expected_at")),
                "state": _text(row.get("state"), 16),
            }
            for row in rows
        ],
    }


def _thesis_risks(config: dict, thesis_id: str) -> dict:
    try:
        rows = query_many(
            RISKS_SQL,
            {"thesis_id": thesis_id, "limit": MAX_THESIS_DETAIL},
            config=config,
        )
    except Exception:
        return {"available": False, "rows": []}
    return {
        "available": True,
        "rows": [
            {
                "id": str(row["id"]),
                "description": _text(row.get("description"), 500),
                "kind": _text(row.get("kind"), 24),
                "severity": _text(row.get("severity"), 16),
            }
            for row in rows
        ],
    }


def _thesis_atoms(config: dict, thesis_id: str) -> dict:
    """Bounded supporting/contradicting atoms linked to one thesis."""
    try:
        rows = query_many(
            ATOMS_SQL, {"thesis_id": thesis_id, "limit": MAX_ATOMS}, config=config
        )
    except Exception:
        return {"available": False, "supporting": [], "contradicting": []}
    supporting: list[dict] = []
    contradicting: list[dict] = []
    for row in rows:
        atom = {
            "id": str(row["id"]),
            "claim": _text(row.get("claim"), 1000),
            "confidence": _finite(row.get("confidence")),
            "status": _text(row.get("status"), 16),
            "published_at": _iso(row.get("published_at")),
        }
        if _text(row.get("relationship"), 16) == "supports":
            supporting.append(atom)
        elif _text(row.get("relationship"), 16) == "contradicts":
            contradicting.append(atom)
    return {
        "available": True,
        "supporting": supporting,
        "contradicting": contradicting,
    }


def _theme_indicators(config: dict, key_indicators) -> dict:
    series_ids = [
        str(item)[:64] for item in _json_list(key_indicators, 20) if str(item).strip()
    ]
    if not series_ids:
        return {"available": True, "rows": []}
    try:
        rows = query_many(
            INDICATORS_SQL,
            {"series_ids": series_ids, "limit": MAX_INDICATORS},
            config=config,
        )
    except Exception:
        return {"available": False, "rows": []}
    return {
        "available": True,
        "rows": [
            {
                "series_id": _text(row.get("series_id"), 64),
                "title": _text(row.get("title"), 120) or None,
                "units": _text(row.get("units"), 16) or None,
                "value": _finite(row.get("value")),
                "observed_at": _iso(row.get("observed_at")),
            }
            for row in rows
        ],
    }


def _theme_events(config: dict) -> dict:
    try:
        rows = query_many(EVENTS_SQL, {"limit": MAX_EVENTS}, config=config)
    except Exception:
        return {"available": False, "rows": []}
    return {
        "available": True,
        "rows": [
            {
                "event_id": _text(row.get("event_id"), 64),
                "event_name": _text(row.get("event_name"), 200),
                "country": _text(row.get("country"), 32),
                "scheduled_at": _iso(row.get("scheduled_at")),
                "impact_level": _text(row.get("impact_level"), 16) or None,
                "consensus": _text(row.get("consensus"), 64) or None,
                "previous": _text(row.get("previous"), 64) or None,
                "actual": _text(row.get("actual"), 64) or None,
            }
            for row in rows
        ],
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/research")
def research_page(request: Request):
    config = load_config()
    try:
        index = load_research_index(config)
    except Exception:
        index = {
            "status": "unavailable",
            "themes": [],
            "funnel": [dict(step) for step in FUNNEL_STEPS],
        }
    try:
        if _research_queries is None:
            raise RuntimeError("research intelligence helpers unavailable")
        with get_session(config) as session:
            index["cases"] = _research_queries.list_cases(session, limit=30)
        index["cases_status"] = "available"
    except Exception:
        index["cases"] = []
        index["cases_status"] = "unavailable"
    return request.app.state.templates.TemplateResponse(
        request,
        "research.html",
        {"request": request, "index": index},
    )


@router.get("/research/evaluation")
def research_evaluation_page(request: Request):
    config = load_config()
    payload = {
        "status": "available",
        "benchmarks": [],
        "replays": [],
        "cohorts": [],
        "comparisons": [],
        "research_status": {},
    }
    try:
        if (
            _research_queries is None
            or _list_benchmarks is None
            or _live_case_cohorts is None
        ):
            raise RuntimeError("research evaluation helpers unavailable")
        payload["benchmarks"] = [
            {
                "id": item.episode_id,
                "version": item.version,
                "kind": item.episode_kind,
                "synthetic": item.synthetic,
                "description": item.description,
                "replay_dates": item.replay_dates,
                "evidence_count": len(item.evidence),
            }
            for item in _list_benchmarks()
        ]
        with get_session(config) as session:
            replays = _research_queries.list_replay_runs(session, limit=30)
            for run in replays:
                stages = run.get("stage_metrics") or []
                run["latency_ms"] = sum(
                    int(stage.get("duration_ms") or 0)
                    for stage in stages
                    if isinstance(stage, dict)
                )
                run["failure_count"] = len(
                    (run.get("result_summary") or {}).get("errors") or []
                )
                identity = run.get("variant_identity") or {}
                run["variant_models"] = sorted(
                    {
                        str(stage.get("model"))
                        for stage in identity.values()
                        if isinstance(stage, dict) and stage.get("model")
                    }
                )
                run["variant_short"] = str(run.get("variant_fingerprint") or "")[:10]
            payload["replays"] = replays
            payload["comparisons"] = _research_queries.list_quality_metrics(
                session, metric_scope="comparison", limit=10
            )
            payload["cohorts"] = _live_case_cohorts(session)
            payload["research_status"] = _research_queries.research_status(
                session, limit=5
            )
    except Exception:
        payload["status"] = "unavailable"
    return request.app.state.templates.TemplateResponse(
        request,
        "research_evaluation.html",
        {"request": request, "evaluation": payload},
    )


@router.get("/research/theses")
def research_theses_page(request: Request):
    """Render the bounded thesis-tournament desk shell."""
    return request.app.state.templates.TemplateResponse(
        request,
        "research_theses.html",
        {"request": request},
    )


@router.get("/research/theses/{thesis_id}")
def research_thesis_page(request: Request, thesis_id: str):
    """Render one dossier only after validating its immutable identifier."""
    try:
        normalized = str(UUID(thesis_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=404, detail="Thesis not found") from exc
    return request.app.state.templates.TemplateResponse(
        request,
        "research_thesis.html",
        {"request": request, "thesis_id": normalized},
    )


@router.get("/research/theses/review")
@router.get("/research/proposals")
@router.get("/research/review-queue")
def research_review_queue_page(request: Request):
    """Render the bounded thesis proposal review queue shell."""
    return request.app.state.templates.TemplateResponse(
        request,
        "research_theses.html",
        {"request": request, "view": "review_queue"},
    )


@router.get("/research/theses/proposals/{proposal_id}")
@router.get("/research/proposals/{proposal_id}")
def research_proposal_page(request: Request, proposal_id: str):
    """Render one proposal dossier for review queue inspection."""
    try:
        normalized = str(UUID(proposal_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=404, detail="Proposal not found") from exc
    return request.app.state.templates.TemplateResponse(
        request,
        "research_thesis.html",
        {
            "request": request,
            "thesis_id": normalized,
            "proposal_id": normalized,
            "is_proposal": True,
        },
    )


@router.get("/research/cases/{case_id}")
def research_case_page(request: Request, case_id: str):
    try:
        normalized = str(UUID(case_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=404, detail="Research case not found") from exc
    config = load_config()
    try:
        if _research_queries is None:
            raise RuntimeError("research intelligence helpers unavailable")
        with get_session(config) as session:
            payload = _research_queries.get_case(session, normalized, detail_limit=150)
            history = _research_queries.case_history(session, normalized, limit=20)
    except Exception:
        payload, history = {"status": "unavailable", "case": None}, []
    if payload is None:
        raise HTTPException(status_code=404, detail="Research case not found")
    case = payload.get("case") if isinstance(payload, dict) else None
    snapshot = (
        case.get("current_snapshot")
        if isinstance(case, dict) and isinstance(case.get("current_snapshot"), dict)
        else {}
    )
    return request.app.state.templates.TemplateResponse(
        request,
        "research_case.html",
        {
            "request": request,
            "research_case": case,
            "detail": payload,
            "snapshot": snapshot,
            "deliverable": snapshot.get("deliverable") or {},
            "history": history,
        },
    )


@router.get("/research/themes/{theme_id}")
def research_theme_page(request: Request, theme_id: str):
    normalized = _validate_theme_id(theme_id)
    config = load_config()
    try:
        payload = load_theme_page(config, normalized)
    except Exception:
        payload = {"status": "unavailable", "theme": None}
    if payload is None:
        raise HTTPException(status_code=404, detail="Theme not found")
    return request.app.state.templates.TemplateResponse(
        request,
        "research_theme.html",
        {
            "request": request,
            "theme": payload.get("theme"),
            "status": payload.get("status"),
        },
    )
