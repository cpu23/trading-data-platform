"""Collector and registered-news execution.

This module owns source-facing work while :mod:`run_lifecycle` owns durable run
ownership and :mod:`publication` owns durable output writes.  Dependencies that
historically lived on ``orchestrator`` are resolved at call time so existing
patch targets remain effective during the facade cutover.
"""

from __future__ import annotations

import sys
import time
import traceback
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from collectors import get_collector
from collectors.base import CollectionResult, elapsed_ms
from db import get_session, upsert_records
from errors import (
    InvalidSourceData,
    PersistenceError,
    TransientSourceError,
    classify_error,
)
from events.freshness import record_collection_freshness
from events.publisher import publish_collector_records_atomic
from locks import advisory_lock
from logging_config import get_logger
from publication import _write_collection_log
from run_lifecycle import (
    RunAcceptanceConflict,
    accept_run,
    finalize_run_safely,
    maintain_run_heartbeat,
    start_run,
)

DEFAULT_COLLECTOR_WORKERS = 3
MAX_COLLECTOR_WORKERS = 8

logger = get_logger("orchestrator.collector_execution")

# Keep local patchability when this module is tested directly, while preferring
# facade patch targets when it is called through the compatibility facade.
_LOCAL_DEPENDENCIES = {
    "get_collector": get_collector,
    "upsert_records": upsert_records,
    "publish_collector_records_atomic": publish_collector_records_atomic,
    "record_collection_freshness": record_collection_freshness,
    "get_session": get_session,
    "advisory_lock": advisory_lock,
    "accept_run": accept_run,
    "start_run": start_run,
    "maintain_run_heartbeat": maintain_run_heartbeat,
    "finalize_run_safely": finalize_run_safely,
    "_write_collection_log": _write_collection_log,
    "_estimate_api_calls": None,
    "_run_collector_impl": None,
    "logger": logger,
}


def _dependency(name: str) -> Any:
    """Resolve a dependency through patched facade globals when available."""
    local = globals().get(name)
    if local is not _LOCAL_DEPENDENCIES.get(name):
        return local
    facade = sys.modules.get("orchestrator")
    if facade is not None:
        candidate = getattr(facade, name, None)
        if candidate is not None:
            return candidate
    return local


def _resolved_config(config: dict | None) -> dict:
    if config is not None:
        return config
    from config_loader import load_config

    return load_config()


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


def _safe_collection_token(value: object, default: str) -> str:
    """Return a bounded identifier without copying arbitrary diagnostic text."""
    text_value = str(value or "")[:64]
    safe = "".join(
        character
        for character in text_value
        if character.isalnum() or character in "._-"
    )
    return safe or default


def _collection_issue_reason(
    errors: list[dict], total_series: int, all_failed: bool
) -> str:
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


def _estimate_api_calls(source_id: str, records_fetched: int, config: dict) -> int:
    """Estimate calls for legacy collectors without exact metrics."""
    del records_fetched
    if source_id == "fred":
        series_count = len(
            config.get("collectors", {}).get("fred", {}).get("series", [])
        )
        return series_count * 2
    if source_id == "oanda":
        oanda_config = config.get("collectors", {}).get("oanda", {})
        return 2 if oanda_config.get("instruments") else 1
    return 1


def _policy_metadata(exc: BaseException) -> dict[str, Any]:
    policy = classify_error(exc)
    return {
        "error_class": policy.error_class,
        "retryable": policy.retryable,
        "status": policy.status,
    }


def _structured_collection_policy(errors: list[dict]) -> dict[str, Any]:
    error_classes: set[str] = set()
    for issue in errors:
        if not isinstance(issue, dict):
            continue
        error_class = issue.get("error_class")
        if isinstance(error_class, str):
            error_classes.add(error_class)
            continue
        code = issue.get("code")
        if code in {"metadata_request_failed", "request_failed"}:
            error_classes.add(TransientSourceError.error_class)
        elif code == "cache_degraded":
            error_classes.add(PersistenceError.error_class)
        else:
            error_classes.add(InvalidSourceData.error_class)

    if InvalidSourceData.error_class in error_classes:
        error = InvalidSourceData("structured collection result contains invalid data")
    elif PersistenceError.error_class in error_classes:
        error = PersistenceError(
            "structured collection result contains persistence failures"
        )
    elif TransientSourceError.error_class in error_classes:
        error = TransientSourceError(
            "structured collection result contains source failures"
        )
    else:
        error = InvalidSourceData(
            "structured collection result contains unclassified errors"
        )
    return _policy_metadata(error)


def _event_pipeline_settings(config: dict) -> dict:
    settings = config.get("event_pipeline", {})
    return settings if isinstance(settings, dict) else {}


