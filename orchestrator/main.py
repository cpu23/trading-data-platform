import json
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text

from budgets import BudgetBlock, BudgetExceeded
from config_loader import config_version, load_config
from contracts import (
    CycleStatusResponse,
    InvestmentUrlIngestRequest,
    OrchestratorHealthResponse,
    QualityResponse,
    RunAcceptedResponse,
)
from db import check_connection
from db import get_session as get_session

try:
    from data_quality import (
        DATA_QUALITY_CHECKS,
        evaluate_quality,
        normalize_quality_results,
        readiness_critical_checks,
        required_quality_checks,
        run_quality_checks,
    )
except ImportError:
    DATA_QUALITY_CHECKS = {}

    def run_quality_checks(config):
        return {}

    def normalize_quality_results(results):
        return results

    def required_quality_checks(config):
        return set()

    def readiness_critical_checks(config, required):
        return set()

    def evaluate_quality(results, required):
        return "unknown"


from events.publisher import event_pipeline_summary
from events.repository import operations_summary
from http_client import close_shared_client
from investment_service import (
    MAX_DOCUMENT_BYTES,
    AnalysisInProgress,
    enqueue_investment_analysis,
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
    store_document_path as store_investment_document_path,
)
from investment_service import (
    store_document_url as store_investment_document_url,
)
from llm_client import model_preflight
from logging_config import get_logger, setup_logging
from operation_jobs import (
    accept_and_enqueue_operation,
    latest_cycle_status,
    operation_queue_summary,
)
from orchestrator import (
    RunAcceptanceConflict,
    get_last_collection_runs,
    get_run_for_retry,
)
from price_stream import db_snapshot
from role_heartbeat import (
    fresh_role_heartbeats,
    update_role_heartbeat,
)
from run_lifecycle import accept_run, finalize_run_safely, start_run

app = FastAPI(title="Trading Data Orchestrator")
logger = get_logger("orchestrator.api")
optional_basic = HTTPBasic(auto_error=False)
VALID_CYCLE_MODES = frozenset({"refresh", "analyze", "force_full"})


class _StrictRequest(BaseModel):
    """Strict durable-acceptance bodies: no coercion, no unknown fields."""

    model_config = ConfigDict(extra="forbid")

    @field_validator("idempotency_key", check_fields=False)
    @classmethod
    def _idempotency_key_nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("idempotency_key must be a nonblank string")
        return value


class CycleRequest(_StrictRequest):
    correlation_id: UUID | None = None
    idempotency_key: Annotated[
        str | None, Field(min_length=1, max_length=128, strict=True)
    ] = None
    mode: Literal["refresh", "analyze", "force_full"] = "refresh"
    budget_confirmed: Annotated[bool, Field(strict=True)] = False


class RunRequest(_StrictRequest):
    correlation_id: UUID | None = None
    idempotency_key: Annotated[
        str | None, Field(min_length=1, max_length=128, strict=True)
    ] = None


class FilingsRequest(_StrictRequest):
    correlation_id: UUID | None = None
    idempotency_key: Annotated[
        str | None, Field(min_length=1, max_length=128, strict=True)
    ] = None
    auto_analyze: Annotated[bool, Field(strict=True)] = False

# The API role owns no worker/scheduler/stream singletons; it only records its
# own durable liveness so Compose and /health can observe the role.  Cadence
# stays coherent with role_heartbeat.DEFAULT_HEARTBEAT_TIMEOUT (12s).
_API_HEARTBEAT_INTERVAL_SECONDS = 5.0
_api_heartbeat_stop = threading.Event()


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


def _api_heartbeat_loop(config: dict) -> None:
    import os

    captured_version = config_version()
    while not _api_heartbeat_stop.wait(_API_HEARTBEAT_INTERVAL_SECONDS):
        try:
            load_config()
        except Exception:
            # A rejected reload retains the prior snapshot; keep serving it
            # until the operator repairs or restarts.
            pass
        if config_version() != captured_version:
            # Committed configuration changed: this process still runs the
            # previous snapshot (scheduler/workers capture config at
            # startup).  Exit cleanly so Compose restarts the role with the
            # new config — never dispatch stale credentials indefinitely.
            logger.info(
                "config_version_changed_restarting",
                previous=captured_version,
                current=config_version(),
            )
            os._exit(0)
        try:
            update_role_heartbeat(
                config,
                "api",
                "running",
                {
                    "pid": os.getpid(),
                    "host": os.environ.get("HOSTNAME", ""),
                    "config_version": config_version(),
                },
            )
        except Exception:
            # A heartbeat write failure must not affect request handling.
            continue


