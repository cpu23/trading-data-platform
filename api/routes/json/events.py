import json
from datetime import datetime, time as dt_time, timezone, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query

from config import load_config
from db import query_one, query_many
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


def _serialize_event(row: dict, window: dict) -> dict:
    scheduled_at = row["scheduled_at"]
    london_dt = scheduled_at.astimezone(window["london"])
    ny_dt = scheduled_at.astimezone(window["ny"])
    metadata = _metadata_value(row.get("metadata"))
    currency = metadata.get("currency") or COUNTRY_TO_CURRENCY.get(
        row.get("country"), row.get("country")
    )
    return {
        "event_id": row["event_id"],
        "event_name": row["event_name"],
        "country": row["country"],
        "currency": currency,
        "scheduled_at": scheduled_at.isoformat() if hasattr(scheduled_at, "isoformat") else str(scheduled_at),
        "day_key": london_dt.date().isoformat(),
        "london_time": london_dt.strftime("%H:%M"),
        "ny_time": ny_dt.strftime("%H:%M"),
        "time_display": f"{window['london_label']} {london_dt.strftime('%H:%M')} / {window['ny_label']} {ny_dt.strftime('%H:%M')}",
        "impact_level": row.get("impact_level"),
        "consensus": row.get("consensus"),
        "previous": row.get("previous"),
        "actual": row.get("actual"),
        "source": row.get("source"),
    }


@router.get("/events/upcoming")
def get_events_upcoming(days: int = Query(default=14, ge=1, le=90)):
    config = load_config()
    thresholds = get_staleness_config(config)
    window = _event_window(config)

    sql = """
        SELECT event_id, event_name, country, scheduled_at, impact_level,
               consensus, previous, actual, source, metadata
        FROM econ_events
        WHERE scheduled_at >= :start
          AND scheduled_at <= :end
          AND lower(COALESCE(impact_level, '')) IN ('high', 'medium')
          AND (
              country IN ('US', 'EU', 'GB', 'JP', 'AU', 'CN')
              OR metadata ->> 'currency' IN ('USD', 'EUR', 'GBP', 'JPY', 'AUD', 'CNY')
          )
        ORDER BY scheduled_at ASC
    """
    rows = query_many(
        sql,
        params={
            "start": window["period_start"].astimezone(timezone.utc),
            "end": _window_end_for_days(window, days).astimezone(timezone.utc),
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

    events = [_serialize_event(row, window) for row in rows]
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

    now = datetime.now(timezone.utc)
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
        events.append({
            "event_id": row["event_id"],
            "event_name": row["event_name"],
            "country": row["country"],
            "scheduled_at": scheduled_at.isoformat() if hasattr(scheduled_at, "isoformat") else str(scheduled_at),
            "impact_level": row.get("impact_level"),
            "consensus": row.get("consensus"),
            "previous": row.get("previous"),
            "actual": row.get("actual"),
            "source": row.get("source"),
        })

    return {
        "events": events,
        "days": days,
    }
