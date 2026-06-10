from datetime import datetime, timezone
from uuid import uuid4

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from logging_config import get_logger
from orchestrator import finish_run, run_collector, run_processor

logger = get_logger("scheduler")
_scheduler: BackgroundScheduler | None = None


def _scheduled_collector(source_id: str, config: dict) -> None:
    correlation_id = str(uuid4())
    result = run_collector(source_id, config=config, correlation_id=correlation_id)
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
                    processor_id, config=config, correlation_id=correlation_id
                )
    overall = "failed" if all(item["status"] == "failed" for item in stages.values()) else (
        "partial" if any(item["status"] == "failed" for item in stages.values()) else "success"
    )
    finish_run(correlation_id, overall, {"stages": stages}, config)


def _scheduled_processor(processor_id: str, config: dict) -> None:
    correlation_id = str(uuid4())
    result = run_processor(processor_id, config=config, correlation_id=correlation_id)
    finish_run(correlation_id, result["status"], result, config, result.get("error"))


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