@app.on_event("startup")
def on_startup():
    config = _get_config()
    setup_logging(level=config.get("logging", {}).get("level", "INFO"))

    if not check_connection(config):
        raise RuntimeError("Database connection failed")
    _api_heartbeat_stop.clear()
    threading.Thread(
        target=_api_heartbeat_loop,
        args=(config,),
        name="api-role-heartbeat",
        daemon=True,
    ).start()
    logger.info("orchestrator_http_started", action="startup")


@app.on_event("shutdown")
def on_shutdown():
    _api_heartbeat_stop.set()
    try:
        update_role_heartbeat(
            _get_config(),
            "api",
            "stopped",
            {"started_at": datetime.now(UTC).isoformat()},
        )
    except Exception:
        # Shutdown must never fail because the DB is already unreachable.
        pass
    close_shared_client()


def _durable_scheduler_snapshot(config: dict) -> dict:
    """Durable scheduler status: a fresh running leader plus job ids."""
    try:
        leader_instances = [
            heartbeat
            for heartbeat in fresh_role_heartbeats(config, "scheduler")
            if heartbeat.get("status") == "running"
        ]
    except Exception as exc:
        # Health must stay answerable while the database is unavailable:
        # without durable heartbeats no leader can be verified running, so
        # report the scheduler as stopped instead of failing the whole probe.
        logger.warning(
            "health_scheduler_snapshot_unavailable",
            error_type=type(exc).__name__,
        )
        leader_instances = []
    jobs = []
    try:
        for source_id, source_config in config.get("collectors", {}).items():
            schedule = source_config.get("schedule")
            if (
                source_config.get("enabled", True)
                and schedule
                and schedule != "after_dependency"
            ):
                jobs.append({"id": f"collector:{source_id}", "next_due_at": None})
        for processor_id, processor_config in config.get("processors", {}).items():
            schedule = processor_config.get("schedule")
            if (
                processor_config.get("enabled", False)
                and schedule
                and schedule != "after_dependency"
            ):
                jobs.append({"id": f"processor:{processor_id}", "next_due_at": None})
        research_config = config.get("research_intelligence", {})
        if (
            research_config.get("enabled", False)
            and research_config.get("schedule_enabled", False)
            and research_config.get("schedule")
        ):
            jobs.append({"id": "research:discovery", "next_due_at": None})
        if not config.get("demo", {}).get("enabled", False):
            from sources.news_registry import get_news_source_ids

            for source_id in get_news_source_ids():
                source_config = config.get(source_id, {})
                if (
                    source_config.get("enabled", False)
                    and source_config.get("schedule_enabled", False)
                    and source_config.get("schedule")
                ):
                    jobs.append({"id": f"news:{source_id}", "next_due_at": None})
        filings_config = config.get("investment_filings", {})
        if filings_config.get("enabled", False) and filings_config.get("schedule"):
            jobs.append({"id": "filings:investment_filings", "next_due_at": None})
    except Exception:
        jobs = []
    return {
        "status": "running" if leader_instances else "stopped",
        "jobs": jobs,
        "checked_at": datetime.now(UTC).isoformat(),
    }


def _durable_stream_snapshot(config: dict) -> dict:
    """Durable quote-stream status from the freshest quotes instance."""
    try:
        fresh = fresh_role_heartbeats(config, "quotes")
    except Exception as exc:
        # The database (or its configuration) is unavailable; without durable
        # heartbeats the stream status cannot be observed, so report stopped
        # rather than failing the health probe.
        logger.warning(
            "health_stream_snapshot_unavailable",
            error_type=type(exc).__name__,
        )
        fresh = []
    if not fresh:
        return {"status": "stopped", "last_heartbeat": None, "error": None}
    heartbeat = fresh[0]
    last = heartbeat.get("last_heartbeat_at")
    return {
        "status": heartbeat.get("status", "stopped"),
        "last_heartbeat": last.isoformat() if hasattr(last, "isoformat") else last,
        "error": (heartbeat.get("detail") or {}).get("error"),
    }


#: Statuses that count as healthy per role; freshness alone is not sufficient.
_ROLE_HEALTHY_STATUS: dict[str, frozenset[str]] = {
    "api": frozenset({"running"}),
    "scheduler": frozenset({"running"}),
    "worker": frozenset({"running"}),
    "outbox": frozenset({"running"}),
    "quotes": frozenset({"connected", "simulated"}),
}
_ROLE_ORDER = ("api", "scheduler", "worker", "outbox", "quotes")


