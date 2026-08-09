"""Bounded adapters from source-owned tables to normalized research evidence."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import text

from research_intelligence.context import ResearchContext
from research_intelligence.contracts import (
    EvidenceType,
    NormalizedEntity,
    NormalizedEvidence,
)
from research_intelligence.relationships import normalize_entity


class EvidenceAdapter(Protocol):
    name: str

    def collect(self, session: Any, *, since: datetime, until: datetime | None = None, limit: int) -> list[NormalizedEvidence]:
        ...


@dataclass(frozen=True, slots=True)
class EvidenceCollection:
    items: tuple[NormalizedEvidence, ...]
    failures: Mapping[str, str]


def _rows(result: Any) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in result.mappings().all()]
    except (AttributeError, TypeError):
        try:
            return [dict(row._mapping) for row in result]
        except (AttributeError, TypeError):
            return []


def _execute(session: Any, statement: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _rows(session.execute(text(statement), dict(params)))


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _timestamp(value: Any, default: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = default or datetime.now(UTC)
    else:
        parsed = default or datetime.now(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)

def _latest_timestamp(*values: Any, default: datetime) -> datetime:
    timestamps = [_timestamp(value) for value in values if value is not None]
    return max(timestamps, default=default)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _entities(*values: NormalizedEntity | None) -> tuple[NormalizedEntity, ...]:
    output: list[NormalizedEntity] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        if value is None:
            continue
        key = (value.entity_type, value.normalized_key)
        if key not in seen:
            seen.add(key)
            output.append(value)
    return tuple(output)


def _entity(kind: str, value: Any) -> NormalizedEntity | None:
    if value is None or not str(value).strip():
        return None
    try:
        return normalize_entity(kind, value)
    except ValueError:
        return None


def _json_entities(value: Any) -> tuple[NormalizedEntity, ...]:
    output: list[NormalizedEntity] = []
    for item in _list(value)[:50]:
        if isinstance(item, Mapping):
            name = item.get("name") or item.get("display_name") or item.get("id")
            kind = item.get("type") or item.get("entity_type") or "concept"
        else:
            name, kind = item, "concept"
        try:
            entity = normalize_entity(kind, name)
        except ValueError:
            continue
        if entity not in output:
            output.append(entity)
    return tuple(output)


def _freshness(source_timestamp: datetime, since: datetime) -> str:
    return "current" if source_timestamp >= since else "stale"


_INSTITUTION_REGIONS = {
    "fed": "US",
    "ecb": "euro_area",
    "boe": "UK",
    "boj": "Japan",
}

class MacroObservationAdapter:
    name = "macro_observations"

    def collect(self, session: Any, *, since: datetime, until: datetime | None = None, limit: int) -> list[NormalizedEvidence]:
        rows = _execute(
            session,
            """
            SELECT m.series_id, m.observed_at, m.value, m.source, m.released_at,
                   m.revision_at, m.metadata, m.created_at, m.updated_at,
                   md.title, md.units, md.frequency
            FROM macro_series AS m
            LEFT JOIN macro_series_metadata AS md ON md.series_id = m.series_id
            WHERE COALESCE(m.revision_at, m.released_at, m.observed_at) >= :since
              AND (
                  :until IS NULL OR
                  COALESCE(m.revision_at, m.released_at, m.observed_at) <= :until
              )
              AND (:until IS NULL OR m.created_at <= :until)
              AND (:until IS NULL OR m.updated_at <= :until)
            ORDER BY COALESCE(m.revision_at, m.released_at, m.observed_at) DESC,
                     m.series_id
            LIMIT :limit
            """,
            {"since": since, "until": until, "limit": limit},
        )
        evidence: list[NormalizedEvidence] = []
        for row in rows:
            observed = _timestamp(row.get("released_at") or row.get("observed_at"), since)
            metadata = _mapping(row.get("metadata"))
            title = row.get("title") or row.get("series_id")
            value = _finite(row.get("value"))
            unit = row.get("units")
            excerpt = f"{title}: {value:g}{f' {unit}' if unit else ''}" if value is not None else f"{title}: value unavailable"
            series = str(row.get("series_id"))
            evidence.append(
                NormalizedEvidence.create(
                    evidence_type=EvidenceType.MACRO_OBSERVATION,
                    evidence_id=f"{series}@{_timestamp(row.get('observed_at'), observed).isoformat()}",
                    source_name=row.get("source") or "macro",
                    source_timestamp=observed,
                    acquired_at=row.get("created_at"),
                    available_at=_latest_timestamp(
                        row.get("revision_at"),
                        row.get("released_at"),
                        observed,
                        row.get("created_at"),
                        row.get("updated_at"),
                        default=observed,
                    ),
                    availability_basis="source_and_local_vintage_timestamps",
                    title=title,
                    bounded_excerpt=excerpt,
                    entities=_entities(
                        _entity("concept", series),
                        _entity("macro_region", metadata.get("region")),
                    ),
                    structured_fields={
                        "series_id": series,
                        "value": value,
                        "unit": unit,
                        "frequency": row.get("frequency"),
                        "observed_at": row.get("observed_at"),
                        "released_at": row.get("released_at"),
                        "revision_at": row.get("revision_at"),
                    },
                    provenance={
                        "adapter": self.name,
                        "metadata": metadata,
                        "revision_at": row.get("revision_at"),
                        "source_updated_at": row.get("updated_at"),
                    },
                    freshness=_freshness(observed, since),
                )
            )
        return evidence



class OfficialDocumentAdapter:
    """Expose bounded official communications without copying source-owned rows."""

    name = "official_documents"

    def collect(self, session: Any, *, since: datetime, until: datetime | None = None, limit: int) -> list[NormalizedEvidence]:
        rows = _execute(
            session,
            """
            SELECT document_id, source, institution, document_type, title,
                   published_at, url, content, metadata, created_at
            FROM source_documents
            WHERE published_at >= :since
              AND (:until IS NULL OR published_at <= :until)
              AND (:until IS NULL OR created_at <= :until)
            ORDER BY published_at DESC, document_id
            LIMIT :limit
            """,
            {"since": since, "until": until, "limit": limit},
        )
        evidence: list[NormalizedEvidence] = []
        for row in rows:
            observed = _timestamp(row.get("published_at"), since)
            institution = str(row.get("institution") or "").strip().casefold()
            evidence.append(
                NormalizedEvidence.create(
                    evidence_type=EvidenceType.OFFICIAL_DOCUMENT,
                    evidence_id=str(row.get("document_id")),
                    source_name=institution or row.get("source") or "official",
                    source_timestamp=observed,
                    acquired_at=row.get("created_at"),
                    available_at=_latest_timestamp(
                        observed, row.get("created_at"), default=observed
                    ),
                    availability_basis="published_or_acquired_at",
                    title=row.get("title"),
                    bounded_excerpt=row.get("content"),
                    source_reference=row.get("url"),
                    entities=_entities(
                        _entity("concept", institution),
                        _entity("macro_region", _INSTITUTION_REGIONS.get(institution)),
                    ),
                    structured_fields={
                        "institution": institution or None,
                        "document_type": row.get("document_type"),
                    },
                    provenance={
                        "adapter": self.name,
                        "source": row.get("source"),
                        "metadata": _mapping(row.get("metadata")),
                    },
                    freshness=_freshness(observed, since),
                )
            )
        return evidence


class MacroReleaseAdapter:
    name = "macro_releases"

    def collect(self, session: Any, *, since: datetime, until: datetime | None = None, limit: int) -> list[NormalizedEvidence]:
        rows = _execute(
            session,
            """
            WITH eligible AS (
                SELECT c.*
                FROM macro_release_cards AS c
                WHERE COALESCE(c.revision_at, c.released_at, c.observed_at, c.created_at) >= :since
                  AND (
                      :until IS NULL OR
                      COALESCE(c.revision_at, c.released_at, c.observed_at, c.created_at) <= :until
                  )
                  AND (:until IS NULL OR c.created_at <= :until)
            ),
            latest AS (
                SELECT DISTINCT ON (release_identity) *
                FROM eligible
                ORDER BY release_identity,
                         COALESCE(revision_at, released_at, observed_at, created_at) DESC,
                         revision_number DESC,
                         created_at DESC
            )
            SELECT id, release_identity, series_id, event_name, actual, consensus,
                   previous, revised_previous, absolute_surprise,
                   standardized_surprise, impact, source, observed_at, released_at,
                   revision_at, revision_number, quality_flags, stage,
                   reaction_summary, created_at
            FROM latest
            ORDER BY COALESCE(revision_at, released_at, observed_at, created_at) DESC,
                     id
            LIMIT :limit
            """,
            {"since": since, "until": until, "limit": limit},
        )
        evidence: list[NormalizedEvidence] = []
        for row in rows:
            observed = _timestamp(
                row.get("released_at") or row.get("observed_at") or row.get("created_at"),
                since,
            )
            evidence.append(
                NormalizedEvidence.create(
                    evidence_type=EvidenceType.MACRO_RELEASE,
                    evidence_id=str(row.get("id")),
                    source_name=row.get("source") or "economic_calendar",
                    source_timestamp=observed,
                    acquired_at=row.get("created_at"),
                    available_at=_latest_timestamp(
                        row.get("revision_at"),
                        row.get("released_at"),
                        observed,
                        row.get("created_at"),
                        default=observed,
                    ),
                    availability_basis="release_vintage_created_at",
                    title=row.get("event_name") or row.get("release_identity"),
                    bounded_excerpt=(
                        f"Actual {row.get('actual')}; consensus {row.get('consensus')}; previous {row.get('previous')}"
                    ),
                    entities=_entities(
                        _entity("concept", row.get("series_id") or row.get("event_name"))
                    ),
                    structured_fields={
                        key: row.get(key)
                        for key in (
                            "release_identity",
                            "series_id",
                            "actual",
                            "consensus",
                            "previous",
                            "revised_previous",
                            "absolute_surprise",
                            "standardized_surprise",
                            "impact",
                            "stage",
                            "reaction_summary",
                            "revision_number",
                            "quality_flags",
                        )
                    },
                    provenance={
                        "adapter": self.name,
                        "immutable_card": True,
                        "revision_at": row.get("revision_at"),
                        "vintage_available": True,
                    },
                    freshness=_freshness(observed, since),
                )
            )
        return evidence


class MarketStateAdapter:
    name = "market_state"

    def collect(self, session: Any, *, since: datetime, until: datetime | None = None, limit: int) -> list[NormalizedEvidence]:
        rows = _execute(
            session,
            """
            SELECT symbol, as_of, source_event_id, features, unavailable, created_at
            FROM market_feature_snapshots
            WHERE as_of >= :since
              AND (:until IS NULL OR as_of <= :until)
              AND (:until IS NULL OR created_at <= :until)
            ORDER BY as_of DESC, symbol
            LIMIT :limit
            """,
            {"since": since, "until": until, "limit": limit},
        )
        return [
            NormalizedEvidence.create(
                evidence_type=EvidenceType.MARKET_STATE,
                evidence_id=str(row.get("source_event_id")),
                source_name="market_state",
                source_timestamp=_timestamp(row.get("as_of"), since),
                acquired_at=row.get("created_at"),
                available_at=_latest_timestamp(
                    row.get("as_of"), row.get("created_at"), default=since
                ),
                availability_basis="snapshot_computed_at",
                title=f"{row.get('symbol')} deterministic market state",
                bounded_excerpt="Calculated market features; see structured fields.",
                entities=_entities(_entity("market", row.get("symbol"))),
                structured_fields={
                    "symbol": row.get("symbol"),
                    "features": _mapping(row.get("features")),
                    "unavailable": _mapping(row.get("unavailable")),
                },
                provenance={"adapter": self.name, "calculation": "deterministic"},
                freshness=_freshness(_timestamp(row.get("as_of"), since), since),
            )
            for row in rows
        ]


class StoryClusterAdapter:
    name = "story_clusters"

    def collect(self, session: Any, *, since: datetime, until: datetime | None = None, limit: int) -> list[NormalizedEvidence]:
        if until is None:
            statement = """
                SELECT s.id, s.title, s.summary, s.state, s.lane, s.first_seen_at,
                       s.last_seen_at, s.last_material_change_at, s.importance,
                       s.novelty, s.confidence, s.entities, s.markets, s.source_count,
                       s.version, s.change_summary, s.clustering_reason, s.updated_at,
                       ARRAY(
                           SELECT DISTINCT m.source FROM story_cluster_members AS m
                           WHERE m.cluster_id = s.id ORDER BY m.source LIMIT 10
                       ) AS member_sources
                FROM story_clusters AS s
                WHERE s.last_seen_at >= :since
                  AND s.state NOT IN ('stale', 'closed')
                ORDER BY s.last_material_change_at DESC, s.id
                LIMIT :limit
            """
        else:
            statement = """
                WITH versions AS (
                    SELECT DISTINCT ON (v.cluster_id)
                           v.cluster_id AS id, v.snapshot, v.changed_at
                    FROM story_cluster_versions AS v
                    WHERE v.changed_at <= :until
                    ORDER BY v.cluster_id, v.version DESC
                )
                SELECT v.id, v.snapshot->>'title' AS title,
                       v.snapshot->>'summary' AS summary,
                       v.snapshot->>'state' AS state,
                       v.snapshot->>'lane' AS lane,
                       (v.snapshot->>'first_seen_at')::TIMESTAMPTZ AS first_seen_at,
                       (v.snapshot->>'last_seen_at')::TIMESTAMPTZ AS last_seen_at,
                       (v.snapshot->>'last_material_change_at')::TIMESTAMPTZ
                           AS last_material_change_at,
                       (v.snapshot->>'importance')::DOUBLE PRECISION AS importance,
                       (v.snapshot->>'novelty')::DOUBLE PRECISION AS novelty,
                       (v.snapshot->>'confidence')::DOUBLE PRECISION AS confidence,
                       COALESCE(v.snapshot->'entities', '[]'::JSONB) AS entities,
                       COALESCE(v.snapshot->'markets', '[]'::JSONB) AS markets,
                       (v.snapshot->>'source_count')::INTEGER AS source_count,
                       (v.snapshot->>'version')::INTEGER AS version,
                       v.snapshot->>'change_summary' AS change_summary,
                       COALESCE(v.snapshot->'clustering_reason', '{}'::JSONB)
                           AS clustering_reason,
                       v.changed_at AS updated_at,
                       ARRAY(
                           SELECT DISTINCT m.source FROM story_cluster_members AS m
                           WHERE m.cluster_id = v.id
                             AND m.published_at <= :until
                             AND m.created_at <= :until
                           ORDER BY m.source LIMIT 10
                       ) AS member_sources
                FROM versions AS v
                WHERE (v.snapshot->>'last_seen_at')::TIMESTAMPTZ >= :since
                  AND v.snapshot->>'state' NOT IN ('stale', 'closed')
                ORDER BY
                    (v.snapshot->>'last_material_change_at')::TIMESTAMPTZ DESC,
                    v.id
                LIMIT :limit
            """
        rows = _execute(
            session,
            statement,
            {"since": since, "until": until, "limit": limit},
        )
        evidence: list[NormalizedEvidence] = []
        for row in rows:
            observed = _timestamp(row.get("last_seen_at"), since)
            market_entities = tuple(
                entity
                for item in _list(row.get("markets"))[:20]
                if (entity := _entity("market", item.get("symbol") if isinstance(item, Mapping) else item))
            )
            evidence.append(
                NormalizedEvidence.create(
                    evidence_type=EvidenceType.STORY_CLUSTER,
                    evidence_id=str(row.get("id")),
                    source_name="canonical_story",
                    source_timestamp=observed,
                    acquired_at=row.get("updated_at"),
                    available_at=_latest_timestamp(
                        observed, row.get("updated_at"), default=observed
                    ),
                    availability_basis="story_version_changed_at",
                    valid_from=row.get("updated_at"),
                    title=row.get("title"),
                    bounded_excerpt=row.get("summary") or row.get("change_summary"),
                    entities=(*_json_entities(row.get("entities")), *market_entities),
                    structured_fields={
                        key: row.get(key)
                        for key in (
                            "state",
                            "lane",
                            "first_seen_at",
                            "last_material_change_at",
                            "importance",
                            "novelty",
                            "confidence",
                            "source_count",
                            "version",
                            "change_summary",
                        )
                    },
                    provenance={
                        "adapter": self.name,
                        "source_names": _list(row.get("member_sources")),
                        "clustering_reason": _mapping(row.get("clustering_reason")),
                    },
                    freshness=_freshness(observed, since),
                )
            )
        return evidence


class MarketConfirmationAdapter:
    name = "market_confirmations"

    def collect(self, session: Any, *, since: datetime, until: datetime | None = None, limit: int) -> list[NormalizedEvidence]:
        story_limit = max(1, limit // 2)
        story_rows = _execute(
            session,
            """
            SELECT id, cluster_id, source_event_id, market_symbol, headline_at,
                   observed_at, pre_headline_move, move_5m, move_30m, move_session,
                   flags, missing_reasons, provenance, created_at, updated_at
            FROM story_market_confirmations
            WHERE observed_at >= :since
              AND (:until IS NULL OR observed_at <= :until)
              AND (:until IS NULL OR updated_at <= :until)
            ORDER BY observed_at DESC, id
            LIMIT :limit
            """,
            {"since": since, "until": until, "limit": story_limit},
        )
        window_rows = _execute(
            session,
            """
            SELECT id, event_id, instrument_symbol, horizon, event_at, target_at,
                   observed_at, baseline_price, target_price, absolute_move,
                   percentage_move, volatility_adjusted_move, expected_direction,
                   sensitivity, direction_vs_expected, reaction_state,
                   missing_data_reason, provenance, created_at, updated_at
            FROM event_reaction_windows
            WHERE COALESCE(observed_at, target_at) >= :since
              AND (:until IS NULL OR target_at <= :until)
              AND (:until IS NULL OR COALESCE(observed_at, target_at) <= :until)
              AND (:until IS NULL OR updated_at <= :until)
            ORDER BY COALESCE(observed_at, target_at) DESC, id
            LIMIT :limit
            """,
            {"since": since, "until": until, "limit": max(1, limit - story_limit)},
        )
        evidence: list[NormalizedEvidence] = []
        for row in story_rows:
            observed = _timestamp(row.get("observed_at"), since)
            evidence.append(
                NormalizedEvidence.create(
                    evidence_type=EvidenceType.MARKET_CONFIRMATION,
                    evidence_id=f"story:{row.get('id')}",
                    source_name="market_data",
                    source_timestamp=observed,
                    acquired_at=row.get("created_at"),
                    available_at=_latest_timestamp(
                        observed, row.get("updated_at"), default=observed
                    ),
                    availability_basis="reaction_updated_at",
                    title=f"{row.get('market_symbol')} reaction to story {row.get('cluster_id')}",
                    bounded_excerpt="Descriptive market confirmation window; not an alpha score.",
                    entities=_entities(_entity("market", row.get("market_symbol"))),
                    structured_fields={
                        key: row.get(key)
                        for key in (
                            "cluster_id",
                            "source_event_id",
                            "headline_at",
                            "observed_at",
                            "pre_headline_move",
                            "move_5m",
                            "move_30m",
                            "move_session",
                            "flags",
                            "missing_reasons",
                        )
                    },
                    provenance={"adapter": self.name, **_mapping(row.get("provenance"))},
                    freshness=_freshness(observed, since),
                )
            )
        for row in window_rows:
            observed = _timestamp(row.get("observed_at") or row.get("target_at"), since)
            evidence.append(
                NormalizedEvidence.create(
                    evidence_type=EvidenceType.MARKET_CONFIRMATION,
                    evidence_id=f"release:{row.get('id')}",
                    source_name="market_data",
                    source_timestamp=observed,
                    acquired_at=row.get("created_at"),
                    available_at=_latest_timestamp(
                        observed, row.get("updated_at"), default=observed
                    ),
                    availability_basis="reaction_updated_at",
                    title=f"{row.get('instrument_symbol')} {row.get('horizon')} release reaction",
                    bounded_excerpt="Deterministic event reaction window; confirmation may be absent.",
                    entities=_entities(_entity("market", row.get("instrument_symbol"))),
                    structured_fields={
                        key: row.get(key)
                        for key in (
                            "event_id",
                            "horizon",
                            "event_at",
                            "target_at",
                            "observed_at",
                            "baseline_price",
                            "target_price",
                            "absolute_move",
                            "percentage_move",
                            "volatility_adjusted_move",
                            "expected_direction",
                            "sensitivity",
                            "direction_vs_expected",
                            "reaction_state",
                            "missing_data_reason",
                        )
                    },
                    provenance={"adapter": self.name, **_mapping(row.get("provenance"))},
                    freshness=_freshness(observed, since),
                )
            )
        return evidence[:limit]


class InvestmentObservationAdapter:
    name = "investment_observations"

    def collect(self, session: Any, *, since: datetime, until: datetime | None = None, limit: int) -> list[NormalizedEvidence]:
        rows = _execute(
            session,
            """
            SELECT observation_id, source_kind, source_id, observed_at, industry,
                   company, symbol, region, metrics, narrative, themes, score,
                   state, provenance, created_at, updated_at
            FROM investment_research_observations
            WHERE observed_at >= :since
              AND (:until IS NULL OR observed_at <= :until)
              AND (:until IS NULL OR updated_at <= :until)
            ORDER BY observed_at DESC, observation_id
            LIMIT :limit
            """,
            {"since": since, "until": until, "limit": limit},
        )
        evidence: list[NormalizedEvidence] = []
        for row in rows:
            observed = _timestamp(row.get("observed_at"), since)
            narrative = _mapping(row.get("narrative"))
            title = row.get("company") or row.get("industry") or "Investment observation"
            excerpt = narrative.get("summary") or narrative.get("thesis")
            evidence.append(
                NormalizedEvidence.create(
                    evidence_type=EvidenceType.INVESTMENT_OBSERVATION,
                    evidence_id=str(row.get("observation_id")),
                    source_name=row.get("source_kind") or "investment",
                    source_timestamp=observed,
                    acquired_at=row.get("created_at"),
                    available_at=_latest_timestamp(
                        observed, row.get("updated_at"), default=observed
                    ),
                    availability_basis="observation_updated_at",
                    title=title,
                    bounded_excerpt=excerpt,
                    entities=_entities(
                        _entity("company", row.get("company")),
                        _entity("symbol", row.get("symbol")),
                        _entity("industry", row.get("industry")),
                        _entity("macro_region", row.get("region")),
                    ),
                    structured_fields={
                        "source_id": row.get("source_id"),
                        "metrics": _mapping(row.get("metrics")),
                        "themes": _list(row.get("themes")),
                        "score": _finite(row.get("score")),
                        "state": row.get("state"),
                    },
                    provenance={"adapter": self.name, **_mapping(row.get("provenance"))},
                    freshness=_freshness(observed, since),
                )
            )
        return evidence


class FilingDeltaAdapter:
    name = "filing_deltas"

    def collect(self, session: Any, *, since: datetime, until: datetime | None = None, limit: int) -> list[NormalizedEvidence]:
        rows = _execute(
            session,
            """
            SELECT f.id, f.document_id, f.previous_document_id, f.category,
                   f.change_kind, f.section_hash, f.previous_section_hash,
                   f.excerpt, f.previous_excerpt, f.metrics, f.created_at,
                   d.company, d.symbol, d.industry, d.region, d.report_date,
                   d.source_url, d.filing_source
            FROM investment_filing_deltas AS f
            JOIN investment_documents AS d ON d.document_id = f.document_id
            WHERE f.created_at >= :since
              AND f.change_kind <> 'unchanged'
              AND (:until IS NULL OR f.created_at <= :until)
            ORDER BY f.created_at DESC, f.id
            LIMIT :limit
            """,
            {"since": since, "until": until, "limit": limit},
        )
        return [
            NormalizedEvidence.create(
                evidence_type=EvidenceType.FILING_DELTA,
                evidence_id=str(row.get("id")),
                source_name=row.get("filing_source") or "filing",
                source_timestamp=_timestamp(row.get("created_at"), since),
                acquired_at=row.get("created_at"),
                available_at=row.get("created_at"),
                availability_basis="local_derivation_at",
                title=f"{row.get('company')} {row.get('category')} {row.get('change_kind')}",
                bounded_excerpt=row.get("excerpt"),
                source_reference=row.get("source_url"),
                entities=_entities(
                    _entity("company", row.get("company")),
                    _entity("symbol", row.get("symbol")),
                    _entity("industry", row.get("industry")),
                    _entity("macro_region", row.get("region")),
                ),
                structured_fields={
                    key: row.get(key)
                    for key in (
                        "document_id",
                        "previous_document_id",
                        "category",
                        "change_kind",
                        "section_hash",
                        "previous_section_hash",
                        "metrics",
                        "report_date",
                    )
                },
                provenance={"adapter": self.name, "deterministic_delta": True},
                freshness="current",
            )
            for row in rows
        ]


class InvestmentAnalysisAdapter:
    name = "investment_analyses"

    def collect(self, session: Any, *, since: datetime, until: datetime | None = None, limit: int) -> list[NormalizedEvidence]:
        rows = _execute(
            session,
            """
            SELECT a.analysis_id, a.document_id, a.previous_document_id, a.facts,
                   a.analysis, a.model, a.created_at, a.updated_at, d.company,
                   d.symbol, d.industry, d.region, d.document_type, d.report_date,
                   d.source_url, d.filing_source
            FROM investment_analyses AS a
            JOIN investment_documents AS d ON d.document_id = a.document_id
            WHERE a.updated_at >= :since
              AND (:until IS NULL OR a.updated_at <= :until)
            ORDER BY a.updated_at DESC, a.analysis_id
            LIMIT :limit
            """,
            {"since": since, "until": until, "limit": limit},
        )
        evidence: list[NormalizedEvidence] = []
        for row in rows:
            analysis = _mapping(row.get("analysis"))
            facts = _mapping(row.get("facts"))
            observed = _timestamp(row.get("updated_at") or row.get("created_at"), since)
            evidence.append(
                NormalizedEvidence.create(
                    evidence_type=EvidenceType.INVESTMENT_ANALYSIS,
                    evidence_id=str(row.get("analysis_id")),
                    source_name=row.get("filing_source") or "investment_analysis",
                    source_timestamp=observed,
                    acquired_at=row.get("created_at"),
                    available_at=row.get("updated_at") or row.get("created_at"),
                    availability_basis="analysis_completed_at",
                    title=f"{row.get('company')} {row.get('document_type')} analysis",
                    bounded_excerpt=analysis.get("summary") or analysis.get("thesis"),
                    source_reference=row.get("source_url"),
                    entities=_entities(
                        _entity("company", row.get("company")),
                        _entity("symbol", row.get("symbol")),
                        _entity("industry", row.get("industry")),
                        _entity("macro_region", row.get("region")),
                    ),
                    structured_fields={
                        "document_id": row.get("document_id"),
                        "previous_document_id": row.get("previous_document_id"),
                        "report_date": row.get("report_date"),
                        "metrics": _mapping(facts.get("metrics")),
                        "qualitative": _mapping(facts.get("qualitative")),
                        "state": analysis.get("state"),
                    },
                    provenance={
                        "adapter": self.name,
                        "model": row.get("model"),
                        "deterministic_metrics": True,
                    },
                    freshness=_freshness(observed, since),
                )
            )
        return evidence


class SourceClaimAdapter:
    name = "source_claims"

    def collect(self, session: Any, *, since: datetime, until: datetime | None = None, limit: int) -> list[NormalizedEvidence]:
        rows = _execute(
            session,
            """
            SELECT id, evidence_type, evidence_id, subject, predicate,
                   object_value, unit, period, geography, direction, claim_kind,
                   source_span, observed_at, confidence, entities, model_slug,
                   prompt_version, input_fingerprint, provenance, created_at
            FROM research_source_claims
            WHERE observed_at >= :since
              AND (:until IS NULL OR created_at <= :until)
            ORDER BY observed_at DESC, id
            LIMIT :limit
            """,
            {"since": since, "until": until, "limit": limit},
        )
        evidence: list[NormalizedEvidence] = []
        for row in rows:
            observed = _timestamp(row.get("observed_at"), since)
            entities = _json_entities(row.get("entities"))
            evidence.append(
                NormalizedEvidence.create(
                    evidence_type=EvidenceType.SOURCE_CLAIM,
                    evidence_id=str(row.get("id")),
                    source_name="source_claim",
                    source_timestamp=observed,
                    acquired_at=row.get("created_at"),
                    available_at=row.get("created_at"),
                    availability_basis="claim_extracted_at",
                    title=f"{row.get('subject')} — {row.get('predicate')}",
                    bounded_excerpt=row.get("source_span"),
                    entities=entities,
                    structured_fields={
                        key: row.get(key)
                        for key in (
                            "subject",
                            "predicate",
                            "object_value",
                            "unit",
                            "period",
                            "geography",
                            "direction",
                            "claim_kind",
                            "confidence",
                        )
                    },
                    provenance={
                        "adapter": self.name,
                        "source_evidence_type": row.get("evidence_type"),
                        "source_evidence_id": row.get("evidence_id"),
                        "model_slug": row.get("model_slug"),
                        "prompt_version": row.get("prompt_version"),
                        "input_fingerprint": row.get("input_fingerprint"),
                        **_mapping(row.get("provenance")),
                    },
                    freshness=_freshness(observed, since),
                )
            )
        return evidence


DEFAULT_ADAPTERS: tuple[EvidenceAdapter, ...] = (
    MacroObservationAdapter(),
    MacroReleaseAdapter(),
    MarketStateAdapter(),
    StoryClusterAdapter(),
    OfficialDocumentAdapter(),
    MarketConfirmationAdapter(),
    InvestmentObservationAdapter(),
    FilingDeltaAdapter(),
    InvestmentAnalysisAdapter(),
    SourceClaimAdapter(),
)


class EvidenceRegistry:
    def __init__(self, adapters: Sequence[EvidenceAdapter] = DEFAULT_ADAPTERS):
        names = [adapter.name for adapter in adapters]
        if len(names) != len(set(names)):
            raise ValueError("evidence adapter names must be unique")
        self.adapters = tuple(adapters)

    def collect(
        self,
        session: Any,
        *,
        rolling_window_days: int,
        limit: int,
        now: datetime | None = None,
        context: ResearchContext | None = None,
    ) -> EvidenceCollection:
        bounded_limit = max(1, min(int(limit), 2_000))
        effective_now = (
            context.effective_now(now)
            if context is not None
            else _timestamp(now, datetime.now(UTC))
        )
        since = effective_now - timedelta(days=max(1, min(int(rolling_window_days), 730)))
        per_adapter = max(1, (bounded_limit + len(self.adapters) - 1) // len(self.adapters))
        until = context.as_of if context is not None and context.is_replay else None
        items: list[NormalizedEvidence] = []
        failures: dict[str, str] = {}
        for adapter in self.adapters:
            try:
                rows = adapter.collect(
                    session, since=since, until=until, limit=per_adapter
                )
            except Exception as exc:
                failures[adapter.name] = type(exc).__name__
                continue
            items.extend(rows[:per_adapter])
        if context is not None:
            items = list(context.filter_evidence(items))
        deduped: dict[str, NormalizedEvidence] = {}
        for item in sorted(
            items, key=lambda value: (value.available_at, value.ref), reverse=True
        ):
            deduped.setdefault(item.ref, item)
        return EvidenceCollection(
            items=tuple(list(deduped.values())[:bounded_limit]), failures=failures
        )


__all__ = [
    "DEFAULT_ADAPTERS",
    "EvidenceAdapter",
    "EvidenceCollection",
    "EvidenceRegistry",
    "FilingDeltaAdapter",
    "InvestmentAnalysisAdapter",
    "InvestmentObservationAdapter",
    "MacroObservationAdapter",
    "OfficialDocumentAdapter",
    "MacroReleaseAdapter",
    "MarketConfirmationAdapter",
    "MarketStateAdapter",
    "SourceClaimAdapter",
    "StoryClusterAdapter",
]
