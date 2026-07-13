from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from config import load_config
from db import query_many, query_one
from budgets import get_budget_status
from staleness import get_staleness_config, is_stale
from logging_config import get_logger

router = APIRouter()

logger = get_logger("system.health")


def _fmt(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _validate_orchestrator_health_contract(payload):
    if not isinstance(payload, dict):
        raise ValueError("invalid orchestrator health contract: payload must be an object")
    if payload.get("liveness") != "ok":
        raise ValueError("invalid orchestrator health contract: liveness must be 'ok'")
    if payload.get("readiness") not in {"ready", "degraded", "unready"}:
        raise ValueError("invalid orchestrator health contract: invalid readiness")
    if payload.get("data_health") not in {"healthy", "degraded"}:
        raise ValueError("invalid orchestrator health contract: invalid data_health")

    components = payload.get("components")
    if not isinstance(components, list):
        raise ValueError("invalid orchestrator health contract: components must be a list")
    for index, component in enumerate(components):
        if not isinstance(component, dict):
            raise ValueError(
                f"invalid orchestrator health contract: component {index} must be an object"
            )
        if not isinstance(component.get("name"), str) or not component["name"]:
            raise ValueError(
                f"invalid orchestrator health contract: component {index} has invalid name"
            )
        if not isinstance(component.get("status"), str) or not component["status"]:
            raise ValueError(
                f"invalid orchestrator health contract: component {index} has invalid status"
            )
        if "kind" in component and not isinstance(component["kind"], str):
            raise ValueError(
                f"invalid orchestrator health contract: component {index} has invalid kind"
            )
        if "critical" in component and not isinstance(component["critical"], bool):
            raise ValueError(
                f"invalid orchestrator health contract: component {index} has invalid critical flag"
            )
        if "reason" in component and component["reason"] is not None and not isinstance(
            component["reason"], str
        ):
            raise ValueError(
                f"invalid orchestrator health contract: component {index} has invalid reason"
            )


def _validate_orchestrator_quality_contract(payload):
    if not isinstance(payload, dict):
        raise ValueError("invalid orchestrator quality contract: payload must be an object")
    if payload.get("overall") not in {"healthy", "degraded", "unhealthy"}:
        raise ValueError("invalid orchestrator quality contract: invalid overall status")

    checks = payload.get("checks")
    if not isinstance(checks, (dict, list)):
        raise ValueError("invalid orchestrator quality contract: checks must be an object or list")
    entries = checks.items() if isinstance(checks, dict) else enumerate(checks)
    for check_id, check in entries:
        if isinstance(checks, dict) and not isinstance(check_id, str):
            raise ValueError("invalid orchestrator quality contract: check names must be strings")
        if not isinstance(check, dict):
            raise ValueError(
                f"invalid orchestrator quality contract: check {check_id} must be an object"
            )
        if "healthy" in check and not isinstance(check["healthy"], bool):
            raise ValueError(
                f"invalid orchestrator quality contract: check {check_id} has invalid healthy flag"
            )
        for field in ("name", "status", "freshness", "detail", "source_id"):
            if field in check and not isinstance(check[field], str):
                raise ValueError(
                    f"invalid orchestrator quality contract: check {check_id} has invalid {field}"
                )


@router.get("/system/health")
def get_system_health():
    config = load_config()
    thresholds = get_staleness_config(config)

    # ── DB queries (wrapped — failure → readiness "unready", HTTP 503) ──
    try:
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

        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        cost_rows = query_many(
            "SELECT COALESCE(SUM(cost_usd), 0) as total_cost, "
            "COALESCE(SUM(tokens_input + tokens_output), 0) as total_tokens "
            "FROM processing_log WHERE started_at >= :today_start",
            params={"today_start": today_start},
            config=config,
        )
    except Exception as db_exc:
        logger.error("db_unavailable", error=str(db_exc))
        return JSONResponse(
            status_code=503,
            content={
                "liveness": "ok",
                "readiness": "unready",
                "data_health": "degraded",
                "components": [],
                "today_llm_cost_usd": 0.0,
                "today_token_count": 0,
                "quality": {},
                "error": "Database unavailable",
            },
        )

    today_cost = 0.0
    today_tokens = 0
    if cost_rows:
        today_cost = float(cost_rows[0].get("total_cost", 0) or 0)
        today_tokens = int(cost_rows[0].get("total_tokens", 0) or 0)

    components = []
    schedule_map = {}
    stream_info = {}

    # ── Initialize quality_warn_map BEFORE using it ──
    quality = {}
    quality_warn_map = {}

    # ── Fetch and validate orchestrator contracts ──
    try:
        health_response = httpx.get("http://orchestrator:8000/health", timeout=2.0)
        health_response.raise_for_status()
        orchestration = health_response.json()
        _validate_orchestrator_health_contract(orchestration)
        if orchestration["readiness"] == "unready":
            raise ValueError(
                f"orchestrator is not ready ({orchestration['readiness']})"
            )

        quality_response = httpx.get("http://orchestrator:8000/quality", timeout=5.0)
        quality_response.raise_for_status()
        quality = quality_response.json()
        _validate_orchestrator_quality_contract(quality)
    except Exception as exc:
        reason = str(exc) or type(exc).__name__
        logger.warning("orchestrator_contract_unavailable", error=reason)
        return JSONResponse(
            status_code=503,
            content={
                "liveness": "ok",
                "readiness": "unready",
                "data_health": "degraded",
                "overall": "degraded",
                "components": [
                    {
                        "name": "orchestrator", "kind": "service",
                        "last_run_at": None, "last_status": "error",
                        "next_due_at": None, "stale": True,
                        "quality_warn": False, "error_message": reason,
                    },
                    {
                        "name": "live_prices", "kind": "stream",
                        "last_run_at": None, "last_status": "unknown",
                        "next_due_at": None, "stale": True,
                        "quality_warn": False,
                        "error_message": "orchestrator unavailable",
                    },
                ],
                "today_llm_cost_usd": round(today_cost, 4),
                "today_token_count": today_tokens,
                "quality": {},
            },
        )

    schedule_map = {
        job["id"].split(":", 1)[-1]: job.get("next_due_at")
        for job in orchestration.get("scheduler", {}).get("jobs", [])
    }
    stream_info = orchestration.get("stream", {})

    raw_checks = quality["checks"]
    checks_iter = raw_checks.items() if isinstance(raw_checks, dict) else (
        (check.get("name", f"check_{index}"), check)
        for index, check in enumerate(raw_checks)
    )
    unhealthy_checks = []
    invalid_states = {"unhealthy", "stale", "future-invalid", "future_invalid"}
    for check_id, check_data in checks_iter:
        status = str(check_data.get("status", check_data.get("freshness", ""))).lower()
        unhealthy = not check_data.get("healthy", True) or status in invalid_states
        if unhealthy:
            unhealthy_checks.append((check_id, check_data))
            source_id = check_data.get("source_id", "")
            if source_id:
                quality_warn_map[source_id] = True

    quality_degraded = quality.get("overall") != "healthy" or bool(unhealthy_checks)

    # ── Add live_prices stream component AFTER quality_warn_map is populated ──
    stream_stale = True
    if stream_info:
        stream_stale = stream_info.get("status") not in ("connected", "simulated")
    components.append({
        "name": "live_prices",
        "kind": "stream",
        "last_run_at": stream_info.get("last_heartbeat") if stream_info else None,
        "last_status": stream_info.get("status", "stopped") if stream_info else "unknown",
        "next_due_at": None,
        "stale": stream_stale,
        "quality_warn": quality_warn_map.get("live_prices", False),
        "error_message": None,
    })

    if quality_degraded:
        reasons = [
            f"{check_id}: {check.get('detail', check.get('status', 'unhealthy'))}"
            for check_id, check in unhealthy_checks
        ]
        if not reasons:
            reasons = [f"orchestrator quality overall is {quality.get('overall')}"]
        components.append({
            "name": "quality_checks",
            "kind": "data",
            "last_run_at": None,
            "last_status": "degraded",
            "next_due_at": None,
            "stale": False,
            "quality_warn": True,
            "error_message": "; ".join(reasons),
        })

    # ── Build collector/processor components ──
    enabled_collectors = config.get("collectors", {})
    collector_map = {r["collector"]: r for r in collector_rows}

    for source_id, coll_config in enabled_collectors.items():
        if not coll_config.get("enabled", True):
            continue
        row = collector_map.get(source_id)
        if row:
            threshold_hours = thresholds.get("macro_hours", 30)
            if source_id == "forex_factory":
                threshold_hours = thresholds.get("events_hours", 8)
            stale, _ = is_stale(row["started_at"], threshold_hours)
            components.append({
                "name": source_id,
                "kind": "collector",
                "last_run_at": _fmt(row["started_at"]),
                "last_status": row.get("status", "unknown"),
                "next_due_at": schedule_map.get(source_id),
                "stale": stale,
                "quality_warn": quality_warn_map.get(source_id, False),
                "error_message": row.get("error_message") if row.get("status") in ("failed", "partial") else None,
            })
        else:
            components.append({
                "name": source_id,
                "kind": "collector",
                "last_run_at": None,
                "last_status": "never_run",
                "next_due_at": schedule_map.get(source_id),
                "stale": True,
                "quality_warn": quality_warn_map.get(source_id, False),
            })

    enabled_processors = config.get("processors", {})
    processor_map = {r["processor"]: r for r in processor_rows}

    for proc_id, proc_config in enabled_processors.items():
        if not proc_config.get("enabled", False):
            continue
        row = processor_map.get(proc_id)
        if row:
            threshold_hours = thresholds.get("regime_hours", 18)
            if proc_id == "briefing":
                threshold_hours = thresholds.get("briefing_hours", 18)
            stale, _ = is_stale(row["started_at"], threshold_hours)
            components.append({
                "name": proc_id,
                "kind": "processor",
                "last_run_at": _fmt(row["started_at"]),
                "last_status": row.get("status", "unknown"),
                "next_due_at": schedule_map.get(proc_id),
                "stale": stale,
                "quality_warn": quality_warn_map.get(proc_id, False),
            })
        else:
            components.append({
                "name": proc_id,
                "kind": "processor",
                "last_run_at": None,
                "last_status": "never_run",
                "next_due_at": schedule_map.get(proc_id),
                "stale": True,
                "quality_warn": quality_warn_map.get(proc_id, False),
            })

    # ── Compute separate liveness/readiness/data_health ──
    liveness = "ok"

    any_stale = any(c.get("stale") for c in components)
    any_error = any(
        c.get("last_status") in ("failed", "partial", "error", "never_run", "unknown")
        for c in components
    )
    any_quality_warn = any(c.get("quality_warn") for c in components)
    has_components = len(components) > 0

    if not has_components:
        readiness = "degraded"
    elif any_stale or any_error:
        readiness = "degraded"
    else:
        readiness = "ready"

    if any_quality_warn or any_stale or not has_components:
        data_health = "degraded"
    else:
        data_health = "healthy"

    # Keep backward-compatible overall for any consumers
    all_ok = all(
        not c.get("stale")
        and c["last_status"] in ("success", "connected", "simulated")
        for c in components
    )
    overall = "healthy" if all_ok else "degraded"

    return {
        "liveness": liveness,
        "readiness": readiness,
        "data_health": data_health,
        "overall": overall,
        "components": components,
        "today_llm_cost_usd": round(today_cost, 4),
        "today_token_count": today_tokens,
        "quality": quality,
    }


@router.get("/system/budget")
def get_budget():
    return get_budget_status()


@router.get("/system/logs")
def get_system_logs(
    component: str = Query(default=""),
    status: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
    include_detail: bool = Query(default=False),
    from_date: datetime | None = Query(default=None, alias="from"),
    correlation_id: str = Query(default=""),
):
    config = load_config()

    params: dict = {"limit": limit}

    collector_sql = """
        SELECT log_id, correlation_id, 'collection' as log_type, collector as component, started_at,
               completed_at, status, records_fetched, records_written,
               duration_ms, error_message, error_traceback,
               NULL as model_used, NULL as tokens_input, NULL as tokens_output,
               NULL as cost_usd, NULL as prompt_text, NULL as raw_response
        FROM collection_log
    """
    processor_sql = """
        SELECT log_id, correlation_id, 'processing' as log_type, processor as component, started_at,
               completed_at, status, NULL as records_fetched, NULL as records_written,
               duration_ms, error_message, NULL as error_traceback,
               model_used, tokens_input, tokens_output, cost_usd,
               prompt_text, raw_response
        FROM processing_log
    """

    where_clauses_collector = []
    where_clauses_processor = []

    if component:
        where_clauses_collector.append("collector LIKE :comp_filter")
        where_clauses_processor.append("processor LIKE :comp_filter")
        params["comp_filter"] = f"%{component}%"

    if status:
        where_clauses_collector.append("status = :status_filter")
        where_clauses_processor.append("status = :status_filter")
        params["status_filter"] = status

    if from_date:
        where_clauses_collector.append("started_at >= :from_date")
        where_clauses_processor.append("started_at >= :from_date")
        params["from_date"] = from_date

    if correlation_id:
        where_clauses_collector.append("correlation_id = :correlation_id")
        where_clauses_processor.append("correlation_id = :correlation_id")
        params["correlation_id"] = correlation_id

    collector_where = (" WHERE " + " AND ".join(where_clauses_collector)) if where_clauses_collector else ""
    processor_where = (" WHERE " + " AND ".join(where_clauses_processor)) if where_clauses_processor else ""

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
            "correlation_id": str(row["correlation_id"]) if row.get("correlation_id") else None,
            "log_type": row["log_type"],
            "component": row["component"],
            "started_at": _fmt(row.get("started_at")),
            "completed_at": _fmt(row.get("completed_at")),
            "status": row["status"],
            "duration_ms": row.get("duration_ms"),
            "error_message": row.get("error_message"),
        }
        if row["log_type"] == "collection":
            entry["records_fetched"] = row.get("records_fetched")
            entry["records_written"] = row.get("records_written")
            if include_detail:
                entry["error_traceback"] = row.get("error_traceback")
        elif row["log_type"] == "processing":
            entry["model_used"] = row.get("model_used")
            entry["tokens_input"] = row.get("tokens_input")
            entry["tokens_output"] = row.get("tokens_output")
            entry["cost_usd"] = float(row["cost_usd"]) if row.get("cost_usd") else None
            if include_detail:
                entry["prompt_text"] = row.get("prompt_text")
                entry["raw_response"] = row.get("raw_response")
        logs.append(entry)

    return {"logs": logs, "limit": limit}


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
        "error_message": row.get("error_message"),
        "summary": summary,
    }