def _has_scheduled_jobs(config: dict) -> bool:
    """True when the configuration schedules at least one job."""
    for _source_id, source_config in config.get("collectors", {}).items():
        schedule = source_config.get("schedule")
        if (
            source_config.get("enabled", True)
            and schedule
            and schedule != "after_dependency"
        ):
            return True
    for _processor_id, processor_config in config.get("processors", {}).items():
        schedule = processor_config.get("schedule")
        if (
            processor_config.get("enabled", False)
            and schedule
            and schedule != "after_dependency"
        ):
            return True
    research = config.get("research_intelligence", {})
    if (
        research.get("enabled", False)
        and research.get("schedule_enabled", False)
        and research.get("schedule")
    ):
        return True
    if not config.get("demo", {}).get("enabled", False):
        try:
            from sources.news_registry import get_news_source_ids

            for source_id in get_news_source_ids():
                source_config = config.get(source_id, {})
                if (
                    source_config.get("enabled", False)
                    and source_config.get("schedule_enabled", False)
                    and source_config.get("schedule")
                ):
                    return True
        except Exception:
            pass
    filings = config.get("investment_filings", {})
    return bool(filings.get("enabled", False) and filings.get("schedule"))


def _required_role_dependencies(config: dict) -> dict[str, bool]:
    """Map each runtime role to whether it is required for readiness.

    Mirrors the durable-role predicates: a role is required only when the
    configuration expects it to be live (schedule exists, worker/outbox
    enabled, live quotes stream expected).  Optional roles never fail
    readiness.
    """
    required: dict[str, bool] = {"api": True}
    required["scheduler"] = _has_scheduled_jobs(config)
    pipeline = config.get("event_pipeline", {})
    pipeline = pipeline if isinstance(pipeline, Mapping) else {}
    jobs_cfg = pipeline.get("jobs", {})
    jobs_cfg = jobs_cfg if isinstance(jobs_cfg, Mapping) else {}
    required["worker"] = bool(jobs_cfg.get("enabled", True))
    required["outbox"] = bool(
        pipeline.get("enabled", True) and pipeline.get("outbox_worker_enabled", True)
    )
    oanda = config.get("collectors", {}).get("oanda", {})
    oanda = oanda if isinstance(oanda, Mapping) else {}
    demo_enabled = bool(config.get("demo", {}).get("enabled", False))
    required["quotes"] = demo_enabled or bool(
        oanda.get("enabled", True) and oanda.get("stream_enabled", True)
    )
    return required


def _role_unhealthy_reason(heartbeat: dict | None) -> str:
    if heartbeat is None:
        return "no heartbeat"
    return f"unhealthy status {heartbeat.get('status', 'unknown')}"


def _role_readiness(config: dict) -> tuple[list[dict], bool]:
    """Evaluate required role heartbeats; returns (components, ready).

    A required role with a missing, stale, or unhealthy-status heartbeat
    makes the service unready.  Optional roles appear only when they have a
    heartbeat (informational) and never block readiness.
    """
    required = _required_role_dependencies(config)
    components: list[dict] = []
    ready = True
    active_version = config_version()
    for role in _ROLE_ORDER:
        is_required = required.get(role, False)
        try:
            heartbeats = fresh_role_heartbeats(config, role)
            heartbeat = heartbeats[0] if heartbeats else None
        except Exception as exc:
            logger.warning(
                "role_heartbeat_read_failed", role=role, error_type=type(exc).__name__
            )
            heartbeat = None
        healthy_status = (
            heartbeat is not None
            and str(heartbeat.get("status", "")).strip()
            in _ROLE_HEALTHY_STATUS.get(role, frozenset())
        )
        role_version = (
            (heartbeat.get("detail") or {}).get("config_version")
            if heartbeat is not None
            else None
        )
        version_mismatch = bool(
            role_version is not None
            and active_version is not None
            and role_version != active_version
        )
        # fresh_role_heartbeats() returns only fresh (non-stale, non-future)
        # rows, so freshness is guaranteed by the contract.
        healthy = healthy_status and not version_mismatch
        component: dict = {
            "name": f"role:{role}",
            "kind": "service",
            "status": "available" if healthy else "degraded",
            "reason": None if healthy else _role_unhealthy_reason(heartbeat),
        }
        if role_version is not None:
            component["config_version"] = role_version
        if version_mismatch:
            component["restart_required"] = True
            component["reason"] = (
                f"config version mismatch: role runs {role_version}, "
                f"active {active_version}; restart required"
            )
        if not is_required:
            if heartbeat is None:
                continue
            component["critical"] = False
            components.append(component)
            continue
        if not healthy:
            ready = False
        component["critical"] = True
        component["status"] = "available" if healthy else "unavailable"
        components.append(component)
    return components, ready


