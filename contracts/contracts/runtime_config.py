"""Frozen, runtime-validated configuration models shared by the API and orchestrator.

These models replace the historical TypedDict casts in ``api/config.py`` and
``orchestrator/config_loader.py``.  Every value loaded from ``config.yaml``,
the operator profile, and the secrets file is validated against a strict
schema: unknown fields are rejected, types/ranges/units are enforced, and
cross-field constraints are checked, so misconfiguration fails at startup
with a clear error instead of surfacing mid-run.

All models are frozen and expose a read-only ``Mapping`` interface so the
existing ``config.get("section", {}).get("key", default)`` call sites keep
working unchanged.

The market-state schema is owned by the market-state workstream; keep it in
sync with ``orchestrator/market_state.py``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import time as time_of_day
from functools import wraps
from typing import Annotated, Any, BinaryIO, Literal, TextIO, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

_TIME_PATTERN = re.compile(r"^\d{2}:\d{2}:\d{2}$")
_MARKET_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9._:/-]{1,64}$")
_CIK_PATTERN = re.compile(r"^\d{10}$")
MAX_CIK_SYMBOLS = 500
#: Hard cap enforced by the public_equities collector at runtime
#: (``collectors.public_equities.HARD_MAX_SYMBOLS``).  Four hundred covers
#: the checked-in 300-company investment universe plus a bounded live-thesis
#: margin without admitting an unbounded scrape.
PUBLIC_EQUITIES_HARD_MAX_SYMBOLS = 400
PUBLIC_EQUITIES_HARD_MAX_CONCURRENCY = 16
#: Canonical symbol grammar enforced by the public_equities collector at
#: runtime (``orchestrator.collectors.public_equities``).  A validated
#: config must only carry symbols the collector accepts: after the
#: collector's ``strip().upper()`` canonicalization each symbol must match
#: ``^[A-Z0-9][A-Z0-9.\-^=]{0,19}$``.  Kept in this shared contract so
#: startup validation cannot drift from the collector grammar.
PUBLIC_EQUITIES_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-^=]{0,19}$")
_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"})


class ConfigError(ValueError):
    """Configuration could not be loaded or failed validation.

    Subclasses :class:`ValueError` so existing callers that treat bad
    configuration as a ``ValueError`` keep working.
    """


def demo_mode_enabled() -> bool:
    """Return whether the explicit credential-free demo mode is active."""
    return os.environ.get("DEMO_MODE", "").strip().lower() in {"1", "true", "yes"}


def apply_demo_transform(raw: dict[str, Any]) -> None:
    """Apply one shared offline demo policy before snapshot validation/hash."""
    if not demo_mode_enabled():
        return
    demo = raw.setdefault("demo", {})
    if isinstance(demo, dict):
        demo["enabled"] = True
    for section in ("collectors", "processors"):
        values = raw.get(section, {})
        if isinstance(values, dict):
            for item in values.values():
                if isinstance(item, dict):
                    item["enabled"] = False
    filings = raw.setdefault("investment_filings", {})
    if isinstance(filings, dict):
        filings["enabled"] = False
        filings["schedule"] = None
        filings["run_on_startup"] = False


def demo_missing_env_fallback(var_name: str) -> str | None:
    """Resolve absent provider placeholders only in explicit demo mode."""
    del var_name
    return "demo-disabled" if demo_mode_enabled() else None


class FrozenModel(BaseModel, Mapping[str, Any]):
    """A frozen strict model that also behaves as a read-only mapping."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        use_enum_values=True,
        validate_default=True,
    )

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError as exc:  # pragma: no cover - trivial
            raise KeyError(key) from exc

    # pydantic's BaseModel.__iter__ yields (field, value) tuples, but the
    # Mapping[str, Any] contract requires a key iterator.  Our key-iteration
    # semantics are intentional and no single annotation is a subtype of
    # both signatures, so the override conflict is scoped out explicitly.
    def __iter__(self) -> Iterator[str]:  # type: ignore[override]
        return iter(type(self).model_fields)

    def __len__(self) -> int:
        return len(type(self).model_fields)


def _validate_time_string(value: str) -> str:
    if not _TIME_PATTERN.match(value):
        raise ValueError("must be a time in HH:MM:SS form")
    try:
        time_of_day.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("must be a valid HH:MM:SS time") from exc
    return value


def _validate_market_symbol(value: str) -> str:
    if not _MARKET_SYMBOL_PATTERN.match(value):
        raise ValueError("must match ^[A-Za-z0-9._:/-]{1,64}$")
    return value


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------


class DatabaseConfig(FrozenModel):
    host: NonBlankText
    port: int = Field(default=5432, ge=1, le=65535)
    name: NonBlankText
    user: NonBlankText
    password: str = ""

    @field_validator("port", mode="before")
    @classmethod
    def _coerce_port(cls, value: Any) -> Any:
        """Accept numeric strings (e.g. ``"5432"``) as ports."""
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return value


# ---------------------------------------------------------------------------
# llm
# ---------------------------------------------------------------------------


class LlmRoleConfig(FrozenModel):
    reasoning_effort: NonBlankText


class LlmConfig(FrozenModel):
    # The only runtime provider: llm_client always talks to the OpenRouter
    # endpoint.  Anything else is inert configuration and is rejected early.
    provider: Literal["openrouter"] = "openrouter"
    api_key: str = ""
    api_keys: dict[str, str] = Field(default_factory=dict)
    # Exactly one model slot: "default".  Per-processor selectors are
    # unsupported (every processor inherits models.default; setup_state
    # promotes any legacy default).
    models: dict[Literal["default"], NonBlankText] = Field(default_factory=dict)
    include_temperature: bool = True
    # Per-consumer price ceilings: {consumer: {unit: cap}}.
    max_prices: dict[str, dict[str, float]] = Field(default_factory=dict)
    max_output_tokens: dict[str, int] = Field(default_factory=dict)
    temperatures: dict[str, float] = Field(default_factory=dict)
    structured_response: dict[str, bool] = Field(default_factory=dict)
    require_parameters: dict[str, bool] = Field(default_factory=dict)
    intelligence_roles: dict[str, LlmRoleConfig] = Field(default_factory=dict)
    stage_timeout_seconds: int = Field(default=90, ge=1, le=3600)
    validation_retries: int = Field(default=1, ge=0, le=20)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)


# ---------------------------------------------------------------------------
# timezone / logging / orchestration
# ---------------------------------------------------------------------------


class TimezoneZoneConfig(FrozenModel):
    name: NonBlankText
    label: NonBlankText | None = None


class TimezoneConfig(FrozenModel):
    primary: TimezoneZoneConfig = Field(
        default_factory=lambda: TimezoneZoneConfig(name="UTC")
    )
    secondary: TimezoneZoneConfig | None = None


class LoggingRotateConfig(FrozenModel):
    when: NonBlankText = "midnight"
    interval: int = Field(default=1, ge=1, le=36500)
    backup_count: int = Field(default=7, ge=0, le=100000)
    encoding: str | None = None
    utc: bool = False


class LoggingConfig(FrozenModel):
    level: NonBlankText = "INFO"
    format: NonBlankText = "structured_json"
    output: list[str] = Field(default_factory=lambda: ["stdout"])
    rotate: LoggingRotateConfig | None = None

    @field_validator("level")
    @classmethod
    def _validate_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in _LOG_LEVELS:
            raise ValueError(f"must be one of {', '.join(sorted(_LOG_LEVELS))}")
        return normalized


class OrchestrationConfig(FrozenModel):
    collector_workers: int = Field(default=3, ge=1, le=64)


# ---------------------------------------------------------------------------
# event pipeline
# ---------------------------------------------------------------------------


class SseConfig(FrozenModel):
    enabled: bool = True
    heartbeat_seconds: int = Field(default=15, ge=1, le=3600)
    poll_seconds: float = Field(default=0.5, ge=0.05, le=60.0)
    replay_limit: int = Field(default=100, ge=1, le=10000)
    max_streams_per_client: int = Field(default=3, ge=1, le=1000)
    retention_hours: int = Field(default=48, ge=1, le=24 * 365)


class EventPipelineWorkerConfig(FrozenModel):
    enabled: bool = True
    id: NonBlankText = "analysis-jobs"
    batch_size: int = Field(default=25, ge=1, le=10000)
    poll_seconds: float = Field(default=1.0, ge=0.1, le=3600.0)
    lease_seconds: int = Field(default=120, ge=1, le=86400)


class EventPipelineRetryConfig(FrozenModel):
    max_attempts: int = Field(default=5, ge=1, le=1000)
    base_seconds: float = Field(default=1.0, ge=0.0, le=86400.0)
    max_seconds: float = Field(default=300.0, ge=0.0, le=86400.0)
    jitter_seconds: float = Field(default=0.25, ge=0.0, le=86400.0)


class EventPipelineQueryConfig(FrozenModel):
    max_source_rows: int = Field(default=100, ge=1, le=100000)
    max_watchlist_rows: int = Field(default=100, ge=1, le=100000)
    max_release_cards: int = Field(default=20, ge=1, le=100000)
    max_story_clusters: int = Field(default=50, ge=1, le=100000)
    max_reconcile_jobs: int = Field(default=100, ge=1, le=100000)
    max_reconcile_freshness: int = Field(default=100, ge=1, le=100000)
    max_reconcile_snapshots: int = Field(default=100, ge=1, le=100000)


class EventPipelineJobsConfig(FrozenModel):
    enabled: bool = True
    worker: EventPipelineWorkerConfig = Field(default_factory=EventPipelineWorkerConfig)
    retry: EventPipelineRetryConfig = Field(default_factory=EventPipelineRetryConfig)
    query: EventPipelineQueryConfig = Field(default_factory=EventPipelineQueryConfig)


class EventPipelineConfig(FrozenModel):
    enabled: bool = True
    sse: SseConfig = Field(default_factory=SseConfig)
    sources: list[NonBlankText] = Field(default_factory=lambda: ["fred", "oanda"])
    outbox_worker_enabled: bool = True
    poll_interval_seconds: float = Field(default=1.0, ge=0.1, le=3600.0)
    lease_seconds: int = Field(default=120, ge=1, le=86400)
    max_attempts: int = Field(default=8, ge=1, le=1000)
    base_backoff_seconds: float = Field(default=1.0, ge=0.0, le=86400.0)
    max_backoff_seconds: float = Field(default=300.0, ge=0.0, le=86400.0)
    freshness_grace_seconds: int = Field(default=300, ge=0, le=86400)
    jobs: EventPipelineJobsConfig = Field(default_factory=EventPipelineJobsConfig)


# ---------------------------------------------------------------------------
# market state (schema owned by the market-state workstream)
# ---------------------------------------------------------------------------


class MarketStateLookback(FrozenModel):
    value: int = Field(default=7, ge=1, le=3650)
    unit: Literal["minutes", "hours", "days"] = "days"


class MarketStateThresholds(FrozenModel):
    trend_slope_epsilon: float = Field(default=0.001, ge=0.0)
    high_volatility_threshold: float = Field(default=0.02, ge=0.0)
    high_correlation_threshold: float = Field(default=0.75, ge=0.0, le=1.0)


