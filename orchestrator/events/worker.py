"""Bounded daemon worker for the transactional market-event outbox."""

from __future__ import annotations

import random
import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from db import get_session

from .contracts import MarketEvent
from .repository import (
    OutboxClaim,
    claim_outbox,
    complete_outbox,
    retry_outbox,
    terminal_fail_outbox,
)
from .routing import route_event


def _pipeline_config(config: Any) -> Mapping[str, Any]:
    if isinstance(config, Mapping):
        value = config.get("event_pipeline", {})
        return value if isinstance(value, Mapping) else {}
    return {}


def _number(
    settings: Mapping[str, Any], key: str, default: float, minimum: float = 0.0
) -> float:
    try:
        return max(minimum, float(settings.get(key, default)))
    except (TypeError, ValueError):
        return default


class LeaseLostError(RuntimeError):
    """Raised to roll back handler writes after an outbox lease is lost."""


class OutboxWorker:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {
            "running": False,
            "worker_id": None,
            "last_poll_at": None,
            "last_success_at": None,
            "last_error": None,
            "claimed": 0,
            "completed": 0,
            "retried": 0,
            "failed": 0,
        }
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._config: Any = None

    def start(self, config: Any = None) -> OutboxWorker:
        if self._thread is not None and self._thread.is_alive():
            return self
        settings = _pipeline_config(config)
        if (
            settings.get("enabled", False) is False
            or settings.get("outbox_worker_enabled", False) is False
        ):
            self.state.update({"running": False, "disabled": True})
            return self
        self._config = config
        self._stop.clear()
        worker_id = f"outbox-{uuid.uuid4().hex[:12]}"
        self.state.update({"running": True, "disabled": False, "worker_id": worker_id})
        self._thread = threading.Thread(target=self._run, name=worker_id, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        if thread is not None and thread.is_alive():
            self.state["running"] = True
            return
        self.state["running"] = False
        self._thread = None

    def _run(self) -> None:
        settings = _pipeline_config(self._config)
        poll = _number(settings, "poll_interval_seconds", 1.0, 0.01)
        # Sequential worker: one claim per poll so a queued in-memory claim
        # cannot expire and be reclaimed by another replica mid-processing.
        batch = 1
        lease = _number(settings, "lease_seconds", 30.0, 1.0)
        max_attempts = max(1, int(_number(settings, "max_attempts", 5, 1)))
        base_backoff = _number(settings, "base_backoff_seconds", 1.0, 0.01)
        max_backoff = max(
            base_backoff, _number(settings, "max_backoff_seconds", 60.0, base_backoff)
        )
        try:
            while not self._stop.is_set():
                self.state["last_poll_at"] = datetime.now(UTC)
                try:
                    with get_session(self._config) as session:
                        claims = claim_outbox(
                            session,
                            worker_id=self.state["worker_id"],
                            limit=batch,
                            lease_seconds=lease,
                        )
                    self.state["claimed"] += len(claims)
                    for claim in claims:
                        self._process(claim, max_attempts, base_backoff, max_backoff)
                except Exception as exc:
                    # Poll errors must not kill the daemon; store only a type name.
                    self.state["last_error"] = type(exc).__name__
                self._stop.wait(poll)
        finally:
            self.state["running"] = False

    def _load_event(self, session: Any, event_id: Any) -> MarketEvent:
        from sqlalchemy import text

        row = (
            session.execute(
                text("SELECT * FROM market_events WHERE id = :id"), {"id": event_id}
            )
            .mappings()
            .one()
        )
        from .repository import _event_from_row

        return _event_from_row(row)

    def _process(
        self,
        claim: OutboxClaim,
        max_attempts: int,
        base_backoff: float,
        max_backoff: float,
    ) -> None:
        try:
            with get_session(self._config) as session:
                event = claim.event or self._load_event(session, claim.event_id)
                route_event(session, event, topic=claim.topic)
                if not complete_outbox(session, claim.id, worker_id=claim.claimed_by):
                    raise LeaseLostError("outbox lease lost")
                self.state["completed"] += 1
                self.state["last_success_at"] = datetime.now(UTC)
        except Exception as exc:
            self.state["last_error"] = type(exc).__name__
            attempt = claim.attempt_count
            with get_session(self._config) as session:
                if attempt >= max_attempts:
                    if terminal_fail_outbox(
                        session, claim.id, worker_id=claim.claimed_by, error=exc
                    ):
                        self.state["failed"] += 1
                else:
                    delay = min(max_backoff, base_backoff * (2 ** max(0, attempt - 1)))
                    delay *= random.uniform(0.8, 1.2)
                    retry_at = datetime.now(UTC) + timedelta(seconds=delay)
                    if retry_outbox(
                        session,
                        claim.id,
                        worker_id=claim.claimed_by,
                        available_at=retry_at,
                        error=exc,
                    ):
                        self.state["retried"] += 1


outbox_worker = OutboxWorker()

__all__ = ["OutboxWorker", "outbox_worker"]
