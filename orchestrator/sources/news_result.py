"""Typed outcomes shared by news source collectors."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class NewsCollectionResult:
    """A source collection outcome; failures may retain successfully collected items."""

    items: list[dict[str, Any]]
    status: Literal["ok", "error"]
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "ok"