class MarketStateConfig(FrozenModel):
    enabled: bool = True
    rows_per_symbol: int = Field(default=500, ge=1, le=10000)
    snapshot_limit: int = Field(default=100, ge=1, le=500)
    trend_bars: int = Field(default=20, ge=2, le=500)
    zscore_bars: int = Field(default=60, ge=2, le=10000)
    volatility_bars: int = Field(default=30, ge=2, le=10000)
    lookback: MarketStateLookback = Field(default_factory=MarketStateLookback)
    state_thresholds: MarketStateThresholds = Field(
        default_factory=MarketStateThresholds
    )
    baskets: dict[NonBlankText, list[str]] = Field(default_factory=dict)
    yield_curves: dict[NonBlankText, list[str]] = Field(default_factory=dict)

    @field_validator("baskets")
    @classmethod
    def _validate_baskets(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        for name, members in value.items():
            _validate_market_symbol(name)
            if not members:
                raise ValueError("market_state.baskets members must be non-empty")
            for member in members:
                _validate_market_symbol(member)
        return value

    @field_validator("yield_curves")
    @classmethod
    def _validate_yield_curves(
        cls, value: dict[str, list[str]]
    ) -> dict[str, list[str]]:
        for label, keys in value.items():
            _validate_market_symbol(label)
            if len(keys) != 2 or len(set(keys)) != 2:
                raise ValueError(
                    "market_state.yield_curves values must contain exactly 2 "
                    "distinct symbol keys"
                )
            for key in keys:
                _validate_market_symbol(key)
        return value


# ---------------------------------------------------------------------------
# reaction windows / story clustering / analysis routing
# ---------------------------------------------------------------------------


class VenueCalendarConfig(FrozenModel):
    timezone: NonBlankText
    open_time: str = Field(default="08:00:00")
    close_time: str = Field(default="17:00:00")
    weekdays: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])
    holidays: list[str] = Field(default_factory=list)
    early_closes: dict[str, str] = Field(default_factory=dict)

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        _validate_zoneinfo(value)
        return value

    @field_validator("open_time", "close_time")
    @classmethod
    def _validate_times(cls, value: str) -> str:
        return _validate_time_string(value)

    @model_validator(mode="after")
    def _validate_session_order(self) -> VenueCalendarConfig:
        # Equal times are allowed for weekly venues (e.g. FX: Sunday 17:00
        # open, Friday 17:00 close); a strictly later open is always invalid.
        if self.open_time > self.close_time:
            raise ValueError("venue open_time must not be later than close_time")
        return self

    @field_validator("weekdays")
    @classmethod
    def _validate_weekdays(cls, value: list[int]) -> list[int]:
        if not value:
            raise ValueError("venue weekdays must not be empty")
        for day in value:
            if not isinstance(day, int) or isinstance(day, bool) or not 1 <= day <= 7:
                raise ValueError("venue weekdays must be integers 1 (Mon) .. 7 (Sun)")
        if len(set(value)) != len(value):
            raise ValueError("venue weekdays must not contain duplicates")
        return value

    @field_validator("holidays")
    @classmethod
    def _validate_holidays(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        for holiday in value:
            _validate_iso_date(holiday)
            if holiday in seen:
                raise ValueError("venue holidays must not contain duplicates")
            seen.add(holiday)
        return value

    @field_validator("early_closes")
    @classmethod
    def _validate_early_closes(cls, value: dict[str, str]) -> dict[str, str]:
        for day, close in value.items():
            _validate_iso_date(day)
            _validate_time_string(close)
        return value


class InstrumentCalendarConfig(FrozenModel):
    venue: NonBlankText
    exchange_calendar: NonBlankText | None = None
    timezone: NonBlankText | None = None
    session_open: str | None = None
    session_close: str | None = None
    price_timeframe: NonBlankText | None = None
    target_selection_policy: Literal["first"] = "first"

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_zoneinfo(value)
        return value

    @field_validator("session_open", "session_close")
    @classmethod
    def _validate_times(cls, value: str | None) -> str | None:
        return _validate_time_string(value) if value is not None else None

    @model_validator(mode="after")
    def _validate_session_order(self) -> InstrumentCalendarConfig:
        if (
            self.session_open is not None
            and self.session_close is not None
            and self.session_open >= self.session_close
        ):
            raise ValueError(
                "instrument session_open must be earlier than session_close"
            )
        return self


#: Built-in calendar rule names that may be referenced without a
#: ``calendars.venues`` entry.
CALENDAR_BUILTINS = frozenset({"fx_24x5", "nyse", "lse", "xetra"})


class CalendarsConfig(FrozenModel):
    """Typed trading-calendar registry (reaction-windows workstream).

    ``instruments`` maps a symbol to its instrument metadata; a plain venue
    name string is accepted as shorthand for ``{"venue": <name>}``.  Venue
    references (``default_venue``, ``instrument.venue``,
    ``instrument.exchange_calendar``) must name a ``calendars.venues`` entry
    or one of the built-in rules (:data:`CALENDAR_BUILTINS`); a typo is
    rejected instead of silently mapping to the default.
    """

    default_venue: NonBlankText | None = None
    instruments: dict[NonBlankText, InstrumentCalendarConfig] = Field(
        default_factory=dict
    )
    venues: dict[NonBlankText, VenueCalendarConfig] = Field(default_factory=dict)

    @field_validator("instruments")
    @classmethod
    def _validate_symbols(
        cls, value: dict[str, InstrumentCalendarConfig]
    ) -> dict[str, InstrumentCalendarConfig]:
        for symbol in value:
            _validate_market_symbol(symbol)
        return value

    @field_validator("instruments", mode="before")
    @classmethod
    def _coerce_instrument_shorthand(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized: dict[str, object] = {}
        for symbol, entry in value.items():
            if isinstance(entry, str):
                if not entry.strip():
                    raise ValueError(f"instrument {symbol!r} venue must be non-blank")
                normalized[str(symbol)] = {"venue": entry.strip()}
            else:
                normalized[str(symbol)] = entry
        return normalized

    @model_validator(mode="after")
    def _validate_calendar_references(self) -> CalendarsConfig:
        known = set(self.venues) | CALENDAR_BUILTINS
        if self.default_venue is not None and self.default_venue not in known:
            raise ValueError(
                f"default_venue {self.default_venue!r} is not a defined "
                "venue or built-in calendar"
            )
        for symbol, instrument in self.instruments.items():
            if instrument.venue not in known:
                raise ValueError(
                    f"instrument {symbol!r} venue {instrument.venue!r} "
                    "is not a defined venue or built-in calendar"
                )
            if (
                instrument.exchange_calendar is not None
                and instrument.exchange_calendar not in known
            ):
                raise ValueError(
                    f"instrument {symbol!r} exchange_calendar "
                    f"{instrument.exchange_calendar!r} is not a defined "
                    "venue or built-in calendar"
                )
        return self


def _validate_iso_date(value: str) -> str:
    from datetime import date as date_of_year

    try:
        date_of_year.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("must be a YYYY-MM-DD date") from exc
    return value


def _validate_zoneinfo(value: str) -> str:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"must be a valid IANA timezone (got {value!r})") from exc
    return value


class ReactionWindowsConfig(FrozenModel):
    baseline_lookback_minutes: int = Field(default=120, ge=1, le=60 * 24 * 7)
    target_tolerance_minutes: int = Field(default=5, ge=0, le=60 * 24)
    max_event_age_minutes: int = Field(default=360, ge=1, le=60 * 24 * 7)
    backfill_limit: int = Field(default=100, ge=1, le=100000)
    volatility_lookback_minutes: int = Field(default=1440, ge=1, le=1440)
    # Trading-calendar registry; ``calendars.venues.<venue>.close_time`` is
    # authoritative for session close (the historical ``session_close`` knob
    # was removed as an inert duplicate).
    calendars: CalendarsConfig = Field(default_factory=CalendarsConfig)


class StoryClusteringConfig(FrozenModel):
    enabled: bool = True
    publish_limit: int = Field(default=500, ge=1, le=100000)
    candidate_window_hours: int = Field(default=72, ge=1, le=24 * 365)
    candidate_limit: int = Field(default=100, ge=1, le=100000)
    similarity_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    material_change_threshold: float = Field(default=0.25, ge=0.0, le=1.0)
    low_confidence_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    market_moving_min_importance: float = Field(default=0.75, ge=0.0, le=1.0)
    watchlist: list[str] = Field(default_factory=list)
    source_confidence: dict[str, float] = Field(default_factory=dict)
    lane_keywords: dict[str, list[str]] = Field(default_factory=dict)


class StoryConfirmationConfig(FrozenModel):
    target_tolerance_minutes: int = Field(default=5, ge=0, le=60 * 24)
    pre_headline_minutes: int = Field(default=5, ge=0, le=60 * 24)
    material_move_percent: float = Field(default=0.25, ge=0.0, le=100.0)
    session_close: str = Field(default="21:00:00")
    query_limit: int = Field(default=5000, ge=1, le=1000000)
    backfill_limit: int = Field(default=100, ge=1, le=100000)

    @field_validator("session_close")
    @classmethod
    def _validate_session_close(cls, value: str) -> str:
        return _validate_time_string(value)


class AnalysisAtomsConfig(FrozenModel):
    enabled: bool = True
    event_interpretation_hours: int = Field(default=48, ge=1, le=24 * 365)
    regime_hours: int = Field(default=168, ge=1, le=24 * 365)
    intraday_session_close: str = Field(default="21:00:00")
    expire_limit: int = Field(default=100, ge=1, le=100000)

    @field_validator("intraday_session_close")
    @classmethod
    def _validate_session_close(cls, value: str) -> str:
        return _validate_time_string(value)


class AnalysisRoutingConfig(FrozenModel):
    event_atom_min_score: float = Field(default=0.55, ge=0.0, le=1.0)
    story_summary_min_score: float = Field(default=0.60, ge=0.0, le=1.0)
    briefing_invalidation_min_score: float = Field(default=0.50, ge=0.0, le=1.0)
    investment_thesis_review_min_score: float = Field(default=0.65, ge=0.0, le=1.0)
    reaction_window_min_score: float = Field(default=0.55, ge=0.0, le=1.0)
    source_confidence: dict[str, float] = Field(default_factory=dict)
    debounce_seconds: int = Field(default=30, ge=0, le=3600)
    max_debounce_seconds: int = Field(default=120, ge=0, le=3600)


# ---------------------------------------------------------------------------
# research intelligence
# ---------------------------------------------------------------------------


class ResearchStageConfig(FrozenModel):
    enabled: bool = True
    prompt_template: str | None = None
    max_output_tokens: int | None = Field(default=None, ge=1, le=100000)


class ResearchLimitsConfig(FrozenModel):
    maximum_candidate_evidence: int = Field(default=240, ge=1, le=100000)
    maximum_macro_evidence: int = Field(default=48, ge=1, le=100000)
    maximum_market_drivers: int = Field(default=8, ge=1, le=100000)
    maximum_cases_per_run: int = Field(default=8, ge=1, le=100000)
    maximum_claim_documents_per_run: int = Field(default=8, ge=1, le=100000)
    evidence_per_candidate: int = Field(default=24, ge=1, le=100000)
    publication_limit: int = Field(default=20, ge=1, le=100000)
    history_limit: int = Field(default=50, ge=1, le=100000)


class ResearchDiscoveryConfig(FrozenModel):
    minimum_evidence_count: int = Field(default=3, ge=1, le=100000)
    minimum_source_diversity: int = Field(default=2, ge=1, le=100000)
    candidate_similarity_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    merge_similarity_threshold: float = Field(default=0.72, ge=0.0, le=1.0)


class ResearchGraphConfig(FrozenModel):
    depth: int = Field(default=3, ge=1, le=100)
    hard_depth: int = Field(default=5, ge=1, le=100)
    maximum_nodes: int = Field(default=40, ge=1, le=100000)
    maximum_edges: int = Field(default=60, ge=1, le=100000)


class ResearchLifecycleThresholdsConfig(FrozenModel):
    forming_evidence: int = Field(default=3, ge=0, le=100000)
    corroborated_evidence: int = Field(default=5, ge=0, le=100000)
    corroborated_days: int = Field(default=7, ge=0, le=36500)
    research_ready_evidence: int = Field(default=6, ge=0, le=100000)
    mature_evidence: int = Field(default=10, ge=0, le=100000)
    mature_snapshots: int = Field(default=3, ge=0, le=100000)
    weakening_days: int = Field(default=45, ge=0, le=36500)
    archive_days: int = Field(default=120, ge=0, le=36500)


class ResearchIntelligenceConfig(FrozenModel):
    enabled: bool = True
    schedule_enabled: bool = True
    schedule: str | None = None
    rolling_window_days: int = Field(default=45, ge=1, le=36500)
    model_budget_usd_per_run: float = Field(default=0.75, ge=0.0)
    claim_extraction_enabled: bool = True
    macro_drivers_enabled: bool = True
    promote_discovered_themes: bool = True
    hot_market_universe: list[str] = Field(default_factory=list)
    region_universe: list[str] = Field(default_factory=list)
    limits: ResearchLimitsConfig = Field(default_factory=ResearchLimitsConfig)
    discovery: ResearchDiscoveryConfig = Field(default_factory=ResearchDiscoveryConfig)
    graph: ResearchGraphConfig = Field(default_factory=ResearchGraphConfig)
    lifecycle_thresholds: ResearchLifecycleThresholdsConfig = Field(
        default_factory=ResearchLifecycleThresholdsConfig
    )
    stages: dict[str, ResearchStageConfig] = Field(default_factory=dict)
    model_overrides: dict[str, str] = Field(default_factory=dict)
    reasoning_effort: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# thesis autonomy
# ---------------------------------------------------------------------------


class ThesisAutonomyConfig(FrozenModel):
    """Scheduled autonomous thesis-fusion desk settings.

    Bounds mirror the pure engine caps exactly (``thesis_tournament`` and
    ``thesis_fusion``), so a value the engine would clamp or reject never
    reaches it via config.  ``model_budget_usd_per_run`` is the per-run
    ceiling; the global daily budget (``budgets.daily_llm_usd``) remains the
    ultimate cap and is enforced by
    ``AppConfig._check_thesis_autonomy_budget``.  ``cost``/``liquidity``/
    ``downside`` are optional finite scoring inputs; None keeps the pure
    scoring defaults (unknown, never invented).
    """

    enabled: bool = True
    schedule_enabled: bool = True
    schedule: str | None = None
    lookback_days: int = Field(default=30, ge=1, le=3650)
    # Mirrors the EvidenceRegistry collection cap (2000); the tournament
    # prompt brief stays bounded at 200 items and the raw-candidate bound is
    # the engine's own cap.
    maximum_evidence: int = Field(default=96, ge=1, le=2000)
    maximum_promoted: int = Field(default=64, ge=1, le=64)
    maximum_challenges_per_run: int = Field(default=25, ge=1, le=100)
    event_debounce_minutes: int = Field(default=60, ge=1, le=1440)
    maximum_event_runs_per_day: int = Field(default=4, ge=0, le=24)
    falsification_budget_fraction: float = Field(default=0.45, ge=0.1, le=0.9)
    model_budget_usd_per_run: float = Field(default=0.75, ge=0.0)
    minimum_supporting_source_families: int = Field(default=1, ge=1, le=10)
    require_cited_excerpts: bool = False
    require_opposing_variants: bool = False
    reasoning_effort: str | None = None
    model_override: str | None = None
    max_output_tokens: int | None = Field(default=None, ge=1, le=100000)
    # Optional finite scoring inputs; None keeps the pure-scoring defaults.
    cost: float | None = Field(default=None, ge=0.0, le=100.0)
    liquidity: float | None = Field(default=None, ge=0.0, le=1.0)
    downside: float | None = Field(default=None, ge=0.0, le=1.0)


class ResearchControlPlaneConfig(FrozenModel):
    """Bounded autonomous research maintenance and planning policy."""

    enabled: bool = True
    planning_interval_minutes: int = Field(default=15, ge=1, le=1440)
    event_debounce_seconds: int = Field(default=120, ge=0, le=86400)
    maximum_questions_per_plan: int = Field(default=20, ge=1, le=1000)
    maximum_work_orders_per_plan: int = Field(default=8, ge=1, le=100)
    maximum_runtime_seconds_per_plan: int = Field(default=900, ge=1, le=86400)
    model_budget_usd_per_plan: float = Field(
        default=0.0, ge=0.0, le=100.0, allow_inf_nan=False
    )
    minimum_priority: float = Field(
        default=0.0, ge=0.0, le=1_000_000.0, allow_inf_nan=False
    )
    catalyst_lookahead_days: int = Field(default=30, ge=0, le=3650)
    stale_question_days: int = Field(default=14, ge=1, le=3650)
    priority_policy_version: Literal["v1"] = "v1"
    materiality_policy_version: Literal["v1"] = "v1"


# ---------------------------------------------------------------------------
# macro events
# ---------------------------------------------------------------------------


class MacroEventMappingConfig(FrozenModel):
    event_name: NonBlankText
    instruments: list[NonBlankText] = Field(default_factory=list)
    priority: int = Field(default=10, ge=1, le=1000)
    expected_sensitivity: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# collectors / processors
# ---------------------------------------------------------------------------


class InstrumentConfig(FrozenModel):
    symbol: NonBlankText
    oanda_instrument: NonBlankText
    enabled: bool = True


class SourceSeriesConfig(FrozenModel):
    """A collector series definition; only ``id`` is universal.

    FRED series use ``id`` + ``frequency``; official-macro series add the
    HTTP/source mapping fields.  Unknown fields remain rejected.
    """

    id: NonBlankText
    frequency: Literal["daily", "weekly", "monthly", "quarterly"] = "daily"
    title: str | None = None
    url: str | None = None
    # HTTP parameter bags: keys are provider-defined, values bounded scalars.
    params: dict[str, str | int | float | bool] = Field(default_factory=dict)
    format: str | None = None
    date_field: str | None = None
    date_format: str | None = None
    value_field: str | None = None
    semantic_feature: str | None = None
    region: str | None = None
    provider_series: str | None = None
    records_path: list[str] = Field(default_factory=list)


class CollectorFeedConfig(FrozenModel):
    institution: NonBlankText
    document_type: NonBlankText
    url: NonBlankText
    headers: dict[str, str] = Field(default_factory=dict)


class CftcContractConfig(FrozenModel):
    market_id: NonBlankText
    name: NonBlankText
    assets: list[NonBlankText] = Field(default_factory=list)


class CftcDatasetConfig(FrozenModel):
    """One official CFTC report schema and its compatible contract mappings."""

    name: NonBlankText
    url: NonBlankText
    semantics: NonBlankText
    limit: int = Field(default=5000, ge=1, le=1_000_000)
    categories: list[tuple[NonBlankText, NonBlankText, NonBlankText]]
    contracts: list[CftcContractConfig]


class IssuerNewsFeedConfig(FrozenModel):
    """One configured issuer_news primary feed (RSS/Atom or HTML/JSON-LD page).

    Bounds mirror the ``issuer_news`` collector's per-feed clamps exactly, so
    a value the collector would clamp or reject never reaches it via config.
    """

    name: NonBlankText | None = None
    url: NonBlankText
    enabled: bool = True
    kind: Literal["feed", "html", "html_jsonld", "jsonld"] = "feed"
    content_role: Literal["primary", "derivative"] = "primary"
    document_type: NonBlankText = "issuer_update"
    institution: NonBlankText | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    max_bytes: int = Field(default=5_000_000, ge=64_000, le=50_000_000)
    timeout_seconds: int = Field(default=30, ge=5, le=120)
    max_redirects: int = Field(default=5, ge=0, le=10)
    max_items: int = Field(default=100, ge=1, le=1_000)
    max_title_chars: int = Field(default=500, ge=20, le=2_000)
    max_content_chars: int = Field(default=4_000, ge=0, le=100_000)
    fetch_full_text: bool = False
    max_document_bytes: int = Field(default=2_000_000, ge=64_000, le=10_000_000)
    max_full_text_items: int = Field(default=20, ge=1, le=100)
    content_origins: list[NonBlankText] = Field(default_factory=list, max_length=10)
    # SEC EDGAR "current events" Atom entries are titled
    # "<form> - <company> (<10-digit CIK>) (<role>)". Opting into
    # ``sec_edgar`` makes the collector resolve the per-filing company into
    # the record institution and ``metadata.cik`` (plus an optional
    # ``metadata.ticker`` from ``cik_symbols``) instead of keeping the static
    # feed-level institution. Non-matching titles keep the static
    # institution; nothing is inferred from malformed titles.
    entity_parser: Literal["sec_edgar"] | None = None
    cik_symbols: dict[str, NonBlankText] = Field(
        default_factory=dict, max_length=MAX_CIK_SYMBOLS
    )

    @field_validator("cik_symbols")
    @classmethod
    def _validate_cik_symbols(cls, value: dict[str, str]) -> dict[str, str]:
        for cik in value:
            if not _CIK_PATTERN.fullmatch(cik):
                raise ValueError(f"cik_symbols keys must be 10-digit CIKs, got {cik!r}")
        return value

    @model_validator(mode="after")
    def _validate_entity_parser_fields(self) -> IssuerNewsFeedConfig:
        if self.cik_symbols and self.entity_parser != "sec_edgar":
            raise ValueError("cik_symbols requires entity_parser: sec_edgar")
        return self


class SecForm4IssuerConfig(FrozenModel):
    """One SEC EDGAR issuer watched for Form 4 insider filings."""

    cik: NonBlankText
    symbol: NonBlankText


class FinraSymbolConfig(FrozenModel):
    """One FINRA Reg SHO symbol, optionally mapped to platform assets."""

    symbol: NonBlankText
    assets: list[NonBlankText] = Field(default_factory=list)


class IssuerTranscriptConfig(FrozenModel):
    """One issuer transcript source page (official IR events/transcripts).

    Mirrors the ``issuer_transcripts`` collector's per-issuer consumption:
    ``url`` is the discovery page or event feed; ``kind`` selects bounded
    HTML/feed autodiscovery or the Q4 public event contract; and
    ``institution``/``ticker`` identify the issuer. Optional per-issuer
    ``user_agent``/``headers`` override the section defaults.
    """

    kind: Literal["html", "feed", "q4_events"] = "html"
    institution: NonBlankText | None = None
    ticker: NonBlankText | None = None
    url: NonBlankText
    document_type: NonBlankText = "earnings_transcript"
    user_agent: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)


