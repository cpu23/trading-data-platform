"""UTC-day LLM budget display adapter.

A non-positive cap retains the documented unlimited semantics. Positive caps
are exceeded at ``recorded spend >= cap``. This is a recorded-spend check, not a
projected-cost reservation for concurrent requests.
"""

import json
import math
from datetime import datetime, timedelta, timezone

from config import load_config
from db import get_session, query_one
from logging_config import get_logger
from sqlalchemy import text

logger = get_logger("budgets")
DEFAULT_DAILY_LLM_USD = 2.0
DEFAULT_WARN_AT_PCT = 80


def _finite_number(value, name: str) -> float:
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
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def get_budget_config(config: dict | None = None) -> tuple[float, float]:
    if config is None:
        config = load_config()
    configured = config.get("budgets", {})
    cap = _finite_number(
        configured.get("daily_llm_usd", DEFAULT_DAILY_LLM_USD),
        "budgets.daily_llm_usd",
    )
    warn_at = _finite_number(
        configured.get("warn_at_pct", DEFAULT_WARN_AT_PCT), "budgets.warn_at_pct"
    )
    if not 0 <= warn_at <= 100:
        raise ValueError("budgets.warn_at_pct must be between 0 and 100")
    return cap, warn_at


def get_today_spend(
    config: dict | None = None, *, now: datetime | None = None
) -> tuple[float, int]:
    if config is None:
        config = load_config()
    today_start, tomorrow_start = utc_day_bounds(now)
    row = query_one(
        "SELECT COALESCE(SUM(COALESCE(cost_usd, 0)), 0) AS total_cost, "
        "COALESCE(SUM(COALESCE(tokens_input, 0) + COALESCE(tokens_output, 0)), 0) "
        "AS total_tokens FROM processing_log "
        "WHERE started_at >= :today_start AND started_at < :tomorrow_start",
        params={"today_start": today_start, "tomorrow_start": tomorrow_start},
        config=config,
    )
    if row is None:
        return 0.0, 0
    return float(row.get("total_cost") or 0), int(row.get("total_tokens") or 0)


def budget_status(
    today_cost: float, cap, warn_at_pct: float = DEFAULT_WARN_AT_PCT
) -> dict:
    daily_cap = _finite_number(cap, "budgets.daily_llm_usd")
    cost = _finite_number(today_cost, "today_cost")
    warn_at = _finite_number(warn_at_pct, "budgets.warn_at_pct")
    if not 0 <= warn_at <= 100:
        raise ValueError("budgets.warn_at_pct must be between 0 and 100")
    unlimited = daily_cap <= 0
    if unlimited:
        usage_pct, warning, exceeded = 0.0, False, False
        remaining_usd = None
    else:
        usage_pct = round((cost / daily_cap) * 100, 2)
        exceeded = cost >= daily_cap
        warning = not exceeded and usage_pct >= warn_at
        remaining_usd = round(max(daily_cap - cost, 0.0), 6)
    return {
        "budget_cap_usd": daily_cap,
        "unlimited": unlimited,
        "warn_at_pct": warn_at,
        "usage_pct": usage_pct,
        "warning": warning,
        "exceeded": exceeded,
        "hard_limit_reached": exceeded,
        "paid_calls_allowed": unlimited or not exceeded,
        "remaining_usd": remaining_usd,
    }


def _unavailable(status: str, cap=None, warn_at=None) -> dict:
    return {
        "today_cost_usd": None,
        "today_tokens": None,
        "budget_cap_usd": cap,
        "unlimited": False,
        "warn_at_pct": warn_at,
        "usage_pct": None,
        "warning": False,
        "exceeded": False,
        "available": False,
        "status": status,
    }


def get_budget_status(config: dict | None = None) -> dict:
    if config is None:
        config = load_config()
    try:
        daily_cap, warn_at = get_budget_config(config)
    except ValueError:
        logger.warning("budget_config_invalid", blocked_code="daily_llm_budget_unavailable")
        return _unavailable("invalid_config")
    try:
        today_cost, today_tokens = get_today_spend(config)
        status = budget_status(today_cost, daily_cap, warn_at)
    except Exception:
        logger.warning("budget_status_unavailable", blocked_code="daily_llm_budget_unavailable")
        return _unavailable("unavailable", daily_cap, warn_at)
    result = {
        "today_cost_usd": round(today_cost, 6),
        "today_tokens": today_tokens,
        **status,
        "available": True,
        "status": "unlimited" if status["unlimited"] else "exceeded" if status["exceeded"] else "warning" if status["warning"] else "ok",
    }
    logger.debug(
        "budget_status",
        today_cost_usd=result["today_cost_usd"],
        usage_pct=result["usage_pct"],
        warning=result["warning"],
        exceeded=result["exceeded"],
        unlimited=result["unlimited"],
    )
    return result




def register_manual_override(
    correlation_id: str,
    run_kind: str,
    requested_component: str | None,
    reason: str,
    requested_by: str,
    config: dict | None = None,
) -> dict:
    """Persist an auditable, one-run budget override without a schema change."""
    if config is None:
        config = load_config()

    override = {
        "requested": True,
        "reason": reason,
        "requested_by": requested_by,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "scope": "one_run",
        "run_kind": run_kind,
        "requested_component": requested_component,
    }
    summary = json.dumps({"budget_override": override})

    with get_session(config) as session:
        session.execute(
            text(
                "INSERT INTO cycle_runs "
                "(correlation_id, status, started_at, triggered_by, run_kind, "
                "requested_component, summary) "
                "VALUES (:cid, 'running', :started_at, 'api_manual_override', "
                ":run_kind, :component, CAST(:summary AS JSONB)) "
                "ON CONFLICT (correlation_id) DO UPDATE SET "
                "status = 'running', triggered_by = 'api_manual_override', "
                "run_kind = EXCLUDED.run_kind, "
                "requested_component = EXCLUDED.requested_component, "
                "summary = cycle_runs.summary || EXCLUDED.summary"
            ),
            {
                "cid": correlation_id,
                "started_at": datetime.now(timezone.utc),
                "run_kind": run_kind,
                "component": requested_component,
                "summary": summary,
            },
        )

    logger.warning(
        "budget_override_registered",
        correlation_id=correlation_id,
        run_kind=run_kind,
        requested_component=requested_component,
        requested_by=requested_by,
        reason=reason,
    )
    return override


def mark_override_dispatch_failed(
    correlation_id: str,
    error: str,
    config: dict | None = None,
) -> None:
    """Close an override audit record when dispatch never reached the orchestrator."""
    if config is None:
        config = load_config()

    with get_session(config) as session:
        session.execute(
            text(
                "UPDATE cycle_runs SET status = 'failed', result_status = 'failed', "
                "completed_at = :completed_at, error_message = :error, "
                "summary = jsonb_set("
                "COALESCE(summary, '{}'::jsonb), "
                "'{budget_override,dispatch_failed_at}', "
                "to_jsonb(CAST(:dispatch_failed_at AS TEXT)), true"
                ") WHERE correlation_id = :cid"
            ),
            {
                "cid": correlation_id,
                "completed_at": datetime.now(timezone.utc),
                "dispatch_failed_at": datetime.now(timezone.utc).isoformat(),
                "error": error,
            },
        )
