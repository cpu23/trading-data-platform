"""Deterministic, revision-aware macro release cards.

This module deliberately contains no model or network dependency.  It accepts
normalized MarketEvent objects (or plain mappings in tests), keeps source
payloads in provenance, and uses caller-owned SQLAlchemy sessions without
committing them.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text

from contracts.db_results import result_first, result_rows

STAGES = ("t0", "developing", "reaction", "final")
_MAX_READ = 500


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def _value(event: Any, key: str, default: Any = None) -> Any:
    if isinstance(event, Mapping):
        return event.get(key, default)
    return getattr(event, key, default)


def _merged(event: Any) -> dict[str, Any]:
    payload = dict(_mapping(_value(event, "payload", {})))
    metadata = dict(_mapping(_value(event, "metadata", {})))
    payload_metadata = dict(_mapping(payload.get("metadata", {})))
    # Top-level payload values are authoritative. Nested payload metadata and
    # event metadata supply collector annotations in descending precedence.
    merged = dict(metadata)
    merged.update(payload_metadata)
    merged.update(payload)
    return merged


def _macro_mapping(
    config: Any, event: Any, values: Mapping[str, Any]
) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    mappings = config.get("macro_event_mappings", {})
    if not isinstance(mappings, Mapping):
        return {}
    keys = (
        _first(values, "series_id", "indicator", "indicator_id"),
        _first(values, "event_name", "release_name", "name"),
        _value(event, "event_type"),
    )
    for key in keys:
        if key is not None and isinstance(mappings.get(str(key)), Mapping):
            return mappings[str(key)]
    return {}


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _bounded_int(value: Any, default: int, lower: int, upper: int) -> int:
    try:
        return max(lower, min(upper, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _first(values: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in values and values[key] is not None:
            return values[key]
    return None


def _timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return None
    if result.tzinfo is None or result.utcoffset() is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _iso(value: Any) -> str | None:
    stamp = _timestamp(value)
    return stamp.isoformat() if stamp is not None else None


def _json_value(value: Any) -> Any:
    """Return a JSONB-compatible, finite representation of an external value."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, (datetime, UUID)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, set):
        return [_json_value(item) for item in sorted(value, key=lambda item: str(item))]
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)


def _json_text(value: Any) -> str:
    return json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"))


def _event_type(event: Any) -> str:
    value = _value(event, "event_type", "")
    return getattr(value, "value", str(value)).strip().lower()


def canonical_release_identity(event: Any) -> str:
    """Build the stable identity shared by a release and all its revisions."""
    values = _merged(event)
    source = (
        str(_value(event, "source", values.get("source", "unknown")) or "unknown")
        .strip()
        .lower()
    )
    explicit = _first(
        values, "canonical_release_id", "release_identity", "release_key", "release_id"
    )
    if explicit is not None and str(explicit).strip():
        return f"{source}:{str(explicit).strip()}"
    event_name = _first(
        values,
        "event_name",
        "release_name",
        "name",
        "title",
        "series_name",
        "series_id",
    )
    series = _first(values, "series_id", "indicator", "indicator_id")
    reference = _first(
        values, "reference_period", "period", "observation_period", "observed_at"
    )
    if reference is not None:
        reference = _iso(reference) or str(reference).strip()
    pieces = [
        str(part).strip()
        for part in (event_name or series or "macro_release", reference)
        if part is not None and str(part).strip()
    ]
    return source + ":" + ":".join(pieces)


def _history_surprises(history: Iterable[Any] | None) -> list[float]:
    values: list[float] = []
    if history is None:
        return values
    for item in history:
        if isinstance(item, Mapping):
            direct = _first(item, "absolute_surprise", "surprise")
            if direct is None:
                actual = _finite(_first(item, "actual", "value"))
                consensus = _finite(_first(item, "consensus", "forecast", "expected"))
                direct = (
                    actual - consensus
                    if actual is not None and consensus is not None
                    else None
                )
        else:
            direct = item
        number = _finite(direct)
        if number is not None:
            values.append(number)
    return values


