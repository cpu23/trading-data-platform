from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import FastAPI, BackgroundTasks, Body, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import text

from config_loader import load_config
from db import check_connection, get_session

try:
    from data_quality import DATA_QUALITY_CHECKS, run_quality_checks
except ImportError:
    DATA_QUALITY_CHECKS = {}

    def run_quality_checks(config):
        return {}
from logging_config import get_logger, setup_logging
from orchestrator import ensure_run, finish_run, run_full_cycle, run_collector, run_processor, get_last_collection_runs

from price_stream import quote_stream
from scheduler import scheduler_status, start_scheduler, stop_scheduler

app = FastAPI(title="Trading Data Orchestrator")
logger = get_logger("orchestrator.api")

_cycle_lock = None
_cycle_correlation_id: str | None = None



def _get_config():
    return load_config()


@app.on_event("startup")
def on_startup():
    global _cycle_lock
    _cycle_lock = __import__("threading").Lock()


    config = _get_config()
    setup_logging(level=config.get("logging", {}).get("level", "INFO"))

    if not check_connection(config):
        raise RuntimeError("Database connection failed")
    start_scheduler(config)
    quote_stream.start(config)
    logger.info("orchestrator_http_started", action="startup")


@app.on_event("shutdown")
def on_shutdown():
    quote_stream.stop()
    stop_scheduler()


@app.get("/health")
def health():
    config = _get_config()
    scheduler = scheduler_status()
    stream = quote_stream.state
    if not check_connection(config):
        payload = {
            "liveness": "ok",
            "readiness": "unready",
            "data_health": "degraded",
            "status": "unhealthy",
            "components": [{
                "name": "database", "kind": "service", "critical": True,
                "status": "unavailable", "reason": "database connection failed",
            }],
            "scheduler": scheduler,
            "stream": stream,
            "collectors": {},
        }
        return JSONResponse(status_code=503, content=payload)

    last_runs = get_last_collection_runs(config)
    collectors_status = {}
    for run in last_runs:
        started = run.get("started_at")
        collectors_status[run.get("collector")] = {
            "last_status": run.get("status"),
            "last_run_at": started.isoformat() if hasattr(started, "isoformat") else str(started) if started else None,
            "records_fetched": run.get("records_fetched"),
            "records_written": run.get("records_written"),
            "error_message": run.get("error_message"),
        }

    components = [{
        "name": "database", "kind": "service", "critical": True,
        "status": "available", "reason": None,
    }]
    try:
        quality_results = run_quality_checks(config)
    except Exception as exc:
        logger.error("health_quality_checks_failed", error=str(exc))
        quality_results = {"quality_runner": {"healthy": False, "detail": str(exc)}}
    unhealthy = {
        check_id: result for check_id, result in quality_results.items()
        if not result.get("healthy", True)
    }
    if unhealthy:
        components.append({
            "name": "data_quality", "kind": "data", "critical": False,
            "status": "degraded",
            "reason": "; ".join(
                f"{check_id}: {result.get('detail', 'unhealthy')}"
                for check_id, result in unhealthy.items()
            ),
        })

    data_health = "degraded" if unhealthy else "healthy"
    return {
        "liveness": "ok",
        "readiness": "ready",
        "data_health": data_health,
        "status": "degraded" if unhealthy else "healthy",
        "components": components,
        "scheduler": scheduler,
        "stream": stream,
        "collectors": collectors_status,
    }


@app.get("/quotes")
def quotes():
    return quote_stream.snapshot()


def _correlation_id_from_body(body: dict | None) -> str:
    if isinstance(body, dict) and body.get("correlation_id"):
        try:
            return str(UUID(str(body["correlation_id"])))
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="correlation_id must be a valid UUID",
            )
    return str(uuid4())


def _write_cycle_run(
    correlation_id: str,
    status: str,
    error_message: str | None = None,
    result_status: str | None = None,
    summary: dict | None = None,
):
    config = _get_config()
    try:
        if status == "running":
            ensure_run(correlation_id, config, triggered_by="api")
        else:
            finish_run(
                correlation_id,
                result_status or ("failed" if status == "failed" else "success"),
                summary or {},
                config,
                error_message,
            )
    except Exception as exc:
        logger.error("cycle_run_write_failed", correlation_id=correlation_id, status=status, error=str(exc))


