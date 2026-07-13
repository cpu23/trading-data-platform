import json
import threading
import time
import traceback
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from collectors import get_all_collectors, get_collector
from collectors.base import CollectionResult
from db import get_session, insert_records, upsert_records
from logging_config import get_logger
from locks import RunConflict, advisory_lock
from processors import get_all_processors, get_processor
from sqlalchemy import text

logger = get_logger("orchestrator")

DEFAULT_ACCEPTED_TIMEOUT = timedelta(minutes=15)
DEFAULT_HEARTBEAT_TIMEOUT = timedelta(minutes=5)
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30.0


class RunAcceptanceConflict(RuntimeError):
    """A correlation or idempotency key already owns a durable run."""


class RunStartConflict(RuntimeError):
    """A synchronously invoked run could not claim its accepted row."""


def _resolved_config(config: dict | None) -> dict:
    if config is not None:
        return config
    from config_loader import load_config

    return load_config()


def accept_run(
    config: dict,
    correlation_id: str,
    triggered_by: str,
    run_kind: str,
    requested_component: str | None = None,
    idempotency_key: str | None = None,
) -> datetime:
    """Persist durable acceptance, raising a typed conflict on unique-key races."""
    from sqlalchemy.exc import IntegrityError

    accepted_at = datetime.now(timezone.utc)
    try:
        with get_session(config) as session:
            session.execute(
                text(
                    "INSERT INTO cycle_runs "
                    "(correlation_id, status, accepted_at, triggered_by, run_kind, "
                    "requested_component, idempotency_key) "
                    "VALUES (:cid, 'accepted', :accepted_at, :triggered_by, :run_kind, "
                    ":component, :idempotency_key)"
                ),
                {
                    "cid": correlation_id,
                    "accepted_at": accepted_at,
                    "triggered_by": triggered_by,
                    "run_kind": run_kind,
                    "component": requested_component,
                    "idempotency_key": idempotency_key,
                },
            )
    except IntegrityError as exc:
        logger.info(
            "run_acceptance_conflict",
            action="accept_run",
            correlation_id=correlation_id,
            run_kind=run_kind,
        )
        raise RunAcceptanceConflict("run correlation or idempotency key already exists") from exc
    return accepted_at


def reconcile_abandoned_runs(
    config: dict,
    now: datetime | None = None,
    accepted_timeout: timedelta = DEFAULT_ACCEPTED_TIMEOUT,
    heartbeat_timeout: timedelta = DEFAULT_HEARTBEAT_TIMEOUT,
) -> dict:
    """Mark jobs that could not have survived a process restart as abandoned."""
    completed_at = now or datetime.now(timezone.utc)
    accepted_reason = "abandoned by restart reconciliation: acceptance timeout exceeded"
    running_reason = "abandoned by restart reconciliation: heartbeat timeout exceeded"

    with get_session(config) as session:
        accepted_result = session.execute(
            text(
                "UPDATE cycle_runs SET status = :abandoned, result_status = :abandoned, "
                "completed_at = :completed_at, error_message = :reason "
                "WHERE status = 'accepted' AND accepted_at < :cutoff "
                "RETURNING correlation_id"
            ),
            {
                "abandoned": "abandoned",
                "completed_at": completed_at,
                "reason": accepted_reason,
                "cutoff": completed_at - accepted_timeout,
            },
        )
        running_result = session.execute(
            text(
                "UPDATE cycle_runs SET status = :abandoned, result_status = :abandoned, "
                "completed_at = :completed_at, error_message = :reason "
                "WHERE status = 'running' "
                "AND COALESCE(heartbeat_at, started_at) < :cutoff "
                "RETURNING correlation_id"
            ),
            {
                "abandoned": "abandoned",
                "completed_at": completed_at,
                "reason": running_reason,
                "cutoff": completed_at - heartbeat_timeout,
            },
        )
        accepted_ids = list(accepted_result.scalars().all())
        running_ids = list(running_result.scalars().all())

    return {
        "accepted_ids": accepted_ids,
        "running_ids": running_ids,
        "total": len(accepted_ids) + len(running_ids),
    }


def get_run_for_retry(config: dict, correlation_id: str) -> dict | None:
    """Return the immutable dispatch metadata needed for an explicit retry."""
    with get_session(config) as session:
        row = session.execute(
            text(
                "SELECT correlation_id, status, run_kind, requested_component, triggered_by "
                "FROM cycle_runs WHERE correlation_id = :cid"
            ),
            {"cid": correlation_id},
        ).fetchone()
    return dict(row._mapping) if row is not None else None


