"""Schedule loop for durable operation jobs.

The scheduler never executes a run inline: every scheduled fire calls the
transactional ``accept_and_enqueue_operation`` and returns.  A worker role
claims the durable job and executes it.  Duplicate logical runs are prevented
by the operation_jobs active-identity index plus an advisory transaction lock
keyed by the logical window, so two scheduler processes firing for the same
window enqueue exactly one job.
"""

from datetime import UTC, datetime
from uuid import uuid4

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import text

from logging_config import get_logger
from operation_jobs import accept_and_enqueue_operation
from schedules import build_cron_trigger

logger = get_logger("scheduler")
_scheduler: BackgroundScheduler | None = None

_build_cron_trigger = build_cron_trigger


def _window_key(fired_at: datetime) -> str:
    """Stable logical identity for one scheduled window (UTC minute)."""
    return fired_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:00")


def _enqueue_scheduled_run(
    config: dict,
    run_kind: str,
    component: str | None,
    *,
    dedupe_key: str,
    payload: dict | None = None,
    priority: int = 100,
    max_attempts: int = 5,
) -> None:
    correlation_id = str(uuid4())
    fired_at = datetime.now(UTC)
    identity = f"{dedupe_key}:{_window_key(fired_at)}"
    try:
        accepted_at, enqueued = accept_and_enqueue_operation(
            config,
            correlation_id=correlation_id,
            triggered_by="scheduler",
            run_kind=run_kind,
            requested_component=component,
            request_summary={
                "triggered_by": "scheduler",
                "run_kind": run_kind,
                "component": component,
                "window": _window_key(fired_at),
            },
            dedupe_key=identity,
            input_fingerprint=str(
                int(fired_at.replace(second=0, microsecond=0).timestamp())
            ),
            payload=payload or {},
            priority=priority,
            max_attempts=max_attempts,
        )
    except Exception as exc:
        logger.error(
            "scheduled_enqueue_failed",
            run_kind=run_kind,
            component=component,
            error_type=type(exc).__name__,
        )
        return
    if enqueued.inserted:
        logger.info(
            "scheduled_run_enqueued",
            run_kind=run_kind,
            component=component,
            correlation_id=correlation_id,
        )
    else:
        logger.info(
            "scheduled_run_deduplicated",
            run_kind=run_kind,
            component=component,
            correlation_id=correlation_id,
            accepted_at=accepted_at.isoformat(),
        )


def _scheduled_collector(source_id: str, config: dict) -> None:
    _enqueue_scheduled_run(
        config,
        "collector",
        source_id,
        dedupe_key=f"collector:{source_id}",
        payload={
            "run_dependents": True,
            "mode": "refresh",
        },
        priority=90,
        max_attempts=3,
    )


def _scheduled_processor(processor_id: str, config: dict) -> None:
    _enqueue_scheduled_run(
        config,
        "processor",
        processor_id,
        dedupe_key=f"processor:{processor_id}",
        payload={"mode": "refresh"},
        priority=90,
        max_attempts=3,
    )


def _scheduled_news(source_id: str, config: dict) -> None:
    _enqueue_scheduled_run(
        config,
        "news",
        source_id,
        dedupe_key=f"news:{source_id}",
        payload={"mode": "refresh"},
        priority=90,
        max_attempts=3,
    )


def _scheduled_filings(config: dict) -> None:
    _enqueue_scheduled_run(
        config,
        "filings",
        "investment_filings",
        dedupe_key="filings:investment_filings",
        payload={
            "auto_analyze": bool(
                config.get("investment_filings", {}).get("auto_analyze", False)
            )
        },
        priority=90,
        max_attempts=3,
    )


def _scheduled_research(config: dict) -> None:
    """Enqueue the bounded research workflow on the durable analysis queue."""
    from research_intelligence.operations import enqueue_research_job

    try:
        enqueue_research_job(
            config,
            job_type="research_discovery",
            force=False,
            triggered_by="scheduler",
        )
    except Exception as exc:
        logger.error(
            "scheduled_research_enqueue_failed",
            error_type=type(exc).__name__,
        )


def _scheduled_thesis_autonomy(config: dict) -> None:
    """Enqueue one bounded autonomous thesis cycle on the durable queue."""
    from thesis_autonomy import enqueue_thesis_autonomy_job

    try:
        enqueue_thesis_autonomy_job(config, triggered_by="scheduler")
    except Exception as exc:
        logger.error(
            "scheduled_thesis_autonomy_enqueue_failed",
            error_type=type(exc).__name__,
        )


def _try_acquire_leader_connection(config: dict):
    """Try to take the scheduler advisory leader lock; None when not leader.

    The lock is session-scoped: the returned connection must stay open while
    this process is the leader, and closing it releases the lock so a standby
    can take over.
    """
    from db import get_engine

    engine = get_engine(config)
    connection = engine.connect()
    try:
        acquired = connection.execute(
            text("SELECT pg_try_advisory_lock(hashtext('scheduler-leader'))")
        ).scalar()
    except Exception:
        connection.close()
        raise
    if not acquired:
        connection.close()
        return None
    return connection


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
    research_config = config.get("research_intelligence", {})
    research_schedule = research_config.get("schedule")
    if (
        research_config.get("enabled", False)
        and research_config.get("schedule_enabled", False)
        and research_schedule
    ):
        _scheduler.add_job(
            _scheduled_research,
            _build_cron_trigger(research_schedule),
            args=[config],
            id="research:discovery",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
    autonomy_config = config.get("thesis_autonomy", {})
    autonomy_schedule = autonomy_config.get("schedule")
    if (
        autonomy_config.get("enabled", False)
        and autonomy_config.get("schedule_enabled", False)
        and autonomy_schedule
    ):
        _scheduler.add_job(
            _scheduled_thesis_autonomy,
            _build_cron_trigger(autonomy_schedule),
            args=[config],
            id="thesis-autonomy:run",
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
