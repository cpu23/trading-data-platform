import json
import threading
import time
import traceback
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from budgets import BudgetBlock, BudgetContext, BudgetExceeded
from collectors import get_all_collectors, get_collector
from collectors.base import CollectionResult, elapsed_ms
from db import get_session, insert_records, upsert_records
from logging_config import get_logger
from locks import RunConflict, advisory_lock
from llm_client import resolve_model
from processors import get_all_processors, get_processor
from processors.base import canonical_fingerprint
from schedules import build_cron_trigger
from sqlalchemy import text

logger = get_logger("orchestrator")

DEFAULT_ACCEPTED_TIMEOUT = timedelta(minutes=15)
DEFAULT_HEARTBEAT_TIMEOUT = timedelta(minutes=5)
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30.0
DEFAULT_COLLECTOR_WORKERS = 3
MAX_COLLECTOR_WORKERS = 8
VALID_CYCLE_MODES = frozenset({"refresh", "analyze", "force_full"})


class RunAcceptanceConflict(RuntimeError):
    """A correlation or idempotency key already owns a durable run."""


class RunStartConflict(RuntimeError):
    """A synchronously invoked run could not claim its accepted row."""


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
    return "partial"


def build_processor_fingerprint(processor, config: dict) -> str | None:
    """Hash bounded processor markers plus explicit prompt/model/code versions."""
    input_builder = getattr(processor, "get_fingerprint_inputs", None)
    schema_version = getattr(processor, "PROCESSOR_SCHEMA_VERSION", None)
    if not callable(input_builder) or schema_version is None:
        return None
    payload = {
        "processor_id": processor.processor_id,
        "processor_schema_version": str(schema_version),
        "prompt_version": processor.get_prompt_version(),
        "prompt_identity": processor.get_prompt_identity(config),
        "model": resolve_model(config, processor_id=processor.processor_id),
        "inputs": input_builder(config),
    }
    return canonical_fingerprint(payload)


def _find_reusable_processor_output(
    processor_id: str, fingerprint: str, config: dict
) -> dict | None:
    """Find a prior successful output; failed or blocked rows are never reusable."""
    sql = text(
        "SELECT output_id, completed_at FROM processing_log "
        "WHERE processor = :processor AND input_fingerprint = :fingerprint "
        "AND status = 'success' AND output_id IS NOT NULL "
        "ORDER BY completed_at DESC LIMIT 1"
    )
    with get_session(config) as session:
        row = session.execute(
            sql, {"processor": processor_id, "fingerprint": fingerprint}
        ).fetchone()
    return dict(row._mapping) if row is not None else None


