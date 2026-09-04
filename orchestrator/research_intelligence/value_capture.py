"""Multidimensional, nullable value-capture assessment validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from processors._validators import scan_prohibited_language

from research_intelligence.contracts import (
    VALUE_CAPTURE_DIMENSIONS,
    NormalizedEvidence,
    ValueCaptureDraft,
    evidence_catalog,
    reject_embedded_evidence_references,
    validate_evidence_references,
)
from research_intelligence.discovery import reject_unsupported_numeric_text
from research_intelligence.relationships import normalize_entity

_ASSESSMENT_KEYS = frozenset(
    {"node", "dimensions", "rationale", "evidence_ids", "unknowns"}
)
_DIMENSION_VALUES = frozenset({"low", "moderate", "high"})


def _text(value: Any, maximum: int, field: str, required: bool = False) -> str | None:
    cleaned = " ".join(str(value or "").split())
    if required and not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned[:maximum] if cleaned else None


def _unknowns(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 30:
        raise ValueError("value-capture unknowns must contain at most 30 items")
    output: list[str] = []
    for item in value:
        cleaned = _text(item, 400, "unknown", required=True)
        if cleaned not in output:
            output.append(cleaned)
    return tuple(output)


def validate_value_capture_output(
    output: Any,
    evidence: Sequence[NormalizedEvidence],
    *,
    maximum_assessments: int = 40,
) -> tuple[ValueCaptureDraft, ...]:
    if not isinstance(output, Mapping) or set(output) != {"abstained", "assessments"}:
        raise ValueError("value-capture output keys do not match the strict contract")
    reject_embedded_evidence_references(output)
    if not isinstance(output.get("abstained"), bool):
        raise ValueError("value-capture abstained flag must be boolean")
    raw_assessments = output.get("assessments")
    if (
        not isinstance(raw_assessments, list)
        or len(raw_assessments) > maximum_assessments
    ):
        raise ValueError("value-capture assessments exceed configured bound")
    if output["abstained"]:
        if raw_assessments:
            raise ValueError(
                "abstained value-capture output cannot include assessments"
            )
        return ()
    catalog = evidence_catalog(evidence)
    drafts: list[ValueCaptureDraft] = []
    seen_nodes: set[tuple[str, str]] = set()
    for raw in raw_assessments:
        if not isinstance(raw, Mapping) or set(raw) != _ASSESSMENT_KEYS:
            raise ValueError("value-capture assessment keys are invalid")
        node_raw = raw.get("node")
        if not isinstance(node_raw, Mapping) or set(node_raw) != {
            "entity_type",
            "name",
        }:
            raise ValueError("value-capture node keys are invalid")
        node = normalize_entity(node_raw.get("entity_type"), node_raw.get("name"))
        node_id = (node.entity_type, node.normalized_key)
        if node_id in seen_nodes:
            raise ValueError("duplicate value-capture node")
        seen_nodes.add(node_id)
        dimensions_raw = raw.get("dimensions")
        if not isinstance(dimensions_raw, Mapping) or set(dimensions_raw) != set(
            VALUE_CAPTURE_DIMENSIONS
        ):
            raise ValueError("value-capture dimensions do not match the contract")
        dimensions: dict[str, str | None] = {}
        for dimension in VALUE_CAPTURE_DIMENSIONS:
            value = dimensions_raw.get(dimension)
            if value is None or str(value).strip().casefold() == "unknown":
                dimensions[dimension] = None
                continue
            normalized = str(value).strip().casefold()
            if normalized not in _DIMENSION_VALUES:
                raise ValueError(f"value-capture dimension {dimension} is invalid")
            dimensions[dimension] = normalized
        rationale_raw = raw.get("rationale")
        if not isinstance(rationale_raw, Mapping) or set(rationale_raw) != set(
            VALUE_CAPTURE_DIMENSIONS
        ):
            raise ValueError("value-capture rationale does not match dimensions")
        rationale = {
            dimension: _text(
                rationale_raw.get(dimension),
                500,
                f"rationale.{dimension}",
            )
            or ""
            for dimension in VALUE_CAPTURE_DIMENSIONS
        }
        references = validate_evidence_references(raw.get("evidence_ids"), catalog)
        if any(value is not None for value in dimensions.values()) and not references:
            raise ValueError("non-null value-capture dimensions require evidence")
        unknowns = _unknowns(raw.get("unknowns"))
        if scan_prohibited_language(raw):
            raise ValueError(
                "value-capture output contains prohibited advisory language"
            )
        reject_unsupported_numeric_text(
            {"rationale": rationale, "unknowns": unknowns}, evidence
        )
        drafts.append(
            ValueCaptureDraft(
                node_type=node.entity_type,
                node_key=node.normalized_key,
                node_name=node.display_name,
                dimensions=dimensions,
                rationale=rationale,
                evidence_ids=references,
                unknowns=unknowns,
            )
        )
    return tuple(drafts)


__all__ = ["validate_value_capture_output"]