def start_run(config: dict, correlation_id: str, worker_id: str) -> bool:
    now = datetime.now(timezone.utc)
    with get_session(config) as session:
        result = session.execute(
            text(
                "UPDATE cycle_runs SET status = 'running', started_at = :started_at, "
                "heartbeat_at = :heartbeat_at, worker_id = :worker_id "
                "WHERE correlation_id = :cid AND status = 'accepted'"
            ),
            {
                "cid": correlation_id,
                "started_at": now,
                "heartbeat_at": now,
                "worker_id": worker_id,
            },
        )
        return result.rowcount == 1


def heartbeat_run(
    config: dict, correlation_id: str, worker_id: str | None = None
) -> bool:
    owner_clause = " AND worker_id = :worker_id" if worker_id is not None else ""
    with get_session(config) as session:
        result = session.execute(
            text(
                "UPDATE cycle_runs SET heartbeat_at = :heartbeat_at "
                "WHERE correlation_id = :cid AND status = 'running'" + owner_clause
            ),
            {
                "cid": correlation_id,
                "heartbeat_at": datetime.now(timezone.utc),
                "worker_id": worker_id,
            },
        )
        return result.rowcount == 1


def _heartbeat_interval_seconds(config: dict) -> float:
    jobs = config.get("jobs", {})
    if not isinstance(jobs, dict):
        return DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    try:
        interval = float(
            jobs.get("heartbeat_interval_seconds", DEFAULT_HEARTBEAT_INTERVAL_SECONDS)
        )
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    return interval if interval > 0 else DEFAULT_HEARTBEAT_INTERVAL_SECONDS


