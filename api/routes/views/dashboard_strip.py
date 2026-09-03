"""Dashboard compact top strip: session label, regime, next catalyst.

Dashboard-focused module owning the lean dashboard's compact session
snapshot — the ``/partials/dashboard/top-strip`` partial — and the shared
session/catalyst time primitives it needs (display timezone, ISO
normalization, countdown display).  ``cockpit_panels`` imports
``primary_zone``/``as_datetime``/``iso``/``time_display``/``countdown_display``
from here for its catalysts loader and panels; no dashboard backend depends
on private cockpit helpers.

The compact strip loads only the session label, the current regime, and the
single next catalyst — never last price, last material event, direction
chips, source health, or budget (lean dashboard contract).  Every sub-fetch
is isolated fail-soft: one unavailable source degrades only its own field.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request

import config as app_config
from db import query_one
from logging_config import get_logger
from routes.json.regime import get_regime_current

router = APIRouter()
logger = get_logger("dashboard.strip")


_NEXT_CATALYST_SQL = """
    SELECT event_id, event_name, country, scheduled_at, source, metadata
    FROM econ_events
    WHERE scheduled_at >= :now
      AND lower(COALESCE(impact_level, '')) = 'high'
    ORDER BY scheduled_at ASC
    LIMIT 1
"""


def primary_zone(config: dict) -> ZoneInfo:
    name = config.get("timezone", {}).get("primary", {}).get("name", "Europe/London")
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Europe/London")


def session_label_for_hour(hour: int) -> str:
    """Deterministic session label for the display-timezone hour.

    Asia covers the Tokyo window, London the European morning/afternoon
    overlap, New York the US afternoon window.
    """
    if 0 <= hour < 8:
        return "Asia"
    if 8 <= hour < 17:
        return "London"
    return "New York"


def as_datetime(value):
    """Normalize a datetime or ISO string to an aware UTC datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def iso(value) -> str | None:
    parsed = as_datetime(value)
    return parsed.isoformat() if parsed else None


def time_display(value) -> str | None:
    parsed = as_datetime(value)
    return parsed.strftime("%d %b %H:%M UTC") if parsed else None


def countdown_display(minutes) -> str | None:
    if minutes is None:
        return None
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60}m"




def load_compact_strip(config: dict) -> dict:
    """Compact session snapshot for the lean dashboard strip.

    Sources only the session label, current regime, and the single next
    catalyst (contract: the dashboard strip must not load last price, last
    material event, direction chips, source health, or budget).  Every
    sub-fetch is isolated fail-soft.
    """
    strip: dict = {
        "available": False,
        "session_available": False,
        "session_label": None,
        "regime_available": False,
        "regime": None,
        "next_catalyst_available": False,
        "next_catalyst": None,
    }
    try:
        strip["session_label"] = session_label_for_hour(
            datetime.now(primary_zone(config)).hour
        )
        strip["session_available"] = True
    except Exception as exc:
        logger.warning(
            "dashboard_strip_session_unavailable",
            error_type=type(exc).__name__,
        )

    try:
        regime = get_regime_current()
        if isinstance(regime, dict):
            strip["regime"] = {
                "regime": regime.get("regime"),
                "sub_regime": regime.get("sub_regime"),
                "confidence": regime.get("confidence"),
                "created_at": regime.get("created_at"),
            }
        strip["regime_available"] = True
    except Exception as exc:
        logger.warning(
            "dashboard_strip_regime_unavailable",
            error_type=type(exc).__name__,
        )

    try:
        now = datetime.now(UTC)
        row = query_one(_NEXT_CATALYST_SQL, params={"now": now}, config=config)
        if row:
            scheduled = as_datetime(row.get("scheduled_at"))
            minutes = None
            if scheduled is not None:
                minutes = max(
                    0, int((scheduled - datetime.now(UTC)).total_seconds() // 60)
                )
            strip["next_catalyst"] = {
                "event_name": row.get("event_name"),
                "country": row.get("country"),
                "scheduled_at": iso(scheduled),
                "countdown_minutes": minutes,
                "countdown_display": countdown_display(minutes),
            }
        strip["next_catalyst_available"] = True
    except Exception as exc:
        logger.warning(
            "dashboard_strip_catalyst_unavailable",
            error_type=type(exc).__name__,
        )

    strip["available"] = any(
        strip[key]
        for key in (
            "session_available",
            "regime_available",
            "next_catalyst_available",
        )
    )
    return strip


@router.get("/partials/dashboard/top-strip")
def partial_top_strip(request: Request):
    config = app_config.load_config()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "partials/top_strip.html",
        {
            "request": request,
            "strip": load_compact_strip(config),
            "live_updates_enabled": app_config.live_updates_enabled(config),
        },
    )
