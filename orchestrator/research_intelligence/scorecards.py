"""Lightweight benchmark scorecard contracts and human-review persistence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import text

HUMAN_REVIEW_LABELS = frozenset({"pass", "partial", "fail", "unclear"})
SCORECARD_DIMENSIONS = frozenset(
    {
        "discovery",
        "lead_time",
        "specificity",
        "causal_quality",
        "second_order_reasoning",
        "value_capture_reasoning",
        "evidence_quality",
        "counter_thesis_quality",
        "hypothesis_discovery",
        "unknown_handling",
        "novelty",
        "point_in_time_integrity",
    }
)
_HUMAN_ANNOTATION_KEYS = frozenset({"overall_label", "dimension_labels", "notes"})


def validate_human_annotations(value: Any) -> dict[str, Any]:
    """Validate bounded human labels without altering deterministic dimensions."""
    if not isinstance(value, Mapping):
        raise ValueError("human annotations must be an object")
    unknown = set(value) - _HUMAN_ANNOTATION_KEYS
    if unknown:
        raise ValueError("human annotations contain unknown fields")
    output: dict[str, Any] = {}
    if value.get("overall_label") is not None:
        overall = str(value["overall_label"]).strip().casefold()
        if overall not in HUMAN_REVIEW_LABELS:
            raise ValueError("human annotation overall_label is invalid")
        output["overall_label"] = overall
    if value.get("dimension_labels") is not None:
        raw_dimensions = value["dimension_labels"]
        if not isinstance(raw_dimensions, Mapping):
            raise ValueError("human annotation dimension_labels must be an object")
        if len(raw_dimensions) > len(SCORECARD_DIMENSIONS):
            raise ValueError("human annotation dimension_labels exceed bound")
        dimensions: dict[str, str] = {}
        for raw_dimension, raw_label in raw_dimensions.items():
            dimension = str(raw_dimension).strip()
            label = str(raw_label).strip().casefold()
            if dimension not in SCORECARD_DIMENSIONS:
                raise ValueError("human annotation dimension is invalid")
            if label not in HUMAN_REVIEW_LABELS:
                raise ValueError("human annotation dimension label is invalid")
            dimensions[dimension] = label
        if dimensions:
            output["dimension_labels"] = dict(sorted(dimensions.items()))
    if value.get("notes") is not None:
        notes = str(value["notes"]).strip()
        if not notes or len(notes) > 4000:
            raise ValueError("human annotation notes must contain 1 to 4000 characters")
        output["notes"] = notes
    if not output:
        raise ValueError("human annotations cannot be empty")
    return output


def annotate_benchmark_scorecard(
    session: Any,
    replay_run_id: str,
    annotations: Mapping[str, Any],
    *,
    annotated_by: str,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Append one immutable human review and update the scorecard projection."""
    try:
        parsed_run_id = str(UUID(str(replay_run_id)))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("invalid replay_run_id") from None
    reviewer = str(annotated_by or "").strip()
    if not reviewer or len(reviewer) > 120:
        raise ValueError("annotated_by must contain 1 to 120 characters")
    if expected_version is not None and (
        not isinstance(expected_version, int)
        or isinstance(expected_version, bool)
        or expected_version < 0
    ):
        raise ValueError("expected_version must be a non-negative integer")
    cleaned = validate_human_annotations(annotations)
    result = session.execute(
        text(
            """
            SELECT id, annotation_version
            FROM research_benchmark_scorecards
            WHERE replay_run_id = :replay_run_id
            FOR UPDATE
            """
        ),
        {"replay_run_id": parsed_run_id},
    )
    try:
        row = result.mappings().first()
    except AttributeError:
        row = result.first()
        row = row._mapping if row is not None and hasattr(row, "_mapping") else row
    if row is None:
        raise ValueError("benchmark scorecard not found")
    current_version = int(row["annotation_version"] or 0)
    if expected_version is not None and expected_version != current_version:
        raise ValueError("human annotation version conflict")
    next_version = current_version + 1
    payload = json.dumps(cleaned, sort_keys=True)
    updated = session.execute(
        text(
            """
            UPDATE research_benchmark_scorecards
            SET human_annotations = CAST(:annotations AS JSONB),
                annotation_version = :next_version,
                annotated_by = :annotated_by,
                annotated_at = NOW()
            WHERE id = :scorecard_id
              AND annotation_version = :current_version
            """
        ),
        {
            "scorecard_id": row["id"],
            "current_version": current_version,
            "next_version": next_version,
            "annotations": payload,
            "annotated_by": reviewer,
        },
    )
    if int(getattr(updated, "rowcount", 0) or 0) != 1:
        raise RuntimeError("human annotation concurrent update")
    session.execute(
        text(
            """
            INSERT INTO research_benchmark_annotations (
                scorecard_id, annotation_version, annotations, annotated_by
            ) VALUES (
                :scorecard_id, :annotation_version,
                CAST(:annotations AS JSONB), :annotated_by
            )
            """
        ),
        {
            "scorecard_id": row["id"],
            "annotation_version": next_version,
            "annotations": payload,
            "annotated_by": reviewer,
        },
    )
    return {
        "replay_run_id": parsed_run_id,
        "annotation_version": next_version,
        "annotations": cleaned,
        "annotated_by": reviewer,
    }


__all__ = [
    "HUMAN_REVIEW_LABELS",
    "SCORECARD_DIMENSIONS",
    "annotate_benchmark_scorecard",
    "validate_human_annotations",
]
