from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request

from config import load_config
from db import query_many, query_one
from routes.json.briefing import get_briefing_latest
from routes.json.events import get_events_upcoming
from routes.json.macro import get_macro_dashboard
from routes.json.regime import get_regime_current
from staleness import get_staleness_config, is_stale

router = APIRouter()


def _get_templates(request: Request):
    return request.app.state.templates


def _last_cycle_text(config: dict) -> str:
    sql = """
        SELECT started_at, completed_at FROM cycle_runs
        WHERE status = 'completed'
        ORDER BY completed_at DESC, started_at DESC
        LIMIT 1
    """
    row = query_one(sql, config=config)
    if row and (row.get("completed_at") or row.get("started_at")):
        ts = row.get("completed_at") or row["started_at"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return f"Last cycle: {ts.strftime('%d %b %H:%M UTC')}"
    return "No cycle run yet"


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
    hours_part = stale_reason[len("Data is "):]
    if section == "regime":
        return f"Macro data is {hours_part}"
    if section == "briefing":
        return f"Briefing is {hours_part}. Run cycle to refresh."
    if section == "indicators":
        return f"Macro data is {hours_part}"
    return stale_reason


def _primary_timezone(config: dict) -> ZoneInfo:
    tz_name = (
        config.get("timezone", {})
        .get("primary", {})
        .get("name", "Europe/London")
    )
    return ZoneInfo(tz_name)


def _event_template_context(events_data: dict, config: dict) -> dict:
    grouped = events_data.get("grouped", {}) if isinstance(events_data, dict) else {}
    filtered_events = events_data.get("events", []) if isinstance(events_data, dict) else []
    london = _primary_timezone(config)
    today_str = datetime.now(london).strftime("%Y-%m-%d")

    def day_label_for(day_key: str) -> str:
        if day_key == today_str:
            return "Today"
        try:
            dt = datetime.strptime(day_key, "%Y-%m-%d")
            return dt.strftime("%A")
        except Exception:
            return day_key

    return {
        "filtered_events": filtered_events,
        "grouped": grouped,
        "today_str": today_str,
        "day_label_for": day_label_for,
    }


def _get_latest_prices(config: dict) -> dict:
    sql = """
        SELECT DISTINCT ON (symbol)
            symbol, close AS price, timestamp
        FROM market_data
        WHERE source = 'oanda' AND timeframe = 'PRICE'
        ORDER BY symbol, timestamp DESC
    """
    rows = query_many(sql, config=config)
    price_map = {}
    for row in rows:
        price = row.get("price")
        if price is None:
            continue
        price_map[row["symbol"]] = {
            "price": float(price),
            "timestamp": _parse_iso(row.get("timestamp")),
        }
    return price_map


@router.get("/")
def dashboard(request: Request):
    config = load_config()
    templates = _get_templates(request)

    # Fetch data with graceful error handling per section
    regime = {}
    try:
        regime = get_regime_current()
        if regime.get("stale") and regime.get("stale_reason"):
            regime = dict(regime)
            regime["stale_reason"] = _format_stale_reason(regime["stale_reason"], "regime")
    except Exception as exc:
        regime = {"error": str(exc)}

    briefing = None
    try:
        briefing = get_briefing_latest()
        if briefing and briefing.get("stale") and briefing.get("stale_reason"):
            briefing = dict(briefing)
            briefing["stale_reason"] = _format_stale_reason(briefing["stale_reason"], "briefing")
    except Exception:
        briefing = None

    events_data = {}
    try:
        events_data = get_events_upcoming(days=14)
        # Parse ISO strings to datetime for template convenience
        for ev in events_data.get("events", []):
            ev["scheduled_at"] = _parse_iso(ev.get("scheduled_at"))
    except Exception as exc:
        events_data = {"error": str(exc)}

    indicators = []
    indicators_stale = False
    indicators_stale_reason = None
    try:
        macro_data = get_macro_dashboard()
        indicator_configs = config.get("dashboard", {}).get("indicators", [])
        precision_map = {ic["series_id"]: ic.get("precision", 2) for ic in indicator_configs}
        note_map = {ic["series_id"]: ic.get("note") for ic in indicator_configs}
        for ind in macro_data.get("indicators", []):
            ind["precision"] = precision_map.get(ind["series_id"], 2)
            ind["note"] = note_map.get(ind["series_id"])
            indicators.append(ind)
        # Stale check for indicators section (fred collector)
        last_run = macro_data.get("last_collector_run")
        if last_run:
            thresholds = get_staleness_config(config)
            indicators_stale, indicators_stale_reason = is_stale(
                _parse_iso(last_run), thresholds.get("macro_hours", 30)
            )
            indicators_stale_reason = _format_stale_reason(indicators_stale_reason, "indicators")
    except Exception as exc:
        indicators = [{"error": str(exc)}]

    now = datetime.now(timezone.utc)
    event_context = _event_template_context(events_data, config)
    price_map = _get_latest_prices(config)

    any_stale = bool(
        (regime.get("stale") if isinstance(regime, dict) else False)
        or (events_data.get("stale") if isinstance(events_data, dict) else False)
        or (briefing and briefing.get("stale"))
        or any(i.get("stale") for i in indicators if isinstance(i, dict))
    )

    context = {
        "request": request,
        "last_cycle_text": _last_cycle_text(config),
        "regime": regime,
        "briefing": briefing,
        "events_data": events_data,
        "indicators": indicators,
        "stale": indicators_stale,
        "stale_reason": indicators_stale_reason,
        "current_time": now,
        "timedelta": timedelta,
        "any_stale": any_stale,
        "price_map": price_map,
        **event_context,
    }
    return templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/partials/header")
def partial_header(request: Request):
    config = load_config()
    templates = _get_templates(request)
    now = datetime.now(timezone.utc)

    regime = {}
    try:
        regime = get_regime_current()
    except Exception:
        pass

    briefing = None
    try:
        briefing = get_briefing_latest()
    except Exception:
        pass

    events_data = {}
    try:
        events_data = get_events_upcoming(days=14)
    except Exception:
        pass

    indicators = []
    try:
        macro_data = get_macro_dashboard()
        indicators = macro_data.get("indicators", [])
    except Exception:
        pass

    any_stale = bool(
        (regime.get("stale") if isinstance(regime, dict) else False)
        or (events_data.get("stale") if isinstance(events_data, dict) else False)
        or (briefing and briefing.get("stale"))
        or any(i.get("stale") for i in indicators if isinstance(i, dict))
    )

    return templates.TemplateResponse(request, "partials/header.html", {
        "request": request,
        "last_cycle_text": _last_cycle_text(config),
        "current_time": now,
        "any_stale": any_stale,
    })


@router.get("/partials/regime")
def partial_regime(request: Request):
    templates = _get_templates(request)
    regime = {}
    try:
        regime = get_regime_current()
        if regime.get("stale") and regime.get("stale_reason"):
            regime = dict(regime)
            regime["stale_reason"] = _format_stale_reason(regime["stale_reason"], "regime")
    except Exception as exc:
        regime = {"error": str(exc)}
    return templates.TemplateResponse(request, "partials/regime_section.html", {
        "request": request,
        "regime": regime,
    })


@router.get("/partials/events")
def partial_events(request: Request):
    config = load_config()
    templates = _get_templates(request)
    events_data = {}
    try:
        events_data = get_events_upcoming(days=14)
        for ev in events_data.get("events", []):
            ev["scheduled_at"] = _parse_iso(ev.get("scheduled_at"))
    except Exception as exc:
        events_data = {"error": str(exc)}

    now = datetime.now(timezone.utc)
    event_context = _event_template_context(events_data, config)

    return templates.TemplateResponse(request, "partials/events_section.html", {
        "request": request,
        "events_data": events_data,
        "current_time": now,
        "timedelta": timedelta,
        **event_context,
    })


@router.get("/partials/cards/clear")
def partial_cards_clear(request: Request):
    templates = _get_templates(request)
    return templates.TemplateResponse(request, "partials/expansion_panel.html", {
        "request": request,
    })


@router.get("/partials/cards/{symbol}")
def partial_cards_symbol(request: Request, symbol: str):
    config = load_config()
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
        return templates.TemplateResponse(request, "partials/expansion_content.html", {
            "request": request,
            "note": note,
            "price": _get_latest_prices(config).get(symbol),
        })

    # Symbol not found: return empty panel
    return templates.TemplateResponse(request, "partials/expansion_panel.html", {
        "request": request,
    })


@router.get("/partials/cards")
def partial_cards(request: Request):
    config = load_config()
    templates = _get_templates(request)
    briefing = None
    try:
        briefing = get_briefing_latest()
        if briefing and briefing.get("stale") and briefing.get("stale_reason"):
            briefing = dict(briefing)
            briefing["stale_reason"] = _format_stale_reason(briefing["stale_reason"], "briefing")
    except Exception:
        briefing = None
    return templates.TemplateResponse(request, "partials/cards_section.html", {
        "request": request,
        "briefing": briefing,
        "price_map": _get_latest_prices(config),
    })


@router.get("/partials/indicators")
def partial_indicators(request: Request):
    config = load_config()
    templates = _get_templates(request)
    indicators = []
    stale = False
    stale_reason = None
    try:
        macro_data = get_macro_dashboard()
        indicator_configs = config.get("dashboard", {}).get("indicators", [])
        precision_map = {ic["series_id"]: ic.get("precision", 2) for ic in indicator_configs}
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
        return templates.TemplateResponse(request, "partials/indicators_section.html", {
            "request": request,
            "error": str(exc),
            "indicators": [],
            "stale": False,
            "stale_reason": None,
        })
    return templates.TemplateResponse(request, "partials/indicators_section.html", {
        "request": request,
        "indicators": indicators,
        "stale": stale,
        "stale_reason": stale_reason,
    })


@router.get("/partials/briefing")
def partial_briefing(request: Request):
    templates = _get_templates(request)
    briefing = None
    try:
        briefing = get_briefing_latest()
    except Exception:
        briefing = None
    return templates.TemplateResponse(request, "partials/briefing_prose.html", {
        "request": request,
        "briefing": briefing,
    })
