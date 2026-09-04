"""Bounded adapters from source-owned tables to normalized research evidence."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import text

from contracts.db_results import result_rows
from research_intelligence.context import ResearchContext
from research_intelligence.contracts import (
    EvidenceType,
    NormalizedEntity,
    NormalizedEvidence,
)
from research_intelligence.relationships import normalize_entity


class EvidenceAdapter(Protocol):
    name: str

    def collect(
        self,
        session: Any,
        *,
        since: datetime,
        until: datetime | None = None,
        limit: int,
    ) -> list[NormalizedEvidence]: ...


@dataclass(frozen=True, slots=True)
class EvidenceCollection:
    items: tuple[NormalizedEvidence, ...]
    failures: Mapping[str, str]


def _execute(
    session: Any, statement: str, params: Mapping[str, Any]
) -> list[dict[str, Any]]:
    return result_rows(session.execute(text(statement), dict(params)))


def _recovery_savepoint(session: Any):
    begin = getattr(session, "begin_nested", None)
    return begin() if callable(begin) else nullcontext()


def _recover_execute(
    session: Any, statement: str, params: Mapping[str, Any]
) -> list[dict[str, Any]]:
    try:
        with _recovery_savepoint(session):
            return _execute(session, statement, params)
    except Exception:
        return []


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
    if isinstance(value, (list, tuple)):
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


def _strict_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _date_timestamp(value: Any, default: datetime) -> datetime:
    if isinstance(value, datetime):
        return _timestamp(value, default)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value[:10])
        except ValueError:
            parsed = None
        if parsed is not None:
            return datetime(parsed.year, parsed.month, parsed.day, tzinfo=UTC)
    return _timestamp(value, default)


def _latest_timestamp(*values: Any, default: datetime) -> datetime:
    timestamps = [_timestamp(v) for v in values if v is not None]
    return max(timestamps, default=default)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _count(value: Any) -> int | None:
    parsed = _finite(value)
    return None if parsed is None else int(parsed)


def _fmt_number(value: Any) -> str:
    parsed = _finite(value)
    return f"{parsed:g}" if parsed is not None else "n/a"


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


_INSTITUTION_REGIONS = {"fed": "US", "ecb": "euro_area", "boe": "UK", "boj": "Japan"}
_ISSUER_DOCUMENT_SOURCES = frozenset(
    {"issuer_transcripts", "issuer_news", "company_expectations"}
)
_POSITIONING_SEMANTICS = {
    "insider_activity": "SEC Form 4 open-market insider transactions; sells are dispositions of shares held, never short sales",
    "short_volume": "daily short-sale volume, a delayed flow proxy; never short interest",
    "short_interest": "shares held short as of the report date; short interest, not volume",
    "futures_positioning": "CFTC Commitments of Traders futures/options positions; not short interest",
}
_TRANSCRIPT_SEGMENT_MAX = 1_500
_MAX_TRANSCRIPT_SEGMENTS = 8
_FINANCE_KEYWORDS = (
    "guidance",
    "demand",
    "margin",
    "pricing",
    "inventory",
    "backlog",
    "capex",
    "orders",
    "supply",
    "customer",
    "competition",
    "risk",
)
_FINANCE_KEYWORD_RES = tuple(
    re.compile(rf"\b{re.escape(k)}\b") for k in _FINANCE_KEYWORDS
)
_QNA_HEADING_RE = re.compile(
    r"(?i)^(?:\s*q(?:uestion)?s?[ &/]+a(?:nswer)?s?|question[- ]+and[- ]+answer(?:s)?|questions?[- ]+and[- ]+answers?)[ -]*(?:session|section|segment)?[.:]*$"
)


def _segment_cut(text: str, maximum: int) -> int:
    end = min(len(text), maximum)
    if end >= len(text):
        return end
    for marker in ("\n\n", "\n"):
        b = text.rfind(marker, 0, end)
        if b > 0:
            return min(b + len(marker), end)
    return end


def _qna_heading_offset(text: str) -> int | None:
    offset = 0
    for line in text.splitlines(keepends=True):
        if _QNA_HEADING_RE.fullmatch(line.strip()):
            return offset
        offset += len(line)
    return None


def _transcript_windows(text: str, start: int, maximum: int) -> list[tuple[int, int]]:
    windows: list[tuple[int, int]] = []
    cursor, length = start, len(text)
    while cursor < length:
        end = min(cursor + maximum, length)
        if end < length:
            nl = text.rfind("\n", cursor, end)
            if nl > cursor:
                end = nl + 1
        windows.append((cursor, end))
        cursor = end
    return windows


def _finance_signal(window: str) -> int:
    cf = window.casefold()
    return sum(len(r.findall(cf)) for r in _FINANCE_KEYWORD_RES)


def _transcript_segments(content: str) -> list[tuple[str, int, int]]:
    text = content.strip()
    if not text:
        return []
    qa = _qna_heading_offset(text)
    if qa is not None:
        opening_end = _segment_cut(text, min(qa, _TRANSCRIPT_SEGMENT_MAX))
        body_start, body_kind = qa, "qa"
    else:
        opening_end = _segment_cut(text, _TRANSCRIPT_SEGMENT_MAX)
        body_start, body_kind = opening_end, "body"
    segments: list[tuple[str, int, int]] = []
    if opening_end > 0:
        segments.append(("opening", 0, opening_end))
    windows = [
        (body_kind, s, e)
        for s, e in _transcript_windows(text, body_start, _TRANSCRIPT_SEGMENT_MAX)
        if e > s
    ]
    windows.sort(key=lambda item: (-_finance_signal(text[item[1] : item[2]]), item[1]))
    segments.extend(windows[: _MAX_TRANSCRIPT_SEGMENTS - len(segments)])
    return segments


def _recovery_observed(
    row: Mapping[str, Any], available_by: datetime | None
) -> datetime:
    created = _timestamp(
        row.get("created_at"), available_by or datetime.min.replace(tzinfo=UTC)
    )
    return _timestamp(
        row.get("observed_at") or row.get("published_at") or row.get("updated_at"),
        created,
    )


# ---------------------------------------------------------------------------
# Declarative Row Mappers & Collectors
# ---------------------------------------------------------------------------


def _transform_macro_observation(
    row: dict[str, Any], since: datetime, adapter_name: str
) -> NormalizedEvidence:
    observed = _timestamp(row.get("released_at") or row.get("observed_at"), since)
    metadata = _mapping(row.get("metadata"))
    title = row.get("title") or row.get("series_id")
    value = _finite(row.get("value"))
    unit = row.get("units")
    excerpt = (
        f"{title}: {value:g}{f' {unit}' if unit else ''}"
        if value is not None
        else f"{title}: value unavailable"
    )
    series = str(row.get("series_id"))
    return NormalizedEvidence.create(
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
            _entity("concept", series), _entity("macro_region", metadata.get("region"))
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
            "adapter": adapter_name,
            "metadata": metadata,
            "revision_at": row.get("revision_at"),
            "source_updated_at": row.get("updated_at"),
        },
        freshness=_freshness(observed, since),
    )


def _build_official_doc_item(
    row: Mapping[str, Any],
    since: datetime,
    adapter_name: str,
    *,
    identifier: str | None = None,
    plan_item: tuple[int, str, int, int] | None = None,
    recovered: bool = False,
    available_by: datetime | None = None,
) -> NormalizedEvidence | None:
    metadata = _mapping(row.get("metadata"))
    source = str(row.get("source") or "").strip()
    if source == "issuer_transcripts" and metadata.get("available") is False:
        return None
    created = _timestamp(row.get("created_at"), available_by if recovered else since)
    observed = _timestamp(row.get("published_at"), created if recovered else since)
    institution = str(row.get("institution") or "").strip().casefold()
    if source in _ISSUER_DOCUMENT_SOURCES:
        entities = _entities(
            _entity("company", row.get("institution")),
            _entity("symbol", metadata.get("ticker") or metadata.get("symbol")),
        )
    else:
        entities = _entities(
            _entity("concept", institution),
            _entity("macro_region", _INSTITUTION_REGIONS.get(institution)),
        )
    base_fields = {
        "institution": institution or None,
        "document_type": row.get("document_type"),
        "ticker": metadata.get("ticker") or metadata.get("symbol"),
    }
    parent_prov = {
        "adapter": adapter_name,
        "source": row.get("source"),
        "metadata": metadata,
    }
    if recovered:
        parent_prov["recovered_by_exact_id"] = True
    avail_at = _latest_timestamp(
        observed,
        row.get("acquired_at"),
        row.get("created_at"),
        row.get("updated_at"),
        default=observed,
    )
    doc_id = identifier or str(row.get("document_id"))
    fresh = _freshness(observed, (available_by or observed) if recovered else since)
    if plan_item is None:
        title = (
            (row.get("title") or row.get("document_type") or "official document")
            if recovered
            else row.get("title")
        )
        return NormalizedEvidence.create(
            evidence_type=EvidenceType.OFFICIAL_DOCUMENT,
            evidence_id=doc_id,
            source_name=institution or row.get("source") or "official",
            source_timestamp=observed,
            acquired_at=row.get("acquired_at") or row.get("created_at"),
            available_at=avail_at,
            availability_basis="published_or_acquired_at",
            title=title,
            bounded_excerpt=row.get("content"),
            source_reference=row.get("url"),
            entities=entities,
            structured_fields=base_fields,
            provenance=parent_prov,
            freshness=fresh,
        )
    index, kind, start, end = plan_item
    title_base = (
        (row.get("title") or row.get("document_type") or "official document")
        if recovered
        else row.get("title")
    )
    seg_id = identifier or f"{row.get('document_id')}:seg{index}"
    return NormalizedEvidence.create(
        evidence_type=EvidenceType.OFFICIAL_DOCUMENT,
        evidence_id=seg_id,
        source_name=institution or row.get("source") or "official",
        source_timestamp=observed,
        acquired_at=row.get("acquired_at") or row.get("created_at"),
        available_at=avail_at,
        availability_basis="published_or_acquired_at",
        title=f"{title_base} — segment {index} ({kind})",
        bounded_excerpt=str(row.get("content") or "")[start:end],
        source_reference=row.get("url"),
        entities=entities,
        structured_fields={
            **base_fields,
            "parent_document_id": str(row.get("document_id")),
            "segment_index": index,
            "segment_kind": kind,
            "char_start": start,
            "char_end": end,
        },
        provenance={**parent_prov, "segment": True},
        freshness=fresh,
    )


def _collect_official_documents(
    session: Any, since: datetime, until: datetime | None, limit: int, adapter_name: str
) -> list[NormalizedEvidence]:
    stmt = (
        "WITH ranked AS ("
        " SELECT document_id, source, institution, document_type, title, published_at, url, content, metadata, created_at, updated_at, acquired_at,"
        " ROW_NUMBER() OVER (PARTITION BY source, institution ORDER BY published_at DESC, document_id) AS source_rank"
        " FROM source_documents WHERE published_at >= :since"
        " AND (:until IS NULL OR published_at <= :until)"
        " AND (:until IS NULL OR COALESCE(acquired_at, created_at) <= :until)"
        " AND (:until IS NULL OR updated_at <= :until))"
        " SELECT document_id, source, institution, document_type, title, published_at, url, content, metadata, created_at, updated_at, acquired_at"
        " FROM ranked ORDER BY source_rank, published_at DESC, source, institution, document_id LIMIT :limit"
    )
    rows = _execute(session, stmt, {"since": since, "until": until, "limit": limit})
    kept_rows, plans = [], []
    for row in rows:
        metadata = _mapping(row.get("metadata"))
        source = str(row.get("source") or "").strip()
        if source == "issuer_transcripts" and metadata.get("available") is False:
            continue
        content = row.get("content")
        if (
            source == "issuer_transcripts"
            and isinstance(content, str)
            and content.strip()
        ):
            plans.append(
                [
                    (i, k, s, e)
                    for i, (k, s, e) in enumerate(
                        _transcript_segments(content), start=1
                    )
                ]
            )
        else:
            plans.append([None])
        kept_rows.append(row)
    selected: list[tuple[int, int]] = []
    slot = 0
    while len(selected) < limit:
        advanced = False
        for plan_index, plan in enumerate(plans):
            if slot < len(plan):
                selected.append((plan_index, slot))
                if len(selected) >= limit:
                    break
                advanced = True
        if not advanced:
            break
        slot += 1
    evidence: list[NormalizedEvidence] = []
    for plan_index, slot in selected:
        item = _build_official_doc_item(
            kept_rows[plan_index],
            since,
            adapter_name,
            plan_item=plans[plan_index][slot],
        )
        if item is not None:
            evidence.append(item)
    return evidence


def _transform_macro_release(
    row: dict[str, Any], since: datetime, adapter_name: str
) -> NormalizedEvidence:
    observed = _timestamp(
        row.get("released_at") or row.get("observed_at") or row.get("created_at"), since
    )
    return NormalizedEvidence.create(
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
        bounded_excerpt=f"Actual {row.get('actual')}; consensus {row.get('consensus')}; previous {row.get('previous')}",
        entities=_entities(
            _entity("concept", row.get("series_id") or row.get("event_name"))
        ),
        structured_fields={
            k: row.get(k)
            for k in (
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
            "adapter": adapter_name,
            "immutable_card": True,
            "revision_at": row.get("revision_at"),
            "vintage_available": True,
        },
        freshness=_freshness(observed, since),
    )


def _transform_market_state(
    row: dict[str, Any], since: datetime, adapter_name: str
) -> NormalizedEvidence:
    as_of = _timestamp(row.get("as_of"), since)
    return NormalizedEvidence.create(
        evidence_type=EvidenceType.MARKET_STATE,
        evidence_id=str(row.get("source_event_id")),
        source_name="market_state",
        source_timestamp=as_of,
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
        provenance={"adapter": adapter_name, "calculation": "deterministic"},
        freshness=_freshness(as_of, since),
    )


def _transform_story_cluster(
    row: dict[str, Any],
    since: datetime,
    adapter_name: str,
    *,
    recovered: bool = False,
    available_by: datetime | None = None,
) -> NormalizedEvidence:
    observed = _timestamp(
        row.get("last_seen_at"),
        _timestamp(row.get("updated_at"), available_by) if recovered else since,
    )
    market_entities = tuple(
        entity
        for item in _list(row.get("markets"))[:20]
        if (
            entity := _entity(
                "market", item.get("symbol") if isinstance(item, Mapping) else item
            )
        )
    )
    prov = {
        "adapter": adapter_name,
        "clustering_reason": _mapping(row.get("clustering_reason")),
    }
    if recovered:
        prov["recovered_by_exact_id"] = True
    else:
        prov["source_names"] = _list(row.get("member_sources"))
    return NormalizedEvidence.create(
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
            k: row.get(k)
            for k in (
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
        provenance=prov,
        freshness=_freshness(
            observed, (available_by or observed) if recovered else since
        ),
    )


def _collect_story_clusters(
    session: Any, since: datetime, until: datetime | None, limit: int, adapter_name: str
) -> list[NormalizedEvidence]:
    if until is None:
        statement = (
            "SELECT s.id, s.title, s.summary, s.state, s.lane, s.first_seen_at, s.last_seen_at, s.last_material_change_at,"
            " s.importance, s.novelty, s.confidence, s.entities, s.markets, s.source_count, s.version, s.change_summary,"
            " s.clustering_reason, s.updated_at,"
            " ARRAY(SELECT DISTINCT m.source FROM story_cluster_members AS m WHERE m.cluster_id = s.id ORDER BY m.source LIMIT 10) AS member_sources"
            " FROM story_clusters AS s WHERE s.last_seen_at >= :since AND s.state NOT IN ('stale', 'closed')"
            " ORDER BY s.last_material_change_at DESC, s.id LIMIT :limit"
        )
    else:
        statement = (
            "WITH versions AS (SELECT DISTINCT ON (v.cluster_id) v.cluster_id AS id, v.snapshot, v.changed_at"
            " FROM story_cluster_versions AS v WHERE v.changed_at <= :until ORDER BY v.cluster_id, v.version DESC)"
            " SELECT v.id, v.snapshot->>'title' AS title, v.snapshot->>'summary' AS summary, v.snapshot->>'state' AS state,"
            " v.snapshot->>'lane' AS lane, (v.snapshot->>'first_seen_at')::TIMESTAMPTZ AS first_seen_at,"
            " (v.snapshot->>'last_seen_at')::TIMESTAMPTZ AS last_seen_at,"
            " (v.snapshot->>'last_material_change_at')::TIMESTAMPTZ AS last_material_change_at,"
            " (v.snapshot->>'importance')::DOUBLE PRECISION AS importance, (v.snapshot->>'novelty')::DOUBLE PRECISION AS novelty,"
            " (v.snapshot->>'confidence')::DOUBLE PRECISION AS confidence, COALESCE(v.snapshot->'entities', '[]'::JSONB) AS entities,"
            " COALESCE(v.snapshot->'markets', '[]'::JSONB) AS markets, (v.snapshot->>'source_count')::INTEGER AS source_count,"
            " (v.snapshot->>'version')::INTEGER AS version, v.snapshot->>'change_summary' AS change_summary,"
            " COALESCE(v.snapshot->'clustering_reason', '{}'::JSONB) AS clustering_reason, v.changed_at AS updated_at,"
            " ARRAY(SELECT DISTINCT m.source FROM story_cluster_members AS m WHERE m.cluster_id = v.id AND m.published_at <= :until AND m.created_at <= :until ORDER BY m.source LIMIT 10) AS member_sources"
            " FROM versions AS v WHERE (v.snapshot->>'last_seen_at')::TIMESTAMPTZ >= :since AND v.snapshot->>'state' NOT IN ('stale', 'closed')"
            " ORDER BY (v.snapshot->>'last_material_change_at')::TIMESTAMPTZ DESC, v.id LIMIT :limit"
        )
    rows = _execute(
        session, statement, {"since": since, "until": until, "limit": limit}
    )
    return [_transform_story_cluster(row, since, adapter_name) for row in rows]


def _collect_market_confirmations(
    session: Any, since: datetime, until: datetime | None, limit: int, adapter_name: str
) -> list[NormalizedEvidence]:
    story_limit = max(1, limit // 2)
    s_stmt = (
        "SELECT id, cluster_id, source_event_id, market_symbol, headline_at, observed_at, pre_headline_move, move_5m, move_30m, move_session,"
        " flags, missing_reasons, provenance, created_at, updated_at FROM story_market_confirmations WHERE observed_at >= :since"
        " AND (:until IS NULL OR observed_at <= :until) AND (:until IS NULL OR updated_at <= :until) ORDER BY observed_at DESC, id LIMIT :limit"
    )
    w_stmt = (
        "SELECT id, event_id, instrument_symbol, horizon, event_at, target_at, observed_at, baseline_price, target_price, absolute_move,"
        " percentage_move, volatility_adjusted_move, expected_direction, sensitivity, direction_vs_expected, reaction_state,"
        " missing_data_reason, provenance, created_at, updated_at FROM event_reaction_windows WHERE COALESCE(observed_at, target_at) >= :since"
        " AND (:until IS NULL OR target_at <= :until) AND (:until IS NULL OR COALESCE(observed_at, target_at) <= :until)"
        " AND (:until IS NULL OR updated_at <= :until) ORDER BY COALESCE(observed_at, target_at) DESC, id LIMIT :limit"
    )
    story_rows = _execute(
        session, s_stmt, {"since": since, "until": until, "limit": story_limit}
    )
    window_rows = _execute(
        session,
        w_stmt,
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
                    k: row.get(k)
                    for k in (
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
                provenance={"adapter": adapter_name, **_mapping(row.get("provenance"))},
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
                    k: row.get(k)
                    for k in (
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
                provenance={"adapter": adapter_name, **_mapping(row.get("provenance"))},
                freshness=_freshness(observed, since),
            )
        )
    return evidence[:limit]


def _transform_investment_observation(
    row: dict[str, Any],
    since: datetime,
    adapter_name: str,
    *,
    recovered: bool = False,
    available_by: datetime | None = None,
) -> NormalizedEvidence:
    observed = (
        _recovery_observed(row, available_by)
        if recovered
        else _timestamp(row.get("observed_at"), since)
    )
    narrative = _mapping(row.get("narrative"))
    prov = {"adapter": adapter_name, **_mapping(row.get("provenance"))}
    if recovered:
        prov["recovered_by_exact_id"] = True
    return NormalizedEvidence.create(
        evidence_type=EvidenceType.INVESTMENT_OBSERVATION,
        evidence_id=str(row.get("observation_id")),
        source_name=row.get("source_kind") or "investment",
        source_timestamp=observed,
        acquired_at=row.get("created_at"),
        available_at=_latest_timestamp(
            observed, row.get("updated_at"), default=observed
        ),
        availability_basis="observation_updated_at",
        title=row.get("company") or row.get("industry") or "Investment observation",
        bounded_excerpt=narrative.get("summary")
        or narrative.get("thesis")
        or narrative.get("counter_thesis"),
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
        provenance=prov,
        freshness=_freshness(
            observed, (available_by or observed) if recovered else since
        ),
    )


def _transform_filing_delta(
    row: dict[str, Any],
    since: datetime,
    adapter_name: str,
    *,
    recovered: bool = False,
    available_by: datetime | None = None,
) -> NormalizedEvidence:
    created = _timestamp(row.get("created_at"), available_by if recovered else since)
    title = (
        (
            " ".join(
                part
                for part in (
                    row.get("company"),
                    row.get("category"),
                    row.get("change_kind"),
                )
                if part
            )
            or "filing delta"
        )
        if recovered
        else f"{row.get('company')} {row.get('category')} {row.get('change_kind')}"
    )
    prov = {"adapter": adapter_name, "deterministic_delta": True}
    if recovered:
        prov["recovered_by_exact_id"] = True
    return NormalizedEvidence.create(
        evidence_type=EvidenceType.FILING_DELTA,
        evidence_id=str(row.get("id")),
        source_name=row.get("filing_source") or "filing",
        source_timestamp=created,
        acquired_at=row.get("created_at"),
        available_at=row.get("created_at"),
        availability_basis="local_derivation_at",
        title=title,
        bounded_excerpt=row.get("excerpt"),
        source_reference=row.get("source_url"),
        entities=_entities(
            _entity("company", row.get("company")),
            _entity("symbol", row.get("symbol")),
            _entity("industry", row.get("industry")),
            _entity("macro_region", row.get("region")),
        ),
        structured_fields={
            k: row.get(k)
            for k in (
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
        provenance=prov,
        freshness="current",
    )


def _transform_investment_analysis(
    row: dict[str, Any],
    since: datetime,
    adapter_name: str,
    *,
    recovered: bool = False,
    available_by: datetime | None = None,
) -> NormalizedEvidence:
    analysis, facts = _mapping(row.get("analysis")), _mapping(row.get("facts"))
    if recovered:
        title = (
            " ".join(
                part for part in (row.get("company"), row.get("document_type")) if part
            )
            or "investment analysis"
        )
        excerpt = (
            " | ".join(
                part
                for part in (
                    str(analysis.get("summary") or "").strip(),
                    str(analysis.get("thesis") or "").strip(),
                    str(analysis.get("counter_thesis") or "").strip(),
                )
                if part
            )
            or None
        )
        observed = _timestamp(
            row.get("updated_at") or row.get("created_at"), available_by
        )
        structured_metrics = _mapping(facts.get("metrics"))
        deterministic_metrics = None
        valuation = None
    else:
        metrics, valuation = (
            _mapping(analysis.get("metrics")),
            _mapping(analysis.get("valuation")),
        )
        compact_facts: list[str] = []
        for name in (
            "revenue",
            "capex",
            "fcf",
            "backlog",
            "diluted_eps",
            "shares_outstanding",
            "net_debt",
            "gross_margin",
        ):
            metric = _mapping(metrics.get(name))
            value = _finite(metric.get("value"))
            if value is not None:
                part = f"{name}={_fmt_number(value)}"
                if metric.get("unit"):
                    part += f" {metric['unit']}"
                change_pct = _finite(metric.get("change_pct"))
                if change_pct is not None:
                    part += f" change_pct={_fmt_number(change_pct)}"
                compact_facts.append(part)
        dcf = _mapping(valuation.get("dcf"))
        for label, val in (
            ("pe_ratio", valuation.get("pe_ratio")),
            ("dcf_per_share", dcf.get("per_share")),
            ("dcf_enterprise_value", dcf.get("enterprise_value")),
        ):
            if (parsed := _finite(val)) is not None:
                compact_facts.append(f"{label}={_fmt_number(parsed)}")
        summary, thesis, counter_thesis = (
            str(analysis.get("summary") or "").strip(),
            str(analysis.get("thesis") or "").strip(),
            str(analysis.get("counter_thesis") or "").strip(),
        )
        excerpt = "Deterministic filing data: " + (
            "; ".join(compact_facts) if compact_facts else "unavailable"
        )
        if summary:
            excerpt += " | Filing observation: " + summary
        if thesis:
            excerpt += " | Filing interpretation: " + thesis
        if counter_thesis:
            excerpt += " | Filing counter-thesis: " + counter_thesis
        observed = _timestamp(row.get("updated_at") or row.get("created_at"), since)
        title = f"{row.get('company')} {row.get('document_type')} analysis"
        structured_metrics = _mapping(facts.get("metrics"))
        deterministic_metrics = metrics

    fields: dict[str, Any] = {
        "document_id": row.get("document_id"),
        "previous_document_id": row.get("previous_document_id"),
        "report_date": row.get("report_date"),
        "metrics": structured_metrics,
        "qualitative": _mapping(facts.get("qualitative")),
        "state": analysis.get("state"),
        "counter_thesis": analysis.get("counter_thesis"),
        "materiality_assessment": _mapping(analysis.get("materiality_assessment")),
        "relationship_facts": analysis.get("relationship_facts"),
        "material_relationships": analysis.get("material_relationships"),
        "relationship_reconciliations": analysis.get("relationship_reconciliations"),
        "catalysts": _list(analysis.get("catalysts")),
        "risks": _list(analysis.get("risks")),
    }
    if not recovered:
        fields["deterministic_metrics"] = deterministic_metrics
        fields["valuation"] = valuation

    prov = {
        "adapter": adapter_name,
        "model": row.get("model"),
        "deterministic_metrics": True,
    }
    if recovered:
        prov["recovered_by_exact_id"] = True

    return NormalizedEvidence.create(
        evidence_type=EvidenceType.INVESTMENT_ANALYSIS,
        evidence_id=str(row.get("analysis_id")),
        source_name=row.get("filing_source") or "investment_analysis",
        source_timestamp=observed,
        acquired_at=row.get("created_at"),
        available_at=row.get("updated_at") or row.get("created_at"),
        availability_basis="analysis_completed_at",
        title=title,
        bounded_excerpt=excerpt,
        source_reference=row.get("source_url"),
        entities=_entities(
            _entity("company", row.get("company")),
            _entity("symbol", row.get("symbol")),
            _entity("industry", row.get("industry")),
            _entity("macro_region", row.get("region")),
        ),
        structured_fields=fields,
        provenance=prov,
        freshness=_freshness(
            observed, (available_by or observed) if recovered else since
        ),
    )


def _transform_source_claim(
    row: dict[str, Any],
    since: datetime,
    adapter_name: str,
    *,
    recovered: bool = False,
    available_by: datetime | None = None,
) -> NormalizedEvidence:
    observed = (
        _recovery_observed(row, available_by)
        if recovered
        else _timestamp(row.get("observed_at"), since)
    )
    prov = {
        "adapter": adapter_name,
        "source_evidence_type": row.get("evidence_type"),
        "source_evidence_id": row.get("evidence_id"),
        "model_slug": row.get("model_slug"),
        "prompt_version": row.get("prompt_version"),
        "input_fingerprint": row.get("input_fingerprint"),
        **_mapping(row.get("provenance")),
    }
    if recovered:
        prov["recovered_by_exact_id"] = True
    title = (
        f"{str(row.get('subject') or 'claim')} — {str(row.get('predicate') or 'observed')}"
        if recovered
        else f"{row.get('subject')} — {row.get('predicate')}"
    )
    return NormalizedEvidence.create(
        evidence_type=EvidenceType.SOURCE_CLAIM,
        evidence_id=str(row.get("id")),
        source_name="source_claim",
        source_timestamp=observed,
        acquired_at=row.get("created_at"),
        available_at=row.get("created_at"),
        availability_basis="claim_extracted_at",
        title=title,
        bounded_excerpt=row.get("source_span"),
        entities=_json_entities(row.get("entities")),
        structured_fields={
            k: row.get(k)
            for k in (
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
        provenance=prov,
        freshness=_freshness(
            observed, (available_by or observed) if recovered else since
        ),
    )


def _transform_public_equities(
    row: dict[str, Any], since: datetime, adapter_name: str
) -> NormalizedEvidence:
    bar_time = _timestamp(row.get("timestamp"), since)
    metadata = _mapping(row.get("metadata"))
    open_, high, low, close, volume = (
        _finite(row.get("open")),
        _finite(row.get("high")),
        _finite(row.get("low")),
        _finite(row.get("close")),
        _finite(row.get("volume")),
    )
    excerpt = f"{row.get('symbol')} " + " ".join(
        f"{lbl} {_fmt_number(v)}"
        for lbl, v in (
            ("open", open_),
            ("high", high),
            ("low", low),
            ("close", close),
            ("volume", volume),
        )
    )
    return NormalizedEvidence.create(
        evidence_type=EvidenceType.MARKET_CONFIRMATION,
        evidence_id=f"{row.get('symbol')}:{row.get('timeframe')}@{bar_time.isoformat()}",
        source_name=row.get("source") or "market_data",
        source_timestamp=bar_time,
        acquired_at=row.get("created_at"),
        available_at=_latest_timestamp(
            _strict_timestamp(metadata.get("available_at")),
            row.get("created_at"),
            row.get("updated_at"),
            default=bar_time,
        ),
        availability_basis="provider_fetch_or_persisted_at",
        title=f"{row.get('symbol')} {row.get('timeframe')} bar {bar_time.isoformat()}",
        bounded_excerpt=excerpt,
        entities=_entities(_entity("symbol", row.get("symbol"))),
        structured_fields={
            "timeframe": row.get("timeframe"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "adjusted": metadata.get("adjusted"),
            "provider_symbol": metadata.get("provider_symbol"),
            "currency": metadata.get("currency"),
            "exchange_name": metadata.get("exchange_name"),
            "provider_source_timestamp": metadata.get("source_timestamp"),
        },
        provenance={"adapter": adapter_name, "metadata": metadata},
        freshness=_freshness(bar_time, since),
    )


def _collect_public_equity_trends(
    session: Any, since: datetime, until: datetime | None, limit: int, adapter_name: str
) -> list[NormalizedEvidence]:
    stmt = (
        "WITH selected_symbols AS ("
        " SELECT symbol, MAX(timestamp) AS latest FROM market_data"
        " WHERE source = 'public_equities' AND timeframe = '1d' AND timestamp >= :since"
        " AND (:until IS NULL OR timestamp <= :until) AND (:until IS NULL OR created_at <= :until) AND (:until IS NULL OR updated_at <= :until)"
        " GROUP BY symbol ORDER BY latest DESC, symbol LIMIT :limit),"
        " ranked AS ("
        " SELECT m.symbol, m.timestamp, m.close, m.volume, m.source, m.metadata, m.created_at, m.updated_at,"
        " ROW_NUMBER() OVER (PARTITION BY m.symbol ORDER BY m.timestamp DESC) AS row_number FROM market_data m"
        " JOIN selected_symbols s ON s.symbol = m.symbol WHERE m.source = 'public_equities' AND m.timeframe = '1d'"
        " AND m.timestamp >= :since AND (:until IS NULL OR m.timestamp <= :until) AND (:until IS NULL OR m.created_at <= :until) AND (:until IS NULL OR m.updated_at <= :until))"
        " SELECT symbol, timestamp, close, volume, source, metadata, created_at, updated_at FROM ranked WHERE row_number <= 63 ORDER BY symbol, timestamp"
    )
    rows = _execute(session, stmt, {"since": since, "until": until, "limit": limit})
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        close = _finite(row.get("close"))
        if symbol and close is not None and close > 0:
            grouped.setdefault(symbol, []).append(row)
    evidence: list[NormalizedEvidence] = []
    for symbol, points in grouped.items():
        points.sort(key=lambda row: _timestamp(row.get("timestamp"), since))
        latest = points[-1]
        latest_close = _finite(latest.get("close"))
        if latest_close is None or latest_close <= 0:
            continue
        trailing_returns: dict[int, float | None] = {}
        for sessions in (5, 20, 60):
            prior = (
                _finite(points[-sessions - 1].get("close"))
                if len(points) > sessions
                else None
            )
            trailing_returns[sessions] = (
                ((latest_close / prior - 1.0) * 100.0)
                if prior is not None and prior > 0
                else None
            )
        closes = [
            v
            for row in points[-21:]
            if (v := _finite(row.get("close"))) is not None and v > 0
        ]
        daily_returns = [
            math.log(c / p)
            for p, c in zip(closes, closes[1:], strict=False)
            if p > 0 and c > 0
        ]
        realized_volatility = None
        if len(daily_returns) >= 2:
            mean_return = sum(daily_returns) / len(daily_returns)
            variance = sum((v - mean_return) ** 2 for v in daily_returns) / (
                len(daily_returns) - 1
            )
            realized_volatility = math.sqrt(variance) * math.sqrt(252) * 100.0
        volumes = [
            v
            for row in points[-20:]
            if (v := _finite(row.get("volume"))) is not None and v >= 0
        ]
        average_volume = sum(volumes) / len(volumes) if volumes else None
        latest_volume = _finite(latest.get("volume"))
        volume_ratio = (
            (latest_volume / average_volume)
            if latest_volume is not None
            and average_volume is not None
            and average_volume > 0
            else None
        )
        observed = _timestamp(latest.get("timestamp"), since)
        metadata = _mapping(latest.get("metadata"))
        metrics = {
            "close": latest_close,
            "return_5_session_pct": trailing_returns[5],
            "return_20_session_pct": trailing_returns[20],
            "return_60_session_pct": trailing_returns[60],
            "realized_volatility_20_session_pct": realized_volatility,
            "latest_volume": latest_volume,
            "average_volume_20_session": average_volume,
            "latest_to_average_volume_ratio": volume_ratio,
            "observation_count": len(points),
            "currency": metadata.get("currency"),
        }
        excerpt = (
            f"{symbol} measured daily trend as of {observed.date().isoformat()}: "
            + "; ".join(
                f"{k}={_fmt_number(v)}" for k, v in metrics.items() if v is not None
            )
        )
        evidence.append(
            NormalizedEvidence.create(
                evidence_type=EvidenceType.MARKET_CONFIRMATION,
                evidence_id=f"{symbol}:trend@{observed.isoformat()}",
                source_name=latest.get("source") or "public_equities",
                source_timestamp=observed,
                acquired_at=latest.get("created_at"),
                available_at=_latest_timestamp(
                    _strict_timestamp(metadata.get("available_at")),
                    latest.get("created_at"),
                    latest.get("updated_at"),
                    default=observed,
                ),
                availability_basis="latest_public_daily_bars",
                title=f"{symbol} quantified public-equity trend",
                bounded_excerpt=excerpt,
                source_reference=metadata.get("source_reference"),
                entities=_entities(_entity("symbol", symbol)),
                structured_fields=metrics,
                provenance={
                    "adapter": adapter_name,
                    "source_family": "public_equities",
                    "calculation": {
                        "returns": "close/current divided by lagged close minus one",
                        "realized_volatility": "sample standard deviation of log returns times sqrt(252)",
                        "volume_ratio": "latest volume divided by 20-session mean",
                    },
                    "metadata": metadata,
                },
                freshness=_freshness(observed, since),
            )
        )
    evidence.sort(key=lambda item: (-item.source_timestamp.timestamp(), item.ref))
    return evidence[:limit]


def _transform_expectations_sentiment(
    row: dict[str, Any], since: datetime, adapter_name: str
) -> NormalizedEvidence | None:
    metadata = _mapping(row.get("metadata"))
    symbol = str(metadata.get("ticker") or "").strip().upper()
    if not symbol:
        return None
    observed = _timestamp(row.get("published_at"), since)
    measured = {
        "quarterly_forecasts": _list(metadata.get("quarterly"))[:2],
        "yearly_forecasts": _list(metadata.get("yearly"))[:2],
        "institutional_positioning": _mapping(
            metadata.get("institutional_positioning")
        ),
        "short_interest": _list(metadata.get("short_interest"))[:2],
        "next_earnings": _mapping(metadata.get("next_earnings")),
        "borrow": _mapping(metadata.get("borrow")),
    }
    excerpt = (
        f"{symbol} dated expectations and positioning as of {observed.date().isoformat()}: "
        + json.dumps(measured, sort_keys=True, separators=(",", ":"))
    )
    return NormalizedEvidence.create(
        evidence_type=EvidenceType.MARKET_CONFIRMATION,
        evidence_id=f"expectations:{row.get('document_id')}",
        source_name="nasdaq_public",
        source_timestamp=observed,
        acquired_at=row.get("acquired_at") or row.get("created_at"),
        available_at=_latest_timestamp(
            row.get("acquired_at"),
            row.get("created_at"),
            row.get("updated_at"),
            default=observed,
        ),
        availability_basis="provider_fetch_or_persisted_at",
        title=f"{symbol} measured expectations and positioning",
        bounded_excerpt=excerpt,
        source_reference=row.get("url"),
        entities=_entities(
            _entity("company", row.get("institution")), _entity("symbol", symbol)
        ),
        structured_fields=measured,
        provenance={
            "adapter": adapter_name,
            "source_family": "company_expectations",
            "provider": metadata.get("provider") or "nasdaq",
            "point_in_time": metadata.get("point_in_time") is True,
        },
        freshness=_freshness(observed, since),
    )


def _transform_corporate_action(
    row: dict[str, Any],
    since: datetime,
    adapter_name: str,
    *,
    recovered: bool = False,
    available_by: datetime | None = None,
) -> NormalizedEvidence:
    observed = _timestamp(
        row.get("source_timestamp"), available_by if recovered else since
    )
    amount, num, den = (
        _finite(row.get("amount")),
        _finite(row.get("ratio_numerator")),
        _finite(row.get("ratio_denominator")),
    )
    action_type = str(row.get("action_type") or "")
    if recovered:
        excerpt = f"{row.get('symbol')} {row.get('action_type')} effective {row.get('effective_date')}"
        fields = {
            "action_type": row.get("action_type"),
            "effective_date": row.get("effective_date"),
            "description": row.get("description"),
        }
    else:
        if action_type == "split":
            excerpt = (
                f"{row.get('symbol')} {num:g}/{den:g} split effective {row.get('effective_date')}"
                if num is not None and den is not None
                else f"{row.get('symbol')} split effective {row.get('effective_date')}"
            )
        else:
            excerpt = (
                f"{row.get('symbol')} dividend {amount:g} per share effective {row.get('effective_date')}"
                if amount is not None
                else f"{row.get('symbol')} dividend effective {row.get('effective_date')}"
            )
        fields = {
            "action_type": action_type or None,
            "effective_date": row.get("effective_date"),
            "amount": amount,
            "ratio_numerator": num,
            "ratio_denominator": den,
            "description": row.get("description"),
        }
    prov = {
        "adapter": adapter_name,
        "metadata": _mapping(row.get("metadata")),
        "immutable_snapshot": True,
    }
    if recovered:
        prov["recovered_by_exact_id"] = True
    return NormalizedEvidence.create(
        evidence_type=EvidenceType.MARKET_CONFIRMATION,
        evidence_id=str(row.get("action_id")),
        source_name=row.get("source") or "corporate_action",
        source_timestamp=observed,
        acquired_at=row.get("created_at"),
        available_at=row.get("available_at"),
        availability_basis="provider_available_at",
        title=f"{row.get('symbol')} {action_type} effective {row.get('effective_date')}",
        bounded_excerpt=excerpt,
        entities=_entities(_entity("symbol", row.get("symbol"))),
        structured_fields=fields,
        provenance=prov,
        freshness=_freshness(
            observed, (available_by or observed) if recovered else since
        ),
    )


def _transform_positioning_report(
    row: dict[str, Any],
    since: datetime,
    adapter_name: str,
    *,
    recovered: bool = False,
    available_by: datetime | None = None,
) -> NormalizedEvidence:
    metadata = _mapping(row.get("metadata"))
    kind = str(metadata.get("positioning_kind") or "").strip() or None
    source_time = _strict_timestamp(metadata.get("source_time"))
    observed = source_time or _date_timestamp(
        row.get("report_date"),
        _timestamp(row.get("created_at"), available_by) if recovered else since,
    )
    if kind == "insider_activity":
        entities = _entities(
            _entity("company", metadata.get("issuer_name")),
            _entity(
                "symbol", metadata.get("issuer_trading_symbol") or row.get("market_id")
            ),
        )
    elif kind in ("short_volume", "short_interest"):
        entities = _entities(_entity("symbol", row.get("market_id")))
    else:
        entities = _entities(_entity("market", row.get("market_id")))
    if recovered:
        excerpt = (
            f"{row.get('market_id')} {row.get('category')} {row.get('report_date')}"
        )
        fields = {
            "market_id": row.get("market_id"),
            "category": row.get("category"),
            "report_date": row.get("report_date"),
            "positioning_kind": kind,
        }
        prov = {
            "adapter": adapter_name,
            "metadata": metadata,
            "recovered_by_exact_id": True,
        }
    else:
        semantics = _POSITIONING_SEMANTICS.get(kind) if kind is not None else None
        excerpt = f"{row.get('market_id')} {row.get('category')} {row.get('report_date')}: short {_fmt_number(row.get('short_positions'))} long {_fmt_number(row.get('long_positions'))} net {_fmt_number(row.get('net_position'))}"
        fields = {
            "market_id": row.get("market_id"),
            "category": row.get("category"),
            "report_date": row.get("report_date"),
            "positioning_kind": kind,
            "source_time_kind": metadata.get("source_time_kind")
            or ("report_date" if source_time is None else "source_time"),
            "long_positions": _finite(row.get("long_positions")),
            "short_positions": _finite(row.get("short_positions")),
            "net_position": _finite(row.get("net_position")),
            "open_interest": _finite(row.get("open_interest")),
            "net_pct_open_interest": _finite(row.get("net_pct_open_interest")),
            "is_short_interest": (None if kind is None else kind == "short_interest"),
            "semantics": semantics
            or "unknown: source did not declare positioning_kind",
        }
        prov = {"adapter": adapter_name, "metadata": metadata}
    return NormalizedEvidence.create(
        evidence_type=EvidenceType.MARKET_CONFIRMATION,
        evidence_id=f"{row.get('source')}:{row.get('market_id')}:{row.get('report_date')}:{row.get('category')}",
        source_name=row.get("source") or "positioning",
        source_timestamp=observed,
        acquired_at=row.get("acquired_at") or row.get("created_at"),
        available_at=_latest_timestamp(
            row.get("acquired_at"),
            row.get("created_at"),
            row.get("updated_at"),
            default=observed,
        ),
        availability_basis="acquired_or_updated_at",
        title=f"{row.get('market_id')} {row.get('category')} {row.get('report_date')}",
        bounded_excerpt=excerpt,
        entities=entities,
        structured_fields=fields,
        provenance=prov,
        freshness=_freshness(
            observed, (available_by or observed) if recovered else since
        ),
    )


def _collect_option_chain_snapshots(
    session: Any, since: datetime, until: datetime | None, limit: int, adapter_name: str
) -> list[NormalizedEvidence]:
    sample_size = 25
    stmt = (
        "WITH ranked AS ("
        " SELECT o.source, o.symbol, o.captured_at, o.source_timestamp, o.contract_symbol, o.expiration, o.strike, o.option_type,"
        " o.bid, o.ask, o.last, o.volume, o.open_interest, o.implied_volatility, o.underlying_price, o.created_at,"
        " ROW_NUMBER() OVER (PARTITION BY o.source, o.symbol, o.captured_at ORDER BY o.volume DESC NULLS LAST, o.contract_symbol) AS rn"
        " FROM option_chain_snapshots AS o WHERE COALESCE(o.source_timestamp, o.captured_at) >= :since"
        " AND (:until IS NULL OR o.captured_at <= :until) AND (:until IS NULL OR o.source_timestamp IS NULL OR o.source_timestamp <= :until)"
        " AND (:until IS NULL OR o.created_at <= :until)),"
        " agg AS ("
        " SELECT r.source, r.symbol, r.captured_at, MIN(r.source_timestamp) AS source_timestamp, MIN(r.created_at) AS created_at,"
        " COUNT(*) AS contract_count, COUNT(*) FILTER (WHERE r.option_type = 'call') AS call_count, COUNT(*) FILTER (WHERE r.option_type = 'put') AS put_count,"
        " COUNT(DISTINCT r.expiration) AS expiration_count, MIN(r.strike) AS min_strike, MAX(r.strike) AS max_strike,"
        " COUNT(r.volume) AS volume_contracts, SUM(r.volume) AS total_volume, COUNT(r.open_interest) AS open_interest_contracts,"
        " SUM(r.open_interest) AS total_open_interest, COUNT(r.implied_volatility) AS iv_contracts, AVG(r.implied_volatility) AS mean_implied_volatility,"
        " COUNT(r.underlying_price) AS underlying_price_contracts, MIN(r.underlying_price) AS min_underlying_price, MAX(r.underlying_price) AS max_underlying_price,"
        " JSON_AGG(JSON_BUILD_OBJECT('contract_symbol', r.contract_symbol, 'expiration', r.expiration::TEXT, 'strike', r.strike, 'option_type', r.option_type,"
        " 'bid', r.bid, 'ask', r.ask, 'last', r.last, 'volume', r.volume, 'open_interest', r.open_interest, 'implied_volatility', r.implied_volatility)"
        " ORDER BY r.volume DESC NULLS LAST, r.contract_symbol) FILTER (WHERE r.rn <= :sample_size) AS contract_sample,"
        " f.feature_version, f.available_at AS feature_available_at, f.created_at AS feature_created_at, f.contract_count AS feature_contract_count,"
        " f.analytics AS feature_analytics, f.metadata AS feature_metadata"
        " FROM ranked AS r LEFT JOIN option_snapshot_features AS f ON f.source = r.source AND f.symbol = r.symbol AND f.captured_at = r.captured_at"
        " AND (:until IS NULL OR f.available_at <= :until) AND (:until IS NULL OR f.created_at <= :until)"
        " GROUP BY r.source, r.symbol, r.captured_at, f.feature_version, f.available_at, f.created_at, f.contract_count, f.analytics, f.metadata)"
        " SELECT * FROM agg ORDER BY captured_at DESC, symbol, source LIMIT :limit"
    )
    rows = _execute(
        session,
        stmt,
        {"since": since, "until": until, "limit": limit, "sample_size": sample_size},
    )
    evidence: list[NormalizedEvidence] = []
    for row in rows:
        captured = _timestamp(row.get("captured_at"), since)
        observed = _strict_timestamp(row.get("source_timestamp")) or captured
        contract_count, call_count, put_count = (
            _count(row.get("contract_count")) or 0,
            _count(row.get("call_count")) or 0,
            _count(row.get("put_count")) or 0,
        )
        expiration_count = _count(row.get("expiration_count")) or 0
        total_volume, total_open_interest = (
            _finite(row.get("total_volume")),
            _finite(row.get("total_open_interest")),
        )
        sample = _list(row.get("contract_sample"))[:sample_size]
        feature_version = row.get("feature_version")
        feature_state = (
            "available" if isinstance(feature_version, str) else "unavailable"
        )
        feature_available_at = _strict_timestamp(row.get("feature_available_at"))
        feature_created_at = _strict_timestamp(row.get("feature_created_at"))
        analytics = _mapping(row.get("feature_analytics"))
        expiries = _list(analytics.get("expiries"))
        nearest = expiries[0] if expiries and isinstance(expiries[0], Mapping) else None
        nearest_atm = (
            nearest.get("atm")
            if nearest and isinstance(nearest.get("atm"), Mapping)
            else None
        )
        skew = (
            nearest.get("put_call_skew")
            if nearest and isinstance(nearest.get("put_call_skew"), Mapping)
            else None
        )
        unusualness = (
            analytics.get("unusualness")
            if isinstance(analytics.get("unusualness"), Mapping)
            else None
        )
        totals = (
            analytics.get("totals")
            if isinstance(analytics.get("totals"), Mapping)
            else None
        )
        term_structure = _list(analytics.get("term_structure"))[:40]
        avail_at = _latest_timestamp(
            captured,
            row.get("created_at"),
            feature_available_at,
            feature_created_at,
            default=captured,
        )
        evidence.append(
            NormalizedEvidence.create(
                evidence_type=EvidenceType.MARKET_CONFIRMATION,
                evidence_id=f"{row.get('source')}:{row.get('symbol')}@{captured.isoformat()}",
                source_name=row.get("source") or "options",
                source_timestamp=observed,
                acquired_at=row.get("created_at"),
                available_at=avail_at,
                availability_basis="snapshot_captured_at"
                if feature_state == "unavailable"
                else "snapshot_captured_at_and_feature_available",
                title=f"{row.get('symbol')} options chain snapshot {captured.isoformat()}",
                bounded_excerpt=(
                    f"{row.get('symbol')} options snapshot at {captured.isoformat()}: {contract_count} contracts across {expiration_count} expirations "
                    f"({call_count} calls / {put_count} puts), total volume {_fmt_number(total_volume)}, open interest {_fmt_number(total_open_interest)}, "
                    f"analytics {analytics.get('state') or feature_state}"
                ),
                entities=_entities(_entity("symbol", row.get("symbol"))),
                structured_fields={
                    "symbol": row.get("symbol"),
                    "captured_at": row.get("captured_at"),
                    "contract_count": contract_count,
                    "call_count": call_count,
                    "put_count": put_count,
                    "expiration_count": expiration_count,
                    "min_strike": _finite(row.get("min_strike")),
                    "max_strike": _finite(row.get("max_strike")),
                    "volume_contracts": _count(row.get("volume_contracts")) or 0,
                    "total_volume": total_volume,
                    "open_interest_contracts": _count(
                        row.get("open_interest_contracts")
                    )
                    or 0,
                    "total_open_interest": total_open_interest,
                    "iv_contracts": _count(row.get("iv_contracts")) or 0,
                    "mean_implied_volatility": _finite(
                        row.get("mean_implied_volatility")
                    ),
                    "underlying_price_contracts": _count(
                        row.get("underlying_price_contracts")
                    )
                    or 0,
                    "min_underlying_price": _finite(row.get("min_underlying_price")),
                    "max_underlying_price": _finite(row.get("max_underlying_price")),
                    "contracts": sample,
                    "aggregate_notes": "counts and sums over provider-reported values; missing quotes are excluded, never backfilled",
                    "feature_state": feature_state,
                    "feature_version": feature_version,
                    "feature_available_at": feature_available_at.isoformat()
                    if feature_available_at is not None
                    else None,
                    "feature_contract_count": _count(row.get("feature_contract_count")),
                    "analytics_state": analytics.get("state"),
                    "analytics_reason": analytics.get("reason"),
                    "analytics_underlying_price": _finite(
                        analytics.get("underlying_price")
                    ),
                    "atm_iv": _finite(nearest_atm.get("iv")) if nearest_atm else None,
                    "atm_strike": _finite(nearest_atm.get("strike"))
                    if nearest_atm
                    else None,
                    "atm_state": nearest_atm.get("state") if nearest_atm else None,
                    "implied_move_pct": _finite(nearest.get("implied_move_pct"))
                    if nearest
                    else None,
                    "implied_move_method": nearest.get("implied_move_method")
                    if nearest
                    else None,
                    "put_call_skew": _finite(skew.get("value")) if skew else None,
                    "put_call_skew_state": skew.get("state") if skew else None,
                    "term_structure_state": analytics.get("term_structure_state"),
                    "term_structure_reason": analytics.get("term_structure_reason"),
                    "term_structure": term_structure,
                    "unusualness_state": unusualness.get("state")
                    if unusualness
                    else None,
                    "unusualness_reason": unusualness.get("reason")
                    if unusualness
                    else None,
                    "unusual_volume": unusualness.get("unusual_volume")
                    if unusualness
                    else None,
                    "unusual_open_interest": unusualness.get("unusual_open_interest")
                    if unusualness
                    else None,
                    "volume_percentile": _finite(unusualness.get("volume_percentile"))
                    if unusualness
                    else None,
                    "open_interest_percentile": _finite(
                        unusualness.get("open_interest_percentile")
                    )
                    if unusualness
                    else None,
                    "analytics_volume": _finite(totals.get("volume"))
                    if totals
                    else None,
                    "analytics_open_interest": _finite(totals.get("open_interest"))
                    if totals
                    else None,
                    "analytics_volume_complete": totals.get("volume_complete")
                    if totals
                    else None,
                    "analytics_oi_complete": totals.get("oi_complete")
                    if totals
                    else None,
                    "feature_notes": (
                        "analytics values are read verbatim from the immutable option_snapshot_features row; "
                        "when the feature row is unavailable (state 'unavailable') no ATM IV, move, skew or unusualness is invented, "
                        "and no dealer-gamma inference is performed"
                    ),
                },
                provenance={
                    "adapter": adapter_name,
                    "source_family": row.get("source"),
                    "aggregation": "per_source_symbol_captured_at",
                    "sample_size": sample_size,
                    "immutable_snapshot": True,
                    "feature_version": feature_version,
                    "analytics_source": "option_snapshot_features"
                    if feature_state == "available"
                    else None,
                },
                freshness=_freshness(observed, since),
            )
        )
    return evidence


@dataclass(frozen=True, slots=True)
class AdapterSpec:
    name: str
    query: str | None = None
    row_mapper: (
        Callable[[dict[str, Any], datetime, str], NormalizedEvidence | None] | None
    ) = None
    collector: (
        Callable[[Any, datetime, datetime | None, int, str], list[NormalizedEvidence]]
        | None
    ) = None


_ADAPTER_SPECS: dict[str, AdapterSpec] = {
    "macro_observations": AdapterSpec(
        name="macro_observations",
        query=(
            "SELECT m.series_id, m.observed_at, m.value, m.source, m.released_at, m.revision_at, m.metadata, m.created_at, m.updated_at,"
            " md.title, md.units, md.frequency FROM macro_series AS m LEFT JOIN macro_series_metadata AS md ON md.series_id = m.series_id"
            " WHERE COALESCE(m.revision_at, m.released_at, m.observed_at) >= :since"
            " AND (:until IS NULL OR COALESCE(m.revision_at, m.released_at, m.observed_at) <= :until)"
            " AND (:until IS NULL OR m.created_at <= :until) AND (:until IS NULL OR m.updated_at <= :until)"
            " ORDER BY COALESCE(m.revision_at, m.released_at, m.observed_at) DESC, m.series_id LIMIT :limit"
        ),
        row_mapper=_transform_macro_observation,
    ),
    "official_documents": AdapterSpec(
        name="official_documents", collector=_collect_official_documents
    ),
    "macro_releases": AdapterSpec(
        name="macro_releases",
        query=(
            "WITH eligible AS (SELECT c.* FROM macro_release_cards AS c"
            " WHERE COALESCE(c.revision_at, c.released_at, c.observed_at, c.created_at) >= :since"
            " AND (:until IS NULL OR COALESCE(c.revision_at, c.released_at, c.observed_at, c.created_at) <= :until)"
            " AND (:until IS NULL OR c.created_at <= :until)),"
            " latest AS (SELECT DISTINCT ON (release_identity) * FROM eligible"
            " ORDER BY release_identity, COALESCE(revision_at, released_at, observed_at, created_at) DESC, revision_number DESC, created_at DESC)"
            " SELECT id, release_identity, series_id, event_name, actual, consensus, previous, revised_previous, absolute_surprise, standardized_surprise,"
            " impact, source, observed_at, released_at, revision_at, revision_number, quality_flags, stage, reaction_summary, created_at"
            " FROM latest ORDER BY COALESCE(revision_at, released_at, observed_at, created_at) DESC, id LIMIT :limit"
        ),
        row_mapper=_transform_macro_release,
    ),
    "market_state": AdapterSpec(
        name="market_state",
        query=(
            "SELECT symbol, as_of, source_event_id, features, unavailable, created_at FROM market_feature_snapshots"
            " WHERE as_of >= :since AND (:until IS NULL OR as_of <= :until) AND (:until IS NULL OR created_at <= :until)"
            " ORDER BY as_of DESC, symbol LIMIT :limit"
        ),
        row_mapper=_transform_market_state,
    ),
    "story_clusters": AdapterSpec(
        name="story_clusters", collector=_collect_story_clusters
    ),
    "market_confirmations": AdapterSpec(
        name="market_confirmations", collector=_collect_market_confirmations
    ),
    "investment_observations": AdapterSpec(
        name="investment_observations",
        query=(
            "SELECT observation_id, source_kind, source_id, observed_at, industry, company, symbol, region, metrics, narrative, themes, score, state, provenance, created_at, updated_at"
            " FROM investment_research_observations WHERE observed_at >= :since AND (:until IS NULL OR observed_at <= :until) AND (:until IS NULL OR updated_at <= :until)"
            " ORDER BY observed_at DESC, observation_id LIMIT :limit"
        ),
        row_mapper=_transform_investment_observation,
    ),
    "filing_deltas": AdapterSpec(
        name="filing_deltas",
        query=(
            "SELECT f.id, f.document_id, f.previous_document_id, f.category, f.change_kind, f.section_hash, f.previous_section_hash,"
            " f.excerpt, f.previous_excerpt, f.metrics, f.created_at, d.company, d.symbol, d.industry, d.region, d.report_date, d.source_url, d.filing_source"
            " FROM investment_filing_deltas AS f JOIN investment_documents AS d ON d.document_id = f.document_id"
            " WHERE f.created_at >= :since AND f.change_kind <> 'unchanged' AND (:until IS NULL OR f.created_at <= :until)"
            " ORDER BY f.created_at DESC, f.id LIMIT :limit"
        ),
        row_mapper=_transform_filing_delta,
    ),
    "investment_analyses": AdapterSpec(
        name="investment_analyses",
        query=(
            "SELECT a.analysis_id, a.document_id, a.previous_document_id, a.facts, a.analysis, a.model, a.created_at, a.updated_at,"
            " d.company, d.symbol, d.industry, d.region, d.document_type, d.report_date, d.source_url, d.filing_source"
            " FROM investment_analyses AS a JOIN investment_documents AS d ON d.document_id = a.document_id"
            " WHERE a.updated_at >= :since AND (:until IS NULL OR a.updated_at <= :until) ORDER BY a.updated_at DESC, a.analysis_id LIMIT :limit"
        ),
        row_mapper=_transform_investment_analysis,
    ),
    "source_claims": AdapterSpec(
        name="source_claims",
        query=(
            "SELECT id, evidence_type, evidence_id, subject, predicate, object_value, unit, period, geography, direction, claim_kind,"
            " source_span, observed_at, confidence, entities, model_slug, prompt_version, input_fingerprint, provenance, created_at"
            " FROM research_source_claims WHERE observed_at >= :since AND (:until IS NULL OR created_at <= :until) ORDER BY observed_at DESC, id LIMIT :limit"
        ),
        row_mapper=_transform_source_claim,
    ),
    "public_equities": AdapterSpec(
        name="public_equities",
        query=(
            "SELECT symbol, timeframe, timestamp, open, high, low, close, volume, source, metadata, created_at, updated_at"
            " FROM market_data WHERE timestamp >= :since AND (:until IS NULL OR timestamp <= :until) AND (:until IS NULL OR created_at <= :until) AND (:until IS NULL OR updated_at <= :until)"
            " ORDER BY timestamp DESC, symbol, timeframe LIMIT :limit"
        ),
        row_mapper=_transform_public_equities,
    ),
    "public_equity_trends": AdapterSpec(
        name="public_equity_trends", collector=_collect_public_equity_trends
    ),
    "expectations_sentiment": AdapterSpec(
        name="expectations_sentiment",
        query=(
            "WITH ranked AS ("
            " SELECT document_id, institution, title, published_at, acquired_at, url, metadata, created_at, updated_at,"
            " ROW_NUMBER() OVER (PARTITION BY COALESCE(metadata->>'ticker', institution) ORDER BY published_at DESC, document_id) AS source_rank"
            " FROM source_documents WHERE source = 'company_expectations' AND published_at >= :since"
            " AND (:until IS NULL OR published_at <= :until) AND (:until IS NULL OR COALESCE(acquired_at, created_at) <= :until) AND (:until IS NULL OR updated_at <= :until))"
            " SELECT document_id, institution, title, published_at, acquired_at, url, metadata, created_at, updated_at FROM ranked"
            " WHERE source_rank = 1 ORDER BY published_at DESC, document_id LIMIT :limit"
        ),
        row_mapper=_transform_expectations_sentiment,
    ),
    "corporate_actions": AdapterSpec(
        name="corporate_actions",
        query=(
            "SELECT action_id, symbol, action_type, effective_date, source, source_timestamp, available_at, amount, ratio_numerator, ratio_denominator, description, metadata, created_at"
            " FROM corporate_actions WHERE source_timestamp >= :since AND (:until IS NULL OR source_timestamp <= :until) AND (:until IS NULL OR available_at <= :until) AND (:until IS NULL OR created_at <= :until)"
            " ORDER BY source_timestamp DESC, action_id LIMIT :limit"
        ),
        row_mapper=_transform_corporate_action,
    ),
    "positioning_reports": AdapterSpec(
        name="positioning_reports",
        query=(
            "SELECT source, market_id, report_date, category, long_positions, short_positions, net_position, open_interest, net_pct_open_interest, metadata, created_at, updated_at, acquired_at"
            " FROM positioning_reports WHERE report_date::TIMESTAMPTZ >= :since AND (:until IS NULL OR report_date::TIMESTAMPTZ <= :until) AND (:until IS NULL OR COALESCE(acquired_at, created_at) <= :until) AND (:until IS NULL OR updated_at <= :until)"
            " ORDER BY report_date DESC, source, market_id, category LIMIT :limit"
        ),
        row_mapper=_transform_positioning_report,
    ),
    "option_chain_snapshots": AdapterSpec(
        name="option_chain_snapshots", collector=_collect_option_chain_snapshots
    ),
}


class GenericEvidenceAdapter:
    name: str = ""
    spec: AdapterSpec | None = None

    def __init__(self, spec: AdapterSpec | None = None) -> None:
        if spec is not None:
            self.spec = spec
            self.name = spec.name
        elif getattr(self, "spec", None) is None:
            name = getattr(self, "name", "")
            if name in _ADAPTER_SPECS:
                self.spec = _ADAPTER_SPECS[name]

    def collect(
        self,
        session: Any,
        *,
        since: datetime,
        until: datetime | None = None,
        limit: int,
    ) -> list[NormalizedEvidence]:
        spec = self.spec
        if spec is None:
            return []
        if spec.collector is not None:
            return spec.collector(session, since, until, limit, self.name)
        if spec.query is not None and spec.row_mapper is not None:
            rows = _execute(
                session, spec.query, {"since": since, "until": until, "limit": limit}
            )
            items: list[NormalizedEvidence] = []
            for row in rows:
                item = spec.row_mapper(row, since, self.name)
                if item is not None:
                    items.append(item)
            return items
        return []


class MacroObservationAdapter(GenericEvidenceAdapter):
    name = "macro_observations"


class OfficialDocumentAdapter(GenericEvidenceAdapter):
    name = "official_documents"


class MacroReleaseAdapter(GenericEvidenceAdapter):
    name = "macro_releases"


class MarketStateAdapter(GenericEvidenceAdapter):
    name = "market_state"


class StoryClusterAdapter(GenericEvidenceAdapter):
    name = "story_clusters"


class MarketConfirmationAdapter(GenericEvidenceAdapter):
    name = "market_confirmations"


class InvestmentObservationAdapter(GenericEvidenceAdapter):
    name = "investment_observations"


class FilingDeltaAdapter(GenericEvidenceAdapter):
    name = "filing_deltas"


class InvestmentAnalysisAdapter(GenericEvidenceAdapter):
    name = "investment_analyses"


class SourceClaimAdapter(GenericEvidenceAdapter):
    name = "source_claims"


class PublicEquitiesAdapter(GenericEvidenceAdapter):
    name = "public_equities"


class PublicEquityTrendAdapter(GenericEvidenceAdapter):
    name = "public_equity_trends"


class ExpectationsSentimentAdapter(GenericEvidenceAdapter):
    name = "expectations_sentiment"


class CorporateActionAdapter(GenericEvidenceAdapter):
    name = "corporate_actions"


class PositioningReportAdapter(GenericEvidenceAdapter):
    name = "positioning_reports"


class OptionChainSnapshotAdapter(GenericEvidenceAdapter):
    name = "option_chain_snapshots"
    SAMPLE_SIZE = 25


DEFAULT_ADAPTERS: tuple[EvidenceAdapter, ...] = (
    MacroObservationAdapter(),
    MacroReleaseAdapter(),
    MarketStateAdapter(),
    StoryClusterAdapter(),
    OfficialDocumentAdapter(),
    ExpectationsSentimentAdapter(),
    MarketConfirmationAdapter(),
    InvestmentObservationAdapter(),
    FilingDeltaAdapter(),
    InvestmentAnalysisAdapter(),
    SourceClaimAdapter(),
    PublicEquitiesAdapter(),
    PublicEquityTrendAdapter(),
    CorporateActionAdapter(),
    PositioningReportAdapter(),
    OptionChainSnapshotAdapter(),
)

_SEGMENT_ID_RE = re.compile(r"^(.+):seg([1-9][0-9]*)$")
_AT_ID_RE = re.compile(r"^([^:]+):([^:]+)@(.+)$")
_POSITIONING_ID_RE = re.compile(r"^([^:]+):([^:]+):([0-9]{4}-[0-9]{2}-[0-9]{2}):(.+)$")


def _recover_official_documents(
    session: Any, identifiers: Sequence[str], *, available_by: datetime | None
) -> list[NormalizedEvidence]:
    wanted: dict[str, tuple[str, int | None]] = {}
    for ident in identifiers:
        if ident not in wanted:
            match = _SEGMENT_ID_RE.fullmatch(ident)
            wanted[ident] = (
                (match.group(1), int(match.group(2))) if match else (ident, None)
            )
    stmt = (
        "/* autonomy_identity_recovery:source_documents */"
        " SELECT document_id, source, institution, document_type, title, published_at, url, content, metadata, created_at, updated_at, acquired_at"
        " FROM source_documents WHERE document_id = ANY(:ids)"
        " AND (:available_by IS NULL OR published_at <= :available_by)"
        " AND (:available_by IS NULL OR COALESCE(acquired_at, created_at) <= :available_by)"
        " AND (:available_by IS NULL OR updated_at <= :available_by) ORDER BY document_id"
    )
    rows = _recover_execute(
        session,
        stmt,
        {"ids": sorted({p for p, _ in wanted.values()}), "available_by": available_by},
    )
    rows_by_id = {str(r.get("document_id")): r for r in rows}
    items: list[NormalizedEvidence] = []
    for ident in identifiers:
        parent, segment_index = wanted[ident]
        row = rows_by_id.get(parent)
        if row is None:
            continue
        if segment_index is None:
            item = _build_official_doc_item(
                row,
                available_by or datetime.min.replace(tzinfo=UTC),
                OfficialDocumentAdapter.name,
                identifier=ident,
                recovered=True,
                available_by=available_by,
            )
            if item is not None:
                items.append(item)
        else:
            content = str(row.get("content") or "")
            segments = _transcript_segments(content)
            source = str(row.get("source") or "").strip()
            if source == "issuer_transcripts" and segment_index <= len(segments):
                kind, start, end = segments[segment_index - 1]
                item = _build_official_doc_item(
                    row,
                    available_by or datetime.min.replace(tzinfo=UTC),
                    OfficialDocumentAdapter.name,
                    identifier=ident,
                    plan_item=(segment_index, kind, start, end),
                    recovered=True,
                    available_by=available_by,
                )
                if item is not None:
                    items.append(item)
    return items


def _recover_source_claims(
    session: Any, identifiers: Sequence[str], *, available_by: datetime | None
) -> list[NormalizedEvidence]:
    stmt = (
        "/* autonomy_identity_recovery:research_source_claims */"
        " SELECT id, evidence_type, evidence_id, subject, predicate, object_value, unit, period, geography, direction, claim_kind,"
        " source_span, observed_at, confidence, entities, model_slug, prompt_version, input_fingerprint, provenance, created_at"
        " FROM research_source_claims WHERE id::TEXT = ANY(:ids) AND (:available_by IS NULL OR created_at <= :available_by) ORDER BY id"
    )
    rows = _recover_execute(
        session, stmt, {"ids": sorted(set(identifiers)), "available_by": available_by}
    )
    return [
        _transform_source_claim(
            r,
            available_by or datetime.min.replace(tzinfo=UTC),
            SourceClaimAdapter.name,
            recovered=True,
            available_by=available_by,
        )
        for r in rows
    ]


def _recover_filing_deltas(
    session: Any, identifiers: Sequence[str], *, available_by: datetime | None
) -> list[NormalizedEvidence]:
    stmt = (
        "/* autonomy_identity_recovery:investment_filing_deltas */"
        " SELECT f.id, f.document_id, f.previous_document_id, f.category, f.change_kind, f.section_hash, f.previous_section_hash,"
        " f.excerpt, f.previous_excerpt, f.metrics, f.created_at, d.company, d.symbol, d.industry, d.region, d.report_date, d.source_url, d.filing_source"
        " FROM investment_filing_deltas AS f JOIN investment_documents AS d ON d.document_id = f.document_id"
        " WHERE f.id::TEXT = ANY(:ids) AND f.change_kind <> 'unchanged' AND (:available_by IS NULL OR f.created_at <= :available_by) ORDER BY f.id"
    )
    rows = _recover_execute(
        session, stmt, {"ids": sorted(set(identifiers)), "available_by": available_by}
    )
    return [
        _transform_filing_delta(
            r,
            available_by or datetime.min.replace(tzinfo=UTC),
            FilingDeltaAdapter.name,
            recovered=True,
            available_by=available_by,
        )
        for r in rows
    ]


def _recover_investment_observations(
    session: Any, identifiers: Sequence[str], *, available_by: datetime | None
) -> list[NormalizedEvidence]:
    stmt = (
        "/* autonomy_identity_recovery:investment_research_observations */"
        " SELECT observation_id, source_kind, source_id, observed_at, industry, company, symbol, region, metrics, narrative, themes, score,"
        " state, provenance, created_at, updated_at FROM investment_research_observations WHERE observation_id::TEXT = ANY(:ids)"
        " AND (:available_by IS NULL OR observed_at <= :available_by) AND (:available_by IS NULL OR updated_at <= :available_by) ORDER BY observation_id"
    )
    rows = _recover_execute(
        session, stmt, {"ids": sorted(set(identifiers)), "available_by": available_by}
    )
    return [
        _transform_investment_observation(
            r,
            available_by or datetime.min.replace(tzinfo=UTC),
            InvestmentObservationAdapter.name,
            recovered=True,
            available_by=available_by,
        )
        for r in rows
    ]


def _recover_investment_analyses(
    session: Any, identifiers: Sequence[str], *, available_by: datetime | None
) -> list[NormalizedEvidence]:
    stmt = (
        "/* autonomy_identity_recovery:investment_analyses */"
        " SELECT a.analysis_id, a.document_id, a.previous_document_id, a.facts, a.analysis, a.model, a.created_at, a.updated_at,"
        " d.company, d.symbol, d.industry, d.region, d.document_type, d.report_date, d.source_url, d.filing_source"
        " FROM investment_analyses AS a JOIN investment_documents AS d ON d.document_id = a.document_id"
        " WHERE a.analysis_id::TEXT = ANY(:ids) AND (:available_by IS NULL OR a.updated_at <= :available_by) ORDER BY a.analysis_id"
    )
    rows = _recover_execute(
        session, stmt, {"ids": sorted(set(identifiers)), "available_by": available_by}
    )
    return [
        _transform_investment_analysis(
            r,
            available_by or datetime.min.replace(tzinfo=UTC),
            InvestmentAnalysisAdapter.name,
            recovered=True,
            available_by=available_by,
        )
        for r in rows
    ]


def _pick_recovery_row(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    def availability(row: Mapping[str, Any]) -> datetime:
        return _latest_timestamp(
            row.get("available_at"),
            row.get("created_at"),
            row.get("updated_at"),
            default=datetime.min.replace(tzinfo=UTC),
        )

    return dict(
        sorted(
            rows,
            key=lambda row: (availability(row), str(row.get("source"))),
            reverse=True,
        )[0]
    )


def _recover_market_confirmations(
    session: Any, identifiers: Sequence[str], *, available_by: datetime | None
) -> list[NormalizedEvidence]:
    bar_keys, option_keys, position_keys, corporate_ids = {}, {}, {}, []
    for identifier in identifiers:
        if identifier.startswith("story:") or identifier.startswith("release:"):
            continue
        if (at_match := _AT_ID_RE.fullmatch(identifier)) is not None:
            first, second, iso = at_match.groups()
            if _strict_timestamp(iso) is not None:
                bar_keys.setdefault(identifier, (first, second, iso))
                option_keys.setdefault(identifier, (first, second, iso))
            continue
        if (position_match := _POSITIONING_ID_RE.fullmatch(identifier)) is not None:
            source, market_id, report_date, category = position_match.groups()
            position_keys.setdefault(
                identifier, (source, market_id, report_date, category)
            )
            continue
        corporate_ids.append(identifier)

    items_by_ref: dict[str, list[NormalizedEvidence]] = {}
    if bar_keys:
        symbols, timeframes, timestamps = (
            [k[0] for k in bar_keys.values()],
            [k[1] for k in bar_keys.values()],
            [k[2] for k in bar_keys.values()],
        )
        bar_id_by_key = {
            (symbol, timeframe, iso): identifier
            for identifier, (symbol, timeframe, iso) in bar_keys.items()
        }
        stmt = (
            "/* autonomy_identity_recovery:market_data */"
            " SELECT symbol, timeframe, timestamp, open, high, low, close, volume, source, metadata, created_at, updated_at FROM market_data"
            " WHERE (symbol, timeframe, timestamp) IN (SELECT * FROM unnest(CAST(:symbols AS TEXT[]), CAST(:timeframes AS TEXT[]), CAST(:timestamps AS TIMESTAMPTZ[])))"
            " AND (:available_by IS NULL OR created_at <= :available_by) AND (:available_by IS NULL OR updated_at <= :available_by)"
            " ORDER BY symbol, timeframe, timestamp, source"
        )
        rows = _recover_execute(
            session,
            stmt,
            {
                "symbols": symbols,
                "timeframes": timeframes,
                "timestamps": timestamps,
                "available_by": available_by,
            },
        )
        bar_groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            key = (
                str(row.get("symbol")),
                str(row.get("timeframe")),
                _timestamp(row.get("timestamp")).isoformat(),
            )
            if (identifier := bar_id_by_key.get(key)) is not None:
                bar_groups.setdefault(identifier, []).append(dict(row))
        for identifier, matched in bar_groups.items():
            row = _pick_recovery_row(matched)
            bar_time = _timestamp(row.get("timestamp"))
            items_by_ref.setdefault(identifier, []).append(
                NormalizedEvidence.create(
                    evidence_type=EvidenceType.MARKET_CONFIRMATION,
                    evidence_id=identifier,
                    source_name=row.get("source") or "market_data",
                    source_timestamp=bar_time,
                    acquired_at=row.get("created_at"),
                    available_at=_latest_timestamp(
                        row.get("created_at"), row.get("updated_at"), default=bar_time
                    ),
                    availability_basis="provider_fetch_or_persisted_at",
                    title=f"{row.get('symbol')} {row.get('timeframe')} bar {bar_time.isoformat()}",
                    bounded_excerpt=f"{row.get('symbol')} {row.get('timeframe')} bar {bar_time.isoformat()}",
                    entities=_entities(_entity("symbol", row.get("symbol"))),
                    structured_fields={
                        "timeframe": row.get("timeframe"),
                        "source": row.get("source"),
                    },
                    provenance={
                        "adapter": PublicEquitiesAdapter.name,
                        "metadata": _mapping(row.get("metadata")),
                        "recovered_by_exact_id": True,
                    },
                    freshness=_freshness(bar_time, available_by or bar_time),
                )
            )
    if option_keys:
        sources, symbols, captured_ats = (
            [k[0] for k in option_keys.values()],
            [k[1] for k in option_keys.values()],
            [k[2] for k in option_keys.values()],
        )
        option_id_by_key = {
            (source, symbol, iso): identifier
            for identifier, (source, symbol, iso) in option_keys.items()
        }
        stmt = (
            "/* autonomy_identity_recovery:option_chain_snapshots */"
            " SELECT source, symbol, captured_at, source_timestamp, created_at FROM option_chain_snapshots"
            " WHERE (source, symbol, captured_at) IN (SELECT * FROM unnest(CAST(:sources AS TEXT[]), CAST(:symbols AS TEXT[]), CAST(:captured_ats AS TIMESTAMPTZ[])))"
            " AND (:available_by IS NULL OR captured_at <= :available_by)"
            " AND (:available_by IS NULL OR source_timestamp IS NULL OR source_timestamp <= :available_by)"
            " AND (:available_by IS NULL OR created_at <= :available_by) ORDER BY source, symbol, captured_at"
        )
        rows = _recover_execute(
            session,
            stmt,
            {
                "sources": sources,
                "symbols": symbols,
                "captured_ats": captured_ats,
                "available_by": available_by,
            },
        )
        option_groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            key = (
                str(row.get("source")),
                str(row.get("symbol")),
                _timestamp(row.get("captured_at")).isoformat(),
            )
            if (identifier := option_id_by_key.get(key)) is not None:
                option_groups.setdefault(identifier, []).append(dict(row))
        for identifier, matched in option_groups.items():
            row = _pick_recovery_row(matched)
            captured = _timestamp(row.get("captured_at"))
            items_by_ref.setdefault(identifier, []).append(
                NormalizedEvidence.create(
                    evidence_type=EvidenceType.MARKET_CONFIRMATION,
                    evidence_id=identifier,
                    source_name=row.get("source") or "options",
                    source_timestamp=_timestamp(row.get("source_timestamp"), captured),
                    acquired_at=row.get("created_at"),
                    available_at=_latest_timestamp(
                        captured, row.get("created_at"), default=captured
                    ),
                    availability_basis="snapshot_captured_at",
                    title=f"{row.get('symbol')} options chain snapshot {captured.isoformat()}",
                    bounded_excerpt=f"{row.get('symbol')} options snapshot {captured.isoformat()}",
                    entities=_entities(_entity("symbol", row.get("symbol"))),
                    structured_fields={
                        "symbol": row.get("symbol"),
                        "captured_at": row.get("captured_at"),
                    },
                    provenance={
                        "adapter": OptionChainSnapshotAdapter.name,
                        "source_family": row.get("source"),
                        "recovered_by_exact_id": True,
                    },
                    freshness=_freshness(captured, available_by or captured),
                )
            )
    if position_keys:
        sources, market_ids, report_dates, categories = (
            [k[0] for k in position_keys.values()],
            [k[1] for k in position_keys.values()],
            [k[2] for k in position_keys.values()],
            [k[3] for k in position_keys.values()],
        )
        position_id_by_key = {
            (source, market_id, report_date, category): identifier
            for identifier, (
                source,
                market_id,
                report_date,
                category,
            ) in position_keys.items()
        }
        stmt = (
            "/* autonomy_identity_recovery:positioning_reports */"
            " SELECT source, market_id, report_date, category, metadata, created_at, updated_at, acquired_at FROM positioning_reports"
            " WHERE (source, market_id, report_date, category) IN (SELECT * FROM unnest(CAST(:sources AS TEXT[]), CAST(:market_ids AS TEXT[]), CAST(:report_dates AS DATE[]), CAST(:categories AS TEXT[])))"
            " AND (:available_by IS NULL OR report_date::TIMESTAMPTZ <= :available_by)"
            " AND (:available_by IS NULL OR COALESCE(acquired_at, created_at) <= :available_by)"
            " AND (:available_by IS NULL OR updated_at <= :available_by) ORDER BY source, market_id, report_date, category"
        )
        rows = _recover_execute(
            session,
            stmt,
            {
                "sources": sources,
                "market_ids": market_ids,
                "report_dates": report_dates,
                "categories": categories,
                "available_by": available_by,
            },
        )
        position_groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            key = (
                str(row.get("source")),
                str(row.get("market_id")),
                str(row.get("report_date")),
                str(row.get("category")),
            )
            if (identifier := position_id_by_key.get(key)) is not None:
                position_groups.setdefault(identifier, []).append(dict(row))
        for identifier, matched in position_groups.items():
            row = _pick_recovery_row(matched)
            item = _transform_positioning_report(
                row,
                available_by or datetime.min.replace(tzinfo=UTC),
                PositioningReportAdapter.name,
                recovered=True,
                available_by=available_by,
            )
            items_by_ref.setdefault(identifier, []).append(item)
    if corporate_ids:
        stmt = (
            "/* autonomy_identity_recovery:corporate_actions */"
            " SELECT action_id, symbol, action_type, effective_date, source, source_timestamp, available_at, description, metadata, created_at"
            " FROM corporate_actions WHERE action_id = ANY(:ids)"
            " AND (:available_by IS NULL OR source_timestamp <= :available_by)"
            " AND (:available_by IS NULL OR available_at <= :available_by)"
            " AND (:available_by IS NULL OR created_at <= :available_by) ORDER BY action_id"
        )
        rows = _recover_execute(
            session,
            stmt,
            {"ids": sorted(set(corporate_ids)), "available_by": available_by},
        )
        for row in rows:
            identifier = str(row.get("action_id"))
            item = _transform_corporate_action(
                row,
                available_by or datetime.min.replace(tzinfo=UTC),
                CorporateActionAdapter.name,
                recovered=True,
                available_by=available_by,
            )
            items_by_ref.setdefault(identifier, []).append(item)
    items: list[NormalizedEvidence] = []
    for _identifier, candidates in items_by_ref.items():
        if len(candidates) == 1:
            items.append(candidates[0])
    return items


def _recover_story_clusters(
    session: Any, identifiers: Sequence[str], *, available_by: datetime | None
) -> list[NormalizedEvidence]:
    stmt = (
        "/* autonomy_identity_recovery:story_clusters */"
        " WITH versions AS (SELECT DISTINCT ON (v.cluster_id) v.cluster_id AS id, v.snapshot, v.changed_at"
        " FROM story_cluster_versions AS v WHERE v.cluster_id::TEXT = ANY(:ids) AND (:available_by IS NULL OR v.changed_at <= :available_by)"
        " ORDER BY v.cluster_id, v.version DESC)"
        " SELECT v.id, v.snapshot->>'title' AS title, v.snapshot->>'summary' AS summary, v.snapshot->>'state' AS state, v.snapshot->>'lane' AS lane,"
        " (v.snapshot->>'first_seen_at')::TIMESTAMPTZ AS first_seen_at, (v.snapshot->>'last_seen_at')::TIMESTAMPTZ AS last_seen_at,"
        " (v.snapshot->>'last_material_change_at')::TIMESTAMPTZ AS last_material_change_at,"
        " (v.snapshot->>'importance')::DOUBLE PRECISION AS importance, (v.snapshot->>'novelty')::DOUBLE PRECISION AS novelty,"
        " (v.snapshot->>'confidence')::DOUBLE PRECISION AS confidence, COALESCE(v.snapshot->'entities', '[]'::JSONB) AS entities,"
        " COALESCE(v.snapshot->'markets', '[]'::JSONB) AS markets, (v.snapshot->>'source_count')::INTEGER AS source_count,"
        " (v.snapshot->>'version')::INTEGER AS version, v.snapshot->>'change_summary' AS change_summary,"
        " COALESCE(v.snapshot->'clustering_reason', '{}'::JSONB) AS clustering_reason, v.changed_at AS updated_at"
        " FROM versions AS v WHERE v.snapshot->>'state' NOT IN ('stale', 'closed') ORDER BY v.id"
    )
    rows = _recover_execute(
        session, stmt, {"ids": sorted(set(identifiers)), "available_by": available_by}
    )
    return [
        _transform_story_cluster(
            r,
            available_by or datetime.min.replace(tzinfo=UTC),
            StoryClusterAdapter.name,
            recovered=True,
            available_by=available_by,
        )
        for r in rows
    ]


def _recovered_within_cutoff(
    item: NormalizedEvidence, available_by: datetime | None
) -> bool:
    if not item.point_in_time_safe:
        return False
    if available_by is None:
        return True
    return (
        _timestamp(item.source_timestamp) <= available_by
        and _timestamp(item.available_at) <= available_by
    )


_EXACT_LOOKUP_BUILDERS: Mapping[str, Any] = {
    "official_document": _recover_official_documents,
    "source_claim": _recover_source_claims,
    "filing_delta": _recover_filing_deltas,
    "investment_observation": _recover_investment_observations,
    "investment_analysis": _recover_investment_analyses,
    "market_confirmation": _recover_market_confirmations,
    "story_cluster": _recover_story_clusters,
}


def exact_evidence_lookup(
    session: Any,
    refs: Sequence[str],
    *,
    available_by: datetime | None = None,
    limit: int = 500,
) -> dict[str, NormalizedEvidence]:
    bounded = max(1, min(int(limit), 2_000))
    wanted = tuple(dict.fromkeys(str(ref) for ref in refs))[:bounded]
    if not wanted:
        return {}
    cutoff = _strict_timestamp(available_by)
    by_type: dict[str, list[str]] = {}
    for ref in wanted:
        kind, _, identifier = ref.partition(":")
        by_type.setdefault(kind, []).append(identifier)
    recovered: dict[str, NormalizedEvidence] = {}
    for kind in sorted(by_type):
        builder = _EXACT_LOOKUP_BUILDERS.get(kind)
        if builder is None:
            continue
        try:
            for item in builder(
                session, tuple(by_type[kind]), available_by=available_by
            ):
                if not _recovered_within_cutoff(item, cutoff):
                    continue
                recovered.setdefault(item.ref, item)
        except Exception:
            continue
    return recovered


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
        since = effective_now - timedelta(
            days=max(1, min(int(rolling_window_days), 730))
        )
        per_adapter = max(
            1, (bounded_limit + len(self.adapters) - 1) // len(self.adapters)
        )
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
    "exact_evidence_lookup",
    "ExpectationsSentimentAdapter",
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
    "PublicEquitiesAdapter",
    "PublicEquityTrendAdapter",
    "CorporateActionAdapter",
    "PositioningReportAdapter",
    "OptionChainSnapshotAdapter",
]