@app.get("/live")
def live():
    """Process liveness: always 200 while the process can serve requests."""
    return {"status": "ok"}


@app.get("/ready")
def ready():
    """Dependency-aware readiness: 503 until required dependencies answer.

    Required dependencies are the database, every runtime role the
    configuration expects to be live (scheduler/worker/outbox/quotes/api),
    and — when quality checks are configured required — a healthy quality
    verdict.  A required role that is missing, stale, unhealthy, or running a
    different config version than this process means unready; the bounded
    quality snapshot cache is reused so readiness never triggers a fresh
    quality sweep per call.
    """
    config = _get_config()
    dependencies: dict[str, Any] = {"database": "unavailable"}
    if check_connection(config):
        dependencies["database"] = "ok"
        role_components, roles_ready = _role_readiness(config)
        dependencies["roles"] = {
            "required": sorted(
                component["name"].split(":", 1)[-1]
                for component in role_components
                if component.get("critical")
            ),
            "unhealthy": sorted(
                component["name"].split(":", 1)[-1]
                for component in role_components
                if component["status"] != "available"
            ),
            "version_mismatch": any(
                component.get("restart_required") for component in role_components
            ),
        }
        required = required_quality_checks(config)
        critical = readiness_critical_checks(config, required)
        try:
            quality_results = _health_quality_snapshot(config)
            quality_overall = evaluate_quality(quality_results, required)
            critical_failing = {
                check_id
                for check_id in critical
                if check_id not in quality_results
                or quality_results[check_id].get("healthy") is not True
            }
        except Exception as exc:
            logger.warning(
                "ready_quality_unavailable",
                error_type=type(exc).__name__,
            )
            quality_overall = "unknown"
            critical_failing = set(critical)
        dependencies["quality"] = {
            "overall": quality_overall,
            "required": bool(required),
            "readiness_critical": sorted(critical),
        }
        quality_ready = not critical_failing
        if roles_ready and quality_ready:
            return {
                "status": "ready",
                "dependencies": dependencies,
                "config_version": config_version(),
            }
    return JSONResponse(
        status_code=503,
        content={
            "status": "unready",
            "dependencies": dependencies,
            "config_version": config_version(),
        },
    )


@app.get("/health", response_model=OrchestratorHealthResponse)
def health():
    config = _get_config()
    scheduler = _durable_scheduler_snapshot(config)
    stream = _durable_stream_snapshot(config)
    if not check_connection(config):
        payload = {
            "liveness": "ok",
            "readiness": "unready",
            "data_health": "degraded",
            "status": "degraded",
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
            # Persisted error text may embed secrets; the health surface
            # exposes only a bounded label, never the message text
            # (details remain in collection_log and the logs).
            "error_message": "Error" if run.get("error_message") else None,
        }

    role_components, roles_ready = _role_readiness(config)
    if not roles_ready:
        payload = {
            "liveness": "ok",
            "readiness": "unready",
            "data_health": "degraded",
            "status": "degraded",
            "components": [
                {
                    "name": "database",
                    "kind": "service",
                    "critical": True,
                    "status": "available",
                    "reason": None,
                },
                *role_components,
            ],
            "scheduler": scheduler,
            "stream": stream,
            "collectors": {},
        }
        return JSONResponse(status_code=503, content=payload)

    components = [
        {
            "name": "database",
            "kind": "service",
            "critical": True,
            "status": "available",
            "reason": None,
        },
        *role_components,
    ]
    try:
        quality_results = _health_quality_snapshot(config)
    except Exception as exc:
        logger.error(
            "health_quality_checks_failed", error_type=type(exc).__name__
        )
        quality_results = {
            "quality_runner": {
                "healthy": False,
                "detail": type(exc).__name__,
            }
        }
    required = required_quality_checks(config)
    critical = readiness_critical_checks(config, required)
    quality_overall = evaluate_quality(quality_results, required)
    missing = required - set(quality_results)
    malformed = {
        check_id
        for check_id in required
        if check_id in quality_results
        and quality_results[check_id].get("healthy") is not True
        and quality_results[check_id].get("healthy") is not False
    }
    unhealthy = {
        check_id: result
        for check_id, result in quality_results.items()
        if result.get("healthy") is False
    }
    if unhealthy or missing or malformed:
        reasons = [
            f"{check_id}: {result.get('detail', 'unhealthy')}"
            for check_id, result in unhealthy.items()
        ]
        reasons.extend(
            f"{check_id}: required check missing" for check_id in sorted(missing)
        )
        reasons.extend(
            f"{check_id}: malformed result" for check_id in sorted(malformed)
        )
        components.append(
            {
                "name": "data_quality",
                "kind": "data",
                "critical": bool(critical),
                "status": (
                    "unavailable"
                    if (missing or malformed or quality_overall == "unknown")
                    else "degraded"
                ),
                "reason": "; ".join(reasons) or "quality cannot be assessed",
            }
        )

    quality_payload = {
        "overall": quality_overall,
        "checks": normalize_quality_results(quality_results),
    }

    critical_failing = {
        check_id
        for check_id in critical
        if check_id not in quality_results
        or quality_results[check_id].get("healthy") is not True
    }
    if critical_failing:
        payload = {
            "liveness": "ok",
            "readiness": "unready",
            "data_health": quality_overall,
            "status": quality_overall,
            "components": components,
            "scheduler": scheduler,
            "stream": stream,
            "collectors": collectors_status,
            "quality": quality_payload,
            "config_version": config_version(),
        }
        return JSONResponse(status_code=503, content=payload)

    data_health = quality_overall
    return {
        "liveness": "ok",
        "readiness": "ready",
        "data_health": data_health,
        "status": quality_overall,
        "components": components,
        "scheduler": scheduler,
        "stream": stream,
        "collectors": collectors_status,
        "quality": quality_payload,
        "config_version": config_version(),
    }


