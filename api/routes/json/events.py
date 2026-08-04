import json
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request

from config import load_config
from db import query_many, query_one
from routes.json.settings import timezone_context
from staleness import get_staleness_config, is_stale

router = APIRouter()

RELEVANT_COUNTRIES = ("US", "EU", "GB", "JP", "AU", "CN")
RELEVANT_CURRENCIES = ("USD", "EUR", "GBP", "JPY", "AUD", "CNY")
COUNTRY_TO_CURRENCY = {
    "US": "USD",
    "EU": "EUR",
    "GB": "GBP",
    "JP": "JPY",
    "AU": "AUD",
    "CN": "CNY",
}


def _bounded_positive_integer(raw: str, *, maximum: int, name: str) -> int:
    if not raw.isascii() or not raw.isdecimal():
        raise HTTPException(
            status_code=422, detail=f"{name} must be a positive integer"
        )
    value = int(raw)
    if value < 1:
        raise HTTPException(status_code=422, detail=f"{name} must be at least 1")
    return min(value, maximum)


@router.get("/calendar/events")
def get_calendar_events(
    request: Request,
    hours: str = Query(default="24"),
    limit: str = Query(default="100"),
):
    normalized_hours = _bounded_positive_integer(hours, maximum=168, name="hours")
    normalized_limit = _bounded_positive_integer(limit, maximum=500, name="limit")
    config = load_config()
    start = datetime.now(UTC)
    end = start + timedelta(hours=normalized_hours)
    rows = query_many(
        """SELECT event_id, event_name, country, scheduled_at, impact_level,
                  consensus, previous, actual, source, metadata
           FROM econ_events
           WHERE scheduled_at >= :start AND scheduled_at <= :end
           ORDER BY scheduled_at ASC
           LIMIT :limit""",
        params={"start": start, "end": end, "limit": normalized_limit},
        config=config,
    )
    display_zone = timezone_context(request, config)["display_zone"]
    events = []
    for row in rows:
        event = dict(row)
        scheduled = event.get("scheduled_at")
        if isinstance(scheduled, datetime):
            event["scheduled_at"] = scheduled.isoformat()
            event["display_time"] = scheduled.astimezone(display_zone).isoformat()
        else:
            event["scheduled_at"] = str(scheduled)
            event["display_time"] = event["scheduled_at"]
        events.append(event)
    return {"events": events, "hours": normalized_hours, "limit": normalized_limit}


def _timezone_config(config: dict) -> dict:
    primary = config.get("timezone", {}).get("primary", {})
    secondary = config.get("timezone", {}).get("secondary", {})
    return {
        "london": ZoneInfo(primary.get("name", "Europe/London")),
        "ny": ZoneInfo(secondary.get("name", "America/New_York")),
        "london_label": primary.get("label", "London"),
        "ny_label": secondary.get("label", "NY"),
    }


def _event_window(config: dict) -> dict:
    tz = _timezone_config(config)
    now_london = datetime.now(tz["london"])
    today = now_london.date()
    if now_london.weekday() >= 5:
        monday = today + timedelta(days=7 - now_london.weekday())
        start_date = monday
    else:
        monday = today - timedelta(days=today.weekday())
        start_date = today
    friday = monday + timedelta(days=4)
    return {
        **tz,
        "today": today,
        "period_start": datetime.combine(start_date, dt_time.min, tzinfo=tz["london"]),
        "period_end": datetime.combine(friday, dt_time.max, tzinfo=tz["london"]),
    }


def _window_end_for_days(window: dict, days: int) -> datetime:
    requested_end = window["period_start"] + timedelta(days=days)
    return min(window["period_end"], requested_end)


