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
    """Load the validated configuration.

    load_config() returns the last validated snapshot after a rejected
    reload, so routing never runs on an empty config; an initial invalid
    configuration propagates and stops work instead of silently falling
    back to defaults.
    """
    from config_loader import load_config

    return load_config()


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


def _event_ingested_at(event: Any) -> datetime | None:
    """Return the canonical acquisition time used for work coalescing."""
    value = _event_value(event, "ingested_at")
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        return None
    return value.astimezone(UTC)


def _ingestion_bucket(event: Any, minutes: int) -> str:
    """Floor canonical acquisition time for bounded downstream work."""
    bounded = max(1, min(1440, int(minutes)))
    event_at = _event_ingested_at(event) or datetime.now(UTC)
    bucket_seconds = bounded * 60
    floored = datetime.fromtimestamp(
        int(event_at.timestamp()) // bucket_seconds * bucket_seconds, tz=UTC
    )
    return floored.strftime("%Y-%m-%dT%H:%M:00")


#: Material source event types that trigger the bounded autonomous
#: thesis-fusion desk: filings, transcripts, issuer news, macro,
#: price/corporate action, options, and positioning.
THESIS_AUTONOMY_EVENT_TYPES = frozenset(
    {
        "regulatory_filing_published",
        "filing_ingested",
        "transcript_published",
        "headline_published",
        "story_updated",
        "macro_release",
        "macro_revision",
        "central_bank_communication",
        "price_tick",
        "price_bar_closed",
        "corporate_action_published",
        "option_chain_published",
        "volatility_state_changed",
        "positioning_report_published",
    }
)


def _thesis_autonomy_bucket(event: Any, config: Mapping[str, Any]) -> str | None:
    """Deterministic UTC ingestion bucket for one event, or None when disabled.

    Acquisition time, rather than the event's economic timestamp, coalesces
    historical backfills into one bounded cycle instead of replaying one model
    run per historical release or market bar.
    """
    settings = config.get("thesis_autonomy", {})
    if not isinstance(settings, Mapping) or not settings.get("enabled", False):
        return None
    try:
        debounce = max(1, min(1440, int(settings.get("event_debounce_minutes", 60))))
    except (TypeError, ValueError, OverflowError):
        debounce = 60
    return _ingestion_bucket(event, debounce)


def _thesis_event_run_limit_reached(
    session: Any,
    config: Mapping[str, Any],
    *,
    as_of: datetime,
) -> bool:
    """Return whether the UTC-day event-cycle quota has been consumed."""
    settings = config.get("thesis_autonomy", {})
    if not isinstance(settings, Mapping):
        return True
    configured = settings.get("maximum_event_runs_per_day")
    if configured is None:
        # Raw mapping fakes and legacy validated snapshots predate the quota.
        return False
    try:
        limit = max(0, min(24, int(configured)))
    except (TypeError, ValueError, OverflowError):
        return True
    if limit == 0:
        return True
    from sqlalchemy import text

    day_start = as_of.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    # Serialize the daily count+enqueue decision inside the caller's
    # transaction. Without this lock, workers handling different debounce
    # buckets could both observe one remaining slot and exceed the hard quota.
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:quota_key, 0))"),
        {"quota_key": f"thesis-autonomy:event:{day_start.date().isoformat()}"},
    )
    row = (
        session.execute(
            text(
                """SELECT COUNT(*) AS count
                   FROM analysis_jobs
                   WHERE job_type = 'thesis_autonomy_run'
                     AND dedupe_key LIKE 'thesis-autonomy:event:%'
                     AND created_at >= :day_start
                     AND created_at < :day_end"""
            ),
            {"day_start": day_start, "day_end": day_end},
        )
        .mappings()
        .first()
    )
    return int((row or {}).get("count") or 0) >= limit


