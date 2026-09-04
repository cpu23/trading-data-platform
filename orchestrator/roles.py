"""Explicit executable role commands for the orchestrator.

The runtime collapsed execution to postgres, web, and worker services.
The worker role concurrently runs the scheduler, canonical job worker,
outbox publisher, and quote stream with coherent cancellation and failure handling.

    python -m roles run worker   # worker: scheduler + jobs + outbox + quotes
    python -m roles check worker # one-shot durable liveness check (exit code)

The worker writes a ``role_heartbeats`` row while alive; ``check`` and the
health endpoints read that durable state instead of process memory.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from logging_config import get_logger
from role_heartbeat import (
    ROLES,
    default_instance_id,
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


def _signal_handlers(stop_event: threading.Event | None = None) -> None:
    def stop(_signum: int, _frame: Any) -> None:
        _HEARTBEAT_LOOP_STOP[0] = True
        if stop_event is not None:
            stop_event.set()

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
    stop_event: threading.Event | None = None,
) -> None:
    """Write durable role liveness until a signal or exit hook requests stop."""
    from config_loader import config_version

    interval = _role_interval_seconds()
    status_value = status if isinstance(status, str) else "running"
    started_at = datetime.now(UTC).isoformat()
    captured_version = config_version()
    while not _HEARTBEAT_LOOP_STOP[0] and (
        stop_event is None or not stop_event.is_set()
    ):
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
                    if stop_event is not None:
                        stop_event.set()
                    break
            except Exception as exc:
                logger.warning(
                    "role_exit_hook_failed",
                    role=role,
                    error_type=type(exc).__name__,
                )
        deadline = time.monotonic() + interval
        while (
            not _HEARTBEAT_LOOP_STOP[0]
            and (stop_event is None or not stop_event.is_set())
            and time.monotonic() < deadline
        ):
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
    return os.getpid()


def _sleep_interruptibly(
    seconds: float, stop_event: threading.Event | None = None
) -> None:
    deadline = time.monotonic() + seconds
    while (
        not _HEARTBEAT_LOOP_STOP[0]
        and (stop_event is None or not stop_event.is_set())
        and time.monotonic() < deadline
    ):
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


def _run_scheduler_loop(config: dict, stop_event: threading.Event) -> None:
    """Acquires scheduler leadership and manages the scheduler lifecycle."""
    from scheduler import (
        _try_acquire_leader_connection,
        start_scheduler,
        stop_scheduler,
    )

    leader = None
    try:
        while not stop_event.is_set() and not _HEARTBEAT_LOOP_STOP[0]:
            if leader is None:
                try:
                    leader = _try_acquire_leader_connection(config)
                except Exception as exc:
                    logger.warning(
                        "scheduler_leadership_probe_failed",
                        error_type=type(exc).__name__,
                    )
                if leader is None:
                    _sleep_interruptibly(2.0, stop_event)
                    continue
            start_scheduler(config)
            while not stop_event.is_set() and not _HEARTBEAT_LOOP_STOP[0]:
                _sleep_interruptibly(1.0, stop_event)
            stop_scheduler()
            break
    except Exception as exc:
        logger.error("scheduler_loop_error", error_type=type(exc).__name__)
    finally:
        if leader is not None:
            try:
                leader.close()
            except Exception:
                pass


def run_worker() -> int:
    """Worker role: concurrently runs scheduler, job worker, outbox, and quotes."""
    from config_loader import config_version, load_config
    from demo_live import run_demo_live
    from events.worker import outbox_worker
    from jobs import run_job_worker_forever
    from price_stream import quote_stream
    from scheduler import scheduler_status, stop_scheduler

    config = load_config()
    stop_event = threading.Event()
    _signal_handlers(stop_event)
    should_exit = _config_version_exit_hook(config_version())

    outbox_worker.start(config)
    quote_stream.start(config)

    scheduler_thread = threading.Thread(
        target=_run_scheduler_loop,
        args=(config, stop_event),
        name="worker-scheduler",
        daemon=True,
    )
    scheduler_thread.start()

    jobs_thread = threading.Thread(
        target=run_job_worker_forever,
        args=(config,),
        kwargs={"stop_event": stop_event},
        name="worker-jobs",
        daemon=True,
    )
    jobs_thread.start()
    demo_thread = None
    if bool(config.get("demo", {}).get("enabled")) and os.environ.get(
        "DEMO_MODE", ""
    ).lower() in {"1", "true", "yes"}:
        demo_thread = threading.Thread(
            target=run_demo_live,
            args=(config, stop_event),
            name="worker-demo-live",
            daemon=True,
        )
        demo_thread.start()

    def _worker_status() -> str:
        if stop_event.is_set() or _HEARTBEAT_LOOP_STOP[0]:
            return "stopped"
        if not jobs_thread.is_alive():
            return "stopped"
        if demo_thread is not None and not demo_thread.is_alive():
            return "stopped"
        return "running"

    def _worker_detail() -> dict[str, Any]:
        return {
            "worker_id": default_instance_id(),
            "scheduler": scheduler_status(),
            "outbox": {
                "running": outbox_worker.state.get("running", False),
                "claimed": outbox_worker.state.get("claimed", 0),
                "completed": outbox_worker.state.get("completed", 0),
                "failed": outbox_worker.state.get("failed", 0),
            },
            "quotes": {
                "status": quote_stream.state.get("status", "stopped"),
                "error": quote_stream.state.get("error"),
            },
            "jobs": {
                "running": jobs_thread.is_alive(),
            },
            "demo_live": {
                "running": demo_thread is not None and demo_thread.is_alive(),
            },
        }

    try:
        _heartbeat_loop(
            config,
            "worker",
            status=_worker_status,
            detail=_worker_detail,
            should_exit=should_exit,
            stop_event=stop_event,
        )
    finally:
        stop_event.set()
        _HEARTBEAT_LOOP_STOP[0] = True
        outbox_worker.stop()
        quote_stream.stop()
        stop_scheduler()
        jobs_thread.join(timeout=15.0)
        scheduler_thread.join(timeout=5.0)
        if demo_thread is not None:
            demo_thread.join(timeout=5.0)
        _write_stopped(config, "worker")
    return 0


_ROLE_RUNNERS: dict[str, Callable[[], int]] = {
    "worker": run_worker,
}

_HEALTHY_STATUSES: dict[str, frozenset[str]] = {
    "worker": frozenset({"running"}),
}

_REQUIRED_PREDICATES: dict[str, Callable[[dict], bool]] = {
    "worker": lambda config: True,
}


def check_role(role: str, timeout_seconds: float | None = None) -> int:
    """One-shot durable liveness check suitable for Compose healthchecks."""
    from config_loader import load_config

    from db import check_connection

    if role not in ROLES and role not in _ROLE_RUNNERS:
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
        predicate = _REQUIRED_PREDICATES.get(role, lambda _c: True)
        if not predicate(config):
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

    healthy_statuses = _HEALTHY_STATUSES.get(role, frozenset({"running"}))
    healthy = [
        heartbeat
        for heartbeat in fresh
        if heartbeat_is_fresh(heartbeat)
        and str(heartbeat.get("status") or "") in healthy_statuses
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
    check_parser.add_argument("role", choices=sorted(set(ROLES) | set(_ROLE_RUNNERS)))
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
