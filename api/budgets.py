"""UTC-day LLM budget display adapter.

A zero cap denies all paid calls (fail closed; there is no unlimited mode);
negative or malformed caps fail closed as unavailable. Positive caps are
exceeded at committed spend (per-correlation reconciled ``processing_log``
cost minus settled reservation actuals, plus active reservation estimates plus
settled reservation actuals anchored to their day) reaching the cap — the same
admission semantics the orchestrator worker enforces, so the UI never reports
a paid call as allowed when the worker would deny it.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import text

from config import load_config
from contracts.budgets import (
    BUDGET_OVERRIDE_TTL_SECONDS,
    budget_status,
    utc_day_bounds,
)
from contracts.budgets import (
    get_budget_config as _contract_get_budget_config,
)
from db import get_session, query_one
from logging_config import get_logger

logger = get_logger("budgets")


def get_budget_config(config: dict | None = None) -> tuple[float, float]:
    if config is None:
        config = load_config()
    return _contract_get_budget_config(config)

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
        logger.warning(
            "budget_config_invalid", blocked_code="daily_llm_budget_unavailable"
        )
        return _unavailable("invalid_config")
    try:
        today_cost, today_tokens = get_today_spend(config)
        day_start, day_end = utc_day_bounds()
        row = query_one(
            "SELECT "
            "(SELECT COALESCE(SUM(unreserved), 0) FROM ( "
            "  SELECT GREATEST( "
            "    COALESCE(p.total_cost, 0) - COALESCE(r.total_settled, 0), 0) "
            "    AS unreserved "
            "  FROM ( "
            "    SELECT correlation_id, processor, "
            "           SUM(COALESCE(cost_usd, 0)) AS total_cost "
            "    FROM processing_log "
            "    WHERE started_at >= :day_start AND started_at < :day_end "
            "    GROUP BY correlation_id, processor "
            "  ) p "
            "  LEFT JOIN ( "
            "    SELECT correlation_id, processor, "
            "           SUM(COALESCE(settled_usd, estimated_usd)) AS total_settled "
            "    FROM budget_reservations "
            "    WHERE status = 'settled' AND settled_at IS NOT NULL "
            "    GROUP BY correlation_id, processor "
            "  ) r ON p.correlation_id IS NOT DISTINCT FROM r.correlation_id "
            "     AND p.processor = r.processor "
            ") pairs) AS spent_usd, "
            "( "
            "  (SELECT COALESCE(SUM(estimated_usd), 0) FROM budget_reservations "
            "   WHERE budget_day = :day AND status = 'active' "
            "     AND expires_at > :now) "
            "  + "
            "  (SELECT COALESCE(SUM(COALESCE(settled_usd, estimated_usd)), 0) "
            "   FROM budget_reservations "
            "   WHERE budget_day = :day AND status = 'settled' "
            "     AND settled_at IS NOT NULL) "
            ") AS reserved_usd",
            params={
                "day_start": day_start,
                "day_end": day_end,
                "day": day_start.date(),
                "now": datetime.now(UTC),
            },
            config=config,
        )
        spent_usd = float(row.get("spent_usd") or 0) if row else 0.0
        reserved_usd = float(row.get("reserved_usd") or 0) if row else 0.0
        committed = spent_usd + reserved_usd
        status = budget_status(committed, daily_cap, warn_at)
    except Exception:
        logger.warning(
            "budget_status_unavailable", blocked_code="daily_llm_budget_unavailable"
        )
        return _unavailable("unavailable", daily_cap, warn_at)
    result = {
        "today_cost_usd": round(today_cost, 6),
        "today_tokens": today_tokens,
        "unreserved_spend_usd": round(spent_usd, 6),
        "reserved_usd": round(reserved_usd, 6),
        "committed_usd": round(committed, 6),
        **status,
        "available": True,
        "status": "unlimited"
        if status["unlimited"]
        else "exceeded"
        if status["exceeded"]
        else "warning"
        if status["warning"]
        else "ok",
    }
    logger.debug(
        "budget_status",
        today_cost_usd=result["today_cost_usd"],
        reserved_usd=result["reserved_usd"],
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

    requested_at = datetime.now(UTC)
    override = {
        "requested": True,
        "reason": reason,
        "requested_by": requested_by,
        "requested_at": requested_at.isoformat(),
        "expires_at": (
            requested_at + timedelta(seconds=BUDGET_OVERRIDE_TTL_SECONDS)
        ).isoformat(),
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
                "started_at": datetime.now(UTC),
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
                "completed_at": datetime.now(UTC),
                "dispatch_failed_at": datetime.now(UTC).isoformat(),
                "error": error,
            },
        )


def extract_manual_override(
    body: Any, request: Request | None = None
) -> dict[str, str] | None:
    """Extract and validate manual budget override from a request body."""
    if hasattr(body, "model_dump"):
        body = body.model_dump()
    if not isinstance(body, dict) or "budget_override" not in body:
        return None
    if not isinstance(body["budget_override"], bool):
        raise HTTPException(
            status_code=422, detail="budget_override must be a boolean"
        )
    if body["budget_override"] is not True:
        return None

    reason = body.get("override_reason")
    if not isinstance(reason, str):
        raise HTTPException(
            status_code=422, detail="override_reason must be a string"
        )
    reason = reason.strip()
    if not 1 <= len(reason) <= 500:
        raise HTTPException(
            status_code=422,
            detail="override_reason must be between 1 and 500 characters",
        )

    client_host = (
        request.client.host
        if request and getattr(request, "client", None)
        else "unknown"
    )
    return {
        "reason": reason,
        "requested_by": f"authenticated_api_user@{client_host}",
    }


def enforce_api_budget(
    budget: dict[str, Any] | None,
    override: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Enforce a resolved daily LLM budget before dispatching paid workloads.

    Fails closed on missing or unavailable budget status (HTTP 503) unless an
    explicit manual override is present. Fails closed with HTTP 429 when budget
    is exhausted unless overridden.
    """
    if not isinstance(budget, dict) or budget.get("available") is not True:
        if override is None:
            logger.warning(
                "paid_trigger_budget_unavailable",
                status=budget.get("status") if isinstance(budget, dict) else None,
            )
            raise HTTPException(
                status_code=503,
                detail="Daily LLM budget status unavailable",
            )
        return budget or {}
    if not budget.get("paid_calls_allowed", False) and override is None:
        logger.warning(
            "paid_trigger_budget_denied",
            today_cost_usd=budget.get("today_cost_usd"),
            budget_cap_usd=budget.get("budget_cap_usd"),
        )
        raise HTTPException(
            status_code=429,
            detail={
                "message": "Daily LLM budget reached",
                "budget": budget,
                "override": {
                    "supported": True,
                    "body_fields": {
                        "budget_override": True,
                        "override_reason": "required, non-empty string",
                    },
                },
            },
        )
    return budget
