from datetime import datetime

from api_db import query_many
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from topology import build_system_topology, unavailable_system_topology

from config import load_config
from routes.json.atoms import load_atom_context
from routes.json.settings import timezone_context
from routes.json.system import get_system_health
from routes.views.news import load_news_context

router = APIRouter()
OVERVIEW_LIMIT = 10


async def _source_state(request: Request) -> dict:
    unavailable = {"available": False, "message": "Unavailable"}
    try:
        health = await get_system_health(request)
        if isinstance(health, JSONResponse):
            return unavailable
        return {
            "available": True,
            "readiness": health.get("readiness", "unknown"),
            "components": health.get("components", [])[:OVERVIEW_LIMIT],
        }
    except Exception:
        return unavailable


def _feed_snapshot(config: dict) -> dict:
    context = load_news_context(config)
    if context["status"] == "not_published":
        return {"status": "not_published", "item_count": 0, "published_at": None}
    if context["status"] != "published":
        raise ValueError("invalid feed")
    return {
        "status": "published",
        "item_count": len(context["items"]),
        "published_at": context["generated_at"],
    }


def _local_time(value, zone):
    if not value:
        return None
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
        return parsed.astimezone(zone).strftime("%d %b %Y %H:%M %Z")
    except (TypeError, ValueError):
        return None


def _duration_ms(row: dict):
    value = row.get("duration_ms")
    if value is not None:
        return value
    try:
        start = (
            row["started_at"]
            if isinstance(row["started_at"], datetime)
            else datetime.fromisoformat(str(row["started_at"]).replace("Z", "+00:00"))
        )
        end = (
            row["completed_at"]
            if isinstance(row["completed_at"], datetime)
            else datetime.fromisoformat(str(row["completed_at"]).replace("Z", "+00:00"))
        )
        return max(0, int((end - start).total_seconds() * 1000))
    except (KeyError, TypeError, ValueError):
        return None


def _local_snapshot(request: Request) -> dict:
    config = load_config()
    tz = timezone_context(request, config)
    unavailable = {"available": False, "message": "Unavailable"}

    try:
        processor_rows = query_many(
            """SELECT processor, status, model_used, cost_usd, started_at, duration_ms
               FROM processing_log ORDER BY started_at DESC LIMIT :limit""",
            params={"limit": OVERVIEW_LIMIT},
            config=config,
        )
        processors = {
            "available": True,
            "items": [
                {
                    **row,
                    "time_display": _local_time(
                        row.get("started_at"), tz["display_zone"]
                    ),
                }
                for row in processor_rows
            ],
        }
    except Exception:
        processors = unavailable

    try:
        feed = {"available": True, **_feed_snapshot(config)}
    except Exception:
        feed = unavailable

    try:
        run_rows = query_many(
            """SELECT correlation_id, run_kind, requested_component, status, result_status,
                      started_at, completed_at, error_message
               FROM cycle_runs ORDER BY started_at DESC LIMIT :limit""",
            params={"limit": OVERVIEW_LIMIT},
            config=config,
        )
        runs = {
            "available": True,
            "items": [
                {
                    "correlation_id": str(row.get("correlation_id", "")),
                    "mode": row.get("run_kind") or "cycle",
                    "component": row.get("requested_component") or "all",
                    "status": row.get("result_status")
                    or row.get("status")
                    or "unknown",
                    "duration_ms": _duration_ms(row),
                    "time_display": _local_time(
                        row.get("started_at"), tz["display_zone"]
                    ),
                    "summary": "Completed with errors"
                    if row.get("error_message")
                    else "—",
                }
                for row in run_rows
            ],
        }
    except Exception:
        runs = unavailable

    try:
        outbox_rows = query_many(
            """SELECT
                   COUNT(*) FILTER (
                       WHERE completed_at IS NULL AND failed_at IS NULL
                   ) AS pending,
                   COUNT(*) FILTER (
                       WHERE completed_at IS NULL
                         AND failed_at IS NULL
                         AND claimed_at IS NOT NULL
                   ) AS claimed,
                   COUNT(*) FILTER (WHERE failed_at IS NOT NULL) AS failed,
                   MIN(created_at) FILTER (
                       WHERE completed_at IS NULL AND failed_at IS NULL
                   ) AS oldest_pending_at
               FROM event_outbox""",
            config=config,
        )
        event_rows = query_many(
            """SELECT COUNT(*) AS events_24h
               FROM market_events
               WHERE ingested_at >= NOW() - INTERVAL '24 hours'""",
            config=config,
        )
        freshness_rows = query_many(
            """SELECT source, state, expected_next_at, last_success_at,
                      lag_seconds, reason_code
               FROM source_freshness_state
               ORDER BY source
               LIMIT :limit""",
            params={"limit": OVERVIEW_LIMIT},
            config=config,
        )
        outbox = outbox_rows[0] if outbox_rows else {}
        event_counts = event_rows[0] if event_rows else {}
        event_pipeline = {
            "available": True,
            "pending": int(outbox.get("pending") or 0),
            "claimed": int(outbox.get("claimed") or 0),
            "failed": int(outbox.get("failed") or 0),
            "oldest_pending_display": _local_time(
                outbox.get("oldest_pending_at"), tz["display_zone"]
            ),
            "events_24h": int(event_counts.get("events_24h") or 0),
            "freshness": [
                {
                    **row,
                    "expected_next_display": _local_time(
                        row.get("expected_next_at"), tz["display_zone"]
                    ),
                    "last_success_display": _local_time(
                        row.get("last_success_at"), tz["display_zone"]
                    ),
                }
                for row in freshness_rows
            ],
        }
    except Exception:
        event_pipeline = unavailable

    try:
        atom_context = load_atom_context(config, limit=50, include_history=True)
        grouped: dict[tuple[str, str], list[dict]] = {}
        for atom in atom_context["atoms"]:
            grouped.setdefault((atom["subject_type"], atom["subject_id"]), []).append(
                atom
            )
        claim_history = {
            "available": True,
            "groups": [
                {"subject_type": subject_type, "subject_id": subject_id, "atoms": atoms}
                for (subject_type, subject_id), atoms in grouped.items()
            ],
        }
    except Exception:
        claim_history = unavailable
    try:
        topology = build_system_topology().model_dump(mode="json")
    except Exception:
        topology = unavailable_system_topology().model_dump(mode="json")

    return {
        "tz": tz,
        "processors": processors,
        "feed": feed,
        "runs": runs,
        "event_pipeline": event_pipeline,
        "claim_history": claim_history,
        "topology": topology,
    }


