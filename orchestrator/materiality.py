"""Deterministic event materiality scoring and caller-owned persistence.

This module deliberately has no scheduling or model dependencies.  It evaluates
an event synchronously and records the result so routing can decide whether to
hand the event to a durable job queue later in the transaction.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

COMPONENTS = (
    "importance",
    "relevance",
    "novelty",
    "source_confidence",
    "time_sensitivity",
)
DEFAULT_COMPONENTS = {
    "importance": 0.5,
    "relevance": 0.5,
    "novelty": 1.0,
    "source_confidence": 0.5,
    "time_sensitivity": 0.5,
}
DEFAULT_THRESHOLD = 0.5


class MaterialityValidationError(ValueError):
    """Raised when a configured or derived component is not a finite score."""


def _mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    return None


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _section(config: Any, *names: str) -> Mapping[str, Any]:
    for name in names:
        value = _field(config, name)
        if isinstance(value, Mapping):
            return value
    return {}


def _finite_number(value: Any, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MaterialityValidationError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise MaterialityValidationError(f"{name} must be finite")
    return result


def _normalization_spec(config: Any, name: str) -> tuple[float, float]:
    normalization = _section(config, "component_normalization", "normalization")
    spec = normalization.get(name)
    if spec is None:
        return 0.0, 1.0
    if isinstance(spec, Mapping):
        lower = spec.get("min", spec.get("lower", 0.0))
        upper = spec.get("max", spec.get("upper", 1.0))
    elif (
        isinstance(spec, Sequence)
        and not isinstance(spec, (str, bytes))
        and len(spec) == 2
    ):
        lower, upper = spec
    else:
        raise MaterialityValidationError(
            f"normalization for {name} must define min/max or a two-item range"
        )
    low = _finite_number(lower, name=f"{name} normalization minimum")
    high = _finite_number(upper, name=f"{name} normalization maximum")
    if high <= low:
        raise MaterialityValidationError(
            f"{name} normalization maximum must exceed minimum"
        )
    return low, high


def normalize_component(value: Any, name: str, config: Any = None) -> float:
    """Normalize one configured component, rejecting invalid values strictly.

    By default inputs must already be in ``[0, 1]``.  A component-specific
    ``normalization``/``component_normalization`` range may instead be used;
    values outside that range are rejected rather than silently clamped.
    """

    if name not in COMPONENTS and name != "routing_threshold":
        raise MaterialityValidationError(f"unknown materiality component: {name}")
    raw = _finite_number(value, name=name)
    lower, upper = _normalization_spec(config, name)
    if raw < lower or raw > upper:
        raise MaterialityValidationError(f"{name} must be within [{lower}, {upper}]")
    normalized = (raw - lower) / (upper - lower)
    if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
        raise MaterialityValidationError(
            f"{name} normalization produced an invalid score"
        )
    return normalized


def score_materiality(
    importance: float | Mapping[str, Any] | None = None,
    relevance: float | None = None,
    novelty: float | None = None,
    source_confidence: float | None = None,
    time_sensitivity: float | None = None,
    *,
    config: Any = None,
    components: Mapping[str, Any] | None = None,
):
    if components is not None:
        importance = components
    if isinstance(importance, Mapping):
        values = importance
        importance = values.get("importance")
        relevance = values.get("relevance")
        novelty = values.get("novelty")
        source_confidence = values.get("source_confidence")
        time_sensitivity = values.get("time_sensitivity")
    raw = {
        "importance": importance,
        "relevance": relevance,
        "novelty": novelty,
        "source_confidence": source_confidence,
        "time_sensitivity": time_sensitivity,
    }
    if any(value is None for value in raw.values()):
        raise MaterialityValidationError("all materiality components are required")
    values = {
        name: normalize_component(value, name, config) for name, value in raw.items()
    }
    score = round(math.prod(values.values()), 15)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise MaterialityValidationError("materiality product is invalid")
    return score


@dataclass(frozen=True)
class MaterialityDecision:
    event_id: UUID | str
    job_type: str
    importance: float
    relevance: float
    novelty: float
    source_confidence: float
    time_sensitivity: float
    score: float
    threshold: float
    should_route: bool
    suppression_reason: str | None
    rationale: dict[str, Any]
    provenance: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @property
    def final_product_score(self) -> float:
        return self.score

    @property
    def routing_threshold(self) -> float:
        return self.threshold

    @property
    def decision(self) -> str:
        return "route" if self.should_route else "suppress"

    @property
    def component_rationale(self) -> dict[str, Any]:
        return self.rationale

    @property
    def component_provenance(self) -> dict[str, Any]:
        return self.provenance


def _lookup(mapping: Mapping[str, Any], key: str, default: Any = None) -> Any:
    if key in mapping:
        return mapping[key]
    return default


def _event_type(event: Any) -> str:
    value = _field(event, "event_type", "unknown")
    return getattr(value, "value", str(value))


def _macro_mapping(config: Any, event: Any) -> Mapping[str, Any] | None:
    macro = _section(config, "macro_event_mappings")
    payload = _field(event, "payload", {}) or {}
    metadata = _field(event, "metadata", {}) or {}
    keys = [
        _field(event, "event_type", None),
        _field(event, "event_name", None),
        _field(payload, "series_id", None),
        _field(payload, "event_name", None),
        _field(metadata, "series_id", None),
        _field(metadata, "event_name", None),
    ]
    for key in keys:
        if key is None:
            continue
        key = getattr(key, "value", str(key))
        value = macro.get(key)
        if isinstance(value, Mapping):
            return value
    return None


def _configured_component(
    config: Any, name: str, event_type: str, event: Any = None
) -> Any:
    # Macro mappings use an auditable priority (1..10); convert it to the
    # common unit interval before strict normalization.
    if name == "importance":
        mapped = (
            _macro_mapping(config, event)
            if event is not None
            else _section(config, "macro_event_mappings").get(event_type)
        )
        if isinstance(mapped, Mapping):
            if "importance" in mapped or "score" in mapped:
                return mapped.get("importance", mapped.get("score"))
            mapped = mapped.get("priority")
        if mapped is not None:
            priority = _finite_number(mapped, name="macro event priority")
            if not 1.0 <= priority <= 10.0:
                raise MaterialityValidationError(
                    "macro event priority must be between 1 and 10"
                )
            return (priority - 1.0) / 9.0
    maps = {
        "importance": (
            "importance_by_event_type",
            "event_class_importance",
            "importance",
        ),
        "time_sensitivity": (
            "time_sensitivity_by_event_type",
            "event_class_time_sensitivity",
            "time_sensitivity",
        ),
    }
    for section_name in maps.get(name, ()):
        section = _section(config, section_name)
        if event_type in section:
            value = section[event_type]
            if isinstance(value, Mapping):
                value = value.get("score", value.get("value"))
            return value
        if "default" in section:
            return section["default"]
    direct = _section(config, "components", "component_defaults")
    if name in direct:
        return direct[name]
    return DEFAULT_COMPONENTS[name]


def _entity_values(event: Any) -> set[str]:
    values: set[str] = set()
    for entity in _field(event, "entities", ()) or ():
        for name in ("canonical_id", "display_name", "symbol", "name", "id"):
            value = _field(entity, name)
            if value is not None:
                values.add(str(value).strip().casefold())
    for market in _field(event, "markets", ()) or ():
        for name in ("canonical_id", "display_name", "symbol", "name", "id"):
            value = _field(market, name)
            if value is not None:
                values.add(str(value).strip().casefold())
    return {value for value in values if value}


def _relevance(event: Any, config: Any) -> tuple[float, str, dict[str, Any]]:
    configured_watched = _field(config, "watched_entities", None)
    if configured_watched is None:
        configured_watched = _field(config, "watchlist", {})
    watched = configured_watched
    entity_values = _entity_values(event)
    macro = _macro_mapping(config, event)
    if macro:
        for key in ("instruments", "markets", "symbols", "watchlist_entities"):
            mapped_values = macro.get(key, ())
            if isinstance(mapped_values, str):
                mapped_values = (mapped_values,)
            if isinstance(mapped_values, Sequence):
                entity_values.update(
                    str(value).strip().casefold() for value in mapped_values
                )
    default = _field(config, "relevance_default", None)
    if isinstance(watched, Mapping):
        default = watched.get("default", default)
    if default is None:
        default = DEFAULT_COMPONENTS["relevance"]
    scores: list[tuple[str, Any]] = []
    if isinstance(watched, Mapping):
        entries = watched.get("entities", watched.get("trading", watched))
        if isinstance(entries, Mapping):
            for key, value in entries.items():
                if str(key).casefold() in entity_values:
                    score = (
                        value.get("score", value)
                        if isinstance(value, Mapping)
                        else value
                    )
                    scores.append((str(key), score))
        elif isinstance(entries, Sequence) and not isinstance(entries, (str, bytes)):
            for entry in entries:
                if isinstance(entry, Mapping):
                    key = entry.get(
                        "symbol", entry.get("canonical_id", entry.get("name"))
                    )
                    score = entry.get("score", entry.get("relevance", 1.0))
                else:
                    key, score = entry, 1.0
                if key is not None:
                    scores.append((str(key), score))
    elif isinstance(watched, Sequence) and not isinstance(watched, (str, bytes)):
        for entry in watched:
            if isinstance(entry, Mapping):
                key = entry.get("symbol", entry.get("canonical_id", entry.get("name")))
                score = entry.get("score", entry.get("relevance", 1.0))
            else:
                key, score = entry, 1.0
            if key is not None:
                scores.append((str(key), score))
    matched = [(key, value) for key, value in scores if key.casefold() in entity_values]
    if matched:
        value = max(value for _, value in matched)
        return (
            value,
            "watched entity matched",
            {"matched_entities": [key for key, _ in matched]},
        )
    return default, "no watched entity matched", {"matched_entities": []}


def _source_confidence(event: Any, config: Any) -> tuple[Any, str]:
    source = str(_field(event, "source", "unknown"))
    reliability = _section(
        config, "source_reliability", "source_confidence", "source_reliabilities"
    )
    if not reliability:
        reliability = _section(config, "analysis_routing").get("source_confidence", {})
    if source in reliability:
        value = reliability[source]
        return (
            value.get("score", value) if isinstance(value, Mapping) else value
        ), "configured source reliability"
    return (
        _field(
            config,
            "source_confidence_default",
            reliability.get("default", DEFAULT_COMPONENTS["source_confidence"]),
        ),
        "source reliability default",
    )


def _novelty(event: Any, config: Any) -> tuple[Any, str, dict[str, Any]]:
    metadata = _field(event, "metadata", {}) or {}
    if not isinstance(metadata, Mapping):
        metadata = {}
    novelty_config = _section(config, "novelty")
    exact = _field(event, "exact_duplicate", None)
    if exact is None:
        exact = metadata.get(
            "exact_duplicate", metadata.get("is_exact_duplicate", False)
        )
    exact = exact or _field(event, "duplicate_of_event_id", None) is not None
    exact = exact or metadata.get("duplicate_of_event_id") is not None
    if exact:
        return (
            novelty_config.get("exact_score", 0.0),
            "exact duplicate",
            {"exact_duplicate": True},
        )
    similarity = _field(event, "near_duplicate_similarity", None)
    if similarity is None:
        similarity = metadata.get("near_duplicate_similarity")
    if similarity is None and isinstance(
        _field(event, "near_duplicate", None), (int, float)
    ):
        similarity = _field(event, "near_duplicate")
    if similarity is None and isinstance(metadata.get("near_duplicate"), (int, float)):
        similarity = metadata["near_duplicate"]
    if similarity is not None:
        sim = normalize_component(
            similarity, "novelty", {"normalization": {"novelty": [0.0, 1.0]}}
        )
        return (
            novelty_config.get("near_duplicate_score", 1.0 - sim),
            "near duplicate similarity",
            {"similarity": sim},
        )
    count = _field(event, "near_duplicate_count", None)
    if count is None:
        count = metadata.get("near_duplicate_count", metadata.get("duplicate_count"))
    if count is not None:
        count_number = _finite_number(count, name="near_duplicate_count")
        if count_number < 0:
            raise MaterialityValidationError(
                "near_duplicate_count must not be negative"
            )
        return (
            1.0 / (1.0 + count_number),
            "duplicate count",
            {"duplicate_count": count_number},
        )
    return (
        novelty_config.get("default", DEFAULT_COMPONENTS["novelty"]),
        "no duplicate detected",
        {"exact_duplicate": False},
    )


def _threshold(config: Any, job_type: str) -> Any:
    aliases = {
        "event_atom": "event_atom_min_score",
        "story_summary": "story_summary_min_score",
        "briefing_invalidation": "briefing_invalidation_min_score",
        "investment_thesis_review": "investment_thesis_review_min_score",
        "reaction_window": "reaction_window_min_score",
    }
    for name in (
        "job_thresholds",
        "routing_thresholds",
        "thresholds",
        "analysis_routing",
    ):
        section = _section(config, name)
        key = aliases.get(job_type, job_type)
        if key in section:
            value = section[key]
            return (
                value.get("threshold", value.get("score", value))
                if isinstance(value, Mapping)
                else value
            )
        if job_type in section:
            value = section[job_type]
            return (
                value.get("threshold", value.get("score", value))
                if isinstance(value, Mapping)
                else value
            )
        if "default" in section:
            default = section["default"]
            return (
                default.get("threshold", default)
                if isinstance(default, Mapping)
                else default
            )
    direct = _field(config, "routing_threshold", None)
    return DEFAULT_THRESHOLD if direct is None else direct


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise MaterialityValidationError("JSON values must be finite")
        return value
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    return str(value)


def _json_text(value: Mapping[str, Any]) -> str:
    return json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"))


def _persist(session: Any, decision: MaterialityDecision) -> None:
    params = {
        "event_id": decision.event_id,
        "job_type": decision.job_type,
        "importance": decision.importance,
        "relevance": decision.relevance,
        "novelty": decision.novelty,
        "source_confidence": decision.source_confidence,
        "time_sensitivity": decision.time_sensitivity,
        "score": decision.score,
        "routing_threshold": decision.threshold,
        "decision": decision.decision,
        "suppression_reason": decision.suppression_reason,
        "component_rationale": _json_text(decision.rationale),
        "component_provenance": _json_text(decision.provenance),
    }
    session.execute(
        text("""INSERT INTO event_materiality
            (event_id, job_type, importance, relevance, novelty,
             source_confidence, time_sensitivity, score, routing_threshold,
             decision, suppression_reason, component_rationale,
             component_provenance)
            VALUES (:event_id, :job_type, :importance, :relevance, :novelty,
                    :source_confidence, :time_sensitivity, :score,
                    :routing_threshold, :decision, :suppression_reason,
                    CAST(:component_rationale AS JSONB),
                    CAST(:component_provenance AS JSONB))
            ON CONFLICT (event_id, job_type) DO UPDATE SET
                importance = EXCLUDED.importance,
                relevance = EXCLUDED.relevance,
                novelty = EXCLUDED.novelty,
                source_confidence = EXCLUDED.source_confidence,
                time_sensitivity = EXCLUDED.time_sensitivity,
                score = EXCLUDED.score,
                routing_threshold = EXCLUDED.routing_threshold,
                decision = EXCLUDED.decision,
                suppression_reason = EXCLUDED.suppression_reason,
                component_rationale = EXCLUDED.component_rationale,
                component_provenance = EXCLUDED.component_provenance,
                updated_at = CURRENT_TIMESTAMP"""),
        params,
    )


def assess_event_materiality(
    session: Any,
    event: Any,
    config: Any,
    *,
    job_type: str = "event_atom",
) -> MaterialityDecision:
    """Score and upsert an event's decision using the caller's transaction."""

    if not isinstance(job_type, str) or not job_type.strip():
        raise MaterialityValidationError("job_type must be a nonblank string")
    event_id = _field(event, "event_id", _field(event, "id"))
    if event_id is None or (isinstance(event_id, str) and not event_id.strip()):
        raise MaterialityValidationError("event must provide event_id")
    event_type = _event_type(event)
    importance_raw = _field(event, "importance_hint", None)
    importance_raw = (
        _configured_component(config, "importance", event_type, event)
        if importance_raw is None
        else importance_raw
    )
    relevance_raw, relevance_reason, relevance_input = _relevance(event, config)
    novelty_raw, novelty_reason, novelty_input = _novelty(event, config)
    source_raw, source_reason = _source_confidence(event, config)
    time_raw = _configured_component(config, "time_sensitivity", event_type, event)
    components = {
        "importance": normalize_component(importance_raw, "importance", config),
        "relevance": normalize_component(relevance_raw, "relevance", config),
        "novelty": normalize_component(novelty_raw, "novelty", config),
        "source_confidence": normalize_component(
            source_raw, "source_confidence", config
        ),
        "time_sensitivity": normalize_component(time_raw, "time_sensitivity", config),
    }
    score = score_materiality(components)
    threshold = normalize_component(
        _threshold(config, job_type),
        "routing_threshold",
        {"normalization": {"routing_threshold": [0.0, 1.0]}},
    )
    should_route = score >= threshold
    now = datetime.now(UTC)
    rationale = {
        "importance": {
            "value": components["importance"],
            "reason": f"event class {event_type}",
        },
        "relevance": {
            "value": components["relevance"],
            "reason": relevance_reason,
            **relevance_input,
        },
        "novelty": {
            "value": components["novelty"],
            "reason": novelty_reason,
            **novelty_input,
        },
        "source_confidence": {
            "value": components["source_confidence"],
            "reason": source_reason,
        },
        "time_sensitivity": {
            "value": components["time_sensitivity"],
            "reason": f"event class {event_type}",
        },
        "score": {
            "value": score,
            "formula": "importance * relevance * novelty * source_confidence * time_sensitivity",
        },
    }
    provenance = {
        "source": str(_field(event, "source", "unknown")),
        "event_type": event_type,
        "source_event_id": _field(event, "source_event_id"),
        "content_hash": _field(event, "content_hash"),
        "components": {
            "importance": "event importance hint or event-class configuration",
            "relevance": "watched-entity configuration",
            "novelty": "event duplicate metadata",
            "source_confidence": source_reason,
            "time_sensitivity": "event-class configuration",
        },
    }
    decision = MaterialityDecision(
        event_id=event_id,
        job_type=job_type.strip(),
        importance=components["importance"],
        relevance=components["relevance"],
        novelty=components["novelty"],
        source_confidence=components["source_confidence"],
        time_sensitivity=components["time_sensitivity"],
        score=score,
        threshold=threshold,
        should_route=should_route,
        suppression_reason=None if should_route else "below_threshold",
        rationale=rationale,
        provenance=provenance,
        created_at=now,
        updated_at=now,
    )
    _persist(session, decision)
    return decision


__all__ = [
    "COMPONENTS",
    "MaterialityDecision",
    "MaterialityValidationError",
    "assess_event_materiality",
    "normalize_component",
    "score_materiality",
]
