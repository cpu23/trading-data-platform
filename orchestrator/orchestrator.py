import json
import time
import traceback
from datetime import datetime, timezone
from uuid import uuid4

from collectors import get_all_collectors, get_collector
from db import (
    advisory_lock,
    get_session,
    insert_records,
    insert_records_in_session,
    upsert_records,
    upsert_records_in_session,
)
from logging_config import get_logger
from processors import get_all_processors, get_processor
from processors._validators import OutputPolicyError
from sqlalchemy import text

logger = get_logger("orchestrator")

RUNTIME_LOCK_NAME = "trading-data-platform:runtime"
DEPENDENCY_READY_STATES = {"success", "partial", "degraded_cache"}
COMPLETE_STAGE_STATES = {"success"}
FAILED_STAGE_STATES = {"failed", "validation_failed"}


def ensure_run(
    correlation_id: str,
    config: dict,
    run_kind: str = "cycle",
    requested_component: str | None = None,
    triggered_by: str = "internal",
) -> None:
    with get_session(config) as session:
        session.execute(
            text(
                "INSERT INTO cycle_runs "
                "(correlation_id, status, started_at, triggered_by, run_kind, requested_component) "
                "VALUES (:cid, 'running', :started_at, :triggered_by, :run_kind, :component) "
                "ON CONFLICT (correlation_id) DO NOTHING"
            ),
            {
                "cid": correlation_id,
                "started_at": datetime.now(timezone.utc),
                "triggered_by": triggered_by,
                "run_kind": run_kind,
                "component": requested_component,
            },
        )


def finish_run(
    correlation_id: str,
    result_status: str,
    summary: dict,
    config: dict,
    error_message: str | None = None,
) -> None:
    lifecycle_status = (
        "failed" if result_status in FAILED_STAGE_STATES else "completed"
    )
    publish_complete_snapshot = result_status == "success"
    publication_status = (
        "published"
        if publish_complete_snapshot
        else "failed"
    )
    completed_at = datetime.now(timezone.utc)
    with get_session(config) as session:
        session.execute(
            text(
                "UPDATE cycle_runs SET status = :status, result_status = :result_status, "
                "summary = CAST(:summary AS JSONB), completed_at = :completed_at, "
                "error_message = :error_message, "
                "publication_status = :publication_status, "
                "published_at = CASE WHEN :publication_status = 'published' "
                "THEN :completed_at ELSE published_at END "
                "WHERE correlation_id = :cid"
            ),
            {
                "cid": correlation_id,
                "status": lifecycle_status,
                "result_status": result_status,
                "summary": json.dumps(summary),
                "completed_at": completed_at,
                "error_message": error_message,
                "publication_status": publication_status,
            },
        )
        if publish_complete_snapshot:
            session.execute(
                text(
                    "UPDATE structured_opinions SET lifecycle_status = 'published', "
                    "published_at = :published_at "
                    "WHERE correlation_id = :cid AND lifecycle_status = 'validated'"
                ),
                {"cid": correlation_id, "published_at": completed_at},
            )
            session.execute(
                text(
                    "UPDATE daily_briefings SET lifecycle_status = 'published', "
                    "published_at = :published_at "
                    "WHERE correlation_id = :cid AND lifecycle_status = 'validated'"
                ),
                {"cid": correlation_id, "published_at": completed_at},
            )


def _aggregate_stage_status(results: dict[str, dict]) -> str:
    if not results:
        return "failed"
    statuses = [result.get("status", "failed") for result in results.values()]
    if all(status in COMPLETE_STAGE_STATES for status in statuses):
        return "success"
    if all(status in FAILED_STAGE_STATES | {"skipped", "budget_denied"} for status in statuses):
        if "validation_failed" in statuses:
            return "validation_failed"
        if "budget_denied" in statuses and not any(
            status in FAILED_STAGE_STATES for status in statuses
        ):
            return "budget_denied"
        return "failed"
    return "partial"


def _lock_skipped_result(
    component_key: str,
    correlation_id: str,
) -> dict:
    return {
        "component": component_key,
        "status": "skipped",
        "duration_ms": 0,
        "error": "Another cycle or component run is already active",
        "correlation_id": correlation_id,
    }


def _runtime_lock_context(config: dict, acquire: bool):
    if not acquire or not config.get("database"):
        return __import__("contextlib").nullcontext(True)
    return advisory_lock(RUNTIME_LOCK_NAME, config)