@contextmanager
def maintain_run_heartbeat(
    config: dict,
    correlation_id: str,
    worker_id: str,
    *,
    event_factory=threading.Event,
    thread_factory=threading.Thread,
):
    """Maintain an owned running row without sharing caller DB sessions."""
    stop_event = event_factory()
    interval = _heartbeat_interval_seconds(config)

    def heartbeat_loop() -> None:
        while not stop_event.wait(interval):
            try:
                owned = heartbeat_run(config, correlation_id, worker_id)
            except Exception:
                logger.error(
                    "run_heartbeat_failed",
                    action="heartbeat_run",
                    correlation_id=correlation_id,
                    worker_id=worker_id,
                )
                continue
            if not owned:
                logger.warning(
                    "run_heartbeat_lost_ownership",
                    action="heartbeat_run",
                    correlation_id=correlation_id,
                    worker_id=worker_id,
                )
                return

    thread = thread_factory(
        target=heartbeat_loop,
        name=f"run-heartbeat-{correlation_id}",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stop_event.set()
        thread.join()


def ensure_run(
    correlation_id: str,
    config: dict,
    run_kind: str = "cycle",
    requested_component: str | None = None,
    triggered_by: str = "internal",
) -> None:
    """Compatibility helper for callers that need an immediate accepted→running run."""
    accept_run(
        config,
        correlation_id,
        triggered_by,
        run_kind,
        requested_component=requested_component,
    )
    if not start_run(config, correlation_id, f"sync:{uuid4()}"):
        raise RunStartConflict("accepted run could not be started")


def finish_run(
    correlation_id: str,
    result_status: str,
    summary: dict,
    config: dict,
    error_message: str | None = None,
    worker_id: str | None = None,
) -> bool:
    lifecycle_status = "failed" if result_status == "failed" else "completed"
    allowed_from = "('running', 'accepted')" if lifecycle_status == "failed" else "('running')"
    owner_clause = " AND worker_id = :worker_id" if worker_id is not None else ""
    with get_session(config) as session:
        result = session.execute(
            text(
                "UPDATE cycle_runs SET status = :status, result_status = :result_status, "
                "summary = CAST(:summary AS JSONB), completed_at = :completed_at, "
                "heartbeat_at = :completed_at, error_message = :error_message "
                f"WHERE correlation_id = :cid AND status IN {allowed_from}" + owner_clause
            ),
            {
                "cid": correlation_id,
                "status": lifecycle_status,
                "result_status": result_status,
                "summary": json.dumps(summary),
                "completed_at": datetime.now(timezone.utc),
                "error_message": error_message,
                "worker_id": worker_id,
            },
        )
        return result.rowcount == 1


def finalize_run_safely(
    correlation_id: str,
    result_status: str,
    summary: dict,
    config: dict,
    error_message: str | None = None,
    *,
    worker_id: str | None = None,
    run_kind: str,
    component: str | None = None,
) -> bool:
    """Finalize durably without masking the work outcome or exception."""
    try:
        finalized = finish_run(
            correlation_id,
            result_status,
            summary,
            config,
            error_message,
            worker_id,
        )
    except Exception:
        logger.error(
            "run_finalization_failed",
            action="finish_run",
            correlation_id=correlation_id,
            run_kind=run_kind,
            component=component,
            worker_id=worker_id,
        )
        return False
    if not finalized:
        logger.warning(
            "run_finalization_lost_ownership",
            action="finish_run",
            correlation_id=correlation_id,
            run_kind=run_kind,
            component=component,
            worker_id=worker_id,
        )
        return False
    return True


def update_run_progress(
    correlation_id: str,
    progress: dict,
    config: dict,
    worker_id: str | None = None,
) -> bool:
    owner_clause = " AND worker_id = :worker_id" if worker_id is not None else ""
    try:
        with get_session(config) as session:
            result = session.execute(
                text(
                    "UPDATE cycle_runs SET summary = CAST(:summary AS JSONB), "
                    "heartbeat_at = :heartbeat_at "
                    "WHERE correlation_id = :cid AND status = 'running'" + owner_clause
                ),
                {
                    "cid": correlation_id,
                    "summary": json.dumps({"progress": progress}),
                    "heartbeat_at": datetime.now(timezone.utc),
                    "worker_id": worker_id,
                },
            )
            return result.rowcount == 1
    except Exception as exc:
        logger.error(
            "cycle_progress_write_failed",
            action="update_run_progress",
            correlation_id=correlation_id,
            error=str(exc),
        )
        return False


def _run_collector_impl(
    source_id: str,
    config: dict | None = None,
    correlation_id: str | None = None,
    manage_lifecycle: bool = True,
) -> dict | None:
    if config is None:
        from config_loader import load_config

        config = load_config()

    correlation_id = correlation_id or str(uuid4())
    if manage_lifecycle:
        accept_run(config, correlation_id, "internal", "collector", source_id)
        if not start_run(config, correlation_id, f"sync:{uuid4()}"):
            return None

    import structlog.contextvars

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)

    collector = get_collector(source_id)

    started_at = datetime.now(timezone.utc)
    start_ms = time.monotonic() * 1000

    logger.info(
        "collector_started",
        action="run_collector",
        collector=source_id,
        correlation_id=correlation_id,
    )

    records_fetched = 0
    records_written = 0
    api_calls_made = 0
    status = "success"
    error_message = None
    error_traceback = None
    collection_errors: list[dict] = []

    try:
        raw_result = collector.collect(config, correlation_id)

        # Normalise: accept both CollectionResult and plain list[dict]
        if isinstance(raw_result, CollectionResult):
            records = raw_result.records
            collection_errors = raw_result.errors
            # Derive collection-level status from structured result
            if raw_result.all_failed:
                status = "failed"
                error_message = (
                    f"All {raw_result.total_series} series failed: "
                    + "; ".join(
                        f"{e['series_id']}: {e['error']}" for e in raw_result.errors[:3]
                    )
                )
            elif raw_result.partial_failure:
                status = "partial"
        else:
            records = raw_result
            # Backward compat: try collector.last_errors
            collection_errors = getattr(collector, "last_errors", [])

        records_fetched = len(records)

        if records:
            table_name = collector.get_target_table()
            conflict_columns = collector.get_conflict_columns()
            write_result = upsert_records(
                table_name=table_name,
                records=records,
                conflict_columns=conflict_columns,
                config=config,
            )
            records_written = write_result.written

            # Derive write-level status using WriteResult.status (Task 8)
            write_status = write_result.status
            if write_status == "failed":
                status = "failed"
                if not error_message:
                    error_message = (
                        f"All {write_result.attempted} DB writes failed "
                        f"for table {table_name}"
                    )
            elif write_status == "partial":
                if status == "success":
                    status = "partial"
                error_message = error_message or (
                    f"Partial DB write: {write_result.written}/{write_result.attempted} "
                    f"records written to {table_name}"
                )

        api_calls_made = _estimate_api_calls(source_id, records_fetched, config)

    except Exception as exc:
        status = "failed"
        error_message = str(exc)
        error_traceback = traceback.format_exc()
        logger.error(
            "collector_failed",
            action="run_collector",
            collector=source_id,
            error=str(exc),
            correlation_id=correlation_id,
        )

    completed_at = datetime.now(timezone.utc)
    duration_ms = int(time.monotonic() * 1000 - start_ms)

    _write_collection_log(
        collector_id=source_id,
        started_at=started_at,
        completed_at=completed_at,
        status=status,
        records_fetched=records_fetched,
        records_written=records_written,
        error_message=error_message,
        error_traceback=error_traceback,
        duration_ms=duration_ms,
        api_calls_made=api_calls_made,
        config=config,
        correlation_id=correlation_id,
    )

    result = {
        "collector": source_id,
        "status": status,
        "records_fetched": records_fetched,
        "records_written": records_written,
        "duration_ms": duration_ms,
        "error": error_message,
        "correlation_id": correlation_id,
    }

    logger.info(
        "collector_work_finished",
        action="run_collector",
        **result,
    )

    if manage_lifecycle:
        finalize_run_safely(
            correlation_id, status, result, config, error_message,
            run_kind="collector", component=source_id,
        )

    return result


