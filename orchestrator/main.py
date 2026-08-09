import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from budgets import (
    BudgetBlock,
    BudgetContext,
    BudgetExceeded,
    mint_trusted_manual_authorization,
    trusted_manual_budget_context,
)
from config_loader import load_config
from contracts import (
    CycleStatusResponse,
    OrchestratorHealthResponse,
    QualityResponse,
    RunAcceptedResponse,
)
from db import check_connection
from db import get_session as get_session

try:
    from data_quality import DATA_QUALITY_CHECKS, run_quality_checks
except ImportError:
    DATA_QUALITY_CHECKS = {}

    def run_quality_checks(config):
        return {}


from events.publisher import event_pipeline_summary
from events.worker import outbox_worker
from http_client import close_shared_client
from investment_service import (
    AnalysisInProgress,
)
from investment_service import (
    analyze_document as analyze_investment_document,
)
from investment_service import (
    get_analysis as get_investment_analysis,
)
from investment_service import (
    get_dashboard as get_investment_dashboard,
)
from investment_service import (
    store_document as store_investment_document,
)
from investment_service import (
    store_document_url as store_investment_document_url,
)
from job_worker import job_worker
from llm_client import model_preflight
from locks import RunConflict
from logging_config import get_logger, setup_logging
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
    run_news_source,
    run_processor,
    start_run,
)
from price_stream import quote_stream
from scheduler import scheduler_status, start_scheduler, stop_scheduler

app = FastAPI(title="Trading Data Orchestrator")
logger = get_logger("orchestrator.api")
optional_basic = HTTPBasic(auto_error=False)
VALID_CYCLE_MODES = frozenset({"refresh", "analyze", "force_full"})

# Status compatibility only; PostgreSQL advisory locks provide coordination.
_cycle_correlation_id: str | None = None


def require_internal_basic(
    credentials: HTTPBasicCredentials | None = Depends(optional_basic),
):
    username = os.environ.get("DASHBOARD_USER", "")
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    supplied_user = credentials.username if credentials else ""
    supplied_pass = credentials.password if credentials else ""
    valid = (
        bool(username and password)
        and secrets.compare_digest(supplied_user, username)
        and secrets.compare_digest(supplied_pass, password)
    )
    if not valid:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    return supplied_user


def _get_config():
    return load_config()


_quality_cache_lock = threading.Lock()
_quality_cache_config: dict | None = None
_quality_cache_until = 0.0
_quality_cache_results: dict[str, dict] = {}


def _health_quality_snapshot(config: dict) -> dict[str, dict]:
    try:
        ttl_seconds = max(
            float(os.environ.get("HEALTH_QUALITY_CACHE_SECONDS", "30")),
            0.0,
        )
    except (TypeError, ValueError):
        ttl_seconds = 30.0

    now = time.monotonic()
    global _quality_cache_config, _quality_cache_until, _quality_cache_results
    with _quality_cache_lock:
        if _quality_cache_config is config and now < _quality_cache_until:
            return _quality_cache_results
        results = run_quality_checks(config)
        _quality_cache_config = config
        _quality_cache_until = time.monotonic() + ttl_seconds
        _quality_cache_results = results
        return results


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
    outbox_worker.start(config)
    try:
        job_worker.start(config)
    except Exception:
        logger.warning("analysis_job_worker_start_failed", error_type="startup")
    start_scheduler(config)
    quote_stream.start(config)
    logger.info("orchestrator_http_started", action="startup")


@app.on_event("shutdown")
def on_shutdown():
    outbox_worker.stop()
    try:
        job_worker.stop()
    except Exception:
        logger.warning("analysis_job_worker_stop_failed", error_type="shutdown")
    quote_stream.stop()
    stop_scheduler()
    close_shared_client()