def standardized_surprise(
    absolute_surprise: Any, history: Iterable[Any] | None = None
) -> float | None:
    """Return a population z-score when at least two finite historical values exist."""
    current = _finite(absolute_surprise)
    values = _history_surprises(history)
    if current is None or len(values) < 2:
        return None
    deviation = statistics.pstdev(values)
    if not math.isfinite(deviation) or deviation == 0:
        return None
    result = (current - statistics.fmean(values)) / deviation
    return result if math.isfinite(result) else None


def _impact(values: Mapping[str, Any], hint: Any) -> str:
    explicit = _first(values, "impact", "impact_level", "importance")
    if explicit is not None and not isinstance(explicit, (int, float, Decimal)):
        label = str(explicit).strip().lower()
        if label in {"high", "medium", "low", "unknown"}:
            return label
    score = _finite(hint)
    if score is None:
        score = _finite(_first(values, "importance_hint", "importance_score"))
    if score is None:
        return "unknown"
    return "high" if score >= 0.75 else "medium" if score >= 0.4 else "low"


def _reaction_summary(values: Mapping[str, Any]) -> dict[str, Any]:
    summary = _first(values, "reaction_summary", "reaction", "market_reaction")
    return dict(_mapping(summary)) if isinstance(summary, Mapping) else {}


def advance_stage(
    current_stage: Any = "t0",
    observed_stage: Any = None,
    *,
    actual_present: bool = False,
    reaction_available: bool = False,
    finalized: bool = False,
) -> str:
    """Advance only forward through t0, developing, reaction, final."""
    current = str(getattr(current_stage, "value", current_stage) or "t0").lower()
    if current not in STAGES:
        current = "t0"
    candidate = observed_stage
    if candidate is None:
        candidate = (
            "final"
            if finalized
            else "reaction"
            if reaction_available
            else "t0"
            if actual_present
            else "developing"
        )
    candidate = str(getattr(candidate, "value", candidate) or "t0").lower()
    if candidate not in STAGES:
        candidate = "t0"
    if current == "developing" and candidate == "t0":
        return "t0"
    return STAGES[max(STAGES.index(current), STAGES.index(candidate))]