def update_run_progress(correlation_id: str, progress: dict, config: dict) -> None:
    try:
        with get_session(config) as session:
            session.execute(
                text(
                    "UPDATE cycle_runs SET summary = "
                    "COALESCE(summary, '{}'::jsonb) || CAST(:summary AS JSONB) "
                    "WHERE correlation_id = :cid AND status = 'running'"
                ),
                {
                    "cid": correlation_id,
                    "summary": json.dumps({"progress": progress}),
                },
            )
    except Exception as exc:
        logger.error(
            "cycle_progress_write_failed",
            action="update_run_progress",
            correlation_id=correlation_id,
            error=str(exc),
        )


def run_collector(
    source_id: str,
    config: dict | None = None,
    correlation_id: str | None = None,
    *,
    acquire_runtime_lock: bool = True,
) -> dict:
    if config is None:
        from config_loader import load_config

        config = load_config()

    standalone = correlation_id is None
    if standalone:
        correlation_id = str(uuid4())
    ensure_run(
        correlation_id,
        config,
        run_kind="collector",
        requested_component=source_id,
    )

    lock_context = _runtime_lock_context(config, acquire_runtime_lock)
    with lock_context as acquired:
        if not acquired:
            result = {
                **_lock_skipped_result(f"collector:{source_id}", correlation_id),
                "collector": source_id,
                "records_fetched": 0,
                "records_written": 0,
            }
            if standalone:
                finish_run(
                    correlation_id, "skipped", result, config, result["error"]
                )
            return result

        import structlog.contextvars

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)

        started_at = datetime.now(timezone.utc)
        start_ms = time.monotonic() * 1000
        records_fetched = 0
        records_written = 0
        api_calls_made = 0
        status = "success"
        error_message = None
        error_traceback = None

        logger.info(
            "collector_started",
            action="run_collector",
            collector=source_id,
            correlation_id=correlation_id,
        )

        try:
            collector = get_collector(source_id)
            records = collector.collect(config, correlation_id)
            raw_metadata = getattr(collector, "last_result_metadata", {})
            result_metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
            records_fetched = len(records)

            if records:
                records_written = upsert_records(
                    table_name=collector.get_target_table(),
                    records=records,
                    conflict_columns=collector.get_conflict_columns(),
                    config=config,
                )
                if records_written != records_fetched:
                    raise RuntimeError(
                        f"Incomplete persistence: wrote {records_written} "
                        f"of {records_fetched} records"
                    )

            api_calls_made = _estimate_api_calls(
                source_id, records_fetched, config
            )
        except Exception as exc:
            raw_metadata = getattr(
                locals().get("collector"), "last_result_metadata", {}
            )
            result_metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
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
            result_metadata=result_metadata,
        )

        result = {
            "collector": source_id,
            "status": status,
            "records_fetched": records_fetched,
            "records_written": records_written,
            "duration_ms": duration_ms,
            "error": error_message,
            "correlation_id": correlation_id,
            "metadata": result_metadata,
        }
        logger.info("collector_completed", action="run_collector", **result)
        if standalone:
            finish_run(correlation_id, status, result, config, error_message)
        return result


def run_full_cycle(
    config: dict | None = None, correlation_id: str | None = None
) -> dict:
    if config is None:
        from config_loader import load_config

        config = load_config()

    if correlation_id is None:
        correlation_id = str(uuid4())
    ensure_run(correlation_id, config, run_kind="cycle")
    with _runtime_lock_context(config, True) as acquired:
        if not acquired:
            result = {
                **_lock_skipped_result("cycle", correlation_id),
                "collectors": {},
                "processors": {},
            }
            finish_run(
                correlation_id, "skipped", result, config, result["error"]
            )
            return result
        try:
            result = _run_full_cycle_unlocked(config, correlation_id)
        except Exception as exc:
            result = {
                "status": "failed",
                "collectors": {},
                "processors": {},
                "correlation_id": correlation_id,
                "error": str(exc),
            }
            finish_run(correlation_id, "failed", result, config, str(exc))
            logger.exception(
                "full_cycle_failed",
                action="run_full_cycle",
                correlation_id=correlation_id,
            )
            return result

        finish_run(
            correlation_id,
            result["status"],
            result,
            config,
            result.get("error"),
        )
        return result


