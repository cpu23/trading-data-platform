"""Topic routing for durable market-event processing."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from .contracts import MarketEvent


def _event_value(event: Any, key: str, default: Any = None) -> Any:
    if isinstance(event, Mapping):
        return event.get(key, default)
    return getattr(event, key, default)


def _config() -> Mapping[str, Any]:
    try:
        from config_loader import load_config

        value = load_config()
    except Exception:
        return {}
    return value if isinstance(value, Mapping) else {}


def _event_type(event: Any) -> str:
    value = _event_value(event, "event_type", "")
    return str(getattr(value, "value", value)).strip().lower()


def _event_at(event: Any) -> datetime | None:
    payload = _event_value(event, "payload", {})
    if isinstance(payload, Mapping):
        for key in ("released_at", "revision_at", "published_at"):
            value = payload.get(key)
            if isinstance(value, str):
                try:
                    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    value = None
            if isinstance(value, datetime):
                if value.tzinfo is None or value.utcoffset() is None:
                    continue
                return value.astimezone(UTC)
    for key in ("effective_at", "observed_at"):
        value = _event_value(event, key)
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                continue
            return value.astimezone(UTC)
    return None


def _enqueue_section_jobs(
    session: Any,
    event: Any,
    *,
    source: str,
    event_hash: str,
    correlation_id: Any,
    source_event_id: Any,
) -> list[Any]:
    from analysis_jobs import enqueue_job

    payload = {
        "source": source,
        "event_content_hash": event_hash,
        "event_type": _event_type(event),
    }
    jobs = []
    for job_type, section in (
        ("publish_source_health_snapshot", "source_health"),
        ("publish_watchlist_snapshot", "watchlist"),
    ):
        jobs.append(
            enqueue_job(
                session,
                job_type=job_type,
                dedupe_key=f"{section}:global",
                input_fingerprint=event_hash,
                payload=payload,
                correlation_id=correlation_id,
                source_event_id=source_event_id,
            )
        )
    return jobs


def _enqueue_macro_jobs(
    session: Any,
    event: Any,
    config: Mapping[str, Any],
    *,
    event_hash: str,
    correlation_id: Any,
    source_event_id: Any,
    material: bool,
) -> list[Any]:
    from analysis_jobs import enqueue_job
    from reaction_windows import horizon_target

    event_id = str(source_event_id)
    jobs = [
        enqueue_job(
            session,
            job_type="publish_macro_release_snapshot",
            dedupe_key="macro_releases:global",
            input_fingerprint=event_hash,
            payload={"event_id": event_id, "stage": "t0"},
            correlation_id=correlation_id,
            source_event_id=source_event_id,
            priority=1000,
        )
    ]
    event_at = _event_at(event)
    settings = config.get("reaction_windows", {})
    if not material or event_at is None or not isinstance(settings, Mapping):
        return jobs
    now = datetime.now(UTC)
    try:
        max_age = max(1, min(1440, int(settings.get("max_event_age_minutes", 360))))
    except (TypeError, ValueError, OverflowError):
        max_age = 360
    if event_at < now - timedelta(minutes=max_age):
        return jobs
    stages = (
        ("t1", "developing", event_at + timedelta(minutes=1)),
        ("t5", "reaction", event_at + timedelta(minutes=5)),
        ("t15", "reaction", event_at + timedelta(minutes=15)),
        ("t30", "reaction", event_at + timedelta(minutes=30)),
        ("t60", "reaction", event_at + timedelta(minutes=60)),
        ("eos", "final", horizon_target(event_at, "end_of_session", config)),
    )
    for name, stage, not_before in stages:
        jobs.append(
            enqueue_job(
                session,
                job_type="update_macro_release_reactions",
                dedupe_key=f"macro_release:{event_id}:{name}",
                input_fingerprint=f"{event_hash}:{name}",
                payload={"event_id": event_id, "stage": stage, "window": name},
                correlation_id=correlation_id,
                source_event_id=source_event_id,
                priority=900 if name == "t1" else 500,
                not_before=max(now, not_before),
            )
        )
    return jobs


def _enqueue_atom_jobs(
    session: Any,
    event: Any,
    *,
    event_hash: str,
    correlation_id: Any,
    source_event_id: Any,
) -> list[Any]:
    from analysis_jobs import enqueue_job

    return [
        enqueue_job(
            session,
            job_type="publish_analysis_atoms_snapshot",
            dedupe_key="analysis_atoms:global",
            input_fingerprint=f"{event_hash}:atoms",
            payload={"event_id": str(source_event_id)},
            correlation_id=correlation_id,
            source_event_id=source_event_id,
            priority=500,
        )
    ]


def _enqueue_story_jobs(
    session: Any,
    event: Any,
    assignment: Any,
    config: Mapping[str, Any],
    *,
    event_hash: str,
    correlation_id: Any,
    source_event_id: Any,
) -> list[Any]:
    from analysis_jobs import enqueue_job
    from story_confirmation import session_target

    cluster_id = str(assignment.cluster_id)
    jobs = [
        enqueue_job(
            session,
            job_type="publish_story_clusters_snapshot",
            dedupe_key="news_clusters:global",
            input_fingerprint=f"{event_hash}:{assignment.version}",
            payload={"cluster_id": cluster_id, "event_id": str(source_event_id)},
            correlation_id=correlation_id,
            source_event_id=source_event_id,
            priority=900,
        )
    ]
    if not assignment.materially_changed:
        return jobs
    event_at = _event_at(event)
    markets = _event_value(event, "markets", []) or []
    if event_at is None or not markets:
        return jobs
    settings = config.get("story_confirmation", {})
    settings = settings if isinstance(settings, Mapping) else {}
    now = datetime.now(UTC)
    stages = (
        ("t5", event_at + timedelta(minutes=5)),
        ("t30", event_at + timedelta(minutes=30)),
        ("session", session_target(event_at, settings)),
    )
    for horizon, not_before in stages:
        jobs.append(
            enqueue_job(
                session,
                job_type="update_story_market_confirmation",
                dedupe_key=f"story_confirmation:{cluster_id}:{source_event_id}:{horizon}",
                input_fingerprint=f"{event_hash}:{horizon}",
                payload={
                    "cluster_id": cluster_id,
                    "event_id": str(source_event_id),
                    "horizon": horizon,
                },
                correlation_id=correlation_id,
                source_event_id=source_event_id,
                priority=600 if horizon == "t5" else 400,
                not_before=max(now, not_before),
            )
        )
    return jobs


def initial_handler(session: Any, event: MarketEvent) -> dict[str, Any]:
    """Persist deterministic fast-path state and enqueue bounded publication work."""
    from .canonicalize import content_hash
    from .freshness import record_event_observation

    freshness = record_event_observation(session, event)
    source = str(_event_value(event, "source", "unknown")).strip().lower()
    event_hash = str(_event_value(event, "content_hash", ""))
    if len(event_hash) != 64:
        event_hash = content_hash(
            {
                "event_id": str(_event_value(event, "event_id", "")),
                "source": source,
                "event_type": _event_type(event),
            }
        )
    correlation_id = _event_value(event, "correlation_id")
    source_event_id = _event_value(event, "event_id")
    jobs = _enqueue_section_jobs(
        session,
        event,
        source=source,
        event_hash=event_hash,
        correlation_id=correlation_id,
        source_event_id=source_event_id,
    )
    config = _config()
    phase5_enabled = (
        isinstance(config.get("market_state"), Mapping)
        and config["market_state"].get("enabled") is True
    )
    story_settings = config.get("story_clustering", {})
    story_enabled = (
        isinstance(story_settings, Mapping) and story_settings.get("enabled") is True
    )
    result: dict[str, Any] = {"freshness": freshness, "jobs": jobs}
    if not phase5_enabled and not story_enabled:
        return result

    event_type = _event_type(event)
    from materiality import assess_event_materiality

    decision = assess_event_materiality(session, event, config, job_type="event_atom")
    result["materiality"] = decision
    atom_settings = config.get("analysis_atoms", {})
    atom_enabled = (
        isinstance(atom_settings, Mapping) and atom_settings.get("enabled") is True
    )
    if atom_enabled and decision.should_route:
        jobs.extend(
            _enqueue_atom_jobs(
                session,
                event,
                event_hash=event_hash,
                correlation_id=correlation_id,
                source_event_id=source_event_id,
            )
        )
    if event_type == "headline_published" and story_enabled:
        from stories import cluster_news_story

        assignment = cluster_news_story(session, event, config)
        story_decision = assess_event_materiality(
            session, event, config, job_type="story_summary"
        )
        jobs.extend(
            _enqueue_story_jobs(
                session,
                event,
                assignment,
                config,
                event_hash=event_hash,
                correlation_id=correlation_id,
                source_event_id=source_event_id,
            )
        )
        result.update(
            story=assignment,
            story_materiality=story_decision,
        )
        return result
    if event_type == "price_tick":
        from market_state import update_price_features

        result["market_state"] = update_price_features(session, event, config)
    elif event_type in {"macro_release", "macro_revision"}:
        from macro_releases import upsert_macro_release_card
        from reaction_windows import initialize_reaction_windows

        card = upsert_macro_release_card(session, event, config=config)
        reaction_decision = assess_event_materiality(
            session, event, config, job_type="reaction_window"
        )
        reactions = (
            initialize_reaction_windows(session, event, config)
            if reaction_decision.should_route
            else {
                "event_id": str(source_event_id),
                "mapped_instruments": 0,
                "created": 0,
                "suppressed": True,
            }
        )
        jobs.extend(
            _enqueue_macro_jobs(
                session,
                event,
                config,
                event_hash=event_hash,
                correlation_id=correlation_id,
                source_event_id=source_event_id,
                material=reaction_decision.should_route,
            )
        )
        result.update(
            macro_release=card,
            reaction_materiality=reaction_decision,
            reactions=reactions,
        )
    return result


HANDLERS: dict[str, Callable[[Any, MarketEvent], Any]] = {
    "market_event": initial_handler
}


def handler_for(topic: str) -> Callable[[Any, MarketEvent], Any]:
    try:
        return HANDLERS[topic]
    except KeyError as exc:
        raise ValueError("unsupported event topic") from exc


def route_event(
    session: Any, event: MarketEvent, *, topic: str = "market_event"
) -> Any:
    return handler_for(topic)(session, event)


# Explicit name useful to workers and integrations.
process_market_event = initial_handler

__all__ = [
    "HANDLERS",
    "handler_for",
    "initial_handler",
    "process_market_event",
    "route_event",
]
