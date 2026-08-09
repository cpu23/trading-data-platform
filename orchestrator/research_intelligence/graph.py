"""Strict causal-edge validation, semantic deduplication and bounded traversal."""

from __future__ import annotations

import math
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from processors._validators import scan_prohibited_language
from research_intelligence.config import ResearchSettings
from research_intelligence.contracts import (
    CausalEdgeDraft,
    EpistemicState,
    EvidenceType,
    NormalizedEntity,
    NormalizedEvidence,
    evidence_catalog,
    reject_embedded_evidence_references,
    validate_evidence_references,
)
from research_intelligence.discovery import reject_unsupported_numeric_text
from research_intelligence.relationships import (
    causal_edge_fingerprint,
    normalize_entity,
    validate_relationship,
)

_EDGE_KEYS = frozenset(
    {
        "from_entity",
        "relationship",
        "to_entity",
        "mechanism",
        "epistemic_state",
        "evidence_ids",
        "confidence",
        "missing_evidence",
        "break_conditions",
        "depth",
        "valid_from",
        "valid_to",
    }
)
_DIRECT_OBSERVATION_TYPES = frozenset(
    {
        EvidenceType.MACRO_OBSERVATION.value,
        EvidenceType.MACRO_RELEASE.value,
        EvidenceType.MARKET_STATE.value,
        EvidenceType.MARKET_CONFIRMATION.value,
        EvidenceType.FILING_DELTA.value,
        EvidenceType.SOURCE_CLAIM.value,
    }
)
_STATE_PRIORITY = {
    EpistemicState.REJECTED.value: 4,
    EpistemicState.OBSERVED.value: 3,
    EpistemicState.SUPPORTED.value: 2,
    EpistemicState.HYPOTHESIS.value: 1,
}


def _text(value: Any, maximum: int, field: str, required: bool = False) -> str | None:
    cleaned = " ".join(str(value or "").split())
    if required and not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned[:maximum] if cleaned else None


