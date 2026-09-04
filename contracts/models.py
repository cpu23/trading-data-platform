from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints

NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="allow", use_enum_values=True)


class LivenessStatus(StrEnum):
    OK = "ok"


class ReadinessStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    UNREADY = "unready"


class DataHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class ComponentStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DEGRADED = "degraded"


class ComponentKind(StrEnum):
    SERVICE = "service"
    DATA = "data"


class SystemComponentKind(StrEnum):
    SERVICE = "service"
    DATA = "data"
    STREAM = "stream"
    COLLECTOR = "collector"
    PROCESSOR = "processor"


class SchedulerStatus(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"


class StreamStatus(StrEnum):
    STOPPED = "stopped"
    SIMULATED = "simulated"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    DISABLED = "disabled"


class QualityOverall(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class QualityStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    FRESH = "fresh"
    STALE = "stale"
    FUTURE = "future"
    FUTURE_INVALID = "future-invalid"
    FUTURE_INVALID_UNDERSCORE = "future_invalid"


class CycleMode(StrEnum):
    REFRESH = "refresh"
    ANALYZE = "analyze"
    FORCE_FULL = "force_full"


class RunKind(StrEnum):
    CYCLE = "cycle"
    COLLECTOR = "collector"
    PROCESSOR = "processor"
    NEWS = "news"
    FILINGS = "filings"


class RunLifecycleStatus(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


class RunResultStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


class HealthComponent(ContractModel):
    name: NonBlankText
    kind: ComponentKind | None = None
    critical: StrictBool | None = None
    status: ComponentStatus
    reason: str | None = None
    config_version: str | None = None
    restart_required: StrictBool | None = None


class SchedulerJob(ContractModel):
    id: NonBlankText
    next_due_at: str | datetime | None = None


class SchedulerSnapshot(ContractModel):
    status: SchedulerStatus = SchedulerStatus.STOPPED
    jobs: list[SchedulerJob] = Field(default_factory=list)
    checked_at: str | datetime | None = None


class StreamSnapshot(ContractModel):
    status: StreamStatus
    last_heartbeat: str | datetime | None = None
    error: str | None = None


class QualityCheck(ContractModel):
    healthy: StrictBool
    name: str | None = None
    status: QualityStatus | None = None
    freshness: QualityStatus | None = None
    detail: str | None = None
    source_id: str | None = None


class QualitySnapshot(ContractModel):
    overall: QualityOverall
    checks: dict[str, QualityCheck] | list[QualityCheck]


class OrchestratorHealthResponse(ContractModel):
    liveness: LivenessStatus
    readiness: ReadinessStatus
    data_health: DataHealthStatus
    status: QualityOverall | None = None
    components: list[HealthComponent] = Field(default_factory=list)
    scheduler: SchedulerSnapshot | None = None
    stream: StreamSnapshot | None = None
    collectors: dict[str, Any] = Field(default_factory=dict)
    quality: QualitySnapshot | None = None
    config_version: str | None = None


class SystemHealthComponent(ContractModel):
    name: NonBlankText
    kind: SystemComponentKind
    last_run_at: str | datetime | None = None
    last_status: NonBlankText
    next_due_at: str | datetime | None = None
    stale: StrictBool
    quality_warn: StrictBool
    error_message: str | None = None


class SystemHealthResponse(ContractModel):
    liveness: LivenessStatus
    readiness: ReadinessStatus
    data_health: DataHealthStatus
    overall: QualityOverall
    components: list[SystemHealthComponent] = Field(default_factory=list)
    today_llm_cost_usd: float
    today_token_count: int
    quality: QualitySnapshot
    config_version: str | None = None


class InvestmentUrlIngestRequest(ContractModel):
    """Strict, bounded shape for the investment URL-ingest boundary.

    Shared by the API and orchestrator routes so unknown metadata fields,
    unbounded strings, and coerced booleans are rejected identically at both
    boundaries (extra fields forbidden). Semantic validation (region set,
    document_type set, report_date format) stays in the orchestrator's
    ``normalize_metadata``, which remains the single source of truth.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    url: NonBlankText = Field(max_length=2048)
    company: NonBlankText = Field(max_length=160)
    symbol: str | None = Field(default=None, max_length=24)
    region: str | None = Field(default=None, max_length=16)
    industry: str | None = Field(default=None, max_length=160)
    document_type: str | None = Field(default=None, max_length=40)
    report_date: str | None = Field(default=None, max_length=10)
    filename: str | None = Field(default=None, max_length=240)
    source_url: str | None = Field(default=None, max_length=2048)
    filing_source: str | None = Field(default=None, max_length=40)
    filing_id: str | None = Field(default=None, max_length=160)
    analyze: StrictBool = False


class RunAcceptanceRequest(ContractModel):
    correlation_id: str | None = None
    mode: CycleMode = CycleMode.REFRESH
    budget_confirmed: StrictBool = False


class RunAcceptedResponse(ContractModel):
    job_id: NonBlankText
    accepted_at: str | datetime
    prior_job_id: str | None = None


class CycleStatusResponse(ContractModel):
    running: bool
    correlation_id: str | None = None


class RunStage(ContractModel):
    log_id: str | None = None
    correlation_id: str | None = None
    kind: str
    component: str
    started_at: str | datetime | None = None
    completed_at: str | datetime | None = None
    status: str
    duration_ms: int | float | None = None
    records_fetched: int | None = None
    records_written: int | None = None
    tokens_input: int | None = None
    tokens_output: int | None = None
    cost_usd: float | None = None
    error_message: str | None = None


class RunDetailResponse(ContractModel):
    correlation_id: str
    status: str
    result_status: str | None = None
    run_kind: RunKind = RunKind.CYCLE
    requested_component: str | None = None
    triggered_by: str | None = None
    started_at: str | datetime | None = None
    completed_at: str | datetime | None = None
    error_message: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    stages: list[RunStage] = Field(default_factory=list)


class RunListResponse(ContractModel):
    runs: list[RunDetailResponse] = Field(default_factory=list)
    limit: int


class RunStatusResponse(ContractModel):
    status: str
    correlation_id: str
    lifecycle_status: str | None = None
    result_status: str | None = None
    started_at: str | datetime | None = None
    completed_at: str | datetime | None = None
    error_message: str | None = None
    progress: dict[str, Any] = Field(default_factory=dict)
    stages: list[RunStage] = Field(default_factory=list)


class QualityResponse(QualitySnapshot):
    pass