def _enqueue_thesis_autonomy_job(
    session: Any,
    event: Any,
    config: Mapping[str, Any],
    *,
    event_hash: str,
    correlation_id: Any,
    source_event_id: Any,
) -> list[Any]:
    """Enqueue at most one autonomy job per deterministic debounce bucket."""
    from analysis_jobs import enqueue_job

    bucket = _thesis_autonomy_bucket(event, config)
    if bucket is None:
        return []
    as_of = _event_ingested_at(event) or datetime.now(UTC)
    if _thesis_event_run_limit_reached(session, config, as_of=as_of):
        return []
    return [
        enqueue_job(
            session,
            job_type="thesis_autonomy_run",
            dedupe_key=f"thesis-autonomy:event:{bucket}",
            input_fingerprint=f"bucket:{bucket}",
            payload={
                "source": str(_event_value(event, "source", "unknown")).strip().lower(),
                "event_type": _event_type(event),
                "event_content_hash": event_hash,
                "bucket": bucket,
                "as_of": as_of.isoformat(),
            },
            correlation_id=correlation_id,
            source_event_id=source_event_id,
            priority=700,
        )
    ]


def _match_due_playbooks(
    session: Any,
    event: Any,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Append one bounded ``context`` match per due playbook that overlaps.

    Pure overlap only (event type + normalized symbol/company entities);
    no confirmation/invalidation is ever inferred here — the falsification
    challenger decides semantics later.  Matches are appended exactly once
    per (playbook, event, kind) inside the caller's transaction.  The
    ledger is best-effort: a failure is reported bounded and never blocks
    the autonomy job enqueue or the event itself.
    """
    from thesis_playbooks import (
        event_matches_playbook,
        list_due_playbooks,
        record_event_match,
    )

    settings = config.get("thesis_autonomy", {})
    if not isinstance(settings, Mapping) or not settings.get("enabled", False):
        return {"playbooks_loaded": 0, "matches_recorded": 0, "thesis_ids": []}
    try:
        due = list_due_playbooks(session, reference=datetime.now(UTC), limit=100)
    except Exception as exc:
        return {
            "playbooks_loaded": 0,
            "matches_recorded": 0,
            "thesis_ids": [],
            "error": type(exc).__name__,
        }
    event_type = _event_type(event)
    source = str(_event_value(event, "source", "unknown")).strip().lower()
    observed_at = _event_at(event)
    recorded = 0
    thesis_ids: list[str] = []
    for row in due[:100]:
        if not isinstance(row, Mapping):
            continue
        try:
            match = event_matches_playbook(event, row, entity_keys=())
        except Exception:
            continue
        if not match.matched:
            continue
        try:
            changed = record_event_match(
                session,
                playbook_id=row.get("id"),
                market_event_id=str(_event_value(event, "event_id", "")),
                match_kind="context",
                evidence_refs=row.get("cited_evidence_refs") or (),
                observed_at=observed_at,
                assessment={"event_type": event_type, "source": source},
            )
        except Exception:
            continue
        if changed:
            recorded += 1
        thesis_id = row.get("thesis_id")
        if thesis_id and str(thesis_id) not in thesis_ids:
            thesis_ids.append(str(thesis_id))
    return {
        "playbooks_loaded": len(due),
        "matches_recorded": recorded,
        "thesis_ids": thesis_ids[:20],
    }


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

    bucket = _ingestion_bucket(event, 1)
    payload = {
        "source": source,
        "event_content_hash": event_hash,
        "event_type": _event_type(event),
        "ingestion_bucket": bucket,
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
                input_fingerprint=f"ingestion:{bucket}:source:{source}",
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
    event_type = _event_type(event)
    # The autonomous desk runs on material source event types, coalesced to
    # at most one job per deterministic UTC debounce bucket.  Enqueued
    # before the fast-path early returns so bursts of market events still
    # reach the bounded cycle.
    if event_type in THESIS_AUTONOMY_EVENT_TYPES:
        jobs.extend(
            _enqueue_thesis_autonomy_job(
                session,
                event,
                config,
                event_hash=event_hash,
                correlation_id=correlation_id,
                source_event_id=source_event_id,
            )
        )
        # Bounded context-match ledger for due playbooks, before the
        # fast-path early returns; the autonomy job enqueue above stays
        # fail-closed and transactional.
        result["playbook_matches"] = _match_due_playbooks(session, event, config)
    control_plane_settings = config.get("research_control_plane", {})
    if (
        isinstance(control_plane_settings, Mapping)
        and control_plane_settings.get("enabled") is True
        and event_type in THESIS_AUTONOMY_EVENT_TYPES
    ):
        try:
            with session.begin_nested():
                from analysis_jobs import enqueue_job
                from research_control_plane.domain import content_fingerprint
                from research_control_plane.repository import (
                    propagate_event_dependencies,
                    questions_from_event,
                    upsert_question,
                )

                event_payload = event.model_dump(mode="python")
                accepted_cutoff = _event_value(event, "ingested_at")
                drafts = questions_from_event(
                    event_payload,
                    accepted_cutoff=accepted_cutoff,
                )
                debounce_seconds = max(
                    1,
                    min(
                        int(control_plane_settings.get("event_debounce_seconds", 120)),
                        3600,
                    ),
                )
                event_bucket = int(accepted_cutoff.timestamp()) // debounce_seconds
                target_refs = sorted({draft.candidate.target_ref for draft in drafts})
                planner_dedupe_key = "research-planner:event"
                if event_type == "price_tick":
                    planner_dedupe_key += (
                        ":" + content_fingerprint({"targets": target_refs})[:24]
                    )
                planner = enqueue_job(
                    session,
                    job_type="research_planner",
                    dedupe_key=planner_dedupe_key,
                    input_fingerprint=content_fingerprint(
                        {
                            "event_bucket": event_bucket,
                            "priority_policy_version": (
                                control_plane_settings.get(
                                    "priority_policy_version", "v1"
                                )
                            ),
                        }
                    ),
                    payload={
                        "trigger_kind": "event",
                        "trigger_ref": str(source_event_id),
                        "accepted_cutoff": accepted_cutoff.isoformat(),
                    },
                    correlation_id=correlation_id,
                    source_event_id=source_event_id,
                    priority=95,
                    max_attempts=5,
                    not_before=accepted_cutoff,
                )
                # A raw tick burst may contain a new event identity each time.
                # The durable planner identity gates dependency/question writes
                # to one target set per acquisition bucket. Other event kinds
                # preserve event-by-event dirty propagation.
                should_refresh = event_type != "price_tick" or planner.inserted
                if should_refresh:
                    propagation = propagate_event_dependencies(
                        session,
                        event_payload,
                        accepted_cutoff=accepted_cutoff,
                    )
                    persisted = sum(
                        1 for draft in drafts if upsert_question(session, draft)
                    )
                else:
                    propagation = {
                        "nodes_touched": 0,
                        "edges_touched": 0,
                        "theses_affected": 0,
                    }
                    persisted = 0
                if persisted:
                    from research_control_plane.notifications import (
                        publish_control_plane_invalidations,
                    )

                    publish_control_plane_invalidations(
                        session,
                        {
                            "research_questions",
                            "research_control_plane",
                            "system_topology",
                        },
                    )
                if planner.job is not None:
                    jobs.append(planner.job)
                control_plane_result: dict[str, Any] = {
                    "status": "accepted",
                    "questions_created_or_refreshed": persisted,
                    "planner_job_created": planner.inserted,
                    "planner_job_coalesced": not planner.inserted,
                    **propagation,
                }
            result["research_control_plane"] = control_plane_result
        except Exception:
            result["research_control_plane"] = {
                "status": "failed",
                "questions_created_or_refreshed": 0,
                "planner_job_created": False,
                "planner_job_coalesced": False,
                "nodes_touched": 0,
                "edges_touched": 0,
                "theses_affected": 0,
            }
    if not phase5_enabled and not story_enabled:
        return result

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
    if event_type == "price_tick" and phase5_enabled:
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
