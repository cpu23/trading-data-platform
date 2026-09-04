"""Caller-owned SQL persistence for research intelligence domain objects."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from contracts.db_results import result_first, result_rows
from research_intelligence.adversarial import AdversarialAssessment
from research_intelligence.config import ResearchSettings
from research_intelligence.contracts import (
    VALUE_CAPTURE_DIMENSIONS,
    CausalEdgeDraft,
    EconomicFactorDraft,
    MarketDriverDraft,
    ModelProvenance,
    NormalizedEvidence,
    ValueCaptureDraft,
    canonical_fingerprint,
    evidence_catalog,
)
from research_intelligence.discovery import PatternAssessment, token_similarity
from research_intelligence.lifecycle import (
    CaseStats,
    next_lifecycle_state,
)
from research_intelligence.relationships import causal_edge_fingerprint

_MAX_CASE_MATCHES = 200


@dataclass(frozen=True, slots=True)
class CaseMutation:
    case_id: str
    created: bool
    changed: bool
    input_fingerprint: str


@dataclass(frozen=True, slots=True)
class SnapshotMutation:
    snapshot_id: str | None
    version: int
    changed: bool


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _utc(value: datetime | None = None) -> datetime:
    result = value or datetime.now(UTC)
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _uuid(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("invalid UUID") from None


def find_case_match_rows(
    session: Any, limit: int = _MAX_CASE_MATCHES
) -> list[dict[str, Any]]:
    bounded = max(1, min(int(limit), _MAX_CASE_MATCHES))
    return result_rows(
        session.execute(
            text(
                """
            SELECT c.id, c.semantic_fingerprint, c.title, c.definition,
                   c.input_fingerprint, c.lifecycle_state, c.current_version,
                   c.last_evidence_at,
                   (
                       SELECT s.payload->>'evidence_input_fingerprint'
                       FROM research_case_snapshots s
                       WHERE s.case_id = c.id AND s.version = c.current_version
                       LIMIT 1
                   ) AS evidence_input_fingerprint,
                   (
                       SELECT s.payload->>'pipeline_input_fingerprint'
                       FROM research_case_snapshots s
                       WHERE s.case_id = c.id AND s.version = c.current_version
                       LIMIT 1
                   ) AS pipeline_input_fingerprint,
                   (
                       SELECT s.payload->>'blocking_key'
                       FROM research_case_snapshots s
                       WHERE s.case_id = c.id AND s.version = c.current_version
                       LIMIT 1
                   ) AS blocking_key,
                   (
                       SELECT s.payload->>'pipeline_complete'
                       FROM research_case_snapshots s
                       WHERE s.case_id = c.id AND s.version = c.current_version
                       LIMIT 1
                   ) AS pipeline_complete,
                   COALESCE(
                       ARRAY_AGG(a.alias ORDER BY a.alias)
                           FILTER (WHERE a.alias IS NOT NULL),
                       '{}'::TEXT[]
                   ) AS aliases
            FROM research_cases AS c
            LEFT JOIN research_case_aliases AS a ON a.case_id = c.id
            WHERE c.lifecycle_state <> 'archived'
            GROUP BY c.id
            ORDER BY c.last_evidence_at DESC, c.id
            LIMIT :limit
            """
            ),
            {"limit": bounded},
        )
    )


def _case_input_fingerprint(
    assessment: PatternAssessment, evidence_fingerprint: str
) -> str:
    return canonical_fingerprint(
        {
            "pattern": {
                "label": assessment.label,
                "definition": assessment.definition,
                "case_type": assessment.case_type,
                "horizon": assessment.horizon,
                "importance": dict(assessment.importance),
                "supporting": assessment.supporting_evidence_ids,
                "contradicting": assessment.contradicting_evidence_ids,
            },
            "evidence": evidence_fingerprint,
            "prompt": "research_pattern_discovery_v2",
        }
    )


def _case_row(session: Any, case_id: str) -> dict[str, Any] | None:
    return result_first(
        session.execute(
            text("SELECT * FROM research_cases WHERE id = :case_id LIMIT 1"),
            {"case_id": case_id},
        )
    )


def _attach_case_aliases(
    session: Any, case_id: str, aliases: Sequence[str], now: datetime
) -> None:
    for alias in aliases[:30]:
        normalized = "-".join(
            token
            for token in str(alias).casefold().replace("_", "-").split("-")
            if token
        )[:200]
        if not normalized:
            continue
        session.execute(
            text(
                """
                INSERT INTO research_case_aliases (
                    case_id, alias, normalized_alias, created_at
                ) VALUES (:case_id, :alias, :normalized_alias, :created_at)
                ON CONFLICT (case_id, normalized_alias) DO NOTHING
                """
            ),
            {
                "case_id": case_id,
                "alias": str(alias)[:160],
                "normalized_alias": normalized,
                "created_at": now,
            },
        )


def _attach_case_evidence(
    session: Any,
    case_id: str,
    assessment: PatternAssessment,
    evidence: Sequence[NormalizedEvidence],
) -> None:
    relationships: dict[str, str] = {}
    relationships.update(
        {ref: "supports" for ref in assessment.supporting_evidence_ids}
    )
    relationships.update(
        {ref: "contradicts" for ref in assessment.contradicting_evidence_ids}
    )
    relationships.update({ref: "context" for ref in assessment.context_evidence_ids})
    for item in evidence[:100]:
        relationship = relationships.get(item.ref, "context")
        session.execute(
            text(
                """
                INSERT INTO research_case_evidence (
                    case_id, evidence_type, evidence_id, source_name, title,
                    source_reference, relationship, evidence_fingerprint,
                    source_timestamp, excerpt
                ) VALUES (
                    :case_id, :evidence_type, :evidence_id, :source_name, :title,
                    :source_reference, :relationship, :evidence_fingerprint,
                    :source_timestamp, :excerpt
                ) ON CONFLICT DO NOTHING
                """
            ),
            {
                "case_id": case_id,
                "evidence_type": item.evidence_type,
                "evidence_id": item.evidence_id,
                "source_name": item.source_name,
                "title": item.title,
                "source_reference": item.source_reference,
                "relationship": relationship,
                "evidence_fingerprint": item.content_fingerprint,
                "source_timestamp": item.source_timestamp,
                "excerpt": item.bounded_excerpt,
            },
        )


def _attach_case_entities(
    session: Any,
    case_id: str,
    assessment: PatternAssessment,
    now: datetime,
) -> None:
    entities = list(assessment.entities)
    known = {(item.entity_type, item.normalized_key) for item in entities}
    for industry in assessment.industries:
        from research_intelligence.relationships import normalize_entity

        entity = normalize_entity("industry", industry)
        if (entity.entity_type, entity.normalized_key) not in known:
            known.add((entity.entity_type, entity.normalized_key))
            entities.append(entity)
    for entity in entities[:100]:
        session.execute(
            text(
                """
                INSERT INTO research_case_entities (
                    case_id, entity_type, normalized_key, display_name,
                    first_seen_at, last_seen_at
                ) VALUES (
                    :case_id, :entity_type, :normalized_key, :display_name,
                    :now, :now
                ) ON CONFLICT (case_id, entity_type, normalized_key) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    last_seen_at = EXCLUDED.last_seen_at
                """
            ),
            {
                "case_id": case_id,
                "entity_type": entity.entity_type,
                "normalized_key": entity.normalized_key,
                "display_name": entity.display_name,
                "now": now,
            },
        )


def upsert_case(
    session: Any,
    assessment: PatternAssessment,
    evidence: Sequence[NormalizedEvidence],
    *,
    evidence_input_fingerprint: str,
    provenance: ModelProvenance,
    correlation_id: str | None,
    matched_case: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> CaseMutation:
    if not evidence:
        raise ValueError("research case requires evidence")
    effective_now = _utc(now)
    input_fingerprint = _case_input_fingerprint(assessment, evidence_input_fingerprint)
    existing = None
    if matched_case is not None and matched_case.get("id"):
        existing = _case_row(session, _uuid(matched_case["id"]))
    if existing is None:
        existing = result_first(
            session.execute(
                text(
                    "SELECT * FROM research_cases WHERE semantic_fingerprint = :fingerprint LIMIT 1"
                ),
                {"fingerprint": assessment.semantic_fingerprint},
            )
        )
    created = existing is None
    first_seen = min(item.source_timestamp for item in evidence)
    last_evidence = max(item.source_timestamp for item in evidence)
    importance = dict(assessment.importance)
    params = {
        "semantic_fingerprint": assessment.semantic_fingerprint,
        "title": assessment.label,
        "definition": assessment.definition,
        "horizon": assessment.horizon,
        "case_type": assessment.case_type,
        **importance,
        "importance_rationale": _json(dict(assessment.importance_rationale)),
        "first_seen_at": first_seen,
        "last_evidence_at": last_evidence,
        "last_changed_at": effective_now,
        "input_fingerprint": input_fingerprint,
        "model_slug": provenance.model_slug,
        "prompt_version": provenance.prompt_version,
        "generation_attempt_id": provenance.generation_attempt_id,
        "correlation_id": correlation_id,
        "now": effective_now,
    }
    if created:
        inserted = result_first(
            session.execute(
                text(
                    """
                INSERT INTO research_cases (
                    semantic_fingerprint, title, definition, horizon,
                    case_type, lifecycle_state, origin,
                    economic_significance, market_sensitivity, persistence,
                    breadth, investability, evidence_strength, time_sensitivity,
                    importance_rationale, first_seen_at, last_evidence_at,
                    last_changed_at, input_fingerprint, model_slug,
                    prompt_version, generation_attempt_id, correlation_id,
                    created_at, updated_at
                ) VALUES (
                    :semantic_fingerprint, :title, :definition, :horizon,
                    :case_type, 'candidate', 'discovered',
                    :economic_significance, :market_sensitivity, :persistence,
                    :breadth, :investability, :evidence_strength,
                    :time_sensitivity, CAST(:importance_rationale AS JSONB),
                    :first_seen_at, :last_evidence_at, :last_changed_at,
                    :input_fingerprint, :model_slug, :prompt_version,
                    :generation_attempt_id, :correlation_id, :now, :now
                ) ON CONFLICT (semantic_fingerprint) DO NOTHING
                RETURNING id
                """
                ),
                params,
            )
        )
        if inserted is None:
            existing = result_first(
                session.execute(
                    text(
                        "SELECT * FROM research_cases WHERE semantic_fingerprint = :fingerprint LIMIT 1"
                    ),
                    {"fingerprint": assessment.semantic_fingerprint},
                )
            )
            created = False
        else:
            existing = {"id": inserted["id"], "input_fingerprint": None, "title": None}
    if existing is None:
        raise RuntimeError("research case insert did not return an identity")
    case_id = _uuid(existing["id"])
    changed = existing.get("input_fingerprint") != input_fingerprint
    prior_title = str(existing.get("title") or "").strip()
    if not created:
        session.execute(
            text(
                """
                UPDATE research_cases SET
                    title = :title,
                    definition = :definition,
                    horizon = :horizon,
                    case_type = :case_type,
                    economic_significance = :economic_significance,
                    market_sensitivity = :market_sensitivity,
                    persistence = :persistence,
                    breadth = :breadth,
                    investability = :investability,
                    evidence_strength = :evidence_strength,
                    time_sensitivity = :time_sensitivity,
                    importance_rationale = CAST(:importance_rationale AS JSONB),
                    first_seen_at = LEAST(first_seen_at, :first_seen_at),
                    last_evidence_at = GREATEST(last_evidence_at, :last_evidence_at),
                    last_changed_at = CASE
                        WHEN input_fingerprint <> :input_fingerprint THEN :last_changed_at
                        ELSE last_changed_at
                    END,
                    input_fingerprint = :input_fingerprint,
                    model_slug = :model_slug,
                    prompt_version = :prompt_version,
                    generation_attempt_id = :generation_attempt_id,
                    correlation_id = :correlation_id,
                    updated_at = :now
                WHERE id = :case_id
                """
            ),
            {**params, "case_id": case_id},
        )
    aliases = [*assessment.aliases]
    if prior_title and prior_title.casefold() != assessment.label.casefold():
        aliases.append(prior_title)
    _attach_case_aliases(session, case_id, aliases, effective_now)
    _attach_case_entities(session, case_id, assessment, effective_now)
    _attach_case_evidence(session, case_id, assessment, evidence)
    return CaseMutation(case_id, created, changed or created, input_fingerprint)


def load_case_stats(session: Any, case_id: str) -> CaseStats:
    parsed = _uuid(case_id)
    row = result_first(
        session.execute(
            text(
                """
            SELECT c.first_seen_at, c.last_evidence_at,
                   COUNT(DISTINCT (e.evidence_type, e.evidence_id)) AS evidence_count,
                   COUNT(DISTINCT e.source_name) AS source_diversity,
                   (SELECT COUNT(*) FROM research_case_snapshots s WHERE s.case_id = c.id) AS snapshot_count,
                   EXISTS(SELECT 1 FROM research_causal_edges x WHERE x.case_id = c.id AND x.superseded_at IS NULL) AS has_causal_chain,
                   EXISTS(SELECT 1 FROM research_value_capture_assessments v WHERE v.case_id = c.id AND v.superseded_at IS NULL) AS has_value_capture,
                   EXISTS(
                       SELECT 1 FROM research_case_snapshots sa
                       WHERE sa.case_id = c.id
                         AND JSONB_TYPEOF(sa.payload->'adversarial') = 'object'
                   ) AS has_adversarial_review,
                   EXISTS(
                       SELECT 1 FROM research_case_snapshots sd
                       WHERE sd.case_id = c.id
                         AND JSONB_TYPEOF(sd.payload->'deliverable') = 'object'
                   ) AS has_deliverable
            FROM research_cases c
            LEFT JOIN research_case_evidence e ON e.case_id = c.id
            WHERE c.id = :case_id
            GROUP BY c.id
            """
            ),
            {"case_id": parsed},
        )
    )
    if row is None:
        raise ValueError("research case not found")
    first_seen = _utc(row.get("first_seen_at"))
    last_evidence = _utc(row.get("last_evidence_at"))
    return CaseStats(
        evidence_count=int(row.get("evidence_count") or 0),
        source_diversity=int(row.get("source_diversity") or 0),
        persistence_days=max(0, (last_evidence - first_seen).days),
        snapshot_count=int(row.get("snapshot_count") or 0),
        has_causal_chain=bool(row.get("has_causal_chain")),
        has_value_capture=bool(row.get("has_value_capture")),
        has_adversarial_review=bool(row.get("has_adversarial_review")),
        has_deliverable=bool(row.get("has_deliverable")),
        last_evidence_at=last_evidence,
    )


def persist_causal_edges(
    session: Any,
    case_id: str,
    edges: Sequence[CausalEdgeDraft],
    evidence: Sequence[NormalizedEvidence],
    provenance: ModelProvenance,
) -> tuple[str, ...]:
    parsed_case = _uuid(case_id)
    catalog = evidence_catalog(evidence)
    inserted_ids: list[str] = []
    now = datetime.now(UTC)
    active_fingerprints: set[str] = set()
    for edge in edges[:400]:
        fingerprint = causal_edge_fingerprint(
            from_type=edge.from_type,
            from_key=edge.from_key,
            relationship=edge.relationship,
            to_type=edge.to_type,
            to_key=edge.to_key,
        )
        active_fingerprints.add(fingerprint)
        current = result_first(
            session.execute(
                text(
                    """
                SELECT id, input_fingerprint FROM research_causal_edges
                WHERE case_id = :case_id AND edge_fingerprint = :fingerprint
                  AND superseded_at IS NULL LIMIT 1
                """
                ),
                {"case_id": parsed_case, "fingerprint": fingerprint},
            )
        )
        input_fingerprint = provenance.input_fingerprint or canonical_fingerprint(
            {
                "edge": fingerprint,
                "evidence": edge.evidence_ids,
                "mechanism": edge.mechanism,
            }
        )
        if current and current.get("input_fingerprint") == input_fingerprint:
            inserted_ids.append(str(current["id"]))
            continue
        if current:
            session.execute(
                text(
                    "UPDATE research_causal_edges SET superseded_at = :now WHERE id = :id"
                ),
                {"now": now, "id": current["id"]},
            )
        inserted = result_first(
            session.execute(
                text(
                    """
                INSERT INTO research_causal_edges (
                    case_id, edge_fingerprint, from_type, from_key, from_name,
                    relationship, to_type, to_key, to_name, mechanism,
                    epistemic_state, confidence, missing_evidence,
                    break_conditions, depth, valid_from, valid_to,
                    input_fingerprint, model_slug, prompt_version,
                    generation_attempt_id
                ) VALUES (
                    :case_id, :edge_fingerprint, :from_type, :from_key,
                    :from_name, :relationship, :to_type, :to_key, :to_name,
                    :mechanism, :epistemic_state, :confidence,
                    :missing_evidence, :break_conditions, :depth,
                    :valid_from, :valid_to, :input_fingerprint, :model_slug,
                    :prompt_version, :generation_attempt_id
                ) RETURNING id
                """
                ),
                {
                    "case_id": parsed_case,
                    "edge_fingerprint": fingerprint,
                    "from_type": edge.from_type,
                    "from_key": edge.from_key,
                    "from_name": edge.from_name,
                    "relationship": edge.relationship,
                    "to_type": edge.to_type,
                    "to_key": edge.to_key,
                    "to_name": edge.to_name,
                    "mechanism": edge.mechanism,
                    "epistemic_state": edge.epistemic_state,
                    "confidence": edge.confidence,
                    "missing_evidence": list(edge.missing_evidence),
                    "break_conditions": list(edge.break_conditions),
                    "depth": edge.depth,
                    "valid_from": edge.valid_from,
                    "valid_to": edge.valid_to,
                    "input_fingerprint": input_fingerprint,
                    "model_slug": provenance.model_slug,
                    "prompt_version": provenance.prompt_version,
                    "generation_attempt_id": provenance.generation_attempt_id,
                },
            )
        )
        if inserted is None:
            raise RuntimeError("causal edge insert did not return an identity")
        edge_id = str(inserted["id"])
        inserted_ids.append(edge_id)
        for reference in edge.evidence_ids:
            item = catalog[reference]
            session.execute(
                text(
                    """
                    INSERT INTO research_causal_edge_evidence (
                        edge_id, evidence_type, evidence_id, relationship, excerpt
                    ) VALUES (
                        :edge_id, :evidence_type, :evidence_id, 'supports', :excerpt
                    ) ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "edge_id": edge_id,
                    "evidence_type": item.evidence_type,
                    "evidence_id": item.evidence_id,
                    "excerpt": item.bounded_excerpt,
                },
            )
    current_edges = result_rows(
        session.execute(
            text(
                """
            SELECT id, edge_fingerprint FROM research_causal_edges
            WHERE case_id = :case_id AND superseded_at IS NULL
            """
            ),
            {"case_id": parsed_case},
        )
    )
    for row in current_edges:
        if row.get("edge_fingerprint") not in active_fingerprints:
            session.execute(
                text(
                    "UPDATE research_causal_edges SET superseded_at = :now WHERE id = :id"
                ),
                {"now": now, "id": row["id"]},
            )
    return tuple(inserted_ids)


