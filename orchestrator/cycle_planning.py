"""Cycle planning and orchestration.

This module owns stage aggregation, due checks, dependency availability, and
full-cycle execution. Runtime dependencies are resolved through the
``orchestrator`` facade at call time so existing patch seams remain valid while
the facade itself stays small.
"""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import as_completed
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING
from uuid import uuid4

from logging_config import get_logger
from schedules import build_cron_trigger

if TYPE_CHECKING:
    from budgets import BudgetContext

logger = get_logger("orchestrator.cycle_planning")

VALID_CYCLE_MODES = frozenset({"refresh", "analyze", "force_full"})
COMPLETE_STAGE_STATES = {"success"}
FAILED_STAGE_STATES = {"failed", "validation_failed"}


def aggregate_stage_statuses(statuses: Iterable[str]) -> str:
    """Combine stage outcomes; skipped analytical work is a healthy no-op."""
    values = ["success" if status == "skipped" else status for status in statuses]
    if not values or all(status == "success" for status in values):
        return "success"
    if all(status == "failed" for status in values):
        return "failed"
    budget_statuses = {"budget_blocked", "budget_unavailable"}
    if all(status in budget_statuses for status in values):
        if all(status == "budget_unavailable" for status in values):
            return "budget_unavailable"
        if all(status == "budget_blocked" for status in values):
            return "budget_blocked"
    if all(status in COMPLETE_STAGE_STATES for status in values):
        return "success"
    if all(
        status in FAILED_STAGE_STATES | {"skipped", "budget_denied"}
        for status in values
    ) and any(status in FAILED_STAGE_STATES for status in values):
        return "failed"
    return "partial"


def _aggregate_stage_status(results: dict[str, dict]) -> str:
    """Aggregate blocking stages, preserving validation and budget outcomes."""
    if not results:
        return "failed"
    statuses = [
        result.get("status", "failed")
        for result in results.values()
        if result.get("blocking", True)
    ]
    if not statuses:
        return "success"
    if all(status in COMPLETE_STAGE_STATES for status in statuses):
        return "success"
    if all(
        status in FAILED_STAGE_STATES | {"skipped", "budget_denied"}
        for status in statuses
    ):
        if "validation_failed" in statuses:
            return "validation_failed"
        if "budget_denied" in statuses and not any(
            status in FAILED_STAGE_STATES for status in statuses
        ):
            return "budget_denied"
        return "failed"
    return "partial"


def _facade():
    import orchestrator

    return orchestrator


def _last_successful_collection(source_id: str, config: dict) -> datetime | None:
    from sqlalchemy import text

    facade = _facade()
    with facade.get_session(config) as session:
        row = session.execute(
            text(
                "SELECT completed_at FROM collection_log "
                "WHERE collector = :collector AND status = 'success' "
                "ORDER BY completed_at DESC LIMIT 1"
            ),
            {"collector": source_id},
        ).fetchone()
    if row is None:
        return None
    mapping = getattr(row, "_mapping", None)
    return mapping["completed_at"] if mapping is not None else row[0]


def _collector_is_due(
    source_id: str, config: dict, *, now: datetime | None = None
) -> bool:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(UTC)
    try:
        completed_at = _facade()._last_successful_collection(source_id, config)
        if completed_at is None:
            return True
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=UTC)
        completed_at = completed_at.astimezone(UTC)
        schedule = config.get("collectors", {}).get(source_id, {}).get("schedule")
        if not isinstance(schedule, str) or not schedule.strip():
            raise ValueError("collector schedule missing")
        trigger = build_cron_trigger(schedule)
        next_fire = trigger.get_next_fire_time(completed_at, completed_at)
        return next_fire is None or next_fire <= current
    except Exception as exc:
        from errors import classify_error

        policy = classify_error(exc)
        logger.warning(
            "collector_due_check_failed_safe_due",
            action="select_cycle_collectors",
            collector=source_id,
            safe_code="collector_due_check_unavailable",
            error_class=policy.error_class,
            retryable=policy.retryable,
        )
        return True


