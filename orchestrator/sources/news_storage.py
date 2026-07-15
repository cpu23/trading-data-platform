"""Crash-safe JSON storage helpers for news source state and snapshots."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from logging_config import get_logger

logger = get_logger("news_storage")


@contextmanager
def publication_lock(data_dir: Path):
    """Own the stable, process-wide publication lock for one news data directory."""
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / ".news-publication.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        logger.warning("news_json_read_failed", path=str(path), error=str(exc))
        return default


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        json.loads(Path(temporary).read_text())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def merge_items(path: Path, items: list[dict]) -> list[dict]:
    existing = read_json(path, [])
    if not isinstance(existing, list):
        existing = []
    merged: dict[str, dict] = {}
    for item in [*existing, *items]:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]:
            merged[item["id"]] = item
    result = list(merged.values())
    atomic_write_json(path, result)
    return result
