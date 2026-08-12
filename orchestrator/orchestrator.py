"""Compatibility facade for the orchestrator service.

Behavior lives in concern modules. This module intentionally keeps the historic
imports and patch seams used by workers, API routes, and tests while composing
run lifecycle with the cycle implementation.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from uuid import uuid4

from budgets import BudgetBlock, BudgetContext, BudgetExceeded
from collector_execution import (
    DEFAULT_COLLECTOR_WORKERS,
    MAX_COLLECTOR_WORKERS,
    _collection_issue_reason,
    _estimate_api_calls,
    _run_collector_impl,
    _safe_collection_token,
    collector_worker_limit,
    get_last_collection_runs,
    run_collector,
    run_news_source,
)
from collectors import get_all_collectors, get_collector
from collectors.base import CollectionResult, elapsed_ms
from cycle_planning import (
    COMPLETE_STAGE_STATES,
    FAILED_STAGE_STATES,
    VALID_CYCLE_MODES,
    _aggregate_stage_status,
    _collector_is_due,
    _last_successful_collection,
    _run_full_cycle_impl,
    aggregate_stage_statuses,
)
from db import get_session, insert_records, upsert_records
from errors import (
    BudgetDenied,
    InvalidSourceData,
    OrchestratorError,
    PersistenceError,
    TransientSourceError,
    classify_error,
)
from llm_client import resolve_model
from locks import advisory_lock
from logging_config import get_logger
from processor_execution import (
    _authorize_claimed_run_budget,
    _consume_budget_override,
    _find_reusable_processor_output,
    _resolve_and_run_processors,
    _run_processor_impl,
    build_processor_fingerprint,
    run_processor,
)
from processors import get_all_processors, get_processor
from processors.base import canonical_fingerprint
from publication import (
    _persist_processor_result,
    _write_collection_log,
    _write_processing_log,
)
from recovery import reconcile_abandoned_runs
from run_lifecycle import (
    DEFAULT_ACCEPTED_TIMEOUT,
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    DEFAULT_HEARTBEAT_TIMEOUT,
    RunAcceptanceConflict,
    RunStartConflict,
    accept_run,
    ensure_run,
    finalize_run_safely,
    finish_run,
    get_run_for_retry,
    heartbeat_run,
    maintain_run_heartbeat,
    start_run,
    update_run_progress,
)

logger = get_logger("orchestrator")


def _resolved_config(config: dict | None) -> dict:
    if config is not None:
        return config
    from config_loader import load_config

    return load_config()


def run_full_cycle(
    config: dict | None = None,
    correlation_id: str | None = None,
    manage_lifecycle: bool = True,
    budget_context: BudgetContext | None = None,
    mode: str = "refresh",
) -> dict | None:
    """Run one cycle while composing durable ownership and cycle planning."""
    if mode not in VALID_CYCLE_MODES:
        raise ValueError("invalid cycle mode")
    config = _resolved_config(config)
    correlation_id = correlation_id or str(uuid4())
    lifecycle_created = False
    worker_id: str | None = None
    try:
        if manage_lifecycle:
            accept_run(config, correlation_id, "internal", "cycle")
            lifecycle_created = True
            claim_worker_id = f"sync:{uuid4()}"
            if not start_run(config, correlation_id, claim_worker_id):
                return None
            worker_id = claim_worker_id
            if budget_context is None:
                budget_context = _authorize_claimed_run_budget(
                    config,
                    correlation_id,
                    run_kind="cycle",
                    component=None,
                )
        heartbeat = (
            maintain_run_heartbeat(config, correlation_id, worker_id)
            if worker_id is not None
            else nullcontext()
        )
        with heartbeat:
            with advisory_lock("cycle", config):
                result = _run_full_cycle_impl(
                    config,
                    correlation_id,
                    manage_lifecycle=False,
                    worker_id=worker_id,
                    budget_context=budget_context,
                    mode=mode,
                )
            if manage_lifecycle and result is not None:
                finalized = finalize_run_safely(
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
    except RunAcceptanceConflict:
        raise
    except Exception as exc:
        policy = classify_error(exc)
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
            finalize_run_safely(
                correlation_id,
                "failed",
                result,
                config,
                error_message,
                worker_id=worker_id,
                run_kind="cycle",
            )
        raise


__all__ = [
    "BudgetBlock",
    "BudgetContext",
    "BudgetDenied",
    "BudgetExceeded",
    "CollectionResult",
    "InvalidSourceData",
    "OrchestratorError",
    "PersistenceError",
    "RunAcceptanceConflict",
    "RunStartConflict",
    "TransientSourceError",
    "DEFAULT_ACCEPTED_TIMEOUT",
    "DEFAULT_HEARTBEAT_TIMEOUT",
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "DEFAULT_COLLECTOR_WORKERS",
    "MAX_COLLECTOR_WORKERS",
    "VALID_CYCLE_MODES",
    "COMPLETE_STAGE_STATES",
    "FAILED_STAGE_STATES",
    "logger",
    "accept_run",
    "advisory_lock",
    "aggregate_stage_statuses",
    "build_processor_fingerprint",
    "canonical_fingerprint",
    "classify_error",
    "collector_worker_limit",
    "ensure_run",
    "finalize_run_safely",
    "finish_run",
    "get_last_collection_runs",
    "get_run_for_retry",
    "heartbeat_run",
    "maintain_run_heartbeat",
    "reconcile_abandoned_runs",
    "run_collector",
    "run_full_cycle",
    "run_news_source",
    "run_processor",
    "start_run",
    "update_run_progress",
    "get_session",
    "insert_records",
    "upsert_records",
    "get_all_collectors",
    "get_collector",
    "get_all_processors",
    "get_processor",
    "resolve_model",
    "elapsed_ms",
    "ThreadPoolExecutor",
    "time",
    "_aggregate_stage_status",
    "_collector_is_due",
    "_last_successful_collection",
    "_run_full_cycle_impl",
    "_run_collector_impl",
    "_safe_collection_token",
    "_collection_issue_reason",
    "_estimate_api_calls",
    "_find_reusable_processor_output",
    "_resolve_and_run_processors",
    "_run_processor_impl",
    "_consume_budget_override",
    "_authorize_claimed_run_budget",
    "_write_collection_log",
    "_write_processing_log",
    "_persist_processor_result",
]