def persist_value_capture(
    session: Any,
    case_id: str,
    assessments: Sequence[ValueCaptureDraft],
    evidence: Sequence[NormalizedEvidence],
    provenance: ModelProvenance,
) -> tuple[tuple[str, str], ...]:
    parsed_case = _uuid(case_id)
    catalog = evidence_catalog(evidence)
    now = datetime.now(UTC)
    nodes: list[tuple[str, str]] = []
    active_nodes: set[tuple[str, str]] = set()
    dimension_columns = ", ".join(VALUE_CAPTURE_DIMENSIONS)
    dimension_params = ", ".join(f":{name}" for name in VALUE_CAPTURE_DIMENSIONS)
    for assessment in assessments[:100]:
        identity = {
            "case_id": parsed_case,
            "node_type": assessment.node_type,
            "node_key": assessment.node_key,
        }
        active_nodes.add((assessment.node_type, assessment.node_key))
        input_fingerprint = provenance.input_fingerprint or canonical_fingerprint(
            {
                "node": identity,
                "dimensions": dict(assessment.dimensions),
                "evidence": assessment.evidence_ids,
            }
        )
        current = result_first(
            session.execute(
                text(
                    """
                SELECT id, input_fingerprint FROM research_value_capture_assessments
                WHERE case_id = :case_id AND node_type = :node_type
                  AND node_key = :node_key AND superseded_at IS NULL LIMIT 1
                """
                ),
                identity,
            )
        )
        if current and current.get("input_fingerprint") == input_fingerprint:
            nodes.append((assessment.node_type, assessment.node_key))
            continue
        if current:
            session.execute(
                text(
                    "UPDATE research_value_capture_assessments SET superseded_at = :now WHERE id = :id"
                ),
                {"now": now, "id": current["id"]},
            )
        inserted = result_first(
            session.execute(
                text(
                    f"""
                INSERT INTO research_value_capture_assessments (
                    case_id, node_type, node_key, node_name,
                    {dimension_columns}, assessment_rationale, unknowns,
                    input_fingerprint, model_slug, prompt_version,
                    generation_attempt_id
                ) VALUES (
                    :case_id, :node_type, :node_key, :node_name,
                    {dimension_params}, CAST(:assessment_rationale AS JSONB),
                    :unknowns, :input_fingerprint, :model_slug,
                    :prompt_version, :generation_attempt_id
                ) RETURNING id
                """
                ),
                {
                    **identity,
                    "node_name": assessment.node_name,
                    **dict(assessment.dimensions),
                    "assessment_rationale": _json(dict(assessment.rationale)),
                    "unknowns": list(assessment.unknowns),
                    "input_fingerprint": input_fingerprint,
                    "model_slug": provenance.model_slug,
                    "prompt_version": provenance.prompt_version,
                    "generation_attempt_id": provenance.generation_attempt_id,
                },
            )
        )
        if inserted is None:
            raise RuntimeError("value-capture insert did not return an identity")
        assessment_id = str(inserted["id"])
        for reference in assessment.evidence_ids:
            item = catalog[reference]
            session.execute(
                text(
                    """
                    INSERT INTO research_value_capture_evidence (
                        assessment_id, evidence_type, evidence_id, excerpt
                    ) VALUES (:assessment_id, :evidence_type, :evidence_id, :excerpt)
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "assessment_id": assessment_id,
                    "evidence_type": item.evidence_type,
                    "evidence_id": item.evidence_id,
                    "excerpt": item.bounded_excerpt,
                },
            )
        nodes.append((assessment.node_type, assessment.node_key))
    current_assessments = result_rows(
        session.execute(
            text(
                """
            SELECT id, node_type, node_key
            FROM research_value_capture_assessments
            WHERE case_id = :case_id AND superseded_at IS NULL
            """
            ),
            {"case_id": parsed_case},
        )
    )
    for row in current_assessments:
        if (row.get("node_type"), row.get("node_key")) not in active_nodes:
            session.execute(
                text(
                    "UPDATE research_value_capture_assessments SET superseded_at = :now WHERE id = :id"
                ),
                {"now": now, "id": row["id"]},
            )
    return tuple(nodes)


def ensure_hypothesis_data_requests(
    session: Any,
    case_id: str,
    edges: Sequence[CausalEdgeDraft],
    evidence: Sequence[NormalizedEvidence],
) -> dict[str, int]:
    """Create one resolvable cold-data request for every current hypothesis edge."""
    parsed_case = _uuid(case_id)
    created = 0
    matched = 0
    for edge in edges:
        fingerprint = causal_edge_fingerprint(
            from_type=edge.from_type,
            from_key=edge.from_key,
            relationship=edge.relationship,
            to_type=edge.to_type,
            to_key=edge.to_key,
        )
        current = result_first(
            session.execute(
                text(
                    """
                SELECT id FROM research_causal_edges
                WHERE case_id = :case_id AND edge_fingerprint = :fingerprint
                  AND superseded_at IS NULL
                LIMIT 1
                """
                ),
                {"case_id": parsed_case, "fingerprint": fingerprint},
            )
        )
        if current is None:
            continue
        if edge.epistemic_state != "hypothesis":
            resolved_status = (
                "satisfied"
                if edge.epistemic_state in {"observed", "supported"}
                else "obsolete"
            )
            session.execute(
                text(
                    """
                    UPDATE research_data_requests
                    SET status = :status, last_reconsidered_at = NOW(),
                        updated_at = NOW()
                    WHERE case_id = :case_id
                      AND causal_edge_id IN (
                          SELECT id FROM research_causal_edges
                          WHERE case_id = :case_id
                            AND edge_fingerprint = :fingerprint
                      )
                      AND status IN (
                          'unresolved', 'in_progress', 'partially_satisfied'
                      )
                    """
                ),
                {
                    "case_id": parsed_case,
                    "fingerprint": fingerprint,
                    "status": resolved_status,
                },
            )
            continue
        missing = list(edge.missing_evidence) or [
            f"Independent evidence testing {edge.from_name} "
            f"{edge.relationship.replace('_', ' ')} {edge.to_name}."
        ]
        weakening = list(edge.break_conditions) or [
            f"{edge.to_name} changes independently of {edge.from_name}."
        ]
        evidence_type = (
            "industry_capacity"
            if edge.relationship
            in {"constrains", "raises_supply_of", "reduces_supply_of"}
            else "supply_chain"
        )
        request_fingerprint = canonical_fingerprint(
            {
                "edge_fingerprint": fingerprint,
                "requested_evidence_type": evidence_type,
                "support_criteria": missing,
            }
        )
        result = session.execute(
            text(
                """
                INSERT INTO research_data_requests (
                    case_id, request_fingerprint, subject,
                    requested_evidence_type, reason, desired_frequency,
                    priority, status, candidate_source_class, input_fingerprint,
                    prompt_version, causal_edge_id, support_criteria,
                    weakening_criteria, minimum_independent_sources,
                    last_reconsidered_at
                ) VALUES (
                    :case_id, :request_fingerprint, :subject,
                    :requested_evidence_type, :reason, 'monthly',
                    :priority, 'unresolved', 'industry', :input_fingerprint,
                    'deterministic_hypothesis_request_v1', :causal_edge_id,
                    :support_criteria, :weakening_criteria, 2, NOW()
                ) ON CONFLICT (case_id, request_fingerprint) DO UPDATE SET
                    causal_edge_id = EXCLUDED.causal_edge_id,
                    support_criteria = EXCLUDED.support_criteria,
                    weakening_criteria = EXCLUDED.weakening_criteria,
                    last_reconsidered_at = NOW(),
                    updated_at = NOW()
                """
            ),
            {
                "case_id": parsed_case,
                "request_fingerprint": request_fingerprint,
                "subject": f"{edge.from_name} -> {edge.to_name}",
                "requested_evidence_type": evidence_type,
                "reason": f"Resolve hypothesis edge: {edge.mechanism}",
                "priority": "high" if edge.depth <= 2 else "moderate",
                "input_fingerprint": request_fingerprint,
                "causal_edge_id": current["id"],
                "support_criteria": missing,
                "weakening_criteria": weakening,
            },
        )
        created += max(0, int(getattr(result, "rowcount", 0) or 0))

    open_requests = result_rows(
        session.execute(
            text(
                """
            SELECT id, subject, requested_evidence_type, created_at,
                   minimum_independent_sources
            FROM research_data_requests
            WHERE case_id = :case_id
              AND status IN (
                  'unresolved', 'in_progress', 'partially_satisfied'
              )
            ORDER BY created_at, id
            LIMIT 100
            """
            ),
            {"case_id": parsed_case},
        )
    )
    for request in open_requests:
        requested_type = str(request.get("requested_evidence_type") or "")
        subject = str(request.get("subject") or "")
        created_at = _utc(request.get("created_at"))
        for item in evidence[:200]:
            if item.available_at <= created_at:
                continue
            data_types = item.structured_fields.get("research_data_types")
            typed_match = isinstance(data_types, list) and requested_type in data_types
            semantic_match = (
                token_similarity(
                    subject,
                    f"{item.title} {item.bounded_excerpt or ''}",
                )
                >= 0.35
            )
            if not typed_match and not semantic_match:
                continue
            result = session.execute(
                text(
                    """
                    INSERT INTO research_data_request_evidence (
                        request_id, evidence_type, evidence_id, source_name,
                        match_reason
                    ) VALUES (
                        :request_id, :evidence_type, :evidence_id, :source_name,
                        :match_reason
                    ) ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "request_id": request["id"],
                    "evidence_type": item.evidence_type,
                    "evidence_id": item.evidence_id,
                    "source_name": item.source_name,
                    "match_reason": (
                        f"evidence type {requested_type}"
                        if typed_match
                        else "deterministic subject similarity"
                    ),
                },
            )
            matched += max(0, int(getattr(result, "rowcount", 0) or 0))
        session.execute(
            text(
                """
                UPDATE research_data_requests AS r
                SET status = CASE
                        WHEN matched.source_count >= r.minimum_independent_sources
                            THEN 'satisfied'
                        WHEN matched.source_count > 0 THEN 'partially_satisfied'
                        ELSE r.status
                    END,
                    last_reconsidered_at = NOW(),
                    updated_at = NOW()
                FROM (
                    SELECT request_id, COUNT(DISTINCT source_name) AS source_count
                    FROM research_data_request_evidence
                    WHERE request_id = :request_id
                    GROUP BY request_id
                ) AS matched
                WHERE r.id = matched.request_id
                """
            ),
            {"request_id": request["id"]},
        )
    return {"requests": created, "evidence_matches": matched}


