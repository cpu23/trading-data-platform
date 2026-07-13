from datetime import datetime, timedelta, timezone
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
from locks import RunConflict
from orchestrator import (
    DEFAULT_ACCEPTED_TIMEOUT,
    DEFAULT_HEARTBEAT_TIMEOUT,
    RunAcceptanceConflict,
    accept_run,
    finalize_run_safely,
    get_last_collection_runs,
    get_run_for_retry,
    maintain_run_heartbeat,
    reconcile_abandoned_runs,
    run_collector,
    run_full_cycle,
    run_processor,
    start_run,
)

from price_stream import quote_stream
from scheduler import scheduler_status, start_scheduler, stop_scheduler

app = FastAPI(title="Trading Data Orchestrator")
logger = get_logger("orchestrator.api")

# Status compatibility only; PostgreSQL advisory locks provide coordination.
_cycle_correlation_id: str | None = None



def _get_config():
    return load_config()


def _job_timeout(config: dict, key: str, default: timedelta) -> timedelta:
    jobs = config.get("jobs", {})
    if not isinstance(jobs, dict):
        return default
    value = jobs.get(key)
    if value is None:
        return default
    try:
        timeout = timedelta(minutes=float(value))
    except (TypeError, ValueError, OverflowError):
        return default
    return timeout if timeout.total_seconds() >= 0 else default


@app.on_event("startup")
def on_startup():
    config = _get_config()
    setup_logging(level=config.get("logging", {}).get("level", "INFO"))

    if not check_connection(config):
        raise RuntimeError("Database connection failed")
    reconciliation = reconcile_abandoned_runs(
        config,
        accepted_timeout=_job_timeout(
            config, "accepted_timeout_minutes", DEFAULT_ACCEPTED_TIMEOUT
        ),
        heartbeat_timeout=_job_timeout(
            config, "heartbeat_timeout_minutes", DEFAULT_HEARTBEAT_TIMEOUT
        ),
    )
    logger.info(
        "abandoned_runs_reconciled",
        accepted=len(reconciliation.get("accepted_ids", [])),
        running=len(reconciliation.get("running_ids", [])),
        total=reconciliation.get("total", 0),
    )
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


def _idempotency_key_from_body(body: dict | None) -> str | None:
    if not isinstance(body, dict):
        return None
    value = body.get("idempotency_key")
    return str(value) if value else None


def _failed_run_summary(exc: Exception) -> dict:
    summary = {"status": "failed", "reason": str(exc)}
    if isinstance(exc, RunConflict):
        summary["conflict"] = exc.lock_name
    return summary


def _start_http_run(
    config: dict,
    correlation_id: str,
    worker_id: str,
    run_kind: str,
    component: str | None = None,
) -> bool | None:
    """Claim an accepted run, finalizing it safely when the claim errors."""
    try:
        return start_run(config, correlation_id, worker_id)
    except Exception:
        reason = "run start unavailable"
        logger.error(
            "run_start_failed",
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


def _accept_http_run(
    correlation_id: str,
    run_kind: str,
    requested_component: str | None,
    body: dict | None,
) -> datetime:
    try:
        return accept_run(
            _get_config(),
            correlation_id,
            "api",
            run_kind,
            requested_component,
            _idempotency_key_from_body(body),
        )
    except RunAcceptanceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "run_acceptance_failed",
            correlation_id=correlation_id,
            run_kind=run_kind,
            error=str(exc),
        )
        raise HTTPException(status_code=503, detail="Run acceptance unavailable") from exc


@app.post("/runs/{correlation_id}/retry", status_code=202)
def retry_abandoned_run(
    correlation_id: UUID,
    background_tasks: BackgroundTasks,
):
    config = _get_config()
    correlation_id_str = str(correlation_id)
    try:
        previous = get_run_for_retry(config, correlation_id_str)
    except Exception as exc:
        logger.error("run_retry_lookup_failed", correlation_id=correlation_id_str)
        raise HTTPException(status_code=503, detail="Run lookup unavailable") from exc
    if previous is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if previous.get("status") != "abandoned":
        raise HTTPException(status_code=409, detail="Only abandoned runs can be retried")

    run_kind = previous.get("run_kind")
    component = previous.get("requested_component")
    if run_kind not in {"cycle", "collector", "processor"}:
        raise HTTPException(status_code=409, detail="Run kind cannot be retried")
    if run_kind in {"collector", "processor"} and not component:
        raise HTTPException(status_code=409, detail="Run is missing its requested component")
    if run_kind == "collector":
        from collectors import get_all_collectors

        if component not in get_all_collectors():
            raise HTTPException(
                status_code=409,
                detail=f"Requested collector is no longer available: {component}",
            )
    elif run_kind == "processor":
        from processors import get_all_processors

        if component not in get_all_processors():
            raise HTTPException(
                status_code=409,
                detail=f"Requested processor is no longer available: {component}",
            )

    new_correlation_id = str(uuid4())
    try:
        accepted_at = accept_run(
            config,
            new_correlation_id,
            "retry",
            run_kind,
            component,
        )
    except RunAcceptanceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "run_retry_acceptance_failed",
            prior_correlation_id=correlation_id_str,
            correlation_id=new_correlation_id,
            error=str(exc),
        )
        raise HTTPException(status_code=503, detail="Run acceptance unavailable") from exc

    if run_kind == "cycle":
        background_tasks.add_task(_run_cycle_task, new_correlation_id)
    elif run_kind == "collector":
        background_tasks.add_task(_run_collector_task, str(component), new_correlation_id)
    else:
        background_tasks.add_task(_run_processor_task, str(component), new_correlation_id)

    return {
        "job_id": new_correlation_id,
        "prior_job_id": correlation_id_str,
        "accepted_at": accepted_at.isoformat(),
    }


