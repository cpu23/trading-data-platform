"""UTC-day LLM spending policy and orchestrator enforcement.

Admission is transactional: each distinct paid call reserves an estimated cost
immediately before it is dispatched, so ``committed spend + active
reservations + estimate`` must fit under the daily cap. Committed spend is
reconciled per (correlation, processor): the pair's ``processing_log`` total
minus its settled reservation actuals (floored at zero), plus all settled
reservation actuals anchored to their reservation day — every dollar is
counted exactly once on the day it was admitted, whether the run was purely
reserved, purely legacy, or mixed. A reservation settling after its TTL
expired still records its real actual. A zero cap denies all paid calls (fail
closed, no unlimited mode); a missing or malformed budget result always fails
closed into ``BudgetUnavailable``.

Model pricing is a first-class admission input: a known-free model slug
(OpenRouter ``:free`` variant) reserves zero cost and is admitted without
consuming the cap, while a paid model whose per-call pricing is not
configured fails closed — the orchestrator never guesses an estimate for a
paid call.
"""

from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from contracts.budgets import (
    DEFAULT_RESERVATION_TTL_SECONDS,
    coerce_finite_number,
    get_budget_config,
    utc_day_bounds,
)
from db import get_session
from logging_config import get_logger

logger = get_logger("budgets")
# Mirrors llm_client.DEFAULT_STAGE_TIMEOUT_SECONDS without importing it
# (llm_client imports this module). The reservation must outlive the single
# make_request deadline: stage_timeout + 30s slack (paid calls are
# single-attempt; validation retries are separately-budgeted calls).
_DEFAULT_STAGE_TIMEOUT_SECONDS = 90.0
_REQUEST_DEADLINE_SLACK_SECONDS = 30.0
_TRUSTED_MANUAL_AUTHORIZATION = object()
_BUDGET_PERMIT = object()
def _known_free_model(model: str | None) -> bool:
    """True when the model slug is a known-free variant.

    OpenRouter free tiers are addressed with a ``:free`` suffix
    (``provider/model:free``).  A known-free model may reserve zero cost:
    it never consumes the daily cap, yet still carries a reservation row so
    dispatch and settlement stay audit-symmetric with paid calls.
    """
    return isinstance(model, str) and model.strip().lower().endswith(":free")


def _reservation_policy(
    config: dict, processor: str, model: str | None = None
) -> tuple[float, float]:
    """Return (estimate_usd, ttl_seconds) for one call; invalid -> ValueError.

    A known-free model reserves zero.  A paid model requires explicitly
    configured pricing (per-processor ``budgets.estimates``, else the generic
    ``budgets.reservation_estimate_usd``); missing pricing fails closed with
    ``ValueError`` so ``enforce_budget`` maps it to ``BudgetUnavailable`` and
    no HTTP request is attempted.
    """
    from collections.abc import Mapping

    budget_cfg = config.get("budgets", {})
    if not isinstance(budget_cfg, Mapping):
        raise ValueError("budgets must be an object")
    if _known_free_model(model):
        return 0.0, _reservation_ttl(config)
    estimate = None
    estimates = budget_cfg.get("estimates")
    if isinstance(estimates, Mapping):
        candidate = estimates.get(processor)
        if candidate is not None:
            estimate = candidate
    if estimate is None:
        estimate = budget_cfg.get("reservation_estimate_usd")
    if estimate is None:
        raise ValueError(
            f"no configured pricing for paid model {processor!r}; "
            "set budgets.estimates or budgets.reservation_estimate_usd"
        )
    estimate = coerce_finite_number(estimate, "budgets.reservation_estimate_usd")
    if estimate <= 0:
        raise ValueError("budgets.reservation_estimate_usd must be positive")
    return estimate, _reservation_ttl(config)