def _event_publication_enabled(source_id: str, config: dict) -> bool:
    settings = _event_pipeline_settings(config)
    sources = settings.get("sources", ())
    return (
        bool(settings.get("enabled", False))
        and isinstance(sources, (list, tuple, set, frozenset))
        and source_id in sources
    )


def _record_source_freshness(
    *,
    collector: Any,
    source_id: str,
    config: dict,
    started_at: datetime,
    completed_at: datetime,
    status: str,
    records_fetched: int,
    error_class: str | None,
) -> None:
    settings = _event_pipeline_settings(config)
    if not settings.get("enabled", False):
        return
    source_config = dict(config.get("collectors", {}).get(source_id, {}))
    source_config.setdefault(
        "freshness_grace_seconds",
        settings.get("freshness_grace_seconds", 300),
    )
    result_metadata = getattr(collector, "last_result_metadata", {})
    cache_mode = (
        result_metadata.get("payload_source")
        if isinstance(result_metadata, dict)
        else None
    )
    freshness_status = status
    if status not in {"success", "partial"}:
        freshness_status = "rate_limited" if status == "rate_limited" else "failed"
    try:
        with _dependency("get_session")(config) as session:
            _dependency("record_collection_freshness")(
                session,
                source=source_id,
                source_config=source_config,
                status=freshness_status,
                attempted_at=started_at,
                completed_at=completed_at,
                records_fetched=records_fetched,
                cache_mode=cache_mode,
                reason_code=error_class or status,
                detail={"collection_status": status},
            )
    except Exception as exc:
        _dependency("logger").warning(
            "source_freshness_update_failed",
            action="record_source_freshness",
            collector=source_id,
            error_type=type(exc).__name__,
        )


