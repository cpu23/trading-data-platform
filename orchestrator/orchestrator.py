import json
import time
import traceback
from datetime import datetime, timezone
from uuid import uuid4

from collectors import get_all_collectors, get_collector
from db import get_session, insert_records, upsert_records
from logging_config import get_logger
from processors import get_all_processors, get_processor
from sqlalchemy import text

logger = get_logger("orchestrator")


def run_collector(
    source_id: str, config: dict | None = None, correlation_id: str | None = None
) -> dict:
    if config is None:
        from config_loader import load_config

        config = load_config()

    if correlation_id is None:
        correlation_id = str(uuid4())

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

    try:
        records = collector.collect(config, correlation_id)
        records_fetched = len(records)

        if records:
            table_name = collector.get_target_table()
            conflict_columns = collector.get_conflict_columns()
            records_written = upsert_records(
                table_name=table_name,
                records=records,
                conflict_columns=conflict_columns,
                config=config,
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
        "collector_completed",
        action="run_collector",
        **result,
    )

    return result


def run_full_cycle(
    config: dict | None = None, correlation_id: str | None = None
) -> dict:
    if config is None:
        from config_loader import load_config

        config = load_config()

    if correlation_id is None:
        correlation_id = str(uuid4())

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

    for source_id in enabled_collectors:
        result = run_collector(source_id, config=config, correlation_id=correlation_id)
        collector_results[source_id] = result
        if result["status"] in ("success", "partial"):
            successful_collectors.add(source_id)

    processor_results = _resolve_and_run_processors(
        config=config,
        correlation_id=correlation_id,
        successful_collectors=successful_collectors,
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
            break

        for pid in this_pass:
            del remaining[pid]

        for pid in this_pass:
            result = run_processor(pid, config=config, correlation_id=correlation_id)
            processor_results[pid] = result
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
    processor_id: str, config: dict | None = None, correlation_id: str | None = None
) -> dict:
    if config is None:
        from config_loader import load_config

        config = load_config()

    if correlation_id is None:
        correlation_id = str(uuid4())

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

    logger.info("processor_completed", action="run_processor", **result)
    return result


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