@router.get("/operations")
async def operations_overview(request: Request):
    snapshot = await run_in_threadpool(_local_snapshot, request)
    source_state = await _source_state(request)

    return request.app.state.templates.TemplateResponse(
        request,
        "operations.html",
        {
            "request": request,
            **snapshot["tz"],
            "source_state": source_state,
            "topology": snapshot.get("topology", {}),
            "processors": snapshot["processors"],
            "feed": snapshot["feed"],
            "runs": snapshot["runs"],
            "event_pipeline": snapshot["event_pipeline"],
            "claim_history": snapshot["claim_history"],
        },
    )


@router.get("/partials/operations/source-health")
async def partial_source_health(request: Request):
    source_state = await _source_state(request)
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/source_health.html",
        {
            "request": request,
            "source_state": source_state,
        },
    )


@router.get("/partials/operations/system-topology")
async def partial_system_topology(request: Request):
    try:
        topology = await run_in_threadpool(build_system_topology)
    except Exception:
        topology = unavailable_system_topology()
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/system_topology.html",
        {
            "request": request,
            "topology": topology.model_dump(mode="json"),
        },
    )


@router.get("/partials/operations/claim-history")
async def partial_claim_history(request: Request):
    config = await run_in_threadpool(load_config)
    try:
        atom_context = await run_in_threadpool(
            load_atom_context, config, limit=50, include_history=True
        )
        grouped: dict[tuple[str, str], list[dict]] = {}
        for atom in atom_context["atoms"]:
            grouped.setdefault((atom["subject_type"], atom["subject_id"]), []).append(
                atom
            )
        claim_history = {
            "available": True,
            "groups": [
                {"subject_type": subject_type, "subject_id": subject_id, "atoms": atoms}
                for (subject_type, subject_id), atoms in grouped.items()
            ],
        }
    except Exception:
        claim_history = {"available": False, "groups": []}
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/claim_history.html",
        {"request": request, "claim_history": claim_history},
    )