def _strings(value: Any, maximum: int, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{field} must be an array of at most {maximum} items")
    output: list[str] = []
    for item in value:
        cleaned = _text(item, 400, field, required=True)
        if cleaned not in output:
            output.append(cleaned)
    return tuple(output)


def _confidence(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("edge confidence must be numeric or null")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError("edge confidence must be numeric or null") from None
    if not math.isfinite(parsed) or not 0 <= parsed <= 1:
        raise ValueError("edge confidence must be between 0 and 1")
    return parsed


def _timestamp(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be ISO text or null")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{field} is invalid") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _entity(raw: Any, field: str) -> NormalizedEntity:
    if not isinstance(raw, Mapping) or set(raw) != {"entity_type", "name"}:
        raise ValueError(f"{field} must contain exactly entity_type and name")
    return normalize_entity(raw.get("entity_type"), raw.get("name"))


def _node(entity: NormalizedEntity) -> tuple[str, str]:
    return entity.entity_type, entity.normalized_key


def _path_exists(
    adjacency: Mapping[tuple[str, str], set[tuple[str, str]]],
    start: tuple[str, str],
    target: tuple[str, str],
    hard_depth: int,
) -> bool:
    queue: deque[tuple[tuple[str, str], int]] = deque([(start, 0)])
    visited = {start}
    while queue:
        node, depth = queue.popleft()
        if node == target:
            return True
        if depth >= hard_depth:
            continue
        for child in adjacency.get(node, set()):
            if child not in visited:
                visited.add(child)
                queue.append((child, depth + 1))
    return False


def _entity_supported(entity: NormalizedEntity, evidence: Sequence[NormalizedEvidence]) -> bool:
    for item in evidence:
        if any(
            candidate.entity_type == entity.entity_type
            and candidate.normalized_key == entity.normalized_key
            for candidate in item.entities
        ):
            return True
    terms = set(entity.normalized_key.split("-"))
    if not terms:
        return False
    corpus = " ".join(
        f"{item.title} {item.bounded_excerpt or ''}" for item in evidence
    ).casefold()
    return all(term in corpus for term in terms)


def validate_causal_output(
    output: Any,
    evidence: Sequence[NormalizedEvidence],
    settings: ResearchSettings,
    *,
    seed_entities: Sequence[NormalizedEntity] = (),
) -> tuple[CausalEdgeDraft, ...]:
    if not isinstance(output, Mapping) or set(output) != {"abstained", "edges"}:
        raise ValueError("causal output must contain exactly abstained and edges")
    reject_embedded_evidence_references(output)
    if not isinstance(output.get("abstained"), bool):
        raise ValueError("causal abstained flag must be boolean")
    raw_edges = output.get("edges")
    if not isinstance(raw_edges, list) or len(raw_edges) > settings.maximum_graph_edges:
        raise ValueError("causal edges exceed configured bound")
    if output["abstained"]:
        if raw_edges:
            raise ValueError("abstained causal output cannot include edges")
        return ()
    catalog = evidence_catalog(evidence)
    allowed_seed = {(_node(entity)) for entity in seed_entities}
    drafts_by_fingerprint: dict[str, CausalEdgeDraft] = {}
    adjacency: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    nodes: set[tuple[str, str]] = set()
    for raw in raw_edges:
        if not isinstance(raw, Mapping) or set(raw) != _EDGE_KEYS:
            raise ValueError("causal edge keys do not match the strict contract")
        source = _entity(raw.get("from_entity"), "from_entity")
        target = _entity(raw.get("to_entity"), "to_entity")
        if allowed_seed and _node(source) not in allowed_seed and not _entity_supported(source, evidence):
            raise ValueError(f"unsupported graph entity: {source.display_name}")
        if allowed_seed and _node(target) not in allowed_seed and not _entity_supported(target, evidence):
            raise ValueError(f"unsupported graph entity: {target.display_name}")
        relationship = validate_relationship(raw.get("relationship"))
        fingerprint = causal_edge_fingerprint(
            from_type=source.entity_type,
            from_key=source.normalized_key,
            relationship=relationship,
            to_type=target.entity_type,
            to_key=target.normalized_key,
        )
        state = str(raw.get("epistemic_state") or "").strip().casefold()
        if state not in {item.value for item in EpistemicState}:
            raise ValueError("causal epistemic state is invalid")
        references = validate_evidence_references(raw.get("evidence_ids"), catalog)
        if state in {EpistemicState.OBSERVED.value, EpistemicState.SUPPORTED.value} and not references:
            raise ValueError(f"{state} edge requires evidence")
        if state == EpistemicState.OBSERVED.value and not any(
            catalog[reference].evidence_type in _DIRECT_OBSERVATION_TYPES
            for reference in references
        ):
            raise ValueError("observed edge lacks direct observation evidence")
        mechanism = _text(raw.get("mechanism"), 1_000, "mechanism", required=True)
        missing = _strings(raw.get("missing_evidence"), 20, "missing_evidence")
        breaks = _strings(raw.get("break_conditions"), 20, "break_conditions")
        depth = raw.get("depth")
        if isinstance(depth, bool) or not isinstance(depth, int):
            raise ValueError("edge depth must be an integer")
        if not 1 <= depth <= settings.graph_depth or depth > settings.hard_graph_depth:
            raise ValueError("edge depth exceeds configured bound")
        valid_from = _timestamp(raw.get("valid_from"), "valid_from")
        valid_to = _timestamp(raw.get("valid_to"), "valid_to")
        if valid_from and valid_to and valid_to < valid_from:
            raise ValueError("edge valid_to precedes valid_from")
        if scan_prohibited_language(raw):
            raise ValueError("causal edge contains prohibited advisory language")
        reject_unsupported_numeric_text(
            {"mechanism": mechanism, "missing_evidence": missing, "break_conditions": breaks},
            evidence,
        )
        draft = CausalEdgeDraft(
            from_type=source.entity_type,
            from_key=source.normalized_key,
            from_name=source.display_name,
            relationship=relationship,
            to_type=target.entity_type,
            to_key=target.normalized_key,
            to_name=target.display_name,
            mechanism=mechanism,
            epistemic_state=state,
            evidence_ids=references,
            confidence=_confidence(raw.get("confidence")),
            missing_evidence=missing,
            break_conditions=breaks,
            depth=depth,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        prior = drafts_by_fingerprint.get(fingerprint)
        if prior is not None:
            if _STATE_PRIORITY[state] > _STATE_PRIORITY[prior.epistemic_state]:
                drafts_by_fingerprint[fingerprint] = draft
            continue
        source_node, target_node = _node(source), _node(target)
        if _path_exists(adjacency, target_node, source_node, settings.hard_graph_depth):
            continue
        adjacency[source_node].add(target_node)
        nodes.update((source_node, target_node))
        if len(nodes) > settings.maximum_graph_nodes:
            raise ValueError("causal graph node bound exceeded")
        drafts_by_fingerprint[fingerprint] = draft
    return tuple(drafts_by_fingerprint.values())


def bounded_traversal(
    edges: Sequence[CausalEdgeDraft | Mapping[str, Any]],
    start_type: str,
    start_key: str,
    *,
    max_depth: int = 3,
    hard_max_depth: int = 5,
) -> tuple[tuple[CausalEdgeDraft | Mapping[str, Any], ...], ...]:
    if not 1 <= max_depth <= hard_max_depth <= 8:
        raise ValueError("invalid graph traversal bounds")
    adjacency: dict[tuple[str, str], list[CausalEdgeDraft | Mapping[str, Any]]] = defaultdict(list)
    for edge in edges[:400]:
        if isinstance(edge, Mapping):
            source = (str(edge.get("from_type")), str(edge.get("from_key")))
        else:
            source = (edge.from_type, edge.from_key)
        adjacency[source].append(edge)
    paths: list[tuple[CausalEdgeDraft | Mapping[str, Any], ...]] = []
    queue: deque[tuple[tuple[str, str], tuple[Any, ...], frozenset[tuple[str, str]]]] = deque(
        [((start_type, start_key), (), frozenset({(start_type, start_key)}))]
    )
    while queue:
        node, path, visited = queue.popleft()
        if len(path) >= max_depth:
            continue
        for edge in adjacency.get(node, []):
            if isinstance(edge, Mapping):
                target = (str(edge.get("to_type")), str(edge.get("to_key")))
            else:
                target = (edge.to_type, edge.to_key)
            if target in visited:
                continue
            next_path = (*path, edge)
            paths.append(next_path)
            queue.append((target, next_path, visited | {target}))
    return tuple(paths)


__all__ = ["bounded_traversal", "validate_causal_output"]
