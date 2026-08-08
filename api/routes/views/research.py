"""Phase 9 long-horizon research workspace: deterministic, fail-soft pages.

Themes, theses, evidence, and company dossiers render statically on save.
Every section is bounded and fail-soft; nothing here calls an LLM or streams
live data.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from config import load_config
from db import query_many, query_one

router = APIRouter()

MAX_THEMES = 200
MAX_ENTITIES = 500
MAX_THESES = 100
MAX_ATOMS = 20
MAX_INDICATORS = 10
MAX_EVENTS = 10
MAX_THESIS_DETAIL = 10  # catalysts / risks per thesis
MAX_DELTAS = 20
MAX_FINANCIAL_TRENDS = 5
MAX_SOURCES = 10
EXCERPT_LIMIT = 500
CHANGE_KINDS = ("new", "changed", "removed", "unchanged")

# Ordered idea-funnel outline rendered as nav on the research index page.
FUNNEL_STEPS = (
    {
        "key": "structural_trend",
        "label": "Structural trend",
        "detail": "The long-horizon force the idea rests on",
    },
    {
        "key": "affected_industries",
        "label": "Affected industries",
        "detail": "Industries the trend reshapes",
    },
    {
        "key": "candidate_companies",
        "label": "Candidate companies",
        "detail": "Companies with the most exposure",
    },
    {
        "key": "evidence",
        "label": "Evidence",
        "detail": "Signals that support or contradict the thesis",
    },
    {
        "key": "expectations_valuation",
        "label": "Expectations and valuation",
        "detail": "What the market prices and what the thesis assumes",
    },
    {
        "key": "catalysts",
        "label": "Catalysts",
        "detail": "Events that could trigger repricing",
    },
    {
        "key": "risks_counter_thesis",
        "label": "Risks and counter-thesis",
        "detail": "What would break the thesis",
    },
)

# Deterministic metric names pulled from investment_analyses.facts JSONB.
STANDARD_METRICS = (
    "revenue",
    "operating_cash_flow",
    "capex",
    "net_income",
    "diluted_eps",
    "shares_outstanding",
    "net_debt",
    "gross_margin",
    "inventory",
    "backlog",
    "gross_profit",
    "cash",
    "total_debt",
    "total_assets",
    "total_liabilities",
    "equity",
    "current_assets",
    "current_liabilities",
)

THEMES_SQL = """
SELECT t.id, t.name, t.definition, t.horizon, t.status, t.confidence, t.review_at,
       (SELECT COUNT(*) FROM investment_theme_entities e WHERE e.theme_id = t.id)
           AS entity_count,
       (SELECT COUNT(*) FROM investment_theses th WHERE th.theme_id = t.id)
           AS thesis_count
FROM investment_themes t
ORDER BY t.updated_at DESC, t.name ASC
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
ORDER BY entity_type ASC, display_name ASC NULLS LAST, entity_id ASC
LIMIT :limit
"""

THESES_SQL = """
SELECT th.id, th.company, th.symbol, th.claim, th.variant_perception, th.status,
       th.horizon, th.review_at, th.confidence, th.invalidation_conditions,
       th.updated_at,
       v.version, v.claim AS latest_claim, v.rationale, v.changed_by,
       v.created_at AS version_created_at,
       counts.supports, counts.contradicts, counts.context
FROM investment_theses th
LEFT JOIN LATERAL (
    SELECT version, claim, rationale, changed_by, created_at
    FROM investment_thesis_versions
    WHERE thesis_id = th.id
    ORDER BY version DESC
    LIMIT 1
) v ON TRUE
LEFT JOIN LATERAL (
    SELECT COUNT(*) FILTER (WHERE relationship = 'supports') AS supports,
           COUNT(*) FILTER (WHERE relationship = 'contradicts') AS contradicts,
           COUNT(*) FILTER (WHERE relationship = 'context') AS context
    FROM investment_thesis_evidence
    WHERE thesis_id = th.id
) counts ON TRUE
WHERE th.theme_id = CAST(:theme_id AS UUID)
ORDER BY th.updated_at DESC, th.id DESC
LIMIT :limit
"""

CATALYSTS_SQL = """
SELECT id, description, expected_at, state
FROM investment_catalysts
WHERE thesis_id = CAST(:thesis_id AS UUID)
ORDER BY expected_at ASC NULLS LAST, created_at DESC
LIMIT :limit
"""

RISKS_SQL = """
SELECT id, description, kind, severity
FROM investment_risks
WHERE thesis_id = CAST(:thesis_id AS UUID)
ORDER BY created_at DESC
LIMIT :limit
"""

ATOMS_SQL = """
SELECT e.relationship, a.id, a.claim, a.confidence, a.status, a.published_at
FROM investment_thesis_evidence e
JOIN analysis_atoms a ON a.id::text = e.evidence_id
WHERE e.thesis_id = CAST(:thesis_id AS UUID)
  AND e.evidence_type = 'atom'
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
WHERE scheduled_at >= NOW()
ORDER BY scheduled_at ASC
LIMIT :limit
"""

PROFILE_SQL = """
SELECT company, symbol, business_overview, segments, key_operating_drivers,
       capital_allocation, valuation_assumptions, guidance, updated_at
