"""Bounded API contracts for system topology visualization and status."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

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


class TopologyContract(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    @field_validator("*", mode="after")
    @classmethod
    def _require_timezone_aware_datetimes(cls, value: Any) -> Any:
        if isinstance(value, datetime) and (
            value.tzinfo is None or value.utcoffset() is None
        ):
            raise ValueError("datetimes must be timezone-aware")
        return value


class TopologyNodeStatus(StrEnum):
    ACTIVE = "active"
    HEALTHY = "healthy"
    IDLE = "idle"
    DEGRADED = "degraded"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


class SystemTopologyNode(TopologyContract):
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
    navigation_target: (
        Annotated[
            str, StringConstraints(pattern=r"^/[A-Za-z0-9_/?=&.%-]*$", max_length=500)
        ]
        | None
    ) = None
    inferred_activity: bool = False


class SystemTopologyEdge(TopologyContract):
    source: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
    target: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
    kind: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    status: TopologyNodeStatus
    recent_activity_count: int | None = Field(default=None, ge=0, le=1_000_000)
    last_activity_at: datetime | None = None
    safe_detail: Annotated[str, StringConstraints(max_length=1000)]


class SystemTopologyResponse(TopologyContract):
    schema_version: Literal[1] = 1
    generated_at: datetime
    status: Literal["available", "partial", "unavailable"]
    nodes: list[SystemTopologyNode] = Field(max_length=64)
    edges: list[SystemTopologyEdge] = Field(max_length=128)
    unavailable_components: list[NonBlank] = Field(default_factory=list, max_length=32)
    summary: ShortText

    @model_validator(mode="after")
    def _validate_graph_state(self) -> SystemTopologyResponse:
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("topology node IDs must be unique")
        known_ids = set(node_ids)
        if any(
            edge.source not in known_ids or edge.target not in known_ids
            for edge in self.edges
        ):
            raise ValueError("topology edges must reference existing nodes")
        if self.status == "available" and self.unavailable_components:
            raise ValueError("available topology cannot name unavailable components")
        if self.status == "partial" and not self.unavailable_components:
            raise ValueError("partial topology must name unavailable components")
        if self.status == "unavailable":
            if self.nodes or self.edges:
                raise ValueError("unavailable topology cannot contain graph data")
            if not self.unavailable_components:
                raise ValueError(
                    "unavailable topology must name an unavailable component"
                )
        return self


__all__ = [
    "SystemTopologyEdge",
    "SystemTopologyNode",
    "SystemTopologyResponse",
    "TopologyNodeStatus",
]
