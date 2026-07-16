import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request

from config import load_config
from db import query_many, query_one
from routes.json.briefing import get_briefing_latest
from routes.json.events import get_events_upcoming_data
from routes.json.macro import get_macro_dashboard
from routes.json.regime import get_regime_current
from routes.json.system import get_system_health
from routes.json.settings import timezone_context
from budgets import get_budget_status
from staleness import get_staleness_config, is_stale

router = APIRouter()

ASSET_EVENT_RULES = {
    "EURUSD": {"currencies": {"EUR", "USD"}},
    "DXY": {"currencies": {"USD"}},
    "USDJPY": {"currencies": {"USD", "JPY"}},
    "AUDJPY": {"currencies": {"AUD", "JPY"}},
    "SP500": {"currencies": {"USD"}, "countries": {"US"}},
    "XAUUSD": {"currencies": {"USD"}, "keywords": {"inflation", "cpi", "ppi", "rates", "rate", "fed", "fomc", "yield", "risk", "jobs", "payroll"}},
    "XPTUSD": {"currencies": {"USD"}, "keywords": {"inflation", "cpi", "ppi", "industrial", "manufacturing", "pmi", "risk", "growth", "china"}},
    "GER40": {"currencies": {"EUR"}, "countries": {"EU", "DE"}, "keywords": {"germany", "german", "ecb", "eurozone"}},
    "UK100": {"currencies": {"GBP"}, "countries": {"GB", "UK"}, "keywords": {"uk", "britain", "boe", "bank of england"}},
}

# Curated destinations only: never derive an external URL from a rendered symbol.
SYMBOL_LINKS = {
    "AUDJPY": {"url": "https://finance.yahoo.com/quote/AUDJPY=X", "label": "Yahoo Finance AUD/JPY"},
    "XAUUSD": {"url": "https://finance.yahoo.com/quote/GC=F", "label": "Yahoo Finance Gold Futures"},
    "XPTUSD": {"url": "https://finance.yahoo.com/quote/PL=F", "label": "Yahoo Finance Platinum Futures"},
    "JP225": {"url": "https://finance.yahoo.com/quote/%5EN225", "label": "Yahoo Finance Nikkei 225"},
    "UK100": {"url": "https://finance.yahoo.com/quote/%5EFTSE", "label": "Yahoo Finance FTSE 100"},
    "DE40": {"url": "https://finance.yahoo.com/quote/%5EGDAXI", "label": "Yahoo Finance DAX"},
    "EURCHF": {"url": "https://finance.yahoo.com/quote/EURCHF=X", "label": "Yahoo Finance EUR/CHF"},
}


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


def _latest_cycle_status(config: dict) -> str:
    row = query_one(
        "SELECT status, result_status FROM cycle_runs ORDER BY started_at DESC LIMIT 1",
        config=config,
    )
    if not row:
        return "unknown"
    return row.get("result_status") or row.get("status") or "unknown"


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


def _freshness_dot(stale: bool = False, failed: bool = False, title: str | None = None) -> dict:
    if failed:
        return {"state": "failed", "title": title or "Section failed to load"}
    if stale:
        return {"state": "stale", "title": title or "Data may be stale"}
    return {"state": "", "title": ""}


def _section_dots(regime: dict, events_data: dict, briefing: dict | None, indicators_stale: bool, indicators_stale_reason: str | None) -> dict:
    return {
        "regime": _freshness_dot(
            stale=bool(isinstance(regime, dict) and regime.get("stale")),
            failed=bool(isinstance(regime, dict) and regime.get("error")),
            title=(regime.get("error") or regime.get("stale_reason") or regime.get("created_at")) if isinstance(regime, dict) else None,
        ),
        "events": _freshness_dot(
            stale=bool(isinstance(events_data, dict) and events_data.get("stale")),
            failed=bool(isinstance(events_data, dict) and events_data.get("error")),
            title=(events_data.get("error") or events_data.get("stale_reason")) if isinstance(events_data, dict) else None,
        ),
        "indicators": _freshness_dot(
            stale=indicators_stale,
            failed=False,
            title=indicators_stale_reason,
        ),
        "briefing": _freshness_dot(
            stale=bool(briefing and briefing.get("stale")),
            failed=False,
            title=(briefing.get("stale_reason") or briefing.get("created_at")) if briefing else None,
        ),
    }