def run_collector(
    source_id: str,
    config: dict | None = None,
    correlation_id: str | None = None,
    manage_lifecycle: bool = True,
) -> dict | None:
    config = _resolved_config(config)
    correlation_id = correlation_id or str(uuid4())
    lifecycle_created = False
    worker_id: str | None = None
    try:
        if manage_lifecycle:
            accept_run(config, correlation_id, "internal", "collector", source_id)
            lifecycle_created = True
            worker_id = f"sync:{uuid4()}"
            if not start_run(config, correlation_id, worker_id):
                return None
        heartbeat = (
            maintain_run_heartbeat(config, correlation_id, worker_id)
            if worker_id is not None
            else nullcontext()
        )
        with heartbeat:
            with advisory_lock(f"collector:{source_id}", config):
                result = _run_collector_impl(
                    source_id, config, correlation_id, manage_lifecycle=False
                )
            if manage_lifecycle and result is not None:
                finalized = finalize_run_safely(
                    correlation_id,
                    result["status"],
                    result,
                    config,
                    result.get("error"),
                    worker_id=worker_id,
                    run_kind="collector",
                    component=source_id,
                )
                if finalized:
                    logger.info("collector_completed", action="run_collector", **result)
            return result
    except RunAcceptanceConflict:
        raise
    except Exception as exc:
        if manage_lifecycle and lifecycle_created:
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
        raise


def _run_full_cycle_impl(
    config: dict | None = None,
    correlation_id: str | None = None,
    manage_lifecycle: bool = True,
    worker_id: str | None = None,
) -> dict | None:
    if config is None:
        from config_loader import load_config

        config = load_config()

    correlation_id = correlation_id or str(uuid4())
    if manage_lifecycle:
        accept_run(config, correlation_id, "internal", "cycle")
        if not start_run(config, correlation_id, f"sync:{uuid4()}"):
            return None

    logger.info(
        "full_cycle_started",
        action="run_full_cycle",
        correlation_id=correlation_id,
    )

    all_collectors = get_all_collectors()
    enabled_collectors = []

    for source_id, collector in all_collectors.items():
        collector_config = config.get("collectors", {}).get(source_id, {})
        if collector_config.get("enabled", True):
            enabled_collectors.append(source_id)

    collector_results = {}
    successful_collectors = set()
    enabled_processors = [
        processor_id
        for processor_id in get_all_processors()
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
        component: str,
        kind: str,
        status: str,
        result: dict | None = None,
    ) -> None:
        stage = next(
            item
            for item in progress["stages"]
            if item["component"] == component and item["kind"] == kind
        )
        stage["status"] = status
        if status == "running":
            stage["started_at"] = datetime.now(timezone.utc).isoformat()
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
                        )
                    }
                )
            progress["completed_stages"] = sum(
                item["status"] not in ("pending", "running")
                for item in progress["stages"]
            )
            progress["current_stage"] = None
            progress["current_kind"] = None
        update_run_progress(correlation_id, progress, config, worker_id)

    update_run_progress(correlation_id, progress, config, worker_id)

    for source_id in enabled_collectors:
        record_progress(source_id, "collector", "running")
        result = run_collector(
            source_id,
            config=config,
            correlation_id=correlation_id,
            manage_lifecycle=False,
        )
        collector_results[source_id] = result
        record_progress(source_id, "collector", result["status"], result)
        if result["status"] in ("success", "partial"):
            successful_collectors.add(source_id)

    processor_results = _resolve_and_run_processors(
        config=config,
        correlation_id=correlation_id,
        successful_collectors=successful_collectors,
        progress_callback=record_progress,
    )

    overall_status = "success"
    all_results = {**collector_results, **processor_results}
    if any(r["status"] == "failed" for r in all_results.values()):
        overall_status = (
            "partial"
            if any(r["status"] in ("success", "partial") for r in all_results.values())
            else "failed"
        )

    cycle_result = {
        "status": overall_status,
        "collectors": collector_results,
        "processors": processor_results,
        "correlation_id": correlation_id,
    }

    logger.info(
        "full_cycle_work_finished",
        action="run_full_cycle",
        overall_status=overall_status,
        collectors_run=list(collector_results.keys()),
        processors_run=list(processor_results.keys()),
        correlation_id=correlation_id,
    )

    if manage_lifecycle:
        finalize_run_safely(
            correlation_id, overall_status, cycle_result, config,
            worker_id=worker_id, run_kind="cycle",
        )
    return cycle_result


