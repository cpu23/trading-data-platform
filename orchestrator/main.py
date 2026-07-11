from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import FastAPI, BackgroundTasks, Body, HTTPException
from sqlalchemy import text

from config_loader import load_config
from db import check_connection, get_session

try:
    from data_quality import DATA_QUALITY_CHECKS
except ImportError:
    DATA_QUALITY_CHECKS = {}
from logging_config import get_logger, setup_logging
from orchestrator import ensure_run, finish_run, run_full_cycle, run_collector, run_processor, get_last_collection_runs
from sources.financial_times import run_financial_times
from price_stream import quote_stream
from scheduler import scheduler_status, start_scheduler, stop_scheduler

app = FastAPI(title="Trading Data Orchestrator")
logger = get_logger("orchestrator.api")

_cycle_lock = None
_cycle_correlation_id: str | None = None
_ft_lock = None
_ft_correlation_id: str | None = None


def _get_config():
    return load_config()


@app.on_event("startup")
def on_startup():
    global _cycle_lock
    _cycle_lock = __import__("threading").Lock()
    global _ft_lock
    _ft_lock = __import__("threading").Lock()

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
    return {
        "status": "ok",
        "scheduler": scheduler_status(),
        "stream": quote_stream.state,
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
    correlation_id = _correlation_id_from_body(body)
    background_tasks.add_task(_run_processor_task, processor_id, correlation_id)
    return {
        "job_id": correlation_id,
        "accepted_at": datetime.now(timezone.utc).isoformat(),
    }


VALID_FT_SECTIONS = {"homepage", "lex", "unhedged"}


@app.post("/run_financial_times", status_code=202)
def trigger_financial_times(
    background_tasks: BackgroundTasks,
    body: dict | None = Body(default=None),
):
    global _ft_correlation_id

    correlation_id = _correlation_id_from_body(body)

    sections = ["homepage", "lex", "unhedged"]
    since = None
    until = None
    max_articles = None
    ingest = True
    wait_for_capture = True

    if isinstance(body, dict):
        sections = body.get("sections", sections)
        if body.get("since"):
            since = datetime.fromisoformat(body["since"])
        if body.get("until"):
            until = datetime.fromisoformat(body["until"])
        max_articles = body.get("max_articles")
        ingest = body.get("ingest", True)
        wait_for_capture = body.get("wait_for_capture", True)

    invalid = [s for s in sections if s not in VALID_FT_SECTIONS]
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid sections: {', '.join(invalid)}. Valid: {', '.join(sorted(VALID_FT_SECTIONS))}",
        )

    acquired = _ft_lock.acquire(blocking=False)
    if not acquired:
        running_id = _ft_correlation_id or "unknown"
        raise HTTPException(
            status_code=409,
            detail=f"FT collection already running: {running_id}",
        )

    try:
        _ft_correlation_id = correlation_id
        background_tasks.add_task(
            _run_ft_task,
            sections,
            since,
            until,
            max_articles,
            ingest,
            wait_for_capture,
            correlation_id,
        )
    except Exception:
        _ft_correlation_id = None
        _ft_lock.release()
        raise

    return {
        "job_id": correlation_id,
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        "status_url": f"/ft_status/{correlation_id}",
    }


def _run_ft_task(
    sections: list[str],
    since,
    until,
    max_articles,
    ingest: bool,
    wait_for_capture: bool,
    correlation_id: str,
):
    global _ft_correlation_id
    try:
        config = _get_config()
        result = run_financial_times(
            config=config,
            correlation_id=correlation_id,
            sections=tuple(sections),
            since=since,
            until=until,
            max_articles=max_articles,
            ingest=ingest,
            wait_for_capture=wait_for_capture,
        )
        logger.info(
            "ft_trigger_completed",
            correlation_id=correlation_id,
            result=result,
        )
    except Exception as exc:
        logger.error(
            "ft_trigger_failed",
            correlation_id=correlation_id,
            error=str(exc),
        )
    finally:
        _ft_correlation_id = None
        if _ft_lock:
            _ft_lock.release()


@app.get("/quality")
def quality():
    config = _get_config()
    logger.info("quality_endpoint_called")
    results: dict[str, dict] = {}
    for check_id, check_fn in DATA_QUALITY_CHECKS.items():
        try:
            results[check_id] = check_fn(config)
        except Exception as exc:
            logger.error("quality_check_failed", check_id=check_id, error=str(exc))
            results[check_id] = {"healthy": False, "detail": f"check failed: {str(exc)}"}
    any_unhealthy = any(not r.get("healthy", True) for r in results.values())
    overall = "degraded" if any_unhealthy else "healthy"
    logger.info("quality_check_complete", overall=overall, check_count=len(results))
    return {"overall": overall, "checks": results}
