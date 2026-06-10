from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request

from config import load_config
from routes.json.system import get_system_logs, get_system_runs

router = APIRouter()


def _get_templates(request: Request):
    return request.app.state.templates


def _fmt_ts(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M UTC")
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            return value
    return str(value)


def _fetch_logs(component: str = "", status: str = "", range_val: str = "", correlation_id: str = "", limit: int = 50):
    from_date = None
    if range_val == "24h":
        from_date = datetime.now(timezone.utc) - timedelta(hours=24)
    elif range_val == "7d":
        from_date = datetime.now(timezone.utc) - timedelta(days=7)

    data = get_system_logs(
        component=component,
        status=status,
        limit=limit,
        include_detail=True,
        from_date=from_date,
        correlation_id=correlation_id,
    )

    logs = []
    for entry in data.get("logs", []):
        entry = dict(entry)
        entry["started_at"] = _fmt_ts(entry.get("started_at"))
        entry["completed_at"] = _fmt_ts(entry.get("completed_at"))
        if entry.get("cost_usd") is not None:
            try:
                entry["cost_usd"] = float(entry["cost_usd"])
            except Exception:
                entry["cost_usd"] = None
        logs.append(entry)
    return logs


def _distinct_components(config: dict) -> list[str]:
    collectors = [
        k for k, v in config.get("collectors", {}).items()
        if v.get("enabled", True)
    ]
    processors = [
        k for k, v in config.get("processors", {}).items()
        if v.get("enabled", False)
    ]
    return sorted(set(collectors + processors))


@router.get("/logs")
def logs_page(
    request: Request,
    component: str = Query(default=""),
    status: str = Query(default=""),
    range: str = Query(default=""),
    correlation_id: str = Query(default=""),
):
    config = load_config()
    templates = _get_templates(request)
    logs = _fetch_logs(component=component, status=status, range_val=range, correlation_id=correlation_id)
    components = _distinct_components(config)

    return templates.TemplateResponse(request, "logs.html", {
        "request": request,
        "logs": logs,
        "components": components,
        "selected_component": component,
        "selected_status": status,
        "selected_range": range,
        "selected_correlation_id": correlation_id,
        "runs": get_system_runs(limit=12).get("runs", []),
    })


@router.get("/partials/logs")
def partial_logs(
    request: Request,
    component: str = Query(default=""),
    status: str = Query(default=""),
    range: str = Query(default=""),
    correlation_id: str = Query(default=""),
):
    templates = _get_templates(request)
    logs = _fetch_logs(component=component, status=status, range_val=range, correlation_id=correlation_id)
    return templates.TemplateResponse(request, "partials/log_rows.html", {
        "request": request,
        "logs": logs,
    })
