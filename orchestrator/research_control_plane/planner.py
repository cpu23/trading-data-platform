"""Pure deterministic value-of-information planning policy."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, cast
from uuid import UUID

from .domain import PriorityInputs, QuestionForPlanning, QuestionStatus

PRIORITY_POLICY_VERSION = "v1"
DEFAULT_RUNTIME_WEIGHT = Decimal("0.001")
DEFAULT_REVIEW_WEIGHT = Decimal("0.01")
DEFAULT_EPSILON = Decimal("0.001")
_REQUIRED_COMPONENTS = (
    "materiality",
    "uncertainty",
    "discrimination_power",
    "urgency",
    "freshness_gap",
    "resolvability",
    "expected_cost_usd",
    "expected_runtime_seconds",
)


def _decimal(
    value: Decimal | float | int | str,
    name: str,
    *,
    minimum: Decimal,
    maximum: Decimal,
) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not result.is_finite() or result < minimum or result > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


@dataclass(frozen=True, slots=True)
class PriorityResult:
    policy_version: str
    score: Decimal | None
    benefit: Decimal | None
    penalty: Decimal | None
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlanPolicy:
    now: datetime
    cost_budget_usd: Decimal | float | int | str
    runtime_budget_seconds: int
    maximum_work_orders: int
    minimum_priority: Decimal | float | int | str
    policy_version: str = PRIORITY_POLICY_VERSION
    runtime_weight: Decimal | float | int | str = DEFAULT_RUNTIME_WEIGHT
    review_weight: Decimal | float | int | str = DEFAULT_REVIEW_WEIGHT
    epsilon: Decimal | float | int | str = DEFAULT_EPSILON

    def __post_init__(self) -> None:
        if self.now.tzinfo is None or self.now.utcoffset() is None:
            raise ValueError("planner now must be timezone-aware")
        if self.policy_version != PRIORITY_POLICY_VERSION:
            raise ValueError("unsupported research priority policy version")
        if (
            isinstance(self.runtime_budget_seconds, bool)
            or not isinstance(self.runtime_budget_seconds, int)
            or not 1 <= self.runtime_budget_seconds <= 86400
        ):
            raise ValueError("runtime_budget_seconds must be between 1 and 86400")
        if (
            isinstance(self.maximum_work_orders, bool)
            or not isinstance(self.maximum_work_orders, int)
            or not 1 <= self.maximum_work_orders <= 100
        ):
            raise ValueError("maximum_work_orders must be between 1 and 100")
        object.__setattr__(self, "now", self.now.astimezone(UTC))
        object.__setattr__(
            self,
            "cost_budget_usd",
            _decimal(
                self.cost_budget_usd,
                "cost_budget_usd",
                minimum=Decimal("0"),
                maximum=Decimal("100"),
            ),
        )
        object.__setattr__(
            self,
            "minimum_priority",
            _decimal(
                self.minimum_priority,
                "minimum_priority",
                minimum=Decimal("0"),
                maximum=Decimal("1000000"),
            ),
        )
        object.__setattr__(
            self,
            "runtime_weight",
            _decimal(
                self.runtime_weight,
                "runtime_weight",
                minimum=Decimal("0"),
                maximum=Decimal("100"),
            ),
        )
        object.__setattr__(
            self,
            "review_weight",
            _decimal(
                self.review_weight,
                "review_weight",
                minimum=Decimal("0"),
                maximum=Decimal("100"),
            ),
        )
        object.__setattr__(
            self,
            "epsilon",
            _decimal(
                self.epsilon,
                "epsilon",
                minimum=Decimal("0.000000001"),
                maximum=Decimal("1"),
            ),
        )


@dataclass(frozen=True, slots=True)
class PlanDecision:
    question_id: UUID
    decision: Literal["selected", "deferred", "blocked"]
    rank: int | None
    score: Decimal | None
    blockers: tuple[str, ...]
    reason_codes: tuple[str, ...]
    estimated_cost_usd: Decimal | None
    estimated_runtime_seconds: int | None


@dataclass(frozen=True, slots=True)
class Agenda:
    policy_version: str
    decisions: tuple[PlanDecision, ...]
    reserved_cost_usd: Decimal
    reserved_runtime_seconds: int
    no_op_reason: str | None

    @property
    def selected(self) -> tuple[PlanDecision, ...]:
        return tuple(item for item in self.decisions if item.decision == "selected")

    @property
    def deferred(self) -> tuple[PlanDecision, ...]:
        return tuple(item for item in self.decisions if item.decision == "deferred")

    @property
    def blocked(self) -> tuple[PlanDecision, ...]:
        return tuple(item for item in self.decisions if item.decision == "blocked")


def score_priority(
    inputs: PriorityInputs,
    *,
    policy_version: str = PRIORITY_POLICY_VERSION,
    runtime_weight: Decimal = DEFAULT_RUNTIME_WEIGHT,
    review_weight: Decimal = DEFAULT_REVIEW_WEIGHT,
    epsilon: Decimal = DEFAULT_EPSILON,
) -> PriorityResult:
    """Compute policy v1 without SQL, network, model calls or hidden defaults."""
    if policy_version != PRIORITY_POLICY_VERSION:
        raise ValueError("unsupported research priority policy version")
    blockers = tuple(
        f"{name}_unknown" for name in _REQUIRED_COMPONENTS if getattr(inputs, name) is None
    )
    if blockers:
        return PriorityResult(policy_version, None, None, None, blockers)

    materiality = cast(Decimal, inputs.materiality)
    uncertainty = cast(Decimal, inputs.uncertainty)
    discrimination = cast(Decimal, inputs.discrimination_power)
    urgency = cast(Decimal, inputs.urgency)
    freshness = cast(Decimal, inputs.freshness_gap)
    resolvability = cast(Decimal, inputs.resolvability)
    cost = cast(Decimal, inputs.expected_cost_usd)
    runtime = cast(int, inputs.expected_runtime_seconds)
    assert materiality is not None
    assert uncertainty is not None
    assert discrimination is not None
    assert urgency is not None
    assert freshness is not None
    assert resolvability is not None
    assert cost is not None
    assert runtime is not None

    benefit = (
        materiality
        * uncertainty
        * discrimination
        * urgency
        * freshness
        * resolvability
    )
    review_minutes = cast(Decimal | None, inputs.expected_human_review_minutes)
    review_penalty = (
        Decimal("0") if review_minutes is None else review_weight * review_minutes
    )
    penalty = cost + runtime_weight * runtime + review_penalty + epsilon
    return PriorityResult(
        policy_version=policy_version,
        score=benefit / penalty,
        benefit=benefit,
        penalty=penalty,
        blockers=(),
    )


def _decision(
    question: QuestionForPlanning,
    decision: Literal["selected", "deferred", "blocked"],
    result: PriorityResult,
    *reasons: str,
    rank: int | None = None,
) -> PlanDecision:
    return PlanDecision(
        question_id=question.id,
        decision=decision,
        rank=rank,
        score=result.score,
        blockers=tuple(sorted(set((*question.blockers, *result.blockers)))),
        reason_codes=tuple(sorted(set(reasons))) or (decision,),
        estimated_cost_usd=cast(
            Decimal | None, question.priority.expected_cost_usd
        ),
        estimated_runtime_seconds=question.priority.expected_runtime_seconds,
    )


def plan_questions(
    questions: Iterable[QuestionForPlanning], policy: PlanPolicy
) -> Agenda:
    """Select a stable bounded agenda under both cost and runtime ceilings."""
    runtime_weight = cast(Decimal, policy.runtime_weight)
    review_weight = cast(Decimal, policy.review_weight)
    epsilon = cast(Decimal, policy.epsilon)
    minimum_priority = cast(Decimal, policy.minimum_priority)
    cost_budget = cast(Decimal, policy.cost_budget_usd)
    supplied = tuple(questions)
    if not supplied:
        return Agenda(policy.policy_version, (), Decimal("0"), 0, "no_questions")

    fixed: list[PlanDecision] = []
    eligible: list[tuple[QuestionForPlanning, PriorityResult]] = []
    for question in supplied:
        result = score_priority(
            question.priority,
            policy_version=policy.policy_version,
            runtime_weight=runtime_weight,
            review_weight=review_weight,
            epsilon=epsilon,
        )
        if question.status not in (QuestionStatus.PENDING, QuestionStatus.PLANNED):
            fixed.append(_decision(question, "blocked", result, "status_not_plannable"))
        elif question.expires_at is not None and question.expires_at <= policy.now:
            fixed.append(_decision(question, "deferred", result, "expired"))
        elif question.not_before > policy.now:
            fixed.append(_decision(question, "deferred", result, "not_before"))
        elif question.blockers or result.blockers:
            fixed.append(
                _decision(
                    question,
                    "blocked",
                    result,
                    *(question.blockers or result.blockers),
                )
            )
        elif result.score is None:
            fixed.append(_decision(question, "blocked", result, "priority_unknown"))
        elif result.score < minimum_priority:
            fixed.append(
                _decision(question, "deferred", result, "below_minimum_priority")
            )
        else:
            eligible.append((question, result))

    far_future = datetime.max.replace(tzinfo=UTC)
    eligible.sort(
        key=lambda item: (
            -item[1].score,  # type: ignore[operator]
            item[0].due_at or far_future,
            item[0].created_at,
            str(item[0].id),
        )
    )

    selected: list[PlanDecision] = []
    deferred: list[PlanDecision] = []
    reserved_cost = Decimal("0")
    reserved_runtime = 0
    for question, result in eligible:
        cost = cast(Decimal, question.priority.expected_cost_usd)
        runtime = cast(int, question.priority.expected_runtime_seconds)
        assert cost is not None
        assert runtime is not None
        reasons: list[str] = []
        if len(selected) >= policy.maximum_work_orders:
            reasons.append("maximum_work_orders_reached")
        if reserved_cost + cost > cost_budget:
            reasons.append("cost_budget_exceeded")
        if reserved_runtime + runtime > policy.runtime_budget_seconds:
            reasons.append("runtime_budget_exceeded")
        if reasons:
            deferred.append(_decision(question, "deferred", result, *reasons))
            continue
        reserved_cost += cost
        reserved_runtime += runtime
        selected.append(
            _decision(
                question,
                "selected",
                result,
                "highest_value_within_budget",
                rank=len(selected) + 1,
            )
        )

    decisions = tuple(
        sorted(
            (*selected, *deferred, *fixed),
            key=lambda item: (
                0 if item.decision == "selected" else 1 if item.decision == "deferred" else 2,
                item.rank if item.rank is not None else 2**31,
                str(item.question_id),
            ),
        )
    )
    no_op_reason = None if selected else "no_eligible_questions"
    return Agenda(
        policy_version=policy.policy_version,
        decisions=decisions,
        reserved_cost_usd=reserved_cost,
        reserved_runtime_seconds=reserved_runtime,
        no_op_reason=no_op_reason,
    )


__all__ = [
    "Agenda",
    "PlanDecision",
    "PlanPolicy",
    "PriorityResult",
    "plan_questions",
    "score_priority",
]
