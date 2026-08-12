"""Explicit executable role commands for the orchestrator.

Each lifecycle is a separate process with one durable responsibility; the
HTTP API role never owns worker/scheduler/stream singletons.

    python -m roles api          # HTTP API (uvicorn main:app); owns nothing
    python -m roles scheduler    # schedule loop; enqueues durable jobs
    python -m roles worker       # operation worker + analysis job worker
    python -m roles outbox       # event outbox worker
    python -m roles quotes       # quote stream, persisted to quote_state
    python -m roles check ROLE   # one-shot durable liveness check (exit code)

All roles write a ``role_heartbeats`` row while alive; ``check`` and the
``/health`` endpoint read that durable state instead of process memory.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from logging_config import get_logger
from role_heartbeat import (
    ROLES,
    fresh_role_heartbeats,
    heartbeat_is_fresh,
    update_role_heartbeat,
)

logger = get_logger("roles")

# Heartbeats are written every few seconds and considered stale after 12s
# (timeout must stay > 2x the interval so a single missed write cannot flap).
# Both are env-overridable; keep them coherent when overriding.
ROLE_HEARTBEAT_INTERVAL_SECONDS = 5.0
ROLE_HEARTBEAT_TIMEOUT_SECONDS = 12.0

_HEARTBEAT_LOOP_STOP: list[bool] = [False]


def _role_timeout_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get("ROLE_HEARTBEAT_TIMEOUT_SECONDS", "")))
    except (TypeError, ValueError):
        return ROLE_HEARTBEAT_TIMEOUT_SECONDS


def _role_interval_seconds() -> float:
    try:
        return max(0.5, float(os.environ.get("ROLE_HEARTBEAT_INTERVAL_SECONDS", "")))
    except (TypeError, ValueError):
        return ROLE_HEARTBEAT_INTERVAL_SECONDS


def _signal_handlers() -> None:
    def stop(_signum: int, _frame: Any) -> None:
        _HEARTBEAT_LOOP_STOP[0] = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)


def _heartbeat_tick(
    config: dict,
    role: str,
    status_value: str,
    detail: Callable[[], dict[str, Any]] | None,
    started_at: str,
    config_version_value: str | None = None,
) -> None:
    """Write one durable liveness record; failures are logged, never fatal."""
    try:
        current_detail = detail() if detail is not None else {}
        update_role_heartbeat(
            config,
            role,
            status_value,
            {
                **current_detail,
                "pid": _pid(),
                "started_at": started_at,
                "config_version": config_version_value,
            },
        )
    except Exception as exc:
        logger.warning(
            "role_heartbeat_write_failed",
            role=role,
            error_type=type(exc).__name__,
        )


def _heartbeat_loop(
    config: dict,
    role: str,
    status: Callable[[], str] | str = "running",
    detail: Callable[[], dict[str, Any]] | None = None,
    should_exit: Callable[[], bool] | None = None,
) -> None:
    """Write durable role liveness until a signal or exit hook requests stop."""
    from config_loader import config_version

    interval = _role_interval_seconds()
    status_value = status if isinstance(status, str) else "running"
    started_at = datetime.now(UTC).isoformat()
    captured_version = config_version()
    while not _HEARTBEAT_LOOP_STOP[0]:
        if not isinstance(status, str):
            status_value = status()
        _heartbeat_tick(
            config,
            role,
            status_value,
            detail,
            started_at,
            captured_version,
        )
        if should_exit is not None:
            try:
                if should_exit():
                    logger.info("role_exit_requested", role=role, reason="exit hook")
                    _HEARTBEAT_LOOP_STOP[0] = True
                    break
            except Exception as exc:
                logger.warning(
                    "role_exit_hook_failed",
                    role=role,
                    error_type=type(exc).__name__,
                )
        deadline = time.monotonic() + interval
        while not _HEARTBEAT_LOOP_STOP[0] and time.monotonic() < deadline:
            time.sleep(0.2)


def _write_stopped(config: dict, role: str) -> None:
    """Record a graceful-shutdown heartbeat so checks fail fast, not after
    the stale window; best-effort (DB may already be unreachable)."""
    try:
        update_role_heartbeat(
            config,
            role,
            "stopped",
            {"pid": _pid(), "started_at": datetime.now(UTC).isoformat()},
        )
    except Exception as exc:
        logger.warning(
            "role_shutdown_heartbeat_failed",
            role=role,
            error_type=type(exc).__name__,
        )


def _pid() -> int:
    import os

    return os.getpid()


def run_api() -> int:
    """HTTP API role: owns no worker/scheduler/stream singletons."""
    import uvicorn

    from logging_config import setup_logging

    setup_logging(level="INFO")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, log_config=None)
    return 0


def _sleep_interruptibly(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not _HEARTBEAT_LOOP_STOP[0] and time.monotonic() < deadline:
        time.sleep(0.2)


def _config_version_exit_hook(start_version: str) -> Callable[[], bool]:
    """Request a safe-boundary restart on a commit or credential reload failure.

    Ordinary invalid operator candidates are handled inside ``ConfigStore`` by
    returning the last valid snapshot, so their unchanged version keeps the
    role alive. A raised ``ConfigError`` is reserved for fail-closed material
    such as malformed/deleted managed secrets; the role must drain and exit
    rather than continue indefinitely with credentials from its startup
    snapshot.
    """

    def check() -> bool:
        try:
            from config_loader import ConfigError, config_version, load_config

            load_config()
        except ConfigError as exc:
            logger.error(
                "role_config_reload_failed_closed",
                error_type=type(exc).__name__,
            )
            return True
        except Exception:
            return False
        return config_version() != start_version

    return check


def run_scheduler() -> int:
    from config_loader import config_version, load_config
    from scheduler import (
        _try_acquire_leader_connection,
        start_scheduler,
        stop_scheduler,
    )

    config = load_config()
    _signal_handlers()
    leader = None
    should_exit = _config_version_exit_hook(config_version())
    try:
        while not _HEARTBEAT_LOOP_STOP[0]:
            if leader is None:
                try:
                    leader = _try_acquire_leader_connection(config)
                except Exception as exc:
                    logger.warning(
                        "scheduler_leadership_probe_failed",
                        error_type=type(exc).__name__,
                    )
                if leader is None:
                    # Standby: never write the shared scheduler heartbeat (the
                    # row is owned by the lock holder; a standby must not
                    # overwrite a live leader's 'running' row).  Healthchecks
                    # read the canonical leader row; readiness requires an
                    # actual leader.  Keep retrying takeover.
                    if should_exit():
                        _HEARTBEAT_LOOP_STOP[0] = True
                        break
                    _sleep_interruptibly(2.0)
                    continue
            start_scheduler(config)
            _heartbeat_loop(config, "scheduler", should_exit=should_exit)
            stop_scheduler()
            break
    finally:
        # Write 'stopped' while STILL holding the leader lock so a newly
        # acquired leader's subsequent 'running' write is never clobbered by
        # this process's shutdown row.  A standby (never a leader) must not
        # touch the shared scheduler heartbeat at all.
        if leader is not None:
            try:
                _write_stopped(config, "scheduler")
            finally:
                try:
                    leader.close()
                except Exception:
                    pass
    return 0


def _worker_status() -> str:
    from job_worker import job_worker
    from operation_worker import operation_worker

    required = []
    for worker in (operation_worker, job_worker):
        try:
            enabled = worker.enabled()
        except Exception:
            enabled = True
        if enabled:
            required.append(worker.state.get("running", False))
    # One dead required worker must not be masked by a live sibling: the role
    # is running only when EVERY enabled worker thread is alive.
    return "running" if required and all(required) else "stopped"


def _worker_detail() -> dict[str, Any]:
    from job_worker import job_worker
    from operation_worker import operation_worker

    return {
        "worker_id": operation_worker.worker_id,
        "analysis_worker_id": job_worker.worker_id,
        "operation_enabled": bool(
            operation_worker.enabled() if operation_worker.config else False
        ),
        "analysis_enabled": bool(job_worker.enabled() if job_worker.config else False),
        "operation_running": operation_worker.state["running"],
        "analysis_running": job_worker.state["running"],
        "operation_poll_errors": operation_worker.counters["poll_errors"],
        "analysis_poll_errors": job_worker.counters["poll_errors"],
    }


def run_worker() -> int:
    from config_loader import config_version, load_config
    from job_worker import job_worker
    from operation_worker import operation_worker

    config = load_config()
    _signal_handlers()
    should_exit = _config_version_exit_hook(config_version())
    job_worker.start(config)
    operation_worker.start(config)
    try:
        _heartbeat_loop(
            config,
            "worker",
            status=_worker_status,
            detail=_worker_detail,
            should_exit=should_exit,
        )
    finally:
        # Quiesce both claim loops before waiting for either one. A config
        # cutover must not let the sibling claim fresh work while the first
        # worker drains an in-flight provider call.
        job_worker.request_stop()
        operation_worker.request_stop()
        job_worker.wait_stopped()
        operation_worker.wait_stopped()
        _write_stopped(config, "worker")
    return 0


def _outbox_status() -> str:
    from events.worker import outbox_worker

    return "running" if outbox_worker.state.get("running", False) else "stopped"


def _outbox_detail() -> dict[str, Any]:
    from events.worker import outbox_worker

    state = outbox_worker.state
    return {
        "worker_id": state.get("worker_id"),
        "last_error": state.get("last_error"),
        "claimed": state.get("claimed", 0),
        "completed": state.get("completed", 0),
        "retried": state.get("retried", 0),
        "failed": state.get("failed", 0),
    }


def run_outbox() -> int:
    from config_loader import config_version, load_config
    from events.worker import outbox_worker

    config = load_config()
    _signal_handlers()
    should_exit = _config_version_exit_hook(config_version())
    outbox_worker.start(config)
    _heartbeat_loop(
        config,
        "outbox",
        status=_outbox_status,
        detail=_outbox_detail,
        should_exit=should_exit,
    )
    outbox_worker.stop()
    _write_stopped(config, "outbox")
    return 0


def _quote_status() -> str:
    from price_stream import quote_stream

    status = quote_stream.state.get("status", "stopped")
    return (
        status
        if status in {"stopped", "simulated", "connected", "reconnecting", "disabled"}
        else "stopped"
    )


def _quote_detail() -> dict[str, Any]:
    from price_stream import quote_stream

    state = quote_stream.state
    return {
        "last_heartbeat": state.get("last_heartbeat"),
        "error": state.get("error"),
        "quote_count": len(quote_stream.quotes),
    }


def run_quotes() -> int:
    from config_loader import config_version, load_config
    from price_stream import quote_stream

    config = load_config()
    _signal_handlers()
    should_exit = _config_version_exit_hook(config_version())
    quote_stream.start(config)
    _heartbeat_loop(
        config,
        "quotes",
        status=_quote_status,
        detail=_quote_detail,
        should_exit=should_exit,
    )
    quote_stream.stop()
    _write_stopped(config, "quotes")
    return 0


_ROLE_RUNNERS: dict[str, Callable[[], int]] = {
    "api": run_api,
    "scheduler": run_scheduler,
    "worker": run_worker,
    "outbox": run_outbox,
    "quotes": run_quotes,
}

# Roles whose heartbeat status counts as healthy when the role is required.
_HEALTHY_STATUSES: dict[str, frozenset[str]] = {
    "api": frozenset({"running"}),
    "scheduler": frozenset({"running"}),
    "worker": frozenset({"running"}),
    "outbox": frozenset({"running"}),
    "quotes": frozenset({"connected", "simulated"}),
}


def _scheduler_required(config: dict) -> bool:
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
        from sources.news_registry import get_news_source_ids

        for source_id in get_news_source_ids():
            source_config = config.get(source_id, {})
            if (
                source_config.get("enabled", False)
                and source_config.get("schedule_enabled", False)
                and source_config.get("schedule")
            ):
                return True
    filings = config.get("investment_filings", {})
    return bool(filings.get("enabled", False) and filings.get("schedule"))


def _worker_required(config: dict) -> bool:
    settings = (config.get("event_pipeline") or {}).get("jobs") or {}
    return bool(settings.get("enabled", False))


def _outbox_required(config: dict) -> bool:
    pipeline = config.get("event_pipeline") or {}
    return bool(
        pipeline.get("enabled", False) and pipeline.get("outbox_worker_enabled", False)
    )


def _quotes_required(config: dict) -> bool:
    if config.get("demo", {}).get("enabled", False):
        return True
    oanda = (config.get("collectors") or {}).get("oanda") or {}
    return bool(oanda.get("enabled", True) and oanda.get("stream_enabled", False))


_REQUIRED_PREDICATES: dict[str, Callable[[dict], bool]] = {
    "api": lambda config: True,
    "scheduler": _scheduler_required,
    "worker": _worker_required,
    "outbox": _outbox_required,
    "quotes": _quotes_required,
}


def check_role(role: str, timeout_seconds: float | None = None) -> int:
    """One-shot durable liveness check suitable for Compose healthchecks.

    A role that is explicitly optional under the current config passes
    without a heartbeat.  A required role passes only when its heartbeat is
    present, fresh, and reports a healthy status.
    """
    from config_loader import load_config
    from db import check_connection

    if role not in ROLES:
        print(f"unknown role: {role}", file=sys.stderr)
        return 2
    timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else float(_role_timeout_seconds())
    )
    try:
        config = load_config()
    except Exception as exc:
        print(f"config unavailable: {type(exc).__name__}", file=sys.stderr)
        return 1
    if not check_connection(config):
        print("database unreachable", file=sys.stderr)
        return 1
    try:
        if not _REQUIRED_PREDICATES[role](config):
            print(f"role {role} not required by config; healthy")
            return 0
        fresh = fresh_role_heartbeats(
            config, role, timeout=timedelta(seconds=max(1.0, timeout))
        )
    except Exception as exc:
        name = type(exc).__name__
        if "role_heartbeats" in str(exc):
            print(
                "role_heartbeats table missing (migration 046 not applied)",
                file=sys.stderr,
            )
            return 1
        print(f"heartbeat read failed: {name}", file=sys.stderr)
        return 1
    if not fresh:
        print(
            f"role {role} has no fresh heartbeat instance",
            file=sys.stderr,
        )
        return 1
    # Defense in depth: never trust a row whose timestamp lies in the future
    # (clock corruption / tampering), even if the upstream freshness filter
    # already passed it through.
    healthy = [
        heartbeat
        for heartbeat in fresh
        if heartbeat_is_fresh(heartbeat)
        and str(heartbeat.get("status") or "") in _HEALTHY_STATUSES[role]
    ]
    if not healthy:
        statuses = ",".join(f"{h.get('instance_id')}={h.get('status')}" for h in fresh)
        print(
            f"role {role} has no healthy fresh instance ({statuses})",
            file=sys.stderr,
        )
        return 1
    if len(healthy) > 1:
        print(f"role {role} healthy ({len(healthy)} fresh healthy instances)")
    else:
        print(f"role {role} healthy")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="roles", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run one role process")
    run_parser.add_argument("role", choices=sorted(_ROLE_RUNNERS))

    check_parser = subparsers.add_parser("check", help="one-shot role liveness check")
    check_parser.add_argument("role", choices=sorted(ROLES))
    check_parser.add_argument(
        "--stale-after",
        type=float,
        default=None,
        help="staleness window in seconds (default ROLE_HEARTBEAT_TIMEOUT_SECONDS or 12)",
    )

    args = parser.parse_args(argv)
    if args.command == "check":
        return check_role(args.role, args.stale_after)
    return _ROLE_RUNNERS[args.role]()


if __name__ == "__main__":
    sys.exit(main())
