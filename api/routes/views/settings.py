import os
from collections.abc import Mapping
from datetime import datetime

from api_db import query_one
from auth import setup_complete
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

import config as app_config
from contracts.budgets import DEFAULT_DAILY_LLM_USD
from routes.json.settings import _read_secrets, active_model, timezone_context
from routes.views.dashboard import _data_status, _get_dashboard_health

router = APIRouter()


def _has_key(secrets: dict, *names: str) -> bool:
    """True when a credential is available for a managed provider.

    After activation the secrets file is authoritative: deleted (tombstoned)
    or absent keys are unavailable and never fall back to the process
    environment. Before activation the environment may provide credentials
    (demo/CI deployments).
    """
    if setup_complete():
        return any(bool(secrets.get(name)) for name in names)
    return any(bool(secrets.get(name)) or bool(os.environ.get(name)) for name in names)


def _last_cycle_text(config: dict) -> str:
    """Human text for the last completed cycle run (settings page only)."""
    sql = """
        SELECT started_at, completed_at FROM cycle_runs
        WHERE status = 'completed'
        ORDER BY completed_at DESC, started_at DESC
        LIMIT 1
    """
    try:
        row = query_one(sql, config=config)
    except Exception:
        return "No cycle run yet"
    if row and (row.get("completed_at") or row.get("started_at")):
        ts = row.get("completed_at") or row["started_at"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return f"Last cycle: {ts.strftime('%d %b %H:%M UTC')}"
    return "No cycle run yet"


def _latest_cycle_status(config: dict) -> str:
    """Result/status of the most recent cycle run (settings page only)."""
    try:
        row = query_one(
            "SELECT status, result_status FROM cycle_runs ORDER BY started_at DESC LIMIT 1",
            config=config,
        )
    except Exception:
        return "unknown"
    if not row:
        return "unknown"
    return row.get("result_status") or row.get("status") or "unknown"


def _next_cycle_text(health: dict | None) -> str:
    """Earliest next_due_at across health components, as an ISO string."""
    health = health or {}
    due = [
        c.get("next_due_at")
        for c in (health.get("components") or [])
        if c.get("next_due_at")
    ]
    if not due:
        return "Not scheduled"
    return str(min(due))


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    templates = request.app.state.templates
    config = await run_in_threadpool(app_config.load_config)
    secrets = _read_secrets()
    llm = config.get("llm", {}) if isinstance(config.get("llm"), Mapping) else {}
    budgets = (
        config.get("budgets", {}) if isinstance(config.get("budgets"), Mapping) else {}
    )
    tz = timezone_context(request, config)
    health = await _get_dashboard_health(request)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "request": request,
            "active_page": "settings",
            "llm": llm,
            "active_model": active_model(config),
            "daily_budget": budgets.get("daily_llm_usd", DEFAULT_DAILY_LLM_USD),
            "has_openrouter_key": _has_key(secrets, "OPENROUTER_API_KEY"),
            "has_fred_key": _has_key(secrets, "FRED_API_KEY"),
            "has_oanda_key": _has_key(secrets, "OANDA_API_KEY"),
            "has_twitter_key": _has_key(secrets, "TWITTERAPI_KEY", "TWITTERAPIKEY"),
            "timezone_choices": tz["timezone_choices"],
            "current_timezone": tz["current_timezone"],
            "next_cycle": _next_cycle_text(health),
            "system_health": health,
            "data_status": _data_status(health),
            "last_cycle_text": await run_in_threadpool(_last_cycle_text, config),
            "last_cycle_status": await run_in_threadpool(_latest_cycle_status, config),
        },
    )