def run_full_cycle(
    config: dict | None = None,
    correlation_id: str | None = None,
    manage_lifecycle: bool = True,
) -> dict | None:
    config = _resolved_config(config)
    correlation_id = correlation_id or str(uuid4())
    lifecycle_created = False
    worker_id: str | None = None
    try:
        if manage_lifecycle:
            accept_run(config, correlation_id, "internal", "cycle")
            lifecycle_created = True
            worker_id = f"sync:{uuid4()}"
            if not start_run(config, correlation_id, worker_id):
                return None
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
        if manage_lifecycle and lifecycle_created:
            finalize_run_safely(
                correlation_id,
                "failed",
                {},
                config,
                str(exc),
                worker_id=worker_id,
                run_kind="cycle",
            )
        raise


def _estimate_api_calls(source_id: str, records_fetched: int, config: dict) -> int:
    if source_id == "fred":
        series_count = len(
            config.get("collectors", {}).get("fred", {}).get("series", [])
        )
        return series_count * 2
    if source_id == "oanda":
        oanda_config = config.get("collectors", {}).get("oanda", {})
        return 2 if oanda_config.get("instruments") else 1
    return 1


def _resolve_and_run_processors(
    config: dict,
    correlation_id: str,
    successful_collectors: set[str],
    progress_callback=None,
) -> dict:
    all_processors = get_all_processors()
    processor_results = {}
    successful_processors = set()
    remaining = {}

    for processor_id, processor in all_processors.items():
        processor_config = config.get("processors", {}).get(processor_id, {})
        if not processor_config.get("enabled", False):
            continue
        remaining[processor_id] = processor

    max_passes = len(remaining) + 1
    for _ in range(max_passes):
        if not remaining:
            break

        this_pass = {}
        for pid, proc in remaining.items():
            depends_on = proc.get_depends_on()
            deps_satisfied = all(
                dep in successful_collectors or dep in successful_processors
                for dep in depends_on
            )
            if deps_satisfied:
                this_pass[pid] = proc

        if not this_pass:
            for pid, proc in remaining.items():
                depends_on = proc.get_depends_on()
                logger.warning(
                    "processor_dependencies_not_met",
                    action="run_full_cycle",
                    processor=pid,
                    depends_on=depends_on,
                    successful_collectors=list(successful_collectors),
                    successful_processors=list(successful_processors),
                    correlation_id=correlation_id,
                )
                if progress_callback:
                    progress_callback(
                        pid,
                        "processor",
                        "skipped",
                        {"error": f"Dependencies not met: {', '.join(depends_on)}"},
                    )
            break

        for pid in this_pass:
            del remaining[pid]

        for pid in this_pass:
            if progress_callback:
                progress_callback(pid, "processor", "running")
            result = run_processor(
                pid,
                config=config,
                correlation_id=correlation_id,
                manage_lifecycle=False,
            )
            processor_results[pid] = result
            if progress_callback:
                progress_callback(pid, "processor", result["status"], result)
            if result["status"] in ("success", "partial"):
                successful_processors.add(pid)

    return processor_results


