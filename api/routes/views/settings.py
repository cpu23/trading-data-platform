import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

import config as app_config
from budgets import DEFAULT_DAILY_LLM_USD
from routes.json.settings import _read_secrets, timezone_context
from routes.views.dashboard import (
    _data_status,
    _get_dashboard_health,
    _last_cycle_text,
    _latest_cycle_status,
)

router = APIRouter()


def _has_key(secrets: dict, *names: str) -> bool:
    return any(secrets.get(name) or os.environ.get(name) for name in names)


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
    llm = config.get("llm", {}) if isinstance(config.get("llm"), dict) else {}
    models = llm.get("models", {}) if isinstance(llm.get("models"), dict) else {}
    budgets = (
        config.get("budgets", {}) if isinstance(config.get("budgets"), dict) else {}
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
            "models": models,
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
