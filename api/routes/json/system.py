from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

import config as app_config
from budgets import get_budget_status
from contracts import (
    OrchestratorHealthResponse,
    RunDetailResponse,
    RunListResponse,
    RunStatusResponse,
    SystemHealthResponse,
    SystemTopologyResponse,
)
from db import query_many, query_one
from logging_config import get_logger
from staleness import get_staleness_config, is_stale

router = APIRouter()

logger = get_logger("system.health")


def _safe_config_version() -> str | None:
    try:
        return app_config.config_version()
    except Exception:
        return None


def _dependency_unready_response(
    *,
    component: str,
    reason: str,
    data_health: str = "unknown",
) -> JSONResponse:
    """Return a contract-valid, redacted readiness failure."""
    payload = SystemHealthResponse.model_validate(
        {
            "liveness": "ok",
            "readiness": "unready",
            "data_health": data_health,
            "overall": "degraded",
            "components": [
                {
                    "name": component,
                    "kind": "service",
                    "last_status": "error",
                    "stale": True,
                    "quality_warn": False,
                    "error_message": reason,
                }
            ],
            "today_llm_cost_usd": 0.0,
            "today_token_count": 0,
            "quality": {"overall": "unknown", "checks": []},
            "config_version": _safe_config_version(),
        }
    )
    return JSONResponse(status_code=503, content=payload.model_dump(mode="json"))


def _load_local_health_data():
    config = app_config.load_config()
    thresholds = get_staleness_config(config)
    collector_rows = query_many(
        """SELECT DISTINCT ON (collector)
               collector, started_at, status, duration_ms, error_message
           FROM collection_log
           ORDER BY collector, started_at DESC""",
        config=config,
    )
    processor_rows = query_many(
        """SELECT DISTINCT ON (processor)
               processor, started_at, status, model_used, cost_usd, duration_ms
           FROM processing_log
           ORDER BY processor, started_at DESC""",
        config=config,
    )
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    cost_rows = query_many(
        "SELECT COALESCE(SUM(cost_usd), 0) as total_cost, "
        "COALESCE(SUM(tokens_input + tokens_output), 0) as total_tokens "
        "FROM processing_log WHERE started_at >= :today_start",
        params={"today_start": today_start},
        config=config,
    )
    return config, thresholds, collector_rows, processor_rows, cost_rows


def _normalized_lines(raw: str) -> int:
    if not raw.isascii() or not raw.isdecimal():
        raise HTTPException(status_code=422, detail="lines must be a positive integer")
    value = int(raw)
    if value < 1:
        raise HTTPException(status_code=422, detail="lines must be at least 1")
    return min(value, 1000)


