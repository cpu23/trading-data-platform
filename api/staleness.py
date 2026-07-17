from datetime import datetime, timezone

from config import load_config


def is_stale(
    timestamp: datetime | None,
    threshold_hours: float,
) -> tuple[bool, str | None]:
    if timestamp is None:
        return True, "No data available"

    now = datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    age_hours = (now - timestamp).total_seconds() / 3600

    if age_hours > threshold_hours:
        hours_int = int(age_hours)
        return True, f"Data is {hours_int} hours old"
    return False, None


def get_staleness_config(config: dict | None = None) -> dict:
    if config is None:
        config = load_config()
    return config.get("dashboard", {}).get("stale_thresholds", {
        "briefing_hours": 18,
        "regime_hours": 18,
        "macro_hours": 30,
        "events_hours": 8,
    })