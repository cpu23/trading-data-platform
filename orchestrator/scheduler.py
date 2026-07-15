from datetime import datetime, timezone
from uuid import uuid4

from apscheduler.schedulers.background import BackgroundScheduler


from logging_config import get_logger
from schedules import build_cron_trigger
from orchestrator import (
    accept_run,
    aggregate_stage_statuses,
    finalize_run_safely,
    maintain_run_heartbeat,
    run_collector,
    run_processor,
    start_run,
)

logger = get_logger("scheduler")
_scheduler: BackgroundScheduler | None = None

def _start_scheduled_run(
    config: dict, correlation_id: str, worker_id: str, run_kind: str, component: str
) -> bool | None:
    """Claim an accepted scheduled run, finalizing it when the claim errors."""
    try:
        return start_run(config, correlation_id, worker_id)
    except Exception:
        reason = "run start unavailable"
        logger.error(
            "scheduled_run_start_failed",
            correlation_id=correlation_id,
            run_kind=run_kind,
            component=component,
        )
        finalize_run_safely(
            correlation_id,
            "failed",
            {"status": "failed", "reason": reason},
            config,
            reason,
            run_kind=run_kind,
            component=component,
        )
        return None


_build_cron_trigger = build_cron_trigger


def _scheduled_collector(source_id: str, config: dict) -> None:
    correlation_id = str(uuid4())
    accept_run(config, correlation_id, "scheduler", "collector", source_id)
    worker_id = f"scheduler:{uuid4()}"
    if _start_scheduled_run(
        config, correlation_id, worker_id, "collector", source_id
    ) is not True:
        return
    try:
        with maintain_run_heartbeat(config, correlation_id, worker_id):
            stages = _run_scheduled_collector_stages(source_id, config, correlation_id)
            overall = aggregate_stage_statuses(
                item["status"] for item in stages.values()
            )
            finalized = finalize_run_safely(
                correlation_id,
                overall,
                {"stages": stages},
                config,
                worker_id=worker_id,
                run_kind="collector",
                component=source_id,
            )
            if finalized:
                logger.info(
                    "scheduled_collector_completed",
                    source_id=source_id,
                    correlation_id=correlation_id,
                )
    except Exception as exc:
        logger.error(
            "scheduled_collector_failed",
            source_id=source_id,
            correlation_id=correlation_id,
            error=str(exc),
        )
        finalize_run_safely(
            correlation_id,
            "failed",
            {},
            config,
            str(exc),
            worker_id=worker_id,
            run_kind="collector",
            component=source_id,
        )


def _run_scheduled_collector_stages(
    source_id: str, config: dict, correlation_id: str
) -> dict:
    result = run_collector(
        source_id,
        config=config,
        correlation_id=correlation_id,
        manage_lifecycle=False,
    )
    stages = {source_id: result}
    if result["status"] in ("success", "partial"):
        for processor_id, processor_config in config.get("processors", {}).items():
            if not processor_config.get("enabled", False):
                continue
            if processor_config.get("schedule") != "after_dependency":
                continue
            from processors import get_processor
            if source_id in get_processor(processor_id).get_depends_on():
                stages[processor_id] = run_processor(
                    processor_id,
                    config=config,
                    correlation_id=correlation_id,
                    manage_lifecycle=False,
                )
    return stages


def _scheduled_processor(processor_id: str, config: dict) -> None:
    correlation_id = str(uuid4())
    accept_run(config, correlation_id, "scheduler", "processor", processor_id)
    worker_id = f"scheduler:{uuid4()}"
    if _start_scheduled_run(
        config, correlation_id, worker_id, "processor", processor_id
    ) is not True:
        return
    try:
        with maintain_run_heartbeat(config, correlation_id, worker_id):
            result = run_processor(
                processor_id,
                config=config,
                correlation_id=correlation_id,
                manage_lifecycle=False,
            )
            if result is None:
                raise RuntimeError("run returned no result after ownership was claimed")
            finalized = finalize_run_safely(
                correlation_id,
                result["status"],
                result,
                config,
                result.get("error"),
                worker_id=worker_id,
                run_kind="processor",
                component=processor_id,
            )
            if finalized:
                logger.info(
                    "scheduled_processor_completed",
                    processor_id=processor_id,
                    correlation_id=correlation_id,
                )
    except Exception as exc:
        logger.error(
            "scheduled_processor_failed",
            processor_id=processor_id,
            correlation_id=correlation_id,
            error=str(exc),
        )
        finalize_run_safely(
            correlation_id,
            "failed",
            {},
            config,
            str(exc),
            worker_id=worker_id,
            run_kind="processor",
            component=processor_id,
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
                _build_cron_trigger(schedule),
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
                _build_cron_trigger(schedule),
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