def _reservation_ttl(config: dict) -> float:
    """TTL for one reservation, validated against the LLM request deadline."""
    from collections.abc import Mapping

    budget_cfg = config.get("budgets", {})
    if not isinstance(budget_cfg, Mapping):
        raise ValueError("budgets must be an object")
    ttl = coerce_finite_number(
        budget_cfg.get("reservation_ttl_seconds", DEFAULT_RESERVATION_TTL_SECONDS),
        "budgets.reservation_ttl_seconds",
    )
    if ttl <= 0:
        raise ValueError("budgets.reservation_ttl_seconds must be positive")
    # Cross-field gate: an in-flight call must never have its admission
    # released before it completes. Paid OpenRouter calls are single-attempt,
    # so the TTL must cover llm.stage_timeout_seconds + 30s (the make_request
    # deadline). Validation retries are separately-budgeted calls.
    llm_cfg = config.get("llm", {})
    if isinstance(llm_cfg, Mapping):
        try:
            stage_timeout = float(
                llm_cfg.get("stage_timeout_seconds", _DEFAULT_STAGE_TIMEOUT_SECONDS)
            )
        except (TypeError, ValueError, OverflowError):
            stage_timeout = _DEFAULT_STAGE_TIMEOUT_SECONDS
        min_ttl = max(stage_timeout, 0.0) + _REQUEST_DEADLINE_SLACK_SECONDS
        if ttl < min_ttl:
            raise ValueError(
                "budgets.reservation_ttl_seconds must be at least "
                f"{min_ttl:g}s to cover the LLM request deadline"
            )
    return ttl


def _reserve_budget_quota(
    config: dict,
    processor: str,
    cap: float,
    estimate_usd: float,
    ttl_seconds: float,
    *,
    correlation_id: str | None = None,
    run_kind: str | None = None,
    component: str | None = None,
    now: datetime | None = None,
    session=None,
) -> str:
    """Transactionally admit one paid call under the daily cap.

    A transaction-level advisory lock keyed by the UTC budget day serializes
    admission across every connection, so concurrent workers can never push
    ``spent + active reservations + estimate`` over the cap. Raises
    ``BudgetExceeded`` when the quota is exhausted; any unreadable budget state
    propagates as an exception the caller maps to ``BudgetUnavailable``.
    A zero estimate (known-free model) cannot oversubscribe and is always
    admitted as long as the day is not already over the cap from paid spend.
    """
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(UTC)
    day_start, day_end = utc_day_bounds(current)
    budget_day = day_start.date()
    lock_key = f"budget_day:{budget_day.isoformat()}"
    expires_at = current + timedelta(seconds=ttl_seconds)

    session_scope = get_session(config) if session is None else nullcontext(session)
    with session_scope as active_session:
        active_session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": lock_key},
        )
        active_session.execute(
            text(
                "UPDATE budget_reservations SET status = 'expired' "
                "WHERE status = 'active' AND expires_at <= :now"
            ),
            {"now": current},
        )
        row = active_session.execute(
            text(
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
                "    /* The reservation day owns the paid call: subtract the "
                "       pair's settled actuals regardless of their budget_day, "
                "       so a run started before midnight whose call was admitted "
                "       after midnight is not double-counted as legacy spend. */ "
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
                ") AS reserved_usd"
            ),
            {
                "day_start": day_start,
                "day_end": day_end,
                "day": budget_day,
                "now": current,
            },
        ).fetchone()
        spent = coerce_finite_number(row._mapping.get("spent_usd"), "budget spent")
        reserved = coerce_finite_number(row._mapping.get("reserved_usd"), "budget reserved")
        if estimate_usd > 0 and spent + reserved + estimate_usd > cap:
            raise BudgetExceeded(spent + reserved, cap, processor=processor)
        inserted = active_session.execute(
            text(
                "INSERT INTO budget_reservations "
                "(budget_day, correlation_id, run_kind, component, processor, "
                "requested_by, reason, estimated_usd, expires_at, reserved_at) "
                "VALUES (:day, :cid, :run_kind, :component, :processor, "
                ":requested_by, :reason, :estimate, :expires_at, :reserved_at) "
                "RETURNING id"
            ),
            {
                "day": budget_day,
                "cid": correlation_id,
                "run_kind": run_kind,
                "component": component,
                "processor": processor,
                "requested_by": None,
                "reason": None,
                "estimate": estimate_usd,
                "expires_at": expires_at,
                "reserved_at": current,
            },
        ).fetchone()
        return str(inserted[0])


