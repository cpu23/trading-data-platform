"""Error taxonomy making retry and reporting policy explicit.

Broad exception handling is appropriate for a failure-isolating orchestrator,
but each catch site must classify the exception through :func:`classify_error`
instead of letting the accident of *where* a catch happens decide whether a
failure is retryable, permanent, or a policy refusal.

Classes
-------
TransientSourceError
    Upstream provider or network fault. Retryable on the next scheduled or
    manual attempt; reported as ``failed`` with ``error_class="transient_source"``.
InvalidSourceData
    Malformed or contract-violating input/output. NOT retryable without
    changing inputs; reported as ``validation_failed`` where durability
    distinguishes it, else ``failed`` with ``error_class="invalid_source_data"``.
PersistenceError
    The platform's own durable write failed. Retryable; reported as ``failed``
    with ``error_class="persistence"``.
BudgetDenied
    Budget policy refused the work. NOT retryable until budget resets or an
    explicit override is granted.

Legacy typed exceptions from other modules (``BudgetBlock`` family,
``CollectorStateError`` family, ``processors._validators.OutputPolicyError``)
are recognized by :func:`classify_error` so existing raise sites join the
taxonomy without rewrites.
"""

from __future__ import annotations

from typing import NamedTuple

from sqlalchemy.exc import SQLAlchemyError


class ErrorPolicy(NamedTuple):
    """Explicit outcome policy for a classified failure."""

    status: str
    error_class: str
    retryable: bool


ERROR_CLASS_UNKNOWN = "unknown"
ERROR_CLASS_TRANSIENT_SOURCE = "transient_source"
ERROR_CLASS_INVALID_SOURCE_DATA = "invalid_source_data"
ERROR_CLASS_PERSISTENCE = "persistence"
ERROR_CLASS_BUDGET_DENIED = "budget_denied"

RETRYABLE_CLASSES = frozenset({ERROR_CLASS_TRANSIENT_SOURCE, ERROR_CLASS_PERSISTENCE})


class OrchestratorError(Exception):
    """Base for orchestrator-taxonomy errors; carries explicit policy."""

    error_class = ERROR_CLASS_UNKNOWN
    retryable = False


class TransientSourceError(OrchestratorError):
    """Upstream provider or network fault worth retrying."""

    error_class = ERROR_CLASS_TRANSIENT_SOURCE
    retryable = True


class InvalidSourceData(OrchestratorError):
    """Malformed or contract-violating data; retrying cannot succeed."""

    error_class = ERROR_CLASS_INVALID_SOURCE_DATA
    retryable = False


class PersistenceError(OrchestratorError):
    """A durable write owned by this platform failed; retryable."""

    error_class = ERROR_CLASS_PERSISTENCE
    retryable = True


class BudgetDenied(OrchestratorError):
    """Budget policy refused the work until reset or explicit override."""

    error_class = ERROR_CLASS_BUDGET_DENIED
    retryable = False


def classify_error(exc: BaseException) -> ErrorPolicy:
    """Map any exception to an explicit ``(status, error_class, retryable)`` policy.

    The default branch is deliberately conservative: unrecognized exceptions
    are permanent ``failed`` outcomes, so new failure modes must opt into
    retryability by raising a taxonomy class rather than inheriting it from
    catch-site placement.
    """
    if isinstance(exc, OrchestratorError):
        if isinstance(exc, InvalidSourceData):
            return ErrorPolicy("validation_failed", exc.error_class, exc.retryable)
        if isinstance(exc, BudgetDenied):
            return ErrorPolicy("budget_blocked", exc.error_class, exc.retryable)
        return ErrorPolicy("failed", exc.error_class, exc.retryable)

    # Budget policy refusals keep their distinct lifecycle statuses.
    from budgets import BudgetBlock, BudgetExceeded

    if isinstance(exc, BudgetBlock):
        status = (
            "budget_blocked"
            if isinstance(exc, BudgetExceeded)
            else "budget_unavailable"
        )
        return ErrorPolicy(status, ERROR_CLASS_BUDGET_DENIED, False)

    if isinstance(exc, SQLAlchemyError):
        return ErrorPolicy("failed", ERROR_CLASS_PERSISTENCE, True)

    # Processor output-policy violations are invalid data, not transient faults.
    try:
        from processors._validators import OutputPolicyError
    except ImportError:  # pragma: no cover - validators always present in practice
        OutputPolicyError = None  # type: ignore[assignment,misc]
    if OutputPolicyError is not None and isinstance(exc, OutputPolicyError):
        return ErrorPolicy("validation_failed", ERROR_CLASS_INVALID_SOURCE_DATA, False)

    # LLM stage failures carry bounded, raise-site-chosen messages (never
    # provider/model payload), so they are safe to surface; a model response
    # that failed validation cannot succeed on a retry of the same prompt, so
    # the outcome is a permanent failed processor run, typed as invalid data.
    try:
        from llm_client import LLMStageFailure
    except ImportError:  # pragma: no cover - llm_client always present in practice
        LLMStageFailure = None  # type: ignore[assignment,misc]
    if LLMStageFailure is not None and isinstance(exc, LLMStageFailure):
        return ErrorPolicy("failed", ERROR_CLASS_INVALID_SOURCE_DATA, False)

    # Collector state errors are expected, non-retryable collection outcomes.
    from collectors.base import (
        CollectorNoData,
        CollectorSetupRequired,
        CollectorStateError,
    )

    if isinstance(exc, (CollectorSetupRequired, CollectorNoData)):
        return ErrorPolicy("failed", ERROR_CLASS_INVALID_SOURCE_DATA, False)
    if isinstance(exc, CollectorStateError):
        return ErrorPolicy("failed", ERROR_CLASS_INVALID_SOURCE_DATA, False)

    # Connection-style faults from httpx are transient by nature.
    try:
        import httpx

        if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
            return ErrorPolicy("failed", ERROR_CLASS_TRANSIENT_SOURCE, True)
    except ImportError:  # pragma: no cover - httpx is a hard dependency
        pass

    return ErrorPolicy("failed", ERROR_CLASS_UNKNOWN, False)
