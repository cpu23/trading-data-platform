from datetime import UTC, datetime
from uuid import uuid4

from apscheduler.schedulers.background import BackgroundScheduler

from logging_config import get_logger
from orchestrator import (
    RunAcceptanceConflict,
    accept_run,
    aggregate_stage_statuses,
    finalize_run_safely,
    maintain_run_heartbeat,
    run_collector,
    run_news_source,
    run_processor,
    start_run,
)
from schedules import build_cron_trigger

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
    if (
        _start_scheduled_run(config, correlation_id, worker_id, "collector", source_id)
        is not True
    ):
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
    if (
        _start_scheduled_run(
            config, correlation_id, worker_id, "processor", processor_id
        )
        is not True
    ):
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


def _scheduled_news(source_id: str, config: dict) -> None:
    correlation_id = str(uuid4())
    try:
        accept_run(config, correlation_id, "scheduler", "news", source_id)
    except RunAcceptanceConflict:
        logger.info("scheduled_news_acceptance_conflict", source_id=source_id)
        return
    except Exception:
        logger.error("scheduled_news_acceptance_failed", source_id=source_id)
        return
    worker_id = f"scheduler:{uuid4()}"
    if (
        _start_scheduled_run(config, correlation_id, worker_id, "news", source_id)
        is not True
    ):
        return
    try:
        with maintain_run_heartbeat(config, correlation_id, worker_id):
            result = run_news_source(
                source_id, correlation_id, config, manage_lifecycle=False
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
                run_kind="news",
                component=source_id,
            )
            if finalized:
                logger.info(
                    "scheduled_news_completed",
                    source_id=source_id,
                    correlation_id=correlation_id,
                )
    except Exception as exc:
        from locks import RunConflict

        conflict = isinstance(exc, RunConflict)
        reason = str(exc) if conflict else "news run failed"
        summary = {
            "status": "failed",
            "state": "conflict" if conflict else "failed",
            "error": reason,
            "code": "news_run_conflict" if conflict else "news_run_failed",
            "feed_published": False,
            "new_item_count": 0,
            "duration_ms": 0,
            "correlation_id": correlation_id,
        }
        logger.error(
            "scheduled_news_failed",
            source_id=source_id,
            correlation_id=correlation_id,
            code=summary["code"],
        )
        finalize_run_safely(
            correlation_id,
            "failed",
            summary,
            config,
            reason,
            worker_id=worker_id,
            run_kind="news",
            component=source_id,
        )


def _scheduled_filings(config: dict) -> None:
    from investment_filings import run_filing_collection

    correlation_id = str(uuid4())
    worker_id = f"scheduler:{uuid4()}"
    try:
        accept_run(config, correlation_id, "scheduler", "filings", "investment_filings")
    except Exception:
        logger.warning(
            "scheduled_filings_acceptance_failed", correlation_id=correlation_id
        )
        return
    if (
        _start_scheduled_run(
            config, correlation_id, worker_id, "filings", "investment_filings"
        )
        is not True
    ):
        return
    try:
        with maintain_run_heartbeat(config, correlation_id, worker_id):
            filings_config = config.get("investment_filings", {})
            auto_analyze = filings_config.get("auto_analyze", False)
            result = run_filing_collection(
                config, correlation_id=correlation_id, auto_analyze=auto_analyze
            )
            finalized = finalize_run_safely(
                correlation_id,
                result.get("status", "completed"),
                result,
                config,
                worker_id=worker_id,
                run_kind="filings",
                component="investment_filings",
            )
            if finalized:
                logger.info(
                    "scheduled_filings_completed",
                    correlation_id=correlation_id,
                    ingested=result.get("ingested", 0),
                )
    except Exception as exc:
        logger.error(
            "scheduled_filings_failed",
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
            run_kind="filings",
            component="investment_filings",
        )


def start_scheduler(config: dict) -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        return
    _scheduler = BackgroundScheduler(timezone=UTC)
    for source_id, source_config in config.get("collectors", {}).items():
        schedule = source_config.get("schedule")
        if (
            source_config.get("enabled", True)
            and schedule
            and schedule != "after_dependency"
        ):
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
        if (
            processor_config.get("enabled", False)
            and schedule
            and schedule != "after_dependency"
        ):
            _scheduler.add_job(
                _scheduled_processor,
                _build_cron_trigger(schedule),
                args=[processor_id, config],
                id=f"processor:{processor_id}",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
    if not config.get("demo", {}).get("enabled", False):
        from sources.news_registry import get_news_source_ids

        for source_id in get_news_source_ids():
            source_config = config.get(source_id, {})
            schedule = source_config.get("schedule")
            if not (
                source_config.get("enabled", False)
                and source_config.get("schedule_enabled", False)
                and schedule
            ):
                continue
            _scheduler.add_job(
                _scheduled_news,
                _build_cron_trigger(schedule),
                args=[source_id, config],
                id=f"news:{source_id}",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
    # Investment filings collection
    filings_config = config.get("investment_filings", {})
    filings_schedule = filings_config.get("schedule", "")
    if filings_config.get("enabled", False) and filings_schedule:
        filing_job_options = {}
        if filings_config.get("run_on_startup", True):
            filing_job_options["next_run_time"] = datetime.now(UTC)
        _scheduler.add_job(
            _scheduled_filings,
            _build_cron_trigger(filings_schedule),
            args=[config],
            id="filings:investment_filings",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            **filing_job_options,
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
                "next_due_at": job.next_run_time.isoformat()
                if job.next_run_time
                else None,
            }
            for job in _scheduler.get_jobs()
        ],
        "checked_at": datetime.now(UTC).isoformat(),
    }
