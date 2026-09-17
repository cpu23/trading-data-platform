"""Orchestrator public entry points.

Concern modules own collection, processing, planning, and lifecycle details;
this module composes the public synchronous cycle entry point.
"""

from __future__ import annotations

from contextlib import nullcontext
from uuid import uuid4

import budgets
import cycle_planning
import errors
import locks
import processor_execution
import run_lifecycle
from logging_config import get_logger

from contracts.runtime_config import load_config

logger = get_logger("orchestrator")


def _resolved_config(config: dict | None) -> dict:
    if config is not None:
        return config
    return load_config()


def run_full_cycle(
    config: dict | None = None,
    correlation_id: str | None = None,
    manage_lifecycle: bool = True,
    budget_context: budgets.BudgetContext | None = None,
    mode: str = "refresh",
) -> dict | None:
    """Run one cycle while composing durable ownership and cycle planning."""
    if mode not in cycle_planning.VALID_CYCLE_MODES:
        raise ValueError("invalid cycle mode")
    config = _resolved_config(config)
    correlation_id = correlation_id or str(uuid4())
    lifecycle_created = False
    worker_id: str | None = None
    try:
        if manage_lifecycle:
            run_lifecycle.accept_run(config, correlation_id, "internal", "cycle")
            lifecycle_created = True
            claim_worker_id = f"sync:{uuid4()}"
            if not run_lifecycle.start_run(config, correlation_id, claim_worker_id):
                return None
            worker_id = claim_worker_id
            if budget_context is None:
                budget_context = processor_execution._authorize_claimed_run_budget(
                    config,
                    correlation_id,
                    run_kind="cycle",
                    component=None,
                )
        heartbeat = (
            run_lifecycle.maintain_run_heartbeat(config, correlation_id, worker_id)
            if worker_id is not None
            else nullcontext()
        )
        with heartbeat:
            with locks.advisory_lock("cycle", config):
                result = cycle_planning._run_full_cycle_impl(
                    config,
                    correlation_id,
                    worker_id=worker_id,
                    budget_context=budget_context,
                    mode=mode,
                )
            if manage_lifecycle and result is not None:
                finalized = run_lifecycle.finalize_run_safely(
                    correlation_id,
                    result["status"],
                    result,
                    config,
                    worker_id=worker_id,
                    run_kind="cycle",
                )
                if finalized:
                    logger.info(
                        "full_cycle_completed",
                        action="run_full_cycle",
                        overall_status=result["status"],
                        correlation_id=correlation_id,
                    )
            return result
    except run_lifecycle.RunAcceptanceConflict:
        raise
    except Exception as exc:
        policy = errors.classify_error(exc)
        if manage_lifecycle and lifecycle_created:
            error_message = (
                str(exc) if worker_id is not None else "run start unavailable"
            )
            result = (
                {}
                if worker_id is not None
                else {
                    "status": policy.status,
                    "reason": error_message,
                    "error_class": policy.error_class,
                    "retryable": policy.retryable,
                }
            )
            run_lifecycle.finalize_run_safely(
                correlation_id,
                "failed",
                result,
                config,
                error_message,
                worker_id=worker_id,
                run_kind="cycle",
            )
        raise

__all__ = ["run_full_cycle"]