def _write_collection_log(
    collector_id: str,
    started_at: datetime,
    completed_at: datetime,
    status: str,
    records_fetched: int,
    records_written: int,
    error_message: str | None,
    error_traceback: str | None,
    duration_ms: int,
    api_calls_made: int,
    config: dict,
    correlation_id: str,
):
    config_snapshot = {}
    collector_config = config.get("collectors", {}).get(collector_id, {})
    safe_keys = ["schedule", "enabled"]
    for key in safe_keys:
        if key in collector_config:
            config_snapshot[key] = collector_config[key]
    if "series" in collector_config:
        config_snapshot["series_count"] = len(collector_config["series"])
    if "instruments" in collector_config:
        config_snapshot["instruments_count"] = len(
            [
                item
                for item in collector_config["instruments"]
                if item.get("enabled", True)
            ]
        )
    if "snapshot_timeframe" in collector_config:
        config_snapshot["snapshot_timeframe"] = collector_config["snapshot_timeframe"]

    log_record = {
        "started_at": started_at,
        "completed_at": completed_at,
        "collector": collector_id,
        "status": status,
        "records_fetched": records_fetched,
        "records_written": records_written,
        "error_message": error_message,
        "error_traceback": error_traceback,
        "duration_ms": duration_ms,
        "api_calls_made": api_calls_made,
        "config_snapshot": json.dumps(config_snapshot),
        "correlation_id": correlation_id,
    }

    try:
        with get_session(config) as session:
            columns = ", ".join(log_record.keys())
            placeholders = ", ".join(f":{k}" for k in log_record.keys())
            sql = text(
                f"INSERT INTO collection_log ({columns}) VALUES ({placeholders})"
            )
            session.execute(sql, log_record)
    except Exception as exc:
        logger.error(
            "collection_log_write_failed",
            action="write_collection_log",
            error=str(exc),
            correlation_id=correlation_id,
        )


def get_last_collection_runs(config: dict | None = None) -> list[dict]:
    if config is None:
        from config_loader import load_config

        config = load_config()

    sql = text(
        "SELECT DISTINCT ON (collector) * FROM collection_log "
        "ORDER BY collector, started_at DESC"
    )

    try:
        with get_session(config) as session:
            result = session.execute(sql)
            return [dict(row._mapping) for row in result]
    except Exception as exc:
        logger.error("last_runs_query_failed", action="get_last_runs", error=str(exc))
        return []