def _run_full_cycle_impl(
    config: dict | None = None,
    correlation_id: str | None = None,
    manage_lifecycle: bool = True,
    worker_id: str | None = None,
    budget_context: BudgetContext | None = None,
    mode: str = "refresh",
    now: datetime | None = None,
) -> dict | None:
    """Execute collector and processor layers, optionally without lifecycle."""
    if mode not in VALID_CYCLE_MODES:
        raise ValueError("invalid cycle mode")
    facade = _facade()
    if config is None:
        config = facade._resolved_config(None)
    correlation_id = correlation_id or str(uuid4())
    if manage_lifecycle:
        facade.accept_run(config, correlation_id, "internal", "cycle")
        if not facade.start_run(config, correlation_id, f"sync:{uuid4()}"):
            return None

    facade.logger.info(
        "full_cycle_started", action="run_full_cycle", correlation_id=correlation_id
    )
    all_collectors = facade.get_all_collectors()
    enabled_collectors = [
        source_id
        for source_id in all_collectors
        if config.get("collectors", {}).get(source_id, {}).get("enabled", True)
    ]
    enabled_processors = [
        processor_id
        for processor_id in facade.get_all_processors()
        if config.get("processors", {}).get(processor_id, {}).get("enabled", False)
    ]
    progress = {
        "current_stage": None,
        "current_kind": None,
        "completed_stages": 0,
        "total_stages": len(enabled_collectors) + len(enabled_processors),
        "stages": [
            {"component": component, "kind": "collector", "status": "pending"}
            for component in enabled_collectors
        ]
        + [
            {"component": component, "kind": "processor", "status": "pending"}
            for component in enabled_processors
        ],
    }

    def record_progress(
        component: str, kind: str, status: str, result: dict | None = None
    ) -> None:
        stage = next(
            item
            for item in progress["stages"]
            if item["component"] == component and item["kind"] == kind
        )
        stage["status"] = status
        if status == "running":
            stage["started_at"] = datetime.now(UTC).isoformat()
            progress["current_stage"] = component
            progress["current_kind"] = kind
        else:
            if result:
                stage.update(
                    {
                        key: value
                        for key, value in result.items()
                        if key
                        in (
                            "duration_ms",
                            "records_fetched",
                            "records_written",
                            "error",
                            "reason",
                            "error_class",
                            "retryable",
                        )
                    }
                )
            progress["completed_stages"] = sum(
                item["status"] not in ("pending", "running")
                for item in progress["stages"]
            )
            running_stage = next(
                (item for item in progress["stages"] if item["status"] == "running"),
                None,
            )
            progress["current_stage"] = (
                running_stage["component"] if running_stage else None
            )
            progress["current_kind"] = running_stage["kind"] if running_stage else None
        facade.update_run_progress(correlation_id, progress, config, worker_id)

    facade.update_run_progress(correlation_id, progress, config, worker_id)
    collector_layer_started = monotonic()
    cycle_now = now or datetime.now(UTC)
    collectors_to_run: list[str] = []
    historical_available: set[str] = set()
    collector_results_by_id: dict[str, dict] = {}
    for source_id in enabled_collectors:
        if mode == "force_full":
            collectors_to_run.append(source_id)
            continue
        if mode == "analyze":
            reason = "analyze_mode_no_collection"
        elif facade._collector_is_due(source_id, config, now=cycle_now):
            collectors_to_run.append(source_id)
            continue
        else:
            reason = "not_due"
        if mode != "analyze":
            try:
                if facade._last_successful_collection(source_id, config) is not None:
                    historical_available.add(source_id)
            except Exception as exc:
                from errors import classify_error

                policy = classify_error(exc)
                facade.logger.warning(
                    "collector_history_lookup_failed",
                    action="select_cycle_collectors",
                    collector=source_id,
                    safe_code="collector_history_unavailable",
                    error_class=policy.error_class,
                    retryable=policy.retryable,
                )
        skipped = {
            "collector": source_id,
            "status": "skipped",
            "reason": reason,
            "mode": mode,
            "no_change": True,
            "correlation_id": correlation_id,
        }
        collector_results_by_id[source_id] = skipped
        record_progress(source_id, "collector", "skipped", skipped)

    worker_limit = facade.collector_worker_limit(config, len(collectors_to_run))
    if collectors_to_run:
        with facade.ThreadPoolExecutor(
            max_workers=worker_limit,
            thread_name_prefix="cycle-collector",
        ) as executor:
            future_sources = {}
            for source_id in collectors_to_run:
                record_progress(source_id, "collector", "running")
                future_sources[
                    executor.submit(
                        facade.run_collector,
                        source_id,
                        config=config,
                        correlation_id=correlation_id,
                        manage_lifecycle=False,
                    )
                ] = source_id
            for future in as_completed(future_sources):
                source_id = future_sources[future]
                try:
                    result = future.result()
                except Exception as exc:
                    from errors import classify_error

                    policy = classify_error(exc)
                    facade.logger.error(
                        "collector_future_failed",
                        action="run_full_cycle",
                        collector=source_id,
                        correlation_id=correlation_id,
                        error_class=policy.error_class,
                        retryable=policy.retryable,
                    )
                    result = {
                        "collector": source_id,
                        "status": policy.status,
                        "error": "collector execution failed",
                        "error_class": policy.error_class,
                        "retryable": policy.retryable,
                        "correlation_id": correlation_id,
                    }
                collector_results_by_id[source_id] = result
                record_progress(source_id, "collector", result["status"], result)

    collector_results = {
        source_id: collector_results_by_id[source_id]
        for source_id in enabled_collectors
    }
    successful_collectors = historical_available | {
        source_id
        for source_id in collectors_to_run
        if collector_results[source_id]["status"] == "success"
    }
    facade.logger.info(
        "collector_layer_finished",
        action="run_full_cycle",
        worker_limit=worker_limit,
        collector_count=len(collectors_to_run),
        enabled_collector_count=len(enabled_collectors),
        duration_ms=facade.elapsed_ms(collector_layer_started),
        correlation_id=correlation_id,
    )
    processor_results = facade._resolve_and_run_processors(
        config=config,
        correlation_id=correlation_id,
        successful_collectors=successful_collectors,
        progress_callback=record_progress,
        budget_context=budget_context,
        force=mode == "force_full",
        analyze_existing_data=mode == "analyze",
    )
    all_results = {**collector_results, **processor_results}
    overall_status = aggregate_stage_statuses(
        result["status"] for result in all_results.values()
    )
    cycle_result = {
        "status": overall_status,
        "mode": mode,
        "forced": mode == "force_full",
        "no_change": bool(all_results)
        and all(result["status"] == "skipped" for result in all_results.values()),
        "collectors": collector_results,
        "processors": processor_results,
        "correlation_id": correlation_id,
    }
    if overall_status == "success":
        try:
            from reconciliation import reconcile_event_pipeline

            cycle_result["reconciliation"] = reconcile_event_pipeline(config)
        except Exception:
            cycle_result["reconciliation"] = {
                "jobs_reconciled": 0,
                "snapshots_reconciled": 0,
                "error_count": 1,
            }
    facade.logger.info(
        "full_cycle_work_finished",
        action="run_full_cycle",
        overall_status=overall_status,
        collectors_run=collectors_to_run,
        collectors_skipped=[
            source_id
            for source_id, result in collector_results.items()
            if result["status"] == "skipped"
        ],
        processors_run=list(processor_results.keys()),
        correlation_id=correlation_id,
    )
    if manage_lifecycle:
        facade.finalize_run_safely(
            correlation_id,
            overall_status,
            cycle_result,
            config,
            worker_id=worker_id,
            run_kind="cycle",
        )
    return cycle_result
