"""Pure deterministic scoring for the autonomous thesis-fusion desk.

This module owns no SQL, no I/O, and no model calls: every function is a
deterministic pure calculation over frozen input contracts from
``research_intelligence.contracts``. The repository layer
(``thesis_fusion.py``) consumes these APIs and owns persistence.

Scoring rules
-------------
- Evidence identity is ``evidence_fingerprint`` (SHA-256 of canonical
  content). Identical content repeated by any number of agents, roles, or
  syndicated sources is the same evidence and scores exactly once.
- Agent/role identifiers never appear in ``EvidenceSignal``; agreement
  between agents or models is not evidence. ``provenance`` is excluded from
  every calculation except the auditable-evidence predicate, which reads
  only its ``excerpt`` and ``structured_fields`` keys (never agent/role
  metadata).
- One auditable-evidence predicate is shared by persistence and scoring:
  ``is_auditable_evidence`` requires an explicitly positive quality score,
  an explicitly positive entailment score, and a nonblank bounded excerpt —
  or, for contradictions only, a non-empty structured observation payload
  in place of the excerpt. Null-excerpt or zero-quality placeholder rows
  (for example empty FRED/story rows) stay historical/context evidence:
  they are counted as context, never as support or contradiction, so they
  cannot raise evidence strength, confidence, or rank eligibility.
- Evidence is first deduplicated by fingerprint (keeping the highest-quality
  representative, ties broken by evidence id), then auditable directional
  evidence is capped: members sharing an ``independence_key`` form one
  group, and members without an independence key but sharing a
  ``source_family`` form a fallback group. Within a group the n-th member
  contributes ``decay**(n-1)`` of its adjusted weight (geometric series, so
  group mass is bounded by ``best_weight / (1 - decay)``).
- Adjusted weight = quality * entailment * freshness * effective_weight,
  where a missing freshness uses the neutral 0.5 (effective_weight defaults
  to 1.0) and is reported through explicit missing counters. Quality and
  entailment are never defaulted: a signal without explicit positive
  quality and entailment is not auditable and contributes nothing.
- Support and contradiction are computed separately as
  ``1 - exp(-total adjusted weight)``: bounded in [0, 1) with diminishing
  returns, over auditable directional evidence only. Context and
  invalidation evidence never contribute mass.
- Confidence is a calibrated blend of support mass, mean quality,
  diversity, mean freshness, and mean entailment, damped multiplicatively by
  contradiction mass. It is None when no directional evidence exists.
- Net expected return is ``sum(probability * return) - cost`` over priced
  scenarios; scenarios with missing probability are never defaulted to
  conviction and probabilities are never renormalized.
- Expected shortfall is the expected loss from negative-return scenarios
  only, and is exactly zero when no downside scenario exists.
- The opportunity score is gated by evidence strength, confidence, neglect,
  catalyst readiness, liquidity, and downside. A missing or sub-threshold
  component blocks the opportunity (score 0.0) and is reported explicitly.

Inputs are bounded (``MAX_EVIDENCE``, ``MAX_SCENARIOS``, ``MAX_CATALYSTS``)
and every numeric input is validated finite, so all outputs are finite and
deterministic regardless of input order.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any

from research_intelligence.contracts import (
    EvidenceScore,
    EvidenceSignal,
    OpportunityScore,
    Scenario,
    _finite_return,
)

# Bounded finite inputs.
MAX_EVIDENCE = 256
MAX_SCENARIOS = 64
MAX_CATALYSTS = 64

# Correlation cap: within one independence group the n-th member contributes
# decay**(n-1) of its adjusted weight, bounding group mass at
# best_weight / (1 - decay).
CORRELATION_DECAY = 0.5

# Neutral default for unknown grade scores; explicit missing counters stay set.
NEUTRAL_SCORE = 0.5
NEUTRAL_WEIGHT = 1.0

# Diversity reaches full credit at these independent-group/modality counts.
DIVERSITY_GROUP_TARGET = 4
DIVERSITY_MODALITY_TARGET = 3

# Calibrated confidence blend; weights sum to 1.0.
CONFIDENCE_WEIGHTS = MappingProxyType(
    {
        "support": 0.30,
        "quality": 0.20,
        "diversity": 0.15,
        "freshness": 0.15,
        "entailment": 0.20,
    }
)
CONTRADICTION_DAMPING = 0.75

# Neglect blend over whichever of attention/crowding is provided.
NEGLECT_WEIGHT_ATTENTION = 0.5
NEGLECT_WEIGHT_CROWDING = 0.5

CATALYST_HORIZON = timedelta(days=90)
CATALYST_PENDING_WITHOUT_DATE = 0.5
CATALYST_STATES = frozenset({"pending", "confirmed", "missed", "expired"})

# Opportunity gates: a component must be present and meet its threshold.
# Downside is a risk measure, so its gate passes when value <= threshold.
OPPORTUNITY_GATES = MappingProxyType(
    {
        "evidence": 0.30,
        "confidence": 0.40,
        "neglect": 0.20,
        "catalyst": 0.25,
        "liquidity": 0.20,
        "downside": 0.50,
    }
)
# Weighted blend over favorable components; weights sum to 1.0.
OPPORTUNITY_WEIGHTS = MappingProxyType(
    {
        "evidence": 0.30,
        "confidence": 0.25,
        "neglect": 0.15,
        "catalyst": 0.10,
        "liquidity": 0.10,
        "downside": 0.10,
    }
)
# Expected shortfall of 0.5 (a 50% expected loss) maps to full downside risk.
DOWNSIDE_NORMALIZER = 0.5


@dataclass(frozen=True, slots=True)
class CatalystSignal:
    """One catalyst leg for readiness assessment (state from the DB enum)."""

    description: str
    state: str
    expected_at: datetime | None

    @classmethod
    def create(
        cls,
        *,
        description: Any,
        state: Any = "pending",
        expected_at: datetime | str | None = None,
    ) -> CatalystSignal:
        text_value = " ".join(str(description or "").split())
        if not text_value:
            raise ValueError("catalyst description is required")
        kind = str(state or "").strip().casefold()
        if kind not in CATALYST_STATES:
            raise ValueError(f"unsupported catalyst state:{kind[:32]}")
        return cls(
            description=text_value[:2000],
            state=kind,
            expected_at=_timestamp(expected_at),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "state": self.state,
            "expected_at": (self.expected_at.isoformat() if self.expected_at else None),
        }


@dataclass(frozen=True, slots=True)
class CorrelationGroup:
    """One capped correlation group with per-member decay multipliers."""

    group_key: str
    group_kind: str
    members: tuple[EvidenceSignal, ...]
    multipliers: tuple[float, ...]
    total_weight: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_key": self.group_key,
            "group_kind": self.group_kind,
            "members": [item.to_dict() for item in self.members],
            "multipliers": list(self.multipliers),
            "total_weight": self.total_weight,
        }


@dataclass(frozen=True, slots=True)
class EvidenceCatalog:
    """Fingerprint-deduplicated evidence set with dropped duplicates."""

    total: int
    unique: tuple[EvidenceSignal, ...]
    dropped_duplicate_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "unique": [item.to_dict() for item in self.unique],
            "dropped_duplicate_ids": list(self.dropped_duplicate_ids),
        }


@dataclass(frozen=True, slots=True)
class IndependenceAssessment:
    """Correlation-group structure of a canonical evidence set."""

    group_count: int
    groups: tuple[CorrelationGroup, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_count": self.group_count,
            "groups": [group.to_dict() for group in self.groups],
        }


@dataclass(frozen=True, slots=True)
class NeglectScore:
    """Neglect (low attention/crowding) measure; higher means more neglected."""

    neglect: float | None
    attention: float | None
    crowding: float | None
    missing: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "neglect": self.neglect,
            "attention": self.attention,
            "crowding": self.crowding,
            "missing": list(self.missing),
        }


@dataclass(frozen=True, slots=True)
class CatalystScore:
    """Catalyst readiness in [0, 1]; None when no catalysts exist."""

    readiness: float | None
    catalyst_count: int
    confirmed_count: int
    pending_count: int
    missed_or_expired_count: int
    missing: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "readiness": self.readiness,
            "catalyst_count": self.catalyst_count,
            "confirmed_count": self.confirmed_count,
            "pending_count": self.pending_count,
            "missed_or_expired_count": self.missed_or_expired_count,
            "missing": list(self.missing),
        }


@dataclass(frozen=True, slots=True)
class ScenarioValuation:
    """Deterministic valuation of scenario legs.

    ``expected_value`` is sum(probability * return) - cost over priced
    scenarios only; missing probabilities are reported, never defaulted, and
    never renormalized (``probability_sum`` may differ from 1.0).
    ``expected_shortfall`` is the expected loss from negative-return
    scenarios only and is exactly zero when no downside exists.
    ``ranks`` order priced scenarios by contribution (ties broken by label,
    ascending), with unpriced scenarios last by label.
    """

    expected_value: float
    expected_shortfall: float
    probability_sum: float
    probabilities_sum_to_one: bool
    scenario_count: int
    priced_scenario_count: int
    missing_probability_count: int
    missing_probability_labels: tuple[str, ...]
    expected_values: Mapping[str, float]
    ranks: Mapping[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected_value": self.expected_value,
            "expected_shortfall": self.expected_shortfall,
            "probability_sum": self.probability_sum,
            "probabilities_sum_to_one": self.probabilities_sum_to_one,
            "scenario_count": self.scenario_count,
            "priced_scenario_count": self.priced_scenario_count,
            "missing_probability_count": self.missing_probability_count,
            "missing_probability_labels": list(self.missing_probability_labels),
            "expected_values": dict(self.expected_values),
            "ranks": dict(self.ranks),
        }


def _timestamp(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ValueError("timestamp must be datetime or ISO text")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def _bounded_component(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, str):
        raise ValueError(f"{name} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    if not 0.0 <= parsed <= 1.0:
        raise ValueError(f"{name} must be within [0, 1]")
    return parsed


def _adjusted_weight(item: EvidenceSignal) -> float:
    quality = item.quality_score if item.quality_score is not None else NEUTRAL_SCORE
    entailment = (
        item.entailment_score if item.entailment_score is not None else NEUTRAL_SCORE
    )
    freshness = (
        item.freshness_score if item.freshness_score is not None else NEUTRAL_SCORE
    )
    weight = (
        item.effective_weight if item.effective_weight is not None else NEUTRAL_WEIGHT
    )
    return quality * entailment * freshness * weight


def evidence_excerpt(signal: EvidenceSignal) -> str | None:
    """The signal's bounded verbatim excerpt, trimmed, or None when blank.

    The excerpt travels inside ``provenance["excerpt"]`` (persistence writes
    it from the source item's bounded excerpt and rebuilds it from the
    persisted ``excerpt`` column); it is the auditable content a reviewer
    can check against the thesis claim.
    """
    provenance = signal.provenance if isinstance(signal.provenance, Mapping) else {}
    excerpt = provenance.get("excerpt")
    if not isinstance(excerpt, str):
        return None
    return excerpt.strip() or None


def has_auditable_content(signal: EvidenceSignal) -> bool:
    """True when the signal carries a nonblank bounded excerpt."""
    return evidence_excerpt(signal) is not None


def has_structured_observation_payload(signal: EvidenceSignal) -> bool:
    """True when the signal carries a non-empty structured observation payload.

    Deterministic observation evidence (for example macro or market
    confirmation rows) can be auditable through its structured fields even
    when it has no narrative excerpt.
    """
    provenance = signal.provenance if isinstance(signal.provenance, Mapping) else {}
    payload = provenance.get("structured_fields")
    return isinstance(payload, Mapping) and bool(payload)


def is_auditable_evidence(
    signal: EvidenceSignal,
    *,
    allow_structured: bool = False,
) -> bool:
    """One auditable-evidence predicate, shared by persistence and scoring.

    A signal is auditable only with an explicitly positive quality score
    and an explicitly positive entailment score. It must additionally carry
    a nonblank bounded excerpt — or, when the signal is a contradiction
    (``relationship == 'contradicts'``) and ``allow_structured`` is true, a
    non-empty structured observation payload. Support always requires a
    verbatim excerpt: a structured payload never audits a support row. A
    stored zero or missing score normalizes to ``None`` inside
    ``EvidenceSignal``, so placeholder rows (empty FRED/story rows, unscored
    manual rows) fail closed here. Such rows remain historical/context
    evidence: they are counted, never contributed, so they cannot raise
    evidence strength, confidence, or rank eligibility.
    """
    if signal.quality_score is None or signal.quality_score <= 0.0:
        return False
    if signal.entailment_score is None or signal.entailment_score <= 0.0:
        return False
    if has_auditable_content(signal):
        return True
    return (
        allow_structured
        and str(signal.relationship).casefold() == "contradicts"
        and has_structured_observation_payload(signal)
    )


def evidence_quality_prior(item: Any) -> float:
    """Deterministic source/content quality prior, never a model judgment.

    One authoritative quality table shared by every signal-grading path
    (the autonomous cycle, the tournament, and persisted rebuilds): macro,
    official, filing, and market confirmation evidence scores 0.9;
    dedicated market-data adapters score 0.9; investment analyses and
    observations score 0.75; story clusters score 0.7; everything else
    defaults to 0.8. The value is always strictly positive, so quality
    alone never makes a row unauditable — the excerpt and entailment
    checks do that.
    """
    evidence_type = str(getattr(item, "evidence_type", "") or "")
    provenance = (
        item.provenance
        if isinstance(getattr(item, "provenance", None), Mapping)
        else {}
    )
    adapter = str(provenance.get("adapter") or "")
    if evidence_type in {
        "macro_observation",
        "macro_release",
        "official_document",
        "filing_delta",
        "market_confirmation",
    }:
        return 0.9
    if adapter in {
        "public_equities",
        "corporate_actions",
        "positioning_reports",
        "option_chain_snapshots",
    }:
        return 0.9
    if evidence_type in {"investment_analysis", "investment_observation"}:
        return 0.75
    if evidence_type == "story_cluster":
        return 0.7
    return 0.8


def _validate_decay(decay: float) -> None:
    try:
        parsed = float(decay)
    except (TypeError, ValueError) as exc:
        raise ValueError("decay must be a number") from exc
    if not math.isfinite(parsed) or not 0.0 < parsed < 1.0:
        raise ValueError("decay must be within (0, 1)")


def canonicalize_evidence(
    evidence: Sequence[EvidenceSignal],
    *,
    limit: int = MAX_EVIDENCE,
) -> EvidenceCatalog:
    """Deduplicate evidence by fingerprint, keeping the best representative.

    Identical content repeated by any number of agents or syndicated sources
    is one evidence item. The kept representative is the one with the highest
    adjusted weight (ties broken by evidence id, ascending), so the result is
    independent of input order. The unique set preserves first-occurrence
    order and is truncated to ``limit`` items.
    """
    if not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    items = list(evidence[:limit])
    best_by_fingerprint: dict[str, EvidenceSignal] = {}
    dropped: list[str] = []
    for item in items:
        current = best_by_fingerprint.get(item.evidence_fingerprint)
        if current is None:
            best_by_fingerprint[item.evidence_fingerprint] = item
            continue
        item_weight = _adjusted_weight(item)
        current_weight = _adjusted_weight(current)
        if item_weight > current_weight or (
            item_weight == current_weight and item.evidence_id < current.evidence_id
        ):
            best_by_fingerprint[item.evidence_fingerprint] = item
            dropped.append(current.evidence_id)
        else:
            dropped.append(item.evidence_id)
    return EvidenceCatalog(
        total=len(items),
        unique=tuple(best_by_fingerprint.values()),
        dropped_duplicate_ids=tuple(dropped),
    )


def assess_independence(
    evidence: Sequence[EvidenceSignal],
    *,
    limit: int = MAX_EVIDENCE,
    decay: float = CORRELATION_DECAY,
) -> IndependenceAssessment:
    """Group canonical evidence into capped correlation groups.

    Members sharing an ``independence_key`` form one group; members without
    one but sharing a ``source_family`` form a fallback group; anything else
    is its own group. Within a group the n-th member (sorted by adjusted
    weight descending, ties by evidence id) carries a ``decay**(n-1)``
    multiplier.
    """
    _validate_decay(decay)
    catalog = canonicalize_evidence(evidence, limit=limit)
    grouped: dict[str, tuple[str, list[EvidenceSignal]]] = {}
    for item in catalog.unique:
        if item.independence_key is not None:
            key = f"independence:{item.independence_key}"
            kind = "independence_key"
        elif item.source_family is not None:
            key = f"family:{item.source_family}"
            kind = "source_family"
        else:
            key = f"unique:{item.evidence_id}"
            kind = "unique"
        if key not in grouped:
            grouped[key] = (kind, [])
        grouped[key][1].append(item)
    groups: list[CorrelationGroup] = []
    for key in sorted(grouped):
        kind, members = grouped[key]
        members.sort(key=lambda item: (-_adjusted_weight(item), item.evidence_id))
        multipliers = tuple(decay**position for position in range(len(members)))
        total_weight = sum(
            _adjusted_weight(member) * multiplier
            for member, multiplier in zip(members, multipliers, strict=True)
        )
        groups.append(
            CorrelationGroup(
                group_key=key,
                group_kind=kind,
                members=tuple(members),
                multipliers=multipliers,
                total_weight=total_weight,
            )
        )
    return IndependenceAssessment(group_count=len(groups), groups=tuple(groups))


def assess_evidence(
    evidence: Sequence[EvidenceSignal],
    *,
    limit: int = MAX_EVIDENCE,
    decay: float = CORRELATION_DECAY,
) -> EvidenceScore:
    """Score support, contradiction, and confidence for one thesis.

    Support and contradiction masses are computed separately as
    1 - exp(-adjusted weight) after fingerprint deduplication and correlation
    capping, over auditable directional evidence only. A directional row
    contributes only when ``is_auditable_evidence`` passes (explicitly
    positive quality and entailment, plus a nonblank bounded excerpt, or a
    structured observation payload for contradictions); null-excerpt or
    zero-quality placeholder rows stay historical/context evidence and are
    counted as context instead. Context and invalidation evidence never add
    directional mass and are counted explicitly. Confidence is a calibrated
    blend (see module docstring) over the contributing evidence and is None
    when no auditable directional (supports/contradicts) evidence exists.
    """
    _validate_decay(decay)
    catalog = canonicalize_evidence(evidence, limit=limit)
    unique = catalog.unique

    def _contributable(item: EvidenceSignal) -> bool:
        if item.relationship == "supports":
            return is_auditable_evidence(item)
        if item.relationship == "contradicts":
            return is_auditable_evidence(item, allow_structured=True)
        return False

    directional_items = [item for item in unique if _contributable(item)]
    assessment = assess_independence(directional_items, limit=limit, decay=decay)
    support_ids: list[str] = []
    contradiction_ids: list[str] = []
    support_weight = 0.0
    contradiction_weight = 0.0
    for group in assessment.groups:
        for member, multiplier in zip(group.members, group.multipliers, strict=True):
            contribution = _adjusted_weight(member) * multiplier
            if member.relationship == "supports":
                support_weight += contribution
                support_ids.append(member.evidence_id)
            elif member.relationship == "contradicts":
                contradiction_weight += contribution
                contradiction_ids.append(member.evidence_id)
    support_mass = 1.0 - math.exp(-support_weight)
    contradiction_mass = 1.0 - math.exp(-contradiction_weight)

    support_count = sum(
        1 for item in directional_items if item.relationship == "supports"
    )
    contradiction_count = sum(
        1 for item in directional_items if item.relationship == "contradicts"
    )
    # Non-auditable directional rows stay historical/context: they cannot
    # increase evidence strength or rank eligibility.
    context_count = sum(1 for item in unique if item.relationship == "context") + sum(
        1
        for item in unique
        if item.relationship in ("supports", "contradicts") and not _contributable(item)
    )
    invalidation_count = sum(
        1 for item in unique if item.relationship == "invalidation"
    )
    directional = support_count + contradiction_count

    def _mean_scores(name: str) -> tuple[float | None, int]:
        values = [getattr(item, name) for item in directional_items]
        present = [value for value in values if value is not None]
        missing = sum(1 for value in values if value is None)
        if not present:
            return None, missing
        return sum(present) / len(present), missing

    average_quality, missing_quality = _mean_scores("quality_score")
    average_freshness, missing_freshness = _mean_scores("freshness_score")
    average_entailment, missing_entailment = _mean_scores("entailment_score")

    modalities = {item.evidence_type for item in directional_items}
    diversity = _clamp01(
        0.5
        * min(
            1.0,
            assessment.group_count / float(DIVERSITY_GROUP_TARGET),
        )
        + 0.5 * min(1.0, len(modalities) / float(DIVERSITY_MODALITY_TARGET))
    )

    if directional:
        quality = average_quality if average_quality is not None else NEUTRAL_SCORE
        freshness = (
            average_freshness if average_freshness is not None else NEUTRAL_SCORE
        )
        entailment = (
            average_entailment if average_entailment is not None else NEUTRAL_SCORE
        )
        base = (
            CONFIDENCE_WEIGHTS["support"] * support_mass
            + CONFIDENCE_WEIGHTS["quality"] * quality
            + CONFIDENCE_WEIGHTS["diversity"] * diversity
            + CONFIDENCE_WEIGHTS["freshness"] * freshness
            + CONFIDENCE_WEIGHTS["entailment"] * entailment
        )
        confidence = _clamp01(base * (1.0 - CONTRADICTION_DAMPING * contradiction_mass))
    else:
        confidence = None

    return EvidenceScore(
        evidence_input_count=catalog.total,
        unique_evidence_count=len(unique),
        support_count=support_count,
        contradiction_count=contradiction_count,
        context_count=context_count,
        invalidation_count=invalidation_count,
        independent_group_count=assessment.group_count,
        support_mass=support_mass,
        contradiction_mass=contradiction_mass,
        average_quality=average_quality,
        average_freshness=average_freshness,
        average_entailment=average_entailment,
        diversity=diversity,
        confidence=confidence,
        dropped_duplicate_ids=catalog.dropped_duplicate_ids,
        support_evidence_ids=tuple(support_ids),
        contradiction_evidence_ids=tuple(contradiction_ids),
        missing_quality_count=missing_quality,
        missing_freshness_count=missing_freshness,
        missing_entailment_count=missing_entailment,
    )


def calculate_neglect(
    *,
    attention: Any = None,
    crowding: Any = None,
) -> NeglectScore:
    """Neglect (low attention, low crowding); higher means more neglected.

    Blend of 1 - attention and 1 - crowding over whichever inputs are
    provided (weights renormalized); both missing yields neglect None with an
    explicit missing state rather than an invented value.
    """
    attention_value = _bounded_component(attention, "attention")
    crowding_value = _bounded_component(crowding, "crowding")
    present: list[tuple[str, float, float]] = []
    if attention_value is not None:
        present.append(("attention", NEGLECT_WEIGHT_ATTENTION, attention_value))
    if crowding_value is not None:
        present.append(("crowding", NEGLECT_WEIGHT_CROWDING, crowding_value))
    missing = tuple(
        name
        for name in ("attention", "crowding")
        if (attention_value if name == "attention" else crowding_value) is None
    )
    if not present:
        return NeglectScore(
            neglect=None, attention=None, crowding=None, missing=missing
        )
    total_weight = sum(weight for _, weight, _ in present)
    neglect = sum(weight * (1.0 - value) for _, weight, value in present) / total_weight
    return NeglectScore(
        neglect=_clamp01(neglect),
        attention=attention_value,
        crowding=crowding_value,
        missing=missing,
    )


def catalyst_readiness(
    catalysts: Sequence[CatalystSignal],
    *,
    as_of: datetime | str | None = None,
    limit: int = MAX_CATALYSTS,
) -> CatalystScore:
    """Readiness of a thesis catalyst set in [0, 1].

    Confirmed catalysts are ready (1.0); missed/expired contribute 0.0;
    pending catalysts score by proximity to ``as_of`` within
    ``CATALYST_HORIZON`` (overdue pending counts as ready), and pending
    catalysts without an expected date contribute the documented neutral 0.5
    while being counted as missing. An empty catalyst set yields readiness
    None with an explicit missing state.
    """
    if not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    if as_of is None:
        as_of = datetime.now(UTC)
    reference = _timestamp(as_of)
    if reference is None:
        raise ValueError("as_of must be a datetime")
    items = list(catalysts[:limit])
    if not items:
        return CatalystScore(
            readiness=None,
            catalyst_count=0,
            confirmed_count=0,
            pending_count=0,
            missed_or_expired_count=0,
            missing=("catalyst",),
        )
    values: list[float] = []
    confirmed = 0
    pending = 0
    dead = 0
    missing_dates = 0
    for catalyst in items:
        state = catalyst.state
        if state == "confirmed":
            values.append(1.0)
            confirmed += 1
        elif state == "pending":
            pending += 1
            if catalyst.expected_at is None:
                missing_dates += 1
                values.append(CATALYST_PENDING_WITHOUT_DATE)
            else:
                days_until = (
                    catalyst.expected_at - reference
                ).total_seconds() / 86400.0
                proximity = 1.0 - days_until / (CATALYST_HORIZON.days or 1)
                values.append(_clamp01(proximity))
        else:  # missed, expired
            values.append(0.0)
            dead += 1
    missing = ("catalyst_expected_at",) if missing_dates else ()
    return CatalystScore(
        readiness=sum(values) / len(values),
        catalyst_count=len(items),
        confirmed_count=confirmed,
        pending_count=pending,
        missed_or_expired_count=dead,
        missing=missing,
    )


def scenario_valuation(
    scenarios: Sequence[Scenario],
    *,
    cost: Any = 0.0,
    limit: int = MAX_SCENARIOS,
) -> ScenarioValuation:
    """Value scenario legs deterministically.

    Net expected return is sum(probability * return) - cost over priced
    scenarios; probabilities that do not sum to one are reported, never
    renormalized, and scenarios with missing probability are never defaulted
    to conviction. Expected shortfall is the expected loss from
    negative-return scenarios only. Ranks order priced scenarios by
    contribution (ties by label ascending), then unpriced scenarios by label.
    """
    if not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    cost_value = _finite_return(cost, "cost")
    items = list(scenarios[:limit])
    labels = [item.label for item in items]
    if len(set(labels)) != len(labels):
        raise ValueError("scenario labels must be unique")
    priced = [item for item in items if item.probability is not None]
    contributions = {
        item.label: item.probability * item.expected_return for item in priced
    }
    expected_value = sum(contributions.values()) - cost_value
    expected_shortfall = -sum(
        contribution for contribution in contributions.values() if contribution < 0.0
    )
    probability_sum = sum(
        item.probability for item in priced if item.probability is not None
    )
    unpriced_labels = [item.label for item in items if item.probability is None]
    ordered_priced = sorted(
        priced, key=lambda item: (-contributions[item.label], item.label)
    )
    ordered_unpriced = sorted(unpriced_labels)
    ranks: dict[str, int] = {}
    position = 0
    for item in ordered_priced:
        position += 1
        ranks[item.label] = position
    for label in ordered_unpriced:
        position += 1
        ranks[label] = position
    return ScenarioValuation(
        expected_value=expected_value,
        expected_shortfall=expected_shortfall,
        probability_sum=probability_sum,
        probabilities_sum_to_one=abs(probability_sum - 1.0) < 1e-9,
        scenario_count=len(items),
        priced_scenario_count=len(priced),
        missing_probability_count=len(unpriced_labels),
        missing_probability_labels=tuple(unpriced_labels),
        expected_values=MappingProxyType(contributions),
        ranks=MappingProxyType(ranks),
    )


def assess_opportunity(
    *,
    evidence_strength: Any = None,
    confidence: Any = None,
    neglect: Any = None,
    catalyst_ready: Any = None,
    liquidity: Any = None,
    downside: Any = None,
) -> OpportunityScore:
    """Gate and blend an opportunity score.

    Every component must be present (not None) and meet its threshold in
    ``OPPORTUNITY_GATES`` (downside passes when at or below its threshold).
    A missing or sub-threshold component yields opportunity 0.0 with the
    failed gates in ``blocked_by``; components without a value also appear in
    ``missing``. When all gates pass the opportunity is the weighted blend of
    favorable components (downside contributes 1 - downside).
    """
    parsed = {
        name: _bounded_component(value, name)
        for name, value in (
            ("evidence", evidence_strength),
            ("confidence", confidence),
            ("neglect", neglect),
            ("catalyst", catalyst_ready),
            ("liquidity", liquidity),
            ("downside", downside),
        )
    }
    gates: dict[str, bool] = {}
    blocked: list[str] = []
    for name, threshold in OPPORTUNITY_GATES.items():
        value = parsed[name]
        if name == "downside":
            passed = value is not None and value <= threshold
        else:
            passed = value is not None and value >= threshold
        gates[name] = passed
        if not passed:
            blocked.append(name)
    if blocked:
        opportunity = 0.0
    else:
        favorable: dict[str, float] = {
            "evidence": parsed["evidence"],
            "confidence": parsed["confidence"],
            "neglect": parsed["neglect"],
            "catalyst": parsed["catalyst"],
            "liquidity": parsed["liquidity"],
            "downside": 1.0 - parsed["downside"],
        }
        opportunity = sum(
            OPPORTUNITY_WEIGHTS[name] * favorable[name] for name in OPPORTUNITY_WEIGHTS
        )
    missing = tuple(name for name, value in parsed.items() if value is None)
    return OpportunityScore(
        opportunity=opportunity,
        evidence_strength=parsed["evidence"],
        confidence=parsed["confidence"],
        neglect=parsed["neglect"],
        catalyst_ready=parsed["catalyst"],
        liquidity=parsed["liquidity"],
        downside=parsed["downside"],
        gates=MappingProxyType(gates),
        missing=missing,
        blocked_by=tuple(blocked),
    )


__all__ = [
    "CATALYST_HORIZON",
    "CATALYST_PENDING_WITHOUT_DATE",
    "CATALYST_STATES",
    "CONFIDENCE_WEIGHTS",
    "CONTRADICTION_DAMPING",
    "CORRELATION_DECAY",
    "CatalystScore",
    "CatalystSignal",
    "CorrelationGroup",
    "DIVERSITY_GROUP_TARGET",
    "DIVERSITY_MODALITY_TARGET",
    "DOWNSIDE_NORMALIZER",
    "EvidenceCatalog",
    "IndependenceAssessment",
    "MAX_CATALYSTS",
    "MAX_EVIDENCE",
    "MAX_SCENARIOS",
    "NEGLECT_WEIGHT_ATTENTION",
    "NEGLECT_WEIGHT_CROWDING",
    "NEUTRAL_SCORE",
    "NEUTRAL_WEIGHT",
    "NeglectScore",
    "OPPORTUNITY_GATES",
    "OPPORTUNITY_WEIGHTS",
    "ScenarioValuation",
    "assess_evidence",
    "assess_independence",
    "assess_opportunity",
    "calculate_neglect",
    "canonicalize_evidence",
    "catalyst_readiness",
    "evidence_excerpt",
    "evidence_quality_prior",
    "has_auditable_content",
    "has_structured_observation_payload",
    "is_auditable_evidence",
    "scenario_valuation",
]
