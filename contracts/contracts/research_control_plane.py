"""Bounded API contracts for the autonomous research control plane."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

NonBlank = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
]


class ControlPlaneContract(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    @field_validator("*", mode="after")
    @classmethod
    def _require_timezone_aware_datetimes(cls, value: Any) -> Any:
        if isinstance(value, datetime) and (
            value.tzinfo is None or value.utcoffset() is None
        ):
            raise ValueError("datetimes must be timezone-aware")
        return value


class ResearchQuestionStatus(StrEnum):
    PENDING = "pending"
    PLANNED = "planned"
    QUEUED = "queued"
    RUNNING = "running"
    RESOLVED = "resolved"
    UNRESOLVABLE = "unresolvable"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ResearchWorkOrderStatus(StrEnum):
    PLANNED = "planned"
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    CANCELLED = "cancelled"
    STALE = "stale"


class ResearchPrioritySnapshot(ControlPlaneContract):
    policy_version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    materiality: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    uncertainty: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    discrimination_power: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False
    )
    urgency: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    freshness_gap: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    resolvability: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    expected_cost_usd: float | None = Field(
        default=None, ge=0, le=100, allow_inf_nan=False
    )
    expected_runtime_seconds: int | None = Field(default=None, ge=0, le=86400)
    expected_human_review_minutes: float | None = Field(
        default=None, ge=0, le=1440, allow_inf_nan=False
    )
    score: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    blockers: list[NonBlank] = Field(default_factory=list, max_length=32)


class ResearchQuestionResponse(ControlPlaneContract):
    id: UUID
    fingerprint: Annotated[
        str, StringConstraints(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    ]
    origin_kind: NonBlank
    question_type: NonBlank
    atomic_question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ]
    target_kind: NonBlank
    target_ref: NonBlank
    accepted_cutoff: datetime
    required_evidence_shape: dict[str, Any] = Field(max_length=32)
    acceptable_source_families: list[NonBlank] = Field(max_length=32)
    priority: ResearchPrioritySnapshot
    status: ResearchQuestionStatus
    attempt_count: int = Field(ge=0, le=100)
    not_before: datetime
    due_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    resolution_evidence_refs: list[NonBlank] = Field(
        default_factory=list, max_length=256
    )
    resolution_summary: Annotated[str, StringConstraints(max_length=4000)] | None = None
    unresolved_reason: ShortText | None = None


class ResearchQuestionListResponse(ControlPlaneContract):
    items: list[ResearchQuestionResponse] = Field(max_length=100)
    limit: int = Field(ge=1, le=100)
    next_cursor: Annotated[str, StringConstraints(max_length=500)] | None = None
    status: Literal["available", "unavailable"] = "available"


class ResearchWorkOrderResponse(ControlPlaneContract):
    id: UUID
    question_id: UUID
    plan_id: UUID
    analysis_job_id: UUID
    skill_key: NonBlank
    skill_version: int = Field(ge=1)
    skill_fingerprint: Annotated[
        str, StringConstraints(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    ]
    accepted_cutoff: datetime
    planning_policy_version: Annotated[
        str, StringConstraints(min_length=1, max_length=64)
    ]
    estimated_value: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    reserved_cost_usd: float = Field(ge=0, le=100, allow_inf_nan=False)
    reserved_runtime_seconds: int = Field(ge=1, le=86400)
    status: ResearchWorkOrderStatus
    attempt_count: int = Field(ge=0, le=20)
    material_effect_summary: Annotated[
        str, StringConstraints(max_length=4000)
    ] | None = None
    error_kind: Annotated[str, StringConstraints(max_length=200)] | None = None
    created_at: datetime
    queued_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ResearchWorkOrderListResponse(ControlPlaneContract):
    items: list[ResearchWorkOrderResponse] = Field(max_length=100)
    limit: int = Field(ge=1, le=100)
    next_cursor: Annotated[str, StringConstraints(max_length=500)] | None = None
    status: Literal["available", "unavailable"] = "available"


class ResearchBacklogCounts(ControlPlaneContract):
    pending: int = Field(default=0, ge=0)
    planned: int = Field(default=0, ge=0)
    queued: int = Field(default=0, ge=0)
    running: int = Field(default=0, ge=0)
    resolved: int = Field(default=0, ge=0)
    unresolvable: int = Field(default=0, ge=0)
    expired: int = Field(default=0, ge=0)
    cancelled: int = Field(default=0, ge=0)


class ResearchProductivityMetrics(ControlPlaneContract):
    material_change_yield: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False
    )
    justified_noop_rate: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False
    )
    cost_per_material_update_usd: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    median_event_to_verified_latency_ms: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    duplicate_work_rate: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False
    )
    evidence_reuse_ratio: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False
    )
    stale_thesis_debt: int = Field(default=0, ge=0)
    forecast_resolution_coverage: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False
    )


class ResearchControlPlaneStatusResponse(ControlPlaneContract):
    status: Literal["available", "degraded", "unavailable"]
    enabled: bool
    generated_at: datetime
    priority_policy_version: Annotated[
        str, StringConstraints(min_length=1, max_length=64)
    ]
    materiality_policy_version: Annotated[
        str, StringConstraints(min_length=1, max_length=64)
    ]
    backlog: ResearchBacklogCounts
    active_work_orders: int = Field(ge=0)
    latest_plan_at: datetime | None = None
    latest_effect_at: datetime | None = None
    metrics: ResearchProductivityMetrics
    unavailable_components: list[NonBlank] = Field(default_factory=list, max_length=16)


class ResearchControlPlaneRunRequest(ControlPlaneContract):
    reason: ShortText
    budget_override: bool = False
    override_reason: ShortText | None = None

    @model_validator(mode="after")
    def _override_reason_matches_flag(self) -> ResearchControlPlaneRunRequest:
        if self.budget_override and self.override_reason is None:
            raise ValueError("override_reason is required when budget_override is true")
        if not self.budget_override and self.override_reason is not None:
            raise ValueError("override_reason requires budget_override")
        return self


class ResearchControlPlaneRunResponse(ControlPlaneContract):
    correlation_id: UUID
    analysis_job_id: UUID
    coalesced: bool
    accepted_at: datetime
    status: Literal["accepted", "coalesced"]


class TopologyNodeStatus(StrEnum):
    ACTIVE = "active"
    HEALTHY = "healthy"
    IDLE = "idle"
    DEGRADED = "degraded"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


class SystemTopologyNode(ControlPlaneContract):
    id: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
    label: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    group: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    kind: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    status: TopologyNodeStatus
    activity_state: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    bounded_count: int | None = Field(default=None, ge=0, le=1_000_000)
    last_activity_at: datetime | None = None
    staleness_reason: Annotated[str, StringConstraints(max_length=500)] | None = None
    safe_detail: Annotated[str, StringConstraints(max_length=1000)]
    navigation_target: Annotated[
        str, StringConstraints(pattern=r"^/[A-Za-z0-9_/?=&.%-]*$", max_length=500)
    ] | None = None
    inferred_activity: bool = False


class SystemTopologyEdge(ControlPlaneContract):
    source: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
    target: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
    kind: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    status: TopologyNodeStatus
    recent_activity_count: int | None = Field(default=None, ge=0, le=1_000_000)
    last_activity_at: datetime | None = None
    safe_detail: Annotated[str, StringConstraints(max_length=1000)]


class SystemTopologyResponse(ControlPlaneContract):
    schema_version: Literal[1] = 1
    generated_at: datetime
    status: Literal["available", "partial", "unavailable"]
    nodes: list[SystemTopologyNode] = Field(max_length=64)
    edges: list[SystemTopologyEdge] = Field(max_length=128)
    unavailable_components: list[NonBlank] = Field(default_factory=list, max_length=32)
    summary: ShortText
