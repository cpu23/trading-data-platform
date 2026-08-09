"""Concise evidence-linked research deliverable contract."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from processors._validators import scan_prohibited_language
from research_intelligence.contracts import (
    NormalizedEvidence,
    evidence_catalog,
    reject_embedded_evidence_references,
    validate_evidence_references,
)
from research_intelligence.discovery import reject_unsupported_numeric_text
from research_intelligence.relationships import (
    normalize_entity_key,
    normalize_entity_type,
)

_OUTPUT_KEYS = frozenset(
    {
        "abstained",
        "what_changed",
        "why_it_matters",
        "transmission",
        "potential_capture",
        "evidence_for",
        "evidence_against",
        "weak_links_unknowns",
        "what_to_watch",
    }
)


@dataclass(frozen=True, slots=True)
class EvidenceBullet:
    text: str
    evidence_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "evidence_ids": list(self.evidence_ids)}


@dataclass(frozen=True, slots=True)
class PotentialCapture:
    node_type: str
    node_key: str
    node_name: str
    text: str
    evidence_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_type": self.node_type,
            "node_key": self.node_key,
            "node_name": self.node_name,
            "text": self.text,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True, slots=True)
class ResearchDeliverable:
    what_changed: EvidenceBullet
    why_it_matters: EvidenceBullet
    transmission_text: str
    transmission_edge_fingerprints: tuple[str, ...]
    potential_capture: tuple[PotentialCapture, ...]
    evidence_for: tuple[EvidenceBullet, ...]
    evidence_against: tuple[EvidenceBullet, ...]
    weak_links_unknowns: tuple[str, ...]
    what_to_watch: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "what_changed": self.what_changed.to_dict(),
            "why_it_matters": self.why_it_matters.to_dict(),
            "transmission": {
                "text": self.transmission_text,
                "edge_fingerprints": list(self.transmission_edge_fingerprints),
            },
            "potential_capture": [item.to_dict() for item in self.potential_capture],
            "evidence_for": [item.to_dict() for item in self.evidence_for],
            "evidence_against": [item.to_dict() for item in self.evidence_against],
            "weak_links_unknowns": list(self.weak_links_unknowns),
            "what_to_watch": list(self.what_to_watch),
        }


def _text(value: Any, maximum: int, field: str, required: bool = False) -> str | None:
    cleaned = " ".join(str(value or "").split())
    if required and not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned[:maximum] if cleaned else None


def _bullet(raw: Any, catalog: Mapping[str, NormalizedEvidence], field: str) -> EvidenceBullet:
    if not isinstance(raw, Mapping) or set(raw) != {"text", "evidence_ids"}:
        raise ValueError(f"{field} keys are invalid")
    text = _text(raw.get("text"), 700, field, required=True)
    references = validate_evidence_references(raw.get("evidence_ids"), catalog)
    if not references:
        raise ValueError(f"{field} requires evidence")
    return EvidenceBullet(text=text, evidence_ids=references)


def _bullets(
    value: Any,
    catalog: Mapping[str, NormalizedEvidence],
    field: str,
    maximum: int,
) -> tuple[EvidenceBullet, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{field} exceeds bound")
    return tuple(_bullet(item, catalog, field) for item in value)


def _strings(value: Any, field: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{field} exceeds bound")
    output: list[str] = []
    for raw in value:
        text = _text(raw, 500, field, required=True)
        if text not in output:
            output.append(text)
    return tuple(output)


def validate_deliverable_output(
    output: Any,
    evidence: Sequence[NormalizedEvidence],
    *,
    edge_fingerprints: Sequence[str] = (),
    assessment_nodes: Sequence[tuple[str, str]] = (),
) -> ResearchDeliverable | None:
    if not isinstance(output, Mapping) or set(output) != _OUTPUT_KEYS:
        raise ValueError("deliverable keys do not match the strict contract")
    reject_embedded_evidence_references(output)
    if not isinstance(output.get("abstained"), bool):
        raise ValueError("deliverable abstained flag must be boolean")
    if output["abstained"]:
        return None
    catalog = evidence_catalog(evidence)
    what_changed = _bullet(output.get("what_changed"), catalog, "what_changed")
    why_it_matters = _bullet(output.get("why_it_matters"), catalog, "why_it_matters")
    transmission = output.get("transmission")
    if not isinstance(transmission, Mapping) or set(transmission) != {
        "text",
        "edge_fingerprints",
    }:
        raise ValueError("transmission keys are invalid")
    transmission_text = _text(
        transmission.get("text"), 900, "transmission", required=True
    )
    raw_edges = transmission.get("edge_fingerprints")
    if not isinstance(raw_edges, list) or len(raw_edges) > 60:
        raise ValueError("transmission edge references exceed bound")
    allowed_edges = set(edge_fingerprints)
    resolved_edges: list[str] = []
    for raw in raw_edges:
        fingerprint = str(raw or "").strip()
        if fingerprint not in allowed_edges:
            raise ValueError("deliverable references unknown causal edge")
        if fingerprint not in resolved_edges:
            resolved_edges.append(fingerprint)
    raw_capture = output.get("potential_capture")
    if not isinstance(raw_capture, list) or len(raw_capture) > 30:
        raise ValueError("potential capture items exceed bound")
    allowed_nodes = set(assessment_nodes)
    captures: list[PotentialCapture] = []
    for raw in raw_capture:
        if not isinstance(raw, Mapping) or set(raw) != {
            "node_type",
            "node_key",
            "node_name",
            "text",
            "evidence_ids",
        }:
            raise ValueError("potential capture keys are invalid")
        node_type = normalize_entity_type(raw.get("node_type"))
        node_key = normalize_entity_key(raw.get("node_key"))
        if allowed_nodes and (node_type, node_key) not in allowed_nodes:
            raise ValueError("potential capture references unknown assessment node")
        captures.append(
            PotentialCapture(
                node_type=node_type,
                node_key=node_key,
                node_name=_text(raw.get("node_name"), 200, "node_name", required=True),
                text=_text(raw.get("text"), 600, "potential capture", required=True),
                evidence_ids=validate_evidence_references(raw.get("evidence_ids"), catalog),
            )
        )
    evidence_for = _bullets(output.get("evidence_for"), catalog, "evidence_for", 20)
    evidence_against = _bullets(
        output.get("evidence_against"), catalog, "evidence_against", 20
    )
    unknowns = _strings(output.get("weak_links_unknowns"), "weak links", 30)
    watch = _strings(output.get("what_to_watch"), "what to watch", 30)
    if scan_prohibited_language(output):
        raise ValueError("deliverable contains prohibited advisory language")
    reject_unsupported_numeric_text(
        {
            "what_changed": what_changed.text,
            "why_it_matters": why_it_matters.text,
            "transmission": transmission_text,
            "potential_capture": [item.text for item in captures],
            "evidence_for": [item.text for item in evidence_for],
            "evidence_against": [item.text for item in evidence_against],
            "weak_links_unknowns": unknowns,
            "what_to_watch": watch,
        },
        evidence,
    )
    return ResearchDeliverable(
        what_changed=what_changed,
        why_it_matters=why_it_matters,
        transmission_text=transmission_text,
        transmission_edge_fingerprints=tuple(resolved_edges),
        potential_capture=tuple(captures),
        evidence_for=evidence_for,
        evidence_against=evidence_against,
        weak_links_unknowns=unknowns,
        what_to_watch=watch,
    )


__all__ = [
    "EvidenceBullet",
    "PotentialCapture",
    "ResearchDeliverable",
    "validate_deliverable_output",
]
