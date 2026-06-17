from datetime import datetime, timezone

from config import load_config
from db import query_one
from logging_config import get_logger

logger = get_logger("budgets")


def get_budget_config(config: dict | None = None) -> dict:
    """Return budget config with defaults."""
    if config is None:
        config = load_config()
    return config.get("budgets", {
        "daily_llm_usd": 2.00,
        "warn_at_pct": 80,
    })


def get_today_spend(config: dict | None = None) -> tuple[float, int]:
    """Query processing_log for today's SUM(cost_usd) and SUM(tokens_input+tokens_output).

    Returns (cost_float, tokens_int). If no rows, returns (0.0, 0).
    """
    if config is None:
        config = load_config()

    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    row = query_one(
        "SELECT COALESCE(SUM(cost_usd), 0) as total_cost, "
        "COALESCE(SUM(tokens_input + tokens_output), 0) as total_tokens "
        "FROM processing_log WHERE started_at >= :today_start",
        params={"today_start": today_start},
        config=config,
    )

    if row is None:
        return 0.0, 0

    cost = float(row.get("total_cost", 0) or 0)
    tokens = int(row.get("total_tokens", 0) or 0)
    return cost, tokens


def get_budget_status(config: dict | None = None) -> dict:
    """Combine budget config + today's spend into a status dict."""
    if config is None:
        config = load_config()

    budget_cfg = get_budget_config(config)
    daily_cap = float(budget_cfg.get("daily_llm_usd", 2.00))
    warn_pct = int(budget_cfg.get("warn_at_pct", 80))

    today_cost, today_tokens = get_today_spend(config)

    unlimited = daily_cap <= 0
    if unlimited:
        usage_pct = 0.0
        warning = False
        exceeded = False
    else:
        usage_pct = round((today_cost / daily_cap) * 100, 2)
        exceeded = today_cost > daily_cap
        warning = not exceeded and usage_pct >= warn_pct

    logger.debug("budget_status", today_cost_usd=round(today_cost, 6), usage_pct=usage_pct, warning=warning, exceeded=exceeded, unlimited=unlimited)

    return {
        "today_cost_usd": round(today_cost, 6),
        "today_tokens": today_tokens,
        "budget_cap_usd": daily_cap,
        "unlimited": unlimited,
        "warn_at_pct": warn_pct,
        "usage_pct": usage_pct,
        "warning": warning,
        "exceeded": exceeded,
    }
