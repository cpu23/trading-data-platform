"""Long-horizon investment research workspace: themes, theses, and portfolio.

Phase 9 normalized research objects.  Every helper takes the caller's session
and never commits or rolls back; all queries are bounded and allowlisted so the
API layer can fail soft without leaking private payloads.  No LLM calls are
made from this module: filing-delta interpretation stays opt-in elsewhere.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

THEME_STATUSES = ("active", "paused", "retired")
THESIS_STATUSES = ("candidate", "active", "paused", "closed")
ENTITY_TYPES = ("industry", "company", "symbol", "macro_series")
EVIDENCE_TYPES = (
    "macro_series",
    "market_data",
    "market_events",
    "econ_events",
    "story_cluster",
    "opinion",
    "atom",
    "filing_delta",
    "document",
)
RELATIONSHIPS = ("supports", "contradicts", "context", "invalidation")
CATALYST_STATES = ("pending", "confirmed", "missed", "expired")
RISK_KINDS = ("counter_thesis", "execution", "external")
RISK_SEVERITIES = ("low", "moderate", "high")
HOLDING_SOURCES = ("manual", "import")

# Evidence keys that belong to the autonomous thesis-fusion desk
# (orchestrator/thesis_fusion.py).  When any row carries one of these keys,
# add_thesis_evidence delegates to the desk repository so provenance,
# fingerprint, and weight metadata are validated and stored consistently.
_DESK_EVIDENCE_KEYS = frozenset(
    {
        "source_name",
        "source_family",
        "origin_key",
        "independence_key",
        "evidence_fingerprint",
        "content",
        "source_timestamp",
        "available_at",
        "quality_score",
        "entailment_score",
        "freshness_score",
        "effective_weight",
    }
)

_MAX_LIST_THEMES = 100
_MAX_THEME_THESES = 20
_MAX_THEME_ENTITIES = 50
_MAX_ATOMS = 20
_MAX_INDICATORS = 10
_MAX_EVENTS = 10
_MAX_THESIS_CHILDREN = 10
_MAX_EXPOSURES = 20
_MAX_REVIEW = 20
_MAX_DELTAS = 20
_MAX_TIMELINE = 10
_MAX_HOLDINGS = 100
_MAX_EVIDENCE = 50
_EXCERPT_LIMIT = 500


def _bounded(value: Any, default: int, maximum: int) -> int:
    try:
        return max(1, min(maximum, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _rows(result: Any) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in result.mappings().all()]
    except AttributeError:
        return [dict(row) for row in result]


def _first(result: Any) -> dict[str, Any] | None:
    try:
        row = result.mappings().first()
    except AttributeError:
        row = result.first()
    return dict(row) if row is not None else None


def _uuid(value: Any, field: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"invalid {field}") from None


def _text(value: Any, maximum: int) -> str | None:
    text_value = str(value or "").strip()
    return text_value[:maximum] if text_value else None


def _text_required(value: Any, maximum: int, field: str) -> str:
    result = _text(value, maximum)
    if not result:
        raise ValueError(f"{field} is required")
    return result


def _excerpt(value: Any) -> str | None:
    text_value = str(value or "").strip()
    return text_value[:_EXCERPT_LIMIT] if text_value else None


def _json_list(value: Any, field: str, maximum: int) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be a list")
    if len(value) > maximum:
        raise ValueError(f"{field} has too many items")
    return list(value)


def _string_list(value: Any, field: str, maximum: int, entry_limit: int) -> list[str]:
    items = _json_list(value, field, maximum)
    cleaned: list[str] = []
    for item in items:
        entry = _text(item, entry_limit)
        if entry:
            cleaned.append(entry)
    return cleaned


def _confidence(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("invalid confidence")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("invalid confidence") from None
    if not math.isfinite(result) or not (0.0 <= result <= 1.0):
        raise ValueError("invalid confidence")
    return result


def _weight(value: Any) -> float:
    """Clamp a numeric portfolio weight into the [0, 1] band."""
    if isinstance(value, bool):
        raise ValueError("invalid weight")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("invalid weight") from None
    if not math.isfinite(result):
        raise ValueError("invalid weight")
    return max(0.0, min(1.0, result))


def _timestamp(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"invalid {field}") from None
    raise ValueError(f"invalid {field}")


def _theme_exists(session: Any, theme_id: str) -> bool:
    row = _first(
        session.execute(
            text(
                "SELECT 1 AS present FROM investment_themes "
                "WHERE id = CAST(:id AS UUID) LIMIT 1"
            ),
            {"id": theme_id},
        )
    )
    return row is not None


def _thesis_exists(session: Any, thesis_id: str) -> bool:
    row = _first(
        session.execute(
            text(
                "SELECT 1 AS present FROM investment_theses "
                "WHERE id = CAST(:id AS UUID) LIMIT 1"
            ),
            {"id": thesis_id},
        )
    )
    return row is not None


def create_theme(
    session: Any,
    *,
    name: str,
    definition: str,
    horizon: str = "multi_year",
    macro_drivers: list[str] | None = None,
    key_indicators: list[str] | None = None,
    invalidation_conditions: list[Any] | None = None,
    confidence: float | None = None,
) -> str:
    """Create one theme and return its id; duplicate names raise ValueError."""
    if macro_drivers is None:
        macro_drivers = []
    if key_indicators is None:
        key_indicators = []
    if invalidation_conditions is None:
        invalidation_conditions = []
    theme_name = _text_required(name, 200, "name")
    theme_definition = _text_required(definition, 5000, "definition")
    theme_horizon = _text(horizon, 50) or "multi_year"
    drivers = _string_list(macro_drivers, "macro_drivers", 50, 200)
    indicators = _string_list(key_indicators, "key_indicators", 50, 200)
    conditions = _json_list(invalidation_conditions, "invalidation_conditions", 200)
    theme_confidence = _confidence(confidence)
    existing = _first(
        session.execute(
            text(
                "SELECT 1 AS present FROM investment_themes WHERE name = :name LIMIT 1"
            ),
            {"name": theme_name},
        )
    )
    if existing is not None:
        raise ValueError("duplicate theme name")
    row = _first(
        session.execute(
            text(
                """INSERT INTO investment_themes
                   (name, definition, horizon, macro_drivers, key_indicators,
                    invalidation_conditions, confidence)
                   VALUES (:name, :definition, :horizon, :macro_drivers,
                           :key_indicators, CAST(:invalidation_conditions AS JSONB),
                           :confidence)
                   RETURNING id"""
            ),
            {
                "name": theme_name,
                "definition": theme_definition,
                "horizon": theme_horizon,
                "macro_drivers": drivers,
                "key_indicators": indicators,
                "invalidation_conditions": json.dumps(conditions),
                "confidence": theme_confidence,
            },
        )
    )
    return str(row["id"])


def attach_theme_entities(
    session: Any,
    theme_id: str,
    entities: list[Mapping[str, Any]] | None = None,
) -> int:
    """Attach entities idempotently; unknown theme ids raise ValueError."""
    if entities is None:
        entities = []
    theme_id = _uuid(theme_id, "theme_id")
    rows: list[dict[str, Any]] = []
    for item in _json_list(entities, "entities", _MAX_THEME_ENTITIES):
        if not isinstance(item, Mapping):
            raise ValueError("invalid entity")
        entity_type = str(item.get("entity_type") or "").strip().lower()
        if entity_type not in ENTITY_TYPES:
            raise ValueError(f"unsupported entity_type:{entity_type[:32]}")
        entity_id = _text_required(item.get("entity_id"), 200, "entity_id")
        rows.append(
            {
                "theme_id": theme_id,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "display_name": _text(item.get("display_name"), 200),
            }
        )
    if not rows:
        return 0
    if not _theme_exists(session, theme_id):
        raise ValueError("unknown theme")
    session.execute(
        text(
            """INSERT INTO investment_theme_entities
               (theme_id, entity_type, entity_id, display_name)
               VALUES (:theme_id, :entity_type, :entity_id, :display_name)
               ON CONFLICT (theme_id, entity_type, entity_id) DO NOTHING"""
        ),
        rows,
    )
    return len(rows)


def create_thesis(
    session: Any,
    *,
    theme_id: str,
    company: str | None = None,
    symbol: str | None = None,
    claim: str,
    variant_perception: str | None = None,
    horizon: str | None = None,
    confidence: float | None = None,
    invalidation_conditions: list[Any] | None = None,
    rationale: str | None = None,
) -> str:
    """Create a thesis and its initial version row (version 1)."""
    if invalidation_conditions is None:
        invalidation_conditions = []
    theme_id = _uuid(theme_id, "theme_id")
    if not _theme_exists(session, theme_id):
        raise ValueError("unknown theme")
    claim_text = _text_required(claim, 5000, "claim")
    conditions = _json_list(invalidation_conditions, "invalidation_conditions", 200)
    row = _first(
        session.execute(
            text(
                """INSERT INTO investment_theses
                   (theme_id, company, symbol, claim, variant_perception, horizon,
                    confidence, invalidation_conditions)
                   VALUES (:theme_id, :company, :symbol, :claim,
                           :variant_perception, :horizon, :confidence,
                           CAST(:invalidation_conditions AS JSONB))
                   RETURNING id"""
            ),
            {
                "theme_id": theme_id,
                "company": _text(company, 200),
                "symbol": _text(symbol, 20),
                "claim": claim_text,
                "variant_perception": _text(variant_perception, 2000),
                "horizon": _text(horizon, 50),
                "confidence": _confidence(confidence),
                "invalidation_conditions": json.dumps(conditions),
            },
        )
    )
    thesis_id = str(row["id"])
    session.execute(
        text(
            """INSERT INTO investment_thesis_versions
               (thesis_id, version, claim, variant_perception, confidence, rationale)
               VALUES (CAST(:thesis_id AS UUID), 1, :claim, :variant_perception,
                       :confidence, :rationale)"""
        ),
        {
            "thesis_id": thesis_id,
            "claim": claim_text,
            "variant_perception": _text(variant_perception, 2000),
            "confidence": _confidence(confidence),
            "rationale": _text(rationale, 5000),
        },
    )
    return thesis_id


def revise_thesis(
    session: Any,
    thesis_id: str,
    *,
    claim: str,
    variant_perception: str | None = None,
    confidence: float | None = None,
    rationale: str,
    changed_by: str = "operator",
) -> int:
    """Bump the version counter and update the thesis row (status preserved)."""
    thesis_id = _uuid(thesis_id, "thesis_id")
    existing = _first(
        session.execute(
            text(
                "SELECT status FROM investment_theses "
                "WHERE id = CAST(:id AS UUID) LIMIT 1"
            ),
            {"id": thesis_id},
        )
    )
    if existing is None:
        raise ValueError("unknown thesis")
    claim_text = _text_required(claim, 5000, "claim")
    variant = _text(variant_perception, 2000)
    thesis_confidence = _confidence(confidence)
    rationale_text = _text_required(rationale, 5000, "rationale")
    changed_by_text = _text(changed_by, 200) or "operator"
    version_row = _first(
        session.execute(
            text(
                "SELECT COALESCE(MAX(version), 0) AS max_version "
                "FROM investment_thesis_versions "
                "WHERE thesis_id = CAST(:id AS UUID)"
            ),
            {"id": thesis_id},
        )
    )
    next_version = int(version_row["max_version"]) + 1
    session.execute(
        text(
            """UPDATE investment_theses
               SET claim = :claim, variant_perception = :variant_perception,
                   confidence = :confidence, updated_at = NOW()
               WHERE id = CAST(:id AS UUID)"""
        ),
        {
            "id": thesis_id,
            "claim": claim_text,
            "variant_perception": variant,
            "confidence": thesis_confidence,
        },
    )
    session.execute(
        text(
            """INSERT INTO investment_thesis_versions
               (thesis_id, version, claim, variant_perception, confidence,
                rationale, changed_by)
               VALUES (CAST(:thesis_id AS UUID), :version, :claim,
                       :variant_perception, :confidence, :rationale, :changed_by)"""
        ),
        {
            "thesis_id": thesis_id,
            "version": next_version,
            "claim": claim_text,
            "variant_perception": variant,
            "confidence": thesis_confidence,
            "rationale": rationale_text,
            "changed_by": changed_by_text,
        },
    )
    return next_version


def add_thesis_evidence(
    session: Any,
    thesis_id: str,
    evidence: list[Mapping[str, Any]] | None = None,
) -> int:
    if evidence is None:
        evidence = []
    # Narrow compatibility delegation: desk evidence (provenance, fingerprint,
    # or weight metadata) is handled by the thesis-fusion repository, which
    # owns the desk columns on investment_thesis_evidence.
    if any(
        isinstance(item, Mapping) and _DESK_EVIDENCE_KEYS & item.keys()
        for item in evidence
    ):
        from thesis_fusion import attach_evidence

        result = attach_evidence(session, thesis_id, evidence=list(evidence))
        return int(result.get("attached") or 0)
    """Attach evidence rows; unknown types or relationships raise ValueError
    before any database access or insert."""
    thesis_id = _uuid(thesis_id, "thesis_id")
    items = _json_list(evidence, "evidence", _MAX_EVIDENCE)
    errors: list[str] = []
    resolved: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            errors.append("invalid evidence row")
            continue
        evidence_type = str(item.get("evidence_type") or "").strip().lower()
        relationship = str(item.get("relationship") or "").strip().lower()
        if evidence_type not in EVIDENCE_TYPES:
            errors.append(f"unsupported_evidence_type:{evidence_type[:32]}")
        if relationship not in RELATIONSHIPS:
            errors.append(f"unsupported_relationship:{relationship[:32]}")
        if errors:
            continue
        evidence_id = _text_required(item.get("evidence_id"), 200, "evidence_id")
        if evidence_type == "atom":
            _uuid(evidence_id, "atom evidence_id")
        resolved.append(
            {
                "thesis_id": thesis_id,
                "evidence_type": evidence_type,
                "evidence_id": evidence_id,
                "relationship": relationship,
                "excerpt": _excerpt(item.get("excerpt")),
            }
        )
    if errors:
        raise ValueError("; ".join(errors))
    if not _thesis_exists(session, thesis_id):
        raise ValueError("unknown thesis")
    if not resolved:
        return 0
    session.execute(
        text(
            """INSERT INTO investment_thesis_evidence
               (thesis_id, evidence_type, evidence_id, relationship, excerpt)
               VALUES (CAST(:thesis_id AS UUID), :evidence_type, :evidence_id,
                       :relationship, :excerpt)
               ON CONFLICT (thesis_id, evidence_type, evidence_id, relationship)
               DO NOTHING"""
        ),
        resolved,
    )
    return len(resolved)


def add_catalyst(
    session: Any,
    thesis_id: str,
    *,
    description: str,
    expected_at: datetime | str | None = None,
    state: str = "pending",
) -> str:
    """Add one catalyst to a thesis; bounded enum-validated insert."""
    thesis_id = _uuid(thesis_id, "thesis_id")
    if state not in CATALYST_STATES:
        raise ValueError(f"unsupported catalyst state:{str(state)[:32]}")
    description_text = _text_required(description, 2000, "description")
    expected = _timestamp(expected_at, "expected_at")
    if not _thesis_exists(session, thesis_id):
        raise ValueError("unknown thesis")
    row = _first(
        session.execute(
            text(
                """INSERT INTO investment_catalysts
                   (thesis_id, description, expected_at, state)
                   VALUES (CAST(:thesis_id AS UUID), :description, :expected_at,
                           :state)
                   RETURNING id"""
            ),
            {
                "thesis_id": thesis_id,
                "description": description_text,
                "expected_at": expected,
                "state": state,
            },
        )
    )
    return str(row["id"])


def add_risk(
    session: Any,
    thesis_id: str,
    *,
    description: str,
    kind: str = "counter_thesis",
    severity: str = "moderate",
) -> str:
    """Add one risk to a thesis; bounded enum-validated insert."""
    thesis_id = _uuid(thesis_id, "thesis_id")
    if kind not in RISK_KINDS:
        raise ValueError(f"unsupported risk kind:{str(kind)[:32]}")
    if severity not in RISK_SEVERITIES:
        raise ValueError(f"unsupported risk severity:{str(severity)[:32]}")
    description_text = _text_required(description, 2000, "description")
    if not _thesis_exists(session, thesis_id):
        raise ValueError("unknown thesis")
    row = _first(
        session.execute(
            text(
                """INSERT INTO investment_risks
                   (thesis_id, description, kind, severity)
                   VALUES (CAST(:thesis_id AS UUID), :description, :kind, :severity)
                   RETURNING id"""
            ),
            {
                "thesis_id": thesis_id,
                "description": description_text,
                "kind": kind,
                "severity": severity,
            },
        )
    )
    return str(row["id"])


def add_watch_item(
    session: Any,
    thesis_id: str,
    *,
    label: str,
    source_kind: str | None = None,
    source_id: str | None = None,
) -> str:
    """Add one watch item to a thesis; bounded insert."""
    thesis_id = _uuid(thesis_id, "thesis_id")
    label_text = _text_required(label, 200, "label")
    source_kind_text = _text(source_kind, 100)
    source_id_text = _text(source_id, 200)
    if not _thesis_exists(session, thesis_id):
        raise ValueError("unknown thesis")
    row = _first(
        session.execute(
            text(
                """INSERT INTO investment_watch_items
                   (thesis_id, label, source_kind, source_id)
                   VALUES (CAST(:thesis_id AS UUID), :label, :source_kind,
                           :source_id)
                   RETURNING id"""
            ),
            {
                "thesis_id": thesis_id,
                "label": label_text,
                "source_kind": source_kind_text,
                "source_id": source_id_text,
            },
        )
    )
    return str(row["id"])


def set_thesis_status(session: Any, thesis_id: str, status: str) -> None:
    """Set a thesis status; unknown statuses or theses raise ValueError."""
    thesis_id = _uuid(thesis_id, "thesis_id")
    if status not in THESIS_STATUSES:
        raise ValueError(f"unsupported thesis status:{str(status)[:32]}")
    if not _thesis_exists(session, thesis_id):
        raise ValueError("unknown thesis")
    session.execute(
        text(
            "UPDATE investment_theses SET status = :status, updated_at = NOW() "
            "WHERE id = CAST(:id AS UUID)"
        ),
        {"status": status, "id": thesis_id},
    )


def set_theme_status(session: Any, theme_id: str, status: str) -> None:
    """Set a theme status; unknown statuses or themes raise ValueError."""
    theme_id = _uuid(theme_id, "theme_id")
    if status not in THEME_STATUSES:
        raise ValueError(f"unsupported theme status:{str(status)[:32]}")
    if not _theme_exists(session, theme_id):
        raise ValueError("unknown theme")
    session.execute(
        text(
            "UPDATE investment_themes SET status = :status, updated_at = NOW() "
            "WHERE id = CAST(:id AS UUID)"
        ),
        {"status": status, "id": theme_id},
    )


def upsert_holdings(
    session: Any,
    holdings: list[Mapping[str, Any]] | None = None,
) -> int:
    """Upsert portfolio holdings keyed by symbol; weights are clamped to [0, 1]."""
    if holdings is None:
        holdings = []
    rows: list[dict[str, Any]] = []
    for item in _json_list(holdings, "holdings", _MAX_HOLDINGS):
        if not isinstance(item, Mapping):
            raise ValueError("invalid holding")
        symbol = _text_required(item.get("symbol"), 20, "symbol")
        source = str(item.get("source") or "manual").strip().lower()
        if source not in HOLDING_SOURCES:
            raise ValueError(f"unsupported source:{source[:32]}")
        rows.append(
            {
                "symbol": symbol,
                "company": _text(item.get("company"), 200),
                "sector": _text(item.get("sector"), 100),
                "country": _text(item.get("country"), 100),
                "currency": _text(item.get("currency"), 10),
                "weight": _weight(item.get("weight", 0.0)),
                "theme_tags": _string_list(
                    item.get("theme_tags"), "theme_tags", 50, 100
                ),
                "rate_sensitivity": _text(item.get("rate_sensitivity"), 100),
                "commodity_sensitivity": _text(item.get("commodity_sensitivity"), 100),
                "source": source,
            }
        )
    if not rows:
        return 0
    session.execute(
        text(
            """INSERT INTO portfolio_holdings
               (symbol, company, sector, country, currency, weight, theme_tags,
                rate_sensitivity, commodity_sensitivity, source)
               VALUES (:symbol, :company, :sector, :country, :currency, :weight,
                       :theme_tags, :rate_sensitivity, :commodity_sensitivity,
                       :source)
               ON CONFLICT (symbol) DO UPDATE SET
                   company = EXCLUDED.company, sector = EXCLUDED.sector,
                   country = EXCLUDED.country, currency = EXCLUDED.currency,
                   weight = EXCLUDED.weight, theme_tags = EXCLUDED.theme_tags,
                   rate_sensitivity = EXCLUDED.rate_sensitivity,
                   commodity_sensitivity = EXCLUDED.commodity_sensitivity,
                   source = EXCLUDED.source, updated_at = NOW()"""
        ),
        rows,
    )
    return len(rows)


def portfolio_context(session: Any) -> dict[str, Any]:
    """Bounded portfolio aggregation: exposures, catalysts, review schedule."""

    def _exposure(column: str) -> list[dict[str, Any]]:
        return _rows(
            session.execute(
                text(
                    f"SELECT {column} AS bucket, SUM(weight) AS exposure, "
                    "COUNT(*) AS holdings FROM portfolio_holdings "
                    "WHERE weight > 0 GROUP BY bucket "
                    "ORDER BY exposure DESC, bucket LIMIT :limit"
                ),
                {"limit": _MAX_EXPOSURES},
            )
        )

    total = _first(
        session.execute(
            text(
                "SELECT COALESCE(SUM(weight), 0) AS total FROM portfolio_holdings "
                "LIMIT 1"
            ),
            {},
        )
    )
    themes = _rows(
        session.execute(
            text(
                """SELECT t.name AS theme, SUM(h.weight) AS exposure,
                          COUNT(*) AS holdings
                   FROM portfolio_holdings h
                   JOIN LATERAL unnest(h.theme_tags) AS tags(tag) ON TRUE
                   JOIN investment_themes t ON t.name = tags.tag
                   WHERE h.weight > 0
                   GROUP BY t.name
                   ORDER BY exposure DESC, t.name
                   LIMIT :limit"""
            ),
            {"limit": _MAX_EXPOSURES},
        )
    )
    catalysts = _rows(
        session.execute(
            text(
                """SELECT c.id, c.description, c.expected_at, c.state,
                          th.theme_id, th.company, th.symbol
                   FROM investment_catalysts c
                   JOIN investment_theses th ON th.id = c.thesis_id
                   WHERE c.state = 'pending'
                   ORDER BY c.expected_at NULLS LAST, c.created_at DESC, c.id
                   LIMIT :limit"""
            ),
            {"limit": _MAX_REVIEW},
        )
    )
    review_schedule = _rows(
        session.execute(
            text(
                """SELECT 'thesis' AS kind, id, claim AS title, review_at, status,
                          created_at
                   FROM investment_theses WHERE review_at IS NOT NULL
                   UNION ALL
                   SELECT 'theme' AS kind, id, name AS title, review_at, status,
                          created_at
                   FROM investment_themes WHERE review_at IS NOT NULL
                   ORDER BY review_at NULLS LAST, created_at DESC, kind, id
                   LIMIT :limit"""
            ),
            {"limit": _MAX_REVIEW},
        )
    )
    return {
        "total_weight": total["total"] if total else 0.0,
        "sectors": _exposure("sector"),
        "countries": _exposure("country"),
        "currencies": _exposure("currency"),
        "themes": themes,
        "catalysts": catalysts,
        "review_schedule": review_schedule,
    }


def list_themes(session: Any, limit: int = 50) -> list[dict[str, Any]]:
    """Bounded theme list with entity and active-thesis counts."""
    bounded = _bounded(limit, 50, _MAX_LIST_THEMES)
    return _rows(
        session.execute(
            text(
                """SELECT t.id, t.name, t.definition, t.horizon, t.status,
                          t.review_at, t.confidence, t.created_at, t.updated_at,
                          (SELECT COUNT(*) FROM investment_theme_entities e
                           WHERE e.theme_id = t.id) AS entity_count,
                          (SELECT COUNT(*) FROM investment_theses th
                           WHERE th.theme_id = t.id AND th.status = 'active')
                              AS active_thesis_count
                   FROM investment_themes t
                   ORDER BY t.created_at DESC, t.name
                   LIMIT :limit"""
            ),
            {"limit": bounded},
        )
    )


def _entity_event_filters(
    entities: Sequence[Mapping[str, Any]],
) -> tuple[list[str], list[str]]:
    values: list[str] = []
    for entity in entities:
        entity_id = str(entity.get("entity_id") or "").strip()
        if entity_id:
            values.append(entity_id)
        display = str(entity.get("display_name") or "").strip()
        if display:
            values.append(display)
    countries = sorted(
        {value.upper() for value in values if len(value) == 2 and value.isalpha()}
    )
    keywords = [f"%{value.lower()}%" for value in values][: _MAX_EVENTS * 2]
    return countries, keywords


def get_theme(session: Any, theme_id: str) -> dict[str, Any] | None:
    """Theme detail: entities, theses, atoms, indicators, and upcoming events."""
    theme_id = _uuid(theme_id, "theme_id")
    theme = _first(
        session.execute(
            text(
                """SELECT id, name, definition, horizon, macro_drivers,
                          key_indicators, status, review_at,
                          invalidation_conditions, confidence,
                          confidence_components, created_at, updated_at
                   FROM investment_themes
                   WHERE id = CAST(:id AS UUID) LIMIT 1"""
            ),
            {"id": theme_id},
        )
    )
    if theme is None:
        return None
    entities = _rows(
        session.execute(
            text(
                """SELECT entity_type, entity_id, display_name, created_at
                   FROM investment_theme_entities
                   WHERE theme_id = CAST(:id AS UUID)
                   ORDER BY entity_type, entity_id
                   LIMIT :limit"""
            ),
            {"id": theme_id, "limit": _MAX_THEME_ENTITIES},
        )
    )
    theses = _rows(
        session.execute(
            text(
                """SELECT id, theme_id, company, symbol, claim, variant_perception,
                          status, horizon, review_at, confidence, created_at,
                          updated_at
                   FROM investment_theses
                   WHERE theme_id = CAST(:id AS UUID)
                   ORDER BY created_at DESC, id
                   LIMIT :limit"""
            ),
            {"id": theme_id, "limit": _MAX_THEME_THESES},
        )
    )
    for thesis in theses:
        thesis_id = str(thesis["id"])
        thesis["latest_version"] = _first(
            session.execute(
                text(
                    """SELECT version, claim, variant_perception, confidence,
                              rationale, changed_by, created_at
                       FROM investment_thesis_versions
                       WHERE thesis_id = CAST(:id AS UUID)
                       ORDER BY version DESC LIMIT 1"""
                ),
                {"id": thesis_id},
            )
        )
        thesis["evidence_counts"] = _rows(
            session.execute(
                text(
                    """SELECT relationship, COUNT(*) AS count
                       FROM investment_thesis_evidence
                       WHERE thesis_id = CAST(:id AS UUID)
                       GROUP BY relationship ORDER BY relationship LIMIT 10"""
                ),
                {"id": thesis_id},
            )
        )
        thesis["catalysts"] = _rows(
            session.execute(
                text(
                    """SELECT id, description, expected_at, state, created_at
                       FROM investment_catalysts
                       WHERE thesis_id = CAST(:id AS UUID)
                       ORDER BY expected_at NULLS LAST, created_at DESC, id
                       LIMIT :limit"""
                ),
                {"id": thesis_id, "limit": _MAX_THESIS_CHILDREN},
            )
        )
        thesis["risks"] = _rows(
            session.execute(
                text(
                    """SELECT id, description, kind, severity, created_at
                       FROM investment_risks
                       WHERE thesis_id = CAST(:id AS UUID)
                       ORDER BY created_at DESC, id
                       LIMIT :limit"""
                ),
                {"id": thesis_id, "limit": _MAX_THESIS_CHILDREN},
            )
        )
    theme["entities"] = entities
    theme["theses"] = theses
    theme["atoms"] = _rows(
        session.execute(
            text(
                """SELECT DISTINCT a.id, a.claim, a.confidence, a.status,
                          a.valid_from, v.thesis_id, v.relationship
                   FROM investment_thesis_evidence v
                   JOIN investment_theses th ON th.id = v.thesis_id
                   JOIN analysis_atoms a ON a.id::text = v.evidence_id
                   WHERE th.theme_id = CAST(:id AS UUID)
                     AND v.evidence_type = 'atom'
                     AND v.relationship IN ('supports', 'contradicts')
                   ORDER BY a.valid_from DESC, a.id
                   LIMIT :limit"""
            ),
            {"id": theme_id, "limit": _MAX_ATOMS},
        )
    )
    indicator_ids = [str(value)[:64] for value in (theme.get("key_indicators") or [])][
        :_MAX_INDICATORS
    ]
    theme["key_indicator_values"] = []
    if indicator_ids:
        theme["key_indicator_values"] = _rows(
            session.execute(
                text(
                    """SELECT DISTINCT ON (series_id) series_id, observed_at,
                              value, released_at
                       FROM macro_series
                       WHERE series_id = ANY(:series_ids)
                       ORDER BY series_id, observed_at DESC
                       LIMIT :limit"""
                ),
                {"series_ids": indicator_ids, "limit": _MAX_INDICATORS},
            )
        )
    countries, keywords = _entity_event_filters(entities)
    conditions = ["scheduled_at >= :now"]
    params: dict[str, Any] = {"now": datetime.now(UTC), "limit": _MAX_EVENTS}
    if countries:
        conditions.append("country = ANY(:countries)")
        params["countries"] = countries
    if keywords:
        conditions.append("event_name ILIKE ANY(:keywords)")
        params["keywords"] = keywords
    theme["upcoming_events"] = _rows(
        session.execute(
            text(
                "SELECT event_id, event_name, country, scheduled_at, impact_level, "
                "consensus, previous, actual FROM econ_events WHERE "
                + " AND ".join(conditions)
                + " ORDER BY scheduled_at, event_id LIMIT :limit"
            ),
            params,
        )
    )
    return theme


def get_dossier(session: Any, company: str) -> dict[str, Any] | None:
    """Company dossier; returns None when no profile or document exists."""
    company_name = _text_required(company, 200, "company")
    profile = _first(
        session.execute(
            text(
                """SELECT company, symbol, business_overview, segments,
                          key_operating_drivers, capital_allocation,
                          valuation_assumptions, guidance, updated_at
                   FROM company_research_profiles
                   WHERE company = :company LIMIT 1"""
            ),
            {"company": company_name},
        )
    )
    document = _first(
        session.execute(
            text(
                "SELECT document_id FROM investment_documents "
                "WHERE company = :company ORDER BY created_at DESC LIMIT 1"
            ),
            {"company": company_name},
        )
    )
    if profile is None and document is None:
        return None
    theses = _rows(
        session.execute(
            text(
                """SELECT id, theme_id, company, symbol, claim, variant_perception,
                          status, horizon, review_at, confidence, created_at,
                          updated_at
                   FROM investment_theses
                   WHERE company = :company
                   ORDER BY created_at DESC, id
                   LIMIT :limit"""
            ),
            {"company": company_name, "limit": _MAX_THEME_THESES},
        )
    )
    deltas = _rows(
        session.execute(
            text(
                """SELECT fd.id, fd.document_id, fd.category, fd.change_kind,
                          fd.section_hash, fd.previous_section_hash, fd.excerpt,
                          fd.previous_excerpt, fd.metrics, fd.created_at,
                          d.report_date, d.document_type
                   FROM investment_filing_deltas fd
                   JOIN investment_documents d ON d.document_id = fd.document_id
                   WHERE d.company = :company
                   ORDER BY fd.created_at DESC, fd.id
                   LIMIT :limit"""
            ),
            {"company": company_name, "limit": _MAX_DELTAS},
        )
    )
    for row in deltas:
        row["excerpt"] = _excerpt(row.get("excerpt"))
        row["previous_excerpt"] = _excerpt(row.get("previous_excerpt"))
    timeline = _rows(
        session.execute(
            text(
                """SELECT d.document_id, d.company, d.symbol, d.document_type,
                          d.report_date, d.created_at, d.status,
                          a.analysis_id, a.model, a.created_at AS analyzed_at
                   FROM investment_documents d
                   LEFT JOIN investment_analyses a ON a.document_id = d.document_id
                   WHERE d.company = :company
                   ORDER BY d.created_at DESC, d.document_id
                   LIMIT :limit"""
            ),
            {"company": company_name, "limit": _MAX_TIMELINE},
        )
    )
    changes = _rows(
        session.execute(
            text(
                """SELECT fd.id, fd.document_id, fd.category, fd.change_kind,
                          fd.excerpt, fd.previous_excerpt, fd.metrics,
                          fd.created_at
                   FROM investment_filing_deltas fd
                   JOIN investment_documents d ON d.document_id = fd.document_id
                   WHERE d.company = :company
                     AND fd.change_kind IN ('new', 'changed', 'removed')
                   ORDER BY fd.created_at DESC, fd.id
                   LIMIT :limit"""
            ),
            {"company": company_name, "limit": _MAX_DELTAS},
        )
    )
    for row in changes:
        row["excerpt"] = _excerpt(row.get("excerpt"))
        row["previous_excerpt"] = _excerpt(row.get("previous_excerpt"))
    return {
        "company": company_name,
        "profile": profile,
        "theses": theses,
        "filing_deltas": deltas,
        "evidence_timeline": timeline,
        "changes": changes,
    }


__all__ = [
    "add_catalyst",
    "add_risk",
    "add_thesis_evidence",
    "add_watch_item",
    "attach_theme_entities",
    "create_theme",
    "create_thesis",
    "get_dossier",
    "get_theme",
    "list_themes",
    "portfolio_context",
    "revise_thesis",
    "set_theme_status",
    "set_thesis_status",
    "upsert_holdings",
]
