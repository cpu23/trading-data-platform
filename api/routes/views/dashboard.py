import asyncio
import json
import re
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import config as app_config
from db import query_many
from logging_config import get_logger
from routes.json.briefing import get_briefing_latest
from routes.json.events import get_events_upcoming_data
from routes.json.settings import timezone_context
from routes.json.system import get_system_health
from routes.views.cockpit_panels import load_briefing_delta
from routes.views.dashboard_strip import load_compact_strip
from routes.views.market_events import (
    format_stale_reason,
    matched_asset_events,
    parse_iso,
)
from routes.views.news import load_story_context
from routes.views.since_last_view import load_since_last_view

logger = get_logger("dashboard")


def _get_templates(request: Request):
    return request.app.state.templates


def _split_sentences(text: str, limit: int) -> list[str]:
    if not isinstance(text, str):
        return []
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return []
    parts = re.split(r"(?<=[.!?])\s+", cleaned)
    bullets = []
    for part in parts:
        part = part.strip(" -•\t")
        if len(part) > 220:
            part = part[:217].rstrip() + "..."
        if part:
            bullets.append(part)
        if len(bullets) >= limit:
            break
    return bullets


def _list_values(value, limit: int) -> list[str]:
    if isinstance(value, list):
        items = []
        for item in value:
            if isinstance(item, dict):
                text = (
                    item.get("summary")
                    or item.get("note")
                    or item.get("text")
                    or item.get("name")
                )
            else:
                text = str(item)
            if text:
                items.append(text.strip())
            if len(items) >= limit:
                break
        return items
    if isinstance(value, str):
        lines = [line.strip(" -•\t") for line in value.splitlines() if line.strip()]
        if len(lines) > 1:
            return lines[:limit]
        return _split_sentences(value, limit)
    return []


