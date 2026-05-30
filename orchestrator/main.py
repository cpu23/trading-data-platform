from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import FastAPI, BackgroundTasks, Body, HTTPException
from sqlalchemy import text

from config_loader import load_config
from db import check_connection, get_session
from logging_config import get_logger, setup_logging
from orchestrator import run_full_cycle, run_collector, run_processor

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
    logger.info("orchestrator_http_started", action="startup")


@app.get("/health")
def health():
    return {"status": "ok"}


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


def _write_cycle_run(correlation_id: str, status: str, error_message: str | None = None):
    config = _get_config()
    now = datetime.now(timezone.utc)
    try:
        with get_session(config) as session:
            if status == "running":
                session.execute(
                    text(
                        "INSERT INTO cycle_runs (correlation_id, status, started_at, triggered_by) "
                        "VALUES (:cid, :status, :started_at, :triggered_by)"
                    ),
                    {
                        "cid": correlation_id,
                        "status": status,
                        "started_at": now,
                        "triggered_by": "api",
                    },
                )
            else:
                session.execute(
                    text(
                        "UPDATE cycle_runs SET status = :status, completed_at = :completed_at, "
                        "error_message = :error_message WHERE correlation_id = :cid"
                    ),
                    {
                        "cid": correlation_id,
                        "status": status,
                        "completed_at": now,
                        "error_message": error_message,
                    },
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
        _write_cycle_run(correlation_id, "completed")
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