def _run_full_cycle_unlocked(config: dict, correlation_id: str) -> dict:
    budget_override = _consume_budget_override(correlation_id, config)

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
        update_run_progress(correlation_id, progress, config)

    update_run_progress(correlation_id, progress, config)

    for source_id in enabled_collectors:
        record_progress(source_id, "collector", "running")
        result = run_collector(
            source_id,
            config=config,
            correlation_id=correlation_id,
            acquire_runtime_lock=False,
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
        budget_override=budget_override,
    )

    all_results = {**collector_results, **processor_results}
    overall_status = _aggregate_stage_status(all_results)

    cycle_result = {
        "status": overall_status,
        "collectors": collector_results,
        "processors": processor_results,
        "correlation_id": correlation_id,
        "budget_override": budget_override,
    }

    logger.info(
        "full_cycle_completed",
        action="run_full_cycle",
        overall_status=overall_status,
        collectors_run=list(collector_results.keys()),
        processors_run=list(processor_results.keys()),
        correlation_id=correlation_id,
    )

    return cycle_result


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
    budget_override: dict | None = None,
    processor_ids: set[str] | None = None,
) -> dict:
    all_processors = get_all_processors()
    processor_results = {}
    successful_processors = set()
    remaining = {}

    for processor_id, processor in all_processors.items():
        processor_config = config.get("processors", {}).get(processor_id, {})
        if not processor_config.get("enabled", False):
            continue
        if processor_ids is not None and processor_id not in processor_ids:
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
                processor_results[pid] = {
                    "processor": pid,
                    "status": "skipped",
                    "duration_ms": 0,
                    "error": f"Dependencies not met: {', '.join(depends_on)}",
                    "correlation_id": correlation_id,
                    "opinion_id": None,
                    "opinion_ids": [],
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
                budget_override=budget_override,
                acquire_runtime_lock=False,
            )
            processor_results[pid] = result
            if progress_callback:
                progress_callback(pid, "processor", result["status"], result)
            if result["status"] in ("success", "partial"):
                successful_processors.add(pid)

    return processor_results


def get_transitive_dependents(
    upstream_component: str,
    config: dict,
) -> set[str]:
    """Return enabled processors reachable from one collector/processor."""
    processors = get_all_processors()
    enabled = {
        processor_id: processor
        for processor_id, processor in processors.items()
        if config.get("processors", {})
        .get(processor_id, {})
        .get("enabled", False)
    }
    reachable = {upstream_component}
    dependents: set[str] = set()
    changed = True
    while changed:
        changed = False
        for processor_id, processor in enabled.items():
            if processor_id in dependents:
                continue
            if any(dep in reachable for dep in processor.get_depends_on()):
                dependents.add(processor_id)
                reachable.add(processor_id)
                changed = True
    return dependents


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
    result_metadata: dict | None = None,
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
    if result_metadata:
        config_snapshot["acquisition"] = result_metadata

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


def run_processor(
    processor_id: str,
    config: dict | None = None,
    correlation_id: str | None = None,
    budget_override: dict | None = None,
    *,
    acquire_runtime_lock: bool = True,
) -> dict:
    if config is None:
        from config_loader import load_config

        config = load_config()

    standalone = correlation_id is None
    if standalone:
        correlation_id = str(uuid4())
    ensure_run(
        correlation_id,
        config,
        run_kind="processor",
        requested_component=processor_id,
    )
    lock_context = _runtime_lock_context(config, acquire_runtime_lock)
    with lock_context as acquired:
        if not acquired:
            result = {
                **_lock_skipped_result(
                    f"processor:{processor_id}", correlation_id
                ),
                "processor": processor_id,
                "opinion_id": None,
                "opinion_ids": [],
            }
        else:
            try:
                result = _run_processor_unlocked(
                    processor_id=processor_id,
                    config=config,
                    correlation_id=correlation_id,
                    budget_override=budget_override,
                    publish_immediately=standalone,
                )
            except Exception as exc:
                logger.exception(
                    "processor_runtime_failed",
                    action="run_processor",
                    processor=processor_id,
                    correlation_id=correlation_id,
                )
                result = {
                    "processor": processor_id,
                    "status": "failed",
                    "duration_ms": 0,
                    "error": str(exc),
                    "correlation_id": correlation_id,
                    "opinion_id": None,
                    "opinion_ids": [],
                }
        if standalone:
            finish_run(
                correlation_id,
                result["status"],
                result,
                config,
                result.get("error"),
            )
        return result