@router.get("/system/runs")
def get_system_runs(limit: int = Query(default=20, ge=1, le=100)):
    rows = query_many(
        "SELECT * FROM cycle_runs ORDER BY started_at DESC LIMIT :limit",
        {"limit": limit},
        config=load_config(),
    )
    return {"runs": [_run_payload(row) for row in rows], "limit": limit}


@router.get("/system/runs/{correlation_id}")
def get_system_run(correlation_id: str):
    config = load_config()
    row = query_one(
        "SELECT * FROM cycle_runs WHERE correlation_id = :cid",
        {"cid": correlation_id},
        config=config,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Run not found")

    stages = query_many(
        """
        SELECT log_id, correlation_id, 'collector' AS kind, collector AS component,
               started_at, completed_at, status, duration_ms, records_fetched,
               records_written, NULL::INTEGER AS tokens_input,
               NULL::INTEGER AS tokens_output, NULL::DOUBLE PRECISION AS cost_usd,
               error_message
        FROM collection_log WHERE correlation_id = :cid
        UNION ALL
        SELECT log_id, correlation_id, 'processor' AS kind, processor AS component,
               started_at, completed_at, status, duration_ms, NULL, NULL,
               tokens_input, tokens_output, cost_usd, error_message
        FROM processing_log WHERE correlation_id = :cid
        ORDER BY started_at
        """,
        {"cid": correlation_id},
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
            "cost_usd": float(stage["cost_usd"]) if stage.get("cost_usd") is not None else None,
        }
        for stage in stages
    ]
    return payload


@router.get("/system/cycle-status")
def get_cycle_status(correlation_id: str = Query(...)):
    config = load_config()

    row = query_one(
        "SELECT status, result_status, started_at, completed_at, error_message, summary "
        "FROM cycle_runs WHERE correlation_id = :cid",
        {"cid": correlation_id},
        config=config,
    )

    if not row:
        return {"status": "unknown", "correlation_id": correlation_id}

    run = get_system_run(correlation_id)
    return {
        "status": row.get("result_status") or row["status"],
        "lifecycle_status": row["status"],
        "result_status": row.get("result_status"),
        "correlation_id": correlation_id,
        "started_at": _fmt(row.get("started_at")),
        "completed_at": _fmt(row.get("completed_at")),
        "error_message": row.get("error_message"),
        "progress": run.get("summary", {}).get("progress", {}),
        "stages": run.get("stages", []),
    }