@app.get("/health", response_model=OrchestratorHealthResponse)
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
            "components": [
                {
                    "name": "database",
                    "kind": "service",
                    "critical": True,
                    "status": "unavailable",
                    "reason": "database connection failed",
                }
            ],
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
            "last_run_at": started.isoformat()
            if hasattr(started, "isoformat")
            else str(started)
            if started
            else None,
            "records_fetched": run.get("records_fetched"),
            "records_written": run.get("records_written"),
            "error_message": run.get("error_message"),
        }

    components = [
        {
            "name": "database",
            "kind": "service",
            "critical": True,
            "status": "available",
            "reason": None,
        }
    ]
    try:
        quality_results = _health_quality_snapshot(config)
    except Exception as exc:
        logger.error("health_quality_checks_failed", error=str(exc))
        quality_results = {"quality_runner": {"healthy": False, "detail": str(exc)}}
    unhealthy = {
        check_id: result
        for check_id, result in quality_results.items()
        if not result.get("healthy", True)
    }
    if unhealthy:
        components.append(
            {
                "name": "data_quality",
                "kind": "data",
                "critical": False,
                "status": "degraded",
                "reason": "; ".join(
                    f"{check_id}: {result.get('detail', 'unhealthy')}"
                    for check_id, result in unhealthy.items()
                ),
            }
        )

    quality_payload = {
        "overall": "degraded" if unhealthy else "healthy",
        "checks": quality_results,
    }

    data_health = quality_payload["overall"]
    return {
        "liveness": "ok",
        "readiness": "ready",
        "data_health": data_health,
        "status": data_health,
        "components": components,
        "scheduler": scheduler,
        "stream": stream,
        "collectors": collectors_status,
        "quality": quality_payload,
    }


@app.get("/quotes")
def quotes():
    return quote_stream.snapshot()


@app.post(
    "/model/preflight",
    dependencies=[Depends(require_internal_basic)],
)
def model_preflight_endpoint(body: dict = Body(default={})):
    """Validate the active (or requested) model slug without paid inference."""
    config = _get_config()
    requested = body.get("model") if isinstance(body, dict) else None
    model = (
        str(requested).strip()
        if isinstance(requested, str) and str(requested).strip()
        else None
    )
    return model_preflight(config, model=model)


@app.get(
    "/events/status",
    dependencies=[Depends(require_internal_basic)],
)
def event_pipeline_status():
    """Return durable backlog, recent event, and worker state."""
    return {
        **event_pipeline_summary(_get_config()),
        "worker": dict(outbox_worker.state),
    }


@app.get(
    "/jobs/status",
    dependencies=[Depends(require_internal_basic)],
)
def analysis_jobs_status():
    try:
        counters = dict(job_worker.state_counters())
    except Exception:
        counters = {}
    try:
        enabled = bool(job_worker.enabled(_get_config()))
    except Exception:
        enabled = False
    thread = getattr(job_worker, "_thread", None)
    return {
        "running": bool(thread is not None and thread.is_alive()),
        "enabled": enabled,
        "worker_id": str(getattr(job_worker, "worker_id", "")),
        "counters": counters,
    }

@app.get(
    "/research/status",
    dependencies=[Depends(require_internal_basic)],
)
def research_intelligence_status():
    """Return bounded case, request, model-cost, and durable-job status."""
    try:
        from research_intelligence.queries import research_status

        config = _get_config()
        with get_session(config) as session:
            return research_status(session)
    except Exception as exc:
        logger.warning(
            "research_status_unavailable", error_type=type(exc).__name__
        )
        raise HTTPException(
            status_code=503, detail="Research status unavailable"
        ) from exc

def _research_force(body: dict | None) -> bool:
    if body is None:
        return False
    if not isinstance(body, dict) or set(body) - {"force"}:
        raise HTTPException(status_code=422, detail="body may contain only force")
    force = body.get("force", False)
    if not isinstance(force, bool):
        raise HTTPException(status_code=422, detail="force must be boolean")
    return force



@app.post(
    "/research/run",
    status_code=202,
    dependencies=[Depends(require_internal_basic)],
)
def trigger_research_discovery(body: dict | None = Body(default=None)):
    """Queue bounded discovery and macro-driver research immediately."""
    force = _research_force(body)
    try:
        from research_intelligence.operations import enqueue_research_job

        return enqueue_research_job(
            _get_config(),
            job_type="research_discovery",
            force=force,
            triggered_by="api",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "research_discovery_enqueue_failed", error_type=type(exc).__name__
        )
        raise HTTPException(
            status_code=503, detail="Research run could not be queued"
        ) from exc


