"""Bounded, fail-soft repair for the event-analysis pipeline."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from db import get_session


def _settings(config: Any) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    event_pipeline = config.get("event_pipeline", {})
    if not isinstance(event_pipeline, Mapping):
        return {}
    jobs = event_pipeline.get("jobs", {})
    return jobs if isinstance(jobs, Mapping) else {}


def _limit(settings: Mapping[str, Any], key: str, default: int = 100) -> int:
    query = settings.get("query", {})
    query = query if isinstance(query, Mapping) else {}
    value = query.get(key, settings.get(key, default))
    try:
        return max(1, min(500, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _count(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, Mapping):
        for key in (
            "repaired",
            "reconciled",
            "completed",
            "updated",
            "expired",
            "count",
            "total",
        ):
            candidate = value.get(key)
            if isinstance(candidate, int):
                return candidate
    try:
        return len(value)
    except (TypeError, ValueError):
        return 0


def reconcile_event_pipeline(config: Any) -> dict[str, Any]:
    """Repair only bounded expired jobs and safe snapshot metadata.

    Each repair class is isolated: a transient job-table failure must not prevent
    snapshot metadata repair, and operational errors are represented by a generic
    type name rather than exception text or database details.
    """
    settings = _settings(config)
    result: dict[str, Any] = {
        "jobs_reconciled": 0,
        "snapshots_reconciled": 0,
        "freshness_reclassified": 0,
        "ui_events_expired": 0,
        "reaction_windows_backfilled": 0,
        "story_confirmations_backfilled": 0,
        "atoms_expired": 0,
        "errors": [],
    }
    try:
        from analysis_jobs import reconcile_jobs

        with get_session(config) as session:
            repaired = reconcile_jobs(
                session,
                limit=_limit(settings, "max_reconcile_jobs", 100),
            )
            result["jobs_reconciled"] = _count(repaired)
    except Exception as exc:
        result["errors"].append("analysis_jobs:" + type(exc).__name__)
    try:
        from section_snapshots import reconcile_snapshots

        with get_session(config) as session:
            repaired = reconcile_snapshots(
                session,
                limit=_limit(settings, "max_reconcile_snapshots", 100),
            )
            result["snapshots_reconciled"] = _count(repaired)
    except Exception as exc:
        result["errors"].append("section_snapshots:" + type(exc).__name__)
    try:
        from reaction_windows import backfill_reaction_windows

        reaction_settings = (
            config.get("reaction_windows", {}) if isinstance(config, Mapping) else {}
        )
        reaction_settings = (
            reaction_settings if isinstance(reaction_settings, Mapping) else {}
        )
        with get_session(config) as session:
            repaired = backfill_reaction_windows(
                session,
                config,
                limit=_limit(
                    reaction_settings,
                    "backfill_limit",
                    _limit(settings, "max_reconcile_jobs", 100),
                ),
            )
            result["reaction_windows_backfilled"] = _count(repaired)
    except Exception as exc:
        result["errors"].append("reaction_windows:" + type(exc).__name__)
    try:
        from story_confirmation import backfill_story_confirmations

        story_settings = (
            config.get("story_confirmation", {}) if isinstance(config, Mapping) else {}
        )
        story_settings = story_settings if isinstance(story_settings, Mapping) else {}
        with get_session(config) as session:
            repaired = backfill_story_confirmations(
                session,
                config,
                limit=_limit(
                    story_settings,
                    "backfill_limit",
                    _limit(settings, "max_reconcile_jobs", 100),
                ),
            )
            result["story_confirmations_backfilled"] = _count(repaired)
    except Exception as exc:
        result["errors"].append("story_confirmations:" + type(exc).__name__)
    try:
        from ui_events import delete_expired_ui_events

        with get_session(config) as session:
            expired = delete_expired_ui_events(
                session,
                limit=_limit(settings, "max_cleanup_ui_events", 100),
            )
            result["ui_events_expired"] = _count(expired)
    except Exception as exc:
        result["errors"].append("ui_events:" + type(exc).__name__)
    try:
        from atoms import expire_atoms

        with get_session(config) as session:
            expired = expire_atoms(session, config)
            result["atoms_expired"] = _count(expired)
    except Exception as exc:
        result["errors"].append("atoms:" + type(exc).__name__)
    try:
        from events.freshness import refresh_freshness_states

        pipeline = (
            config.get("event_pipeline", {}) if isinstance(config, Mapping) else {}
        )
        source_configs = (
            config.get("collectors", {}) if isinstance(config, Mapping) else {}
        )
        grace = (
            pipeline.get("freshness_grace_seconds", 300)
            if isinstance(pipeline, Mapping)
            else 300
        )
        with get_session(config) as session:
            refreshed = refresh_freshness_states(
                session,
                source_configs if isinstance(source_configs, Mapping) else {},
                default_grace_seconds=float(grace),
                limit=_limit(
                    settings,
                    "max_reconcile_freshness",
                    _limit(settings, "max_reconcile_jobs", 100),
                ),
            )
            result["freshness_reclassified"] = _count(
                refreshed.get("changed", 0)
                if isinstance(refreshed, Mapping)
                else refreshed
            )
    except Exception as exc:
        result["errors"].append("freshness:" + type(exc).__name__)
    result["error_count"] = len(result["errors"])
    return result


__all__ = ["reconcile_event_pipeline"]