def _primary_timezone(config: dict) -> ZoneInfo:
    tz_name = (
        config.get("timezone", {})
        .get("primary", {})
        .get("name", "Europe/London")
    )
    return ZoneInfo(tz_name)


def _event_template_context(
    events_data: dict,
    config: dict,
    *,
    display_zone: ZoneInfo | None = None,
) -> dict:
    grouped = events_data.get("grouped", {}) if isinstance(events_data, dict) else {}
    filtered_events = events_data.get("events", []) if isinstance(events_data, dict) else []
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

    return {
        "filtered_events": filtered_events,
        "grouped": grouped,
        "catalysts": _top_catalysts(filtered_events),
        "high_impact_grouped": high_impact_grouped,
        "today_str": today_str,
        "day_label_for": day_label_for,
    }


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
    return datetime.max.replace(tzinfo=timezone.utc)


def _event_time_key(event: dict) -> tuple:
    scheduled = _event_datetime(event)
    if scheduled != datetime.max.replace(tzinfo=timezone.utc):
        return (0, scheduled)
    return (1, event.get("london_time") or event.get("time_display") or "")


def _chronological_event_key(event: dict) -> tuple:
    return (_event_time_key(event), _impact_score(event))


def _event_day_time_display(event: dict) -> str:
    scheduled = _event_datetime(event)
    if scheduled != datetime.max.replace(tzinfo=timezone.utc):
        day_label = event.get("day_label_short") or scheduled.strftime("%a")
        return f"{day_label} · {event.get('time_display') or scheduled.strftime('%H:%M UTC')}"
    return event.get("time_display") or "Time TBC"


def _with_event_display(event: dict) -> dict:
    enriched = dict(event)
    enriched["day_time_display"] = _event_day_time_display(event)
    return enriched


def _top_catalysts(events: list[dict], limit: int = 6) -> list[dict]:
    ordered = sorted(
        [event for event in events if str(event.get("impact_level") or "").lower() == "high"],
        key=_chronological_event_key,
    )
    return [_with_event_display(event) for event in ordered[:limit]]


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


def _matched_asset_events(symbol: str, events: list[dict], limit: int = 6) -> list[dict]:
    rules = ASSET_EVENT_RULES.get((symbol or "").upper(), {})
    matches = sorted(
        [event for event in events if _event_matches_asset(symbol, event)],
        key=_chronological_event_key,
    )
    selected = matches[:limit]

    currencies = rules.get("currencies", set())
    if len(currencies) > 1 and len(matches) > limit:
        selected_exposures = {_event_exposure_key(event) for event in selected}
        missing = [currency for currency in currencies if currency not in selected_exposures]
        for currency in missing:
            replacement = next(
                (event for event in matches if _event_exposure_key(event) == currency and event not in selected),
                None,
            )
            if replacement:
                selected = selected[:-1] + [replacement]
                selected = sorted(selected, key=_chronological_event_key)

    return [_with_event_display(event) for event in selected[:limit]]


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
                text = item.get("summary") or item.get("note") or item.get("text") or item.get("name")
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


def _briefing_bullets(briefing: dict | None, limit_per_section: int = 4) -> list[dict]:
    sections = briefing.get("sections", {}) if briefing else {}
    if not isinstance(sections, dict):
        return []
    section_defs = [
        ("Macro trend", sections.get("macro_trend") or sections.get("macro_summary")),
        ("Today", sections.get("today")),
        ("This week", sections.get("this_week") or sections.get("upcoming_events")),
    ]
    result = []
    for label, value in section_defs:
        bullets = _list_values(value, limit_per_section)
        if bullets:
            result.append({"label": label, "bullets": bullets})
    return result


def _asset_drivers(note: dict, limit: int = 4) -> list[str]:
    for key in ("key_drivers", "drivers", "factors"):
        values = _list_values(note.get(key), limit)
        if values:
            return values
    return _list_values(note.get("summary") or note.get("note"), limit)