class TranscriptionSettingsConfig(FrozenModel):
    """Local faster-whisper transcription settings for issuer_transcripts.

    Bounds mirror ``transcription.normalize_transcription_config`` so
    out-of-range values are rejected here instead of silently clamped at
    runtime.
    """

    model: NonBlankText = "small.en"
    device: NonBlankText = "cpu"
    compute_type: NonBlankText = "int8"
    beam_size: int = Field(default=5, ge=1, le=10)
    language: str | None = "en"
    max_audio_seconds: int = Field(default=7_200, ge=1, le=86_400)
    timeout_seconds: int = Field(default=3_600, ge=1, le=86_400)
    max_audio_bytes: int = Field(default=250_000_000, ge=1, le=2_147_483_648)
    model_dir: str | None = "/var/lib/trading-data/news/models/whisper"
    local_files_only: bool = False
    cpu_threads: int = Field(default=0, ge=0, le=1024)
    vad_filter: bool = True
    condition_on_previous_text: bool = False


class CollectorConfig(FrozenModel):
    """Union schema covering every configured collector source.

    Sources only set the fields they use; any other key is rejected so a
    typo in a collector section fails startup instead of being silently
    ignored by the collector's ``.get(key, default)`` lookups.
    """

    enabled: bool = True
    schedule: str | None = None
    api_key: str = ""
    # fred
    metadata_ttl_days: int = Field(default=30, ge=1, le=36500)
    max_concurrency: int = Field(default=4, ge=1, le=64)
    backfill_years_daily: int = Field(default=2, ge=0, le=100)
    backfill_years_monthly: int = Field(default=5, ge=0, le=100)
    backfill_years_quarterly: int = Field(default=10, ge=0, le=100)
    revision_window_days: dict[str, int] = Field(default_factory=dict)
    series: list[SourceSeriesConfig] = Field(default_factory=list)
    # forex_factory
    source_url: str | None = None
    weekly_export_base_url: str | None = None
    currencies: list[str] = Field(default_factory=list)
    min_impact: str | None = None
    user_agent: str | None = None
    request_delay_seconds: float = Field(default=2.0, ge=0.0, le=3600.0)
    # oanda
    stream_enabled: bool = True
    environment: str = "practice"
    snapshot_timeframe: str = "PRICE"
    account_id: str | None = None
    base_url: str | None = None
    instruments: list[InstrumentConfig] = Field(default_factory=list)
    # cftc / central_banks / official macro
    freshness_hours: int = Field(default=192, ge=1, le=24 * 365)
    url: str | None = None
    limit: int = Field(default=5000, ge=1, le=1000000)
    lookback_days: int = Field(default=400, ge=1, le=36500)
    categories: list[list[str]] = Field(default_factory=list)
    contracts: list[CftcContractConfig] = Field(default_factory=list)
    datasets: list[CftcDatasetConfig] = Field(default_factory=list)
    feeds: list[CollectorFeedConfig | IssuerNewsFeedConfig] = Field(
        default_factory=list
    )
    headers: dict[str, str] = Field(default_factory=dict)
    requires_api_key: bool = False
    credential_name: str | None = None
    public_api_key: str | None = None
    api_key_param: str | None = None
    max_retries: int = Field(default=1, ge=0, le=20)
    # Free public sources (issuer_news / issuer_transcripts / public_equities
    # / sec_form4 / finra_short_volume / cboe_options).  Fields are shared
    # across the union schema; per-source applicability is enforced by
    # ``_COLLECTOR_ALLOWED_FIELDS`` exactly as each collector consumes them.
    state_path: str | None = None
    issuers: list[SecForm4IssuerConfig | IssuerTranscriptConfig] = Field(
        default_factory=list
    )
    symbols: list[str | FinraSymbolConfig] = Field(default_factory=list)
    chart_base_url: str | None = None
    max_symbols: int = Field(default=50, ge=1, le=1000)
    # Strict: only a real YAML boolean opts the desk's symbol expansion in;
    # coerced strings (e.g. "yes") are rejected instead of silently enabling
    # extra market collection.
    include_active_theses: bool = Field(default=False, strict=True)
    # Strict opt-in for the checked-in investment universe.  Only
    # public_equities accepts this flag; other collectors keep their narrower
    # source-specific universes.
    include_investment_universe: bool = Field(default=False, strict=True)
    range: Literal["5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y"] | None = None
    bootstrap_range: (
        Literal["5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y"] | None
    ) = None
    interval: Literal["1d"] | None = None
    timeout_seconds: float = Field(default=30.0, ge=5.0, le=60.0)
    max_issuers: int = Field(default=20, ge=1, le=200)
    max_page_bytes: int = Field(default=2_000_000, ge=65_536, le=52_428_800)
    max_audio_bytes: int = Field(default=250_000_000, ge=1_048_576, le=2_147_483_648)
    max_links_per_page: int = Field(default=50, ge=1, le=500)
    max_records_per_issuer: int = Field(default=25, ge=1, le=500)
    max_redirects: int = Field(default=5, ge=0, le=10)
    audio_timeout_seconds: float = Field(default=300.0, ge=10.0, le=3600.0)
    transcription: TranscriptionSettingsConfig | None = None
    request_interval_seconds: float = Field(default=0.1, ge=0.0, le=60.0)
    max_filings_per_issuer: int = Field(default=100, ge=1, le=500)
    max_document_bytes: int = Field(default=25_000_000, ge=10_000, le=50_000_000)
    max_submissions_bytes: int = Field(default=25_000_000, ge=100_000, le=100_000_000)
    archive_url: str | None = None
    file_prefix: str | None = None
    file_suffix: str | None = None
    dates: list[str] | None = None
    max_file_bytes: int = Field(default=20_000_000, ge=100_000, le=200_000_000)
    source_timezone: str | None = None
    delay_minutes: int = Field(default=15, ge=0, le=1440)
    max_contracts_per_symbol: int = Field(default=20_000, ge=1, le=1_000_000)
    max_expiries: int = Field(default=40, ge=1, le=1000)
    max_response_bytes: int = Field(default=30_000_000, ge=1024, le=500_000_000)
    rate_delay_seconds: float = Field(default=1.0, ge=0.0, le=3600.0)
    request_deadline_seconds: float = Field(default=60.0, ge=1.0, le=3600.0)

    @field_validator("source_timezone")
    @classmethod
    def _validate_source_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return _validate_zoneinfo(value)

    @field_validator("dates")
    @classmethod
    def _validate_explicit_dates(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        from datetime import date as date_of_year

        for item in value:
            try:
                date_of_year.fromisoformat(str(item).strip())
            except ValueError as exc:
                raise ValueError(f"invalid ISO trade date {item!r}") from exc
        return value


class MacroYieldCurveThresholds(FrozenModel):
    deep_inversion: float
    inverted: float
    flat: float
    normal: float

    @model_validator(mode="after")
    def _validate_monotonic(self) -> MacroYieldCurveThresholds:
        if not (self.deep_inversion < self.inverted < self.flat < self.normal):
            raise ValueError(
                "yield_curve thresholds must be strictly increasing "
                "(deep_inversion < inverted < flat < normal)"
            )
        return self


class MacroVixThresholds(FrozenModel):
    very_low: float
    low: float
    moderate: float
    elevated: float
    high: float

    @model_validator(mode="after")
    def _validate_monotonic(self) -> MacroVixThresholds:
        values = [
            self.very_low,
            self.low,
            self.moderate,
            self.elevated,
            self.high,
        ]
        if any(
            previous >= current
            for previous, current in zip(values, values[1:], strict=False)
        ):
            raise ValueError(
                "vix thresholds must be strictly increasing "
                "(very_low < low < moderate < elevated < high)"
            )
        return self


class MacroCreditSpreadThresholds(FrozenModel):
    tight: float
    normal: float
    widening: float

    @model_validator(mode="after")
    def _validate_monotonic(self) -> MacroCreditSpreadThresholds:
        if not (self.tight < self.normal < self.widening):
            raise ValueError(
                "credit_spread thresholds must be strictly increasing "
                "(tight < normal < widening)"
            )
        return self


class MacroRegimeThresholds(FrozenModel):
    yield_curve: MacroYieldCurveThresholds
    vix: MacroVixThresholds
    credit_spread: MacroCreditSpreadThresholds


class ProcessorAssetContextConfig(FrozenModel):
    channels: list[str] = Field(default_factory=list)
    positioning_effects: dict[str, str] = Field(default_factory=dict)
    channel_effects: list[str] = Field(default_factory=list)


class ProcessorConfig(FrozenModel):
    enabled: bool = False
    depends_on: list[str] = Field(default_factory=list)
    prompt_template: str | None = None
    schedule: str | None = None
    # Thresholds are consumed only by macro_regime; the exact category and
    # boundary names are enforced (partial/misspelled/non-monotonic config
    # is rejected instead of silently merging with defaults).
    thresholds: MacroRegimeThresholds | None = None
    asset_context: dict[str, ProcessorAssetContextConfig] = Field(default_factory=dict)
    # Runtime-consumed knobs: briefing atom cap and market-intelligence
    # forced-inference flag.
    max_atoms: int = Field(default=30, ge=1, le=200)
    force_inference: bool = False


# ---------------------------------------------------------------------------
# news sources
# ---------------------------------------------------------------------------


class ReutersConfig(FrozenModel):
    enabled: bool = True
    on_demand_only: bool = False
    schedule_enabled: bool = True
    schedule: str | None = None
    max_pages: int = Field(default=3, ge=1, le=1000)
    state_path: str | None = None
    output_path: str | None = None
    markets_sections: list[str] = Field(default_factory=list)
    markets_keywords: list[str] = Field(default_factory=list)


class KobeissiConfig(FrozenModel):
    enabled: bool = True
    on_demand_only: bool = True
    schedule_enabled: bool = False
    schedule: str | None = None
    api_key: str = ""
    user_id: str | None = None
    api_base: str | None = None
    count: int = Field(default=20, ge=1, le=1000)
    state_path: str | None = None
    output_path: str | None = None


class NewsFeedConfig(FrozenModel):
    output_path: NonBlankText = "/var/lib/trading-data/news"
    history_days: int = Field(default=7, ge=1, le=3650)


# ---------------------------------------------------------------------------
# watchlist / api / dashboard / data quality / budgets / filings
# ---------------------------------------------------------------------------


class WatchlistInstrumentConfig(FrozenModel):
    symbol: NonBlankText
    type: NonBlankText = "forex"


class InvestingWatchlistConfig(FrozenModel):
    name: NonBlankText
    symbols: list[str] = Field(default_factory=list)


class InvestingConfig(FrozenModel):
    watchlists: list[InvestingWatchlistConfig] = Field(default_factory=list)


class WatchlistConfig(FrozenModel):
    trading: list[WatchlistInstrumentConfig] = Field(default_factory=list)
    investing: InvestingConfig = Field(default_factory=InvestingConfig)


class ApiConfig(FrozenModel):
    """API-process configuration.

    The API bind address, internal orchestrator origin, and authentication
    credentials are intentionally deployment-controlled environment settings.
    They are not accepted in setup/operator state because that state is a
    lower-trust input and must not retarget credentialed internal requests.
    """


class StaleThresholdsConfig(FrozenModel):
    briefing_hours: float = Field(default=18, ge=0.0, le=24 * 365)
    regime_hours: float = Field(default=18, ge=0.0, le=24 * 365)
    macro_hours: float = Field(default=30, ge=0.0, le=24 * 365)
    events_hours: float = Field(default=8, ge=0.0, le=24 * 365)


class DashboardIndicatorConfig(FrozenModel):
    series_id: NonBlankText
    label: NonBlankText
    precision: int = Field(default=2, ge=0, le=20)
    category: NonBlankText
    note: str | None = None


class DashboardConfig(FrozenModel):
    stale_thresholds: StaleThresholdsConfig = Field(
        default_factory=StaleThresholdsConfig
    )
    indicators: list[DashboardIndicatorConfig] = Field(default_factory=list)


class DataQualitySourceConfig(FrozenModel):
    grace_periods: dict[str, float] = Field(default_factory=dict)


class ReadinessConfig(FrozenModel):
    """Explicit readiness-critical settings.

    Data-quality checks always produce a truthful verdict (missing expected
    data is unknown/degraded, never healthy) but do NOT block readiness by
    default — a fresh install must not deadlock on a not-yet-run FRED cron.
    Operators who want quality to gate readiness list check ids (or source
    ids such as ``fred``) here; only those checks return 503 when failing.
    """

    data_quality_checks: list[NonBlankText] = Field(default_factory=list)


class BudgetsConfig(FrozenModel):
    daily_llm_usd: float = Field(default=2.0, ge=0.0)
    warn_at_pct: float = Field(default=80.0, ge=0.0, le=100.0)
    # Reservation-policy keys (budget-enforcement workstream). There is NO
    # default per-call estimate: the orchestrator never guesses pricing for a
    # paid model, so an absent reservation_estimate_usd stays None and the
    # paid admission fails closed until pricing is explicitly configured.
    reservation_estimate_usd: float | None = Field(default=None, gt=0.0)
    estimates: dict[str, float] = Field(default_factory=dict)
    reservation_ttl_seconds: float = Field(default=600.0, gt=0.0)

    @field_validator("estimates")
    @classmethod
    def _validate_estimates(cls, value: dict[str, float]) -> dict[str, float]:
        for processor, estimate in value.items():
            if not isinstance(estimate, (int, float)) or estimate <= 0:
                raise ValueError(
                    f"budgets.estimates[{processor}] must be a positive number"
                )
        return value


class InvestmentCompanyConfig(FrozenModel):
    company: NonBlankText
    symbol: NonBlankText
    region: NonBlankText = "US"
    industry: str | None = None
    market: str | None = None
    # Source identifiers; the one matching ``region`` must be set at runtime
    # for the source to be discovered (validated at use time).
    cik: str | None = None
    sec_cik: str | None = None
    company_number: str | None = None
    edinet_code: str | None = None
    edinet: str | None = None
    dart_code: str | None = None
    corp_code: str | None = None


class InvestmentDocumentsConfig(FrozenModel):
    file_root: str | None = None


class InvestmentFilingsConfig(FrozenModel):
    enabled: bool = True
    schedule: str | None = None
    lookback_days: int = Field(default=730, ge=1, le=36500)
    auto_analyze: bool = True
    run_on_startup: bool = True
    company_workers: int = Field(default=1, ge=1, le=256)
    universe: NonBlankText = "top_us_uk_eu_100"
    companies: list[InvestmentCompanyConfig] = Field(default_factory=list)
    sec_user_agent: str = (
        "TradingDataInvestmentResearch/1.0 (research@trading-data-platform.local)"
    )
    companies_house_api_key: str = ""
    edinet_api_key: str = ""
    opendart_api_key: str = ""


class DemoConfig(FrozenModel):
    enabled: bool = False


# ---------------------------------------------------------------------------
# top-level application configuration
# ---------------------------------------------------------------------------


#: Executable component registries — the ids the runtime can actually
#: dispatch.  Config keys must be validated against these so a typo'd or
#: unknown id fails startup instead of being silently ignored (or worse,
#: enqueued for a dispatcher that does not know it).  The API trigger routes
#: import the same constants so there is exactly one authoritative list.
KNOWN_PROCESSORS = frozenset(
    {"macro_regime", "event_impact", "briefing", "market_intelligence"}
)
KNOWN_COLLECTORS = frozenset(
    {
        "fred",
        "forex_factory",
        "oanda",
        "cftc",
        "central_banks",
        "oecd",
        "ecb",
        "boe",
        "eia",
        # Free/public keyless sources (scheduled without credentials).
        "issuer_news",
        "issuer_transcripts",
        "public_equities",
        "sec_form4",
        "finra_short_volume",
        "cboe_options",
        "company_expectations",
    }
)
KNOWN_NEWS_SOURCES = frozenset({"reuters", "kobeissi"})


#: Fields each collector source may actually set, derived from the source's
#: runtime consumers.  A field that is valid for one source is rejected when
#: set on another (e.g. ``fred.base_url`` or ``oanda.series``) instead of
#: validating and then silently doing nothing.
_COLLECTOR_ALLOWED_FIELDS: dict[str, frozenset[str]] = {
    "fred": frozenset(
        {
            "enabled",
            "schedule",
            "api_key",
            "metadata_ttl_days",
            "max_concurrency",
            "backfill_years_daily",
            "backfill_years_monthly",
            "backfill_years_quarterly",
            "revision_window_days",
            "series",
        }
    ),
    "forex_factory": frozenset(
        {
            "enabled",
            "schedule",
            "source_url",
            "weekly_export_base_url",
            "currencies",
            "min_impact",
            "user_agent",
            "request_delay_seconds",
        }
    ),
    "oanda": frozenset(
        {
            "enabled",
            "schedule",
            "api_key",
            "stream_enabled",
            "environment",
            "snapshot_timeframe",
            "account_id",
            "base_url",
            "instruments",
        }
    ),
    "cftc": frozenset(
        {
            "enabled",
            "schedule",
            "freshness_hours",
            "lookback_days",
            "datasets",
        }
    ),
    "central_banks": frozenset({"enabled", "schedule", "freshness_hours", "feeds"}),
    "oecd": frozenset({"enabled", "schedule", "freshness_hours", "headers", "series"}),
    "ecb": frozenset({"enabled", "schedule", "freshness_hours", "headers", "series"}),
    "boe": frozenset({"enabled", "schedule", "freshness_hours", "headers", "series"}),
    "eia": frozenset(
        {
            "enabled",
            "schedule",
            "freshness_hours",
            "headers",
            "requires_api_key",
            "credential_name",
            "api_key",
            "public_api_key",
            "api_key_param",
            "max_retries",
            "series",
        }
    ),
    "issuer_news": frozenset({"enabled", "schedule", "feeds", "state_path"}),
    "issuer_transcripts": frozenset(
        {
            "enabled",
            "schedule",
            "issuers",
            "max_issuers",
            "timeout_seconds",
            "audio_timeout_seconds",
            "max_page_bytes",
            "max_document_bytes",
            "max_audio_bytes",
            "max_redirects",
            "max_links_per_page",
            "max_records_per_issuer",
            "user_agent",
            "headers",
            "transcription",
        }
    ),
    "public_equities": frozenset(
        {
            "enabled",
            "schedule",
            "symbols",
            "max_symbols",
            "max_concurrency",
            "include_active_theses",
            "include_investment_universe",
            "range",
            "bootstrap_range",
            "interval",
            "timeout_seconds",
            "user_agent",
            "chart_base_url",
        }
    ),
    "company_expectations": frozenset(
        {
            "enabled",
            "schedule",
            "symbols",
            "max_symbols",
            "include_active_theses",
            "base_url",
            "timeout_seconds",
            "max_concurrency",
            "user_agent",
            "lookback_days",
        }
    ),
    "sec_form4": frozenset(
        {
            "enabled",
            "schedule",
            "issuers",
            "lookback_days",
            "max_filings_per_issuer",
            "max_concurrency",
            "request_interval_seconds",
            "max_document_bytes",
            "max_submissions_bytes",
            "user_agent",
            "url",
            "archive_url",
        }
    ),
    "finra_short_volume": frozenset(
        {
            "enabled",
            "schedule",
            "symbols",
            "url",
            "file_prefix",
            "file_suffix",
            "lookback_days",
            "max_file_bytes",
            "request_interval_seconds",
            "dates",
        }
    ),
    "cboe_options": frozenset(
        {
            "enabled",
            "schedule",
            "symbols",
            "base_url",
            "source_timezone",
            "user_agent",
            "include_active_theses",
            "delay_minutes",
            "max_symbols",
            "max_contracts_per_symbol",
            "max_expiries",
            "max_response_bytes",
            "rate_delay_seconds",
            "request_deadline_seconds",
        }
    ),
}


class AppConfig(FrozenModel):
    database: DatabaseConfig
    llm: LlmConfig = Field(default_factory=LlmConfig)
    timezone: TimezoneConfig = Field(default_factory=TimezoneConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    orchestration: OrchestrationConfig = Field(default_factory=OrchestrationConfig)
    event_pipeline: EventPipelineConfig = Field(default_factory=EventPipelineConfig)
    market_state: MarketStateConfig = Field(default_factory=MarketStateConfig)
    macro_event_mappings: dict[str, MacroEventMappingConfig] = Field(
        default_factory=dict
    )
    event_class_time_sensitivity: dict[str, float] = Field(default_factory=dict)
    reaction_windows: ReactionWindowsConfig = Field(
        default_factory=ReactionWindowsConfig
    )
    story_clustering: StoryClusteringConfig = Field(
        default_factory=StoryClusteringConfig
    )
    story_confirmation: StoryConfirmationConfig = Field(
        default_factory=StoryConfirmationConfig
    )
    analysis_atoms: AnalysisAtomsConfig = Field(default_factory=AnalysisAtomsConfig)
    analysis_routing: AnalysisRoutingConfig = Field(
        default_factory=AnalysisRoutingConfig
    )
    research_intelligence: ResearchIntelligenceConfig = Field(
        default_factory=ResearchIntelligenceConfig
    )
    thesis_autonomy: ThesisAutonomyConfig = Field(default_factory=ThesisAutonomyConfig)
    research_control_plane: ResearchControlPlaneConfig = Field(
        default_factory=ResearchControlPlaneConfig
    )
    collectors: dict[str, CollectorConfig] = Field(default_factory=dict)
    processors: dict[str, ProcessorConfig] = Field(default_factory=dict)
    reuters: ReutersConfig = Field(default_factory=ReutersConfig)
    kobeissi: KobeissiConfig = Field(default_factory=KobeissiConfig)
    news_feed: NewsFeedConfig = Field(default_factory=NewsFeedConfig)
    watchlist: WatchlistConfig = Field(default_factory=WatchlistConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    data_quality: dict[str, DataQualitySourceConfig] = Field(default_factory=dict)
    readiness: ReadinessConfig = Field(default_factory=ReadinessConfig)
    budgets: BudgetsConfig = Field(default_factory=BudgetsConfig)
    investment_filings: InvestmentFilingsConfig = Field(
        default_factory=InvestmentFilingsConfig
    )
    investment_documents: InvestmentDocumentsConfig = Field(
        default_factory=InvestmentDocumentsConfig
    )
    demo: DemoConfig = Field(default_factory=DemoConfig)

    @model_validator(mode="after")
    def _check_llm_credentials(self) -> AppConfig:
        if (
            "llm" in self.model_fields_set
            and not self.demo.enabled
            and self.llm.provider == "openrouter"
            and not self.llm.api_key
        ):
            raise ValueError(
                "llm.api_key is required when llm.provider is 'openrouter' "
                "(set OPENROUTER_API_KEY or the secrets file)"
            )
        return self

    @model_validator(mode="after")
    def _check_placeholder_credentials(self) -> AppConfig:
        """Reject placeholder/default credentials outside demo/test modes.

        Production must never run with example values such as ``change-me``
        or ``replace-with-*``. Explicit demo/test deployments tolerate fixture
        credentials; values are never logged.
        """
        deployment_mode = (
            os.environ.get("DEPLOYMENT_MODE", "production").strip().lower()
        )
        if self.demo.enabled or deployment_mode in {"demo", "test"}:
            return self
        if not self.database.password:
            raise ValueError("database.password must be set in production mode")
        for label, value in (
            ("database.password", self.database.password),
            ("database.user", self.database.user),
            ("llm.api_key", self.llm.api_key),
        ):
            if value and _is_placeholder_credential(value):
                raise ValueError(
                    f"{label} must not be a placeholder value; set a real secret"
                )
        # Operator (dashboard) credentials are not modeled in config — the
        # auth layer validates them from the environment (api/auth.py).
        for source_id, collector in self.collectors.items():
            if (
                collector.enabled
                and collector.api_key
                and _is_placeholder_credential(collector.api_key)
            ):
                raise ValueError(
                    f"collectors.{source_id}.api_key must not be a placeholder value"
                )
        if (
            self.kobeissi.enabled
            and self.kobeissi.api_key
            and _is_placeholder_credential(self.kobeissi.api_key)
        ):
            raise ValueError("kobeissi.api_key must not be a placeholder value")
        return self

    @model_validator(mode="after")
    def _check_enabled_collector_keys(self) -> AppConfig:
        """Enabled keyed collectors must carry a real credential.

        FRED and OANDA (and any collector declaring ``requires_api_key``)
        fail startup with a blank or placeholder key while enabled; disabled
        collectors may stay blank so an operator can pre-provision config.
        """
        deployment_mode = (
            os.environ.get("DEPLOYMENT_MODE", "production").strip().lower()
        )
        if self.demo.enabled or deployment_mode in {"demo", "test"}:
            return self
        for source_id, collector in self.collectors.items():
            requires_key = bool(collector.requires_api_key) or source_id in {
                "fred",
                "oanda",
            }
            # A configured public/demo key fallback (e.g. EIA's DEMO_KEY)
            # makes a blank api_key valid while the source is enabled.
            if collector.public_api_key:
                requires_key = False
            if not (collector.enabled and requires_key):
                continue
            if not collector.api_key:
                raise ValueError(
                    f"collectors.{source_id}.api_key is required while the "
                    "source is enabled"
                )
            if _is_placeholder_credential(collector.api_key):
                raise ValueError(
                    f"collectors.{source_id}.api_key must not be a placeholder value"
                )
        for source_id, collector in self.collectors.items():
            allowed = _COLLECTOR_ALLOWED_FIELDS.get(source_id)
            if allowed is None:
                continue
            unsupported = set(collector.model_fields_set) - allowed
            if unsupported:
                raise ValueError(
                    f"collectors.{source_id} sets field(s) not applicable to "
                    "this source: " + ", ".join(sorted(unsupported))
                )
        # The shared ``max_symbols`` field is capped per source: the
        # public_equities collector raises at runtime above its hard cap
        # (HARD_MAX_SYMBOLS), while other sources (e.g. cboe_options) accept
        # the full schema bound.  Reject the un-honorable value at startup.
        public_equities = self.collectors.get("public_equities")
        if (
            public_equities is not None
            and public_equities.max_symbols > PUBLIC_EQUITIES_HARD_MAX_SYMBOLS
        ):
            raise ValueError(
                "collectors.public_equities.max_symbols must be at most "
                f"{PUBLIC_EQUITIES_HARD_MAX_SYMBOLS} (the collector's hard cap)"
            )
        if (
            public_equities is not None
            and public_equities.max_concurrency > PUBLIC_EQUITIES_HARD_MAX_CONCURRENCY
        ):
            raise ValueError(
                "collectors.public_equities.max_concurrency must be at most "
                f"{PUBLIC_EQUITIES_HARD_MAX_CONCURRENCY} "
                "(the collector's hard cap)"
            )
        return self

    @model_validator(mode="after")
    def _check_public_equities_symbols(self) -> AppConfig:
        """public_equities accepts only canonical, unique, capped strings.

        The shared ``symbols`` field also admits FINRA structured entries
        (``FinraSymbolConfig``) for finra_short_volume, but the
        public_equities collector raises at runtime on anything but strings.
        Reject structured entries at validation time so a broken section can
        never reach dispatch, in any deployment mode.  String symbols are
        canonicalized exactly like the collector (trim + uppercase), must
        match the collector symbol grammar, be unique after
        canonicalization, and fit within the section's ``max_symbols``
        (itself capped at the collector hard max), so dispatch-time
        validation can never reject the same section later.
        """
        public_equities = self.collectors.get("public_equities")
        if public_equities is None:
            return self
        canonical: list[str] = []
        for symbol in public_equities.symbols:
            if not isinstance(symbol, str):
                raise ValueError(
                    "collectors.public_equities.symbols must contain "
                    "only strings; structured symbol entries are "
                    "reserved for finra_short_volume"
                )
            normalized = symbol.strip().upper()
            if not PUBLIC_EQUITIES_SYMBOL_PATTERN.fullmatch(normalized):
                raise ValueError(
                    "collectors.public_equities.symbols entries must be "
                    "nonblank and match the public_equities symbol grammar "
                    "after trimming and uppercasing"
                )
            canonical.append(normalized)
        if len(set(canonical)) != len(canonical):
            raise ValueError(
                "collectors.public_equities.symbols contains duplicates "
                "after trimming and uppercasing"
            )
        if len(canonical) > public_equities.max_symbols:
            raise ValueError(
                "collectors.public_equities.symbols exceeds the configured "
                f"limit of {public_equities.max_symbols} symbols"
            )
        return self

    @model_validator(mode="after")
    def _check_company_expectation_symbols(self) -> AppConfig:
        collector = self.collectors.get("company_expectations")
        if collector is None:
            return self
        canonical: list[str] = []
        for symbol in collector.symbols:
            if not isinstance(symbol, str):
                raise ValueError(
                    "collectors.company_expectations.symbols must contain only strings"
                )
            normalized = symbol.strip().upper()
            if not PUBLIC_EQUITIES_SYMBOL_PATTERN.fullmatch(normalized):
                raise ValueError(
                    "collectors.company_expectations.symbols entries must be "
                    "canonical market symbols"
                )
            canonical.append(normalized)
        if len(canonical) != len(set(canonical)):
            raise ValueError(
                "collectors.company_expectations.symbols contains duplicates"
            )
        if len(canonical) > collector.max_symbols:
            raise ValueError(
                "collectors.company_expectations.symbols exceeds max_symbols"
            )
        return self

    @model_validator(mode="after")
    def _check_known_components(self) -> AppConfig:
        unknown_collectors = set(self.collectors) - KNOWN_COLLECTORS
        if unknown_collectors:
            raise ValueError(
                "unknown collector id(s) in config: "
                + ", ".join(sorted(unknown_collectors))
                + f"; executable collectors are {', '.join(sorted(KNOWN_COLLECTORS))}"
            )
        unknown_processors = set(self.processors) - KNOWN_PROCESSORS
        if unknown_processors:
            raise ValueError(
                "unknown processor id(s) in config: "
                + ", ".join(sorted(unknown_processors))
                + f"; executable processors are {', '.join(sorted(KNOWN_PROCESSORS))}"
            )
        return self

    @model_validator(mode="after")
    def _check_thesis_autonomy_budget(self) -> AppConfig:
        """Per-run autonomy budget must never exceed the global daily cap.

        The daily budget is the ultimate ceiling for every LLM consumer; a
        per-run ceiling above it would be dead configuration at best and a
        surprise override at worst, so it is rejected instead.
        """
        if self.thesis_autonomy.model_budget_usd_per_run > self.budgets.daily_llm_usd:
            raise ValueError(
                "thesis_autonomy.model_budget_usd_per_run must not exceed "
                "budgets.daily_llm_usd; the global daily budget is the "
                "ultimate cap"
            )
        return self

    @model_validator(mode="after")
    def _check_research_control_plane_budget(self) -> AppConfig:
        """A planning ceiling cannot exceed the authoritative daily LLM cap."""
        if (
            self.research_control_plane.model_budget_usd_per_plan
            > self.budgets.daily_llm_usd
        ):
            raise ValueError(
                "research_control_plane.model_budget_usd_per_plan must not exceed "
                "budgets.daily_llm_usd; the global daily budget is authoritative"
            )
        return self

    @model_validator(mode="after")
    def _check_reservation_ttl_vs_call_deadline(self) -> AppConfig:
        """Reservation lifetime must outlive the longest in-flight call.

        Paid OpenRouter calls are exactly ONE HTTP attempt (no inbound
        Idempotency-Key contract; retries could double-bill), so the
        ``make_request`` deadline is ``stage_timeout_seconds``.  A
        reservation that expires while the paid call is still running would
        release funds and let concurrent calls over-admit, so require at
        least 30s of headroom.  (Schema-validation retries are separate,
        individually budgeted LLMStage calls.)
        """
        deadline = self.llm.stage_timeout_seconds + 30
        if self.budgets.reservation_ttl_seconds < deadline:
            raise ValueError(
                "budgets.reservation_ttl_seconds must be >= "
                f"llm.stage_timeout_seconds + 30 ({deadline}s); "
                "a shorter reservation could expire while a paid call is in flight"
            )
        return self


# ---------------------------------------------------------------------------
# versioned snapshots and restart semantics
# ---------------------------------------------------------------------------

#: Operator-managed provider secrets stored in secrets.env.  Once the
#: secrets file exists these keys are authoritative: an absent key means
#: "unset", never a fallback to the process environment, so a deleted
#: credential cannot silently remain active.
MANAGED_SECRET_KEYS = frozenset(
    {
        "OPENROUTER_API_KEY",
        "FRED_API_KEY",
        "OANDA_API_KEY",
        "EIA_API_KEY",
        "TWITTERAPI_KEY",
    }
)

#: Every top-level section is restart-sensitive.  The scheduler captures
#: job triggers and the full config object at startup, and durable workers
#: (role_worker/OperationWorker) retain that object for every executed job,
#: so ANY configuration change is stale for worker-executed runs until a
#: process restart.  Conservative by design: any change marks
#: restart_required / config-version mismatch.
RESTART_SENSITIVE_SECTIONS = frozenset(AppConfig.model_fields)


@dataclass(frozen=True)
class ConfigSource:
    """One input file contributing to a configuration snapshot."""

    path: str
    mtime_ns: int | None
    digest: str  # short sha256 of the file content; "" when absent


@dataclass(frozen=True)
class ConfigSnapshot:
    """An immutable, versioned view of the active configuration."""

    version: str
    ordinal: int
    applied_at: str
    config: AppConfig
    sources: tuple[ConfigSource, ...]


def _content_digest(config: AppConfig) -> str:
    payload = config.model_dump(mode="json")
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def build_snapshot(
    config: AppConfig,
    sources: Sequence[ConfigSource],
    ordinal: int,
) -> ConfigSnapshot:
    """Build a versioned snapshot from the validated configuration."""
    return ConfigSnapshot(
        version=_content_digest(config),
        ordinal=ordinal,
        applied_at=datetime.now(UTC).isoformat(timespec="seconds"),
        config=config,
        sources=tuple(sources),
    )


def _restart_value(config: AppConfig, section: str) -> Any:
    """Serialize one section for restart comparison (full content)."""
    value = getattr(config, section, None)
    if value is None:
        return None
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {
            str(key): (
                item.model_dump(mode="json") if isinstance(item, BaseModel) else item
            )
            for key, item in value.items()
        }
    return value


def restart_projection(config: AppConfig) -> dict[str, Any]:
    """Serialize only the restart-sensitive sections for comparison."""
    return {name: _restart_value(config, name) for name in RESTART_SENSITIVE_SECTIONS}


def restart_changes(previous: AppConfig, current: AppConfig) -> list[str]:
    """Names of restart-sensitive sections that differ between snapshots."""
    before = restart_projection(previous)
    after = restart_projection(current)
    return sorted(
        name
        for name in RESTART_SENSITIVE_SECTIONS
        if before.get(name) != after.get(name)
    )


def config_restart_required(previous: AppConfig, current: AppConfig) -> bool:
    """True when a restart-sensitive section changed between two snapshots."""
    return bool(restart_changes(previous, current))


# ---------------------------------------------------------------------------
# shared loading machinery (merge, substitution, fingerprints, snapshots)
# ---------------------------------------------------------------------------


_PLACEHOLDER_CREDENTIAL_PATTERN = re.compile(
    r"(?i)^(change-me|change_me|changeme|changeme123|password|secret|demo)$"
    r"|^replace-with-"
)

#: Exact values that are never acceptable as security-sensitive credentials.
_PLACEHOLDER_EXACT = frozenset(
    {
        "changeme",
        "change-me",
        "change_me",
        "changeme123",
        "replaceme",
        "replace-me",
        "replace_me",
        "placeholder",
        "your-key",
        "your-secret",
        "secret",
        "password",
        "demo",
        "demo-key",
        "demo-secret",
        "test",
        "test-key",
        "test-secret",
        "development",
        "dev",
        "example",
        "sample",
    }
)
#: Prefixes that are never acceptable as security-sensitive credentials.
_PLACEHOLDER_PREFIXES = (
    "replace-with-",
    "replace_with_",
    "replaceme",
    "changeme",
    "change-me",
    "change_me",
    "your-key",
    "your_secret",
    "your-secret",
    "sample-",
    "sample_",
    "example-",
    "example_",
    "demo-",
    "demo_",
    "test-",
    "test_",
    "secret-",
    "secret_",
    "dev-",
    "dev_",
    "development-",
)
_PLACEHOLDER_EXACT_LOWER = frozenset(value.lower() for value in _PLACEHOLDER_EXACT)
_PLACEHOLDER_PREFIXES_LOWER = tuple(prefix.lower() for prefix in _PLACEHOLDER_PREFIXES)


def is_placeholder_credential(value: str) -> bool:
    """True when a credential looks like a placeholder/default value."""
    normalized = value.strip().lower()
    if normalized in _PLACEHOLDER_EXACT_LOWER:
        return True
    return normalized.startswith(_PLACEHOLDER_PREFIXES_LOWER)


def validate_operator_credentials(
    username: str,
    password: str,
    *,
    deployment_mode: str,
    demo_enabled: bool = False,
) -> None:
    """Fail-closed production gate for operator (dashboard) credentials.

    Outside demo/test deployment modes the username and password must be set,
    non-empty, and not placeholder values; the password must additionally be
    at least 12 characters.  Demo/test modes tolerate demo credentials.  The
    credential values are never logged; the variable name appears in errors.
    """
    if deployment_mode in {"demo", "test"} or demo_enabled:
        return
    missing = []
    if not username or not username.strip():
        missing.append("DASHBOARD_USER")
    if not password or not password.strip():
        missing.append("DASHBOARD_PASSWORD")
    if missing:
        raise ValueError(
            f"{' and '.join(missing)} must be set in {deployment_mode or 'production'} mode"
        )
    if is_placeholder_credential(username):
        raise ValueError("DASHBOARD_USER must not be a placeholder value")
    if is_placeholder_credential(password):
        raise ValueError("DASHBOARD_PASSWORD must not be a placeholder value")
    if len(password.strip()) < 12:
        raise ValueError("DASHBOARD_PASSWORD must be at least 12 characters")


def _is_placeholder_credential(value: str) -> bool:
    return is_placeholder_credential(value)


_SECRET_LINE_PATTERN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def parse_secrets_file(text: str) -> dict[str, str]:
    """Strict ``KEY=VALUE`` secrets parser shared by setup and config loading.

    Blank lines and ``#`` comments are skipped.  Names must be environment-
    style identifiers (``[A-Za-z_][A-Za-z0-9_]*``); values keep their raw
    semantics (no stripping, so an explicit empty value is a tombstone, not a
    deletion) but must not contain control characters.  Malformed lines and
    duplicate keys raise :class:`ConfigError` instead of being silently
    dropped or overwritten — tampered secret files fail early.
    """
    values: dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _SECRET_LINE_PATTERN.match(line)
        if not match:
            raise ConfigError(
                f"malformed secrets line {line_number}: expected KEY=VALUE "
                "with an environment-style key name"
            )
        key = match.group(1)
        value = match.group(2)
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ConfigError(
                f"secrets value on line {line_number} ({key!r}) contains "
                "control characters"
            )
        if key in values:
            raise ConfigError(f"duplicate secrets key on line {line_number}: {key!r}")
        values[key] = value
    return values


@contextmanager
def shared_state_lock(lock_path: str) -> Iterator[None]:
    """Hold a shared flock so setup commits (exclusive) never interleave.

    Config loading takes the shared lock across resolve + fingerprint +
    parse of operator.yaml/secrets.env; setup commits take the exclusive
    lock on the same ``.setup.lock`` while flipping the versioned ``current``
    pointer.

    Read-only state mounts are tolerated: an existing lock file is opened
    read-only (``flock(LOCK_SH)`` works on read-only fds), and a missing
    lock file on a read-only, pre-setup state directory proceeds unlocked —
    no committed mutation exists yet, and the atomic ``current`` pointer
    flip still prevents split reads.  Writers create the lock file with
    ``a+`` as before.
    """
    if not lock_path or not os.path.isdir(os.path.dirname(lock_path)):
        yield
        return
    # The lock file is either opened read-only as binary (existing file) or
    # created append-mode as text (first writer); both only need fileno/close.
    handle: BinaryIO | TextIO
    if os.path.exists(lock_path):
        handle = open(lock_path, "rb")
    else:
        try:
            handle = open(lock_path, "a+")
        except OSError:
            # Read-only state dir without a lock file: pre-setup, nothing
            # committed yet — proceed unlocked.
            yield
            return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


@contextmanager
def committed_config_paths(
    operator_path: str, secrets_path: str
) -> Iterator[tuple[str, str]]:
    """Resolve one committed operator/secrets version under the root lock.

    Setup serializes commits on ``<state>/.setup.lock`` and flips
    ``<state>/current`` atomically.  Readers must acquire that root lock
    *before* resolving the stable ``operator.yaml``/``secrets.env`` links;
    locking inside the resolved version directory does not synchronize with
    setup and can observe two different versions.

    Custom paths in different directories have no shared setup transaction
    and are returned unchanged.
    """
    operator_dir = os.path.dirname(os.path.abspath(operator_path))
    secrets_dir = os.path.dirname(os.path.abspath(secrets_path))
    if operator_dir != secrets_dir:
        yield operator_path, secrets_path
        return

    with shared_state_lock(os.path.join(operator_dir, ".setup.lock")):
        operator_real = os.path.realpath(operator_path)
        secrets_real = os.path.realpath(secrets_path)
        if os.path.dirname(operator_real) == os.path.dirname(secrets_real):
            version_dir = os.path.dirname(operator_real)
            yield (
                os.path.join(version_dir, os.path.basename(operator_path)),
                os.path.join(version_dir, os.path.basename(secrets_path)),
            )
        else:
            # Legacy/non-versioned paths: the root lock still prevents a
            # setup migration from changing either file during this load.
            yield operator_path, secrets_path


def _store_locked[F: Callable[..., Any]](method: F) -> F:
    """Serialize all mutable ConfigStore state transitions per process."""

    @wraps(method)
    def wrapper(self: ConfigStore, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return cast(F, wrapper)


def _safe_config_error(exc: Exception) -> str:
    """Bounded, redacted error text for rejected-reload status.

    Never includes raw source lines, YAML snippets, or credential values:
    ``ConfigError`` -> its own (already redacted) message; ``ValidationError``
    -> field loc/msg only (input values excluded); YAML-style parser errors
    -> problem plus line/column, no context snippet; ``OSError`` ->
    type/path/errno; anything else -> exception type name only.
    """
    if isinstance(exc, ConfigError):
        return " ".join(str(exc).split())[:500]
    if isinstance(exc, ValidationError):
        return _format_validation_error(exc)
    problem = getattr(exc, "problem", None)
    if isinstance(problem, str):
        mark = getattr(exc, "problem_mark", None)
        line = getattr(mark, "line", None)
        column = getattr(mark, "column", None)
        location = ""
        if isinstance(line, int):
            location = f" at line {line}"
            if isinstance(column, int):
                location += f", column {column}"
        return f"YAML parse error{location}: {problem[:200]}"
    if isinstance(exc, OSError):
        filename = getattr(exc, "filename", "") or ""
        errno = getattr(exc, "errno", None)
        errno_text = f" (errno {errno})" if errno is not None else ""
        return f"{type(exc).__name__}: {filename}{errno_text}"
    return type(exc).__name__


def _read_secrets_file(path: str) -> dict[str, str]:
    """Read and strictly parse a secrets file; absent file means no secrets."""
    if not os.path.exists(path):
        return {}
    with open(path) as handle:
        text = handle.read()
    try:
        return parse_secrets_file(text)
    except ConfigError as exc:
        raise ConfigError(f"Invalid secrets file {path}: {exc}") from exc


def _merge(base: object, override: object) -> object:
    if isinstance(base, dict) and isinstance(override, dict):
        result: dict[str, Any] = dict(base)
        for key, value in override.items():
            result[str(key)] = _merge(result.get(str(key)), value)
        return result
    return override


def _file_fingerprint(path: str) -> tuple[int | None, str]:
    """Return (mtime_ns, sha256-prefix); ``(None, "")`` when the file is absent."""
    if not os.path.exists(path):
        return None, ""
    stat = os.stat(path)
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return stat.st_mtime_ns, digest.hexdigest()[:16]


def _format_validation_error(exc: ValidationError) -> str:
    details = "; ".join(
        f"{'.'.join(str(part) for part in error.get('loc', ())) or '<root>'}: "
        f"{error.get('msg', 'invalid value')}"
        for error in exc.errors(include_url=False, include_context=False)
    )
    return details


@dataclass(frozen=True)
class RejectedConfig:
    """A reload candidate that failed validation, retained for status."""

    attempted_at: str
    error: str
    sources: tuple[tuple[str, int | None, str], ...]


class ConfigStore:
    """Process-local validated-config cache, snapshots, and reload state.

    Shared by the API and orchestrator loaders so the merge/substitution/
    fingerprint/snapshot machinery has exactly one implementation.  Service
    specifics stay in the thin wrapper: default paths, the demo transform
    (demo mode mutates the raw config differently per service), and the
    fallback value for missing environment references (the orchestrator
    substitutes ``"demo-disabled"`` in demo mode).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active_snapshot: ConfigSnapshot | None = None
        self._previous_snapshot: ConfigSnapshot | None = None
        self._startup_snapshot: ConfigSnapshot | None = None
        self._ordinal = 0
        self._force_reload = False
        self._reload_failed = False
        self._rejected: RejectedConfig | None = None
        self._fail_closed_rejected = False

    @_store_locked
    def load(
        self,
        *,
        config_path: str,
        operator_path: str,
        secrets_path: str,
        parse: Callable[[str], object],
        demo_transform: Callable[[dict[str, Any]], None] | None = None,
        missing_env_fallback: Callable[[str], str | None] | None = None,
    ) -> AppConfig:
        """Load, merge, substitute, and validate the effective configuration.

        Returns the cached snapshot when every input file is unchanged.
        When a reload candidate fails validation, the previously active
        snapshot is retained atomically: ordinary consumers keep receiving
        the last valid configuration instead of raising, while
        :meth:`status` exposes the rejected candidate's error and identity.
        """
        config_mtime, config_digest = _file_fingerprint(config_path)
        operator_mtime, operator_digest = _file_fingerprint(operator_path)
        secrets_mtime, secrets_digest = _file_fingerprint(secrets_path)

        force = self._force_reload
        self._force_reload = False

        current_sources = (
            (config_path, config_mtime, config_digest),
            (operator_path, operator_mtime, operator_digest),
            (secrets_path, secrets_mtime, secrets_digest),
        )

        cached = self._active_snapshot
        if cached is not None and not force:
            by_path = {source.path: source for source in cached.sources}
            if all(
                by_path.get(path) is not None
                and by_path[path].mtime_ns == mtime
                and by_path[path].digest == digest
                for path, mtime, digest in current_sources
            ):
                return cached.config
            # Ordinary known-rejected operator candidates are not retried on
            # every read. Credential-boundary failures are different: every
            # consumer must continue to raise rather than receive stale keys.
            if (
                self._rejected is not None
                and self._rejected.sources == current_sources
                and not self._fail_closed_rejected
            ):
                return cached.config

        if self._rejected is not None and self._rejected.sources != current_sources:
            self._rejected = None
            self._reload_failed = False
            self._fail_closed_rejected = False

        # Base/operator parse failures may retain the prior immutable snapshot.
        # Secret-file parsing and substitution are credential revocation
        # boundaries and must fail closed rather than serving stale secrets.
        fail_closed = False
        try:
            if not os.path.exists(config_path):
                raise FileNotFoundError(f"Config file not found: {config_path}")

            raw_config = parse(config_path)
            if not isinstance(raw_config, dict):
                raise ConfigError(
                    f"Config file {config_path} must contain a YAML mapping"
                )

            if os.path.exists(operator_path):
                operator = parse(operator_path)
                if operator is None:
                    operator = {}
                if not isinstance(operator, dict):
                    raise ConfigError(
                        f"Operator profile {operator_path} must contain a YAML mapping"
                    )
                raw_config = _merge(raw_config, operator)

            fail_closed = True
            secrets = _read_secrets_file(secrets_path)
            secrets_authoritative = os.path.exists(secrets_path)
            # Environment substitution is a deployment configuration check:
            # a missing or blank required credential (e.g. a deleted
            # FRED_API_KEY) must fail closed even when a prior snapshot
            # exists. A stale snapshot built from a different environment must
            # never keep serving credentials the operator removed or left
            # unset. Credential parsing/substitution remains fail-closed; only
            # complete validated material may become active.
            raw_config = _substitute_recursive(
                raw_config, secrets, secrets_authoritative, missing_env_fallback
            )
            fail_closed = False
            if demo_transform is not None:
                demo_transform(cast(dict[str, Any], raw_config))

            try:
                config = AppConfig.model_validate(raw_config)
            except ValidationError as exc:
                raise ConfigError(
                    f"Invalid configuration in {config_path}: "
                    f"{_format_validation_error(exc)}"
                ) from exc
        except Exception as exc:
            self._reload_failed = True
            self._fail_closed_rejected = fail_closed
            self._rejected = RejectedConfig(
                attempted_at=datetime.now(UTC).isoformat(timespec="seconds"),
                error=_safe_config_error(exc),
                sources=current_sources,
            )
            if (
                not fail_closed
                and cached is not None
                and cached.sources
                and cached.sources[0].path == config_path
            ):
                # Retain the prior valid snapshot only for a rejected
                # replacement of the same deployment configuration. A caller
                # switching to a different root config has no compatible
                # fallback and must receive the startup/load error.
                return cached.config
            if isinstance(exc, ConfigError):
                raise
            raise ConfigError(
                f"Failed to load configuration from {config_path}: "
                f"{_safe_config_error(exc)}"
            ) from exc
        self._reload_failed = False
        self._rejected = None
        self._fail_closed_rejected = False
        if (
            self._active_snapshot is not None
            and self._active_snapshot.sources
            and os.path.realpath(self._active_snapshot.sources[0].path)
            != os.path.realpath(config_path)
        ):
            # A different configuration root is a new process-local baseline
            # (used by isolated CLI invocations and test deployments).
            self._active_snapshot = None
            self._previous_snapshot = None
            self._startup_snapshot = None

        self._previous_snapshot = self._active_snapshot
        self._ordinal += 1
        self._active_snapshot = build_snapshot(
            config,
            sources=[
                ConfigSource(
                    path=config_path, mtime_ns=config_mtime, digest=config_digest
                ),
                ConfigSource(
                    path=operator_path, mtime_ns=operator_mtime, digest=operator_digest
                ),
                ConfigSource(
                    path=secrets_path, mtime_ns=secrets_mtime, digest=secrets_digest
                ),
            ],
            ordinal=self._ordinal,
        )
        if self._startup_snapshot is None:
            self._startup_snapshot = self._active_snapshot
        return config

    @_store_locked
    def validate_candidate(
        self,
        *,
        config_path: str,
        operator_yaml: str,
        secrets_env: str,
        parse: Callable[[str], object],
        parse_text: Callable[[str], object],
        demo_transform: Callable[[dict[str, Any]], None] | None = None,
        missing_env_fallback: Callable[[str], str | None] | None = None,
    ) -> None:
        """Validate a staged operator/secrets candidate without touching state.

        Runs the same merge, substitution (secrets authoritative), demo
        transform, and :class:`AppConfig` validation as :meth:`load`, but
        mutates nothing (no cache, snapshots, or reload state).  Raises
        :class:`ConfigError` on rejection.
        """
        if not os.path.exists(config_path):
            raise ConfigError(f"Config file not found: {config_path}")
        raw_config = parse(config_path)
        if not isinstance(raw_config, dict):
            raise ConfigError(f"Config file {config_path} must contain a YAML mapping")
        operator = parse_text(operator_yaml)
        if operator is None:
            operator = {}
        if not isinstance(operator, dict):
            raise ConfigError("Operator profile must contain a YAML mapping")
        raw_config = _merge(raw_config, operator)
        try:
            secrets = parse_secrets_file(secrets_env)
        except ConfigError as exc:
            raise ConfigError(f"Invalid secrets candidate: {exc}") from exc
        raw_config = _substitute_recursive(
            raw_config, secrets, True, missing_env_fallback
        )
        if demo_transform is not None:
            demo_transform(cast(dict[str, Any], raw_config))
        try:
            AppConfig.model_validate(raw_config)
        except ValidationError as exc:
            raise ConfigError(
                f"Invalid candidate configuration: {_format_validation_error(exc)}"
            ) from exc

    @_store_locked
    def reload(
        self,
        *,
        config_path: str,
        operator_path: str,
        secrets_path: str,
        parse: Callable[[str], object],
        demo_transform: Callable[[dict[str, Any]], None] | None = None,
        missing_env_fallback: Callable[[str], str | None] | None = None,
    ) -> AppConfig:
        """Invalidate the cache and load a fresh validated configuration."""
        self._force_reload = True
        return self.load(
            config_path=config_path,
            operator_path=operator_path,
            secrets_path=secrets_path,
            parse=parse,
            demo_transform=demo_transform,
            missing_env_fallback=missing_env_fallback,
        )

    @_store_locked
    def version(self) -> str | None:
        """Content-derived version of the active configuration snapshot."""
        return (
            self._active_snapshot.version if self._active_snapshot is not None else None
        )

    @_store_locked
    def snapshot(self) -> ConfigSnapshot | None:
        """The active immutable configuration snapshot, if any."""
        return self._active_snapshot

    @_store_locked
    def restart_changes(self) -> list[str]:
        """Restart-sensitive sections changed since this process started.

        Repeated reloads never clear a pending restart: only constructing a
        new process-local store establishes a new startup baseline.
        """
        if self._reload_failed:
            return ["reload_failed"]
        if self._active_snapshot is None or self._startup_snapshot is None:
            return []
        return restart_changes(
            self._startup_snapshot.config,
            self._active_snapshot.config,
        )

    @_store_locked
    def restart_required(self) -> bool:
        """True when the latest reload changed a restart-sensitive section."""
        return bool(self.restart_changes())

    @_store_locked
    def status(self) -> dict[str, Any]:
        """Observability snapshot: version, restart state, rejected candidate."""
        snapshot = self._active_snapshot
        rejected = self._rejected
        return {
            "version": snapshot.version if snapshot else None,
            "applied_at": snapshot.applied_at if snapshot else None,
            "ordinal": snapshot.ordinal if snapshot else None,
            "restart_required": self.restart_required(),
            "restart_sensitive_changes": self.restart_changes(),
            "sources": (
                [
                    {
                        "path": source.path,
                        "mtime_ns": source.mtime_ns,
                        "digest": source.digest,
                    }
                    for source in snapshot.sources
                ]
                if snapshot
                else []
            ),
            "last_reload": (
                {
                    "failed": True,
                    "error": rejected.error,
                    "attempted_at": rejected.attempted_at,
                    "sources": [
                        {"path": path, "mtime_ns": mtime, "digest": digest}
                        for path, mtime, digest in rejected.sources
                    ],
                }
                if rejected is not None
                else None
            ),
        }


_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _substitute_env_vars(
    value: str,
    secrets: dict[str, str],
    secrets_authoritative: bool,
    missing_env_fallback: Callable[[str], str | None] | None,
) -> str:
    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default = match.group(2)
        # The secrets file wins over process environment, preserving the
        # historical precedence (secrets.env used to overwrite os.environ)
        # without mutating the process environment.
        if var_name in secrets:
            secret_value = secrets[var_name]
            if secret_value or default is not None:
                return secret_value
            raise ConfigError(
                f"Environment variable '{var_name}' referenced in config "
                "must not be empty"
            )
        # Managed provider secrets are authoritative once the secrets file
        # exists: an absent key means "unset" — never the process environment,
        # so a deleted credential cannot remain active through env fallback.
        if secrets_authoritative and var_name in MANAGED_SECRET_KEYS:
            if default is not None:
                return default
            raise ConfigError(
                f"Environment variable '{var_name}' referenced in config but not set"
            )
        if var_name in os.environ:
            value = os.environ[var_name]
            if value or default is not None:
                return value
            raise ConfigError(
                f"Environment variable '{var_name}' referenced in config "
                "must not be empty"
            )
        if default is not None:
            return default
        if missing_env_fallback is not None:
            fallback = missing_env_fallback(var_name)
            if fallback is not None:
                return fallback
        raise ConfigError(
            f"Environment variable '{var_name}' referenced in config but not set"
        )

    return _ENV_VAR_PATTERN.sub(_replace, value)


def _substitute_recursive(
    obj: object,
    secrets: dict[str, str],
    secrets_authoritative: bool,
    missing_env_fallback: Callable[[str], str | None] | None,
) -> object:
    if isinstance(obj, str):
        return _substitute_env_vars(
            obj, secrets, secrets_authoritative, missing_env_fallback
        )
    if isinstance(obj, dict):
        return {
            str(k): _substitute_recursive(
                v, secrets, secrets_authoritative, missing_env_fallback
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [
            _substitute_recursive(
                item, secrets, secrets_authoritative, missing_env_fallback
            )
            for item in obj
        ]
    return obj


__all__ = [
    "AnalysisAtomsConfig",
    "AnalysisRoutingConfig",
    "ApiConfig",
    "AppConfig",
    "BudgetsConfig",
    "CALENDAR_BUILTINS",
    "CalendarsConfig",
    "CftcContractConfig",
    "CftcDatasetConfig",
    "CollectorConfig",
    "CollectorFeedConfig",
    "ConfigError",
    "ConfigSnapshot",
    "ConfigSource",
    "ConfigStore",
    "DashboardConfig",
    "DashboardIndicatorConfig",
    "DataQualitySourceConfig",
    "DatabaseConfig",
    "DemoConfig",
    "EventPipelineConfig",
    "EventPipelineJobsConfig",
    "EventPipelineQueryConfig",
    "EventPipelineRetryConfig",
    "EventPipelineWorkerConfig",
    "FinraSymbolConfig",
    "FrozenModel",
    "InstrumentConfig",
    "IssuerNewsFeedConfig",
    "IssuerTranscriptConfig",
    "InstrumentCalendarConfig",
    "InvestingConfig",
    "KNOWN_COLLECTORS",
    "KNOWN_NEWS_SOURCES",
    "KNOWN_PROCESSORS",
    "InvestingWatchlistConfig",
    "InvestmentDocumentsConfig",
    "InvestmentFilingsConfig",
    "InvestmentCompanyConfig",
    "KobeissiConfig",
    "LlmConfig",
    "LlmRoleConfig",
    "LoggingConfig",
    "MANAGED_SECRET_KEYS",
    "MacroEventMappingConfig",
    "MarketStateConfig",
    "MarketStateLookback",
    "MarketStateThresholds",
    "NewsFeedConfig",
    "NonBlankText",
    "OrchestrationConfig",
    "ProcessorConfig",
    "RESTART_SENSITIVE_SECTIONS",
    "ReactionWindowsConfig",
    "ReadinessConfig",
    "RejectedConfig",
    "ResearchDiscoveryConfig",
    "ResearchGraphConfig",
    "ResearchIntelligenceConfig",
    "ResearchLifecycleThresholdsConfig",
    "ResearchLimitsConfig",
    "ResearchControlPlaneConfig",
    "ResearchStageConfig",
    "ReutersConfig",
    "SourceSeriesConfig",
    "SecForm4IssuerConfig",
    "SseConfig",
    "StaleThresholdsConfig",
    "StoryClusteringConfig",
    "StoryConfirmationConfig",
    "TimezoneConfig",
    "TimezoneZoneConfig",
    "TranscriptionSettingsConfig",
    "VenueCalendarConfig",
    "WatchlistConfig",
    "WatchlistInstrumentConfig",
    "build_snapshot",
    "config_restart_required",
    "is_placeholder_credential",
    "parse_secrets_file",
    "restart_changes",
    "restart_projection",
    "shared_state_lock",
    "validate_operator_credentials",
]
