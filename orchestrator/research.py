"""Long-horizon investment research workspace: themes, theses, and portfolio.

Phase 9 normalized research objects.  Every helper takes the caller's session
and never commits or rolls back; all queries are bounded and allowlisted so the
API layer can fail soft without leaking private payloads.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from contracts.db_results import result_first, result_rows

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

_MAX_LIST_THEMES = 100
_MAX_THEME_THESES = 20
_MAX_THEME_ENTITIES = 50
_MAX_ATOMS = 20
_MAX_INDICATORS = 10
_MAX_EVENTS = 10
_MAX_THESIS_CHILDREN = 10
_MAX_EXPOSURES = 20
_MAX_REVIEW = 20
_MAX_HOLDINGS = 100


def _bounded(value: Any, default: int, maximum: int) -> int:
    try:
        return max(1, min(maximum, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


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
    return [e for item in items if (e := _text(item, entry_limit))]


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


def _entity_event_filters(
    entities: Sequence[Mapping[str, Any]],
) -> tuple[list[str], list[str]]:
    values: list[str] = []
    for entity in entities:
        if entity_id := str(entity.get("entity_id") or "").strip():
            values.append(entity_id)
        if display := str(entity.get("display_name") or "").strip():
            values.append(display)
    countries = sorted({v.upper() for v in values if len(v) == 2 and v.isalpha()})
    keywords = [f"%{v.lower()}%" for v in values][: _MAX_EVENTS * 2]
    return countries, keywords


def upsert_holdings(
    session: Any, holdings: list[Mapping[str, Any]] | None = None
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
                       :theme_tags, :rate_sensitivity, :commodity_sensitivity, :source)
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
        return result_rows(
            session.execute(
                text(
                    f"SELECT {column} AS bucket, SUM(weight) AS exposure, COUNT(*) AS holdings "
                    f"FROM portfolio_holdings WHERE weight > 0 GROUP BY bucket "
                    f"ORDER BY exposure DESC, bucket LIMIT :limit"
                ),
                {"limit": _MAX_EXPOSURES},
            )
        )

    total = result_first(
        session.execute(
            text(
                "SELECT COALESCE(SUM(weight), 0) AS total FROM portfolio_holdings LIMIT 1"
            ),
            {},
        )
    )
    themes = result_rows(
        session.execute(
            text(
                """SELECT t.name AS theme, SUM(h.weight) AS exposure, COUNT(*) AS holdings
               FROM portfolio_holdings h
               JOIN LATERAL unnest(h.theme_tags) AS tags(tag) ON TRUE
               JOIN investment_themes t ON t.name = tags.tag
               WHERE h.weight > 0 GROUP BY t.name ORDER BY exposure DESC, t.name LIMIT :limit"""
            ),
            {"limit": _MAX_EXPOSURES},
        )
    )
    catalysts = result_rows(
        session.execute(
            text(
                """SELECT c.id, c.description, c.expected_at, c.state, th.theme_id, th.company, th.symbol
               FROM investment_catalysts c JOIN investment_theses th ON th.id = c.thesis_id
               WHERE c.state = 'pending' ORDER BY c.expected_at NULLS LAST, c.created_at DESC, c.id
               LIMIT :limit"""
            ),
            {"limit": _MAX_REVIEW},
        )
    )
    review_schedule = result_rows(
        session.execute(
            text(
                """SELECT 'thesis' AS kind, id, claim AS title, review_at, status, created_at
               FROM investment_theses WHERE review_at IS NOT NULL
               UNION ALL
               SELECT 'theme' AS kind, id, name AS title, review_at, status, created_at
               FROM investment_themes WHERE review_at IS NOT NULL
               ORDER BY review_at NULLS LAST, created_at DESC, kind, id LIMIT :limit"""
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
    return result_rows(
        session.execute(
            text(
                """SELECT t.id, t.name, t.definition, t.horizon, t.status, t.review_at,
                      t.confidence, t.created_at, t.updated_at,
                      (SELECT COUNT(*) FROM investment_theme_entities e WHERE e.theme_id = t.id) AS entity_count,
                      (SELECT COUNT(*) FROM investment_theses th
                       WHERE th.theme_id = t.id AND th.status = 'active') AS active_thesis_count
               FROM investment_themes t ORDER BY t.created_at DESC, t.name LIMIT :limit"""
            ),
            {"limit": bounded},
        )
    )


def get_theme(session: Any, theme_id: str) -> dict[str, Any] | None:
    """Theme detail: entities, theses, atoms, indicators, and upcoming events."""
    theme_id = _uuid(theme_id, "theme_id")
    theme = result_first(
        session.execute(
            text(
                """SELECT id, name, definition, horizon, macro_drivers, key_indicators, status,
                      review_at, invalidation_conditions, confidence, confidence_components,
                      created_at, updated_at FROM investment_themes WHERE id = CAST(:id AS UUID) LIMIT 1"""
            ),
            {"id": theme_id},
        )
    )
    if theme is None:
        return None
    entities = result_rows(
        session.execute(
            text(
                """SELECT entity_type, entity_id, display_name, created_at
               FROM investment_theme_entities WHERE theme_id = CAST(:id AS UUID)
               ORDER BY entity_type, entity_id LIMIT :limit"""
            ),
            {"id": theme_id, "limit": _MAX_THEME_ENTITIES},
        )
    )
    theses = result_rows(
        session.execute(
            text(
                """SELECT id, theme_id, company, symbol, claim, variant_perception, status,
                      horizon, review_at, confidence, created_at, updated_at
               FROM investment_theses WHERE theme_id = CAST(:id AS UUID)
               ORDER BY created_at DESC, id LIMIT :limit"""
            ),
            {"id": theme_id, "limit": _MAX_THEME_THESES},
        )
    )
    for thesis in theses:
        thesis_id = str(thesis["id"])
        thesis["latest_version"] = result_first(
            session.execute(
                text(
                    """SELECT version, claim, variant_perception, confidence, rationale, changed_by, created_at
                   FROM investment_thesis_versions WHERE thesis_id = CAST(:id AS UUID)
                   ORDER BY version DESC LIMIT 1"""
                ),
                {"id": thesis_id},
            )
        )
        thesis["evidence_counts"] = result_rows(
            session.execute(
                text(
                    """SELECT relationship, COUNT(*) AS count FROM investment_thesis_evidence
                   WHERE thesis_id = CAST(:id AS UUID) GROUP BY relationship ORDER BY relationship LIMIT 10"""
                ),
                {"id": thesis_id},
            )
        )
        thesis["catalysts"] = result_rows(
            session.execute(
                text(
                    """SELECT id, description, expected_at, state, created_at FROM investment_catalysts
                   WHERE thesis_id = CAST(:id AS UUID) ORDER BY expected_at NULLS LAST, created_at DESC, id
                   LIMIT :limit"""
                ),
                {"id": thesis_id, "limit": _MAX_THESIS_CHILDREN},
            )
        )
        thesis["risks"] = result_rows(
            session.execute(
                text(
                    """SELECT id, description, kind, severity, created_at FROM investment_risks
                   WHERE thesis_id = CAST(:id AS UUID) ORDER BY created_at DESC, id LIMIT :limit"""
                ),
                {"id": thesis_id, "limit": _MAX_THESIS_CHILDREN},
            )
        )
    theme["entities"] = entities
    theme["theses"] = theses
    theme["atoms"] = result_rows(
        session.execute(
            text(
                """SELECT DISTINCT a.id, a.claim, a.confidence, a.status, a.valid_from, v.thesis_id, v.relationship
               FROM investment_thesis_evidence v
               JOIN investment_theses th ON th.id = v.thesis_id
               JOIN analysis_atoms a ON a.id::text = v.evidence_id
               WHERE th.theme_id = CAST(:id AS UUID) AND v.evidence_type = 'atom'
                 AND v.relationship IN ('supports', 'contradicts')
               ORDER BY a.valid_from DESC, a.id LIMIT :limit"""
            ),
            {"id": theme_id, "limit": _MAX_ATOMS},
        )
    )
    indicator_ids = [str(v)[:64] for v in (theme.get("key_indicators") or [])][
        :_MAX_INDICATORS
    ]
    theme["key_indicator_values"] = []
    if indicator_ids:
        theme["key_indicator_values"] = result_rows(
            session.execute(
                text(
                    """SELECT DISTINCT ON (series_id) series_id, observed_at, value, released_at
                   FROM macro_series WHERE series_id = ANY(:series_ids)
                   ORDER BY series_id, observed_at DESC LIMIT :limit"""
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
    theme["upcoming_events"] = result_rows(
        session.execute(
            text(
                f"SELECT event_id, event_name, country, scheduled_at, impact_level, "
                f"consensus, previous, actual FROM econ_events WHERE {' AND '.join(conditions)} "
                f"ORDER BY scheduled_at, event_id LIMIT :limit"
            ),
            params,
        )
    )
    return theme


__all__ = [
    "CATALYST_STATES",
    "ENTITY_TYPES",
    "EVIDENCE_TYPES",
    "HOLDING_SOURCES",
    "RELATIONSHIPS",
    "RISK_KINDS",
    "RISK_SEVERITIES",
    "THEME_STATUSES",
    "THESIS_STATUSES",
    "get_theme",
    "list_themes",
    "portfolio_context",
    "upsert_holdings",
]