def _metadata_value(metadata):
    if isinstance(metadata, dict):
        return metadata
    if isinstance(metadata, str):
        try:
            return json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _serialize_event(
    row: dict,
    window: dict,
    *,
    display_zone: ZoneInfo,
    display_timezone: str,
) -> dict:
    scheduled_at = row["scheduled_at"]
    london_dt = scheduled_at.astimezone(window["london"])
    ny_dt = scheduled_at.astimezone(window["ny"])
    display_dt = scheduled_at.astimezone(display_zone)
    metadata = _metadata_value(row.get("metadata"))
    currency = metadata.get("currency") or COUNTRY_TO_CURRENCY.get(
        row.get("country"), row.get("country")
    )
    return {
        "event_id": row["event_id"],
        "event_name": row["event_name"],
        "country": row["country"],
        "currency": currency,
        "scheduled_at": scheduled_at.isoformat()
        if hasattr(scheduled_at, "isoformat")
        else str(scheduled_at),
        "day_key": display_dt.date().isoformat(),
        "day_label_short": display_dt.strftime("%a"),
        "london_time": london_dt.strftime("%H:%M"),
        "ny_time": ny_dt.strftime("%H:%M"),
        "time_display": display_dt.strftime("%H:%M"),
        "display_timezone": display_timezone,
        "impact_level": row.get("impact_level"),
        "consensus": row.get("consensus"),
        "previous": row.get("previous"),
        "actual": row.get("actual"),
        "source": row.get("source"),
    }


@router.get("/events/upcoming")
def get_events_upcoming(
    request: Request,
    days: int = Query(default=14, ge=1, le=90),
):
    return get_events_upcoming_data(request=request, days=days)


def get_events_upcoming_data(*, request: Request, days: int = 14) -> dict:
    config = load_config()
    display = timezone_context(request, config)
    thresholds = get_staleness_config(config)
    window = _event_window(config)

    sql = """
        SELECT event_id, event_name, country, scheduled_at, impact_level,
               consensus, previous, actual, source, metadata
        FROM econ_events
        WHERE scheduled_at >= :start
          AND scheduled_at <= :end
          AND lower(COALESCE(impact_level, '')) IN ('high', 'medium', 'low')
          AND (
              country IN ('US', 'EU', 'GB', 'JP', 'AU', 'CN')
              OR metadata ->> 'currency' IN ('USD', 'EUR', 'GBP', 'JPY', 'AUD', 'CNY')
          )
        ORDER BY scheduled_at ASC
    """
    rows = query_many(
        sql,
        params={
            "start": window["period_start"].astimezone(UTC),
            "end": _window_end_for_days(window, days).astimezone(UTC),
        },
        config=config,
    )

    last_run_sql = """
        SELECT started_at FROM collection_log
        WHERE collector = 'forex_factory'
        ORDER BY started_at DESC LIMIT 1
    """
    last_run = query_one(last_run_sql, config=config)

    stale, stale_reason = is_stale(
        last_run["started_at"] if last_run else None,
        thresholds["events_hours"],
    )

    events = [
        _serialize_event(
            row,
            window,
            display_zone=display["display_zone"],
            display_timezone=display["current_timezone"],
        )
        for row in rows
    ]
    grouped = {}
    for event in events:
        grouped.setdefault(event["day_key"], []).append(event)

    return {
        "events": events,
        "grouped": grouped,
        "window": {
            "today": window["today"].isoformat(),
            "period_start": window["period_start"].isoformat(),
            "period_end": _window_end_for_days(window, days).isoformat(),
        },
        "stale": stale,
        "stale_reason": stale_reason,
        "days": days,
    }


@router.get("/events/recent")
def get_events_recent(days: int = Query(default=7, ge=1, le=90)):
    config = load_config()

    now = datetime.now(UTC)
    start = now - timedelta(days=days)

    sql = """
        SELECT event_id, event_name, country, scheduled_at, impact_level,
               consensus, previous, actual, source
        FROM econ_events
        WHERE scheduled_at >= :start AND scheduled_at <= :end
        ORDER BY scheduled_at DESC
    """
    rows = query_many(sql, params={"start": start, "end": now}, config=config)

    events = []
    for row in rows:
        scheduled_at = row["scheduled_at"]
        events.append(
            {
                "event_id": row["event_id"],
                "event_name": row["event_name"],
                "country": row["country"],
                "scheduled_at": scheduled_at.isoformat()
                if hasattr(scheduled_at, "isoformat")
                else str(scheduled_at),
                "impact_level": row.get("impact_level"),
                "consensus": row.get("consensus"),
                "previous": row.get("previous"),
                "actual": row.get("actual"),
                "source": row.get("source"),
            }
        )

    return {
        "events": events,
        "days": days,
    }
