"""Cross-process run coordination backed by PostgreSQL advisory locks."""

import hashlib
from contextlib import contextmanager

from logging_config import get_logger
from sqlalchemy import text

from db import get_session

logger = get_logger("orchestrator.locks")


class RunConflict(RuntimeError):
    """Raised when another worker already owns a named run lock."""

    def __init__(self, lock_name: str, context: dict | None = None):
        self.lock_name = lock_name
        self.context = context or {"lock_name": lock_name}
        super().__init__(f"run conflict: {lock_name}")


def stable_lock_key(name: str) -> int:
    """Map a lock name to a deterministic PostgreSQL signed-bigint key."""
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


@contextmanager
def advisory_lock(name: str, config: dict):
    """Try to hold a session-level advisory lock for the protected scope."""
    key = stable_lock_key(name)
    with get_session(config) as session:
        acquired = session.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
        ).scalar_one()
        if not acquired:
            raise RunConflict(name)

        try:
            yield
        finally:
            try:
                unlocked = session.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                ).scalar_one()
                if not unlocked:
                    logger.error(
                        "advisory_lock_release_failed",
                        action="release_advisory_lock",
                        lock_name=name,
                    )
            except Exception as exc:
                # A release problem must never replace an exception from protected work.
                logger.error(
                    "advisory_lock_release_error",
                    action="release_advisory_lock",
                    lock_name=name,
                    error=str(exc),
                )