@app.post(
    "/research/cases/{case_id}/run",
    status_code=202,
    dependencies=[Depends(require_internal_basic)],
)
def trigger_research_case_update(
    case_id: UUID, body: dict | None = Body(default=None)
):
    """Queue one bounded research-case update."""
    force = _research_force(body)
    try:
        from research_intelligence.operations import enqueue_research_job

        return enqueue_research_job(
            _get_config(),
            job_type="research_case_update",
            case_id=str(case_id),
            force=force,
            triggered_by="api",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "research_case_enqueue_failed",
            case_id=str(case_id),
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=503, detail="Research case update could not be queued"
        ) from exc


@app.post(
    "/research/jobs/{job_id}/retry",
    status_code=202,
    dependencies=[Depends(require_internal_basic)],
)
def retry_research_analysis_job(job_id: UUID):
    """Retry failed research work without mutating prior job history."""
    try:
        from research_intelligence.operations import retry_research_job

        return retry_research_job(_get_config(), str(job_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "research_job_retry_failed",
            job_id=str(job_id),
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=503, detail="Research job could not be retried"
        ) from exc


@app.get(
    "/sections/{section_key}",
    dependencies=[Depends(require_internal_basic)],
)
def section_snapshot_status(section_key: str):
    """Return current section data and a bounded version history."""
    key = section_key.strip()
    if (
        not key
        or len(key) > 80
        or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
            for character in key
        )
    ):
        raise HTTPException(status_code=400, detail="Invalid section key")
    try:
        config = _get_config()
        from section_snapshots import get_current_snapshot, list_snapshot_history

        with get_session(config) as session:
            current = get_current_snapshot(session, section_key=key, scope_key="global")
            history = list_snapshot_history(
                session, section_key=key, scope_key="global", limit=20
            )
        return {"section_key": key, "current": current, "history": history}
    except Exception:
        logger.warning("section_snapshot_unavailable", section_key=key)
        return {"section_key": key, "current": None, "history": []}


@app.get(
    "/investment/dashboard",
    dependencies=[Depends(require_internal_basic)],
)
def investment_dashboard():
    return get_investment_dashboard(_get_config())


@app.get(
    "/investment/analyses/{analysis_id}",
    dependencies=[Depends(require_internal_basic)],
)
def investment_analysis(analysis_id: UUID):
    payload = get_investment_analysis(_get_config(), str(analysis_id))
    if payload is None:
        raise HTTPException(status_code=404, detail="Investment analysis not found")
    return payload


@app.post(
    "/investment/documents",
    status_code=201,
    dependencies=[Depends(require_internal_basic)],
)
async def ingest_investment_document(request: Request):
    metadata = dict(request.query_params)
    content = await request.body()
    try:
        return store_investment_document(
            _get_config(),
            metadata,
            content,
            request.headers.get("content-type"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "investment_document_ingest_failed",
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Investment document storage unavailable",
        ) from exc


@app.post(
    "/investment/urls",
    status_code=201,
    dependencies=[Depends(require_internal_basic)],
)
def ingest_investment_url(body: dict = Body(...)):
    try:
        return store_investment_document_url(_get_config(), body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "investment_url_ingest_failed",
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail="Investment document could not be fetched",
        ) from exc


@app.post(
    "/investment/documents/{document_id}/analyze",
    dependencies=[Depends(require_internal_basic)],
)
def run_investment_analysis(
    document_id: UUID,
    body: dict | None = Body(default=None),
):
    market_inputs = body.get("market_inputs") if isinstance(body, dict) else None
    if market_inputs is not None and not isinstance(market_inputs, dict):
        raise HTTPException(status_code=422, detail="market_inputs must be an object")
    try:
        return analyze_investment_document(
            _get_config(),
            str(document_id),
            market_inputs,
        )
    except BudgetBlock as exc:
        status_code = 429 if isinstance(exc, BudgetExceeded) else 503
        raise HTTPException(status_code=status_code, detail=exc.safe_reason) from exc
    except AnalysisInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "investment_analysis_failed",
            document_id=str(document_id),
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Investment analysis failed",
        ) from exc


@app.get(
    "/investment/filings/status",
    dependencies=[Depends(require_internal_basic)],
)
def investment_filings_status():
    from investment_filings import get_filing_source_status

    return get_filing_source_status(_get_config())


