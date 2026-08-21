"""Markets page and market-surface partials (lean dashboard refactor).

Owns the authenticated ``/markets`` page and every market-specific panel:
cross-asset context, upcoming catalysts, macro-release cards, full
regime/history, macro indicators/charts, and the full economic calendar.

Canonical partial URLs are ``/partials/markets/...``; the legacy
``/partials/dashboard/...`` and historical ``/partials/...`` URLs remain
registered as aliases on the very same handlers, so no implementation is
duplicated.  All data comes from the existing bounded readers
(``routes.json.*`` and ``routes.views.cockpit_panels``); no SQL or transform
is copied here.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request

import config as app_config
from routes.json.events import get_events_upcoming_data, get_macro_release_cards_data
from routes.json.macro import get_macro_dashboard
from routes.json.regime import get_regime_current
from routes.json.settings import timezone_context
from routes.views.asset_rules import ASSET_EVENT_RULES
from routes.views.cockpit_panels import load_catalysts, load_cross_asset
from staleness import get_staleness_config, is_stale

router = APIRouter()

EVENTS_WINDOW_DAYS = 14
MACRO_RELEASE_LIMIT = 6
TOP_CATALYST_LIMIT = 6


def _get_templates(request: Request):
    return request.app.state.templates


def _live_updates_enabled(config: dict) -> bool:
    return config.get("event_pipeline", {}).get("sse", {}).get("enabled") is True


def _parse_iso(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


def _format_stale_reason(stale_reason: str | None, section: str) -> str | None:
    if not stale_reason or not stale_reason.startswith("Data is "):
        return stale_reason
    hours_part = stale_reason[len("Data is ") :]
    if section == "regime":
        return f"Macro data is {hours_part}"
    if section == "briefing":
        return f"Briefing is {hours_part}. Run cycle to refresh."
    if section == "indicators":
        return f"Macro data is {hours_part}"
    return stale_reason


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


# ---------------------------------------------------------------------------
# Economic-calendar event shaping (shared by the full calendar and the asset
# drawer compatibility partial on the dashboard).
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


def _event_text(event: dict) -> str:
    return " ".join(
        str(event.get(key) or "")
        for key in ("event_name", "currency", "country", "impact_level", "source")
    ).lower()


def _impact_score(event: dict) -> int:
    impact = str(event.get("impact_level") or "").lower()
    if impact == "high":
        return 0
    if impact == "medium":
        return 1
    return 2


def _event_datetime(event: dict) -> datetime:
    scheduled = _parse_iso(event.get("scheduled_at"))
    if scheduled:
        return scheduled
    return datetime.max.replace(tzinfo=UTC)


def _event_time_key(event: dict) -> tuple:
    scheduled = _event_datetime(event)
    if scheduled != datetime.max.replace(tzinfo=UTC):
        return (0, scheduled)
    return (1, event.get("london_time") or event.get("time_display") or "")


def _chronological_event_key(event: dict) -> tuple:
    return (_event_time_key(event), _impact_score(event))


def _event_day_time_display(event: dict) -> str:
    scheduled = _event_datetime(event)
    if scheduled != datetime.max.replace(tzinfo=UTC):
        day_label = event.get("day_label_short") or scheduled.strftime("%a")
        return f"{day_label} · {event.get('time_display') or scheduled.strftime('%H:%M UTC')}"
    return event.get("time_display") or "Time TBC"


def _with_event_display(event: dict) -> dict:
    enriched = dict(event)
    enriched["day_time_display"] = _event_day_time_display(event)
    return enriched


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
    return [_with_event_display(ev) for ev in picked]


def _event_matches_asset(symbol: str, event: dict) -> bool:
    rules = ASSET_EVENT_RULES.get((symbol or "").upper())
    if not rules:
        return False
    currency = str(event.get("currency") or "").upper()
    country = str(event.get("country") or "").upper()
    impact = str(event.get("impact_level") or "").lower()
    if impact not in {"high", "medium"}:
        return False
    if currency and currency in rules.get("currencies", set()):
        return True
    if country and country in rules.get("countries", set()):
        return True
    text = _event_text(event)
    return any(keyword in text for keyword in rules.get("keywords", set()))


def _event_exposure_key(event: dict) -> str | None:
    currency = str(event.get("currency") or "").upper()
    country = str(event.get("country") or "").upper()
    return currency or country or None


def _matched_asset_events(
    symbol: str, events: list[dict], limit: int = 6
) -> list[dict]:
    rules = ASSET_EVENT_RULES.get((symbol or "").upper(), {})
    matches = sorted(
        [event for event in events if _event_matches_asset(symbol, event)],
        key=_chronological_event_key,
    )
    selected = matches[:limit]

    currencies = rules.get("currencies", set())
    if len(currencies) > 1 and len(matches) > limit:
        selected_exposures = {_event_exposure_key(event) for event in selected}
        missing = [
            currency for currency in currencies if currency not in selected_exposures
        ]
        for currency in missing:
            replacement = next(
                (
                    event
                    for event in matches
                    if _event_exposure_key(event) == currency and event not in selected
                ),
                None,
            )
            if replacement:
                selected = selected[:-1] + [replacement]
                selected = sorted(selected, key=_chronological_event_key)

    return [_with_event_display(event) for event in selected[:limit]]


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
    config = app_config.load_config()
    templates = _get_templates(request)
    return templates.TemplateResponse(
        request,
        "markets.html",
        {
            "request": request,
            "live_updates_enabled": _live_updates_enabled(config),
        },
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
            "live_updates_enabled": _live_updates_enabled(config),
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
            "live_updates_enabled": _live_updates_enabled(config),
        },
    )


@router.get("/partials/markets/macro-releases")
@router.get("/partials/dashboard/macro-releases")
def partial_markets_macro_releases(request: Request):
    config = app_config.load_config()
    templates = _get_templates(request)
    try:
        releases = get_macro_release_cards_data(config=config, limit=MACRO_RELEASE_LIMIT)
    except Exception:
        releases = {"cards": [], "error": "Release cards unavailable."}
    return templates.TemplateResponse(
        request,
        "partials/macro_release_cards.html",
        {
            "request": request,
            "macro_releases": releases,
            "live_updates_enabled": _live_updates_enabled(config),
        },
    )


@router.get("/partials/markets/regime")
@router.get("/partials/regime")
def partial_markets_regime(request: Request):
    templates = _get_templates(request)
    regime = {}
    try:
        regime = get_regime_current()
        if regime.get("stale") and regime.get("stale_reason"):
            regime = dict(regime)
            regime["stale_reason"] = _format_stale_reason(
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
@router.get("/partials/indicators")
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
                _parse_iso(last_run), thresholds.get("macro_hours", 30)
            )
            stale_reason = _format_stale_reason(stale_reason, "indicators")
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
@router.get("/partials/events")
def partial_markets_events(request: Request):
    config = app_config.load_config()
    templates = _get_templates(request)
    events_data = {}
    try:
        events_data = get_events_upcoming_data(request=request, days=EVENTS_WINDOW_DAYS)
        for ev in events_data.get("events", []):
            ev["scheduled_at"] = _parse_iso(ev.get("scheduled_at"))
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
