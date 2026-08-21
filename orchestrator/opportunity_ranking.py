"""Deterministic opportunity orchestration and ranking.

This module composes the pure scoring primitives in ``thesis_scoring.py``
into one per-thesis assessment and a deterministic cross-thesis ranking. It
owns no SQL, no I/O, and no model calls: inputs are the frozen
``research_intelligence.contracts`` values plus validated plain numbers, and
every output is finite, deterministic, and independent of input order.

Orchestration rules
-------------------
- One thesis is assessed by running ``assess_evidence``,
  ``scenario_valuation``, ``calculate_neglect``, ``catalyst_readiness``, and
  ``assess_opportunity`` in a fixed order with the same bounded inputs the
  scoring layer expects; the composed result is an
  ``OpportunityAssessment``. Correlation inputs (``decay`` and the
  evidence/scenario/catalyst limits) pass through unchanged, so correlation
  capping behaves exactly as in the scoring layer.
- Scenario probabilities are never defaulted, renormalized, or reused as
  opportunity weights: expected value is exactly
  ``thesis_scoring.scenario_valuation``'s net return, and the missing
  probability mass (1 - sum of priced probabilities) is reported even when
  priced probabilities exceed one (the mass is then negative).
- Expected value, confidence, and catalyst readiness remain separate
  fields; the gated opportunity score is the only blended figure and is
  never treated as a probability.
- Ranking is a pure sort over each assessment's ``rank_tuple``. An
  assessment is *eligible* when every opportunity gate passes (no blocked
  gates and a positive score) and every scenario is priced (no missing
  probability). Eligible entries sort first, by expected value descending,
  then score descending, then confidence, catalyst readiness, and neglect
  descending, then thesis id ascending. Blocked or unscored entries
  (failed gates, zero score, or missing scenario probabilities) sort after
  every eligible entry, ordered by expected value descending, score
  descending, confidence descending, then thesis id ascending. The thesis
  id tie-breaker makes the order total, so the output is stable for any
  input order. Unknown components (``None`` confidence, catalyst
  readiness, or neglect) sort after every known value in their position:
  a missing value never gains priority from the sentinel encoding.
  Missing probability and value remain unknown and are never invented or
  promoted by ranking.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import thesis_scoring
from research_intelligence.contracts import (
    EvidenceSignal,
    Scenario,
    _finite_return,
)
from thesis_scoring import (
    CORRELATION_DECAY,
    MAX_CATALYSTS,
    MAX_EVIDENCE,
    MAX_SCENARIOS,
    CatalystSignal,
)

# Same identifier shape the contracts use for evidence ids.
_THESIS_ID_RE = re.compile(r"^[A-Za-z0-9._:/@+-]{1,240}$")


@dataclass(frozen=True, slots=True)
class OpportunityAssessment:
    """Composed assessment of one thesis's opportunity.

    ``expected_value`` and ``expected_shortfall`` are exactly the
    ``thesis_scoring.scenario_valuation`` figures (net of
    ``transaction_cost``); ``missing_probability_mass`` is
    ``1 - probability_sum`` over priced scenarios only, so probabilities are
    never defaulted or renormalized and the mass may be negative when priced
    probabilities exceed one. ``score`` is exactly the gated
    ``thesis_scoring.assess_opportunity`` blend and is never a probability.
    ``blocked_by`` names the failed opportunity gates (including components
    whose value is unknown); a non-empty ``blocked_by`` or any missing
    scenario probability makes the assessment ineligible for ranking.
    ``rank_tuple`` is the deterministic sort key documented in the module
    docstring: eligible assessments sort before blocked or unscored ones,
    unknown (``None``) confidence, catalyst readiness, and neglect
    components sort after every known value in their position, and the
    trailing thesis id tie-breaker makes the order total.
    """

    thesis_id: str
    as_of: datetime
    expected_value: float
    expected_shortfall: float
    probability_sum: float
    missing_probability_mass: float
    missing_probability_count: int
    missing_probability_labels: tuple[str, ...]
    transaction_cost: float
    evidence_strength: float
    confidence: float | None
    catalyst_readiness: float | None
    neglect: float | None
    liquidity: float | None
    downside: float | None
    independent_group_count: int
    blocked_by: tuple[str, ...]
    score: float
    rank_tuple: tuple[float | int | str, ...] = field(init=False)

    def __post_init__(self) -> None:
        # Descending components are encoded negated, so an unknown (None)
        # component carries the positive infinity sentinel: it sorts after
        # every known value in its position and can never gain priority.
        confidence = -self.confidence if self.confidence is not None else float("inf")
        if self.blocked_by or self.score <= 0.0 or self.missing_probability_count > 0:
            rank_tuple = (
                1,
                -self.expected_value,
                -self.score,
                confidence,
                self.thesis_id,
            )
        else:
            rank_tuple = (
                0,
                -self.expected_value,
                -self.score,
                confidence,
                (
                    -self.catalyst_readiness
                    if self.catalyst_readiness is not None
                    else float("inf")
                ),
                (-self.neglect if self.neglect is not None else float("inf")),
                self.thesis_id,
            )
        object.__setattr__(self, "rank_tuple", rank_tuple)

    @property
    def eligible(self) -> bool:
        """True when every gate passes and every scenario is priced."""
        return (
            not self.blocked_by
            and self.score > 0.0
            and self.missing_probability_count == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "thesis_id": self.thesis_id,
            "as_of": self.as_of.isoformat(),
            "expected_value": self.expected_value,
            "expected_shortfall": self.expected_shortfall,
            "probability_sum": self.probability_sum,
            "missing_probability_mass": self.missing_probability_mass,
            "missing_probability_count": self.missing_probability_count,
            "missing_probability_labels": list(self.missing_probability_labels),
            "transaction_cost": self.transaction_cost,
            "evidence_strength": self.evidence_strength,
            "confidence": self.confidence,
            "catalyst_readiness": self.catalyst_readiness,
            "neglect": self.neglect,
            "liquidity": self.liquidity,
            "downside": self.downside,
            "independent_group_count": self.independent_group_count,
            "blocked_by": list(self.blocked_by),
            "score": self.score,
            "eligible": self.eligible,
            "rank_tuple": list(self.rank_tuple),
        }


@dataclass(frozen=True, slots=True)
class RankedOpportunity:
    """One assessment at its deterministic 1-based rank position."""

    position: int
    assessment: OpportunityAssessment

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "assessment": self.assessment.to_dict(),
        }


def _bounded_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("thesis_id must be a string")
    if not _THESIS_ID_RE.fullmatch(value):
        raise ValueError("thesis_id contains unsupported characters")
    return value


def _as_of(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("as_of must be a datetime or ISO text") from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ValueError("as_of must be a datetime or ISO text")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def assess_opportunity(
    *,
    thesis_id: Any,
    evidence: Sequence[EvidenceSignal],
    scenarios: Sequence[Scenario],
    catalysts: Sequence[CatalystSignal] = (),
    attention: Any = None,
    crowding: Any = None,
    liquidity: Any = None,
    downside: Any = None,
    cost: Any = 0.0,
    as_of: datetime | str | None = None,
    decay: float = CORRELATION_DECAY,
    evidence_limit: int = MAX_EVIDENCE,
    scenario_limit: int = MAX_SCENARIOS,
    catalyst_limit: int = MAX_CATALYSTS,
) -> OpportunityAssessment:
    """Assess one thesis end to end over the thesis_scoring primitives.

    Runs ``assess_evidence``, ``scenario_valuation`` (with ``cost``),
    ``calculate_neglect``, ``catalyst_readiness`` (with ``as_of``), and
    ``assess_opportunity`` in that fixed order, passing the correlation
    inputs (``decay``, ``evidence_limit``, ``scenario_limit``,
    ``catalyst_limit``) through unchanged. All numeric inputs are validated
    by the scoring layer; missing values stay missing (never invented) and
    scenario probabilities are never normalized.
    """
    identifier = _bounded_id(thesis_id)
    reference = _as_of(as_of)
    evidence_score = thesis_scoring.assess_evidence(
        evidence, limit=evidence_limit, decay=decay
    )
    valuation = thesis_scoring.scenario_valuation(
        scenarios, cost=cost, limit=scenario_limit
    )
    neglect = thesis_scoring.calculate_neglect(attention=attention, crowding=crowding)
    catalyst = thesis_scoring.catalyst_readiness(
        catalysts, as_of=reference, limit=catalyst_limit
    )
    opportunity = thesis_scoring.assess_opportunity(
        evidence_strength=evidence_score.support_mass,
        confidence=evidence_score.confidence,
        neglect=neglect.neglect,
        catalyst_ready=catalyst.readiness,
        liquidity=liquidity,
        downside=downside,
    )
    return OpportunityAssessment(
        thesis_id=identifier,
        as_of=reference,
        expected_value=valuation.expected_value,
        expected_shortfall=valuation.expected_shortfall,
        probability_sum=valuation.probability_sum,
        missing_probability_mass=1.0 - valuation.probability_sum,
        missing_probability_count=valuation.missing_probability_count,
        missing_probability_labels=valuation.missing_probability_labels,
        transaction_cost=_finite_return(cost, "cost"),
        evidence_strength=evidence_score.support_mass,
        confidence=evidence_score.confidence,
        catalyst_readiness=catalyst.readiness,
        neglect=neglect.neglect,
        liquidity=opportunity.liquidity,
        downside=opportunity.downside,
        independent_group_count=evidence_score.independent_group_count,
        blocked_by=opportunity.blocked_by,
        score=opportunity.opportunity,
    )


def rank_opportunities(
    assessments: Sequence[OpportunityAssessment],
) -> tuple[RankedOpportunity, ...]:
    """Rank assessments deterministically by their ``rank_tuple``.

    Eligible entries (all gates passed, fully priced) come first, ordered by
    expected value descending, then score, confidence, catalyst readiness,
    and neglect descending; blocked or unscored entries follow, ordered by
    expected value, score, and confidence descending. Unknown (``None``)
    confidence, catalyst readiness, and neglect components sort after every
    known value in their position. The thesis id tie-breaker makes the
    order total, so the result is stable and independent of input order.
    Positions are 1-based.
    """
    ordered = sorted(assessments, key=lambda assessment: assessment.rank_tuple)
    return tuple(
        RankedOpportunity(position=position, assessment=assessment)
        for position, assessment in enumerate(ordered, start=1)
    )


__all__ = [
    "OpportunityAssessment",
    "RankedOpportunity",
    "assess_opportunity",
    "rank_opportunities",
]
