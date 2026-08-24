"""Bounded, fail-soft live system topology assembled from operational evidence."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import config as app_config
from contracts import SystemTopologyEdge, SystemTopologyNode, SystemTopologyResponse
from db import query_one

_MAX_COUNT = 1_000_000


def _count(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, min(int(value), _MAX_COUNT))
    except (TypeError, ValueError, OverflowError):
        return None


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, str):
        try:
            return _time(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _work_status(
    count: int | None,
    last: datetime | None,
    now: datetime,
    *,
    active_state: str,
    idle_state: str,
) -> tuple[str, str, str | None]:
    if count is None:
        return "unknown", "state unavailable", "operational count unavailable"
    if count > 0:
        return "active", active_state, None
    if last is not None and now - last > timedelta(hours=24):
        return (
            "stale",
            idle_state,
            "last persisted activity is older than 24 hours",
        )
    return "idle", idle_state, None


def _inventory_status(
    count: int | None, *, available_state: str
) -> tuple[str, str, str | None]:
    if count is None:
        return "unknown", "inventory unavailable", "bounded inventory query unavailable"
    if count > 0:
        return "healthy", available_state, None
    return "idle", "no active inventory", None


def _node(
    *,
    node_id: str,
    label: str,
    group: str,
    kind: str,
    status: str,
    activity_state: str,
    safe_detail: str,
    count: int | None = None,
    last: datetime | None = None,
    reason: str | None = None,
    navigation: str | None = None,
    inferred: bool = False,
) -> SystemTopologyNode:
    return SystemTopologyNode(
        id=node_id,
        label=label,
        group=group,
        kind=kind,
        status=status,
        activity_state=activity_state,
        bounded_count=count,
        last_activity_at=last,
        staleness_reason=reason,
        safe_detail=safe_detail,
        navigation_target=navigation,
        inferred_activity=inferred,
    )


def _edge(
    source: str,
    target: str,
    kind: str,
    *,
    detail: str,
) -> SystemTopologyEdge:
    """Describe a structural dependency without inventing relationship activity."""
    return SystemTopologyEdge(
        source=source,
        target=target,
        kind=kind,
        status="unknown",
        recent_activity_count=None,
        last_activity_at=None,
        safe_detail=detail,
    )


def unavailable_system_topology(
    component: str = "system_topology",
) -> SystemTopologyResponse:
    """Return one contract-valid, redacted unavailable topology."""
    return SystemTopologyResponse(
        generated_at=datetime.now(UTC),
        status="unavailable",
        nodes=[],
        edges=[],
        unavailable_components=[component],
        summary="System topology is temporarily unavailable.",
    )


def build_system_topology() -> SystemTopologyResponse:
    """Query bounded aggregates only; one unavailable subsystem cannot hide others."""
    now = datetime.now(UTC)
    config = app_config.load_config()
    unavailable: list[str] = []
    rows: dict[str, Mapping[str, Any]] = {}
    queries = {
        "research": """
            SELECT
                (SELECT COUNT(*) FROM research_questions
                 WHERE status IN ('pending', 'planned')) AS question_backlog,
                (SELECT MAX(updated_at) FROM research_questions) AS question_last_activity,
                (SELECT COUNT(*) FROM research_work_orders
                 WHERE status IN ('queued', 'leased', 'running', 'failed_retryable')) AS active_work_orders,
                (SELECT MAX(updated_at) FROM research_work_orders) AS work_order_last_activity,
                (SELECT COUNT(*) FROM research_effects
                 WHERE created_at >= NOW() - INTERVAL '24 hours') AS recent_effects,
                (SELECT MAX(created_at) FROM research_effects) AS effect_last_activity,
                (SELECT COUNT(*) FROM research_skill_versions
                 WHERE promotion_status = 'active') AS active_skills,
                (SELECT COUNT(*) FROM research_source_capabilities
                 WHERE runtime_available) AS available_source_capabilities,
                (SELECT MAX(checked_at) FROM research_source_capabilities) AS capability_last_activity
        """,
        "runtime": """
            SELECT
                (SELECT COUNT(*) FROM analysis_jobs
                 WHERE state IN ('queued', 'leased', 'running', 'failed_retryable')) AS active_jobs,
                (SELECT MAX(COALESCE(completed_at, started_at, created_at))
                 FROM analysis_jobs) AS job_last_activity,
                (SELECT COUNT(*) FROM analysis_jobs
                 WHERE job_type = 'research_planner'
                   AND state IN ('queued', 'leased', 'running', 'failed_retryable')) AS active_planner_jobs,
                (SELECT MAX(COALESCE(completed_at, started_at, created_at))
                 FROM analysis_jobs WHERE job_type = 'research_planner') AS planner_last_activity,
                (SELECT COUNT(*) FROM collection_log
                 WHERE status = 'running'
                   AND started_at >= NOW() - INTERVAL '7 days') AS running_collections,
                (SELECT MAX(COALESCE(completed_at, started_at)) FROM collection_log
                 WHERE started_at >= NOW() - INTERVAL '7 days') AS collection_last_activity,
                (SELECT COUNT(*) FROM role_heartbeats
                 WHERE role = 'scheduler' AND status = 'running'
                   AND last_heartbeat_at >= NOW() - INTERVAL '5 minutes') AS scheduler_live,
                (SELECT MAX(last_heartbeat_at) FROM role_heartbeats
                 WHERE role = 'scheduler') AS scheduler_last_activity,
                (SELECT COUNT(*) FROM role_heartbeats
                 WHERE role IN ('analysis-worker', 'worker') AND status = 'running'
                   AND last_heartbeat_at >= NOW() - INTERVAL '5 minutes') AS worker_live,
                (SELECT MAX(last_heartbeat_at) FROM role_heartbeats
                 WHERE role IN ('analysis-worker', 'worker')) AS worker_last_activity,
                (SELECT COUNT(*) FROM role_heartbeats
                 WHERE role IN ('quote-stream', 'quote_stream') AND status = 'running'
                   AND last_heartbeat_at >= NOW() - INTERVAL '5 minutes') AS quote_live,
                (SELECT MAX(last_heartbeat_at) FROM role_heartbeats
                 WHERE role IN ('quote-stream', 'quote_stream')) AS quote_last_activity
        """,
        "events": """
            SELECT
                (SELECT COUNT(*) FROM market_events
                 WHERE ingested_at >= NOW() - INTERVAL '24 hours') AS events_24h,
                (SELECT MAX(ingested_at) FROM market_events) AS event_last_activity,
                (SELECT COUNT(*) FROM event_outbox
                 WHERE completed_at IS NULL AND failed_at IS NULL) AS pending_outbox,
                (SELECT MAX(COALESCE(completed_at, claimed_at, created_at))
                 FROM event_outbox) AS outbox_last_activity,
                (SELECT COUNT(*) FROM ui_events
                 WHERE created_at >= NOW() - INTERVAL '5 minutes') AS recent_ui_invalidations,
                (SELECT MAX(created_at) FROM ui_events) AS ui_invalidation_last_activity
        """,
    }
    for component, sql in queries.items():
        try:
            rows[component] = query_one(sql, config=config) or {}
        except Exception:
            unavailable.append(component)
            rows[component] = {}

    nodes: list[SystemTopologyNode] = []

    def operational(
        component: str,
        *,
        node_id: str,
        label: str,
        group: str,
        kind: str,
        count_key: str,
        last_key: str,
        active_state: str,
        idle_state: str,
        detail: str,
        navigation: str | None = None,
        inventory: bool = False,
        inferred: bool = False,
    ) -> None:
        row = rows[component]
        count = _count(row.get(count_key)) if component not in unavailable else None
        last = _time(row.get(last_key))
        if inventory:
            status, activity, reason = _inventory_status(
                count, available_state=active_state
            )
        else:
            status, activity, reason = _work_status(
                count,
                last,
                now,
                active_state=active_state,
                idle_state=idle_state,
            )
        if component in unavailable:
            status = "unavailable"
            activity = "query unavailable"
            reason = "bounded operational query failed"
        nodes.append(
            _node(
                node_id=node_id,
                label=label,
                group=group,
                kind=kind,
                status=status,
                activity_state=activity,
                safe_detail=detail,
                count=count,
                last=last,
                reason=reason,
                navigation=navigation,
                inferred=inferred,
            )
        )

    operational(
        "runtime",
        node_id="collectors",
        label="Collectors",
        group="Sources",
        kind="collector",
        count_key="running_collections",
        last_key="collection_last_activity",
        active_state="running collections",
        idle_state="no running collections",
        detail="Configured source collectors and collection runs from the last seven days.",
        navigation="/operations",
    )
    nodes.append(
        _node(
            node_id="external-sources",
            label="External data sources",
            group="Sources",
            kind="external",
            status="unknown",
            activity_state="provider state not measured",
            safe_detail="Configured upstream providers; this topology does not probe provider availability.",
            inferred=True,
        )
    )
    nodes.append(
        _node(
            node_id="source-adapters",
            label="Source adapters",
            group="Sources",
            kind="adapter",
            status="unknown",
            activity_state="runtime use not measured",
            safe_detail="Allowlisted source adapters used by collectors; inventory does not prove activity.",
            inferred=True,
        )
    )
    operational(
        "runtime",
        node_id="quote-stream",
        label="Quote stream",
        group="Sources",
        kind="stream",
        count_key="quote_live",
        last_key="quote_last_activity",
        active_state="running heartbeat observed",
        idle_state="no running heartbeat",
        detail="Running state requires a fresh persisted quote-stream heartbeat.",
        navigation="/markets",
    )
    operational(
        "events",
        node_id="market-events",
        label="Market and source events",
        group="Events",
        kind="ledger",
        count_key="events_24h",
        last_key="event_last_activity",
        active_state="events persisted in last 24 hours",
        idle_state="no events persisted in last 24 hours",
        detail="Immutable canonical market-event ledger; count covers the last 24 hours.",
    )
    operational(
        "research",
        node_id="source-capabilities",
        label="Source capability registry",
        group="Events",
        kind="registry",
        count_key="available_source_capabilities",
        last_key="capability_last_activity",
        active_state="runtime-available sources registered",
        idle_state="no runtime-available sources",
        detail="Typed point-in-time, coverage, availability and cost capabilities for research sources.",
        navigation="/research",
        inventory=True,
    )
    operational(
        "runtime",
        node_id="scheduler",
        label="Scheduler",
        group="Control plane",
        kind="runtime",
        count_key="scheduler_live",
        last_key="scheduler_last_activity",
        active_state="running heartbeat observed",
        idle_state="no running heartbeat",
        detail="Active state requires a fresh persisted scheduler heartbeat with running status.",
        navigation="/operations",
    )
    operational(
        "runtime",
        node_id="research-planner",
        label="Research planner",
        group="Control plane",
        kind="planner",
        count_key="active_planner_jobs",
        last_key="planner_last_activity",
        active_state="durable planner work active",
        idle_state="no durable planner work active",
        detail="Deterministic value-of-information planner jobs under cost and runtime budgets.",
        navigation="/research",
    )
    operational(
        "research",
        node_id="research-questions",
        label="Research-question queue",
        group="Control plane",
        kind="queue",
        count_key="question_backlog",
        last_key="question_last_activity",
        active_state="questions pending or planned",
        idle_state="no questions pending or planned",
        detail="Durable atomic questions pending or planned.",
        navigation="/research",
    )
    operational(
        "runtime",
        node_id="analysis-queue",
        label="Durable analysis queue",
        group="Control plane",
        kind="queue",
        count_key="active_jobs",
        last_key="job_last_activity",
        active_state="durable jobs active",
        idle_state="no durable jobs active",
        detail="PostgreSQL-backed analysis jobs with leases, retries and recovery.",
        navigation="/operations",
    )
    operational(
        "runtime",
        node_id="worker",
        label="Worker",
        group="Control plane",
        kind="runtime",
        count_key="worker_live",
        last_key="worker_last_activity",
        active_state="running heartbeat observed",
        idle_state="no running heartbeat",
        detail="Active state requires a fresh running worker heartbeat; queued work remains recoverable.",
        navigation="/operations",
    )
    for node_id, label, activity, detail in (
        (
            "thesis-tournament",
            "Thesis tournament",
            "execution not measured here",
            "Candidate generation, deterministic tournament and accepted-reference fusion.",
        ),
        (
            "challenge",
            "Challenge and falsification",
            "execution not measured here",
            "Independent deterministic contradiction and invalidation checks.",
        ),
        (
            "scoring-forecasts",
            "Scoring and forecasts",
            "execution not measured here",
            "Deterministic thesis scoring, forecast resolution and outcome attribution.",
        ),
    ):
        nodes.append(
            _node(
                node_id=node_id,
                label=label,
                group="Research",
                kind="domain",
                status="unknown",
                activity_state=activity,
                safe_detail=detail,
                navigation="/research",
                inferred=True,
            )
        )
    operational(
        "research",
        node_id="research-skills",
        label="Versioned research skills",
        group="Research",
        kind="skill-registry",
        count_key="active_skills",
        last_key="question_last_activity",
        active_state="promoted versions registered",
        idle_state="no promoted versions",
        detail="Immutable promoted skill versions and exact executor contracts.",
        navigation="/research",
        inventory=True,
    )
    operational(
        "research",
        node_id="research-effects",
        label="Effects and feedback",
        group="Research",
        kind="metrics",
        count_key="recent_effects",
        last_key="effect_last_activity",
        active_state="effects persisted in last 24 hours",
        idle_state="no effects persisted in last 24 hours",
        detail="Append-only material effects, justified no-ops and feedback scorecards.",
        navigation="/research",
    )
    database_status = (
        "unavailable"
        if len(unavailable) == len(queries)
        else "degraded"
        if unavailable
        else "healthy"
    )
    nodes.append(
        _node(
            node_id="postgresql",
            label="PostgreSQL and TimescaleDB",
            group="Storage",
            kind="database",
            status=database_status,
            activity_state=(
                "topology queries succeeded"
                if not unavailable
                else "some topology queries failed"
            ),
            safe_detail="Durable coordination, domain state, time series and transactional locks.",
            last=now if not unavailable else None,
            reason=(
                "one or more bounded aggregate queries failed" if unavailable else None
            ),
        )
    )
    operational(
        "events",
        node_id="outbox",
        label="Transactional outbox",
        group="Storage",
        kind="queue",
        count_key="pending_outbox",
        last_key="outbox_last_activity",
        active_state="pending delivery work",
        idle_state="no pending delivery work",
        detail="Leased event delivery with retry and expiry recovery.",
    )
    nodes.append(
        _node(
            node_id="migrations",
            label="Migration and bootstrap",
            group="Storage",
            kind="bootstrap",
            status="unknown",
            activity_state="checksum state not queried",
            safe_detail="Checksum-verified migration state is available to bootstrap checks, not inferred here.",
            inferred=True,
        )
    )
    nodes.append(
        _node(
            node_id="api",
            label="FastAPI",
            group="Delivery",
            kind="service",
            status="unknown",
            activity_state="service health not probed",
            safe_detail="Authenticated bounded JSON, HTML and partial response surface.",
            navigation="/operations",
            inferred=True,
        )
    )
    pipeline = config.get("event_pipeline", {})
    pipeline = pipeline if isinstance(pipeline, Mapping) else {}
    sse = pipeline.get("sse", {})
    sse = sse if isinstance(sse, Mapping) else {}
    if sse.get("enabled") is not True:
        nodes.append(
            _node(
                node_id="sse-htmx",
                label="SSE and HTMX delivery",
                group="Delivery",
                kind="delivery",
                status="disabled",
                activity_state="SSE disabled by configuration",
                safe_detail="HTMX remains available; the SSE stream is disabled by validated configuration.",
                navigation="/operations",
            )
        )
    elif "events" in unavailable:
        nodes.append(
            _node(
                node_id="sse-htmx",
                label="SSE and HTMX delivery",
                group="Delivery",
                kind="delivery",
                status="unavailable",
                activity_state="invalidation query unavailable",
                safe_detail="SSE is configured, but persisted invalidation state could not be read.",
                reason="bounded operational query failed",
                navigation="/operations",
            )
        )
    else:
        ui_row = rows["events"]
        nodes.append(
            _node(
                node_id="sse-htmx",
                label="SSE and HTMX delivery",
                group="Delivery",
                kind="delivery",
                status="unknown",
                activity_state="client delivery not persisted",
                safe_detail="SSE is enabled and persisted UI invalidations are counted; connected-client delivery is not inferred.",
                count=_count(ui_row.get("recent_ui_invalidations")),
                last=_time(ui_row.get("ui_invalidation_last_activity")),
                reason="connected-client delivery state is process-local",
                navigation="/operations",
                inferred=True,
            )
        )
    nodes.append(
        _node(
            node_id="workspaces",
            label="Operations and research workspaces",
            group="Delivery",
            kind="ui",
            status="unknown",
            activity_state="browser state not measured",
            safe_detail="Lean semantic workspaces with bounded live sections.",
            navigation="/operations",
            inferred=True,
        )
    )

    by_id = {node.id: node for node in nodes}
    edge_specs = [
        ("external-sources", "source-adapters", "feeds"),
        ("source-adapters", "collectors", "collects"),
        ("collectors", "market-events", "publishes"),
        ("quote-stream", "market-events", "publishes"),
        ("market-events", "outbox", "enqueues"),
        ("market-events", "source-capabilities", "updates"),
        ("market-events", "research-questions", "dirties"),
        ("scheduler", "research-planner", "triggers"),
        ("research-questions", "research-planner", "ranked-by"),
        ("research-planner", "analysis-queue", "dispatches"),
        ("analysis-queue", "worker", "leases-to"),
        ("worker", "research-skills", "executes"),
        ("research-skills", "thesis-tournament", "updates"),
        ("research-skills", "challenge", "challenges"),
        ("research-skills", "scoring-forecasts", "resolves"),
        ("research-skills", "research-effects", "records"),
        ("research-effects", "research-planner", "feeds-back"),
        ("postgresql", "analysis-queue", "coordinates"),
        ("postgresql", "research-questions", "persists"),
        ("outbox", "sse-htmx", "invalidates"),
        ("api", "workspaces", "serves"),
        ("sse-htmx", "workspaces", "refreshes"),
        ("migrations", "postgresql", "bootstraps"),
    ]
    edges = [
        _edge(
            source,
            target,
            kind,
            detail=(
                f"{by_id[source].label} {kind} {by_id[target].label}. "
                "Structural dependency; relationship activity is not persisted."
            ),
        )
        for source, target, kind in edge_specs
    ]
    status = "partial" if unavailable else "available"
    summary = (
        f"Live bounded topology with {len(nodes)} nodes and {len(edges)} edges."
        if not unavailable
        else f"Partial bounded topology; unavailable aggregates: {', '.join(unavailable)}."
    )
    return SystemTopologyResponse(
        generated_at=now,
        status=status,
        nodes=nodes,
        edges=edges,
        unavailable_components=unavailable,
        summary=summary,
    )


__all__ = ["build_system_topology", "unavailable_system_topology"]