FROM company_research_profiles
WHERE company = :company
"""

LATEST_DOCUMENT_SQL = """
SELECT document_id, company, symbol, region, industry, document_type,
       report_date, filename, status, created_at
FROM investment_documents
WHERE company = :company
ORDER BY report_date DESC NULLS LAST, created_at DESC
LIMIT 1
"""

DOSSIER_THESES_SQL = """
SELECT th.id, th.theme_id, t.name AS theme_name, th.claim, th.status,
       th.horizon, th.confidence, th.updated_at
FROM investment_theses th
LEFT JOIN investment_themes t ON t.id = th.theme_id
WHERE th.company = :company
ORDER BY th.updated_at DESC, th.id DESC
LIMIT :limit
"""

DELTAS_SQL = """
SELECT category, change_kind, excerpt, previous_excerpt, created_at
FROM investment_filing_deltas
WHERE document_id = CAST(:document_id AS UUID)
ORDER BY created_at DESC, category ASC
LIMIT :limit
"""

FINANCIAL_SQL = """
SELECT a.analysis_id, a.facts, a.model, a.created_at,
       d.document_type, d.report_date
FROM investment_analyses a
JOIN investment_documents d ON d.document_id = a.document_id
WHERE d.company = :company
ORDER BY d.report_date DESC NULLS LAST, a.created_at DESC
LIMIT :limit
"""

SOURCES_SQL = """
SELECT d.document_id, d.document_type, d.report_date, d.status, d.created_at,
       (SELECT COUNT(*) FROM investment_thesis_evidence e
        WHERE e.evidence_type = 'investment_document'
          AND e.evidence_id = d.document_id::text) AS evidence_count
FROM investment_documents d
WHERE d.company = :company
ORDER BY d.report_date DESC NULLS LAST, d.created_at DESC
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


