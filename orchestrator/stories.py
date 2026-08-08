"""Deterministic, auditable canonical news-story clustering.

All persistence uses the caller's transaction.  Clustering starts with bounded
recent candidates and explicit, inspectable similarity components; it never
calls a model and never stores arbitrary provider payloads.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text

_STATES = {"developing", "confirmed", "contradicted", "stale", "closed"}
_LANES = {
    "market_moving",
    "watchlist_related",
    "macro_central_banks",
    "filings_regulators",
    "developing",
    "low_confidence",
}
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "has",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "to",
    "was",
    "will",
    "with",
    "after",
    "before",
    "says",
    "said",
    "update",
}
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MAX_CANDIDATES = 200
_MAX_LIST = 200


@dataclass(frozen=True)
class StoryAssignment:
    cluster_id: UUID | str
    canonical_key: str
    version: int
    state: str
    lane: str
    materially_changed: bool
    similarity_score: float
    contribution_type: str


def _field(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _timestamp(value: Any, default: datetime | None = None) -> datetime | None:
    if value is None:
        return default
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return default
    else:
        return default
    if result.tzinfo is None or result.utcoffset() is None:
        return default
    return result.astimezone(UTC)


def _finite(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(1.0, max(0.0, result)) if math.isfinite(result) else default


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_value(item) for item in value]
    return value


def _json(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


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


def normalize_title(value: Any) -> str:
    """Normalize a title without destroying the public display value."""
    text_value = str(value or "").strip().casefold()
    return " ".join(_TOKEN_RE.findall(text_value))[:500]


def title_tokens(value: Any) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            token
            for token in _TOKEN_RE.findall(normalize_title(value))
            if token not in _STOPWORDS and len(token) > 1
        )
    )


def token_overlap(left: Any, right: Any) -> float:
    left_tokens, right_tokens = set(title_tokens(left)), set(title_tokens(right))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _refs(values: Any, *, market: bool = False) -> list[dict[str, Any]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []
    result: dict[str, dict[str, Any]] = {}
    for raw in values[:100]:
        if isinstance(raw, str):
            key = raw.strip()
            item = {"canonical_id": key, "display_name": key}
            if market:
                item["symbol"] = key
        else:
            key = str(
                _field(
                    raw, "canonical_id", _field(raw, "symbol", _field(raw, "name", ""))
                )
                or ""
            ).strip()
            item = {
                name: _field(raw, name)
                for name in (
                    "canonical_id",
                    "display_name",
                    "entity_type",
                    "symbol",
                    "asset_class",
                )
                if _field(raw, name) is not None
            }
        if key:
            item.setdefault("canonical_id", key)
            item.setdefault("display_name", str(item.get("symbol") or key))
            result[key.casefold()] = item
    return list(result.values())


def _ids(values: Any) -> set[str]:
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except json.JSONDecodeError:
            values = []
    if not isinstance(values, Sequence):
        return set()
    return {
        str(_field(item, "canonical_id", _field(item, "symbol", item))).casefold()
        for item in values
        if _field(item, "canonical_id", _field(item, "symbol", item))
    }


def _set_overlap(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left | right) if left and right else 0.0


def _settings(config: Any) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    value = config.get("story_clustering", {})
    return value if isinstance(value, Mapping) else {}


def _source_confidence(source: str, settings: Mapping[str, Any]) -> float:
    values = settings.get("source_confidence", {})
    if isinstance(values, Mapping):
        return _finite(values.get(source, values.get("default", 0.6)), 0.6)
    return 0.6


def _story_values(event: Any) -> dict[str, Any]:
    payload = dict(_mapping(_field(event, "payload", {})))
    title = str(payload.get("title") or "").strip()[:500]
    if not title:
        raise ValueError("headline title is required")
    source = (
        str(_field(event, "source", payload.get("source", "news")) or "news")
        .strip()
        .lower()[:64]
    )
    source_item_id = str(
        payload.get("id")
        or payload.get("source_item_id")
        or _field(event, "source_event_id", "")
    ).strip()[:500]
    if not source_item_id:
        raise ValueError("headline source item id is required")
    published = (
        _timestamp(payload.get("published"))
        or _timestamp(_field(event, "published_at"))
        or _timestamp(_field(event, "effective_at"))
        or _timestamp(_field(event, "observed_at"))
    )
    if published is None:
        raise ValueError("headline published timestamp is required")
    entities = _refs(_field(event, "entities", payload.get("entities", [])))
    markets = _refs(
        _field(event, "markets", payload.get("markets", payload.get("symbols", []))),
        market=True,
    )
    tags = [
        str(item).strip().casefold()[:80]
        for item in payload.get("tags", [])[:50]
        if str(item).strip()
    ]
    return {
        "event_id": _field(event, "event_id", _field(event, "id")),
        "source": source,
        "source_item_id": source_item_id,
        "source_label": str(payload.get("source_label") or source).strip()[:100],
        "title": title,
        "summary": str(payload.get("summary") or "").strip()[:2000] or None,
        "url": str(payload.get("url") or "").strip()[:2000] or None,
        "published_at": published,
        "entities": entities,
        "markets": markets,
        "tags": tags,
        "importance": _finite(
            payload.get("importance", _field(event, "importance_hint", 0.5)), 0.5
        ),
        "explicit_contribution": str(payload.get("contribution_type") or "")
        .strip()
        .lower(),
    }


def _is_contradiction(values: Mapping[str, Any]) -> bool:
    if values.get("explicit_contribution") == "contradiction":
        return True
    tags = set(values.get("tags", []))
    if tags & {"contradiction", "denial", "denied", "false_report"}:
        return True
    normalized = normalize_title(values.get("title"))
    return any(
        phrase in normalized
        for phrase in ("denies report", "denied report", "report is false", "not true")
    )


def _lane(
    values: Mapping[str, Any],
    confidence: float,
    source_count: int,
    settings: Mapping[str, Any],
) -> str:
    low_threshold = _finite(settings.get("low_confidence_threshold", 0.55), 0.55)
    if confidence < low_threshold or source_count <= 1:
        return "low_confidence"
    words = set(title_tokens(values.get("title"))) | set(values.get("tags", []))
    configured_keywords = settings.get("lane_keywords", {})
    configured_keywords = (
        configured_keywords if isinstance(configured_keywords, Mapping) else {}
    )
    filing_words = configured_keywords.get(
        "filings_regulators",
        ("filing", "sec", "regulator", "antitrust", "lawsuit", "investigation"),
    )
    macro_words = configured_keywords.get(
        "macro_central_banks",
        (
            "fed",
            "ecb",
            "boj",
            "boe",
            "central",
            "bank",
            "inflation",
            "gdp",
            "payrolls",
            "rates",
        ),
    )
    if words & {str(item).casefold() for item in filing_words}:
        return "filings_regulators"
    if words & {str(item).casefold() for item in macro_words}:
        return "macro_central_banks"
    watched = settings.get("watchlist", [])
    watched_ids = (
        {str(item).casefold() for item in watched}
        if isinstance(watched, Sequence) and not isinstance(watched, (str, bytes))
        else set()
    )
    if watched_ids & (
        _ids(values.get("entities", [])) | _ids(values.get("markets", []))
    ):
        return "watchlist_related"
    if _finite(values.get("importance"), 0.5) >= _finite(
        settings.get("market_moving_min_importance", 0.75), 0.75
    ):
        return "market_moving"
    return "developing"


def _candidate_score(
    values: Mapping[str, Any],
    candidate: Mapping[str, Any],
    now: datetime,
    window: timedelta,
) -> tuple[float, dict[str, float]]:
    published = values["published_at"]
    candidate_seen = _timestamp(candidate.get("last_seen_at"), now) or now
    elapsed = abs((published - candidate_seen).total_seconds())
    time_score = max(0.0, 1.0 - elapsed / max(1.0, window.total_seconds()))
    components = {
        "title_overlap": token_overlap(values["title"], candidate.get("title")),
        "entity_overlap": _set_overlap(
            _ids(values["entities"]), _ids(candidate.get("entities", []))
        ),
        "market_overlap": _set_overlap(
            _ids(values["markets"]), _ids(candidate.get("markets", []))
        ),
        "time_proximity": time_score,
    }
    score = round(
        components["title_overlap"] * 0.65
        + components["entity_overlap"] * 0.20
        + components["market_overlap"] * 0.10
        + components["time_proximity"] * 0.05,
        12,
    )
    return score, components


def _canonical_key(values: Mapping[str, Any]) -> str:
    identity = {
        "date": values["published_at"].date().isoformat(),
        "tokens": sorted(title_tokens(values["title"]))[:16],
        "entities": sorted(_ids(values["entities"])),
        "markets": sorted(_ids(values["markets"])),
    }
    digest = hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()
    return f"story:{digest}"


def _snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "canonical_key",
            "title",
            "summary",
            "state",
            "lane",
            "first_seen_at",
            "last_seen_at",
            "last_material_change_at",
            "importance",
            "novelty",
            "confidence",
            "entities",
            "markets",
            "source_count",
            "version",
            "change_summary",
            "clustering_reason",
        )
    }


def _insert_version(
    session: Any,
    cluster: Mapping[str, Any],
    member_id: Any,
    prior_state: str | None,
    contribution: str,
    summary: str,
    changed_at: datetime,
) -> None:
    session.execute(
        text("""INSERT INTO story_cluster_versions
        (cluster_id, version, prior_state, state, contribution_type, change_summary, member_id, snapshot, changed_at)
        VALUES (:cluster_id, :version, :prior_state, :state, :contribution, :summary,
                :member_id, CAST(:snapshot AS JSONB), :changed_at)
        ON CONFLICT (cluster_id, version) DO NOTHING"""),
        {
            "cluster_id": cluster["id"],
            "version": cluster["version"],
            "prior_state": prior_state,
            "state": cluster["state"],
            "contribution": contribution,
            "summary": summary[:500],
            "member_id": member_id,
            "snapshot": _json(_snapshot(cluster)),
            "changed_at": changed_at,
        },
    )


def cluster_news_story(
    session: Any, event: Any, config: Any = None, now: Any = None
) -> StoryAssignment:
    """Assign one headline event to exactly one canonical cluster."""
    values = _story_values(event)
    settings = _settings(config)
    current = _timestamp(now, datetime.now(UTC)) or datetime.now(UTC)
    existing = _first(
        session.execute(
            text("""SELECT c.* FROM story_cluster_members m
        JOIN story_clusters c ON c.id = m.cluster_id
        WHERE m.source = :source AND m.source_item_id = :source_item_id LIMIT 1"""),
            {
                "source": values["source"],
                "source_item_id": values["source_item_id"],
            },
        )
    )
    if existing is not None:
        return StoryAssignment(
            existing["id"],
            existing["canonical_key"],
            int(existing["version"]),
            existing["state"],
            existing["lane"],
            False,
            1.0,
            "repeated_coverage",
        )

    try:
        window_hours = max(1, min(168, int(settings.get("candidate_window_hours", 72))))
        candidate_limit = max(
            1, min(_MAX_CANDIDATES, int(settings.get("candidate_limit", 100)))
        )
    except (TypeError, ValueError, OverflowError):
        window_hours, candidate_limit = 72, 100
    window = timedelta(hours=window_hours)
    candidates = _rows(
        session.execute(
            text("""SELECT id, canonical_key, title, summary, state, lane,
        first_seen_at, last_seen_at, last_material_change_at, importance, novelty,
        confidence, entities, markets, source_count, version, change_summary, clustering_reason
        FROM story_clusters WHERE last_seen_at >= :cutoff AND state <> 'closed'
        ORDER BY last_seen_at DESC, id DESC LIMIT :candidate_limit"""),
            {
                "cutoff": values["published_at"] - window,
                "candidate_limit": candidate_limit,
            },
        )
    )
    ranked = [
        (*_candidate_score(values, candidate, current, window), candidate)
        for candidate in candidates
    ]
    ranked.sort(key=lambda item: (-item[0], str(item[2].get("id"))))
    threshold = _finite(settings.get("similarity_threshold", 0.55), 0.55)
    selected = ranked[0] if ranked and ranked[0][0] >= threshold else None
    source_score = _source_confidence(values["source"], settings)

    if selected is None:
        confidence = round(source_score * 0.8, 12)
        lane = _lane(values, confidence, 1, settings)
        reason = {"decision": "new_cluster", "threshold": threshold, "components": {}}
        cluster = _first(
            session.execute(
                text("""INSERT INTO story_clusters
            (canonical_key, title, summary, state, lane, first_seen_at, last_seen_at,
             last_material_change_at, importance, novelty, confidence, entities, markets,
             source_count, version, change_summary, clustering_reason)
            VALUES (:canonical_key, :title, :summary, 'developing', :lane, :seen, :seen,
             :seen, :importance, 1.0, :confidence, CAST(:entities AS JSONB),
             CAST(:markets AS JSONB), 1, 1, :change_summary, CAST(:reason AS JSONB))
            ON CONFLICT (canonical_key) DO UPDATE SET last_seen_at = GREATEST(story_clusters.last_seen_at, EXCLUDED.last_seen_at)
            RETURNING *"""),
                {
                    "canonical_key": _canonical_key(values),
                    "title": values["title"],
                    "summary": values["summary"],
                    "lane": lane,
                    "seen": values["published_at"],
                    "importance": values["importance"],
                    "confidence": confidence,
                    "entities": _json(values["entities"]),
                    "markets": _json(values["markets"]),
                    "change_summary": "Initial report",
                    "reason": _json(reason),
                },
            )
        )
        if cluster is None:
            raise RuntimeError("story cluster insert returned no row")
        member = _first(
            session.execute(
                text("""INSERT INTO story_cluster_members
            (cluster_id, market_event_id, source, source_item_id, source_label, title,
             summary, url, published_at, similarity_score, contribution_type,
             materially_changed, clustering_reason, entities, markets)
            VALUES (:cluster_id, :event_id, :source, :source_item_id, :source_label,
             :title, :summary, :url, :published_at, 1.0, 'origin', TRUE,
             CAST(:reason AS JSONB), CAST(:entities AS JSONB), CAST(:markets AS JSONB))
            ON CONFLICT (source, source_item_id) DO NOTHING RETURNING id"""),
                {
                    "cluster_id": cluster["id"],
                    "event_id": values["event_id"],
                    "source": values["source"],
                    "source_item_id": values["source_item_id"],
                    "source_label": values["source_label"],
                    "title": values["title"],
                    "summary": values["summary"],
                    "url": values["url"],
                    "published_at": values["published_at"],
                    "reason": _json(reason),
                    "entities": _json(values["entities"]),
                    "markets": _json(values["markets"]),
                },
            )
        )
        member_id = member.get("id") if member else None
        _insert_version(
            session, cluster, member_id, None, "origin", "Initial report", current
        )
        return StoryAssignment(
            cluster["id"],
            cluster["canonical_key"],
            1,
            "developing",
            lane,
            True,
            1.0,
            "origin",
        )

    similarity, components, cluster = selected
    source_rows = _rows(
        session.execute(
            text("""SELECT DISTINCT source FROM story_cluster_members
        WHERE cluster_id = :cluster_id ORDER BY source LIMIT 100"""),
            {"cluster_id": cluster["id"]},
        )
    )
    existing_sources = {str(row["source"]) for row in source_rows}
    new_source = values["source"] not in existing_sources
    old_tokens, new_tokens = (
        set(title_tokens(cluster["title"])),
        set(title_tokens(values["title"])),
    )
    fact_novelty = len(new_tokens - old_tokens) / max(1, len(new_tokens | old_tokens))
    new_entities = _ids(values["entities"]) - _ids(cluster.get("entities", []))
    new_markets = _ids(values["markets"]) - _ids(cluster.get("markets", []))
    contradiction = _is_contradiction(values)
    material_threshold = _finite(settings.get("material_change_threshold", 0.25), 0.25)
    if contradiction:
        contribution, materially_changed = "contradiction", True
    elif new_source:
        contribution, materially_changed = "cross_source_confirmation", True
    elif fact_novelty >= material_threshold or new_entities or new_markets:
        contribution, materially_changed = "material_update", True
    else:
        contribution, materially_changed = "repeated_coverage", False
    reason = {
        "decision": "assigned",
        "threshold": threshold,
        "score": similarity,
        "components": components,
        "fact_novelty": round(fact_novelty, 12),
        "new_entities": sorted(new_entities),
        "new_markets": sorted(new_markets),
    }
    member = _first(
        session.execute(
            text("""INSERT INTO story_cluster_members
        (cluster_id, market_event_id, source, source_item_id, source_label, title,
         summary, url, published_at, similarity_score, contribution_type,
         materially_changed, clustering_reason, entities, markets)
        VALUES (:cluster_id, :event_id, :source, :source_item_id, :source_label,
         :title, :summary, :url, :published_at, :similarity, :contribution,
         :material, CAST(:reason AS JSONB), CAST(:entities AS JSONB), CAST(:markets AS JSONB))
        ON CONFLICT (source, source_item_id) DO NOTHING RETURNING id"""),
            {
                "cluster_id": cluster["id"],
                "event_id": values["event_id"],
                "source": values["source"],
                "source_item_id": values["source_item_id"],
                "source_label": values["source_label"],
                "title": values["title"],
                "summary": values["summary"],
                "url": values["url"],
                "published_at": values["published_at"],
                "similarity": similarity,
                "contribution": contribution,
                "material": materially_changed,
                "reason": _json(reason),
                "entities": _json(values["entities"]),
                "markets": _json(values["markets"]),
            },
        )
    )
    if member is None:
        return StoryAssignment(
            cluster["id"],
            cluster["canonical_key"],
            int(cluster["version"]),
            cluster["state"],
            cluster["lane"],
            False,
            similarity,
            "repeated_coverage",
        )

    if not materially_changed:
        session.execute(
            text(
                "UPDATE story_clusters SET last_seen_at = GREATEST(last_seen_at, :seen), updated_at = :now WHERE id = :id"
            ),
            {
                "seen": values["published_at"],
                "now": current,
                "id": cluster["id"],
            },
        )
        return StoryAssignment(
            cluster["id"],
            cluster["canonical_key"],
            int(cluster["version"]),
            cluster["state"],
            cluster["lane"],
            False,
            similarity,
            contribution,
        )

    source_count = len(existing_sources | {values["source"]})
    confidence = round(
        min(
            1.0,
            max(
                _finite(cluster["confidence"]),
                (
                    sum(
                        _source_confidence(source, settings)
                        for source in existing_sources | {values["source"]}
                    )
                    / source_count
                )
                * (0.85 + min(0.15, 0.05 * source_count)),
            ),
        ),
        12,
    )
    state = (
        "contradicted"
        if contradiction
        else "confirmed"
        if source_count >= 2
        else "developing"
    )
    merged_entities = _refs(
        [
            *(_mapping(item) for item in (cluster.get("entities") or [])),
            *values["entities"],
        ]
    )
    merged_markets = _refs(
        [
            *(_mapping(item) for item in (cluster.get("markets") or [])),
            *values["markets"],
        ],
        market=True,
    )
    merged_values = {**values, "entities": merged_entities, "markets": merged_markets}
    lane = _lane(merged_values, confidence, source_count, settings)
    summary = (
        "Contradictory evidence added"
        if contradiction
        else "Confirmed by an additional source"
        if new_source
        else "Material facts updated"
    )
    updated = _first(
        session.execute(
            text("""UPDATE story_clusters SET
        title = :title, summary = :summary_text, state = :state, lane = :lane,
        last_seen_at = GREATEST(last_seen_at, :seen), last_material_change_at = :now,
        importance = GREATEST(importance, :importance), novelty = :novelty,
        confidence = :confidence, entities = CAST(:entities AS JSONB),
        markets = CAST(:markets AS JSONB), source_count = :source_count,
        version = version + 1, change_summary = :change_summary,
        clustering_reason = CAST(:reason AS JSONB), updated_at = :now
        WHERE id = :id RETURNING *"""),
            {
                "title": values["title"]
                if contribution in {"material_update", "contradiction"}
                else cluster["title"],
                "summary_text": values["summary"]
                if contribution in {"material_update", "contradiction"}
                and values["summary"]
                else cluster.get("summary"),
                "state": state,
                "lane": lane,
                "seen": values["published_at"],
                "now": current,
                "importance": values["importance"],
                "novelty": round(max(0.1, fact_novelty), 12),
                "confidence": confidence,
                "entities": _json(merged_entities),
                "markets": _json(merged_markets),
                "source_count": source_count,
                "change_summary": summary,
                "reason": _json(reason),
                "id": cluster["id"],
            },
        )
    )
    if updated is None:
        raise RuntimeError("story cluster update returned no row")
    _insert_version(
        session, updated, member["id"], cluster["state"], contribution, summary, current
    )
    return StoryAssignment(
        updated["id"],
        updated["canonical_key"],
        int(updated["version"]),
        updated["state"],
        updated["lane"],
        True,
        similarity,
        contribution,
    )


def list_story_clusters(
    session: Any, lane: str | None = None, limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(_MAX_LIST, int(limit)))
    bounded_offset = max(0, min(10_000, int(offset)))
    normalized_lane = str(lane).strip().lower() if lane else None
    if normalized_lane is not None and normalized_lane not in _LANES:
        raise ValueError("unsupported story lane")
    rows = _rows(
        session.execute(
            text("""SELECT id, canonical_key, title, summary, state, lane,
        first_seen_at, last_seen_at, last_material_change_at, importance, novelty,
        confidence, entities, markets, source_count, version, change_summary,
        clustering_reason, created_at, updated_at FROM story_clusters
        WHERE (:lane IS NULL OR lane = :lane)
        ORDER BY last_material_change_at DESC, id DESC LIMIT :limit OFFSET :offset"""),
            {
                "lane": normalized_lane,
                "limit": bounded_limit,
                "offset": bounded_offset,
            },
        )
    )
    return rows


__all__ = [
    "StoryAssignment",
    "cluster_news_story",
    "list_story_clusters",
    "normalize_title",
    "title_tokens",
    "token_overlap",
]
