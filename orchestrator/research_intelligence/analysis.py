"""Shared payload and validation helpers for live and replay research stages.

The live service and point-in-time replay deliberately own their boundaries:
persistence, leakage guards, and failure isolation stay in those callers.  This
module owns only the analytical contract that both paths must execute in the
same order with the same payloads and validators.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import datetime
from typing import Any

from research_intelligence.adversarial import validate_adversarial_output
from research_intelligence.contracts import (
    VALUE_CAPTURE_DIMENSIONS,
    CandidateGroup,
    CausalEdgeDraft,
    NormalizedEvidence,
    canonical_fingerprint,
    clean_payload,
)
from research_intelligence.discovery import (
    PatternAssessment,
    candidate_metrics,
)
from research_intelligence.models import STAGE_VERSIONS
from research_intelligence.relationships import causal_edge_fingerprint
from research_intelligence.value_capture import validate_value_capture_output

_ANALYTICAL_STAGES = (
    "claim_extraction",
    "pattern_discovery",
    "causal_chain",
    "value_capture",
    "adversarial",
    "deliverable",
)


def analytical_stage_identities(
    cache_identity: Callable[[str], Any],
) -> dict[str, Any]:
    """Build the cache identity for every stage in the analytical pipeline."""
    return {stage: cache_identity(stage) for stage in _ANALYTICAL_STAGES}


def stage_input_fingerprint(
    stage: str,
    payload: Any,
    *,
    replay_as_of: datetime | str | None = None,
) -> str:
    """Fingerprint one stage input, retaining replay's explicit as-of binding."""
    if replay_as_of is None:
        identity = {"stage": STAGE_VERSIONS[stage], "input": payload}
    else:
        as_of = (
            replay_as_of.isoformat()
            if isinstance(replay_as_of, datetime)
            else str(replay_as_of)
        )
        identity = {"stage": stage, "replay_as_of": as_of, "input": payload}
    return canonical_fingerprint(identity)


def build_claim_payload(
    evidence: Sequence[NormalizedEvidence],
) -> dict[str, Any]:
    return {"evidence": [item.to_dict() for item in evidence]}


def build_pattern_payload(
    group: CandidateGroup,
    *,
    replay_metadata: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "blocking_key": group.blocking_key,
        "metrics": candidate_metrics(group, now=now),
        "source_names": list(group.source_names),
        "industries": list(group.industries),
        "evidence": [item.to_dict() for item in group.evidence],
    }
    if replay_metadata is not None:
        payload = {"replay": dict(replay_metadata), **payload}
    return payload


def build_case_context(
    group: CandidateGroup,
    assessment: PatternAssessment,
    *,
    case_id: str,
    replay_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "case_id": case_id,
        "case": clean_payload(assessment),
        "evidence": [item.to_dict() for item in group.evidence],
    }
    if replay_metadata is not None:
        payload["replay"] = dict(replay_metadata)
    return payload


def build_causal_payload(
    case_context: Mapping[str, Any],
    *,
    graph_depth: int,
    maximum_graph_edges: int,
) -> dict[str, Any]:
    return {
        **case_context,
        "bounds": {
            "maximum_depth": graph_depth,
            "maximum_edges": maximum_graph_edges,
        },
    }


def build_value_payload(
    case_context: Mapping[str, Any], edges: Sequence[CausalEdgeDraft]
) -> dict[str, Any]:
    return {
        **case_context,
        "causal_edges": clean_payload(edges),
        "dimensions": list(VALUE_CAPTURE_DIMENSIONS),
    }


def build_adversarial_payload(
    case_context: Mapping[str, Any],
    edges: Sequence[CausalEdgeDraft],
    value_assessments: Sequence[Any],
    *,
    existing_data_requests: Sequence[Any],
) -> dict[str, Any]:
    return {
        **case_context,
        "causal_edges": clean_payload(edges),
        "edge_fingerprints": edge_fingerprints(edges),
        "value_capture": clean_payload(value_assessments),
        "existing_data_requests": clean_payload(existing_data_requests),
    }


def build_deliverable_payload(
    case_context: Mapping[str, Any],
    edges: Sequence[CausalEdgeDraft],
    value_assessments: Sequence[Any],
    adversarial: Any,
) -> dict[str, Any]:
    return {
        **case_context,
        "causal_edges": clean_payload(edges),
        "edge_fingerprints": edge_fingerprints(edges),
        "value_capture": clean_payload(value_assessments),
        "counterevidence": clean_payload(adversarial),
    }


def validate_value_stage(
    output: Any,
    evidence: Sequence[NormalizedEvidence],
    graph_nodes: Collection[tuple[str, str]],
) -> Any:
    assessments = validate_value_capture_output(output, evidence)
    if any(
        graph_nodes and (item.node_type, item.node_key) not in graph_nodes
        for item in assessments
    ):
        raise ValueError(
            "value-capture output references a node outside the causal graph"
        )
    return assessments


def validate_adversarial_stage(
    output: Any,
    evidence: Sequence[NormalizedEvidence],
    edge_fingerprints_: Sequence[str],
) -> Any:
    return validate_adversarial_output(
        output,
        evidence,
        edge_fingerprints=edge_fingerprints_,
        maximum_counterevidence=30,
        maximum_requests=20,
    )


def edge_fingerprints(edges: Sequence[CausalEdgeDraft]) -> tuple[str, ...]:
    return tuple(
        causal_edge_fingerprint(
            from_type=edge.from_type,
            from_key=edge.from_key,
            relationship=edge.relationship,
            to_type=edge.to_type,
            to_key=edge.to_key,
        )
        for edge in edges
    )


def causal_graph_nodes(edges: Sequence[CausalEdgeDraft]) -> set[tuple[str, str]]:
    return {
        node
        for edge in edges
        for node in (
            (edge.from_type, edge.from_key),
            (edge.to_type, edge.to_key),
        )
    }