def _validate_company(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 64:
        raise HTTPException(status_code=422, detail="Invalid company")
    return normalized


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
# Company dossier
# ---------------------------------------------------------------------------


def load_dossier(config: dict, company: str) -> dict | None:
    """Company dossier; None when the company has neither a profile nor filings."""
    profile = _dossier_profile(config, company)
    latest = _dossier_latest_document(config, company)
    if (
        profile["available"]
        and latest["available"]
        and profile["row"] is None
        and latest["row"] is None
    ):
        return None
    deltas = _dossier_deltas(config, latest)
    return {
        "status": "published",
        "company": company,
        "profile": _profile_row(profile["row"]) if profile["row"] else None,
        "latest_document": (_document_row(latest["row"]) if latest["row"] else None),
        "theses": _dossier_theses(config, company),
        "deltas": deltas,
        "changes_since_previous": [
            delta
            for delta in deltas["rows"]
            if delta["change_kind"] in ("new", "changed", "removed")
        ],
        "financial_trends": _dossier_financial(config, company),
        "sources": _dossier_sources(config, company),
    }


def _dossier_profile(config: dict, company: str) -> dict:
    try:
        row = query_one(PROFILE_SQL, {"company": company}, config=config)
    except Exception:
        return {"available": False, "row": None}
    return {"available": True, "row": row}


def _dossier_latest_document(config: dict, company: str) -> dict:
    try:
        row = query_one(LATEST_DOCUMENT_SQL, {"company": company}, config=config)
    except Exception:
        return {"available": False, "row": None}
    return {"available": True, "row": row}


def _profile_row(row: dict) -> dict:
    return {
        "company": _text(row.get("company"), 64),
        "symbol": _text(row.get("symbol"), 16) or None,
        "business_overview": _text(row.get("business_overview"), 3000) or None,
        "segments": [
            _text(segment, 500)
            for segment in _json_list(row.get("segments"), 20)
            if isinstance(segment, str) and segment.strip()
        ],
        "key_operating_drivers": [
            _text(driver, 200)
            for driver in _json_list(row.get("key_operating_drivers"), 12)
            if isinstance(driver, str) and driver.strip()
        ],
        "capital_allocation": _text(row.get("capital_allocation"), 2000) or None,
        "valuation_assumptions": _json_dict(row.get("valuation_assumptions")),
        "guidance": _json_dict(row.get("guidance")),
        "updated_at": _iso(row.get("updated_at")),
    }


def _document_row(row: dict) -> dict:
    return {
        "document_id": str(row.get("document_id") or ""),
        "company": _text(row.get("company"), 64),
        "symbol": _text(row.get("symbol"), 16) or None,
        "region": _text(row.get("region"), 8),
        "industry": _text(row.get("industry"), 120),
        "document_type": _text(row.get("document_type"), 40),
        "report_date": _iso(row.get("report_date")),
        "filename": _text(row.get("filename"), 240),
        "status": _text(row.get("status"), 16),
        "created_at": _iso(row.get("created_at")),
    }


def _dossier_theses(config: dict, company: str) -> dict:
    try:
        rows = query_many(
            DOSSIER_THESES_SQL,
            {"company": company, "limit": MAX_THESES},
            config=config,
        )
    except Exception:
        return {"available": False, "rows": []}
    return {
        "available": True,
        "rows": [
            {
                "id": str(row["id"]),
                "theme_id": str(row["theme_id"]),
                "theme_name": _text(row.get("theme_name"), 120) or None,
                "claim": _text(row.get("claim"), 1000),
                "status": _text(row.get("status"), 16),
                "horizon": _text(row.get("horizon"), 24) or None,
                "confidence": _finite(row.get("confidence")),
                "updated_at": _iso(row.get("updated_at")),
            }
            for row in rows
        ],
    }


def _dossier_deltas(config: dict, latest: dict) -> dict:
    if not latest.get("available"):
        return {"available": False, "rows": []}
    row = latest.get("row")
    if row is None:
        return {"available": True, "rows": []}
    try:
        rows = query_many(
            DELTAS_SQL,
            {
                "document_id": str(row.get("document_id") or ""),
                "limit": MAX_DELTAS,
            },
            config=config,
        )
    except Exception:
        return {"available": False, "rows": []}
    return {
        "available": True,
        "rows": [
            {
                "category": _text(row.get("category"), 40),
                "change_kind": _text(row.get("change_kind"), 16),
                "excerpt": _text(row.get("excerpt"), EXCERPT_LIMIT) or None,
                "previous_excerpt": (
                    _text(row.get("previous_excerpt"), EXCERPT_LIMIT) or None
                ),
                "created_at": _iso(row.get("created_at")),
            }
            for row in rows
        ],
    }


def _facts_metrics(value, limit: int = 12) -> list[dict]:
    facts = _json_dict(value)
    metrics = _json_dict(facts.get("metrics"))
    items = []
    for name in STANDARD_METRICS:
        record = metrics.get(name)
        record = _json_dict(record) if isinstance(record, dict) else {}
        number = _finite(record.get("value"))
        if number is None:
            continue
        items.append(
            {
                "name": name,
                "value": number,
                "unit": _text(record.get("unit"), 16) or None,
                "period": _text(record.get("period"), 32) or None,
                "change_pct": _finite(record.get("change_pct")),
            }
        )
        if len(items) == limit:
            break
    return items


def _dossier_financial(config: dict, company: str) -> dict:
    try:
        rows = query_many(
            FINANCIAL_SQL,
            {"company": company, "limit": MAX_FINANCIAL_TRENDS},
            config=config,
        )
    except Exception:
        return {"available": False, "rows": []}
    return {
        "available": True,
        "rows": [
            {
                "analysis_id": str(row["analysis_id"]),
                "document_type": _text(row.get("document_type"), 40),
                "report_date": _iso(row.get("report_date")),
                "created_at": _iso(row.get("created_at")),
                "model": _text(row.get("model"), 64) or None,
                "metrics": _facts_metrics(row.get("facts")),
            }
            for row in rows
        ],
    }


def _dossier_sources(config: dict, company: str) -> dict:
    try:
        rows = query_many(
            SOURCES_SQL, {"company": company, "limit": MAX_SOURCES}, config=config
        )
    except Exception:
        return {"available": False, "rows": []}
    return {
        "available": True,
        "rows": [
            {
                "document_id": str(row["document_id"]),
                "document_type": _text(row.get("document_type"), 40),
                "report_date": _iso(row.get("report_date")),
                "status": _text(row.get("status"), 16),
                "created_at": _iso(row.get("created_at")),
                "evidence_count": int(row.get("evidence_count") or 0),
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
    return request.app.state.templates.TemplateResponse(
        request,
        "research.html",
        {"request": request, "index": index},
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


@router.get("/research/companies/{company}")
def research_dossier_page(request: Request, company: str):
    normalized = _validate_company(company)
    config = load_config()
    try:
        payload = load_dossier(config, normalized)
    except Exception:
        payload = {"status": "unavailable", "dossier": None}
    if payload is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return request.app.state.templates.TemplateResponse(
        request,
        "research_dossier.html",
        {
            "request": request,
            "dossier": payload.get("dossier"),
            "status": payload.get("status"),
        },
    )
