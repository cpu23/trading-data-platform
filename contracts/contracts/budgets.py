"""Pure, dependency-light UTC-day budget math and constants.

Shared by the API and orchestrator without importing database or service
dependencies. All calculations operate on finite numbers and UTC calendar day
boundaries.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_DAILY_LLM_USD: float = 2.0
DEFAULT_WARN_AT_PCT: float = 80.0
DEFAULT_RESERVATION_TTL_SECONDS: float = 600.0
BUDGET_OVERRIDE_TTL_SECONDS: int = 600


def coerce_finite_number(value: Any, name: str) -> float:
    """Validate and coerce a finite float value; raise ValueError on invalid input."""
    if value is None or isinstance(value, (bool, str)):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number



def utc_day_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return half-open [start_of_day, start_of_next_day) UTC datetime bounds."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(UTC)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def get_budget_config(
    config: Mapping[str, Any] | None = None,
) -> tuple[float, float]:
    """Extract and validate (daily_cap_usd, warn_at_pct) from config mapping."""
    if config is None:
        configured: Mapping[str, Any] = {}
    elif isinstance(config, Mapping):
        if "budgets" in config:
            candidate = config["budgets"]
            if isinstance(candidate, Mapping):
                configured = candidate
            elif candidate is None:
                configured = {}
            else:
                raise ValueError("budgets must be an object")
        else:
            configured = config
    else:
        configured = {}
    cap = coerce_finite_number(
        configured.get("daily_llm_usd", DEFAULT_DAILY_LLM_USD),
        "budgets.daily_llm_usd",
    )
    # A negative cap is malformed, never unlimited; a zero cap denies all
    # paid calls (fail closed, no unlimited mode).
    if cap < 0:
        raise ValueError("budgets.daily_llm_usd must be non-negative")
    warn_at = coerce_finite_number(
        configured.get("warn_at_pct", DEFAULT_WARN_AT_PCT),
        "budgets.warn_at_pct",
    )
    if not 0 <= warn_at <= 100:
        raise ValueError("budgets.warn_at_pct must be between 0 and 100")
    return cap, warn_at


def budget_status(
    today_cost: float,
    cap: float,
    warn_at_pct: float = DEFAULT_WARN_AT_PCT,
) -> dict[str, Any]:
    """Calculate budget consumption flags and percentages for display/enforcement."""
    daily_cap = coerce_finite_number(cap, "budgets.daily_llm_usd")
    cost = coerce_finite_number(today_cost, "today_cost")
    warn_at = coerce_finite_number(warn_at_pct, "budgets.warn_at_pct")
    if not 0 <= warn_at <= 100:
        raise ValueError("budgets.warn_at_pct must be between 0 and 100")
    if daily_cap < 0:
        raise ValueError("budgets.daily_llm_usd must be non-negative")
    # A zero cap denies all paid calls (fail closed); no unlimited mode.
    if daily_cap == 0:
        return {
            "budget_cap_usd": daily_cap,
            "unlimited": False,
            "warn_at_pct": warn_at,
            "usage_pct": 0.0,
            "warning": False,
            "exceeded": True,
            "hard_limit_reached": True,
            "paid_calls_allowed": False,
            "remaining_usd": 0.0,
        }
    usage_pct = round((cost / daily_cap) * 100, 2)
    exceeded = cost >= daily_cap
    warning = not exceeded and usage_pct >= warn_at
    remaining_usd = round(max(daily_cap - cost, 0.0), 6)
    return {
        "budget_cap_usd": daily_cap,
        "unlimited": False,
        "warn_at_pct": warn_at,
        "usage_pct": usage_pct,
        "warning": warning,
        "exceeded": exceeded,
        "hard_limit_reached": exceeded,
        "paid_calls_allowed": not exceeded,
        "remaining_usd": remaining_usd,
    }

__all__ = [
    "BUDGET_OVERRIDE_TTL_SECONDS",
    "DEFAULT_DAILY_LLM_USD",
    "DEFAULT_RESERVATION_TTL_SECONDS",
    "DEFAULT_WARN_AT_PCT",
    "budget_status",
    "coerce_finite_number",
    "get_budget_config",
    "utc_day_bounds",
]
