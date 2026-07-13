"""Crash-safe JSON storage helpers for news source state and snapshots."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from logging_config import get_logger

logger = get_logger("news_storage")


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
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
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
