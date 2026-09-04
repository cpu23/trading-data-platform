"""Catalyst event playbooks: immutable, evidence-linked monitored scenarios.

A playbook turns one promotion-eligible tournament candidate's catalyst into
bounded, monitored event content that the autonomy cycle can match against
normalized market events (``market_events``, migration 027).  Playbooks are
pure monitoring content: no recommendation, entry/exit, stop/target, sizing,
allocation, or execution field exists anywhere in the module or its tables,
so no playbook can become a trading instruction.

Derivation rules (``build_event_playbook``) are deterministic and never
invent content:

* ``playbook_key`` groups the same thesis + catalyst + horizon (SHA-256 of
  the normalized identity fields).
* ``event_types`` is inferred only from the *cited* evidence: each
  evidence type maps to a fixed family of ``MarketEventType`` values
  (official documents/filings, news/transcripts/story clusters, macro,
  market state / prices / corporate actions, options/positioning via source
  keywords); evidence types with no mapping contribute nothing, and no
  other event type is ever invented.
* ``trigger_conditions`` carries the catalyst verbatim, ``invalidation_conditions``
  the candidate invalidators verbatim, ``confirmation_conditions`` the
  candidate's missing-evidence items verbatim.  ``bull/base/bear`` scenario
  legs are preserved exactly as supplied (unknown legs stay None, never
  fabricated).  ``cited_evidence_refs`` preserves the exact refs.
* ``input_fingerprint`` is content-addressed over every persisted content
  field, so an identical draft upserts idempotently and any content change
  yields a new fingerprint and a new immutable version.

Persistence (migration 051) keeps one active version per ``playbook_key``;
``upsert_event_playbook`` supersedes the active row (one-time NULL -> non-NULL
``superseded_at``) before inserting version+1, and never mutates history.
Matches are appended exactly once per (playbook, market_event, match_kind).
Every helper takes the caller's session and never commits or rolls back.

``event_matches_playbook`` is a pure matcher: event type membership plus
overlapping normalized symbol/company entities.  It performs no semantic
confirmation/invalidation inference — match *kind* is assigned by the caller
when recording a match.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any
from uuid import UUID

from events.contracts import MarketEventType
from research_intelligence.contracts import (
    NormalizedEvidence,
    Scenario,
    canonical_fingerprint,
    evidence_catalog,
    validate_evidence_references,
)
from sqlalchemy import text

from contracts.db_results import result_first, result_rows

MATCH_KINDS = ("trigger", "confirmation", "invalidation", "context")
SCENARIO_LABELS = ("bull", "base", "bear")

# Bounded pipeline inputs (mirror the tournament bounds where shared).
_MAX_EVENT_TYPES = 18  # full MarketEventType vocabulary size
_MAX_CONDITIONS = 20
_MAX_EVIDENCE_REFS = 30
_MAX_CATALYST_CHARS = 2000
_MAX_CONDITION_CHARS = 500
_MAX_REF_CHARS = 320
_MAX_ENTITY_KEYS = 8
_MAX_ENTITY_KEY_CHARS = 120
_MAX_ASSESSMENT_DEPTH = 6
_MAX_ASSESSMENT_STRING = 1000
_MAX_ASSESSMENT_ITEMS = 100
_MAX_LIST_LIMIT = 100
_DEFAULT_LIST_LIMIT = 50

_EVENT_TYPE_VALUES = frozenset(item.value for item in MarketEventType)

# Bounded horizon vocabulary: research-intelligence horizons used by
# tournament candidates plus the market-event horizon set (migration 051
# enforces the same union in SQL).
PLAYBOOK_HORIZONS = frozenset(
    {
        "intraday",
        "days",
        "weeks",
        "months",
        "multi_year",
        "unknown",
        "swing",
        "medium",
        "long_term",
    }
)

# Deterministic evidence-type -> MarketEventType family mapping.  Only these
# families are inferred; an evidence type absent from the table contributes
# no event types (unknown evidence never invents events).
_EVIDENCE_FAMILY_EVENT_TYPES = MappingProxyType(
    {
        # Official documents / filings.
        "official_document": ("regulatory_filing_published", "filing_ingested"),
        "filing_delta": ("regulatory_filing_published", "filing_ingested"),
        # News / transcripts / story clusters.
        "source_claim": ("headline_published", "story_updated"),
        "story_cluster": ("headline_published", "story_updated"),
        "market_confirmation": ("headline_published", "story_updated"),
        # Macro.
        "macro_observation": (
            "macro_release",
            "macro_revision",
            "central_bank_communication",
            "calendar_event_changed",
        ),
        "macro_release": (
            "macro_release",
            "macro_revision",
            "central_bank_communication",
            "calendar_event_changed",
        ),
        # Prices / corporate actions / volatility states.
        "market_state": (
            "price_tick",
            "price_bar_closed",
            "corporate_action_published",
            "volatility_state_changed",
            "correlation_state_changed",
        ),
        # Research observations/analyses map to the manual research channel.
        "investment_observation": ("manual_research_event",),
        "investment_analysis": ("manual_research_event",),
    }
)

# Deterministic source-name keyword families for options/positioning, which
# have no dedicated evidence type: the collector sources advertise
# "options/chain" and "positioning/CFTC" in their source names.
_SOURCE_KEYWORD_EVENT_TYPES = MappingProxyType(
    {
        "transcript": "transcript_published",
        "earnings call": "transcript_published",
        "option": "option_chain_published",
        "chain": "option_chain_published",
        "position": "positioning_report_published",
        "cftc": "positioning_report_published",
        "commitment of traders": "positioning_report_published",
    }
)


def _uuid(value: Any, field: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"invalid {field}") from None


def _text(value: Any, maximum: int, field: str) -> str | None:
    text_value = " ".join(str(value or "").split())
    if not text_value:
        return None
    if len(text_value) > maximum:
        raise ValueError(f"{field} exceeds maximum length")
    return text_value


def _text_required(value: Any, maximum: int, field: str) -> str:
    result = _text(value, maximum, field)
    if result is None:
        raise ValueError(f"{field} is required")
    return result


def _bounded_string_list(
    value: Any, maximum: int, item_maximum: int, field: str
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{field} must be an array")
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be an array")
    if len(value) > maximum:
        raise ValueError(f"{field} has too many items")
    output: list[str] = []
    for item in value:
        cleaned = _text(item, item_maximum, f"{field} item")
        if cleaned is None:
            raise ValueError(f"{field} contains a blank item")
        if cleaned not in output:
            output.append(cleaned)
    return tuple(output)


def _bounded_int(value: Any, field: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"invalid {field}")
    if value < minimum:
        raise ValueError(f"invalid {field}")
    return value


def _aware_timestamp(
    value: Any, field: str, *, default: datetime | None = None
) -> datetime | None:
    if value is None:
        return default
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"invalid {field}") from None
    else:
        raise ValueError(f"invalid {field}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _json_compatible(value: Any, *, path: str, depth: int = 0) -> None:
    """Validate a bounded, JSON-serializable assessment payload."""
    if depth > _MAX_ASSESSMENT_DEPTH:
        raise ValueError(f"{path} nests too deeply")
    if isinstance(value, str):
        if len(value) > _MAX_ASSESSMENT_STRING:
            raise ValueError(f"{path} contains an oversized string")
        return
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite float")
        return
    if isinstance(value, UUID):
        return
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{path} contains a naive datetime")
        return
    if isinstance(value, list | tuple):
        if len(value) > _MAX_ASSESSMENT_ITEMS:
            raise ValueError(f"{path} contains too many items")
        for index, item in enumerate(value):
            _json_compatible(item, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} has a non-string key")
            _json_compatible(item, path=f"{path}.{key}", depth=depth + 1)
        return
    raise ValueError(
        f"{path} contains non-JSON-compatible value {type(value).__name__}"
    )


def _assessment(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("assessment must be an object")
    payload = dict(value)
    _json_compatible(payload, path="assessment")
    return payload


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _thesis_exists(session: Any, thesis_id: str) -> bool:
    row = result_first(
        session.execute(
            text(
                "SELECT 1 AS present FROM investment_theses "
                "WHERE id = CAST(:id AS UUID) LIMIT 1"
            ),
            {"id": thesis_id},
        )
    )
    return row is not None


def _scenario_legs(raw: Any) -> dict[str, dict[str, Any]]:
    """Normalize candidate scenario legs to the three playbook slots."""
    legs: dict[str, dict[str, Any]] = {}
    if raw is None:
        return legs
    if not isinstance(raw, (list, tuple, Mapping)):
        raise ValueError("scenarios must be an array of scenario legs")
    items = raw if isinstance(raw, (list, tuple)) else list(raw.values())
    for item in items:
        if isinstance(item, Scenario):
            leg = item
        elif isinstance(item, Mapping):
            try:
                leg = Scenario.create(
                    label=item.get("label"),
                    probability=item.get("probability"),
                    expected_return=item.get("expected_return"),
                )
            except ValueError as exc:
                message = str(exc)
                field = (
                    "probability"
                    if "probability" in message
                    else "expected_return"
                    if "expected_return" in message
                    else "scenario"
                )
                raise ValueError(f"invalid {field}: {message}") from exc
        else:
            raise ValueError("scenario leg must be a Scenario or mapping")
        if leg.label not in SCENARIO_LABELS:
            raise ValueError(f"unsupported scenario label:{str(leg.label)[:32]}")
        if leg.label in legs:
            raise ValueError(f"duplicate scenario label:{leg.label}")
        legs[leg.label] = leg.to_dict()
    return legs


def _family_event_types(item: NormalizedEvidence) -> frozenset[str]:
    """Deterministic MarketEventType family for one cited evidence item."""
    provenance = item.provenance if isinstance(item.provenance, Mapping) else {}
    if str(provenance.get("source") or "").casefold() == "company_expectations":
        return frozenset({"calendar_event_changed", "manual_research_event"})
    kinds = set(_EVIDENCE_FAMILY_EVENT_TYPES.get(str(item.evidence_type), ()))
    source_name = _normalized_text(item.source_name)
    for token, kind in _SOURCE_KEYWORD_EVENT_TYPES.items():
        if token in source_name:
            kinds.add(kind)
    return frozenset(kinds)


def _entity_keys(*, subject: Any, instrument: Any) -> tuple[str, ...]:
    keys: list[str] = []
    for raw in (subject, instrument):
        token = _normalized_text(raw)
        if token and token not in keys:
            keys.append(token)
    if len(keys) > _MAX_ENTITY_KEYS:
        raise ValueError("entity_keys has too many items")
    for key in keys:
        if len(key) > _MAX_ENTITY_KEY_CHARS:
            raise ValueError("entity key exceeds maximum length")
    return tuple(keys)


# ---------------------------------------------------------------------------
# Domain drafts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlaybookDraft:
    """One immutable event-playbook version, before persistence.

    ``create`` is the strict constructor: every field is validated and
    bounded, event types must come from the ``MarketEventType`` vocabulary,
    timestamps must be timezone-aware, and scenario legs are preserved
    exactly (unknown legs stay None).  ``entity_keys`` and ``as_of`` are
    caller metadata, not persisted content and not part of the fingerprint.
    """

    thesis_id: str
    playbook_key: str
    thesis_version: int
    catalyst: str
    horizon: str
    expected_at: datetime | None
    event_types: tuple[str, ...]
    trigger_conditions: tuple[str, ...]
    confirmation_conditions: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    bull_scenario: Mapping[str, Any] | None
    base_scenario: Mapping[str, Any] | None
    bear_scenario: Mapping[str, Any] | None
    cited_evidence_refs: tuple[str, ...]
    input_fingerprint: str
    entity_keys: tuple[str, ...]
    as_of: datetime

    @classmethod
    def create(
        cls,
        *,
        thesis_id: Any,
        playbook_key: Any,
        thesis_version: Any = None,
        catalyst: Any,
        horizon: Any,
        expected_at: Any = None,
        event_types: Any = (),
        trigger_conditions: Any = (),
        confirmation_conditions: Any = (),
        invalidation_conditions: Any = (),
        scenarios: Any = (),
        bull_scenario: Any = None,
        base_scenario: Any = None,
        bear_scenario: Any = None,
        cited_evidence_refs: Any = (),
        input_fingerprint: Any = None,
        entity_keys: Any = (),
        as_of: Any = None,
    ) -> PlaybookDraft:
        thesis = _uuid(thesis_id, "thesis_id")
        key = _text_required(playbook_key, 64, "playbook_key")
        if len(key) != 64 or not all(
            character in "0123456789abcdef" for character in key
        ):
            raise ValueError("invalid playbook_key")
        version = _bounded_int(
            1 if thesis_version is None else thesis_version, "thesis_version"
        )
        catalyst_text = _text_required(catalyst, _MAX_CATALYST_CHARS, "catalyst")
        horizon_text = _text_required(horizon, 32, "horizon")
        if horizon_text not in PLAYBOOK_HORIZONS:
            raise ValueError(f"unsupported horizon:{horizon_text[:32]}")
        expected = _aware_timestamp(expected_at, "expected_at")
        kinds = _bounded_string_list(event_types, _MAX_EVENT_TYPES, 64, "event_types")
        unknown_kinds = sorted(kind for kind in kinds if kind not in _EVENT_TYPE_VALUES)
        if unknown_kinds:
            raise ValueError(f"unsupported event type:{unknown_kinds[0][:32]}")
        # Canonical, stable order regardless of caller ordering.
        kinds = tuple(sorted(kinds))
        triggers = _bounded_string_list(
            trigger_conditions,
            _MAX_CONDITIONS,
            _MAX_CATALYST_CHARS,
            "trigger_conditions",
        )
        confirmations = _bounded_string_list(
            confirmation_conditions,
            _MAX_CONDITIONS,
            _MAX_CONDITION_CHARS,
            "confirmation_conditions",
        )
        invalidations = _bounded_string_list(
            invalidation_conditions,
            _MAX_CONDITIONS,
            _MAX_CONDITION_CHARS,
            "invalidation_conditions",
        )
        legs = _scenario_legs(scenarios)
        provided_legs = {
            label: value
            for label, value in (
                ("bull", bull_scenario),
                ("base", base_scenario),
                ("bear", bear_scenario),
            )
            if value is not None
        }
        if legs and provided_legs:
            raise ValueError("pass scenarios or the per-leg scenario fields, not both")
        if not legs and provided_legs:
            legs = _scenario_legs(provided_legs)
        refs = _bounded_string_list(
            cited_evidence_refs,
            _MAX_EVIDENCE_REFS,
            _MAX_REF_CHARS,
            "cited_evidence_refs",
        )
        fingerprint = _text_required(input_fingerprint, 64, "input_fingerprint")
        if not (
            len(fingerprint) == 64
            and all(character in "0123456789abcdef" for character in fingerprint)
        ):
            raise ValueError("invalid input_fingerprint")
        keys = _bounded_string_list(
            entity_keys, _MAX_ENTITY_KEYS, _MAX_ENTITY_KEY_CHARS, "entity_keys"
        )
        frozen_at = _aware_timestamp(as_of, "as_of", default=datetime.now(UTC))
        if frozen_at is None:
            raise ValueError("as_of is required")
        return cls(
            thesis_id=thesis,
            playbook_key=key,
            thesis_version=version,
            catalyst=catalyst_text,
            horizon=horizon_text,
            expected_at=expected,
            event_types=kinds,
            trigger_conditions=triggers,
            confirmation_conditions=confirmations,
            invalidation_conditions=invalidations,
            bull_scenario=legs.get("bull"),
            base_scenario=legs.get("base"),
            bear_scenario=legs.get("bear"),
            cited_evidence_refs=refs,
            input_fingerprint=fingerprint,
            entity_keys=keys,
            as_of=frozen_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "thesis_id": self.thesis_id,
            "playbook_key": self.playbook_key,
            "thesis_version": self.thesis_version,
            "catalyst": self.catalyst,
            "horizon": self.horizon,
            "expected_at": self.expected_at,
            "event_types": list(self.event_types),
            "trigger_conditions": list(self.trigger_conditions),
            "confirmation_conditions": list(self.confirmation_conditions),
            "invalidation_conditions": list(self.invalidation_conditions),
            "bull_scenario": (
                dict(self.bull_scenario) if self.bull_scenario is not None else None
            ),
            "base_scenario": (
                dict(self.base_scenario) if self.base_scenario is not None else None
            ),
            "bear_scenario": (
                dict(self.bear_scenario) if self.bear_scenario is not None else None
            ),
            "cited_evidence_refs": list(self.cited_evidence_refs),
            "input_fingerprint": self.input_fingerprint,
            "entity_keys": list(self.entity_keys),
            "as_of": self.as_of,
        }


@dataclass(frozen=True, slots=True)
class PlaybookEventMatch:
    """Facts of one pure event/playbook overlap, with no semantic kind."""

    matched: bool
    playbook_id: str | None = None
    playbook_key: str | None = None
    event_id: str | None = None
    event_type: str | None = None
    matched_entities: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.matched

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "playbook_id": self.playbook_id,
            "playbook_key": self.playbook_key,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "matched_entities": list(self.matched_entities),
        }


# ---------------------------------------------------------------------------
# Pure builder
# ---------------------------------------------------------------------------


def build_event_playbook(
    candidate: Any,
    evidence: Sequence[NormalizedEvidence] | Mapping[str, NormalizedEvidence],
    *,
    thesis_id: Any,
    thesis_version: Any = None,
    as_of: Any = None,
    expected_at: Any = None,
) -> PlaybookDraft:
    """Derive one deterministic event-playbook draft from a validated
    tournament candidate and its cited evidence.

    The candidate may be a ``CandidateDraft`` (or any object with
    ``to_dict``) or a mapping with the same keys: ``catalyst``, ``horizon``,
    ``scenarios``, ``invalidators``, ``missing_evidence``,
    ``evidence_refs``, ``subject``, ``instrument``.  Nothing is invented:
    event types come only from the supplied evidence families, conditions
    come verbatim from the candidate fields, and the fingerprint covers all
    persisted content.
    """
    if isinstance(candidate, Mapping):
        candidate_values = dict(candidate)
    else:
        to_dict = getattr(candidate, "to_dict", None)
        if not callable(to_dict):
            raise ValueError("candidate must be a validated candidate mapping or draft")
        candidate_values = dict(to_dict())

    thesis = _uuid(thesis_id, "thesis_id")
    catalyst = _text_required(
        candidate_values.get("catalyst"), _MAX_CATALYST_CHARS, "catalyst"
    )
    horizon = _text_required(candidate_values.get("horizon"), 32, "horizon")
    if horizon not in PLAYBOOK_HORIZONS:
        raise ValueError(f"unsupported horizon:{horizon[:32]}")

    catalog = evidence_catalog(evidence)
    refs = validate_evidence_references(
        candidate_values.get("evidence_refs") or (), catalog
    )
    if len(refs) > _MAX_EVIDENCE_REFS:
        raise ValueError("evidence_refs has too many items")
    expected = _aware_timestamp(expected_at, "expected_at")

    kinds: set[str] = set()
    for ref in refs:
        kinds.update(_family_event_types(catalog[ref]))
    unknown = sorted(kind for kind in kinds if kind not in _EVENT_TYPE_VALUES)
    if unknown:
        raise ValueError(f"unsupported event type:{unknown[0][:32]}")
    event_types = tuple(sorted(kinds))

    triggers = _bounded_string_list(
        [catalyst], _MAX_CONDITIONS, _MAX_CATALYST_CHARS, "trigger_conditions"
    )
    confirmations = _bounded_string_list(
        candidate_values.get("missing_evidence") or (),
        _MAX_CONDITIONS,
        _MAX_CONDITION_CHARS,
        "confirmation_conditions",
    )
    invalidations = _bounded_string_list(
        candidate_values.get("invalidators") or (),
        _MAX_CONDITIONS,
        _MAX_CONDITION_CHARS,
        "invalidation_conditions",
    )

    thesis_version_value = _bounded_int(
        1 if thesis_version is None else thesis_version, "thesis_version"
    )
    legs = _scenario_legs(candidate_values.get("scenarios"))
    entity_keys_value = _entity_keys(
        subject=candidate_values.get("subject"),
        instrument=candidate_values.get("instrument"),
    )

    fingerprint = canonical_fingerprint(
        {
            "thesis_id": thesis,
            "thesis_version": thesis_version_value,
            "catalyst": catalyst,
            "horizon": horizon,
            "expected_at": expected,
            "event_types": list(event_types),
            "trigger_conditions": list(triggers),
            "confirmation_conditions": list(confirmations),
            "invalidation_conditions": list(invalidations),
            "scenarios": [dict(leg) for leg in legs.values()],
            "cited_evidence_refs": list(refs),
        }
    )
    key = canonical_fingerprint(
        {
            "thesis_id": thesis,
            "catalyst": _normalized_text(catalyst),
            "horizon": _normalized_text(horizon),
        }
    )

    return PlaybookDraft.create(
        thesis_id=thesis,
        playbook_key=key,
        thesis_version=thesis_version_value,
        catalyst=catalyst,
        horizon=horizon,
        expected_at=expected,
        event_types=event_types,
        trigger_conditions=triggers,
        confirmation_conditions=confirmations,
        invalidation_conditions=invalidations,
        scenarios=[dict(leg) for leg in legs.values()],
        cited_evidence_refs=refs,
        input_fingerprint=fingerprint,
        entity_keys=entity_keys_value,
        as_of=as_of,
    )


# ---------------------------------------------------------------------------
# Repository helpers (no commits; bounded, stably ordered reads)
# ---------------------------------------------------------------------------


def upsert_event_playbook(session: Any, draft: Any) -> dict[str, Any]:
    """Persist one playbook draft idempotently (append-only versioning).

    A draft whose fingerprint matches the active version is a no-op.
    Changed content supersedes the active row (one-time NULL -> non-NULL
    ``superseded_at``) and inserts version+1, preserving point-in-time
    history.  Never commits.  Returns ``{"id", "version", "changed"}``.
    """
    if isinstance(draft, Mapping) and not isinstance(draft, PlaybookDraft):
        draft = PlaybookDraft.create(**dict(draft))
    if not isinstance(draft, PlaybookDraft):
        raise ValueError("draft must be a PlaybookDraft or mapping")
    if not _thesis_exists(session, draft.thesis_id):
        raise ValueError("unknown thesis")
    active = result_first(
        session.execute(
            text(
                """SELECT id, thesis_id, version, input_fingerprint
               FROM investment_thesis_event_playbooks
               WHERE playbook_key = :playbook_key
                 AND superseded_at IS NULL LIMIT 1"""
            ),
            {"playbook_key": draft.playbook_key},
        )
    )
    if active is not None:
        if str(active["thesis_id"]) != draft.thesis_id:
            raise ValueError("playbook_key already in use by another thesis")
        if str(active["input_fingerprint"]) == draft.input_fingerprint:
            return {
                "id": str(active["id"]),
                "version": int(active["version"]),
                "changed": False,
            }
        session.execute(
            text(
                """UPDATE investment_thesis_event_playbooks
                   SET superseded_at = NOW()
                   WHERE id = CAST(:id AS UUID) AND superseded_at IS NULL"""
            ),
            {"id": str(active["id"])},
        )
        next_version = int(active["version"]) + 1
    else:
        next_version = 1
    row = result_first(
        session.execute(
            text(
                """INSERT INTO investment_thesis_event_playbooks
               (thesis_id, playbook_key, version, thesis_version, catalyst,
                horizon, expected_at, event_types, trigger_conditions,
                confirmation_conditions, invalidation_conditions,
                bull_scenario, base_scenario, bear_scenario,
                cited_evidence_refs, input_fingerprint)
               VALUES (CAST(:thesis_id AS UUID), :playbook_key, :version,
                       :thesis_version, :catalyst, :horizon, :expected_at,
                       CAST(:event_types AS TEXT[]),
                       CAST(:trigger_conditions AS JSONB),
                       CAST(:confirmation_conditions AS JSONB),
                       CAST(:invalidation_conditions AS JSONB),
                       CAST(:bull_scenario AS JSONB),
                       CAST(:base_scenario AS JSONB),
                       CAST(:bear_scenario AS JSONB),
                       CAST(:cited_evidence_refs AS TEXT[]),
                       :input_fingerprint)
               RETURNING id, version"""
            ),
            {
                "thesis_id": draft.thesis_id,
                "playbook_key": draft.playbook_key,
                "version": next_version,
                "thesis_version": draft.thesis_version,
                "catalyst": draft.catalyst,
                "horizon": draft.horizon,
                "expected_at": draft.expected_at,
                "event_types": list(draft.event_types),
                "trigger_conditions": json.dumps(list(draft.trigger_conditions)),
                "confirmation_conditions": json.dumps(
                    list(draft.confirmation_conditions)
                ),
                "invalidation_conditions": json.dumps(
                    list(draft.invalidation_conditions)
                ),
                "bull_scenario": (
                    json.dumps(dict(draft.bull_scenario))
                    if draft.bull_scenario is not None
                    else None
                ),
                "base_scenario": (
                    json.dumps(dict(draft.base_scenario))
                    if draft.base_scenario is not None
                    else None
                ),
                "bear_scenario": (
                    json.dumps(dict(draft.bear_scenario))
                    if draft.bear_scenario is not None
                    else None
                ),
                "cited_evidence_refs": list(draft.cited_evidence_refs),
                "input_fingerprint": draft.input_fingerprint,
            },
        )
    )
    return {
        "id": str(row["id"]),
        "version": int(row["version"]),
        "changed": True,
    }


def record_event_match(
    session: Any,
    *,
    playbook_id: Any,
    market_event_id: Any,
    match_kind: Any,
    evidence_refs: Any = (),
    observed_at: Any = None,
    assessment: Any = None,
) -> bool:
    """Append one (playbook, event, kind) match exactly once.

    Validates playbook and market-event existence, the allowed match kind,
    bounded evidence refs and a bounded JSON-compatible assessment object,
    and a timezone-aware point-in-time ``observed_at`` (defaults to now).
    A duplicate recording is an idempotent no-op returning False.  Never
    commits.  The kind is caller-assigned: this helper performs no semantic
    confirmation/invalidation inference.
    """
    playbook_uuid = _uuid(playbook_id, "playbook_id")
    event_uuid = _uuid(market_event_id, "market_event_id")
    kind = str(match_kind or "").strip().casefold()
    if kind not in MATCH_KINDS:
        raise ValueError(f"unsupported match_kind:{kind[:32]}")
    refs = _bounded_string_list(
        evidence_refs, _MAX_EVIDENCE_REFS, _MAX_REF_CHARS, "evidence_refs"
    )
    observed = _aware_timestamp(observed_at, "observed_at", default=datetime.now(UTC))
    if observed is None:
        raise ValueError("observed_at is required")
    assessment_value = _assessment(assessment) if assessment is not None else {}

    playbook = result_first(
        session.execute(
            text(
                "SELECT 1 AS present FROM investment_thesis_event_playbooks "
                "WHERE id = CAST(:id AS UUID) LIMIT 1"
            ),
            {"id": playbook_uuid},
        )
    )
    if playbook is None:
        raise ValueError("unknown playbook")
    event = result_first(
        session.execute(
            text(
                "SELECT 1 AS present FROM market_events "
                "WHERE id = CAST(:id AS UUID) LIMIT 1"
            ),
            {"id": event_uuid},
        )
    )
    if event is None:
        raise ValueError("unknown market event")
    existing = result_first(
        session.execute(
            text(
                """SELECT 1 AS present FROM investment_thesis_event_matches
               WHERE playbook_id = CAST(:playbook_id AS UUID)
                 AND market_event_id = CAST(:market_event_id AS UUID)
                 AND match_kind = :match_kind LIMIT 1"""
            ),
            {
                "playbook_id": playbook_uuid,
                "market_event_id": event_uuid,
                "match_kind": kind,
            },
        )
    )
    if existing is not None:
        return False
    session.execute(
        text(
            """INSERT INTO investment_thesis_event_matches
               (playbook_id, market_event_id, match_kind, evidence_refs,
                observed_at, assessment)
               VALUES (CAST(:playbook_id AS UUID),
                       CAST(:market_event_id AS UUID), :match_kind,
                       CAST(:evidence_refs AS TEXT[]), :observed_at,
                       CAST(:assessment AS JSONB))
               ON CONFLICT (playbook_id, market_event_id, match_kind) DO NOTHING"""
        ),
        {
            "playbook_id": playbook_uuid,
            "market_event_id": event_uuid,
            "match_kind": kind,
            "evidence_refs": list(refs),
            "observed_at": observed,
            "assessment": json.dumps(assessment_value),
        },
    )
    return True


def list_due_playbooks(
    session: Any,
    *,
    reference: Any = None,
    limit: int = _DEFAULT_LIST_LIMIT,
) -> list[dict[str, Any]]:
    """List active playbooks due for event matching, stably ordered.

    "Due" means the expected catalyst time has arrived (or is unknown, so
    monitoring starts immediately).  Ordered by (expected_at ASC NULLS LAST,
    created_at ASC, id ASC) with a clamped limit; never commits.
    """
    reference_at = _aware_timestamp(reference, "reference", default=datetime.now(UTC))
    if reference_at is None:
        raise ValueError("reference is required")
    bounded_limit = max(1, min(_MAX_LIST_LIMIT, int(limit)))
    return result_rows(
        session.execute(
            text(
                """SELECT p.id, p.thesis_id, p.playbook_key, p.version,
                      p.thesis_version, p.catalyst, p.horizon,
                      p.expected_at, p.event_types, p.trigger_conditions,
                      p.confirmation_conditions, p.invalidation_conditions,
                      p.bull_scenario, p.base_scenario, p.bear_scenario,
                      p.cited_evidence_refs, p.input_fingerprint,
                      p.superseded_at, p.created_at,
                      ARRAY_REMOVE(ARRAY[t.company, t.symbol], NULL)
                          AS entity_keys
               FROM investment_thesis_event_playbooks AS p
               JOIN investment_theses AS t ON t.id = p.thesis_id
               WHERE p.superseded_at IS NULL
                 AND (p.expected_at IS NULL OR p.expected_at <= :reference)
               ORDER BY p.expected_at ASC NULLS LAST,
                        p.created_at ASC, p.id ASC
               LIMIT :limit"""
            ),
            {"reference": reference_at, "limit": bounded_limit},
        )
    )


def list_playbook_history(
    session: Any,
    *,
    thesis_id: Any = None,
    playbook_key: Any = None,
    limit: int = _DEFAULT_LIST_LIMIT,
) -> list[dict[str, Any]]:
    """List playbook versions for a thesis and/or key, newest version first.

    At least one of ``thesis_id`` or ``playbook_key`` is required.  Ordered
    by (playbook_key ASC, version DESC, id ASC) with a clamped limit; the
    superseded/active history is never mutated here.
    """
    if thesis_id is None and playbook_key is None:
        raise ValueError("thesis_id or playbook_key is required")
    thesis = _uuid(thesis_id, "thesis_id") if thesis_id is not None else None
    key = (
        _text_required(playbook_key, 64, "playbook_key")
        if playbook_key is not None
        else None
    )
    bounded_limit = max(1, min(_MAX_LIST_LIMIT, int(limit)))
    filters: list[str] = []
    params: dict[str, Any] = {"limit": bounded_limit}
    if thesis is not None:
        filters.append("thesis_id = CAST(:thesis_id AS UUID)")
        params["thesis_id"] = thesis
    if key is not None:
        filters.append("playbook_key = :playbook_key")
        params["playbook_key"] = key
    return result_rows(
        session.execute(
            text(
                """SELECT id, thesis_id, playbook_key, version, thesis_version,
                      catalyst, horizon, expected_at, event_types,
                      trigger_conditions, confirmation_conditions,
                      invalidation_conditions, bull_scenario, base_scenario,
                      bear_scenario, cited_evidence_refs, input_fingerprint,
                      superseded_at, created_at
               FROM investment_thesis_event_playbooks
               WHERE """
                + " AND ".join(filters)
                + """
               ORDER BY playbook_key ASC, version DESC, id ASC
               LIMIT :limit"""
            ),
            params,
        )
    )


# ---------------------------------------------------------------------------
# Pure event matching (no semantic inference)
# ---------------------------------------------------------------------------


def _event_type_of(event: Any) -> str | None:
    value = (
        event.get("event_type")
        if isinstance(event, Mapping)
        else getattr(event, "event_type", None)
    )
    kind = str(value or "").strip()
    return kind if kind in _EVENT_TYPE_VALUES else None


def _event_tokens(event: Any) -> set[str]:
    """Normalized symbol/company tokens of one market event."""
    tokens: set[str] = set()
    markets = (
        event.get("markets")
        if isinstance(event, Mapping)
        else getattr(event, "markets", ())
    )
    for market in markets or ():
        symbol = (
            market.get("symbol")
            if isinstance(market, Mapping)
            else getattr(market, "symbol", None)
        )
        canonical = (
            market.get("canonical_id")
            if isinstance(market, Mapping)
            else getattr(market, "canonical_id", None)
        )
        for value in (symbol, canonical):
            token = _normalized_text(value)
            if token:
                tokens.add(token)
    entities = (
        event.get("entities")
        if isinstance(event, Mapping)
        else getattr(event, "entities", ())
    )
    for entity in entities or ():
        if isinstance(entity, Mapping):
            kind = entity.get("entity_type")
            canonical = entity.get("canonical_id")
            display = entity.get("display_name")
        else:
            kind = getattr(entity, "entity_type", None)
            canonical = getattr(entity, "canonical_id", None)
            display = getattr(entity, "display_name", None)
        if str(kind or "") not in (
            "company",
            "instrument",
            "symbol",
            "security",
            "market",
            "macro_region",
            "concept",
        ):
            continue
        for value in (canonical, display):
            token = _normalized_text(value)
            if token:
                tokens.add(token)
    return tokens


def _playbook_types(playbook: Any) -> frozenset[str]:
    value = (
        playbook.get("event_types")
        if isinstance(playbook, Mapping)
        else getattr(playbook, "event_types", ())
    )
    return frozenset(
        str(item) for item in (value or ()) if str(item) in _EVENT_TYPE_VALUES
    )


def _playbook_entity_keys(playbook: Any, entity_keys: Sequence[str]) -> frozenset[str]:
    if isinstance(playbook, Mapping):
        stored = playbook.get("entity_keys") or ()
    else:
        stored = getattr(playbook, "entity_keys", ()) or ()
    return frozenset(
        _normalized_text(item)
        for item in (*tuple(entity_keys), *tuple(stored))
        if _normalized_text(item)
    )


def event_matches_playbook(
    event: Any,
    playbook: Any,
    *,
    entity_keys: Sequence[str] = (),
) -> PlaybookEventMatch:
    """Pure overlap test: event type plus normalized symbol/company entities.

    The event may be a ``MarketEvent`` or a mapping with ``event_type``,
    ``markets``, ``entities``; the playbook a ``PlaybookDraft`` or a row
    mapping with ``event_types``.  No semantic confirmation/invalidation
    inference is performed: a match is purely type membership plus at least
    one overlapping normalized entity token.  With no playbook entity keys
    the match is conservative (False).
    """
    event_type = _event_type_of(event)
    event_id = None
    if isinstance(event, Mapping):
        event_id = event.get("event_id")
    else:
        event_id = getattr(event, "event_id", None)
    playbook_id = (
        playbook.get("playbook_id")
        if isinstance(playbook, Mapping)
        else getattr(playbook, "playbook_id", None)
    )
    playbook_key = (
        playbook.get("playbook_key")
        if isinstance(playbook, Mapping)
        else getattr(playbook, "playbook_key", None)
    )
    if event_type is None:
        return PlaybookEventMatch(
            matched=False,
            playbook_id=playbook_id,
            playbook_key=playbook_key,
            event_id=event_id,
        )
    playbook_types = _playbook_types(playbook)
    if event_type not in playbook_types:
        return PlaybookEventMatch(
            matched=False,
            playbook_id=playbook_id,
            playbook_key=playbook_key,
            event_id=event_id,
            event_type=event_type,
        )
    playbook_keys = _playbook_entity_keys(playbook, entity_keys)
    if not playbook_keys:
        return PlaybookEventMatch(
            matched=False,
            playbook_id=playbook_id,
            playbook_key=playbook_key,
            event_id=event_id,
            event_type=event_type,
        )
    overlap = sorted(playbook_keys & _event_tokens(event))
    if not overlap:
        return PlaybookEventMatch(
            matched=False,
            playbook_id=playbook_id,
            playbook_key=playbook_key,
            event_id=event_id,
            event_type=event_type,
        )
    return PlaybookEventMatch(
        matched=True,
        playbook_id=playbook_id,
        playbook_key=playbook_key,
        event_id=event_id,
        event_type=event_type,
        matched_entities=tuple(overlap),
    )


__all__ = [
    "MATCH_KINDS",
    "PLAYBOOK_HORIZONS",
    "PlaybookDraft",
    "PlaybookEventMatch",
    "build_event_playbook",
    "event_matches_playbook",
    "list_due_playbooks",
    "list_playbook_history",
    "record_event_match",
    "upsert_event_playbook",
]