def reserve_budget_quota(
    config: dict,
    *,
    processor: str,
    estimate_usd: float,
    ttl_seconds: float,
    correlation_id: str | None = None,
    run_kind: str | None = None,
    component: str | None = None,
    now: datetime | None = None,
    session=None,
) -> str:
    """Reserve bounded non-model research cost through the global UTC-day ledger."""
    cap, _warn_at = get_budget_config(config)
    estimate = coerce_finite_number(estimate_usd, "estimate_usd")
    ttl = coerce_finite_number(ttl_seconds, "ttl_seconds")
    if estimate < 0 or estimate > 100:
        raise ValueError("estimate_usd must be between 0 and 100")
    if ttl < 1 or ttl > 86400:
        raise ValueError("ttl_seconds must be between 1 and 86400")
    if not isinstance(processor, str) or not processor.strip():
        raise ValueError("processor must be non-empty")
    return _reserve_budget_quota(
        config,
        processor.strip(),
        cap,
        estimate,
        ttl,
        correlation_id=correlation_id,
        run_kind=run_kind,
        component=component,
        now=now,
        session=session,
    )

def get_today_spend(config: dict, *, now: datetime | None = None) -> tuple[float, int]:
    today_start, tomorrow_start = utc_day_bounds(now)
    sql = text(
        "SELECT COALESCE(SUM(COALESCE(cost_usd, 0)), 0) AS total_cost, "
        "COALESCE(SUM(COALESCE(tokens_input, 0) + COALESCE(tokens_output, 0)), 0) "
        "AS total_tokens FROM processing_log "
        "WHERE started_at >= :today_start AND started_at < :tomorrow_start"
    )
    with get_session(config) as session:
        row = (
            session.execute(
                sql, {"today_start": today_start, "tomorrow_start": tomorrow_start}
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        return 0.0, 0
    return float(row.get("total_cost") or 0), int(row.get("total_tokens") or 0)


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
    if (
        not isinstance(authorization, ManualBudgetAuthorization)
        or not authorization.valid
    ):
        raise ValueError("trusted manual force requires trusted authorization")
    return BudgetContext(
        force=True,
        manual_authorized=True,
        _authorization=authorization,
    )


@dataclass(frozen=True)
class BudgetPermit:
    _token: object | None = field(default=None, repr=False, compare=False)
    reservation_id: str | None = field(default=None, repr=False, compare=False)

    @property
    def valid(self) -> bool:
        return self._token is _BUDGET_PERMIT


def _mint_budget_permit(reservation_id: str | None = None) -> BudgetPermit:
    return BudgetPermit(_BUDGET_PERMIT, reservation_id)


def enforce_budget(
    config: dict,
    processor: str,
    context: BudgetContext | None = None,
    *,
    correlation_id: str | None = None,
    run_kind: str | None = None,
    component: str | None = None,
    model: str | None = None,
    now: datetime | None = None,
) -> BudgetPermit:
    """Admit one call under the daily cap, reserving its estimated cost.

    The resolved model slug classifies the call: a known-free model
    (``:free`` suffix) reserves zero cost, while a paid model with no
    configured pricing fails closed.  A trusted manual override
    short-circuits before any budget state is read; every other path fails
    closed: an unreadable or malformed budget result raises
    ``BudgetUnavailable`` and no HTTP request is attempted.
    """
    context = context or BudgetContext()
    if context.trusted_manual_force:
        logger.info(
            "llm_budget_manual_bypass",
            processor=processor,
            blocked_code="manual_force",
        )
        return _mint_budget_permit()
    try:
        cap, _warn_at = get_budget_config(config)
    except ValueError:
        logger.warning(
            "llm_budget_invalid",
            processor=processor,
            blocked_code="daily_llm_budget_unavailable",
        )
        raise BudgetUnavailable(processor=processor) from None
    try:
        estimate_usd, ttl_seconds = _reservation_policy(
            config, processor, model=model
        )
        reservation_id = _reserve_budget_quota(
            config,
            processor,
            cap,
            estimate_usd,
            ttl_seconds,
            correlation_id=correlation_id,
            run_kind=run_kind,
            component=component,
            now=now,
        )
    except BudgetBlock:
        raise
    except Exception as exc:
        logger.warning(
            "llm_budget_reservation_failed",
            processor=processor,
            blocked_code="daily_llm_budget_unavailable",
            error_type=type(exc).__name__,
        )
        raise BudgetUnavailable(processor=processor) from None
    return _mint_budget_permit(reservation_id=reservation_id)


def settle_budget_reservation(
    reservation_id: str | None,
    actual_usd: float,
    config: dict,
    *,
    now: datetime | None = None,
) -> bool:
    """Record actual spend against a reservation, anchoring it to its day.

    The paid call already happened, so the actual is recorded even if the
    reservation expired mid-call (TTL shorter than the call): the spend is
    real and must not vanish. Idempotent and never raises on database trouble
    (``processing_log`` remains authoritative). A malformed actual value is
    rejected, never silently accepted.
    """
    if not isinstance(reservation_id, str) or not reservation_id:
        return True
    actual = coerce_finite_number(actual_usd, "settled_usd")
    if actual < 0:
        raise ValueError("settled_usd must be non-negative")
    try:
        with get_session(config) as session:
            result = session.execute(
                text(
                    "UPDATE budget_reservations SET status = 'settled', "
                    "settled_usd = :actual, settled_at = :now "
                    "WHERE id = :rid AND status IN ('active', 'expired')"
                ),
                {
                    "rid": reservation_id,
                    "actual": actual,
                    "now": now or datetime.now(UTC),
                },
            )
            return result.rowcount == 1
    except Exception as exc:
        logger.warning(
            "budget_reservation_settle_failed",
            reservation_id=reservation_id,
            error_type=type(exc).__name__,
        )
        return False


def release_budget_reservation(
    reservation_id: str | None,
    config: dict,
    *,
    now: datetime | None = None,
) -> bool:
    """Release an active reservation that definitively incurred no cost.

    Only callers that know no charge happened (transport or HTTP-status
    failure before a 2xx body) may release. Ambiguous failures — a 2xx
    response whose payload was unusable — must use
    :func:`retain_budget_reservation` instead so paid-but-unparseable calls
    still hold their estimate.
    """
    if not isinstance(reservation_id, str) or not reservation_id:
        return True
    try:
        with get_session(config) as session:
            result = session.execute(
                text(
                    "UPDATE budget_reservations SET status = 'released', "
                    "settled_usd = 0, settled_at = :now "
                    "WHERE id = :rid AND status = 'active'"
                ),
                {
                    "rid": reservation_id,
                    "now": now or datetime.now(UTC),
                },
            )
            return result.rowcount == 1
    except Exception as exc:
        logger.warning(
            "budget_reservation_release_failed",
            reservation_id=reservation_id,
            error_type=type(exc).__name__,
        )
        return False


def retain_budget_reservation(
    reservation_id: str | None,
    config: dict,
    *,
    now: datetime | None = None,
) -> bool:
    """Settle an ambiguous call at its estimate: the provider may have charged.

    Keeps the reserved amount counted as a settled actual instead of releasing
    it, so a paid-but-unparseable 2xx response can never undercount the cap.
    Works even when the reservation expired mid-call.
    """
    if not isinstance(reservation_id, str) or not reservation_id:
        return True
    try:
        with get_session(config) as session:
            result = session.execute(
                text(
                    "UPDATE budget_reservations SET status = 'settled', "
                    "settled_usd = estimated_usd, settled_at = :now "
                    "WHERE id = :rid AND status IN ('active', 'expired')"
                ),
                {
                    "rid": reservation_id,
                    "now": now or datetime.now(UTC),
                },
            )
            return result.rowcount == 1
    except Exception as exc:
        logger.warning(
            "budget_reservation_retain_failed",
            reservation_id=reservation_id,
            error_type=type(exc).__name__,
        )
        return False


def expire_abandoned_reservations(
    config: dict, *, now: datetime | None = None
) -> int:
    """Sweep reservations whose TTL lapsed while still active.

    Expired reservations are already excluded from admission by the active-sum
    query, so this is bookkeeping that releases their estimates explicitly and
    retains the expired provenance. Never raises: failures are logged.
    """
    try:
        with get_session(config) as session:
            result = session.execute(
                text(
                    "UPDATE budget_reservations SET status = 'expired' "
                    "WHERE status = 'active' AND expires_at <= :now"
                ),
                {"now": now or datetime.now(UTC)},
            )
            return result.rowcount
    except Exception as exc:
        logger.warning(
            "budget_reservation_expiry_failed",
            error_type=type(exc).__name__,
        )
        return 0