def _run_cycle_task(correlation_id: str):
    global _cycle_correlation_id
    config = _get_config()
    worker_id = f"api:{uuid4()}"
    started = _start_http_run(config, correlation_id, worker_id, "cycle")
    if started is not True:
        if started is False:
            logger.info("cycle_start_lost", correlation_id=correlation_id)
        if _cycle_correlation_id == correlation_id:
            _cycle_correlation_id = None
        return
    try:
        with maintain_run_heartbeat(config, correlation_id, worker_id):
            result = run_full_cycle(
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
                worker_id=worker_id,
                run_kind="cycle",
            )
            if finalized:
                logger.info("cycle_completed", correlation_id=correlation_id, result=result)
    except Exception as exc:
        logger.error("cycle_failed", correlation_id=correlation_id, error=str(exc))
        finalize_run_safely(
            correlation_id,
            "failed",
            _failed_run_summary(exc),
            config,
            str(exc),
            worker_id=worker_id,
            run_kind="cycle",
        )
    finally:
        if _cycle_correlation_id == correlation_id:
            _cycle_correlation_id = None


@app.post("/run_cycle", status_code=202)
def trigger_cycle(
    background_tasks: BackgroundTasks,
    body: dict | None = Body(default=None),
):
    global _cycle_correlation_id

    correlation_id = _correlation_id_from_body(body)
    accepted_at = _accept_http_run(correlation_id, "cycle", None, body)
    _cycle_correlation_id = correlation_id
    background_tasks.add_task(_run_cycle_task, correlation_id)

    return {"job_id": correlation_id, "accepted_at": accepted_at.isoformat()}


@app.get("/cycle_status")
def get_cycle_status():
    return {
        "running": _cycle_correlation_id is not None,
        "correlation_id": _cycle_correlation_id,
    }


def _run_collector_task(source_id: str, correlation_id: str):
    config = _get_config()
    worker_id = f"api:{uuid4()}"
    started = _start_http_run(
        config, correlation_id, worker_id, "collector", source_id
    )
    if started is not True:
        if started is False:
            logger.info("collector_start_lost", source_id=source_id, correlation_id=correlation_id)
        return
    try:
        with maintain_run_heartbeat(config, correlation_id, worker_id):
            result = run_collector(
                source_id,
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
                run_kind="collector",
                component=source_id,
            )
            if finalized:
                logger.info("collector_trigger_completed", source_id=source_id, correlation_id=correlation_id, result=result)
    except Exception as exc:
        logger.error("collector_trigger_failed", source_id=source_id, correlation_id=correlation_id, error=str(exc))
        finalize_run_safely(
            correlation_id,
            "failed",
            _failed_run_summary(exc),
            config,
            str(exc),
            worker_id=worker_id,
            run_kind="collector",
            component=source_id,
        )


@app.post("/run_collector/{source_id}", status_code=202)
def trigger_collector(
    source_id: str,
    background_tasks: BackgroundTasks,
    body: dict | None = Body(default=None),
):
    from collectors import get_all_collectors

    if source_id not in get_all_collectors():
        raise HTTPException(status_code=404, detail=f"Unknown collector: {source_id}")

    correlation_id = _correlation_id_from_body(body)
    accepted_at = _accept_http_run(correlation_id, "collector", source_id, body)
    background_tasks.add_task(_run_collector_task, source_id, correlation_id)
    return {"job_id": correlation_id, "accepted_at": accepted_at.isoformat()}


def _run_processor_task(processor_id: str, correlation_id: str):
    config = _get_config()
    worker_id = f"api:{uuid4()}"
    started = _start_http_run(
        config, correlation_id, worker_id, "processor", processor_id
    )
    if started is not True:
        if started is False:
            logger.info("processor_start_lost", processor_id=processor_id, correlation_id=correlation_id)
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
                logger.info("processor_trigger_completed", processor_id=processor_id, correlation_id=correlation_id, result=result)
    except Exception as exc:
        logger.error("processor_trigger_failed", processor_id=processor_id, correlation_id=correlation_id, error=str(exc))
        finalize_run_safely(
            correlation_id,
            "failed",
            _failed_run_summary(exc),
            config,
            str(exc),
            worker_id=worker_id,
            run_kind="processor",
            component=processor_id,
        )


@app.post("/run_processor/{processor_id}", status_code=202)
def trigger_processor(
    processor_id: str,
    background_tasks: BackgroundTasks,
    body: dict | None = Body(default=None),
):
    from processors import get_all_processors

    if processor_id not in get_all_processors():
        raise HTTPException(status_code=404, detail=f"Unknown processor: {processor_id}")

    correlation_id = _correlation_id_from_body(body)
    accepted_at = _accept_http_run(correlation_id, "processor", processor_id, body)
    background_tasks.add_task(_run_processor_task, processor_id, correlation_id)
    return {"job_id": correlation_id, "accepted_at": accepted_at.isoformat()}



@app.get("/quality")
def quality():
    config = _get_config()
    logger.info("quality_endpoint_called")
    results = run_quality_checks(config)
    any_unhealthy = any(not r.get("healthy", True) for r in results.values())
    overall = "degraded" if any_unhealthy else "healthy"
    logger.info("quality_check_complete", overall=overall, check_count=len(results))
    return {"overall": overall, "checks": results}