def build_macro_release_card(
    event: Any,
    history: Iterable[Any] | None = None,
    *,
    config: Any = None,
    now: Any = None,
    current_stage: Any = "t0",
) -> dict[str, Any]:
    """Construct a JSON-compatible card from one normalized market event."""
    values = _merged(event)
    macro_mapping = _macro_mapping(config, event, values)
    event_type = _event_type(event)
    if event_type not in {"macro_release", "macro_revision"}:
        raise ValueError("macro release card requires macro_release or macro_revision")
    actual = _finite(_first(values, "actual", "released_value", "value"))
    consensus = _finite(_first(values, "consensus", "forecast", "expected"))
    previous = _finite(_first(values, "previous", "prior", "previous_value"))
    revised_previous = _finite(
        _first(values, "revised_previous", "revised_prior", "revised_previous_value")
    )
    absolute = (
        round(actual - consensus, 12)
        if actual is not None and consensus is not None
        else None
    )
    flags: list[str] = []
    if event_type == "macro_revision":
        flags.append("revision")
    if actual is None:
        flags.append("missing_actual")
    if consensus is None:
        flags.append("missing_consensus")
    if previous is None and revised_previous is None:
        flags.append("missing_previous")
    if absolute is not None and standardized_surprise(absolute, history) is None:
        flags.append("insufficient_history")
    if (
        _timestamp(_first(values, "released_at", "release_time", "published_at"))
        is None
    ):
        flags.append("missing_release_timestamp")
    if actual is None:
        flags.append("developing")
    # Preserve declared flags, but never allow arbitrary/non-JSON values into JSONB.
    declared_flags = _first(values, "quality_flags", "quality")
    if isinstance(declared_flags, Mapping):
        declared_flags = [key for key, enabled in declared_flags.items() if enabled]
    if isinstance(declared_flags, (list, tuple, set)):
        flags.extend(str(item) for item in declared_flags if str(item).strip())
    flags = list(dict.fromkeys(flags))
    reaction = _reaction_summary(values)
    stage = advance_stage(
        current_stage,
        _first(values, "stage"),
        actual_present=actual is not None,
        reaction_available=bool(reaction),
        finalized=bool(_first(values, "finalized", "is_final")),
    )
    observed = _timestamp(_first(values, "observed_at", "effective_at"))
    released = _timestamp(_first(values, "released_at", "release_time", "published_at"))
    revision_at = _timestamp(_first(values, "revision_at", "revised_at"))
    source = str(
        _value(event, "source", values.get("source", "unknown")) or "unknown"
    ).strip()
    event_id = _value(event, "event_id", _value(event, "id"))
    revision_of_event_id = _value(event, "revision_of_event_id")
    provenance = {
        "event_id": str(event_id) if event_id is not None else None,
        "event_type": event_type,
        "source": source,
        "source_event_id": _value(event, "source_event_id"),
        "revision_of_event_id": str(revision_of_event_id)
        if revision_of_event_id is not None
        else None,
        "content_hash": _value(event, "content_hash"),
        "payload": _json_value(_value(event, "payload", {})),
        "metadata": _json_value(_value(event, "metadata", {})),
    }
    return {
        "release_identity": canonical_release_identity(event),
        "event_name": str(
            _first(values, "event_name", "release_name", "name", "title", "series_name")
            or macro_mapping.get("event_name")
            or _first(values, "series_id")
            or "macro_release"
        ).strip(),
        "series_id": str(
            _first(values, "series_id", "indicator", "indicator_id")
            or macro_mapping.get("series_id")
            or ""
        ).strip()
        or None,
        "actual": actual,
        "consensus": consensus,
        "previous": previous,
        "revised_previous": revised_previous,
        "absolute_surprise": absolute,
        "standardized_surprise": standardized_surprise(absolute, history),
        "impact": _impact(
            values, macro_mapping.get("priority", _value(event, "importance_hint"))
        ),
        "source": source,
        "observed_at": _iso(observed),
        "released_at": _iso(released),
        "revision_at": _iso(revision_at),
        "quality_flags": flags,
        "stage": stage,
        "reaction_summary": _json_value(reaction),
        "source_event_provenance": _json_value(provenance),
        "source_payload": _json_value(_value(event, "payload", {})),
        "source_event_id": event_id,
        "revision_of_event_id": revision_of_event_id,
        "supersedes_card_id": None,
        "revision_number": 0,
        "created_at": _iso(now)
        or _iso(_value(event, "created_at"))
        or _iso(observed)
        or "1970-01-01T00:00:00+00:00",
        # Explicit fields keep pre-release/developing state observable.
        "developing": actual is None,
        "developing_fields": [
            key
            for key, value in (
                ("actual", actual),
                ("consensus", consensus),
                ("previous", previous),
            )
            if value is None
        ],
    }