def _run_collector_impl(
    source_id: str,
    config: dict | None = None,
    correlation_id: str | None = None,
    manage_lifecycle: bool = True,
) -> dict | None:
    """Collect one registered collector and durably publish its records."""
    config = _resolved_config(config)
    correlation_id = correlation_id or str(uuid4())
    if manage_lifecycle:
        _dependency("accept_run")(
            config, correlation_id, "internal", "collector", source_id
        )
        if not _dependency("start_run")(config, correlation_id, f"sync:{uuid4()}"):
            return None

    try:
        import structlog.contextvars

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
    except ImportError:  # pragma: no cover - structlog is a runtime dependency
        pass

    collector = _dependency("get_collector")(source_id)
    started_at = datetime.now(UTC)
    start_ms = time.monotonic() * 1000
    _dependency("logger").info(
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
    error_class = None
    retryable = False
    collection_metrics: dict[str, int] = {}

    try:
        raw_result = collector.collect(config, correlation_id)
        if isinstance(raw_result, CollectionResult):
            records = raw_result.records
            collection_metrics = dict(raw_result.metrics)
            if raw_result.all_failed:
                error_message = _collection_issue_reason(
                    raw_result.errors, raw_result.total_series, all_failed=True
                )
                policy = _structured_collection_policy(raw_result.errors)
                status = policy["status"]
                error_class = policy["error_class"]
                retryable = policy["retryable"]
            elif raw_result.partial_failure:
                status = "partial"
                error_message = _collection_issue_reason(
                    raw_result.errors, raw_result.total_series, all_failed=False
                )
                policy = _structured_collection_policy(raw_result.errors)
                error_class = policy["error_class"]
                retryable = policy["retryable"]
        else:
            records = raw_result
        records_fetched = len(records)
        exact_api_calls = collection_metrics.get("api_calls_made")
        if isinstance(exact_api_calls, int) and exact_api_calls >= 0:
            api_calls_made = exact_api_calls
        else:
            estimator = _dependency("_estimate_api_calls")
            if estimator is None:
                estimator = _estimate_api_calls
            api_calls_made = estimator(source_id, records_fetched, config)

        if records:
            table_name = collector.get_target_table()
            conflict_columns = collector.get_conflict_columns()
            db_write_started = time.monotonic()
            try:
                if _event_publication_enabled(source_id, config):
                    write_result = _dependency("publish_collector_records_atomic")(
                        source_id=source_id,
                        table_name=table_name,
                        records=records,
                        conflict_columns=conflict_columns,
                        correlation_id=correlation_id,
                        config=config,
                    )
                    collection_metrics.update(
                        events_inserted=write_result.events_inserted,
                        events_deduplicated=write_result.events_deduplicated,
                        outbox_inserted=write_result.outbox_inserted,
                    )
                else:
                    write_result = _dependency("upsert_records")(
                        table_name=table_name,
                        records=records,
                        conflict_columns=conflict_columns,
                        config=config,
                    )
            except Exception as exc:
                raise PersistenceError(f"collector persistence failed: {exc}") from exc
            finally:
                collection_metrics["db_write_duration_ms"] = elapsed_ms(
                    db_write_started
                )
                _dependency("logger").info(
                    "collector_db_write_metrics",
                    action="run_collector",
                    collector=source_id,
                    correlation_id=correlation_id,
                    db_write_duration_ms=collection_metrics["db_write_duration_ms"],
                )
            records_written = write_result.written
            write_status = write_result.status
            if write_status == "failed":
                status = "failed"
                error_class = PersistenceError.error_class
                retryable = True
                error_message = error_message or (
                    f"All {write_result.attempted} DB writes failed for table {table_name}"
                )
            elif write_status == "partial":
                if status == "success":
                    status = "partial"
                error_class = PersistenceError.error_class
                retryable = True
                error_message = error_message or (
                    f"Partial DB write: {write_result.written}/{write_result.attempted} "
                    f"records written to {table_name}"
                )
    except Exception as exc:
        policy = _policy_metadata(exc)
        status = policy["status"]
        error_class = policy["error_class"]
        retryable = policy["retryable"]
        error_message = str(exc)
        error_traceback = traceback.format_exc()
        _dependency("logger").error(
            "collector_failed",
            action="run_collector",
            collector=source_id,
            error=str(exc),
            error_class=error_class,
            retryable=retryable,
            correlation_id=correlation_id,
        )

    completed_at = datetime.now(UTC)
    duration_ms = int(time.monotonic() * 1000 - start_ms)
    writer = _dependency("_write_collection_log")
    if writer is not None:
        writer(
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
    _record_source_freshness(
        collector=collector,
        source_id=source_id,
        config=config,
        started_at=started_at,
        completed_at=completed_at,
        status=status,
        records_fetched=records_fetched,
        error_class=error_class,
    )

    result = {
        "collector": source_id,
        "status": status,
        "records_fetched": records_fetched,
        "records_written": records_written,
        "duration_ms": duration_ms,
        "metrics": collection_metrics,
        "error": error_message,
        "error_class": error_class,
        "retryable": retryable,
        "correlation_id": correlation_id,
    }
    _dependency("logger").info(
        "collector_work_finished", action="run_collector", **result
    )
    if manage_lifecycle:
        _dependency("finalize_run_safely")(
            correlation_id,
            status,
            result,
            config,
            error_message,
            run_kind="collector",
            component=source_id,
        )
    return result


_LOCAL_DEPENDENCIES["_estimate_api_calls"] = _estimate_api_calls
_LOCAL_DEPENDENCIES["_run_collector_impl"] = _run_collector_impl


def run_collector(
    source_id: str,
    config: dict | None = None,
    correlation_id: str | None = None,
    manage_lifecycle: bool = True,
) -> dict | None:
    """Run a collector under its source lock and owned lifecycle row."""
    config = _resolved_config(config)
    correlation_id = correlation_id or str(uuid4())
    lifecycle_created = False
    worker_id: str | None = None
    try:
        if manage_lifecycle:
            _dependency("accept_run")(
                config, correlation_id, "internal", "collector", source_id
            )
            lifecycle_created = True
            claim_worker_id = f"sync:{uuid4()}"
            if not _dependency("start_run")(config, correlation_id, claim_worker_id):
                return None
            worker_id = claim_worker_id
        heartbeat = (
            _dependency("maintain_run_heartbeat")(config, correlation_id, worker_id)
            if worker_id is not None
            else nullcontext()
        )
        with heartbeat:
            with _dependency("advisory_lock")(f"collector:{source_id}", config):
                result = _dependency("_run_collector_impl")(
                    source_id, config, correlation_id, manage_lifecycle=False
                )
            if manage_lifecycle and result is not None:
                finalized = _dependency("finalize_run_safely")(
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
                    _dependency("logger").info(
                        "collector_completed", action="run_collector", **result
                    )
            return result
    except RunAcceptanceConflict:
        raise
    except Exception as exc:
        if manage_lifecycle and lifecycle_created:
            policy = classify_error(exc)
            error_message = (
                str(exc) if worker_id is not None else "run start unavailable"
            )
            result = (
                {}
                if worker_id is not None
                else {
                    "status": policy.status,
                    "reason": error_message,
                    "error_class": policy.error_class,
                    "retryable": policy.retryable,
                }
            )
            _dependency("finalize_run_safely")(
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


def run_news_source(
    source_id: str,
    correlation_id: str | None = None,
    config: dict | None = None,
    manage_lifecycle: bool = True,
) -> dict | None:
    """Collect and atomically publish one registered news source."""
    from sources.news_feed import collect_and_publish
    from sources.news_registry import get_news_collector

    config = _resolved_config(config)
    correlation_id = correlation_id or str(uuid4())
    collector = get_news_collector(source_id, config)
    lifecycle_created = False
    worker_id: str | None = None
    try:
        if manage_lifecycle:
            _dependency("accept_run")(
                config, correlation_id, "internal", "news", source_id
            )
            lifecycle_created = True
            claim_worker_id = f"sync:{uuid4()}"
            if not _dependency("start_run")(config, correlation_id, claim_worker_id):
                return None
            worker_id = claim_worker_id
        heartbeat = (
            _dependency("maintain_run_heartbeat")(config, correlation_id, worker_id)
            if worker_id is not None
            else nullcontext()
        )
        with heartbeat:
            with _dependency("advisory_lock")(f"news:{source_id}", config):
                started = time.monotonic()
                error_class = None
                retryable = False
                try:
                    outcome = collect_and_publish(source_id, config, collector)
                    if outcome.succeeded:
                        status, state, error, code = "success", "published", None, None
                    else:
                        publication_failed = bool(
                            outcome.error_class == PersistenceError.error_class
                            or outcome.error
                            and (
                                outcome.error.startswith(
                                    "News feed publication failed:"
                                )
                                or outcome.error.startswith(
                                    "News state persistence failed:"
                                )
                            )
                        )
                        state = (
                            "publication_failed"
                            if publication_failed
                            else "collection_failed"
                        )
                        code = (
                            "news_publication_failed"
                            if publication_failed
                            else "news_collection_failed"
                        )
                        error = outcome.error or "news collection failed"
                        if publication_failed:
                            failure = PersistenceError(error)
                        elif outcome.error_class == InvalidSourceData.error_class:
                            failure = InvalidSourceData(error)
                        else:
                            failure = TransientSourceError(error)
                        policy = _policy_metadata(failure)
                        status = policy["status"]
                        error_class = policy["error_class"]
                        retryable = policy["retryable"]
                    item_count = len(outcome.items)
                    feed_published = (
                        outcome.feed_published if not outcome.succeeded else True
                    )
                except Exception as exc:
                    policy = _policy_metadata(exc)
                    status, state, item_count = policy["status"], "collection_failed", 0
                    code = "news_collection_failed"
                    error = f"news collection failed: {type(exc).__name__}"
                    feed_published = False
                    error_class = policy["error_class"]
                    retryable = policy["retryable"]
                result = {
                    "status": status,
                    "new_item_count": item_count,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "state": state,
                    "error": error,
                    "code": code,
                    "feed_published": feed_published,
                    "error_class": error_class,
                    "retryable": retryable,
                    "correlation_id": correlation_id,
                }
            if manage_lifecycle:
                finalized = _dependency("finalize_run_safely")(
                    correlation_id,
                    status,
                    result,
                    config,
                    error,
                    worker_id=worker_id,
                    run_kind="news",
                    component=source_id,
                )
                if finalized:
                    _dependency("logger").info(
                        "news_source_completed",
                        action="run_news_source",
                        source_id=source_id,
                        **result,
                    )
            return result
    except RunAcceptanceConflict:
        raise
    except Exception as exc:
        if manage_lifecycle and lifecycle_created:
            policy = classify_error(exc)
            reason = "run start unavailable" if worker_id is None else "news run failed"
            _dependency("finalize_run_safely")(
                correlation_id,
                "failed",
                {
                    "status": policy.status,
                    "state": "failed",
                    "error": reason,
                    "code": "news_run_failed",
                    "feed_published": False,
                    "new_item_count": 0,
                    "duration_ms": 0,
                    "error_class": policy.error_class,
                    "retryable": policy.retryable,
                    "correlation_id": correlation_id,
                },
                config,
                reason,
                worker_id=worker_id,
                run_kind="news",
                component=source_id,
            )
        raise


def get_last_collection_runs(config: dict | None = None) -> list[dict]:
    """Return the newest durable collection row for each collector."""
    config = _resolved_config(config)
    from sqlalchemy import text

    sql = text(
        "SELECT DISTINCT ON (collector) * FROM collection_log "
        "ORDER BY collector, started_at DESC"
    )
    try:
        with _dependency("get_session")(config) as session:
            result = session.execute(sql)
            return [dict(row._mapping) for row in result]
    except Exception as exc:
        _dependency("logger").error(
            "last_runs_query_failed", action="get_last_runs", error=str(exc)
        )
        return []


__all__ = [
    "run_collector",
    "run_news_source",
    "_run_collector_impl",
    "collector_worker_limit",
    "_safe_collection_token",
    "_collection_issue_reason",
    "_estimate_api_calls",
    "get_last_collection_runs",
]