def unresolved_material_hypotheses(session: Any, case_id: str) -> int:
    parsed_case = _uuid(case_id)
    row = result_first(
        session.execute(
            text(
                """
            SELECT COUNT(*) AS count
            FROM research_causal_edges AS e
            WHERE e.case_id = :case_id
              AND e.superseded_at IS NULL
              AND e.epistemic_state = 'hypothesis'
            """
            ),
            {"case_id": parsed_case},
        )
    )
    return int(row.get("count") or 0) if row else 0


def persist_adversarial(
    session: Any,
    case_id: str,
    assessment: AdversarialAssessment,
    evidence: Sequence[NormalizedEvidence],
    provenance: ModelProvenance,
) -> dict[str, int]:
    parsed_case = _uuid(case_id)
    catalog = evidence_catalog(evidence)
    counters = 0
    requests = 0
    for counter in assessment.counterevidence[:50]:
        edge_id = None
        if counter.edge_fingerprint:
            edge = result_first(
                session.execute(
                    text(
                        """
                    SELECT id FROM research_causal_edges
                    WHERE case_id = :case_id AND edge_fingerprint = :fingerprint
                      AND superseded_at IS NULL LIMIT 1
                    """
                    ),
                    {"case_id": parsed_case, "fingerprint": counter.edge_fingerprint},
                )
            )
            edge_id = edge.get("id") if edge else None
        first_item = catalog[counter.evidence_ids[0]] if counter.evidence_ids else None
        inserted = result_first(
            session.execute(
                text(
                    """
                INSERT INTO research_counterevidence (
                    case_id, counter_fingerprint, kind, statement,
                    epistemic_state, evidence_type, evidence_id, edge_id,
                    rationale, input_fingerprint, model_slug, prompt_version,
                    generation_attempt_id
                ) VALUES (
                    :case_id, :counter_fingerprint, :kind, :statement,
                    :epistemic_state, :evidence_type, :evidence_id, :edge_id,
                    :rationale, :input_fingerprint, :model_slug,
                    :prompt_version, :generation_attempt_id
                ) ON CONFLICT (case_id, counter_fingerprint) DO NOTHING
                RETURNING id
                """
                ),
                {
                    "case_id": parsed_case,
                    "counter_fingerprint": counter.counter_fingerprint,
                    "kind": counter.kind,
                    "statement": counter.statement,
                    "epistemic_state": counter.epistemic_state,
                    "evidence_type": first_item.evidence_type if first_item else None,
                    "evidence_id": first_item.evidence_id if first_item else None,
                    "edge_id": edge_id,
                    "rationale": counter.rationale,
                    "input_fingerprint": provenance.input_fingerprint
                    or counter.counter_fingerprint,
                    "model_slug": provenance.model_slug,
                    "prompt_version": provenance.prompt_version,
                    "generation_attempt_id": provenance.generation_attempt_id,
                },
            )
        )
        if inserted is None:
            inserted = result_first(
                session.execute(
                    text(
                        """
                    SELECT id FROM research_counterevidence
                    WHERE case_id = :case_id AND counter_fingerprint = :fingerprint
                    LIMIT 1
                    """
                    ),
                    {
                        "case_id": parsed_case,
                        "fingerprint": counter.counter_fingerprint,
                    },
                )
            )
        if inserted is None:
            continue
        counter_id = str(inserted["id"])
        counters += 1
        for reference in counter.evidence_ids:
            item = catalog[reference]
            session.execute(
                text(
                    """
                    INSERT INTO research_counterevidence_evidence (
                        counterevidence_id, evidence_type, evidence_id, excerpt
                    ) VALUES (
                        :counterevidence_id, :evidence_type, :evidence_id, :excerpt
                    ) ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "counterevidence_id": counter_id,
                    "evidence_type": item.evidence_type,
                    "evidence_id": item.evidence_id,
                    "excerpt": item.bounded_excerpt,
                },
            )
    weakest_edge_id = None
    if assessment.weakest_edge_fingerprint:
        weakest = result_first(
            session.execute(
                text(
                    """
                SELECT id FROM research_causal_edges
                WHERE case_id = :case_id AND edge_fingerprint = :fingerprint
                  AND superseded_at IS NULL LIMIT 1
                """
                ),
                {
                    "case_id": parsed_case,
                    "fingerprint": assessment.weakest_edge_fingerprint,
                },
            )
        )
        weakest_edge_id = weakest.get("id") if weakest else None
    for request in assessment.data_requests[:50]:
        result = session.execute(
            text(
                """
                INSERT INTO research_data_requests (
                    case_id, request_fingerprint, subject,
                    requested_evidence_type, reason, desired_frequency,
                    priority, status, candidate_source_class, input_fingerprint,
                    model_slug, prompt_version, generation_attempt_id,
                    causal_edge_id, support_criteria, weakening_criteria,
                    minimum_independent_sources, last_reconsidered_at
                ) VALUES (
                    :case_id, :request_fingerprint, :subject,
                    :requested_evidence_type, :reason, :desired_frequency,
                    :priority, 'unresolved', :candidate_source_class,
                    :input_fingerprint, :model_slug, :prompt_version,
                    :generation_attempt_id, :causal_edge_id, :support_criteria,
                    :weakening_criteria, 2, NOW()
                ) ON CONFLICT (case_id, request_fingerprint) DO UPDATE SET
                    priority = EXCLUDED.priority,
                    causal_edge_id = COALESCE(
                        EXCLUDED.causal_edge_id,
                        research_data_requests.causal_edge_id
                    ),
                    support_criteria = EXCLUDED.support_criteria,
                    weakening_criteria = EXCLUDED.weakening_criteria,
                    last_reconsidered_at = NOW(),
                    updated_at = NOW()
                """
            ),
            {
                "case_id": parsed_case,
                "request_fingerprint": request.request_fingerprint,
                "subject": request.subject,
                "requested_evidence_type": request.requested_evidence_type,
                "reason": request.reason,
                "desired_frequency": request.desired_frequency,
                "priority": request.priority,
                "candidate_source_class": request.candidate_source_class,
                "input_fingerprint": provenance.input_fingerprint
                or request.request_fingerprint,
                "model_slug": provenance.model_slug,
                "prompt_version": provenance.prompt_version,
                "generation_attempt_id": provenance.generation_attempt_id,
                "causal_edge_id": weakest_edge_id,
                "support_criteria": [request.reason],
                "weakening_criteria": list(assessment.invalidation_conditions),
            },
        )
        requests += max(0, int(getattr(result, "rowcount", 0) or 0))
    return {"counterevidence": counters, "data_requests": requests}


def publish_case_snapshot(
    session: Any,
    case_id: str,
    *,
    lifecycle_state: str,
    payload: Mapping[str, Any],
    input_fingerprint: str,
    change_summary: str,
    provenance: ModelProvenance,
    correlation_id: str | None,
) -> SnapshotMutation:
    parsed_case = _uuid(case_id)
    existing = result_first(
        session.execute(
            text(
                """
            SELECT id, version FROM research_case_snapshots
            WHERE case_id = :case_id AND input_fingerprint = :input_fingerprint
            LIMIT 1
            """
            ),
            {"case_id": parsed_case, "input_fingerprint": input_fingerprint},
        )
    )
    if existing:
        return SnapshotMutation(str(existing["id"]), int(existing["version"]), False)
    case = result_first(
        session.execute(
            text(
                "SELECT current_version FROM research_cases WHERE id = :case_id FOR UPDATE"
            ),
            {"case_id": parsed_case},
        )
    )
    if case is None:
        raise ValueError("research case not found")
    version = int(case.get("current_version") or 0) + 1
    inserted = result_first(
        session.execute(
            text(
                """
            INSERT INTO research_case_snapshots (
                case_id, version, input_fingerprint, lifecycle_state,
                change_summary, payload, model_slug, prompt_version,
                generation_attempt_id, correlation_id
            ) VALUES (
                :case_id, :version, :input_fingerprint, :lifecycle_state,
                :change_summary, CAST(:payload AS JSONB), :model_slug,
                :prompt_version, :generation_attempt_id, :correlation_id
            ) RETURNING id
            """
            ),
            {
                "case_id": parsed_case,
                "version": version,
                "input_fingerprint": input_fingerprint,
                "lifecycle_state": lifecycle_state,
                "change_summary": change_summary[:500],
                "payload": _json(dict(payload)),
                "model_slug": provenance.model_slug,
                "prompt_version": provenance.prompt_version,
                "generation_attempt_id": provenance.generation_attempt_id,
                "correlation_id": correlation_id,
            },
        )
    )
    if inserted is None:
        raise RuntimeError("case snapshot insert did not return an identity")
    session.execute(
        text(
            """
            UPDATE research_cases SET current_version = :version,
                lifecycle_state = :lifecycle_state,
                last_changed_at = NOW(), updated_at = NOW()
            WHERE id = :case_id
            """
        ),
        {
            "version": version,
            "lifecycle_state": lifecycle_state,
            "case_id": parsed_case,
        },
    )
    return SnapshotMutation(str(inserted["id"]), version, True)


def refresh_case_lifecycles(
    session: Any,
    settings: ResearchSettings,
    *,
    correlation_id: str | None = None,
    now: datetime | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Apply inactivity and evidence thresholds to active cases without model work."""
    effective_now = _utc(now)
    bounded = max(1, min(int(limit), 1_000))
    rows = result_rows(
        session.execute(
            text(
                """
            SELECT c.id, c.lifecycle_state, c.first_seen_at,
                   c.last_evidence_at, c.input_fingerprint,
                   (SELECT COUNT(DISTINCT (e.evidence_type, e.evidence_id))
                    FROM research_case_evidence e
                    WHERE e.case_id = c.id) AS evidence_count,
                   (SELECT COUNT(DISTINCT e.source_name)
                    FROM research_case_evidence e
                    WHERE e.case_id = c.id) AS source_diversity,
                   (SELECT COUNT(*) FROM research_case_snapshots s
                    WHERE s.case_id = c.id) AS snapshot_count,
                   EXISTS(
                       SELECT 1 FROM research_causal_edges x
                       WHERE x.case_id = c.id AND x.superseded_at IS NULL
                   ) AS has_causal_chain,
                   EXISTS(
                       SELECT 1 FROM research_value_capture_assessments v
                       WHERE v.case_id = c.id AND v.superseded_at IS NULL
                   ) AS has_value_capture,
                   EXISTS(
                       SELECT 1 FROM research_case_snapshots sa
                       WHERE sa.case_id = c.id
                         AND JSONB_TYPEOF(sa.payload->'adversarial') = 'object'
                   ) AS has_adversarial_review,
                   EXISTS(
                       SELECT 1 FROM research_case_snapshots sd
                       WHERE sd.case_id = c.id
                         AND JSONB_TYPEOF(sd.payload->'deliverable') = 'object'
                   ) AS has_deliverable,
                   EXISTS(
                       SELECT 1 FROM research_causal_edges h
                       WHERE h.case_id = c.id
                         AND h.superseded_at IS NULL
                         AND h.epistemic_state = 'hypothesis'
                   ) AS has_unresolved_hypothesis,
                   (SELECT s.payload FROM research_case_snapshots s
                    WHERE s.case_id = c.id AND s.version = c.current_version
                    LIMIT 1) AS current_payload
            FROM research_cases c
            WHERE c.lifecycle_state <> 'archived'
            ORDER BY c.last_evidence_at ASC, c.id
            LIMIT :limit
            """
            ),
            {"limit": bounded},
        )
    )
    transitions: list[dict[str, Any]] = []
    for row in rows:
        case_id = _uuid(row.get("id"))
        first_seen = _utc(row.get("first_seen_at"))
        last_evidence = _utc(row.get("last_evidence_at"))
        stats = CaseStats(
            evidence_count=int(row.get("evidence_count") or 0),
            source_diversity=int(row.get("source_diversity") or 0),
            persistence_days=max(0, (last_evidence - first_seen).days),
            snapshot_count=int(row.get("snapshot_count") or 0),
            has_causal_chain=bool(row.get("has_causal_chain")),
            has_value_capture=bool(row.get("has_value_capture")),
            has_adversarial_review=bool(row.get("has_adversarial_review")),
            has_deliverable=bool(row.get("has_deliverable")),
            last_evidence_at=last_evidence,
        )
        current = str(row.get("lifecycle_state") or "candidate")
        target = next_lifecycle_state(current, stats, settings, now=effective_now).value
        if row.get("has_unresolved_hypothesis") and target in {
            "research_ready",
            "mature",
        }:
            target = "corroborated"
        if target == current:
            continue
        payload = row.get("current_payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = {}
        snapshot_payload = dict(payload) if isinstance(payload, Mapping) else {}
        transition = {
            "from": current,
            "to": target,
            "at": effective_now.isoformat(),
            "reason": "configured_evidence_and_inactivity_thresholds",
        }
        snapshot_payload["lifecycle_state"] = target
        snapshot_payload["lifecycle_transition"] = transition
        fingerprint = canonical_fingerprint(
            {
                "operation": "deterministic_lifecycle_v1",
                "case_id": case_id,
                "prior_input_fingerprint": row.get("input_fingerprint"),
                "transition": transition,
                "thresholds": dict(settings.lifecycle_thresholds),
            }
        )
        mutation = publish_case_snapshot(
            session,
            case_id,
            lifecycle_state=target,
            payload=snapshot_payload,
            input_fingerprint=fingerprint,
            change_summary=f"Lifecycle changed from {current} to {target}",
            provenance=ModelProvenance(
                prompt_version="deterministic_lifecycle_v1",
                input_fingerprint=fingerprint,
                metadata={"deterministic": True},
            ),
            correlation_id=correlation_id,
        )
        if mutation.changed:
            transitions.append(
                {
                    "case_id": case_id,
                    "from": current,
                    "to": target,
                    "snapshot_version": mutation.version,
                }
            )
    return transitions


def persist_economic_factors(
    session: Any,
    factors: Sequence[EconomicFactorDraft],
    evidence: Sequence[NormalizedEvidence],
    provenance: ModelProvenance,
) -> tuple[dict[str, str], int]:
    """Version shared factor state once before target-specific projections."""
    catalog = evidence_catalog(evidence)
    now = datetime.now(UTC)
    factor_ids: dict[str, str] = {}
    changed = 0
    for factor in factors[:8]:
        input_fingerprint = canonical_fingerprint(
            {
                "factor_key": factor.factor_key,
                "factor_label": " ".join(factor.factor_label.split()).casefold(),
                "state": factor.state,
                "strength": factor.strength,
                "horizon": factor.horizon,
                "mechanism": " ".join(factor.mechanism.split()).casefold(),
                "evidence_ids": sorted(factor.evidence_ids),
                "confidence": factor.confidence,
                "confidence_rationale": " ".join(
                    factor.confidence_rationale.split()
                ).casefold(),
                "invalidation_conditions": sorted(
                    " ".join(value.split()).casefold()
                    for value in factor.invalidation_conditions
                ),
                "transmissions": sorted(
                    (
                        {
                            "target": item.target,
                            "direction": item.direction,
                            "mechanism": " ".join(item.mechanism.split()).casefold(),
                            "invalidation_conditions": sorted(
                                " ".join(value.split()).casefold()
                                for value in item.invalidation_conditions
                            ),
                        }
                        for item in factor.transmissions
                    ),
                    key=lambda item: item["target"],
                ),
            }
        )
        current = result_first(
            session.execute(
                text(
                    """
                SELECT id, input_fingerprint FROM research_economic_factors
                WHERE factor_key = :factor_key AND superseded_at IS NULL
                LIMIT 1
                """
                ),
                {"factor_key": factor.factor_key},
            )
        )
        if current and current.get("input_fingerprint") == input_fingerprint:
            factor_ids[factor.factor_key] = str(current["id"])
            continue
        if current:
            session.execute(
                text(
                    """
                    UPDATE research_economic_factors
                    SET superseded_at = :now, updated_at = :now
                    WHERE id = :id
                    """
                ),
                {"now": now, "id": current["id"]},
            )
        inserted = result_first(
            session.execute(
                text(
                    """
                INSERT INTO research_economic_factors (
                    factor_key, factor_label, state, strength, horizon,
                    mechanism, invalidation_conditions, confidence,
                    confidence_rationale, input_fingerprint, model_slug,
                    prompt_version, generation_attempt_id, valid_from
                ) VALUES (
                    :factor_key, :factor_label, :state, :strength, :horizon,
                    :mechanism, :invalidation_conditions, :confidence,
                    :confidence_rationale, :input_fingerprint, :model_slug,
                    :prompt_version, :generation_attempt_id, :valid_from
                ) RETURNING id
                """
                ),
                {
                    "factor_key": factor.factor_key,
                    "factor_label": factor.factor_label,
                    "state": factor.state,
                    "strength": factor.strength,
                    "horizon": factor.horizon,
                    "mechanism": factor.mechanism,
                    "invalidation_conditions": list(factor.invalidation_conditions),
                    "confidence": factor.confidence,
                    "confidence_rationale": factor.confidence_rationale,
                    "input_fingerprint": input_fingerprint,
                    "model_slug": provenance.model_slug,
                    "prompt_version": provenance.prompt_version
                    or "macro_transmission_v3",
                    "generation_attempt_id": provenance.generation_attempt_id,
                    "valid_from": now,
                },
            )
        )
        if inserted is None:
            raise RuntimeError("economic factor insert did not return an identity")
        factor_id = str(inserted["id"])
        factor_ids[factor.factor_key] = factor_id
        for reference in factor.evidence_ids:
            item = catalog[reference]
            session.execute(
                text(
                    """
                    INSERT INTO research_economic_factor_evidence (
                        factor_id, evidence_type, evidence_id, relationship, excerpt
                    ) VALUES (
                        :factor_id, :evidence_type, :evidence_id, 'supports', :excerpt
                    ) ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "factor_id": factor_id,
                    "evidence_type": item.evidence_type,
                    "evidence_id": item.evidence_id,
                    "excerpt": item.bounded_excerpt,
                },
            )
        for transmission in factor.transmissions:
            session.execute(
                text(
                    """
                    INSERT INTO research_factor_transmissions (
                        factor_id, target, direction, mechanism,
                        invalidation_conditions
                    ) VALUES (
                        :factor_id, :target, :direction, :mechanism,
                        :invalidation_conditions
                    )
                    """
                ),
                {
                    "factor_id": factor_id,
                    "target": transmission.target,
                    "direction": transmission.direction,
                    "mechanism": transmission.mechanism,
                    "invalidation_conditions": list(
                        transmission.invalidation_conditions
                    ),
                },
            )
        changed += 1
    return factor_ids, changed


def current_market_drivers(session: Any, limit: int = 100) -> list[dict[str, Any]]:
    bounded = max(1, min(int(limit), 200))
    rows = result_rows(
        session.execute(
            text(
                """
            SELECT d.*,
                   f.factor_label AS factor_label,
                   f.state AS factor_state,
                   f.mechanism AS factor_mechanism,
                   f.confidence AS factor_confidence,
                   f.invalidation_conditions AS factor_invalidation_conditions,
                   COALESCE(
                       ARRAY_AGG(e.evidence_type || ':' || e.evidence_id)
                           FILTER (WHERE e.evidence_id IS NOT NULL),
                       '{}'::TEXT[]
                   ) AS evidence_ids
            FROM research_market_drivers d
            LEFT JOIN research_economic_factors f ON f.id = d.factor_id
            LEFT JOIN research_market_driver_evidence e ON e.driver_id = d.id
            WHERE d.superseded_at IS NULL
            GROUP BY d.id, f.id
            ORDER BY d.changed_since_prior DESC, d.target, d.driver_key
            LIMIT :limit
            """
            ),
            {"limit": bounded},
        )
    )
    return rows


def persist_market_drivers(
    session: Any,
    drivers: Sequence[MarketDriverDraft],
    evidence: Sequence[NormalizedEvidence],
    provenance: ModelProvenance,
    factor_ids: Mapping[str, str] | None = None,
) -> int:
    catalog = evidence_catalog(evidence)
    changed = 0
    now = datetime.now(UTC)
    for driver in drivers[:200]:
        current = result_first(
            session.execute(
                text(
                    """
                SELECT id, input_fingerprint FROM research_market_drivers
                WHERE target = :target AND driver_key = :driver_key
                  AND superseded_at IS NULL LIMIT 1
                """
                ),
                {"target": driver.target, "driver_key": driver.driver_key},
            )
        )
        linked_factor_id = factor_ids.get(driver.driver_key) if factor_ids else None
        input_fingerprint = canonical_fingerprint(
            {
                "target": driver.target,
                "driver_key": driver.driver_key,
                "driver_label": " ".join(driver.driver_label.split()).casefold(),
                "direction": driver.direction,
                "strength": driver.strength,
                "horizon": driver.horizon,
                "mechanism": " ".join(driver.mechanism.split()).casefold(),
                "evidence_ids": sorted(driver.evidence_ids),
                "invalidation_conditions": sorted(
                    " ".join(value.split()).casefold()
                    for value in driver.invalidation_conditions
                ),
                "confidence": driver.confidence,
                "confidence_rationale": " ".join(
                    driver.confidence_rationale.split()
                ).casefold(),
                "factor_id": linked_factor_id,
            }
        )
        if current and current.get("input_fingerprint") == input_fingerprint:
            continue
        if current:
            session.execute(
                text(
                    "UPDATE research_market_drivers SET superseded_at = :now WHERE id = :id"
                ),
                {"now": now, "id": current["id"]},
            )
        inserted = result_first(
            session.execute(
                text(
                    """
                INSERT INTO research_market_drivers (
                    target, driver_key, driver_label, direction, strength,
                    horizon, mechanism, changed_since_prior,
                    invalidation_conditions, confidence,
                    confidence_rationale, input_fingerprint, model_slug,
                    prompt_version, generation_attempt_id, valid_from,
                    factor_id
                ) VALUES (
                    :target, :driver_key, :driver_label, :direction,
                    :strength, :horizon, :mechanism, :changed_since_prior,
                    :invalidation_conditions, :confidence,
                    :confidence_rationale, :input_fingerprint, :model_slug,
                    :prompt_version, :generation_attempt_id, :valid_from,
                    :factor_id
                ) RETURNING id
                """
                ),
                {
                    "target": driver.target,
                    "driver_key": driver.driver_key,
                    "driver_label": driver.driver_label,
                    "direction": driver.direction,
                    "strength": driver.strength,
                    "horizon": driver.horizon,
                    "mechanism": driver.mechanism,
                    "changed_since_prior": True,
                    "invalidation_conditions": list(driver.invalidation_conditions),
                    "confidence": driver.confidence,
                    "confidence_rationale": driver.confidence_rationale,
                    "input_fingerprint": input_fingerprint,
                    "model_slug": provenance.model_slug,
                    "prompt_version": provenance.prompt_version
                    or "macro_transmission_v3",
                    "generation_attempt_id": provenance.generation_attempt_id,
                    "valid_from": now,
                    "factor_id": linked_factor_id,
                },
            )
        )
        if inserted is None:
            raise RuntimeError("market driver insert did not return an identity")
        driver_id = str(inserted["id"])
        for reference in driver.evidence_ids:
            item = catalog[reference]
            session.execute(
                text(
                    """
                    INSERT INTO research_market_driver_evidence (
                        driver_id, evidence_type, evidence_id, relationship, excerpt
                    ) VALUES (
                        :driver_id, :evidence_type, :evidence_id, 'supports', :excerpt
                    ) ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "driver_id": driver_id,
                    "evidence_type": item.evidence_type,
                    "evidence_id": item.evidence_id,
                    "excerpt": item.bounded_excerpt,
                },
            )
        changed += 1
    return changed


def promote_case_to_theme(
    session: Any,
    case_id: str,
    *,
    similarity_threshold: float = 0.72,
) -> dict[str, Any] | None:
    parsed_case = _uuid(case_id)
    case = result_first(
        session.execute(
            text(
                """
            SELECT c.*, s.payload
            FROM research_cases c
            LEFT JOIN research_case_snapshots s
              ON s.case_id = c.id AND s.version = c.current_version
            WHERE c.id = :case_id LIMIT 1
            """
            ),
            {"case_id": parsed_case},
        )
    )
    if case is None or case.get("lifecycle_state") not in {"research_ready", "mature"}:
        return None
    existing = result_first(
        session.execute(
            text(
                "SELECT id, name, origin, source_case_id FROM investment_themes WHERE source_case_id = :case_id LIMIT 1"
            ),
            {"case_id": parsed_case},
        )
    )
    if existing:
        return {"theme_id": str(existing["id"]), "created": False, "matched": True}
    themes = result_rows(
        session.execute(
            text(
                "SELECT id, name, origin, source_case_id FROM investment_themes ORDER BY updated_at DESC LIMIT 100"
            )
        )
    )
    match = None
    best = 0.0
    for theme in themes:
        score = token_similarity(case.get("title"), theme.get("name"))
        if score > best:
            best, match = score, theme
    payload = case.get("payload") if isinstance(case.get("payload"), Mapping) else {}
    provenance = {
        "source_case_id": parsed_case,
        "source_case_version": case.get("current_version"),
        "input_fingerprint": case.get("input_fingerprint"),
    }
    if match is not None and best >= similarity_threshold:
        session.execute(
            text(
                """
                UPDATE investment_themes SET
                    source_case_id = COALESCE(source_case_id, :case_id),
                    discovery_provenance = CAST(:provenance AS JSONB),
                    updated_at = NOW()
                WHERE id = :theme_id
                """
            ),
            {
                "case_id": parsed_case,
                "theme_id": match["id"],
                "provenance": _json(provenance),
            },
        )
        theme_id, created = str(match["id"]), False
    else:
        inserted = result_first(
            session.execute(
                text(
                    """
                INSERT INTO investment_themes (
                    name, definition, horizon, macro_drivers,
                    invalidation_conditions, status, origin, source_case_id,
                    discovery_provenance
                ) VALUES (
                    :name, :definition, :horizon, :macro_drivers,
                    CAST(:invalidation_conditions AS JSONB), 'active',
                    'discovered', :source_case_id,
                    CAST(:discovery_provenance AS JSONB)
                ) ON CONFLICT (name) DO NOTHING RETURNING id
                """
                ),
                {
                    "name": case["title"],
                    "definition": case["definition"],
                    "horizon": case["horizon"],
                    "macro_drivers": list(payload.get("macro_drivers") or []),
                    "invalidation_conditions": _json(
                        payload.get("invalidation_conditions") or []
                    ),
                    "source_case_id": parsed_case,
                    "discovery_provenance": _json(provenance),
                },
            )
        )
        if inserted is None:
            inserted = result_first(
                session.execute(
                    text("SELECT id FROM investment_themes WHERE name = :name LIMIT 1"),
                    {"name": case["title"]},
                )
            )
        if inserted is None:
            raise RuntimeError("theme promotion did not return an identity")
        theme_id, created = str(inserted["id"]), True
    entities = result_rows(
        session.execute(
            text(
                """
            SELECT entity_type, normalized_key, display_name
            FROM research_case_entities WHERE case_id = :case_id LIMIT 100
            """
            ),
            {"case_id": parsed_case},
        )
    )
    type_map = {"company": "company", "industry": "industry", "symbol": "symbol"}
    for entity in entities:
        theme_type = type_map.get(entity.get("entity_type"))
        if not theme_type:
            continue
        session.execute(
            text(
                """
                INSERT INTO investment_theme_entities (
                    theme_id, entity_type, entity_id, display_name
                ) VALUES (:theme_id, :entity_type, :entity_id, :display_name)
                ON CONFLICT DO NOTHING
                """
            ),
            {
                "theme_id": theme_id,
                "entity_type": theme_type,
                "entity_id": entity["normalized_key"],
                "display_name": entity["display_name"],
            },
        )
    return {"theme_id": theme_id, "created": created, "matched": not created}


__all__ = [
    "CaseMutation",
    "SnapshotMutation",
    "current_market_drivers",
    "ensure_hypothesis_data_requests",
    "find_case_match_rows",
    "load_case_stats",
    "persist_adversarial",
    "persist_economic_factors",
    "persist_causal_edges",
    "persist_market_drivers",
    "persist_value_capture",
    "promote_case_to_theme",
    "publish_case_snapshot",
    "refresh_case_lifecycles",
    "upsert_case",
    "unresolved_material_hypotheses",
]
