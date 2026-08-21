"""Shared economic-calendar event shaping for market surfaces.

Owned jointly by the full economic calendar (``routes.views.markets``) and
the asset drawer compatibility partial (``routes.views.dashboard``): ISO
timestamp parsing, stale-reason formatting, and asset/event matching for
watchlist symbols.  Neither consumer imports private helpers from the other;
this module is the single home for these transforms (no duplication).
"""

from datetime import UTC, datetime

from routes.views.asset_rules import ASSET_EVENT_RULES


def parse_iso(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


def format_stale_reason(stale_reason: str | None, section: str) -> str | None:
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
    scheduled = parse_iso(event.get("scheduled_at"))
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


def with_event_display(event: dict) -> dict:
    enriched = dict(event)
    enriched["day_time_display"] = _event_day_time_display(event)
    return enriched


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


def matched_asset_events(
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

    return [with_event_display(event) for event in selected[:limit]]
