"""Bounded UI/domain invalidations for persisted control-plane changes."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from ui_events import append_ui_invalidation

_ALLOWED_SECTIONS = frozenset(
    {
        "research_questions",
        "research_work_orders",
        "research_effects",
        "research_control_plane",
        "system_topology",
    }
)


def publish_control_plane_invalidations(
    session: Any, sections: Iterable[str]
) -> int:
    """Append at most one wakeup per allowlisted section in caller transaction."""
    bounded = sorted(set(sections) & _ALLOWED_SECTIONS)[:8]
    base_version = int(datetime.now(UTC).timestamp() * 1_000_000)
    published = 0
    for offset, section in enumerate(bounded):
        event = append_ui_invalidation(
            session,
            section_key=section,
            scope_key="global",
            section_version=base_version + offset,
        )
        published += int(event is not None)
    return published


__all__ = ["publish_control_plane_invalidations"]
