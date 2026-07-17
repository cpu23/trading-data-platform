from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from config import load_config
from db import query_many
from routes.json.settings import timezone_context
from routes.json.system import get_system_health
from routes.views.news import load_news_context

router = APIRouter()
OVERVIEW_LIMIT = 10


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
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(zone).strftime("%d %b %Y %H:%M %Z")
    except (TypeError, ValueError):
        return None


def _duration_ms(row: dict):
    value = row.get("duration_ms")
    if value is not None:
        return value
    try:
        start = row["started_at"] if isinstance(row["started_at"], datetime) else datetime.fromisoformat(str(row["started_at"]).replace("Z", "+00:00"))
        end = row["completed_at"] if isinstance(row["completed_at"], datetime) else datetime.fromisoformat(str(row["completed_at"]).replace("Z", "+00:00"))
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
        processors = {"available": True, "items": [
            {**row, "time_display": _local_time(row.get("started_at"), tz["display_zone"])}
            for row in processor_rows
        ]}
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
        runs = {"available": True, "items": [
            {
                "correlation_id": str(row.get("correlation_id", "")),
                "mode": row.get("run_kind") or "cycle",
                "component": row.get("requested_component") or "all",
                "status": row.get("result_status") or row.get("status") or "unknown",
                "duration_ms": _duration_ms(row),
                "time_display": _local_time(row.get("started_at"), tz["display_zone"]),
                "summary": "Completed with errors" if row.get("error_message") else "—",
            }
            for row in run_rows
        ]}
    except Exception:
        runs = unavailable

    return {"tz": tz, "processors": processors, "feed": feed, "runs": runs}


@router.get("/operations")
async def operations_overview(request: Request):
    unavailable = {"available": False, "message": "Unavailable"}
    snapshot = await run_in_threadpool(_local_snapshot, request)
    try:
        health = await get_system_health(request)
        if isinstance(health, JSONResponse):
            source_state = unavailable
        else:
            source_state = {"available": True, "readiness": health.get("readiness", "unknown"), "components": health.get("components", [])[:OVERVIEW_LIMIT]}
    except Exception:
        source_state = unavailable

    return request.app.state.templates.TemplateResponse(request, "operations.html", {
        "request": request,
        **snapshot["tz"],
        "source_state": source_state,
        "processors": snapshot["processors"],
        "feed": snapshot["feed"],
        "runs": snapshot["runs"],
    })
