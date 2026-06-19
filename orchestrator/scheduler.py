from datetime import datetime, timezone
from uuid import uuid4

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from db import advisory_lock
from logging_config import get_logger
from orchestrator import (
    DEPENDENCY_READY_STATES,
    RUNTIME_LOCK_NAME,
    _aggregate_stage_status,
    _resolve_and_run_processors,
    ensure_run,
    finish_run,
    get_transitive_dependents,
    run_collector,
    run_processor,
)

logger = get_logger("scheduler")
_scheduler: BackgroundScheduler | None = None


def _scheduled_collector(source_id: str, config: dict) -> None:
    correlation_id = str(uuid4())
    ensure_run(
        correlation_id,
        config,
        run_kind="collector",
        requested_component=source_id,
        triggered_by="scheduler",
    )
    try:
        with advisory_lock(RUNTIME_LOCK_NAME, config) as acquired:
            if not acquired:
                stages = {
                    source_id: {
                        "collector": source_id,
                        "status": "skipped",
                        "error": "Another cycle or component run is already active",
                        "correlation_id": correlation_id,
                    }
                }
            else:
                collector_result = run_collector(
                    source_id,
                    config=config,
                    correlation_id=correlation_id,
                    acquire_runtime_lock=False,
                )
                stages = {source_id: collector_result}
                processor_ids = {
                    processor_id
                    for processor_id in get_transitive_dependents(
                        source_id, config
                    )
                    if config.get("processors", {})
                    .get(processor_id, {})
                    .get("schedule")
                    == "after_dependency"
                }
                stages.update(
                    _resolve_and_run_processors(
                        config=config,
                        correlation_id=correlation_id,
                        successful_collectors=(
                            {source_id}
                            if collector_result["status"]
                            in DEPENDENCY_READY_STATES
                            else set()
                        ),
                        processor_ids=processor_ids,
                    )
                )
        overall = _aggregate_stage_status(stages)
        finish_run(correlation_id, overall, {"stages": stages}, config)
    except Exception as exc:
        logger.exception(
            "scheduled_collector_failed",
            source_id=source_id,
            correlation_id=correlation_id,
        )
        finish_run(
            correlation_id,
            "failed",
            {"stages": {}, "error": str(exc)},
            config,
            str(exc),
        )


def _scheduled_processor(processor_id: str, config: dict) -> None:
    correlation_id = str(uuid4())
    ensure_run(
        correlation_id,
        config,
        run_kind="processor",
        requested_component=processor_id,
        triggered_by="scheduler",
    )
    try:
        result = run_processor(
            processor_id,
            config=config,
            correlation_id=correlation_id,
        )
        finish_run(
            correlation_id,
            result["status"],
            result,
            config,
            result.get("error"),
        )
    except Exception as exc:
        logger.exception(
            "scheduled_processor_failed",
            processor_id=processor_id,
            correlation_id=correlation_id,
        )
        finish_run(
            correlation_id,
            "failed",
            {"processor": processor_id, "error": str(exc)},
            config,
            str(exc),
        )


def start_scheduler(config: dict) -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        return
    _scheduler = BackgroundScheduler(timezone=timezone.utc)
    for source_id, source_config in config.get("collectors", {}).items():
        schedule = source_config.get("schedule")
        if source_config.get("enabled", True) and schedule and schedule != "after_dependency":
            _scheduler.add_job(
                _scheduled_collector,
                CronTrigger.from_crontab(schedule, timezone=timezone.utc),
                args=[source_id, config],
                id=f"collector:{source_id}",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
    for processor_id, processor_config in config.get("processors", {}).items():
        schedule = processor_config.get("schedule")
        if processor_config.get("enabled", False) and schedule and schedule != "after_dependency":
            _scheduler.add_job(
                _scheduled_processor,
                CronTrigger.from_crontab(schedule, timezone=timezone.utc),
                args=[processor_id, config],
                id=f"processor:{processor_id}",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
    _scheduler.start()
    logger.info("scheduler_started", jobs=len(_scheduler.get_jobs()))


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


def scheduler_status() -> dict:
    if not _scheduler:
        return {"status": "stopped", "jobs": []}
    return {
        "status": "running" if _scheduler.running else "stopped",
        "jobs": [
            {
                "id": job.id,
                "next_due_at": job.next_run_time.isoformat() if job.next_run_time else None,
            }
            for job in _scheduler.get_jobs()
        ],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