def _run_processor_impl(
    processor_id: str,
    config: dict | None = None,
    correlation_id: str | None = None,
    manage_lifecycle: bool = True,
) -> dict | None:
    if config is None:
        from config_loader import load_config

        config = load_config()

    correlation_id = correlation_id or str(uuid4())
    if manage_lifecycle:
        accept_run(config, correlation_id, "internal", "processor", processor_id)
        if not start_run(config, correlation_id, f"sync:{uuid4()}"):
            return None

    import structlog.contextvars

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)

    processor = get_processor(processor_id)

    started_at = datetime.now(timezone.utc)
    start_ms = time.monotonic() * 1000

    logger.info(
        "processor_started",
        action="run_processor",
        processor=processor_id,
        correlation_id=correlation_id,
    )

    status = "success"
    error_message = None
    result_payload = None

    try:
        result_payload = processor.process(config, correlation_id)
    except Exception as exc:
        status = "failed"
        error_message = str(exc)
        logger.error(
            "processor_failed",
            action="run_processor",
            processor=processor_id,
            error=str(exc),
            correlation_id=correlation_id,
        )

    completed_at = datetime.now(timezone.utc)
    duration_ms = int(time.monotonic() * 1000 - start_ms)

    if status == "success" and result_payload:
        try:
            opinion = result_payload["opinion"]
            opinion_written = insert_records(
                table_name="structured_opinions",
                records=[opinion],
                config=config,
            )

            extra_records = result_payload.get("extra_records", {})
            extra_written = {}
            for table_name, records in extra_records.items():
                if table_name == "daily_briefings":
                    count = upsert_records(
                        table_name=table_name,
                        records=records,
                        conflict_columns=["briefing_date"],
                        config=config,
                    )
                else:
                    count = insert_records(
                        table_name=table_name,
                        records=records,
                        config=config,
                    )
                extra_written[table_name] = count

            logger.info(
                "processor_records_written",
                action="run_processor",
                processor=processor_id,
                opinion_written=opinion_written,
                extra_written=extra_written,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            status = "partial"
            error_message = f"DB write failed: {exc}"
            logger.error(
                "processor_db_write_failed",
                action="run_processor",
                processor=processor_id,
                error=str(exc),
                correlation_id=correlation_id,
            )

    processing_log = None
    if status == "success" and result_payload:
        processing_log = result_payload.get("processing_log")
    processing_log = processing_log or {}

    processing_log.setdefault("processor", processor_id)
    processing_log.setdefault("status", status)
    processing_log.setdefault("started_at", started_at)
    processing_log.setdefault("completed_at", completed_at)
    processing_log.setdefault("duration_ms", duration_ms)

    if error_message:
        processing_log["error_message"] = error_message

    _write_processing_log(
        processor_id=processor_id,
        started_at=started_at,
        completed_at=completed_at,
        status=processing_log.get("status", status),
        input_summary=processing_log.get("input_summary"),
        output_id=processing_log.get("output_id"),
        prompt_text=processing_log.get("prompt_text"),
        raw_response=processing_log.get("raw_response"),
        model_used=processing_log.get("model_used"),
        tokens_input=processing_log.get("tokens_input"),
        tokens_output=processing_log.get("tokens_output"),
        cost_usd=processing_log.get("cost_usd"),
        duration_ms=duration_ms,
        error_message=error_message,
        config=config,
        correlation_id=correlation_id,
    )

    result = {
        "processor": processor_id,
        "status": status,
        "duration_ms": duration_ms,
        "error": error_message,
        "correlation_id": correlation_id,
        "opinion_id": result_payload["opinion"]["opinion_id"]
        if result_payload and status == "success"
        else None,
    }

    logger.info("processor_work_finished", action="run_processor", **result)
    if manage_lifecycle:
        finalize_run_safely(
            correlation_id, status, result, config, error_message,
            run_kind="processor", component=processor_id,
        )
    return result


def run_processor(
    processor_id: str,
    config: dict | None = None,
    correlation_id: str | None = None,
    manage_lifecycle: bool = True,
) -> dict | None:
    config = _resolved_config(config)
    correlation_id = correlation_id or str(uuid4())
    lifecycle_created = False
    worker_id: str | None = None
    try:
        if manage_lifecycle:
            accept_run(config, correlation_id, "internal", "processor", processor_id)
            lifecycle_created = True
            worker_id = f"sync:{uuid4()}"
            if not start_run(config, correlation_id, worker_id):
                return None
        heartbeat = (
            maintain_run_heartbeat(config, correlation_id, worker_id)
            if worker_id is not None
            else nullcontext()
        )
        with heartbeat:
            with advisory_lock(f"processor:{processor_id}", config):
                result = _run_processor_impl(
                    processor_id, config, correlation_id, manage_lifecycle=False
                )
            if manage_lifecycle and result is not None:
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
                    logger.info("processor_completed", action="run_processor", **result)
            return result
    except RunAcceptanceConflict:
        raise
    except Exception as exc:
        if manage_lifecycle and lifecycle_created:
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
        raise


def _write_processing_log(
    processor_id: str,
    started_at: datetime,
    completed_at: datetime,
    status: str,
    input_summary: dict | None,
    output_id: str | None,
    prompt_text: str | None,
    raw_response: str | None,
    model_used: str | None,
    tokens_input: int | None,
    tokens_output: int | None,
    cost_usd: float | None,
    duration_ms: int,
    error_message: str | None,
    config: dict,
    correlation_id: str,
):
    log_record = {
        "started_at": started_at,
        "completed_at": completed_at,
        "processor": processor_id,
        "status": status,
        "input_summary": json.dumps(input_summary) if input_summary else None,
        "output_id": output_id,
        "prompt_text": prompt_text,
        "raw_response": raw_response,
        "model_used": model_used,
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "cost_usd": cost_usd,
        "duration_ms": duration_ms,
        "error_message": error_message,
        "correlation_id": correlation_id,
    }

    try:
        with get_session(config) as session:
            columns = ", ".join(log_record.keys())
            placeholders = ", ".join(f":{k}" for k in log_record.keys())
            sql = text(
                f"INSERT INTO processing_log ({columns}) VALUES ({placeholders})"
            )
            session.execute(sql, log_record)
    except Exception as exc:
        logger.error(
            "processing_log_write_failed",
            action="write_processing_log",
            error=str(exc),
            correlation_id=correlation_id,
        )