def _run_processor_unlocked(
    processor_id: str,
    config: dict,
    correlation_id: str,
    budget_override: dict | None,
    publish_immediately: bool,
) -> dict:

    import structlog.contextvars

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)

    if budget_override is None:
        budget_override = _consume_budget_override(correlation_id, config)
    budget = _get_budget_status(config)

    if not budget["paid_calls_allowed"] and budget_override is None:
        started_at = datetime.now(timezone.utc)
        error_message = (
            "Daily LLM budget reached "
            f"({budget['today_cost_usd']:.6f} / "
            f"{budget['budget_cap_usd']:.6f} USD)"
        )
        _write_processing_log(
            processor_id=processor_id,
            started_at=started_at,
            completed_at=started_at,
            status="budget_denied",
            input_summary={"budget": budget, "budget_override": None},
            output_id=None,
            prompt_text=None,
            raw_response=None,
            model_used=None,
            tokens_input=None,
            tokens_output=None,
            cost_usd=None,
            duration_ms=0,
            error_message=error_message,
            config=config,
            correlation_id=correlation_id,
        )
        result = {
            "processor": processor_id,
            "status": "budget_denied",
            "duration_ms": 0,
            "error": error_message,
            "correlation_id": correlation_id,
            "opinion_id": None,
            "budget": budget,
            "budget_override": None,
        }
        logger.warning(
            "processor_budget_denied",
            action="run_processor",
            processor=processor_id,
            correlation_id=correlation_id,
            today_cost_usd=budget["today_cost_usd"],
            budget_cap_usd=budget["budget_cap_usd"],
        )
        return result

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
    except OutputPolicyError as exc:
        status = "validation_failed"
        error_message = str(exc)
        logger.warning(
            "processor_validation_failed",
            action="run_processor",
            processor=processor_id,
            error=str(exc),
            correlation_id=correlation_id,
        )
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

    processing_log = None
    if status == "success" and result_payload:
        processing_log = result_payload.get("processing_log")
    processing_log = processing_log or {}
    input_summary = processing_log.get("input_summary") or {}
    if not isinstance(input_summary, dict):
        input_summary = {"processor_input_summary": input_summary}
    input_summary["budget"] = budget
    input_summary["budget_override"] = budget_override
    processing_log["input_summary"] = input_summary

    processing_log.setdefault("processor", processor_id)
    processing_log.setdefault("status", status)
    processing_log.setdefault("started_at", started_at)
    processing_log.setdefault("completed_at", completed_at)
    processing_log.setdefault("duration_ms", duration_ms)

    if error_message:
        processing_log["error_message"] = error_message

    opinions = []
    if result_payload:
        opinions = result_payload.get("opinions") or []
        if result_payload.get("opinion"):
            opinions = [result_payload["opinion"], *opinions]
    for opinion in opinions:
        opinion.setdefault("correlation_id", correlation_id)
        opinion.setdefault("schema_version", "1")
        opinion.setdefault("payload", {})
        opinion.setdefault(
            "lifecycle_status",
            "published" if publish_immediately else "validated",
        )
        if opinion["lifecycle_status"] == "published":
            opinion.setdefault("published_at", completed_at)
        inputs = opinion.get("data_inputs") or {}
        if isinstance(inputs, dict):
            inputs.pop("opinion_id", None)
            inputs.pop("output_id", None)
            opinion["data_inputs"] = inputs

    extra_records = result_payload.get("extra_records", {}) if result_payload else {}
    for briefing in extra_records.get("daily_briefings", []):
        briefing.setdefault("correlation_id", correlation_id)
        briefing.setdefault(
            "lifecycle_status",
            "published" if publish_immediately else "validated",
        )
        if briefing["lifecycle_status"] == "published":
            briefing.setdefault("published_at", completed_at)

    output_ids = [opinion["opinion_id"] for opinion in opinions]
    processing_log["output_ids"] = output_ids
    processing_log.setdefault("output_id", output_ids[0] if output_ids else None)

    if status == "success" and result_payload:
        try:
            _persist_processor_result(
                opinions=opinions,
                extra_records=extra_records,
                processing_log=processing_log,
                processor_id=processor_id,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
                correlation_id=correlation_id,
                config=config,
            )
        except Exception as exc:
            status = "failed"
            error_message = f"Atomic DB write failed: {exc}"
            logger.error(
                "processor_db_write_failed",
                action="run_processor",
                processor=processor_id,
                error=str(exc),
                correlation_id=correlation_id,
            )
            _write_processing_log(
                processor_id=processor_id,
                started_at=started_at,
                completed_at=completed_at,
                status=status,
                input_summary=processing_log.get("input_summary"),
                output_id=None,
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
    else:
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
        "opinion_id": output_ids[0] if status == "success" and output_ids else None,
        "opinion_ids": output_ids if status == "success" else [],
        "budget": budget,
        "budget_override": budget_override,
    }

    logger.info("processor_completed", action="run_processor", **result)
    return result