@app.post(
    "/investment/filings/collect",
    status_code=202,
    dependencies=[Depends(require_internal_basic)],
)
def trigger_filing_collection(
    background_tasks: BackgroundTasks,
    body: dict | None = Body(default=None),
):
    correlation_id = _correlation_id_from_body(body)
    auto_analyze = bool(body.get("auto_analyze")) if isinstance(body, dict) else False
    accepted_at = _accept_http_run(
        correlation_id, "filings", "investment_filings", body
    )
    background_tasks.add_task(_run_filings_task, correlation_id, auto_analyze)
    return {"job_id": correlation_id, "accepted_at": accepted_at.isoformat()}


def _run_filings_task(correlation_id: str, auto_analyze: bool):
    from investment_filings import run_filing_collection

    config = _get_config()
    worker_id = f"api:{uuid4()}"
    started = _start_http_run(
        config, correlation_id, worker_id, "filings", "investment_filings"
    )
    if started is not True:
        return
    try:
        with maintain_run_heartbeat(config, correlation_id, worker_id):
            result = run_filing_collection(
                config, correlation_id=correlation_id, auto_analyze=auto_analyze
            )
            finalized = finalize_run_safely(
                correlation_id,
                result.get("status", "completed"),
                result,
                config,
                worker_id=worker_id,
                run_kind="filings",
                component="investment_filings",
            )
            if finalized:
                logger.info("filings_trigger_completed", correlation_id=correlation_id)
    except Exception as exc:
        logger.error(
            "filings_trigger_failed", correlation_id=correlation_id, error=str(exc)
        )
        finalize_run_safely(
            correlation_id,
            "failed",
            {},
            config,
            str(exc),
            worker_id=worker_id,
            run_kind="filings",
            component="investment_filings",
        )


