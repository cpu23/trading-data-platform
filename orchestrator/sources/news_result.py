"""Typed outcomes shared by news source collectors."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True)
class NewsPublication:
    """Durable writes that must follow snapshot and feed publication."""

    snapshot_path: Path
    state_path: Path
    candidate_state: dict[str, Any]


@dataclass(frozen=True)
class NewsCollectionResult:
    """A source collection outcome; failures may retain successfully collected items."""

    items: list[dict[str, Any]]
    status: Literal["ok", "error"]
    error: str | None = None
    publication: NewsPublication | None = None
    feed_published: bool = False

    @property
    def succeeded(self) -> bool:
        return self.status == "ok"
