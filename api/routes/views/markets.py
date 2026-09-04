"""Markets page and market-surface partials (lean dashboard refactor).

Owns the authenticated ``/markets`` page and every market-specific panel:
cross-asset context, upcoming catalysts, macro-release cards, full
regime/history, macro indicators/charts, and the full economic calendar.

Canonical partial URLs are under ``/partials/markets``. All data comes from
the existing bounded readers (``routes.json.*`` and
``routes.views.cockpit_panels``); no SQL or transform is copied here.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from staleness import get_staleness_config, is_stale

import config as app_config
from routes.json.events import get_events_upcoming_data, get_macro_release_cards_data
from routes.json.macro import get_macro_dashboard
from routes.json.regime import get_regime_current
from routes.json.settings import timezone_context
from routes.views.cockpit_panels import load_catalysts, load_cross_asset
from routes.views.market_events import (
    format_stale_reason,
    parse_iso,
    with_event_display,
)

router = APIRouter()

EVENTS_WINDOW_DAYS = 14
MACRO_RELEASE_LIMIT = 6
TOP_CATALYST_LIMIT = 6


def _get_templates(request: Request):
    return request.app.state.templates


def _freshness_dot(
    stale: bool = False, failed: bool = False, title: str | None = None
) -> dict:
    if failed:
        return {"state": "failed", "title": title or "Section failed to load"}
    if stale:
        return {"state": "stale", "title": title or "Data may be stale"}
    return {"state": "", "title": ""}


def _section_dots(
    regime: dict,
    events_data: dict,
    briefing: dict | None,
    indicators_stale: bool,
    indicators_stale_reason: str | None,
) -> dict:
    return {
        "regime": _freshness_dot(
            stale=bool(isinstance(regime, dict) and regime.get("stale")),
            failed=bool(isinstance(regime, dict) and regime.get("error")),
            title=(
                regime.get("error")
                or regime.get("stale_reason")
                or regime.get("created_at")
            )
            if isinstance(regime, dict)
            else None,
        ),
        "events": _freshness_dot(
            stale=bool(isinstance(events_data, dict) and events_data.get("stale")),
            failed=bool(isinstance(events_data, dict) and events_data.get("error")),
            title=(events_data.get("error") or events_data.get("stale_reason"))
            if isinstance(events_data, dict)
            else None,
        ),
        "indicators": _freshness_dot(
            stale=indicators_stale,
            failed=False,
            title=indicators_stale_reason,
        ),
        "briefing": _freshness_dot(
            stale=bool(briefing and briefing.get("stale")),
            failed=False,
            title=(briefing.get("stale_reason") or briefing.get("created_at"))
            if briefing
            else None,
        ),
    }


def _primary_timezone(config: dict) -> ZoneInfo:
    tz_name = config.get("timezone", {}).get("primary", {}).get("name", "Europe/London")
    return ZoneInfo(tz_name)


# Economic-calendar event shaping shared by the calendar and asset drawer.
# ---------------------------------------------------------------------------


def _event_template_context(
    events_data: dict,
    config: dict,
    *,
    display_zone: ZoneInfo | None = None,
) -> dict:
    grouped = events_data.get("grouped", {}) if isinstance(events_data, dict) else {}
    filtered_events = (
        events_data.get("events", []) if isinstance(events_data, dict) else []
    )
    high_impact_grouped = {}
    for day_key, events in grouped.items():
        high_impact_events = [
            event
            for event in events
            if str(event.get("impact_level") or "").lower() == "high"
        ]
        if high_impact_events:
            high_impact_grouped[day_key] = high_impact_events
    selected_zone = display_zone or _primary_timezone(config)
    today_str = datetime.now(selected_zone).strftime("%Y-%m-%d")

    def day_label_for(day_key: str) -> str:
        if day_key == today_str:
            return "Today"
        try:
            dt = datetime.strptime(day_key, "%Y-%m-%d")
            return dt.strftime("%A")
        except Exception:
            return day_key

    catalysts = _top_catalysts(events_data)
    return {
        "filtered_events": filtered_events,
        "grouped": grouped,
        "catalysts": catalysts,
        "upcoming_label": _upcoming_label(high_impact_grouped, day_label_for),
        "high_impact_grouped": high_impact_grouped,
        "today_str": today_str,
        "day_label_for": day_label_for,
    }


def _upcoming_label(high_impact_grouped: dict, day_label_for) -> str:
    """Human label for the catalyst window, e.g. 'Today – Friday'."""
    days = sorted(high_impact_grouped.keys())
    if not days:
        return ""
    if len(days) == 1:
        return day_label_for(days[0])
    return f"{day_label_for(days[0])} – {day_label_for(days[-1])}"


def _top_catalysts(events_data: dict, limit: int = TOP_CATALYST_LIMIT) -> list[dict]:
    """High-impact events spread across the week instead of stacking on one busy day."""
    high = [
        ev
        for ev in (events_data.get("events") or [])
        if (ev.get("impact_level") or "").lower() == "high"
    ]
    by_day: dict[str, list[dict]] = {}
    for ev in high:
        by_day.setdefault(ev.get("day_key") or "", []).append(ev)
    picked: list[dict] = []
    queues = list(by_day.values())
    while queues and len(picked) < limit:
        next_queues = []
        for queue in queues:
            picked.append(queue.pop(0))
            if queue:
                next_queues.append(queue)
        queues = next_queues
    picked.sort(key=lambda ev: ev.get("scheduled_at") or "")
    return [with_event_display(ev) for ev in picked]


# ---------------------------------------------------------------------------
# Pages and partials
# ---------------------------------------------------------------------------


@router.get("/markets")
def markets_page(request: Request):
    """Authenticated markets workspace; every section lazy-loads a partial.

    The page shell stays light: each market surface is fetched through its
    canonical ``/partials/markets/...`` URL so a degraded dataset degrades
    only its own section.
    """
    templates = _get_templates(request)
    return templates.TemplateResponse(
        request,
        "markets.html",
        {"request": request},
    )


@router.get("/partials/markets/cross-asset")
def partial_markets_cross_asset(request: Request):
    config = app_config.load_config()
    templates = _get_templates(request)
    return templates.TemplateResponse(
        request,
        "partials/cross_asset.html",
        {
            "request": request,
            "cross_asset": load_cross_asset(config),
        },
    )


@router.get("/partials/markets/catalysts")
def partial_markets_catalysts(request: Request):
    config = app_config.load_config()
    templates = _get_templates(request)
    return templates.TemplateResponse(
        request,
        "partials/catalysts.html",
        {
            "request": request,
            "catalysts": load_catalysts(config),
        },
    )


@router.get("/partials/markets/macro-releases")
def partial_markets_macro_releases(request: Request):
    config = app_config.load_config()
    templates = _get_templates(request)
    try:
        releases = get_macro_release_cards_data(
            config=config, limit=MACRO_RELEASE_LIMIT
        )
    except Exception:
        releases = {"cards": [], "error": "Release cards unavailable."}
    return templates.TemplateResponse(
        request,
        "partials/macro_release_cards.html",
        {
            "request": request,
            "macro_releases": releases,
        },
    )


@router.get("/partials/markets/regime")
def partial_markets_regime(request: Request):
    templates = _get_templates(request)
    regime = {}
    try:
        regime = get_regime_current()
        if regime.get("stale") and regime.get("stale_reason"):
            regime = dict(regime)
            regime["stale_reason"] = format_stale_reason(
                regime["stale_reason"], "regime"
            )
    except Exception as exc:
        regime = {"error": str(exc)}
    return templates.TemplateResponse(
        request,
        "partials/regime_section.html",
        {
            "request": request,
            "regime": regime,
            "dot": _section_dots(regime, {}, None, False, None)["regime"],
        },
    )


@router.get("/partials/markets/indicators")
def partial_markets_indicators(request: Request):
    config = app_config.load_config()
    templates = _get_templates(request)
    indicators = []
    stale = False
    stale_reason = None
    try:
        macro_data = get_macro_dashboard()
        indicator_configs = config.get("dashboard", {}).get("indicators", [])
        precision_map = {
            ic["series_id"]: ic.get("precision", 2) for ic in indicator_configs
        }
        note_map = {ic["series_id"]: ic.get("note") for ic in indicator_configs}
        for ind in macro_data.get("indicators", []):
            ind["precision"] = precision_map.get(ind["series_id"], 2)
            ind["note"] = note_map.get(ind["series_id"])
            indicators.append(ind)
        last_run = macro_data.get("last_collector_run")
        if last_run:
            thresholds = get_staleness_config(config)
            stale, stale_reason = is_stale(
                parse_iso(last_run), thresholds.get("macro_hours", 30)
            )
            stale_reason = format_stale_reason(stale_reason, "indicators")
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "partials/indicators_section.html",
            {
                "request": request,
                "error": str(exc),
                "indicators": [],
                "stale": False,
                "stale_reason": None,
                "dot": _freshness_dot(failed=True, title=str(exc)),
            },
        )
    return templates.TemplateResponse(
        request,
        "partials/indicators_section.html",
        {
            "request": request,
            "indicators": indicators,
            "stale": stale,
            "stale_reason": stale_reason,
            "dot": _freshness_dot(stale=stale, title=stale_reason),
        },
    )


@router.get("/partials/markets/events")
def partial_markets_events(request: Request):
    config = app_config.load_config()
    templates = _get_templates(request)
    events_data = {}
    try:
        events_data = get_events_upcoming_data(request=request, days=EVENTS_WINDOW_DAYS)
        for ev in events_data.get("events", []):
            ev["scheduled_at"] = parse_iso(ev.get("scheduled_at"))
    except Exception as exc:
        events_data = {"error": str(exc)}

    tz_context = timezone_context(request, config)
    now = datetime.now(tz_context["display_zone"])
    event_context = _event_template_context(
        events_data,
        config,
        display_zone=tz_context["display_zone"],
    )

    return templates.TemplateResponse(
        request,
        "partials/events_section.html",
        {
            "request": request,
            "events_data": events_data,
            "current_time": now,
            "timedelta": timedelta,
            "dot": _section_dots({}, events_data, None, False, None)["events"],
            **tz_context,
            **event_context,
        },
    )