def collector_worker_limit(config: dict, enabled_count: int) -> int:
    """Return a safe collector pool size, capped by the available work."""
    orchestration = config.get("orchestration", {})
    if not isinstance(orchestration, dict):
        orchestration = {}
    try:
        configured = int(
            orchestration.get("collector_workers", DEFAULT_COLLECTOR_WORKERS)
        )
    except (TypeError, ValueError, OverflowError):
        configured = DEFAULT_COLLECTOR_WORKERS
    configured = min(max(configured, 1), MAX_COLLECTOR_WORKERS)
    return min(configured, max(enabled_count, 0))


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
    request_summary: dict | None = None,
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
                    "requested_component, idempotency_key, summary) "
                    "VALUES (:cid, 'accepted', :accepted_at, :triggered_by, :run_kind, "
                    ":component, :idempotency_key, CAST(:summary AS JSONB))"
                ),
                {
                    "cid": correlation_id,
                    "accepted_at": accepted_at,
                    "triggered_by": triggered_by,
                    "run_kind": run_kind,
                    "component": requested_component,
                    "idempotency_key": idempotency_key,
                    "summary": json.dumps(request_summary) if request_summary else None,
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
                "SELECT correlation_id, status, run_kind, requested_component, triggered_by, summary "
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
                    "UPDATE cycle_runs SET summary = "
                    "COALESCE(summary, CAST('{}' AS JSONB)) || CAST(:summary AS JSONB), "
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


def _safe_collection_token(value: object, default: str) -> str:
    """Return a bounded identifier without copying arbitrary diagnostic text."""
    text_value = str(value or "")[:64]
    safe = "".join(character for character in text_value if character.isalnum() or character in "._-")
    return safe or default


def _collection_issue_reason(errors: list[dict], total_series: int, all_failed: bool) -> str:
    issues_by_series = {}
    for error in errors:
        series_id = _safe_collection_token(error.get("series_id"), "unknown")
        issues_by_series[series_id] = error
    count = len(issues_by_series)
    noun = "issue" if count == 1 else "issues"
    details = []
    for series_id, error in list(issues_by_series.items())[:3]:
        stage = _safe_collection_token(error.get("stage"), "collection")
        code = _safe_collection_token(error.get("code"), "failed")
        details.append(f"{series_id} [{stage}/{code}]")
    summary = f"{count} collection {noun}"
    if details:
        summary += ": " + "; ".join(details)
    if all_failed:
        return f"All {total_series} series failed; {summary}"
    return summary


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
    collection_metrics: dict[str, int] = {}

    try:
        raw_result = collector.collect(config, correlation_id)

        # Normalise: accept both CollectionResult and plain list[dict]
        if isinstance(raw_result, CollectionResult):
            records = raw_result.records
            collection_errors = raw_result.errors
            collection_metrics = dict(raw_result.metrics)
            # Derive collection-level status from structured result
            if raw_result.all_failed:
                status = "failed"
                error_message = _collection_issue_reason(
                    raw_result.errors, raw_result.total_series, all_failed=True
                )
            elif raw_result.partial_failure:
                status = "partial"
                error_message = _collection_issue_reason(
                    raw_result.errors, raw_result.total_series, all_failed=False
                )
        else:
            records = raw_result
            # Backward compat: try collector.last_errors
            collection_errors = getattr(collector, "last_errors", [])

        records_fetched = len(records)
        exact_api_calls = collection_metrics.get("api_calls_made")
        if isinstance(exact_api_calls, int) and exact_api_calls >= 0:
            api_calls_made = exact_api_calls
        else:
            api_calls_made = _estimate_api_calls(source_id, records_fetched, config)

        if records:
            table_name = collector.get_target_table()
            conflict_columns = collector.get_conflict_columns()
            db_write_started = time.monotonic()
            try:
                write_result = upsert_records(
                    table_name=table_name,
                    records=records,
                    conflict_columns=conflict_columns,
                    config=config,
                )
            finally:
                collection_metrics["db_write_duration_ms"] = elapsed_ms(db_write_started)
                logger.info(
                    "collector_db_write_metrics",
                    action="run_collector",
                    collector=source_id,
                    correlation_id=correlation_id,
                    db_write_duration_ms=collection_metrics["db_write_duration_ms"],
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
        "metrics": collection_metrics,
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
            claim_worker_id = f"sync:{uuid4()}"
            if not start_run(config, correlation_id, claim_worker_id):
                return None
            worker_id = claim_worker_id
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
            error_message = str(exc) if worker_id is not None else "run start unavailable"
            result = (
                {}
                if worker_id is not None
                else {"status": "failed", "reason": error_message}
            )
            finalize_run_safely(
                correlation_id,
                "failed",
                result,
                config,
                error_message,
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
    budget_context: BudgetContext | None = None,
    mode: str = "refresh",
    now: datetime | None = None,
) -> dict | None:
    if mode not in VALID_CYCLE_MODES:
        raise ValueError("invalid cycle mode")
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
                            "reason",
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
        update_run_progress(correlation_id, progress, config, worker_id)

    update_run_progress(correlation_id, progress, config, worker_id)

    collector_layer_started = time.monotonic()
    cycle_now = now or datetime.now(timezone.utc)
    collectors_to_run = []
    historical_available = set()
    collector_results_by_id = {}
    for source_id in enabled_collectors:
        if mode == "force_full":
            collectors_to_run.append(source_id)
            continue
        if mode == "analyze":
            reason = "analyze_mode_no_collection"
        elif _collector_is_due(source_id, config, now=cycle_now):
            collectors_to_run.append(source_id)
            continue
        else:
            reason = "not_due"
        if mode != "analyze":
            try:
                if _last_successful_collection(source_id, config) is not None:
                    historical_available.add(source_id)
            except Exception:
                logger.warning(
                    "collector_history_lookup_failed",
                    action="select_cycle_collectors",
                    collector=source_id,
                    safe_code="collector_history_unavailable",
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

    worker_limit = collector_worker_limit(config, len(collectors_to_run))
    if collectors_to_run:
        with ThreadPoolExecutor(
            max_workers=worker_limit,
            thread_name_prefix="cycle-collector",
        ) as executor:
            future_sources = {}
            for source_id in collectors_to_run:
                record_progress(source_id, "collector", "running")
                future = executor.submit(
                    run_collector,
                    source_id,
                    config=config,
                    correlation_id=correlation_id,
                    manage_lifecycle=False,
                )
                future_sources[future] = source_id

            for future in as_completed(future_sources):
                source_id = future_sources[future]
                try:
                    result = future.result()
                except Exception:
                    logger.error(
                        "collector_future_failed",
                        action="run_full_cycle",
                        collector=source_id,
                        correlation_id=correlation_id,
                    )
                    result = {
                        "collector": source_id,
                        "status": "failed",
                        "error": "collector execution failed",
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
    logger.info(
        "collector_layer_finished",
        action="run_full_cycle",
        worker_limit=worker_limit,
        collector_count=len(collectors_to_run),
        enabled_collector_count=len(enabled_collectors),
        duration_ms=elapsed_ms(collector_layer_started),
        correlation_id=correlation_id,
    )

    processor_results = _resolve_and_run_processors(
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
        "no_change": bool(all_results) and all(
            result["status"] == "skipped" for result in all_results.values()
        ),
        "collectors": collector_results,
        "processors": processor_results,
        "correlation_id": correlation_id,
    }

    logger.info(
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
        finalize_run_safely(
            correlation_id, overall_status, cycle_result, config,
            worker_id=worker_id, run_kind="cycle",
        )
    return cycle_result


def run_full_cycle(
    config: dict | None = None,
    correlation_id: str | None = None,
    manage_lifecycle: bool = True,
    budget_context: BudgetContext | None = None,
    mode: str = "refresh",
) -> dict | None:
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
        if manage_lifecycle and lifecycle_created:
            error_message = str(exc) if worker_id is not None else "run start unavailable"
            result = (
                {}
                if worker_id is not None
                else {"status": "failed", "reason": error_message}
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


def _last_successful_collection(source_id: str, config: dict) -> datetime | None:
    with get_session(config) as session:
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
    source_id: str,
    config: dict,
    *,
    now: datetime | None = None,
) -> bool:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    try:
        completed_at = _last_successful_collection(source_id, config)
        if completed_at is None:
            return True
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=timezone.utc)
        completed_at = completed_at.astimezone(timezone.utc)
        schedule = config.get("collectors", {}).get(source_id, {}).get("schedule")
        if not isinstance(schedule, str) or not schedule.strip():
            raise ValueError("collector schedule missing")
        trigger = build_cron_trigger(schedule)
        next_fire = trigger.get_next_fire_time(completed_at, completed_at)
        return next_fire is None or next_fire <= current
    except Exception:
        logger.warning(
            "collector_due_check_failed_safe_due",
            action="select_cycle_collectors",
            collector=source_id,
            safe_code="collector_due_check_unavailable",
        )
        return True


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
    budget_context: BudgetContext | None = None,
    force: bool = False,
    analyze_existing_data: bool = False,
) -> dict:
    all_processors = get_all_processors()
    collector_dependencies = (
        set(get_all_collectors()) if analyze_existing_data else set()
    )
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
                dep in successful_collectors
                or dep in successful_processors
                or (analyze_existing_data and dep in collector_dependencies)
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
                processor_results[pid] = {
                    "processor": pid,
                    "status": "skipped",
                    "reason": f"Dependencies not met: {', '.join(depends_on)}",
                    "correlation_id": correlation_id,
                }
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
                budget_context=budget_context,
                force=force,
            )
            processor_results[pid] = result
            if progress_callback:
                progress_callback(pid, "processor", result["status"], result)
            if result["status"] in ("success", "partial") or (
                result["status"] == "skipped" and result.get("reusable_output", False)
            ):
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
    budget_context: BudgetContext | None = None,
    force: bool = False,
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
    failure_input_summary = None
    input_fingerprint = None
    reusable_output = None

    try:
        input_fingerprint = build_processor_fingerprint(processor, config)
    except Exception:
        logger.warning(
            "processor_fingerprint_build_failed",
            action="run_processor",
            processor=processor_id,
            correlation_id=correlation_id,
        )

    if input_fingerprint and not force:
        try:
            reusable_output = _find_reusable_processor_output(
                processor_id, input_fingerprint, config
            )
        except Exception:
            logger.warning(
                "processor_fingerprint_lookup_failed",
                action="run_processor",
                processor=processor_id,
                fingerprint_prefix=input_fingerprint[:12],
                correlation_id=correlation_id,
            )
        if reusable_output is not None:
            status = "skipped"

    try:
        if status != "skipped":
            result_payload = processor.process(
                config, correlation_id, budget_context=budget_context
            )
    except BudgetBlock as exc:
        status = (
            "budget_blocked" if isinstance(exc, BudgetExceeded) else "budget_unavailable"
        )
        error_message = exc.safe_reason
        telemetry = getattr(exc, "telemetry", None)
        failure_input_summary = {
            "blocked_code": exc.code,
            **(
                telemetry.as_dict()
                if telemetry is not None and hasattr(telemetry, "as_dict")
                else {}
            ),
        }
        logger.warning(
            "processor_budget_blocked",
            action="run_processor",
            processor=processor_id,
            blocked_code=exc.code,
            correlation_id=correlation_id,
        )
    except Exception as exc:
        status = "failed"
        error_message = str(exc)
        telemetry = getattr(exc, "telemetry", None)
        if telemetry is not None and hasattr(telemetry, "as_dict"):
            failure_input_summary = telemetry.as_dict()
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
    if status == "skipped":
        processing_log.update(
            {
                "status": "skipped",
                "tokens_input": 0,
                "tokens_output": 0,
                "cost_usd": 0.0,
            }
        )
    if failure_input_summary is not None:
        processing_log["input_summary"] = failure_input_summary
        processing_log["tokens_input"] = failure_input_summary.get("tokens_input_total", 0)
        processing_log["tokens_output"] = failure_input_summary.get("tokens_output_total", 0)
        processing_log["cost_usd"] = failure_input_summary.get("cost_usd_total", 0.0)

    # Raw LLM request/response content is never part of operational persistence.
    processing_log["prompt_text"] = None
    processing_log["raw_response"] = None

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
        input_fingerprint=input_fingerprint,
        skip_reason="unchanged_inputs" if status == "skipped" else None,
        forced=force,
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
        "skip_reason": "unchanged_inputs" if status == "skipped" else None,
        "reusable_output": status == "skipped" and reusable_output is not None,
        "forced": force,
    }

    log_result = dict(result)
    if input_fingerprint:
        log_result["input_fingerprint"] = input_fingerprint[:12]
    logger.info("processor_work_finished", action="run_processor", **log_result)
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
    budget_context: BudgetContext | None = None,
    force: bool = False,
) -> dict | None:
    config = _resolved_config(config)
    correlation_id = correlation_id or str(uuid4())
    lifecycle_created = False
    worker_id: str | None = None
    try:
        if manage_lifecycle:
            accept_run(config, correlation_id, "internal", "processor", processor_id)
            lifecycle_created = True
            claim_worker_id = f"sync:{uuid4()}"
            if not start_run(config, correlation_id, claim_worker_id):
                return None
            worker_id = claim_worker_id
        heartbeat = (
            maintain_run_heartbeat(config, correlation_id, worker_id)
            if worker_id is not None
            else nullcontext()
        )
        with heartbeat:
            with advisory_lock(f"processor:{processor_id}", config):
                result = _run_processor_impl(
                    processor_id,
                    config,
                    correlation_id,
                    manage_lifecycle=False,
                    budget_context=budget_context,
                    force=force,
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
                    completed_log = dict(result)
                    if completed_log.get("input_fingerprint"):
                        completed_log["input_fingerprint"] = completed_log["input_fingerprint"][:12]
                    logger.info("processor_completed", action="run_processor", **completed_log)
            return result
    except RunAcceptanceConflict:
        raise
    except Exception as exc:
        if manage_lifecycle and lifecycle_created:
            error_message = str(exc) if worker_id is not None else "run start unavailable"
            result = (
                {}
                if worker_id is not None
                else {"status": "failed", "reason": error_message}
            )
            finalize_run_safely(
                correlation_id,
                "failed",
                result,
                config,
                error_message,
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
    input_fingerprint: str | None,
    skip_reason: str | None,
    forced: bool,
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
        "prompt_text": None,
        "raw_response": None,
        "model_used": model_used,
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "cost_usd": cost_usd,
        "duration_ms": duration_ms,
        "error_message": error_message,
        "input_fingerprint": input_fingerprint,
        "skip_reason": skip_reason,
        "forced": forced,
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