def upsert_macro_release_card(
    session: Any,
    event: Any,
    config: Any = None,
    now: Any = None,
) -> dict[str, Any] | None:
    """Insert one immutable card and move the current pointer atomically."""
    card = build_macro_release_card(event, config=config, now=now)
    if card["source_event_id"] is None:
        raise ValueError("market event id is required")
    identity = card["release_identity"]
    existing = result_first(
        session.execute(
            text(
                "SELECT * FROM macro_release_cards WHERE release_identity = :identity AND source_event_id = :source_event_id LIMIT 1"
            ),
            {"identity": identity, "source_event_id": card["source_event_id"]},
        )
    )
    if existing is not None:
        return dict(existing)
    current = result_first(
        session.execute(
            text(
                "SELECT c.*, p.stage AS pointer_stage FROM macro_release_cards_current p "
                "JOIN macro_release_cards c ON c.id = p.card_id "
                "WHERE p.release_identity = :identity LIMIT 1"
            ),
            {"identity": identity},
        )
    )
    history = result_rows(
        session.execute(
            text(
                "SELECT actual, consensus, absolute_surprise FROM macro_release_cards "
                "WHERE ((:series_id IS NOT NULL AND series_id = :series_id) "
                "OR (:series_id IS NULL AND event_name = :event_name)) "
                "ORDER BY observed_at ASC NULLS LAST, created_at ASC LIMIT :limit"
            ),
            {
                "series_id": card.get("series_id"),
                "event_name": card["event_name"],
                "limit": _MAX_READ,
            },
        )
    )
    if card["absolute_surprise"] is not None:
        score = standardized_surprise(card["absolute_surprise"], history)
        card["standardized_surprise"] = score
        if score is not None:
            card["quality_flags"] = [
                flag
                for flag in card.get("quality_flags", [])
                if flag != "insufficient_history"
            ]
    card["revision_number"] = (
        int(current.get("revision_number", -1)) + 1 if current else 0
    )
    card["supersedes_card_id"] = current.get("id") if current else None
    if current:
        card["stage"] = advance_stage(
            current.get("pointer_stage") or current.get("stage"), card["stage"]
        )
    params = {
        "release_identity": identity,
        "series_id": card.get("series_id"),
        "revision_number": card["revision_number"],
        "source_event_id": card["source_event_id"],
        "revision_of_event_id": card["revision_of_event_id"],
        "supersedes_card_id": card["supersedes_card_id"],
        "event_name": card["event_name"],
        "actual": card["actual"],
        "consensus": card["consensus"],
        "previous": card["previous"],
        "revised_previous": card["revised_previous"],
        "absolute_surprise": card["absolute_surprise"],
        "standardized_surprise": card["standardized_surprise"],
        "impact": card["impact"],
        "source": card["source"],
        "observed_at": _timestamp(card["observed_at"]),
        "released_at": _timestamp(card["released_at"]),
        "revision_at": _timestamp(card["revision_at"]),
        "quality_flags": _json_text(card["quality_flags"]),
        "stage": card["stage"],
        "reaction_summary": _json_text(card["reaction_summary"]),
        "source_event_provenance": _json_text(card["source_event_provenance"]),
        "source_payload": _json_text(card["source_payload"]),
        "created_at": _timestamp(card["created_at"]),
    }
    inserted = result_first(
        session.execute(
            text("""INSERT INTO macro_release_cards
        (release_identity, series_id, revision_number, source_event_id, revision_of_event_id, supersedes_card_id,
         event_name, actual, consensus, previous, revised_previous, absolute_surprise,
         standardized_surprise, impact, source, observed_at, released_at, revision_at,
         quality_flags, stage, reaction_summary, source_event_provenance, source_payload, created_at)
        VALUES (:release_identity, :series_id, :revision_number, :source_event_id, :revision_of_event_id, :supersedes_card_id,
         :event_name, :actual, :consensus, :previous, :revised_previous, :absolute_surprise,
         :standardized_surprise, :impact, :source, :observed_at, :released_at, :revision_at,
         CAST(:quality_flags AS JSONB), :stage, CAST(:reaction_summary AS JSONB),
         CAST(:source_event_provenance AS JSONB), CAST(:source_payload AS JSONB), :created_at)
        ON CONFLICT (release_identity, source_event_id) DO NOTHING RETURNING *"""),
            params,
        )
    )
    if inserted is None:
        duplicate = result_first(
            session.execute(
                text(
                    "SELECT * FROM macro_release_cards WHERE release_identity = :identity AND source_event_id = :source_event_id LIMIT 1"
                ),
                {"identity": identity, "source_event_id": card["source_event_id"]},
            )
        )
        return dict(duplicate) if duplicate else card
    card = dict(inserted)
    session.execute(
        text("""INSERT INTO macro_release_cards_current
        (release_identity, card_id, stage, reaction_summary, updated_at)
        VALUES (:identity, :card_id, :stage, CAST(:reaction_summary AS JSONB), :updated_at)
        ON CONFLICT (release_identity) DO UPDATE SET card_id = EXCLUDED.card_id,
            stage = EXCLUDED.stage, reaction_summary = EXCLUDED.reaction_summary,
            updated_at = EXCLUDED.updated_at"""),
        {
            "identity": identity,
            "card_id": card.get("id"),
            "stage": card.get("stage", "t0"),
            "reaction_summary": _json_text(card.get("reaction_summary", {})),
            "updated_at": _timestamp(now)
            or _timestamp(card.get("created_at"))
            or datetime.now(UTC),
        },
    )
    return card


