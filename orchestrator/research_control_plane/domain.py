"""Pure domain rules for durable atomic research questions."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID

MAX_QUESTION_TEXT = 2000
MAX_TARGET_REF = 500
MAX_SOURCE_FAMILIES = 32
MAX_EVIDENCE_FIELDS = 32

ORIGIN_KINDS = frozenset(
    {
        "promoted_candidate",
        "falsification",
        "stale_dependency",
        "catalyst_confirmation",
        "forecast_resolution",
        "source_event",
        "source_gap",
        "manual",
    }
)
QUESTION_TYPES = frozenset(
    {
        "earnings_guidance_delta",
        "filing_peer_readthrough",
        "positioning_divergence",
        "thesis_challenge",
        "forecast_resolution",
        "catalyst_confirmation",
        "evidence_refresh",
        "source_gap",
    }
)
TARGET_KINDS = frozenset(
    {"thesis", "group", "forecast", "catalyst", "entity", "source"}
)


class QuestionStatus(StrEnum):
    PENDING = "pending"
    PLANNED = "planned"
    QUEUED = "queued"
    RUNNING = "running"
    RESOLVED = "resolved"
    UNRESOLVABLE = "unresolvable"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


TERMINAL_QUESTION_STATUSES = frozenset(
    {
        QuestionStatus.RESOLVED,
        QuestionStatus.UNRESOLVABLE,
        QuestionStatus.EXPIRED,
        QuestionStatus.CANCELLED,
    }
)
QUESTION_TRANSITIONS: Mapping[QuestionStatus, frozenset[QuestionStatus]] = (
    MappingProxyType(
        {
            QuestionStatus.PENDING: frozenset(
                {
                    QuestionStatus.PLANNED,
                    QuestionStatus.UNRESOLVABLE,
                    QuestionStatus.EXPIRED,
                    QuestionStatus.CANCELLED,
                }
            ),
            QuestionStatus.PLANNED: frozenset(
                {
                    QuestionStatus.PENDING,
                    QuestionStatus.QUEUED,
                    QuestionStatus.UNRESOLVABLE,
                    QuestionStatus.EXPIRED,
                    QuestionStatus.CANCELLED,
                }
            ),
            QuestionStatus.QUEUED: frozenset(
                {
                    QuestionStatus.PLANNED,
                    QuestionStatus.RUNNING,
                    QuestionStatus.EXPIRED,
                    QuestionStatus.CANCELLED,
                }
            ),
            QuestionStatus.RUNNING: frozenset(
                {
                    QuestionStatus.PLANNED,
                    QuestionStatus.RESOLVED,
                    QuestionStatus.UNRESOLVABLE,
                    QuestionStatus.EXPIRED,
                    QuestionStatus.CANCELLED,
                }
            ),
            QuestionStatus.RESOLVED: frozenset(),
            QuestionStatus.UNRESOLVABLE: frozenset(),
            QuestionStatus.EXPIRED: frozenset(),
            QuestionStatus.CANCELLED: frozenset(),
        }
    )
)


def _aware(value: datetime, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _bounded_text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{name} must contain 1..{maximum} characters")
    return normalized


def _bounded_decimal(
    value: Decimal | float | int | str | None,
    name: str,
    *,
    minimum: Decimal,
    maximum: Decimal,
) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not number.is_finite() or number < minimum or number > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return number

def _canonical(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        number = _bounded_decimal(
            value,
            "canonical numeric value",
            minimum=Decimal("-1e100"),
            maximum=Decimal("1e100"),
        )
        return format(number, "f") if number is not None else None
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("canonical numeric value must be finite")
        return format(value, "f")
    if isinstance(value, datetime):
        return _aware(value, "canonical datetime").isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("canonical mappings require string keys")
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    raise ValueError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Return a stable, finite, ASCII-safe semantic representation."""
    return json.dumps(
        _canonical(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def content_fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class QuestionCandidate:
    origin_kind: str
    question_type: str
    atomic_question: str
    target_kind: str
    target_ref: str
    accepted_cutoff: datetime
    required_evidence_shape: Mapping[str, Any] = field(default_factory=dict)
    acceptable_source_families: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        origin = str(self.origin_kind).strip().lower()
        question_type = str(self.question_type).strip().lower()
        target_kind = str(self.target_kind).strip().lower()
        if origin not in ORIGIN_KINDS:
            raise ValueError("unsupported research question origin")
        if question_type not in QUESTION_TYPES:
            raise ValueError("unsupported research question type")
        if target_kind not in TARGET_KINDS:
            raise ValueError("unsupported research question target kind")
        question = _bounded_text(
            self.atomic_question, "atomic_question", MAX_QUESTION_TEXT
        )
        target_ref = _bounded_text(self.target_ref, "target_ref", MAX_TARGET_REF)
        cutoff = _aware(self.accepted_cutoff, "accepted_cutoff")
        if not isinstance(self.required_evidence_shape, Mapping):
            raise ValueError("required_evidence_shape must be an object")
        if len(self.required_evidence_shape) > MAX_EVIDENCE_FIELDS:
            raise ValueError("required_evidence_shape is too large")
        evidence = _canonical(self.required_evidence_shape)
        families = tuple(
            sorted(
                {
                    _bounded_text(item, "source family", 100).lower()
                    for item in self.acceptable_source_families
                }
            )
        )
        if len(families) > MAX_SOURCE_FAMILIES:
            raise ValueError("too many acceptable source families")
        object.__setattr__(self, "origin_kind", origin)
        object.__setattr__(self, "question_type", question_type)
        object.__setattr__(self, "atomic_question", question)
        object.__setattr__(self, "target_kind", target_kind)
        object.__setattr__(self, "target_ref", target_ref)
        object.__setattr__(self, "accepted_cutoff", cutoff)
        object.__setattr__(self, "required_evidence_shape", MappingProxyType(evidence))
        object.__setattr__(self, "acceptable_source_families", families)


def question_key(candidate: QuestionCandidate) -> str:
    """Identify one semantic question independent of a refresh cutoff."""
    return content_fingerprint(
        {
            "schema_version": 1,
            "question_type": candidate.question_type,
            "atomic_question": candidate.atomic_question.casefold(),
            "target_kind": candidate.target_kind,
            "target_ref": candidate.target_ref.casefold(),
            "required_evidence_shape": candidate.required_evidence_shape,
            "acceptable_source_families": candidate.acceptable_source_families,
        }
    )


def question_fingerprint(candidate: QuestionCandidate) -> str:
    """Identify one semantic question at one accepted point-in-time boundary.

    Origin is intentionally excluded: the same atomic question discovered by a
    challenge and a source event must share one active ledger row.
    """
    return content_fingerprint(
        {
            "schema_version": 1,
            "question_type": candidate.question_type,
            "atomic_question": candidate.atomic_question.casefold(),
            "target_kind": candidate.target_kind,
            "target_ref": candidate.target_ref.casefold(),
            "accepted_cutoff": candidate.accepted_cutoff,
            "required_evidence_shape": candidate.required_evidence_shape,
            "acceptable_source_families": candidate.acceptable_source_families,
        }
    )


def validate_question_transition(
    current: QuestionStatus | str, target: QuestionStatus | str
) -> QuestionStatus:
    current_status = QuestionStatus(current)
    target_status = QuestionStatus(target)
    if current_status == target_status:
        return target_status
    if current_status in TERMINAL_QUESTION_STATUSES:
        raise ValueError("terminal research question cannot return to an active state")
    if target_status not in QUESTION_TRANSITIONS[current_status]:
        raise ValueError(
            f"invalid research question transition: {current_status.value} -> {target_status.value}"
        )
    return target_status


@dataclass(frozen=True, slots=True)
class PriorityInputs:
    materiality: Decimal | float | int | str | None
    uncertainty: Decimal | float | int | str | None
    discrimination_power: Decimal | float | int | str | None
    urgency: Decimal | float | int | str | None
    freshness_gap: Decimal | float | int | str | None
    resolvability: Decimal | float | int | str | None
    expected_cost_usd: Decimal | float | int | str | None
    expected_runtime_seconds: int | None
    expected_human_review_minutes: Decimal | float | int | str | None = None

    def __post_init__(self) -> None:
        for name in (
            "materiality",
            "uncertainty",
            "discrimination_power",
            "urgency",
            "freshness_gap",
            "resolvability",
        ):
            object.__setattr__(
                self,
                name,
                _bounded_decimal(
                    getattr(self, name),
                    name,
                    minimum=Decimal("0"),
                    maximum=Decimal("1"),
                ),
            )
        object.__setattr__(
            self,
            "expected_cost_usd",
            _bounded_decimal(
                self.expected_cost_usd,
                "expected_cost_usd",
                minimum=Decimal("0"),
                maximum=Decimal("100"),
            ),
        )
        runtime = self.expected_runtime_seconds
        if runtime is not None and (
            isinstance(runtime, bool)
            or not isinstance(runtime, int)
            or runtime < 0
            or runtime > 86400
        ):
            raise ValueError("expected_runtime_seconds must be between 0 and 86400")
        object.__setattr__(
            self,
            "expected_human_review_minutes",
            _bounded_decimal(
                self.expected_human_review_minutes,
                "expected_human_review_minutes",
                minimum=Decimal("0"),
                maximum=Decimal("1440"),
            ),
        )


@dataclass(frozen=True, slots=True)
class QuestionForPlanning:
    id: UUID
    accepted_cutoff: datetime
    priority: PriorityInputs
    status: QuestionStatus
    not_before: datetime
    due_at: datetime | None
    expires_at: datetime | None
    created_at: datetime
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID):
            raise ValueError("id must be a UUID")
        object.__setattr__(self, "status", QuestionStatus(self.status))
        accepted_cutoff = _aware(self.accepted_cutoff, "accepted_cutoff")
        created = _aware(self.created_at, "created_at")
        not_before = _aware(self.not_before, "not_before")
        due = _aware(self.due_at, "due_at") if self.due_at is not None else None
        expires = (
            _aware(self.expires_at, "expires_at")
            if self.expires_at is not None
            else None
        )
        if due is not None and expires is not None and expires < due:
            raise ValueError("expires_at cannot precede due_at")
        normalized_blockers = tuple(
            sorted(
                {
                    re.sub(
                        r"[^a-z0-9_]+", "_", _bounded_text(item, "blocker", 100).lower()
                    ).strip("_")
                    for item in self.blockers
                }
            )
        )
        if len(normalized_blockers) > 32 or any(
            not item for item in normalized_blockers
        ):
            raise ValueError("invalid planner blockers")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "accepted_cutoff", accepted_cutoff)
        object.__setattr__(self, "not_before", not_before)
        object.__setattr__(self, "due_at", due)
        object.__setattr__(self, "expires_at", expires)
        object.__setattr__(self, "blockers", normalized_blockers)


__all__ = [
    "ORIGIN_KINDS",
    "QUESTION_TRANSITIONS",
    "QUESTION_TYPES",
    "TARGET_KINDS",
    "PriorityInputs",
    "QuestionCandidate",
    "QuestionForPlanning",
    "QuestionStatus",
    "canonical_json",
    "content_fingerprint",
    "question_fingerprint",
    "validate_question_transition",
]
