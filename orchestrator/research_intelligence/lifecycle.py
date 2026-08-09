"""Deterministic research-case lifecycle transitions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from research_intelligence.config import ResearchSettings
from research_intelligence.contracts import CaseLifecycle


@dataclass(frozen=True, slots=True)
class CaseStats:
    evidence_count: int
    source_diversity: int
    persistence_days: int
    snapshot_count: int
    has_causal_chain: bool
    has_value_capture: bool
    has_adversarial_review: bool
    has_deliverable: bool
    last_evidence_at: datetime


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _active_state(stats: CaseStats, settings: ResearchSettings) -> CaseLifecycle:
    thresholds = settings.lifecycle_thresholds
    if (
        stats.evidence_count >= thresholds["mature_evidence"]
        and stats.snapshot_count >= thresholds["mature_snapshots"]
        and stats.has_causal_chain
        and stats.has_value_capture
        and stats.has_adversarial_review
        and stats.has_deliverable
    ):
        return CaseLifecycle.MATURE
    if (
        stats.evidence_count >= thresholds["research_ready_evidence"]
        and stats.source_diversity >= settings.minimum_source_diversity
        and stats.has_causal_chain
        and stats.has_value_capture
        and stats.has_adversarial_review
        and stats.has_deliverable
    ):
        return CaseLifecycle.RESEARCH_READY
    if (
        stats.evidence_count >= thresholds["corroborated_evidence"]
        and stats.source_diversity >= settings.minimum_source_diversity
        and stats.persistence_days >= thresholds["corroborated_days"]
    ):
        return CaseLifecycle.CORROBORATED
    if stats.evidence_count >= thresholds["forming_evidence"]:
        return CaseLifecycle.FORMING
    return CaseLifecycle.CANDIDATE


def next_lifecycle_state(
    current: CaseLifecycle | str,
    stats: CaseStats,
    settings: ResearchSettings,
    *,
    now: datetime | None = None,
) -> CaseLifecycle:
    """Return the configured lifecycle state; model assessments are not inputs."""
    state = CaseLifecycle(str(current))
    if state is CaseLifecycle.ARCHIVED:
        return state
    effective_now = _utc(now or datetime.now(UTC))
    inactive_days = max(0, (effective_now - _utc(stats.last_evidence_at)).days)
    thresholds = settings.lifecycle_thresholds
    if inactive_days >= thresholds["archive_days"]:
        return CaseLifecycle.ARCHIVED
    if inactive_days >= thresholds["weakening_days"]:
        return CaseLifecycle.WEAKENING
    return _active_state(stats, settings)


__all__ = ["CaseStats", "next_lifecycle_state"]
