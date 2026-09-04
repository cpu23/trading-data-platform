"""Duck-typed database execution result conversion helpers.

Zero-dependency module safe for use across orchestrator, api, and contracts.
"""

from __future__ import annotations

from typing import Any


def _mapped_result(result: Any) -> Any:
    if result is None:
        return None
    try:
        return result.mappings()
    except (AttributeError, TypeError):
        return result


def result_rows(result: Any) -> list[dict[str, Any]]:
    """Convert a database execution result into a list of dictionaries."""
    res = _mapped_result(result)
    if res is None:
        return []
    try:
        rows = res.all() if hasattr(res, "all") and callable(res.all) else res
        return [dict(getattr(row, "_mapping", row)) for row in rows]
    except (AttributeError, TypeError, ValueError):
        return []


def result_first(result: Any) -> dict[str, Any] | None:
    """Convert the first row of a database execution result into a dictionary."""
    res = _mapped_result(result)
    if res is None:
        return None
    try:
        row = (
            res.first()
            if hasattr(res, "first") and callable(res.first)
            else next(iter(res), None)
        )
        return dict(getattr(row, "_mapping", row)) if row is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


__all__ = [
    "result_first",
    "result_rows",
]