def _get_budget_status(config: dict) -> dict:
    budget_cfg = config.get(
        "budgets",
        {"daily_llm_usd": 2.00, "warn_at_pct": 80},
    )
    daily_cap = float(budget_cfg.get("daily_llm_usd", 2.00))
    warn_pct = int(budget_cfg.get("warn_at_pct", 80))
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    with get_session(config) as session:
        row = session.execute(
            text(
                "SELECT COALESCE(SUM(cost_usd), 0) AS total_cost, "
                "COALESCE(SUM(tokens_input + tokens_output), 0) AS total_tokens "
                "FROM processing_log WHERE started_at >= :today_start"
            ),
            {"today_start": today_start},
        ).fetchone()

    total_cost = float(row._mapping.get("total_cost", 0) or 0) if row else 0.0
    total_tokens = int(row._mapping.get("total_tokens", 0) or 0) if row else 0
    unlimited = daily_cap <= 0
    usage_pct = 0.0 if unlimited else round((total_cost / daily_cap) * 100, 2)
    exceeded = False if unlimited else total_cost >= daily_cap
    warning = False if unlimited or exceeded else usage_pct >= warn_pct

    return {
        "today_cost_usd": round(total_cost, 6),
        "today_tokens": total_tokens,
        "budget_cap_usd": daily_cap,
        "unlimited": unlimited,
        "warn_at_pct": warn_pct,
        "usage_pct": usage_pct,
        "warning": warning,
        "exceeded": exceeded,
        "hard_limit_reached": exceeded,
        "paid_calls_allowed": unlimited or not exceeded,
        "remaining_usd": (
            None
            if unlimited
            else round(max(daily_cap - total_cost, 0.0), 6)
        ),
    }


def _consume_budget_override(
    correlation_id: str,
    config: dict,
) -> dict | None:
    """Atomically consume an API-recorded override for this run."""
    with get_session(config) as session:
        row = session.execute(
            text(
                "SELECT summary FROM cycle_runs "
                "WHERE correlation_id = :cid FOR UPDATE"
            ),
            {"cid": correlation_id},
        ).fetchone()
        if not row:
            return None

        summary = row._mapping.get("summary") or {}
        if isinstance(summary, str):
            summary = json.loads(summary)
        override = summary.get("budget_override")
        if not isinstance(override, dict) or not override.get("requested"):
            return None
        if override.get("consumed_at"):
            if override.get("consumed_by") == correlation_id:
                return override
            return None

        override = {
            **override,
            "consumed_at": datetime.now(timezone.utc).isoformat(),
            "consumed_by": correlation_id,
        }
        summary["budget_override"] = override
        session.execute(
            text(
                "UPDATE cycle_runs SET summary = CAST(:summary AS JSONB) "
                "WHERE correlation_id = :cid"
            ),
            {"cid": correlation_id, "summary": json.dumps(summary)},
        )

    logger.warning(
        "budget_override_consumed",
        correlation_id=correlation_id,
        reason=override.get("reason"),
        requested_by=override.get("requested_by"),
    )
    return override


def _persist_processor_result(
    opinions: list[dict],
    extra_records: dict[str, list[dict]],
    processing_log: dict,
    processor_id: str,
    started_at: datetime,
    completed_at: datetime,
    duration_ms: int,
    correlation_id: str,
    config: dict,
) -> None:
    """Persist one processor result as a single transaction."""
    with get_session(config) as session:
        insert_records_in_session(session, "structured_opinions", opinions)
        for table_name, records in extra_records.items():
            if table_name == "daily_briefings":
                upsert_records_in_session(
                    session, table_name, records, ["briefing_date", "correlation_id"]
                )
            else:
                insert_records_in_session(session, table_name, records)

        log_record = {
            "started_at": started_at,
            "completed_at": completed_at,
            "processor": processor_id,
            "status": "success",
            "input_summary": processing_log.get("input_summary"),
            "output_id": processing_log.get("output_id"),
            "output_ids": processing_log.get("output_ids", []),
            "prompt_text": processing_log.get("prompt_text"),
            "raw_response": processing_log.get("raw_response"),
            "model_used": processing_log.get("model_used"),
            "tokens_input": processing_log.get("tokens_input"),
            "tokens_output": processing_log.get("tokens_output"),
            "cost_usd": processing_log.get("cost_usd"),
            "duration_ms": duration_ms,
            "error_message": None,
            "request_metadata": processing_log.get("request_metadata"),
            "correlation_id": correlation_id,
        }
        insert_records_in_session(session, "processing_log", [log_record])

    logger.info(
        "processor_records_written",
        action="run_processor",
        processor=processor_id,
        opinion_count=len(opinions),
        extra_tables=list(extra_records),
        correlation_id=correlation_id,
    )


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
