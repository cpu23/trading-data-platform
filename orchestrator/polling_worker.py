"""Shared lifecycle, config, counter, and heartbeat mechanics for polling workers."""

from __future__ import annotations

import inspect
import random
import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from db import get_session


def _factory_accepts_config(factory: Callable[..., Any]) -> bool:
    """Determine once whether the session factory takes a config argument."""
    try:
        parameters = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        return True
    return any(
        parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY)
        for parameter in parameters.values()
    ) or any(
        parameter.kind == inspect.Parameter.VAR_POSITIONAL
        for parameter in parameters.values()
    )


def filter_result_ref(
    value: Any, allowed_keys: Sequence[str] | set[str] | frozenset[str]
) -> dict[str, Any] | None:
    """Extract scalar/nullable allowed fields from a result mapping or object."""
    if isinstance(value, Mapping):
        candidates = value
    else:
        candidates = {
            key: getattr(value, key) for key in allowed_keys if hasattr(value, key)
        }
    result = {
        key: candidates[key]
        for key in allowed_keys
        if key in candidates
        and isinstance(candidates[key], (str, int, float, bool, type(None)))
    }
    return result or None


class BasePollingWorker:
    """Base worker managing polling loop, thread lifecycle, and status counters."""

    _default_id_prefix: str = "worker"
    _enabled_key: str = "enabled"
    _default_poll_seconds: float = 1.0
    _poll_error_interval: float = 1.0

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        worker_id: str | None = None,
        session_factory: Callable[..., Any] | None = None,
        random_source: Callable[[float, float], float] | None = None,
    ) -> None:
        self.config = config
        self.worker_id = worker_id or f"{self._default_id_prefix}:{uuid4()}"
        self._session_factory = session_factory or get_session
        self._session_takes_config = _factory_accepts_config(self._session_factory)
        self._random = random_source or random.uniform
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()
        self._last_poll_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._running = False

    @staticmethod
    def _settings(config: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(config, Mapping) or not isinstance(
            config.get("event_pipeline"), Mapping
        ):
            return {}
        settings = config["event_pipeline"].get("jobs")
        return dict(settings) if isinstance(settings, Mapping) else {}

    def _cfg(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._settings(config if config is not None else self.config)

    @staticmethod
    def _worker_settings(settings: dict[str, Any]) -> dict[str, Any]:
        worker = settings.get("worker")
        return dict(worker) if isinstance(worker, Mapping) else {}

    @staticmethod
    def _batch_size(settings: dict[str, Any], worker: dict[str, Any]) -> int:
        try:
            configured = int(worker.get("batch_size", settings.get("batch_size", 1)))
        except (TypeError, ValueError, OverflowError):
            configured = 1
        return max(1, min(configured, 25))

    def _lease_seconds(self, settings: dict[str, Any]) -> float:
        return max(
            1.0,
            float(
                settings.get(
                    "lease_seconds",
                    self._worker_settings(settings).get("lease_seconds", 120),
                )
            ),
        )

    def enabled(self, config: dict[str, Any] | None = None) -> bool:
        settings = self._cfg(config)
        if not bool(settings.get("enabled", False)):
            return False
        return bool(self._worker_settings(settings).get(self._enabled_key, True))

    def start(self, config: dict[str, Any] | None = None) -> bool:
        if config is not None:
            self.config = config
        if not self.enabled():
            return False
        settings = self._cfg()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            configured_id = self._worker_settings(settings).get("id") or settings.get(
                "worker_id"
            )
            if configured_id:
                # A configured id is a shared LABEL, not an identity: append a
                # process-unique instance uuid so replicas never share
                # claimed_by (a stale replica must never renew or finalize a
                # job another replica reclaimed).
                self.worker_id = f"{configured_id}:{uuid4()}"
            self._stop.clear()
            self._running = True
            self._thread = threading.Thread(
                target=self._run, name=self.worker_id, daemon=True
            )
            self._thread.start()
        return True

    def request_stop(self) -> None:
        """Stop claiming after the current poll/job reaches its boundary."""
        self._stop.set()

    def wait_stopped(self, timeout: float | None = None) -> bool:
        """Wait for an in-flight job to finalize; None means a full drain."""
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            join_timeout = None if timeout is None else max(0.0, float(timeout))
            thread.join(join_timeout)
        stopped = thread is None or not thread.is_alive()
        with self._lock:
            if stopped:
                self._thread = None
                self._running = False
        return stopped

    def stop(self, timeout: float | None = None) -> bool:
        self.request_stop()
        return self.wait_stopped(timeout)

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.poll_once()
                except Exception:
                    self._increment("poll_errors")
                settings = self._cfg()
                worker = self._worker_settings(settings)
                try:
                    interval = max(
                        0.05,
                        float(worker.get("poll_seconds", self._default_poll_seconds)),
                    )
                except (TypeError, ValueError):
                    interval = self._poll_error_interval
                self._stop.wait(interval)
        finally:
            with self._lock:
                self._running = False

    def _increment(self, key: str, value: int = 1) -> None:
        with self._lock:
            self._counts[key] += value
            if key == "succeeded":
                self._last_success_at = datetime.now(UTC)

    @property
    def state(self) -> dict[str, Any]:
        with self._lock:
            counts, running = dict(self._counts), self._running
            poll_at, success_at = self._last_poll_at, self._last_success_at
        return {
            "running": running,
            "disabled": not self.enabled(),
            "worker_id": self.worker_id,
            "last_poll_at": poll_at.isoformat() if poll_at is not None else None,
            "last_success_at": (
                success_at.isoformat() if success_at is not None else None
            ),
            "claimed": counts.get("claimed", 0),
            "completed": counts.get("succeeded", 0),
            "retried": counts.get("retried", 0),
            "failed": counts.get("failed", 0),
            "poll_errors": counts.get("poll_errors", 0),
            "handler_errors": counts.get("handler_errors", 0),
            "reconciled": counts.get("reconciled", 0),
        }

    def state_counters(self) -> dict[str, int]:
        with self._lock:
            counts = dict(self._counts)
        return {
            "claimed": counts.get("claimed", 0),
            "succeeded": counts.get("succeeded", 0),
            "retried": counts.get("retried", 0),
            "failed": counts.get("failed", 0),
            "suppressed": counts.get("suppressed", 0),
            "poll_errors": counts.get("poll_errors", 0),
            "handler_errors": counts.get("handler_errors", 0),
            "reconciled": counts.get("reconciled", 0),
        }

    @property
    def counters(self) -> dict[str, int]:
        return self.state_counters()

    def _session(self) -> Any:
        if self._session_takes_config:
            return self._session_factory(self.config)
        return self._session_factory()

    @staticmethod
    def _heartbeat_interval(lease_seconds: float) -> float:
        return max(0.2, min(30.0, lease_seconds / 3.0))

    def _run_heartbeat(
        self,
        job_id: Any,
        lease_seconds: float,
        stop: threading.Event,
        renew_fn: Callable[[Any, Any, str, float], bool],
    ) -> None:
        interval = self._heartbeat_interval(lease_seconds)
        while not stop.wait(interval):
            try:
                with self._session() as session:
                    if not renew_fn(session, job_id, self.worker_id, lease_seconds):
                        return
            except Exception:
                self._increment("poll_errors")

    def _compute_retry_delay(
        self, attempt_count: int, retry_cfg: Mapping[str, Any]
    ) -> float:
        attempts = max(1, int(attempt_count))
        base = max(0.0, float(retry_cfg.get("base_seconds", 1.0)))
        cap = max(base, float(retry_cfg.get("max_seconds", 300.0)))
        jitter = max(0.0, float(retry_cfg.get("jitter_seconds", 0.25)))
        return min(cap, base * (2 ** max(0, attempts - 1))) + self._random(0.0, jitter)

    def poll_once(self) -> dict[str, int]:
        raise NotImplementedError