def _get_latest_prices(config: dict) -> dict:
    sql = """
        SELECT DISTINCT ON (symbol)
            symbol, close AS price, timestamp
        FROM market_data
        WHERE source IN ('oanda', 'demo') AND timeframe = 'PRICE'
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


def _get_dashboard_health() -> dict:
    try:
        return get_system_health()
    except Exception as exc:
        return {
            "overall": "unavailable",
            "error": str(exc),
            "components": [],
            "today_llm_cost_usd": 0,
            "today_token_count": 0,
        }


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
        events_data = get_events_upcoming_data(request=request, days=14)
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

    tz_context = timezone_context(request, config)
    now = datetime.now(tz_context["display_zone"])
    event_context = _event_template_context(
        events_data,
        config,
        display_zone=tz_context["display_zone"],
    )
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
        "last_cycle_status": _latest_cycle_status(config),
        "system_health": _get_dashboard_health(),
        "regime": regime,
        "briefing": briefing,
        "events_data": events_data,
        "indicators": indicators,
        "stale": indicators_stale,
        "stale_reason": indicators_stale_reason,
        "current_time": now,
        "timedelta": timedelta,
        "any_stale": any_stale,
        "dots": _section_dots(regime, events_data, briefing, indicators_stale, indicators_stale_reason),
        "briefing_bullets": _briefing_bullets(briefing),
        "price_map": price_map,
        "budget": get_budget_status(),
        "symbol_links": SYMBOL_LINKS,
        **tz_context,
        **event_context,
    }
    return templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/partials/system-health")
def partial_system_health(request: Request):
    templates = _get_templates(request)
    return templates.TemplateResponse(request, "partials/system_health.html", {
        "request": request,
        "system_health": _get_dashboard_health(),
    })


@router.get("/partials/header")
def partial_header(request: Request):
    config = load_config()
    templates = _get_templates(request)
    tz_context = timezone_context(request, config)
    now = datetime.now(tz_context["display_zone"])

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
        events_data = get_events_upcoming_data(request=request, days=14)
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
        "last_cycle_status": _latest_cycle_status(config),
        "current_time": now,
        "any_stale": any_stale,
        "system_health": _get_dashboard_health(),
        "budget": get_budget_status(),
        "symbol_links": SYMBOL_LINKS,
        **tz_context,
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
        "dot": _section_dots(regime, {}, None, False, None)["regime"],
    })


@router.get("/partials/events")
def partial_events(request: Request):
    config = load_config()
    templates = _get_templates(request)
    events_data = {}
    try:
        events_data = get_events_upcoming_data(request=request, days=14)
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

    return templates.TemplateResponse(request, "partials/events_section.html", {
        "request": request,
        "events_data": events_data,
        "current_time": now,
        "timedelta": timedelta,
        "dot": _section_dots({}, events_data, None, False, None)["events"],
        **tz_context,
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
        events = []
        try:
            events_data = get_events_upcoming_data(request=request, days=14)
            events = events_data.get("events", [])
            for ev in events:
                ev["scheduled_at"] = _parse_iso(ev.get("scheduled_at"))
        except Exception:
            events = []
        return templates.TemplateResponse(request, "partials/expansion_content.html", {
            "request": request,
            "note": note,
            "price": _get_latest_prices(config).get(symbol),
            "drivers": _asset_drivers(note),
            "matched_events": _matched_asset_events(symbol, events),
            "opinion_id": briefing.get("opinion_ids", [])[-1] if briefing.get("opinion_ids") else None,
            "symbol_links": SYMBOL_LINKS,
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
        "symbol_links": SYMBOL_LINKS,
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
            "dot": _freshness_dot(failed=True, title=str(exc)),
        })
    return templates.TemplateResponse(request, "partials/indicators_section.html", {
        "request": request,
        "indicators": indicators,
        "stale": stale,
        "stale_reason": stale_reason,
        "dot": _freshness_dot(stale=stale, title=stale_reason),
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
        "dot": _section_dots({}, {}, briefing, False, None)["briefing"],
        "briefing_bullets": _briefing_bullets(briefing),
    })