def advance_macro_release_stage(
    session: Any,
    event_id: Any,
    stage: Any,
    reaction_summary: Mapping[str, Any] | None = None,
    now: Any = None,
) -> dict[str, Any] | None:
    """Advance the mutable current pointer without rewriting immutable history."""
    row = result_first(
        session.execute(
            text(
                "SELECT c.*, p.stage AS pointer_stage, p.reaction_summary AS pointer_reaction_summary "
                "FROM macro_release_cards_current p JOIN macro_release_cards c ON c.id = p.card_id "
                "WHERE c.source_event_id = :event_id OR c.id = :event_id LIMIT 1"
            ),
            {"event_id": event_id},
        )
    )
    if row is None:
        return None
    current = row.get("pointer_stage") or row.get("stage") or "t0"
    next_stage = advance_stage(
        current,
        stage,
        reaction_available=bool(reaction_summary),
        finalized=str(getattr(stage, "value", stage)).lower() == "final",
    )
    prior_reaction = (
        row.get("pointer_reaction_summary") or row.get("reaction_summary") or {}
    )
    summary = (
        _json_value(reaction_summary)
        if reaction_summary is not None
        else _json_value(prior_reaction)
    )
    if next_stage != current or reaction_summary is not None:
        session.execute(
            text(
                "UPDATE macro_release_cards_current SET stage = :stage, "
                "reaction_summary = CAST(:reaction_summary AS JSONB), updated_at = :updated_at "
                "WHERE card_id = :card_id"
            ),
            {
                "stage": next_stage,
                "reaction_summary": _json_text(summary),
                "updated_at": _timestamp(now) or datetime.now(UTC),
                "card_id": row.get("id"),
            },
        )
    result = dict(row)
    result["stage"] = next_stage
    result["reaction_summary"] = summary
    return result


def list_macro_release_cards(
    session: Any,
    limit: int = 100,
    offset: int = 0,
    release_identity: str | None = None,
    current_only: bool = False,
) -> list[Mapping[str, Any]]:
    """Read at most 500 cards in stable newest-first order."""
    bounded_limit = _bounded_int(limit, 100, 1, _MAX_READ)
    bounded_offset = _bounded_int(offset, 0, 0, 1000000)
    params: dict[str, Any] = {"limit": bounded_limit, "offset": bounded_offset}
    if current_only:
        query = (
            "SELECT c.*, p.stage AS pointer_stage, "
            "p.reaction_summary AS pointer_reaction_summary "
            "FROM macro_release_cards_current p JOIN macro_release_cards c ON c.id = p.card_id"
        )
    else:
        query = "SELECT c.* FROM macro_release_cards c"
    where: list[str] = []
    if release_identity:
        where.append("c.release_identity = :identity")
        params["identity"] = str(release_identity).strip()
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY c.observed_at DESC NULLS LAST, c.created_at DESC, c.id DESC LIMIT :limit OFFSET :offset"
    rows = result_rows(session.execute(text(query), params))
    if current_only:
        normalized: list[Mapping[str, Any]] = []
        for row in rows:
            value = dict(row)
            if value.get("pointer_stage"):
                value["stage"] = value["pointer_stage"]
            if value.get("pointer_reaction_summary") is not None:
                value["reaction_summary"] = value["pointer_reaction_summary"]
            normalized.append(value)
        return normalized
    return rows


def current_macro_release_card(
    session: Any, release_identity: str
) -> Mapping[str, Any] | None:
    row = result_first(
        session.execute(
            text(
                "SELECT c.*, p.stage AS pointer_stage, p.reaction_summary AS pointer_reaction_summary "
                "FROM macro_release_cards_current p JOIN macro_release_cards c ON c.id = p.card_id "
                "WHERE p.release_identity = :identity LIMIT 1"
            ),
            {"identity": str(release_identity).strip()},
        )
    )
    if row is None:
        return None
    value = dict(row)
    if value.get("pointer_stage"):
        value["stage"] = value["pointer_stage"]
    if value.get("pointer_reaction_summary") is not None:
        value["reaction_summary"] = value["pointer_reaction_summary"]
    return value


# Intentionally boring aliases for orchestrator callers and snapshot code.
parse_macro_release = build_macro_release_card
build_release_card = build_macro_release_card
ingest_macro_release_card = upsert_macro_release_card
list_release_cards = list_macro_release_cards
get_current_release_card = current_macro_release_card

__all__ = [
    "STAGES",
    "advance_stage",
    "advance_macro_release_stage",
    "build_macro_release_card",
    "build_release_card",
    "canonical_release_identity",
    "current_macro_release_card",
    "get_current_release_card",
    "ingest_macro_release_card",
    "list_macro_release_cards",
    "list_release_cards",
    "parse_macro_release",
    "standardized_surprise",
    "upsert_macro_release_card",
]
