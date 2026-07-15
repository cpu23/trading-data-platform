"""UTC-day LLM spending policy and orchestrator enforcement.

The guard uses recorded ``processing_log`` spend only. It does not reserve a
projected request cost, so concurrent requests can pass while recorded spend is
below the cap. Once recorded spend is greater than or equal to the cap, later
automatic stages are blocked.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from db import get_session
from logging_config import get_logger
from sqlalchemy import text

logger = get_logger("budgets")
DEFAULT_DAILY_LLM_USD = 2.0
DEFAULT_WARN_AT_PCT = 80
_TRUSTED_MANUAL_AUTHORIZATION = object()
_BUDGET_PERMIT = object()


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


def get_budget_config(config: dict) -> tuple[float, float]:
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
    config: dict, *, now: datetime | None = None
) -> tuple[float, int]:
    today_start, tomorrow_start = utc_day_bounds(now)
    sql = text(
        "SELECT COALESCE(SUM(COALESCE(cost_usd, 0)), 0) AS total_cost, "
        "COALESCE(SUM(COALESCE(tokens_input, 0) + COALESCE(tokens_output, 0)), 0) "
        "AS total_tokens FROM processing_log "
        "WHERE started_at >= :today_start AND started_at < :tomorrow_start"
    )
    with get_session(config) as session:
        row = session.execute(
            sql, {"today_start": today_start, "tomorrow_start": tomorrow_start}
        ).mappings().one_or_none()
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
    else:
        usage_pct = round((cost / daily_cap) * 100, 2)
        exceeded = cost >= daily_cap
        warning = not exceeded and usage_pct >= warn_at
    return {
        "budget_cap_usd": daily_cap,
        "unlimited": unlimited,
        "warn_at_pct": warn_at,
        "usage_pct": usage_pct,
        "warning": warning,
        "exceeded": exceeded,
    }


class BudgetBlock(RuntimeError):
    code = "daily_llm_budget_blocked"
    safe_reason = "daily LLM budget blocked"

    def __init__(self, *, processor: str = "default"):
        super().__init__(self.safe_reason)
        self.processor = processor
        self.telemetry = None


class BudgetExceeded(BudgetBlock):
    code = "daily_llm_budget_exceeded"
    safe_reason = "daily LLM budget reached"

    def __init__(self, today_cost=None, cap=None, *, processor: str = "default"):
        super().__init__(processor=processor)
        self.today_cost = today_cost
        self.cap = cap


class BudgetUnavailable(BudgetBlock):
    code = "daily_llm_budget_unavailable"
    safe_reason = "daily LLM budget unavailable"


@dataclass(frozen=True)
class ManualBudgetAuthorization:
    """Opaque capability minted only after an authenticated manual action."""

    _token: object | None = field(default=None, repr=False, compare=False)

    @property
    def valid(self) -> bool:
        return self._token is _TRUSTED_MANUAL_AUTHORIZATION


def mint_trusted_manual_authorization() -> ManualBudgetAuthorization:
    """Task 29 seam: call only from the authenticated manual-action boundary."""
    return ManualBudgetAuthorization(_TRUSTED_MANUAL_AUTHORIZATION)


@dataclass(frozen=True)
class BudgetContext:
    """Call provenance. Public booleans alone never create bypass authority."""

    force: bool = False
    manual_authorized: bool = False
    _authorization: ManualBudgetAuthorization | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def trusted_manual_force(self) -> bool:
        return (
            self.force
            and self.manual_authorized
            and isinstance(self._authorization, ManualBudgetAuthorization)
            and self._authorization.valid
        )


def trusted_manual_budget_context(
    *,
    force: bool,
    manual_authorized: bool,
    authorization: ManualBudgetAuthorization | object | None,
) -> BudgetContext:
    """Bind force flags to an internally minted authenticated capability."""
    if not (force and manual_authorized):
        raise ValueError("trusted manual force requires force and manual authorization")
    if not isinstance(authorization, ManualBudgetAuthorization) or not authorization.valid:
        raise ValueError("trusted manual force requires trusted authorization")
    return BudgetContext(
        force=True,
        manual_authorized=True,
        _authorization=authorization,
    )


@dataclass(frozen=True)
class BudgetPermit:
    _token: object | None = field(default=None, repr=False, compare=False)

    @property
    def valid(self) -> bool:
        return self._token is _BUDGET_PERMIT


def _mint_budget_permit() -> BudgetPermit:
    return BudgetPermit(_BUDGET_PERMIT)


def enforce_budget(
    config: dict,
    processor: str,
    context: BudgetContext | None = None,
) -> BudgetPermit:
    context = context or BudgetContext()
    if context.trusted_manual_force:
        logger.info(
            "llm_budget_manual_bypass",
            processor=processor,
            blocked_code="manual_force",
        )
        return _mint_budget_permit()
    try:
        cap, warn_at = get_budget_config(config)
    except ValueError:
        logger.warning(
            "llm_budget_invalid",
            processor=processor,
            blocked_code="daily_llm_budget_unavailable",
        )
        raise BudgetUnavailable(processor=processor) from None
    if cap <= 0:
        return _mint_budget_permit()
    try:
        today_cost, _ = get_today_spend(config)
        status = budget_status(today_cost, cap, warn_at)
    except Exception:
        logger.warning(
            "llm_budget_lookup_failed",
            processor=processor,
            blocked_code="daily_llm_budget_unavailable",
        )
        raise BudgetUnavailable(processor=processor) from None
    if status["exceeded"]:
        logger.warning(
            "llm_budget_blocked",
            processor=processor,
            today_cost_usd=round(today_cost, 6),
            budget_cap_usd=cap,
            blocked_code=BudgetExceeded.code,
        )
        raise BudgetExceeded(today_cost, cap, processor=processor)
    return _mint_budget_permit()