def _section_text(value) -> str:
    """Normalize a briefing section value into a single paragraph string."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return " ".join(str(v).strip() for v in value if str(v).strip())
    return ""


def _briefing_sections(briefing: dict | None) -> list[dict]:
    """Return the three fixed briefing sections with truthful legacy fallbacks."""
    if not briefing:
        return []
    sections = briefing.get("sections", {})
    if not isinstance(sections, dict):
        return []

    what_changed = _section_text(sections.get("what_changed"))
    interpretation = _section_text(sections.get("interpretation"))
    invalidation = _section_text(sections.get("invalidation"))

    if not any((what_changed, interpretation, invalidation)):
        # Older records predate the fixed three-section contract. Preserve their
        # decision context under the closest current headings, but never present
        # an upcoming event as an invalidation condition.
        what_changed = _section_text(sections.get("today"))
        interpretation = " ".join(
            text
            for text in (
                _section_text(
                    sections.get("macro_trend") or sections.get("macro_summary")
                ),
                _section_text(
                    sections.get("this_week") or sections.get("upcoming_events")
                ),
            )
            if text
        )

    unavailable = "Not stated in this briefing."
    return [
        {"label": "What changed", "body": what_changed or unavailable},
        {
            "label": "Current interpretation",
            "body": interpretation or unavailable,
        },
        {
            "label": "What would invalidate this",
            "body": invalidation or unavailable,
        },
    ]


def _asset_drivers(note: dict, limit: int = 4) -> list[str]:
    for key in ("key_drivers", "drivers", "factors"):
        values = _list_values(note.get(key), limit)
        if values:
            return values
    return _list_values(note.get("summary") or note.get("note"), limit)


def _get_latest_prices(config: dict) -> dict:
    sql = """
        SELECT symbol, price, timestamp, prev_close FROM (
            SELECT symbol, close AS price, timestamp,
                   LAG(close) OVER (PARTITION BY symbol ORDER BY timestamp) AS prev_close,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY timestamp DESC) AS rn
            FROM market_data
            WHERE source IN ('oanda', 'demo') AND timeframe = 'PRICE'
        ) ranked
        WHERE rn = 1
    """
    rows = query_many(sql, config=config)
    price_map = {}
    for row in rows:
        price = row.get("price")
        if price is None:
            continue
        prev = row.get("prev_close")
        change = None
        if prev is not None:
            try:
                change = round(float(price) - float(prev), 4)
            except (TypeError, ValueError):
                change = None
        price_map[row["symbol"]] = {
            "price": float(price),
            "change": change,
            "timestamp": parse_iso(row.get("timestamp")),
        }
    return price_map


async def _get_dashboard_health(request: Request) -> dict:
    try:
        result = await get_system_health(request)
        if isinstance(result, JSONResponse):
            return json.loads(bytes(result.body))
        return result
    except Exception as exc:
        logger.warning("dashboard_health_unavailable", error_type=type(exc).__name__)
        return {
            "overall": "unavailable",
            "error": "Health data unavailable.",
            "components": [],
            "today_llm_cost_usd": 0,
            "today_token_count": 0,
        }


def _data_status(health: dict | None) -> dict:
    """Compact data-freshness summary for the dashboard header chip."""
    health = health or {}
    components = health.get("components") or []
    delayed = [c for c in components if c.get("stale")]
    if not components:
        label, state = "Data unavailable", "unavailable"
    elif delayed:
        n = len(delayed)
        label, state = f"{n} source{'s' if n != 1 else ''} delayed", "delayed"
    else:
        label, state = "Data current", "current"
    return {
        "label": label,
        "state": state,
        "delayed_count": len(delayed),
        "components": components,
    }



router = APIRouter()


@router.get("/")
async def dashboard(request: Request):
    config = await run_in_threadpool(app_config.load_config)
    templates = _get_templates(request)

    (
        briefing_result,
        since_last_view_result,
        strip_result,
        health_result,
    ) = await asyncio.gather(
        run_in_threadpool(get_briefing_latest),
        run_in_threadpool(load_since_last_view, config),
        run_in_threadpool(load_compact_strip, config),
        _get_dashboard_health(request),
        return_exceptions=True,
    )

    briefing = None if isinstance(briefing_result, Exception) else briefing_result
    briefing_delta_result = await run_in_threadpool(
        load_briefing_delta, config, briefing
    )
    briefing_delta = (
        {"available": False, "bullets": [], "atoms": [], "latest_date": None}
        if isinstance(briefing_delta_result, Exception)
        else briefing_delta_result
    )
    system_health = (
        {
            "overall": "unavailable",
            "error": "Health data unavailable.",
            "components": [],
        }
        if isinstance(health_result, Exception)
        else health_result
    )
    tz_context = timezone_context(request, config)
    now = datetime.now(tz_context["display_zone"])

    context = {
        "request": request,
        "current_time": now,
        "data_status": _data_status(system_health),
        "strip": {} if isinstance(strip_result, Exception) else strip_result,
        "since_last_view": (
            {"available": False, "marker": None, "sections": [], "counts": {}}
            if isinstance(since_last_view_result, Exception)
            else since_last_view_result
        ),
        "briefing": briefing,
        "briefing_sections": _briefing_sections(briefing),
        "briefing_delta": briefing_delta,
        "live_updates_enabled": app_config.live_updates_enabled(config),
        **tz_context,
    }
    return templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/partials/header")
async def partial_header(request: Request):
    config = await run_in_threadpool(app_config.load_config)
    templates = _get_templates(request)
    tz_context = timezone_context(request, config)
    now = datetime.now(tz_context["display_zone"])

    return templates.TemplateResponse(
        request,
        "partials/header.html",
        {
            "request": request,
            "current_time": now,
            "data_status": _data_status(await _get_dashboard_health(request)),
            **tz_context,
        },
    )


@router.get("/partials/dashboard/news")
def partial_news(request: Request):
    config = app_config.load_config()
    templates = _get_templates(request)
    stories = load_story_context(limit=12)
    return templates.TemplateResponse(
        request,
        "partials/news_section.html",
        {
            "request": request,
            "stories": stories,
            "live_updates_enabled": app_config.live_updates_enabled(config),
        },
    )


@router.get("/partials/cards/clear")
def partial_cards_clear(request: Request):
    templates = _get_templates(request)
    return templates.TemplateResponse(
        request,
        "partials/expansion_panel.html",
        {
            "request": request,
        },
    )


@router.get("/partials/cards/{symbol}")
def partial_cards_symbol(request: Request, symbol: str):
    config = app_config.load_config()
    templates = _get_templates(request)
    briefing = None
    try:
        briefing = get_briefing_latest()
    except Exception:
        briefing = None

    note = None
    if briefing and briefing.get("sections") and isinstance(briefing["sections"], dict):
        notes = briefing["sections"].get("watchlist_notes")
        if isinstance(notes, list):
            for n in notes:
                if isinstance(n, dict) and n.get("symbol") == symbol:
                    note = n
                    break

    if note:
        events = []
        try:
            events_data = get_events_upcoming_data(request=request, days=14)
            events = events_data.get("events", [])
            for ev in events:
                ev["scheduled_at"] = parse_iso(ev.get("scheduled_at"))
        except Exception:
            events = []
        return templates.TemplateResponse(
            request,
            "partials/expansion_content.html",
            {
                "request": request,
                "note": note,
                "price": _get_latest_prices(config).get(symbol),
                "drivers": _asset_drivers(note),
                "matched_events": matched_asset_events(symbol, events),
                "opinion_id": briefing.get("opinion_ids", [])[-1]
                if briefing.get("opinion_ids")
                else None,
            },
        )

    # Symbol not found: return empty panel
    return templates.TemplateResponse(
        request,
        "partials/expansion_panel.html",
        {
            "request": request,
        },
    )


@router.get("/partials/dashboard/watchlist")
@router.get("/partials/cards")
def partial_cards(request: Request):
    config = app_config.load_config()
    templates = _get_templates(request)
    briefing = None
    try:
        briefing = get_briefing_latest()
        if briefing and briefing.get("stale") and briefing.get("stale_reason"):
            briefing = dict(briefing)
            briefing["stale_reason"] = format_stale_reason(
                briefing["stale_reason"], "briefing"
            )
    except Exception:
        briefing = None
    try:
        price_map = _get_latest_prices(config)
    except Exception:
        price_map = {}
    return templates.TemplateResponse(
        request,
        "partials/cards_section.html",
        {
            "request": request,
            "briefing": briefing,
            "price_map": price_map,
            "live_updates_enabled": app_config.live_updates_enabled(config),
        },
    )


@router.get("/partials/briefing")
def partial_briefing(request: Request):
    templates = _get_templates(request)
    config = app_config.load_config()
    briefing = None
    try:
        briefing = get_briefing_latest()
    except Exception:
        briefing = None
    briefing_delta = {"available": False, "bullets": [], "atoms": [], "latest_date": None}
    try:
        briefing_delta = load_briefing_delta(config, latest=briefing)
    except Exception:
        pass
    return templates.TemplateResponse(
        request,
        "partials/briefing_prose.html",
        {
            "request": request,
            "briefing": briefing,
            "briefing_sections": _briefing_sections(briefing),
            "briefing_delta": briefing_delta,
        },
    )