def _fmt(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _public_failure(error: object, *, status: object, kind: str) -> str | None:
    """Expose failure state without returning stored provider/model diagnostics."""
    if error is None:
        return None
    normalized = str(status or "error").strip().lower()
    if normalized not in {
        "abandoned",
        "degraded",
        "error",
        "failed",
        "partial",
        "retrying",
        "skipped",
        "stale",
        "unavailable",
        "unhealthy",
    }:
        normalized = "error"
    return f"{kind} reported {normalized}; private diagnostics omitted"


def _public_quality_snapshot(snapshot: object) -> dict:
    if hasattr(snapshot, "model_dump"):
        raw = snapshot.model_dump(mode="json")
    elif isinstance(snapshot, Mapping):
        raw = dict(snapshot)
    else:
        return {"overall": "unknown", "checks": []}

    def public_check(value: object) -> dict:
        check = dict(value) if isinstance(value, Mapping) else {}
        result = {
            "healthy": check.get("healthy") is True,
            "status": check.get("status"),
            "freshness": check.get("freshness"),
            "detail": None,
        }
        for key in ("name", "source_id"):
            candidate = check.get(key)
            if isinstance(candidate, str) and candidate:
                result[key] = candidate[:200]
        return result

    checks = raw.get("checks", [])
    if isinstance(checks, Mapping):
        public_checks: dict | list = {
            str(key)[:200]: public_check(value)
            for key, value in list(checks.items())[:100]
        }
    elif isinstance(checks, list):
        public_checks = [public_check(value) for value in checks[:100]]
    else:
        public_checks = []
    return {"overall": raw.get("overall", "unknown"), "checks": public_checks}


def _public_progress(summary: object) -> dict:
    if not isinstance(summary, Mapping):
        return {}
    progress = summary.get("progress")
    if not isinstance(progress, Mapping):
        return {}
    public: dict[str, object] = {}
    for key in ("total_stages", "completed_stages"):
        value = progress.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            public[key] = max(0, min(value, 10_000))
    current = progress.get("current_stage")
    if (
        isinstance(current, str)
        and 1 <= len(current) <= 100
        and all(character.isalnum() or character in "_-" for character in current)
    ):
        public["current_stage"] = current
    return public


@router.get("/system/health", response_model=SystemHealthResponse)
async def get_system_health(request: Request):
    try:
        (
            config,
            thresholds,
            collector_rows,
            processor_rows,
            cost_rows,
        ) = await run_in_threadpool(_load_local_health_data)
    except Exception as exc:
        logger.warning(
            "system_health_database_unavailable",
            error_type=type(exc).__name__,
        )
        return _dependency_unready_response(
            component="database",
            reason="database dependency unavailable",
            data_health="degraded",
        )

    today_cost = float(cost_rows[0].get("total_cost") or 0) if cost_rows else 0.0
    today_tokens = int(cost_rows[0].get("total_tokens") or 0) if cost_rows else 0

    try:
        health_response = await request.app.state.orchestrator_client.get(
            f"{app_config.orchestrator_url()}/health",
            timeout=5.0,
        )
        health_response.raise_for_status()
        health_model = OrchestratorHealthResponse.model_validate(health_response.json())
        if health_model.readiness != "ready":
            raise ValueError("orchestrator is not ready")
        if health_model.quality is None:
            raise ValueError("orchestrator quality snapshot is missing")
    except ValidationError:
        logger.warning("orchestrator_health_contract_invalid")
        return _dependency_unready_response(
            component="orchestrator",
            reason="invalid orchestrator health contract",
        )
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.warning(
            "orchestrator_health_unavailable",
            error_type=type(exc).__name__,
        )
        return _dependency_unready_response(
            component="orchestrator",
            reason="orchestrator dependency unavailable",
        )

    health = health_model.model_dump(mode="python")
    quality = health_model.quality.model_dump(mode="python")
    schedule = health.get("scheduler") or {}
    schedule_map = {
        job["id"].split(":", 1)[-1]: job.get("next_due_at")
        for job in schedule.get("jobs", [])
    }
    stream_info = health.get("stream") or {}

    components = []
    status_map = {
        "available": "running",
        "degraded": "degraded",
        "unavailable": "error",
    }
    for component in health.get("components", []):
        status = status_map[component["status"]]
        components.append(
            {
                "name": component["name"],
                "kind": component.get("kind") or "service",
                "last_run_at": None,
                "last_status": status,
                "next_due_at": None,
                "stale": status != "running",
                "quality_warn": component.get("kind") == "data" and status != "running",
                "error_message": _public_failure(
                    component.get("reason"),
                    status=status,
                    kind="orchestrator component",
                ),
            }
        )

    quality_warn_map: dict[str, bool] = {}
    raw_checks = quality["checks"]
    if isinstance(raw_checks, dict):
        checks_iter = raw_checks.items()
    else:
        checks_iter = (
            (str(check.get("name") or f"check_{index}"), check)
            for index, check in enumerate(raw_checks)
        )

    unhealthy_checks = []
    for check_id, check_data in checks_iter:
        status = str(check_data.get("status", check_data.get("freshness", ""))).lower()
        unhealthy = not check_data["healthy"] or status in {
            "unhealthy",
            "stale",
            "future-invalid",
            "future_invalid",
        }
        if unhealthy:
            unhealthy_checks.append((check_id, check_data))
            source_id = check_data.get("source_id")
            if source_id:
                quality_warn_map[source_id] = True

    if quality["overall"] == "healthy" and not raw_checks:
        quality["overall"] = "unknown"

    oanda = config.get("collectors", {}).get("oanda", {})
    stream_required = bool(config.get("demo", {}).get("enabled", False)) or bool(
        oanda.get("enabled", False) and oanda.get("stream_enabled", False)
    )
    stream_status = stream_info.get(
        "status", "stopped" if stream_required else "disabled"
    )
    stream_stale = stream_required and stream_status not in {
        "connected",
        "simulated",
    }
    components.append(
        {
            "name": "live_prices",
            "kind": "stream",
            "last_run_at": stream_info.get("last_heartbeat"),
            "last_status": stream_status,
            "next_due_at": None,
            "stale": stream_stale,
            "quality_warn": quality_warn_map.get("live_prices", False),
            "error_message": None,
        }
    )

    quality_degraded = quality["overall"] in {"degraded", "unhealthy"} or bool(
        unhealthy_checks
    )
    if quality_degraded:
        if unhealthy_checks:
            check_ids = ", ".join(
                str(check_id)[:200] for check_id, _ in unhealthy_checks
            )
            quality_reason = (
                f"{len(unhealthy_checks)} orchestrator quality check"
                f"{'s' if len(unhealthy_checks) != 1 else ''} reported unhealthy: "
                f"{check_ids}"
            )
        else:
            quality_reason = f"orchestrator quality overall is {quality['overall']}"
        components.append(
            {
                "name": "quality_checks",
                "kind": "data",
                "last_run_at": None,
                "last_status": "degraded",
                "next_due_at": None,
                "stale": False,
                "quality_warn": True,
                "error_message": quality_reason,
            }
        )

    collector_map = {row["collector"]: row for row in collector_rows}
    for source_id, collector_config in config.get("collectors", {}).items():
        if not collector_config.get("enabled", True):
            continue
        row = collector_map.get(source_id)
        if row is None:
            components.append(
                {
                    "name": source_id,
                    "kind": "collector",
                    "last_run_at": None,
                    "last_status": "never_run",
                    "next_due_at": schedule_map.get(source_id),
                    "stale": True,
                    "quality_warn": quality_warn_map.get(source_id, False),
                }
            )
            continue
        threshold_hours = thresholds.get("macro_hours", 30)
        if source_id == "forex_factory":
            threshold_hours = thresholds.get("events_hours", 8)
        stale, _ = is_stale(row["started_at"], threshold_hours)
        components.append(
            {
                "name": source_id,
                "kind": "collector",
                "last_run_at": _fmt(row["started_at"]),
                "last_status": row.get("status", "unknown"),
                "next_due_at": schedule_map.get(source_id),
                "stale": stale,
                "quality_warn": quality_warn_map.get(source_id, False),
                "error_message": _public_failure(
                    row.get("error_message"),
                    status=row.get("status"),
                    kind="collector",
                ),
            }
        )

    processor_map = {row["processor"]: row for row in processor_rows}
    for processor_id, processor_config in config.get("processors", {}).items():
        if not processor_config.get("enabled", False):
            continue
        row = processor_map.get(processor_id)
        if row is None:
            components.append(
                {
                    "name": processor_id,
                    "kind": "processor",
                    "last_run_at": None,
                    "last_status": "never_run",
                    "next_due_at": schedule_map.get(processor_id),
                    "stale": True,
                    "quality_warn": quality_warn_map.get(processor_id, False),
                }
            )
            continue
        threshold_hours = thresholds.get("regime_hours", 18)
        if processor_id == "briefing":
            threshold_hours = thresholds.get("briefing_hours", 18)
        stale, _ = is_stale(row["started_at"], threshold_hours)
        components.append(
            {
                "name": processor_id,
                "kind": "processor",
                "last_run_at": _fmt(row["started_at"]),
                "last_status": row.get("status", "unknown"),
                "next_due_at": schedule_map.get(processor_id),
                "stale": stale,
                "quality_warn": quality_warn_map.get(processor_id, False),
                "error_message": _public_failure(
                    row.get("error_message"),
                    status=row.get("status"),
                    kind="processor",
                ),
            }
        )

    any_stale = any(component["stale"] for component in components)
    any_error = any(
        component["last_status"]
        in {"failed", "partial", "error", "never_run", "unknown", "degraded"}
        for component in components
    )
    known_data_failure = (
        any_stale
        or any_error
        or any(component["quality_warn"] for component in components)
        or health["data_health"] == "degraded"
    )
    if known_data_failure:
        data_health = "degraded"
    elif quality["overall"] == "unknown" or health["data_health"] == "unknown":
        data_health = "unknown"
    else:
        data_health = "healthy"

    all_ok = all(
        not component["stale"]
        and component["last_status"]
        in {
            "success",
            "healthy",
            "connected",
            "simulated",
            "running",
            "disabled",
        }
        for component in components
    )
    result = {
        "liveness": "ok",
        "readiness": "unready" if stream_stale else "ready",
        "data_health": data_health,
        "overall": "healthy" if all_ok and data_health == "healthy" else "degraded",
        "components": components,
        "today_llm_cost_usd": round(today_cost, 4),
        "today_token_count": today_tokens,
        "quality": _public_quality_snapshot(quality),
        "config_version": health.get("config_version"),
    }
    response_model = SystemHealthResponse.model_validate(result)
    if response_model.readiness == "unready":
        return JSONResponse(
            status_code=503,
            content=response_model.model_dump(mode="json"),
        )
    return response_model.model_dump(mode="json")


@router.get("/system/budget")
def get_budget():
    return get_budget_status()


@router.get("/system/topology", response_model=SystemTopologyResponse)
async def get_system_topology():
    """Return a bounded live topology; partial aggregates remain visible."""
    from topology import build_system_topology, unavailable_system_topology

    try:
        return await run_in_threadpool(build_system_topology)
    except Exception:
        return unavailable_system_topology()


@router.get("/system/logs")
def get_system_logs(
    component: str = Query(
        default="", pattern=r"^[A-Za-z0-9_.-]{0,100}$", max_length=100
    ),
    status: str = Query(default="", pattern=r"^[A-Za-z0-9_-]{0,50}$", max_length=50),
    limit: int = Query(default=50, ge=1, le=500),
    from_date: datetime | None = Query(default=None, alias="from"),
    correlation_id: str = Query(
        default="", pattern=r"^[A-Za-z0-9_.:-]{0,200}$", max_length=200
    ),
):
    parsed_correlation_id = None
    if correlation_id:
        try:
            parsed_correlation_id = UUID(correlation_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="correlation_id must be a UUID"
            ) from exc

    config = app_config.load_config()

    params: dict = {"limit": limit}

    collector_sql = """
        SELECT log_id, correlation_id, 'collection' as log_type,
               collector as component, started_at, completed_at, status,
               records_fetched, records_written, duration_ms, error_message,
               NULL as model_used, NULL as tokens_input, NULL as tokens_output,
               NULL as cost_usd
        FROM collection_log
    """
    processor_sql = """
        SELECT log_id, correlation_id, 'processing' as log_type,
               processor as component, started_at, completed_at, status,
               NULL as records_fetched, NULL as records_written,
               duration_ms, error_message, model_used, tokens_input,
               tokens_output, cost_usd
        FROM processing_log
    """

    where_clauses_collector = []
    where_clauses_processor = []

    if component:
        where_clauses_collector.append("collector LIKE :comp_filter ESCAPE '\\'")
        where_clauses_processor.append("processor LIKE :comp_filter ESCAPE '\\'")
        escaped_component = component.replace("_", "\\_")
        params["comp_filter"] = f"%{escaped_component}%"

    if status:
        where_clauses_collector.append("status = :status_filter")
        where_clauses_processor.append("status = :status_filter")
        params["status_filter"] = status

    if from_date is not None and (
        from_date.tzinfo is None or from_date.utcoffset() is None
    ):
        raise HTTPException(
            status_code=422, detail="from must be a timezone-aware datetime"
        )
    if from_date:
        where_clauses_collector.append("started_at >= :from_date")
        where_clauses_processor.append("started_at >= :from_date")
        params["from_date"] = from_date

    if parsed_correlation_id is not None:
        where_clauses_collector.append("correlation_id = :correlation_id")
        where_clauses_processor.append("correlation_id = :correlation_id")
        params["correlation_id"] = parsed_correlation_id

    collector_where = (
        (" WHERE " + " AND ".join(where_clauses_collector))
        if where_clauses_collector
        else ""
    )
    processor_where = (
        (" WHERE " + " AND ".join(where_clauses_processor))
        if where_clauses_processor
        else ""
    )

    combined_sql = f"""
        SELECT * FROM (
            {collector_sql}{collector_where}
            UNION ALL
            {processor_sql}{processor_where}
        ) AS all_logs
        ORDER BY started_at DESC
        LIMIT :limit
    """

    rows = query_many(combined_sql, params=params, config=config)

    logs = []
    for row in rows:
        entry = {
            "log_id": str(row["log_id"]),
            "correlation_id": str(row["correlation_id"])
            if row.get("correlation_id")
            else None,
            "log_type": row["log_type"],
            "component": row["component"],
            "started_at": _fmt(row.get("started_at")),
            "completed_at": _fmt(row.get("completed_at")),
            "status": row["status"],
            "duration_ms": row.get("duration_ms"),
            "error_message": _public_failure(
                row.get("error_message"),
                status=row.get("status"),
                kind=str(row.get("log_type") or "operation"),
            ),
        }
        if row["log_type"] == "collection":
            entry["records_fetched"] = row.get("records_fetched")
            entry["records_written"] = row.get("records_written")
        elif row["log_type"] == "processing":
            entry["model_used"] = row.get("model_used")
            entry["tokens_input"] = row.get("tokens_input")
            entry["tokens_output"] = row.get("tokens_output")
            entry["cost_usd"] = (
                float(row["cost_usd"]) if row.get("cost_usd") is not None else None
            )
        logs.append(entry)

    return {"logs": logs, "limit": limit}


@router.get("/logs")
def get_bounded_logs(lines: str = Query(default="200")):
    normalized = _normalized_lines(lines)
    rows = query_many(
        """SELECT correlation_id, component, log_type, started_at, completed_at,
                  status, duration_ms, error_message
           FROM (
               SELECT correlation_id, collector AS component, 'collection' AS log_type,
                      started_at, completed_at, status, duration_ms, error_message
               FROM collection_log
               UNION ALL
               SELECT correlation_id, processor AS component, 'processing' AS log_type,
                      started_at, completed_at, status, duration_ms, error_message
               FROM processing_log
           ) AS bounded_logs
           ORDER BY started_at DESC
           LIMIT :limit""",
        params={"limit": normalized},
        config=app_config.load_config(),
    )
    logs = [
        {
            "correlation_id": (
                str(row["correlation_id"]) if row.get("correlation_id") else None
            ),
            "component": row.get("component"),
            "log_type": row.get("log_type"),
            "started_at": _fmt(row.get("started_at")),
            "completed_at": _fmt(row.get("completed_at")),
            "status": row.get("status"),
            "duration_ms": row.get("duration_ms"),
            "error_message": _public_failure(
                row.get("error_message"),
                status=row.get("status"),
                kind=str(row.get("log_type") or "operation"),
            ),
        }
        for row in rows
    ]
    return {"logs": logs, "lines": normalized}


def _run_payload(row: dict) -> dict:
    summary = row.get("summary") or {}
    if isinstance(summary, str):
        import json

        try:
            summary = json.loads(summary)
        except (TypeError, ValueError):
            summary = {}
    return {
        "correlation_id": str(row["correlation_id"]),
        "status": row["status"],
        "result_status": row.get("result_status"),
        "run_kind": row.get("run_kind", "cycle"),
        "requested_component": row.get("requested_component"),
        "triggered_by": row.get("triggered_by"),
        "started_at": _fmt(row.get("started_at")),
        "completed_at": _fmt(row.get("completed_at")),
        "error_message": _public_failure(
            row.get("error_message"),
            status=row.get("result_status") or row.get("status"),
            kind="run",
        ),
        "summary": {"progress": _public_progress(summary)},
    }


@router.get("/system/runs", response_model=RunListResponse)
def get_system_runs(limit: int = Query(default=20, ge=1, le=100)):
    rows = query_many(
        """SELECT correlation_id, status, result_status, run_kind,
                  requested_component, triggered_by, started_at, completed_at,
                  error_message, summary
           FROM cycle_runs ORDER BY started_at DESC LIMIT :limit""",
        {"limit": limit},
        config=app_config.load_config(),
    )
    return {"runs": [_run_payload(row) for row in rows], "limit": limit}


@router.get("/system/runs/{correlation_id}", response_model=RunDetailResponse)
def get_system_run(correlation_id: UUID):
    config = app_config.load_config()
    row = query_one(
        """SELECT correlation_id, status, result_status, run_kind,
                  requested_component, triggered_by, started_at, completed_at,
                  error_message, summary
           FROM cycle_runs WHERE correlation_id = :cid""",
        {"cid": str(correlation_id)},
        config=config,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Run not found")

    stages = query_many(
        """
        SELECT log_id, correlation_id, 'collector' AS kind, collector AS component,
               started_at, completed_at, status, duration_ms, records_fetched,
               records_written, NULL::INTEGER AS tokens_input,
               NULL::INTEGER AS tokens_output, NULL::DOUBLE PRECISION AS cost_usd
        FROM collection_log WHERE correlation_id = :cid
        UNION ALL
        SELECT log_id, correlation_id, 'processor' AS kind, processor AS component,
               started_at, completed_at, status, duration_ms, NULL, NULL,
               tokens_input, tokens_output, cost_usd
        FROM processing_log WHERE correlation_id = :cid
        ORDER BY started_at
        """,
        {"cid": str(correlation_id)},
        config=config,
    )
    payload = _run_payload(row)
    payload["stages"] = [
        {
            **stage,
            "log_id": str(stage["log_id"]),
            "correlation_id": str(stage["correlation_id"]),
            "started_at": _fmt(stage.get("started_at")),
            "completed_at": _fmt(stage.get("completed_at")),
            "cost_usd": float(stage["cost_usd"])
            if stage.get("cost_usd") is not None
            else None,
        }
        for stage in stages
    ]
    return payload


@router.get("/system/cycle-status", response_model=RunStatusResponse)
def get_cycle_status(correlation_id: UUID = Query(...)):
    config = app_config.load_config()

    row = query_one(
        "SELECT status, result_status, started_at, completed_at, error_message, summary "
        "FROM cycle_runs WHERE correlation_id = :cid",
        {"cid": str(correlation_id)},
        config=config,
    )

    if not row:
        return {"status": "unknown", "correlation_id": str(correlation_id)}

    run = get_system_run(correlation_id)
    return {
        "status": row.get("result_status") or row["status"],
        "lifecycle_status": row["status"],
        "result_status": row.get("result_status"),
        "correlation_id": str(correlation_id),
        "started_at": _fmt(row.get("started_at")),
        "completed_at": _fmt(row.get("completed_at")),
        "error_message": _public_failure(
            row.get("error_message"),
            status=row.get("result_status") or row.get("status"),
            kind="run",
        ),
        "progress": run.get("summary", {}).get("progress", {}),
        "stages": run.get("stages", []),
    }


def _snapshot_payload(row: dict | None) -> dict | None:
    if row is None:
        return None
    payload = dict(row)
    payload["id"] = str(payload["id"])
    payload["source_event_ids"] = [
        str(value) for value in (payload.get("source_event_ids") or [])
    ]
    for key in (
        "data_freshness_at",
        "analysis_freshness_at",
        "created_at",
        "published_at",
    ):
        payload[key] = _fmt(payload.get(key))
    return payload


@router.get("/sections/{section_key}")
def get_section_snapshot(
    section_key: str = Path(pattern=r"^[a-z][a-z0-9_]{0,63}$"),
):
    """Return the current published snapshot and bounded publication history."""
    config = app_config.load_config()
    fields = """
        id, section_key, scope_key, version, status, payload, render_context,
        content_hash, data_freshness_at, analysis_freshness_at, source_event_ids,
        created_at, published_at
    """
    current = query_one(
        f"""SELECT {fields}
            FROM section_snapshots
            WHERE section_key = :section_key
              AND scope_key = 'global'
              AND status = 'published'
            ORDER BY version DESC
            LIMIT 1""",
        {"section_key": section_key},
        config=config,
    )
    history = query_many(
        f"""SELECT {fields}
            FROM section_snapshots
            WHERE section_key = :section_key
              AND scope_key = 'global'
            ORDER BY version DESC
            LIMIT 20""",
        {"section_key": section_key},
        config=config,
    )
    return {
        "section_key": section_key,
        "current": _snapshot_payload(current),
        "history": [_snapshot_payload(row) for row in history],
    }


@router.get("/analysis/jobs/status")
def get_analysis_jobs_status():
    """Return bounded aggregate queue state without job payloads or errors."""
    config = app_config.load_config()
    rows = query_many(
        """SELECT state, COUNT(*) AS count, MIN(created_at) AS oldest_created_at
           FROM analysis_jobs
           GROUP BY state
           ORDER BY state""",
        config=config,
    )
    counts = {str(row["state"]): int(row["count"]) for row in rows}
    active_states = {"queued", "leased", "running", "failed_retryable"}
    active_rows = [row for row in rows if row["state"] in active_states]
    oldest_pending_at = min(
        (
            row["oldest_created_at"]
            for row in active_rows
            if row.get("oldest_created_at") is not None
        ),
        default=None,
    )
    return {
        "counts": counts,
        "active": sum(counts.get(state, 0) for state in active_states),
        "oldest_pending_at": _fmt(oldest_pending_at),
    }