@app.get("/quotes")
def quotes():
    return db_snapshot(_get_config())


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
    config = _get_config()
    worker_state = {"running": False, "worker_id": None}
    try:
        fresh = fresh_role_heartbeats(config, "outbox")
        running_instances = [
            heartbeat for heartbeat in fresh if heartbeat.get("status") == "running"
        ]
        if running_instances:
            detail = running_instances[0].get("detail") or {}
            worker_state = {
                "running": True,
                "worker_id": detail.get("worker_id"),
                "instances": len(running_instances),
                "last_poll_at": _iso(running_instances[0].get("last_heartbeat_at")),
                "last_success_at": _iso(
                    running_instances[0].get("last_heartbeat_at")
                ),
                "last_error": detail.get("last_error"),
            }
        with get_session(config) as session:
            ops = operations_summary(session)
        worker_state.update(
            {
                "claimed": ops.get("claimed", 0),
                "completed": ops.get("completed", 0),
                "retried": ops.get("retried", 0),
                "failed": ops.get("failed", 0),
            }
        )
    except Exception:
        worker_state = {"running": False, "worker_id": None}
    return {
        **event_pipeline_summary(config),
        "worker": worker_state,
    }


def _iso(value) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else value


@app.get(
    "/jobs/status",
    dependencies=[Depends(require_internal_basic)],
)
def analysis_jobs_status():
    """Return durable queue state; process-global counters are gone."""
    config = _get_config()
    fresh = fresh_role_heartbeats(config, "worker")
    running_instances = [
        heartbeat for heartbeat in fresh if heartbeat.get("status") == "running"
    ]
    running = bool(running_instances)
    detail = (running_instances[0] if running_instances else {}).get("detail") or {}
    settings = (config.get("event_pipeline") or {}).get("jobs") or {}
    enabled = bool(settings.get("enabled", False))
    counts: dict[str, int] = {}
    try:
        summary = operation_queue_summary(config)
        for state, count in summary.get("counts", {}).items():
            counts[state] = counts.get(state, 0) + int(count)
        with get_session(config) as session:
            rows = session.execute(
                text("SELECT state, COUNT(*) AS count FROM analysis_jobs GROUP BY state")
            ).mappings()
            for row in rows:
                counts[str(row["state"])] = (
                    counts.get(str(row["state"]), 0) + int(row["count"])
                )
    except Exception:
        counts = {}
    counters = {
        "claimed": counts.get("leased", 0) + counts.get("running", 0),
        "succeeded": counts.get("succeeded", 0),
        "retried": counts.get("failed_retryable", 0),
        "failed": counts.get("failed_terminal", 0),
        "suppressed": counts.get("suppressed_duplicate", 0),
        "poll_errors": 0,
        "handler_errors": 0,
        "reconciled": 0,
    }
    return {
        "running": running,
        "enabled": enabled,
        "worker_id": str(detail.get("worker_id", "") or ""),
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


def _fallback_enqueue_thesis_autonomy(
    config: dict[str, Any],
    *,
    triggered_by: str,
    force: bool,
    request_nonce: str | None = None,
) -> dict[str, Any]:
    """Queue one durable thesis-autonomy run through the analysis queue.

    Temporary stand-in used until ``thesis_autonomy`` lands: mirrors
    ``research_intelligence.operations.enqueue_research_job`` with the same
    job identity the sibling helper will use (job type
    ``thesis_autonomy_run``, dedupe key ``thesis-autonomy:global``, and a
    request-date + nonce fingerprint), so both paths coalesce on the one
    durable job and never double-enqueue.
    """
    from analysis_jobs import enqueue_job
    from db import get_session
    from research_intelligence.contracts import canonical_fingerprint

    correlation_id = str(uuid4())
    accepted_at = accept_run(
        config,
        correlation_id,
        triggered_by,
        "research",
        "thesis_autonomy",
        request_summary={
            "job_type": "thesis_autonomy_run",
            "force": bool(force),
        },
    )
    worker_id = f"thesis-autonomy-enqueue:{uuid4()}"
    try:
        started = start_run(config, correlation_id, worker_id)
    except Exception:
        finalize_run_safely(
            correlation_id,
            "failed",
            {"status": "failed", "reason": "thesis autonomy run start unavailable"},
            config,
            "thesis autonomy run start unavailable",
            run_kind="research",
            component="thesis_autonomy",
        )
        raise
    if not started:
        raise RuntimeError("accepted thesis autonomy run could not be claimed")
    try:
        identity = {
            "job_type": "thesis_autonomy_run",
            "request_date": datetime.now(UTC).date().isoformat(),
            "request_nonce": request_nonce or (correlation_id if force else None),
        }
        input_fingerprint = canonical_fingerprint(identity)
        with get_session(config) as session:
            enqueued = enqueue_job(
                session,
                job_type="thesis_autonomy_run",
                dedupe_key="thesis-autonomy:global",
                input_fingerprint=input_fingerprint,
                payload={"force": bool(force)},
                correlation_id=correlation_id,
                priority=90 if force else 80,
                max_attempts=3,
            )
        job = enqueued.job
        result = {
            "status": "queued" if enqueued.inserted else "already_queued",
            "job_id": str(job.id) if job is not None else None,
            "correlation_id": (
                str(job.correlation_id) if job is not None else correlation_id
            ),
            "accepted_at": accepted_at.isoformat(),
            "inserted": enqueued.inserted,
            "force": bool(force),
        }
        finalize_run_safely(
            correlation_id,
            "success",
            result,
            config,
            None,
            worker_id=worker_id,
            run_kind="research",
            component="thesis_autonomy",
        )
        return result
    except Exception:
        finalize_run_safely(
            correlation_id,
            "failed",
            {},
            config,
            "thesis autonomy enqueue failed",
            worker_id=worker_id,
            run_kind="research",
            component="thesis_autonomy",
        )
        raise


def _enqueue_thesis_autonomy(
    config: dict[str, Any],
    *,
    triggered_by: str = "api",
    force: bool = False,
    request_nonce: str | None = None,
) -> dict[str, Any]:
    """Queue one durable thesis-autonomy run (``thesis_autonomy_run``).

    Prefers the sibling ``thesis_autonomy`` helper once deployed; until it
    is importable, falls back to the established analysis queue with the
    same job identity so the durable job is never duplicated.
    """
    try:
        from thesis_autonomy import enqueue_thesis_autonomy_job
    except ImportError:
        return _fallback_enqueue_thesis_autonomy(
            config,
            triggered_by=triggered_by,
            force=force,
            request_nonce=request_nonce,
        )
    return enqueue_thesis_autonomy_job(
        config,
        triggered_by=triggered_by,
        force=force,
        request_nonce=request_nonce,
    )


@app.post(
    "/research/theses/run",
    status_code=202,
    dependencies=[Depends(require_internal_basic)],
)
def trigger_thesis_autonomy(body: dict | None = Body(default=None)):
    """Queue one durable thesis-autonomy run immediately."""
    force = _research_force(body)
    try:
        return _enqueue_thesis_autonomy(
            _get_config(), force=force, triggered_by="api"
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "thesis_autonomy_enqueue_failed", error_type=type(exc).__name__
        )
        raise HTTPException(
            status_code=503, detail="Thesis run could not be queued"
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
    fd, spool_path = tempfile.mkstemp(prefix="investment-ingest-", suffix=".bin")
    try:
        try:
            with os.fdopen(fd, "wb") as handle:
                _reject_declared_oversize(request, MAX_DOCUMENT_BYTES)
                total = 0
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > MAX_DOCUMENT_BYTES:
                        raise ValueError(
                            f"document exceeds {MAX_DOCUMENT_BYTES // 1_000_000} MB"
                        )
                    handle.write(chunk)
                handle.flush()
        except ValueError as exc:
            if str(exc).startswith("document exceeds"):
                raise HTTPException(status_code=413, detail=str(exc)) from exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            result = store_investment_document_path(
                _get_config(),
                metadata,
                spool_path,
                request.headers.get("content-type"),
                extract=False,
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
    finally:
        try:
            os.unlink(spool_path)
        except FileNotFoundError:
            pass
    if _wants_analysis(metadata):
        # Work was requested: the ingest only succeeds once the durable
        # handoff is queued, so a failure is observable instead of silently
        # storing a document that will never be analyzed.
        try:
            result = {
                **result,
                "analysis": enqueue_investment_analysis(
                    _get_config(),
                    str(result["document_id"]),
                ),
            }
        except Exception as exc:
            logger.error(
                "investment_analysis_enqueue_failed",
                document_id=str(result.get("document_id") or ""),
                error_type=type(exc).__name__,
            )
            raise HTTPException(
                status_code=503,
                detail="Investment analysis could not be scheduled",
            ) from exc
    return result


@app.post(
    "/investment/urls",
    status_code=201,
    dependencies=[Depends(require_internal_basic)],
)
def ingest_investment_url(body: InvestmentUrlIngestRequest = Body(...)):
    payload = body.model_dump(exclude_none=True)
    try:
        result = store_investment_document_url(_get_config(), payload)
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
    if _wants_analysis(payload):
        try:
            result = {
                **result,
                "analysis": enqueue_investment_analysis(
                    _get_config(),
                    str(result["document_id"]),
                ),
            }
        except Exception as exc:
            logger.error(
                "investment_analysis_enqueue_failed",
                document_id=str(result.get("document_id") or ""),
                error_type=type(exc).__name__,
            )
            raise HTTPException(
                status_code=503,
                detail="Investment analysis could not be scheduled",
            ) from exc
    return result


def _reject_declared_oversize(request: Request, max_bytes: int) -> None:
    declared = request.headers.get("content-length")
    if not declared:
        return
    try:
        declared_size = int(declared)
    except ValueError:
        raise ValueError("invalid Content-Length") from None
    if declared_size > max_bytes:
        raise ValueError(f"document exceeds {max_bytes // 1_000_000} MB")


def _wants_analysis(metadata: dict | None) -> bool:
    if not isinstance(metadata, dict):
        return False
    value = metadata.get("analyze")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes"}


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
    body: FilingsRequest | None = Body(default=None),
):
    request = FilingsRequest.model_validate(body) if body is not None else FilingsRequest()
    correlation_id = (
        str(request.correlation_id) if request.correlation_id else str(uuid4())
    )
    accepted_at, _ = _accept_and_enqueue(
        correlation_id,
        "filings",
        "investment_filings",
        idempotency_key=request.idempotency_key,
        request_summary={"auto_analyze": request.auto_analyze},
        payload={"auto_analyze": request.auto_analyze},
        max_attempts=3,
    )
    job_id = correlation_id
    return {"job_id": job_id, "accepted_at": accepted_at.isoformat()}


def _accept_and_enqueue(
    correlation_id: str,
    run_kind: str,
    requested_component: str | None,
    *,
    idempotency_key: str | None = None,
    request_summary: dict | None = None,
    payload: dict | None = None,
    dedupe_key: str | None = None,
    input_fingerprint: str | None = None,
    priority: int = 100,
    max_attempts: int = 3,
    triggered_by: str = "api",
):
    """Accept a durable run and enqueue its operation job atomically.

    A duplicate logical identity suppresses the enqueue (the accepted row is
    finalized as already_queued inside the same transaction); the response
    still reports the run as accepted for API compatibility.
    """
    try:
        return accept_and_enqueue_operation(
            _get_config(),
            correlation_id=correlation_id,
            triggered_by=triggered_by,
            run_kind=run_kind,
            requested_component=requested_component,
            idempotency_key=idempotency_key,
            request_summary=request_summary,
            dedupe_key=dedupe_key,
            input_fingerprint=input_fingerprint,
            payload=payload,
            priority=priority,
            max_attempts=max_attempts,
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
    retry_payload = None
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
        retry_payload = {"mode": cycle_mode, "budget_confirmed": False}
    elif run_kind == "collector":
        retry_payload = {"run_dependents": False}
    else:
        retry_payload = {}

    accepted_at, _ = _accept_and_enqueue(
        new_correlation_id,
        run_kind,
        str(component) if component else None,
        request_summary=retry_summary,
        payload=retry_payload,
        max_attempts=3,
        triggered_by="retry",
    )
    job_id = new_correlation_id

    return {
        "job_id": job_id,
        "prior_job_id": correlation_id_str,
        "accepted_at": accepted_at.isoformat(),
    }


@app.post(
    "/run_cycle",
    status_code=202,
    response_model=RunAcceptedResponse,
    dependencies=[Depends(require_internal_basic)],
)
def trigger_cycle(
    body: CycleRequest | None = Body(default=None),
    credentials: HTTPBasicCredentials | None = Depends(optional_basic),
):
    request = (
        CycleRequest.model_validate(body) if body is not None else CycleRequest()
    )
    mode = request.mode

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
        if request.budget_confirmed is not True:
            raise HTTPException(
                status_code=422,
                detail="force_full requires explicit budget confirmation",
            )

    request_summary = {
        "mode": mode,
        "budget_confirmed": mode == "force_full",
    }
    correlation_id = (
        str(request.correlation_id) if request.correlation_id else str(uuid4())
    )
    accepted_at, _ = _accept_and_enqueue(
        correlation_id,
        "cycle",
        None,
        idempotency_key=request.idempotency_key,
        request_summary=request_summary,
        payload={"mode": mode},
        max_attempts=2,
    )
    job_id = correlation_id

    return {"job_id": job_id, "accepted_at": accepted_at.isoformat()}


@app.get("/cycle_status", response_model=CycleStatusResponse)
def get_cycle_status():
    return latest_cycle_status(_get_config())


@app.post(
    "/run_collector/{source_id}",
    status_code=202,
    response_model=RunAcceptedResponse,
    dependencies=[Depends(require_internal_basic)],
)
def trigger_collector(
    source_id: str,
    body: RunRequest | None = Body(default=None),
):
    from collectors import get_all_collectors

    if source_id not in get_all_collectors():
        raise HTTPException(status_code=404, detail=f"Unknown collector: {source_id}")
    request = RunRequest.model_validate(body) if body is not None else RunRequest()
    correlation_id = (
        str(request.correlation_id) if request.correlation_id else str(uuid4())
    )
    accepted_at, _ = _accept_and_enqueue(
        correlation_id,
        "collector",
        source_id,
        idempotency_key=request.idempotency_key,
        request_summary={"mode": "refresh", "run_dependents": False},
        payload={"mode": "refresh", "run_dependents": False},
        max_attempts=3,
    )
    job_id = correlation_id
    return {"job_id": job_id, "accepted_at": accepted_at.isoformat()}


@app.post(
    "/run_news/{source_id}",
    status_code=202,
    response_model=RunAcceptedResponse,
    dependencies=[Depends(require_internal_basic)],
)
def trigger_news(
    source_id: str,
    body: RunRequest | None = Body(default=None),
):
    from sources.news_registry import get_news_source_ids

    if source_id not in get_news_source_ids():
        raise HTTPException(status_code=404, detail=f"Unknown news source: {source_id}")
    request = RunRequest.model_validate(body) if body is not None else RunRequest()
    correlation_id = (
        str(request.correlation_id) if request.correlation_id else str(uuid4())
    )
    accepted_at, _ = _accept_and_enqueue(
        correlation_id,
        "news",
        source_id,
        idempotency_key=request.idempotency_key,
        request_summary={"mode": "refresh"},
        payload={"mode": "refresh"},
        max_attempts=3,
    )
    job_id = correlation_id
    return {"job_id": job_id, "accepted_at": accepted_at.isoformat()}


@app.post(
    "/run_processor/{processor_id}",
    status_code=202,
    response_model=RunAcceptedResponse,
    dependencies=[Depends(require_internal_basic)],
)
def trigger_processor(
    processor_id: str,
    body: RunRequest | None = Body(default=None),
):
    from processors import get_all_processors

    if processor_id not in get_all_processors():
        raise HTTPException(
            status_code=404, detail=f"Unknown processor: {processor_id}"
        )
    request = RunRequest.model_validate(body) if body is not None else RunRequest()
    correlation_id = (
        str(request.correlation_id) if request.correlation_id else str(uuid4())
    )
    accepted_at, _ = _accept_and_enqueue(
        correlation_id,
        "processor",
        processor_id,
        idempotency_key=request.idempotency_key,
        request_summary={"mode": "refresh"},
        payload={"mode": "refresh"},
        max_attempts=3,
    )
    job_id = correlation_id
    return {"job_id": job_id, "accepted_at": accepted_at.isoformat()}


@app.get("/quality", response_model=QualityResponse)
def quality():
    config = _get_config()
    logger.info("quality_endpoint_called")
    results = run_quality_checks(config)
    overall = evaluate_quality(results, required_quality_checks(config))
    logger.info("quality_check_complete", overall=overall, check_count=len(results))
    return {"overall": overall, "checks": normalize_quality_results(results)}