def _correlation_id_from_body(body: dict | None) -> str:
    if isinstance(body, dict) and body.get("correlation_id"):
        try:
            return str(UUID(str(body["correlation_id"])))
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail="correlation_id must be a valid UUID",
            ) from exc
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
    request_summary: dict | None = None,
) -> datetime:
    try:
        return accept_run(
            _get_config(),
            correlation_id,
            "api",
            run_kind,
            requested_component,
            _idempotency_key_from_body(body),
            request_summary=request_summary,
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
        raise HTTPException(
            status_code=503, detail="Run acceptance unavailable"
        ) from exc


@app.post(
    "/runs/{correlation_id}/retry",
    status_code=202,
    response_model=RunAcceptedResponse,
    dependencies=[Depends(require_internal_basic)],
)
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
        raise HTTPException(
            status_code=409, detail="Only abandoned runs can be retried"
        )

    run_kind = previous.get("run_kind")
    component = previous.get("requested_component")
    if run_kind not in {"cycle", "collector", "processor", "news"}:
        raise HTTPException(status_code=409, detail="Run kind cannot be retried")
    if run_kind in {"collector", "processor", "news"} and not component:
        raise HTTPException(
            status_code=409, detail="Run is missing its requested component"
        )
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
    elif run_kind == "news":
        from sources.news_registry import get_news_source_ids

        if component not in get_news_source_ids():
            raise HTTPException(
                status_code=409,
                detail=f"Requested news source is no longer available: {component}",
            )

    new_correlation_id = str(uuid4())
    cycle_mode = "refresh"
    retry_summary = None
    if run_kind == "cycle":
        prior_summary = previous.get("summary")
        if isinstance(prior_summary, str):
            try:
                prior_summary = json.loads(prior_summary)
            except (TypeError, ValueError):
                prior_summary = {}
        if isinstance(prior_summary, dict):
            candidate_mode = prior_summary.get("mode")
            if isinstance(candidate_mode, str) and candidate_mode in VALID_CYCLE_MODES:
                cycle_mode = candidate_mode
        retry_summary = {
            "mode": cycle_mode,
            "budget_confirmed": False,
            "retry": True,
        }
    try:
        accepted_at = accept_run(
            config,
            new_correlation_id,
            "retry",
            run_kind,
            component,
            request_summary=retry_summary,
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
        raise HTTPException(
            status_code=503, detail="Run acceptance unavailable"
        ) from exc

    if run_kind == "cycle":
        background_tasks.add_task(_run_cycle_task, new_correlation_id, cycle_mode, None)
    elif run_kind == "collector":
        background_tasks.add_task(
            _run_collector_task, str(component), new_correlation_id
        )
    elif run_kind == "processor":
        background_tasks.add_task(
            _run_processor_task, str(component), new_correlation_id
        )
    else:
        background_tasks.add_task(_run_news_task, str(component), new_correlation_id)

    return {
        "job_id": new_correlation_id,
        "prior_job_id": correlation_id_str,
        "accepted_at": accepted_at.isoformat(),
    }


def _run_cycle_task(
    correlation_id: str,
    mode: str = "refresh",
    budget_context: BudgetContext | None = None,
):
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
                mode=mode,
                budget_context=budget_context,
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
                logger.info(
                    "cycle_completed", correlation_id=correlation_id, result=result
                )
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


@app.post(
    "/run_cycle",
    status_code=202,
    response_model=RunAcceptedResponse,
    dependencies=[Depends(require_internal_basic)],
)
def trigger_cycle(
    background_tasks: BackgroundTasks,
    body: dict | None = Body(default=None),
    credentials: HTTPBasicCredentials | None = Depends(optional_basic),
):
    global _cycle_correlation_id

    if body is None:
        body = {}
    elif not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Cycle body must be an object")

    mode = body.get("mode", "refresh")
    if not isinstance(mode, str) or mode not in VALID_CYCLE_MODES:
        raise HTTPException(status_code=422, detail="Invalid cycle mode")
    if "budget_confirmed" in body and type(body["budget_confirmed"]) is not bool:
        raise HTTPException(
            status_code=422, detail="budget_confirmed must be a boolean"
        )

    budget_context = None
    if mode == "force_full":
        username = os.environ.get("DASHBOARD_USER", "")
        password = os.environ.get("DASHBOARD_PASSWORD", "")
        if not username or not password:
            raise HTTPException(
                status_code=503, detail="Internal authentication unavailable"
            )
        supplied = credentials if hasattr(credentials, "username") else None
        authenticated = (
            bool(supplied)
            and secrets.compare_digest(supplied.username, username)
            and secrets.compare_digest(supplied.password, password)
        )
        if not authenticated:
            raise HTTPException(
                status_code=401,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Basic"},
            )
        if body.get("budget_confirmed") is not True:
            raise HTTPException(
                status_code=422,
                detail="force_full requires explicit budget confirmation",
            )
        budget_context = trusted_manual_budget_context(
            force=True,
            manual_authorized=True,
            authorization=mint_trusted_manual_authorization(),
        )

    request_summary = {
        "mode": mode,
        "budget_confirmed": mode == "force_full",
    }
    correlation_id = _correlation_id_from_body(body)
    accepted_at = _accept_http_run(
        correlation_id,
        "cycle",
        None,
        body,
        request_summary=request_summary,
    )
    _cycle_correlation_id = correlation_id
    background_tasks.add_task(_run_cycle_task, correlation_id, mode, budget_context)

    return {"job_id": correlation_id, "accepted_at": accepted_at.isoformat()}


@app.get("/cycle_status", response_model=CycleStatusResponse)
def get_cycle_status():
    return {
        "running": _cycle_correlation_id is not None,
        "correlation_id": _cycle_correlation_id,
    }


def _run_collector_task(source_id: str, correlation_id: str):
    config = _get_config()
    worker_id = f"api:{uuid4()}"
    started = _start_http_run(config, correlation_id, worker_id, "collector", source_id)
    if started is not True:
        if started is False:
            logger.info(
                "collector_start_lost",
                source_id=source_id,
                correlation_id=correlation_id,
            )
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
                logger.info(
                    "collector_trigger_completed",
                    source_id=source_id,
                    correlation_id=correlation_id,
                    result=result,
                )
    except Exception as exc:
        logger.error(
            "collector_trigger_failed",
            source_id=source_id,
            correlation_id=correlation_id,
            error=str(exc),
        )
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


@app.post(
    "/run_collector/{source_id}",
    status_code=202,
    response_model=RunAcceptedResponse,
    dependencies=[Depends(require_internal_basic)],
)
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


def _news_failure_summary(exc: Exception, correlation_id: str) -> dict:
    if isinstance(exc, RunConflict):
        return {
            "status": "failed",
            "state": "conflict",
            "error": str(exc),
            "code": "news_run_conflict",
            "feed_published": False,
            "new_item_count": 0,
            "duration_ms": 0,
            "correlation_id": correlation_id,
        }
    return {
        "status": "failed",
        "state": "failed",
        "error": "news run failed",
        "code": "news_run_failed",
        "feed_published": False,
        "new_item_count": 0,
        "duration_ms": 0,
        "correlation_id": correlation_id,
    }


def _run_news_task(source_id: str, correlation_id: str):
    config = _get_config()
    worker_id = f"api:{uuid4()}"
    started = _start_http_run(config, correlation_id, worker_id, "news", source_id)
    if started is not True:
        if started is False:
            logger.info(
                "news_start_lost", source_id=source_id, correlation_id=correlation_id
            )
        return
    try:
        with maintain_run_heartbeat(config, correlation_id, worker_id):
            result = run_news_source(
                source_id, correlation_id, config, manage_lifecycle=False
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
                run_kind="news",
                component=source_id,
            )
            if finalized:
                logger.info(
                    "news_trigger_completed",
                    source_id=source_id,
                    correlation_id=correlation_id,
                )
    except Exception as exc:
        summary = _news_failure_summary(exc, correlation_id)
        logger.error(
            "news_trigger_failed",
            source_id=source_id,
            correlation_id=correlation_id,
            code=summary["code"],
        )
        finalize_run_safely(
            correlation_id,
            "failed",
            summary,
            config,
            summary["error"],
            worker_id=worker_id,
            run_kind="news",
            component=source_id,
        )


@app.post(
    "/run_news/{source_id}",
    status_code=202,
    response_model=RunAcceptedResponse,
    dependencies=[Depends(require_internal_basic)],
)
def trigger_news(
    source_id: str,
    background_tasks: BackgroundTasks,
    body: dict | None = Body(default=None),
):
    from sources.news_registry import get_news_source_ids

    if source_id not in get_news_source_ids():
        raise HTTPException(status_code=404, detail=f"Unknown news source: {source_id}")
    correlation_id = _correlation_id_from_body(body)
    accepted_at = _accept_http_run(correlation_id, "news", source_id, body)
    background_tasks.add_task(_run_news_task, source_id, correlation_id)
    return {"job_id": correlation_id, "accepted_at": accepted_at.isoformat()}


def _run_processor_task(processor_id: str, correlation_id: str):
    config = _get_config()
    worker_id = f"api:{uuid4()}"
    started = _start_http_run(
        config, correlation_id, worker_id, "processor", processor_id
    )
    if started is not True:
        if started is False:
            logger.info(
                "processor_start_lost",
                processor_id=processor_id,
                correlation_id=correlation_id,
            )
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
                logger.info(
                    "processor_trigger_completed",
                    processor_id=processor_id,
                    correlation_id=correlation_id,
                    result=result,
                )
    except Exception as exc:
        logger.error(
            "processor_trigger_failed",
            processor_id=processor_id,
            correlation_id=correlation_id,
            error=str(exc),
        )
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


@app.post(
    "/run_processor/{processor_id}",
    status_code=202,
    response_model=RunAcceptedResponse,
    dependencies=[Depends(require_internal_basic)],
)
def trigger_processor(
    processor_id: str,
    background_tasks: BackgroundTasks,
    body: dict | None = Body(default=None),
):
    from processors import get_all_processors

    if processor_id not in get_all_processors():
        raise HTTPException(
            status_code=404, detail=f"Unknown processor: {processor_id}"
        )

    correlation_id = _correlation_id_from_body(body)
    accepted_at = _accept_http_run(correlation_id, "processor", processor_id, body)
    background_tasks.add_task(_run_processor_task, processor_id, correlation_id)
    return {"job_id": correlation_id, "accepted_at": accepted_at.isoformat()}


@app.get("/quality", response_model=QualityResponse)
def quality():
    config = _get_config()
    logger.info("quality_endpoint_called")
    results = run_quality_checks(config)
    any_unhealthy = any(not r.get("healthy", True) for r in results.values())
    overall = "degraded" if any_unhealthy else "healthy"
    logger.info("quality_check_complete", overall=overall, check_count=len(results))
    return {"overall": overall, "checks": results}