def _run_cycle_task(correlation_id: str):
    global _cycle_correlation_id
    _write_cycle_run(correlation_id, "running")
    try:
        config = _get_config()
        result = run_full_cycle(config=config, correlation_id=correlation_id)
        logger.info("cycle_completed", correlation_id=correlation_id, result=result)
        _write_cycle_run(
            correlation_id,
            "completed",
            result_status=result["status"],
            summary=result,
        )
    except Exception as exc:
        logger.error("cycle_failed", correlation_id=correlation_id, error=str(exc))
        _write_cycle_run(correlation_id, "failed", error_message=str(exc))
    finally:
        _cycle_correlation_id = None
        if _cycle_lock:
            _cycle_lock.release()


@app.post("/run_cycle", status_code=202)
def trigger_cycle(
    background_tasks: BackgroundTasks,
    body: dict | None = Body(default=None),
):
    global _cycle_correlation_id

    correlation_id = _correlation_id_from_body(body)
    acquired = _cycle_lock.acquire(blocking=False)
    if not acquired:
        running_id = _cycle_correlation_id or "unknown"
        raise HTTPException(status_code=409, detail=f"Cycle already running: {running_id}")

    try:
        _cycle_correlation_id = correlation_id
        background_tasks.add_task(_run_cycle_task, correlation_id)
    except Exception:
        _cycle_correlation_id = None
        _cycle_lock.release()
        raise

    return {
        "job_id": correlation_id,
        "accepted_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/cycle_status")
def get_cycle_status():
    return {
        "running": _cycle_correlation_id is not None,
        "correlation_id": _cycle_correlation_id,
    }


def _run_collector_task(source_id: str, correlation_id: str):
    try:
        config = _get_config()
        result = run_collector(source_id, config=config, correlation_id=correlation_id)
        finish_run(correlation_id, result["status"], result, config, result.get("error"))
        logger.info("collector_trigger_completed", source_id=source_id, correlation_id=correlation_id, result=result)
    except Exception as exc:
        logger.error("collector_trigger_failed", source_id=source_id, correlation_id=correlation_id, error=str(exc))


@app.post("/run_collector/{source_id}", status_code=202)
def trigger_collector(
    source_id: str,
    background_tasks: BackgroundTasks,
    body: dict | None = Body(default=None),
):
    from collectors import get_all_collectors

    valid_ids = get_all_collectors()
    if source_id not in valid_ids:
        raise HTTPException(status_code=404, detail=f"Unknown collector: {source_id}")

    correlation_id = _correlation_id_from_body(body)
    background_tasks.add_task(_run_collector_task, source_id, correlation_id)
    return {
        "job_id": correlation_id,
        "accepted_at": datetime.now(timezone.utc).isoformat(),
    }


def _run_processor_task(processor_id: str, correlation_id: str):
    try:
        config = _get_config()
        result = run_processor(processor_id, config=config, correlation_id=correlation_id)
        finish_run(correlation_id, result["status"], result, config, result.get("error"))
        logger.info("processor_trigger_completed", processor_id=processor_id, correlation_id=correlation_id, result=result)
    except Exception as exc:
        logger.error("processor_trigger_failed", processor_id=processor_id, correlation_id=correlation_id, error=str(exc))


@app.post("/run_processor/{processor_id}", status_code=202)
def trigger_processor(
    processor_id: str,
    background_tasks: BackgroundTasks,
    body: dict | None = Body(default=None),
):
    from processors import get_all_processors

    valid_ids = get_all_processors()
    if processor_id not in valid_ids:
        raise HTTPException(status_code=404, detail=f"Unknown processor: {processor_id}")

    correlation_id = _correlation_id_from_body(body)
    background_tasks.add_task(_run_processor_task, processor_id, correlation_id)
    return {
        "job_id": correlation_id,
        "accepted_at": datetime.now(timezone.utc).isoformat(),
    }



@app.get("/quality")
def quality():
    config = _get_config()
    logger.info("quality_endpoint_called")
    results = run_quality_checks(config)
    any_unhealthy = any(not r.get("healthy", True) for r in results.values())
    overall = "degraded" if any_unhealthy else "healthy"
    logger.info("quality_check_complete", overall=overall, check_count=len(results))
    return {"overall": overall, "checks": results}
