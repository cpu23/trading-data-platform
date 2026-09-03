"""Transactional persistence and question generation for the control plane."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from analysis_jobs import enqueue_job
from contracts.db_results import result_first, result_rows
from ui_events import append_ui_invalidations

from .domain import (
    PriorityInputs,
    QuestionCandidate,
    QuestionForPlanning,
    QuestionStatus,
    canonical_json,
    content_fingerprint,
    question_fingerprint,
    question_key,
)
from .planner import Agenda, PlanDecision, PlanPolicy, plan_questions, score_priority

MAX_GENERATED_QUESTIONS = 100
_PLANNER_LOCK_KEY = "autonomous-research-control-plane-planner-v1"
_QUESTION_UPSERT_LOCK_KEY = "autonomous-research-control-plane-question-upsert-v1"


@dataclass(frozen=True, slots=True)
class QuestionDraft:
    candidate: QuestionCandidate
    priority: PriorityInputs
    not_before: datetime
    due_at: datetime | None = None
    expires_at: datetime | None = None
    source_event_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class PlannerRunResult:
    plan_id: uuid.UUID | None
    correlation_id: uuid.UUID
    status: str
    selected_count: int
    considered_count: int
    work_order_ids: tuple[uuid.UUID, ...]
    coalesced: bool
    no_op_reason: str | None




def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any, *, limit: int) -> tuple[Any, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(value[:limit])


def _utc(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    return fallback


def _bounded_score(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    if not number.is_finite():
        return None
    return max(Decimal("0"), min(Decimal("1"), number))


def _question_type(text_value: str) -> str:
    normalized = text_value.casefold()
    if any(
        word in normalized for word in ("guidance", "earnings", "revenue", "margin")
    ):
        return "earnings_guidance_delta"
    if any(
        word in normalized
        for word in ("peer", "supplier", "customer", "read-through", "readthrough")
    ):
        return "filing_peer_readthrough"
    if any(
        word in normalized
        for word in ("positioning", "options", "short interest", "cftc", "flow")
    ):
        return "positioning_divergence"
    return "thesis_challenge"


def _atomic_question(value: Any, *, prefix: str) -> str | None:
    if not isinstance(value, str):
        return None
    text_value = " ".join(value.split())
    if not text_value:
        return None
    if not text_value.endswith("?"):
        text_value = f"{prefix}: {text_value}?"
    return text_value[:2000]


def questions_from_promoted_candidate(
    candidate: Mapping[str, Any],
    *,
    thesis_id: uuid.UUID | str,
    accepted_cutoff: datetime,
) -> tuple[QuestionDraft, ...]:
    """Turn a real promoted candidate's bounded missing-evidence list into questions."""
    missing = _sequence(candidate.get("missing_evidence"), limit=20)
    confidence = _bounded_score(candidate.get("confidence"))
    materiality = _bounded_score(
        candidate.get("opportunity_score", candidate.get("materiality"))
    )
    output: list[QuestionDraft] = []
    for item in missing:
        question_text = _atomic_question(item, prefix="Can current evidence resolve")
        if question_text is None:
            continue
        question_type = _question_type(question_text)
        question = QuestionCandidate(
            origin_kind="promoted_candidate",
            question_type=question_type,
            atomic_question=question_text,
            target_kind="thesis",
            target_ref=str(thesis_id),
            accepted_cutoff=accepted_cutoff,
            required_evidence_shape={
                "missing_evidence": "cited point-in-time evidence"
            },
            acceptable_source_families=tuple(
                str(item)
                for item in _sequence(candidate.get("source_families"), limit=32)
                if str(item).strip()
            ),
        )
        output.append(
            QuestionDraft(
                candidate=question,
                priority=PriorityInputs(
                    materiality=materiality,
                    uncertainty=None
                    if confidence is None
                    else Decimal("1") - confidence,
                    discrimination_power=Decimal("0.8"),
                    urgency=Decimal("0.6"),
                    freshness_gap=Decimal("1"),
                    resolvability=Decimal("0.7"),
                    expected_cost_usd=Decimal("0.05"),
                    expected_runtime_seconds=60,
                    expected_human_review_minutes=None,
                ),
                not_before=accepted_cutoff,
                due_at=accepted_cutoff + timedelta(days=7),
                expires_at=accepted_cutoff + timedelta(days=30),
            )
        )
    return tuple(output)


def questions_from_falsification(
    findings: Mapping[str, Any],
    *,
    thesis_id: uuid.UUID | str,
    accepted_cutoff: datetime,
    materiality: Any = None,
    uncertainty: Any = None,
) -> tuple[QuestionDraft, ...]:
    required = _sequence(findings.get("required_data"), limit=32)
    output: list[QuestionDraft] = []
    for raw in required:
        if isinstance(raw, Mapping):
            raw = raw.get("description") or raw.get("question") or raw.get("data")
        question_text = _atomic_question(raw, prefix="Can falsification data resolve")
        if question_text is None:
            continue
        output.append(
            QuestionDraft(
                candidate=QuestionCandidate(
                    origin_kind="falsification",
                    question_type="thesis_challenge",
                    atomic_question=question_text,
                    target_kind="thesis",
                    target_ref=str(thesis_id),
                    accepted_cutoff=accepted_cutoff,
                    required_evidence_shape={
                        "required_data": "independent contradictory evidence"
                    },
                    acceptable_source_families=(),
                ),
                priority=PriorityInputs(
                    materiality=_bounded_score(materiality),
                    uncertainty=_bounded_score(uncertainty),
                    discrimination_power=Decimal("1"),
                    urgency=Decimal("0.8"),
                    freshness_gap=Decimal("1"),
                    resolvability=Decimal("0.7"),
                    expected_cost_usd=Decimal("0.10"),
                    expected_runtime_seconds=90,
                ),
                not_before=accepted_cutoff,
                due_at=accepted_cutoff + timedelta(days=3),
                expires_at=accepted_cutoff + timedelta(days=30),
            )
        )
    return tuple(output)


def questions_from_event(
    event: Mapping[str, Any], *, accepted_cutoff: datetime
) -> tuple[QuestionDraft, ...]:
    """Create bounded entity-specific dirty-state questions from one source event."""
    event_type = str(event.get("event_type") or "source_event").strip().lower()
    entities = _sequence(event.get("entities"), limit=20)
    markets = _sequence(event.get("markets"), limit=20)
    targets: set[str] = set()
    for entity in entities:
        if isinstance(entity, Mapping):
            value = (
                entity.get("canonical_id") or entity.get("name") or entity.get("symbol")
            )
        else:
            value = entity
        if value:
            targets.add(str(value).strip().lower())
    for market in markets:
        if isinstance(market, Mapping):
            value = market.get("symbol") or market.get("market")
        else:
            value = market
        if value:
            targets.add(str(value).strip().lower())
    question_type = _question_type(event_type.replace("_", " "))
    if question_type == "thesis_challenge":
        question_type = "evidence_refresh"
    importance = _bounded_score(event.get("importance_hint"))
    source_event_id = event.get("event_id")
    try:
        parsed_event_id = uuid.UUID(str(source_event_id)) if source_event_id else None
    except ValueError:
        parsed_event_id = None
    output: list[QuestionDraft] = []
    for target in sorted(filter(None, targets))[:20]:
        output.append(
            QuestionDraft(
                candidate=QuestionCandidate(
                    origin_kind="source_event",
                    question_type=question_type,
                    atomic_question=(
                        f"What thesis-relevant state for {target} changed in the "
                        f"accepted {event_type} event?"
                    ),
                    target_kind="entity",
                    target_ref=target,
                    accepted_cutoff=accepted_cutoff,
                    required_evidence_shape={
                        "event_type": event_type,
                        "effect": "bounded changed state",
                    },
                    acceptable_source_families=(str(event.get("source") or "unknown"),),
                ),
                priority=PriorityInputs(
                    materiality=importance,
                    uncertainty=Decimal("0.7"),
                    discrimination_power=Decimal("0.7"),
                    urgency=Decimal("0.9"),
                    freshness_gap=Decimal("1"),
                    resolvability=Decimal("0.8"),
                    expected_cost_usd=Decimal("0"),
                    expected_runtime_seconds=30,
                ),
                not_before=accepted_cutoff,
                due_at=accepted_cutoff + timedelta(days=1),
                expires_at=accepted_cutoff + timedelta(days=14),
                source_event_id=parsed_event_id,
            )
        )
    return tuple(output)


def _upsert_dependency_node(
    session: Any,
    *,
    node_type: str,
    node_key: str,
    accepted_cutoff: datetime,
    metadata: Mapping[str, Any],
    dirty_since: datetime | None = None,
) -> uuid.UUID:
    state_fingerprint = content_fingerprint(
        {"node_type": node_type, "node_key": node_key, "metadata": metadata}
    )
    row = result_first(session.execute(
        text(
            """
            INSERT INTO research_dependency_nodes (
                node_type, node_key, state_fingerprint, accepted_cutoff,
                dirty_since, metadata, created_at, updated_at
            ) VALUES (
                :node_type, :node_key, :state_fingerprint, :accepted_cutoff,
                CASE
                    WHEN :dirty_since IS NULL THEN NULL
                    ELSE GREATEST(
                        :dirty_since, LEAST(:accepted_cutoff, NOW())
                    )
                END,
                CAST(:metadata AS JSONB), LEAST(:accepted_cutoff, NOW()), NOW()
            )
            ON CONFLICT (node_type, node_key) DO UPDATE
            SET state_fingerprint = CASE
                    WHEN research_dependency_nodes.accepted_cutoff IS NULL
                      OR EXCLUDED.accepted_cutoff >=
                         research_dependency_nodes.accepted_cutoff
                    THEN EXCLUDED.state_fingerprint
                    ELSE research_dependency_nodes.state_fingerprint
                END,
                accepted_cutoff = GREATEST(
                    COALESCE(
                        research_dependency_nodes.accepted_cutoff,
                        EXCLUDED.accepted_cutoff
                    ),
                    EXCLUDED.accepted_cutoff
                ),
                dirty_since = CASE
                    WHEN EXCLUDED.dirty_since IS NULL
                    THEN research_dependency_nodes.dirty_since
                    ELSE LEAST(
                        COALESCE(
                            research_dependency_nodes.dirty_since,
                            GREATEST(
                                EXCLUDED.dirty_since,
                                research_dependency_nodes.created_at
                            )
                        ),
                        GREATEST(
                            EXCLUDED.dirty_since,
                            research_dependency_nodes.created_at
                        )
                    )
                END,
                metadata = research_dependency_nodes.metadata || EXCLUDED.metadata,
                updated_at = NOW()
            RETURNING id
            """
        ),
        {
            "node_type": node_type,
            "node_key": node_key[:500],
            "state_fingerprint": state_fingerprint,
            "accepted_cutoff": accepted_cutoff,
            "dirty_since": dirty_since,
            "metadata": canonical_json(metadata),
        },
    ))
    if row is None:
        raise RuntimeError("dependency node upsert returned no row")
    return uuid.UUID(str(row["id"]))


def _upsert_dependency_edge(
    session: Any,
    *,
    source_node_id: uuid.UUID,
    target_node_id: uuid.UUID,
    edge_kind: str,
) -> None:
    if source_node_id == target_node_id:
        return
    session.execute(
        text(
            """
            INSERT INTO research_dependency_edges (
                source_node_id, target_node_id, edge_kind, active
            ) VALUES (:source_node_id, :target_node_id, :edge_kind, TRUE)
            ON CONFLICT (source_node_id, target_node_id, edge_kind) DO UPDATE
            SET active = TRUE, deactivated_at = NULL
            """
        ),
        {
            "source_node_id": source_node_id,
            "target_node_id": target_node_id,
            "edge_kind": edge_kind,
        },
    )


def _upsert_dependency_nodes(
    session: Any, specs: Sequence[Mapping[str, Any]]
) -> dict[tuple[str, str], uuid.UUID]:
    """Upsert one bounded node set in a single PostgreSQL statement."""
    deduplicated: dict[tuple[str, str], dict[str, Any]] = {}
    for spec in specs:
        node_type = str(spec["node_type"])
        node_key = str(spec["node_key"])[:500]
        metadata = _mapping(spec.get("metadata"))
        accepted_cutoff = _utc(spec.get("accepted_cutoff"), datetime.now(UTC))
        dirty_since = spec.get("dirty_since")
        deduplicated[(node_type, node_key)] = {
            "node_type": node_type,
            "node_key": node_key,
            "state_fingerprint": content_fingerprint(
                {
                    "node_type": node_type,
                    "node_key": node_key,
                    "metadata": metadata,
                }
            ),
            "accepted_cutoff": accepted_cutoff.isoformat(),
            "dirty_since": (
                _utc(dirty_since, accepted_cutoff).isoformat()
                if dirty_since is not None
                else None
            ),
            "metadata": metadata,
        }
    if not deduplicated:
        return {}
    rows = result_rows(session.execute(
        text(
            """
            WITH input AS (
                SELECT *
                FROM JSONB_TO_RECORDSET(CAST(:nodes AS JSONB)) AS item(
                    node_type TEXT,
                    node_key TEXT,
                    state_fingerprint TEXT,
                    accepted_cutoff TIMESTAMPTZ,
                    dirty_since TIMESTAMPTZ,
                    metadata JSONB
                )
            )
            INSERT INTO research_dependency_nodes (
                node_type, node_key, state_fingerprint, accepted_cutoff,
                dirty_since, metadata, created_at, updated_at
            )
            SELECT
                node_type,
                node_key,
                state_fingerprint,
                accepted_cutoff,
                CASE
                    WHEN dirty_since IS NULL THEN NULL
                    ELSE GREATEST(
                        dirty_since, LEAST(accepted_cutoff, NOW())
                    )
                END,
                metadata,
                LEAST(accepted_cutoff, NOW()),
                NOW()
            FROM input
            ON CONFLICT (node_type, node_key) DO UPDATE
            SET state_fingerprint = CASE
                    WHEN research_dependency_nodes.accepted_cutoff IS NULL
                      OR EXCLUDED.accepted_cutoff >=
                         research_dependency_nodes.accepted_cutoff
                    THEN EXCLUDED.state_fingerprint
                    ELSE research_dependency_nodes.state_fingerprint
                END,
                accepted_cutoff = GREATEST(
                    COALESCE(
                        research_dependency_nodes.accepted_cutoff,
                        EXCLUDED.accepted_cutoff
                    ),
                    EXCLUDED.accepted_cutoff
                ),
                dirty_since = CASE
                    WHEN EXCLUDED.dirty_since IS NULL
                    THEN research_dependency_nodes.dirty_since
                    ELSE LEAST(
                        COALESCE(
                            research_dependency_nodes.dirty_since,
                            GREATEST(
                                EXCLUDED.dirty_since,
                                research_dependency_nodes.created_at
                            )
                        ),
                        GREATEST(
                            EXCLUDED.dirty_since,
                            research_dependency_nodes.created_at
                        )
                    )
                END,
                metadata = research_dependency_nodes.metadata || EXCLUDED.metadata,
                updated_at = NOW()
            RETURNING id, node_type, node_key
            """
        ),
        {"nodes": canonical_json(list(deduplicated.values()))},
    ))
    return {
        (str(row["node_type"]), str(row["node_key"])): uuid.UUID(str(row["id"]))
        for row in rows
    }


def _upsert_dependency_edges(
    session: Any, specs: Sequence[tuple[uuid.UUID, uuid.UUID, str]]
) -> int:
    """Upsert one bounded edge set in a single PostgreSQL statement."""
    payload = [
        {
            "source_node_id": str(source),
            "target_node_id": str(target),
            "edge_kind": edge_kind,
        }
        for source, target, edge_kind in sorted(
            {
                (source, target, str(edge_kind))
                for source, target, edge_kind in specs
                if source != target
            },
            key=lambda item: (str(item[0]), str(item[1]), item[2]),
        )
    ]
    if not payload:
        return 0
    session.execute(
        text(
            """
            INSERT INTO research_dependency_edges (
                source_node_id, target_node_id, edge_kind, active
            )
            SELECT source_node_id, target_node_id, edge_kind, TRUE
            FROM JSONB_TO_RECORDSET(CAST(:edges AS JSONB)) AS item(
                source_node_id UUID,
                target_node_id UUID,
                edge_kind TEXT
            )
            ON CONFLICT (source_node_id, target_node_id, edge_kind) DO UPDATE
            SET active = TRUE, deactivated_at = NULL
            """
        ),
        {"edges": canonical_json(payload)},
    )
    return len(payload)


def _event_targets(event: Mapping[str, Any]) -> tuple[str, ...]:
    targets: set[str] = set()
    for value in (
        *_sequence(event.get("entities"), limit=20),
        *_sequence(event.get("markets"), limit=20),
    ):
        if isinstance(value, Mapping):
            value = (
                value.get("canonical_id")
                or value.get("symbol")
                or value.get("name")
                or value.get("market")
            )
        normalized = " ".join(str(value or "").split()).casefold()
        if normalized:
            targets.add(normalized[:500])
    return tuple(sorted(targets)[:20])


def propagate_event_dependencies(
    session: Any,
    event: Mapping[str, Any],
    *,
    accepted_cutoff: datetime,
    limit: int = 100,
) -> Mapping[str, int]:
    """Dirty only dependency nodes targeted by one accepted source event."""
    targets = _event_targets(event)
    if not targets:
        return {"nodes_touched": 0, "edges_touched": 0, "theses_affected": 0}
    source = " ".join(str(event.get("source") or "unknown").split()).casefold()[:100]
    event_type = " ".join(
        str(event.get("event_type") or "source_event").split()
    ).casefold()[:100]
    event_id = str(event.get("event_id") or content_fingerprint(event))[:200]
    observation_key = f"{source}:{event_id}"[:500]
    initial_specs: list[Mapping[str, Any]] = [
        {
            "node_type": "source",
            "node_key": source,
            "accepted_cutoff": accepted_cutoff,
            "metadata": {"source_family": source},
        },
        {
            "node_type": "source_observation",
            "node_key": observation_key,
            "accepted_cutoff": accepted_cutoff,
            "metadata": {
                "source_family": source,
                "event_type": event_type,
                "event_id": event_id,
            },
            "dirty_since": accepted_cutoff,
        },
        *[
            {
                "node_type": "entity",
                "node_key": target,
                "accepted_cutoff": accepted_cutoff,
                "metadata": {"canonical_ref": target},
                "dirty_since": accepted_cutoff,
            }
            for target in targets
        ],
    ]
    initial_nodes = _upsert_dependency_nodes(session, initial_specs)
    source_node = initial_nodes[("source", source)]
    observation_node = initial_nodes[("source_observation", observation_key)]
    entity_nodes = {target: initial_nodes[("entity", target)] for target in targets}
    initial_edges = [
        (observation_node, source_node, "derived_from"),
        *[(observation_node, entity_nodes[target], "mentions") for target in targets],
    ]
    nodes_touched = len(initial_nodes)
    edges_touched = _upsert_dependency_edges(session, initial_edges)

    bounded = max(1, min(int(limit), 100))
    theses = result_rows(session.execute(
        text(
            """
            SELECT t.id, t.claim, t.company, t.symbol, t.status,
                   (
                       SELECT MAX(v.version)
                       FROM investment_thesis_versions v
                       WHERE v.thesis_id = t.id
                         AND v.created_at <= :cutoff
                   ) AS version,
                   t.confidence_score, t.opportunity_score, t.updated_at
            FROM investment_theses t
            WHERE t.created_at <= :cutoff
              AND t.updated_at <= :cutoff
              AND (
                  LOWER(COALESCE(t.symbol, '')) =
                      ANY(CAST(:targets AS TEXT[]))
                  OR LOWER(COALESCE(t.company, '')) =
                      ANY(CAST(:targets AS TEXT[]))
              )
            ORDER BY t.updated_at DESC, t.id
            LIMIT :limit
            """
        ),
        {"cutoff": accepted_cutoff, "targets": list(targets), "limit": bounded},
    ))
    thesis_specs: list[Mapping[str, Any]] = []
    for thesis in theses:
        thesis_id = str(thesis["id"])
        thesis_specs.extend(
            (
                {
                    "node_type": "thesis",
                    "node_key": thesis_id,
                    "accepted_cutoff": accepted_cutoff,
                    "metadata": {
                        "status": thesis.get("status"),
                        "version": thesis.get("version"),
                        "company": thesis.get("company"),
                        "symbol": thesis.get("symbol"),
                        "confidence": thesis.get("confidence_score"),
                        "opportunity": thesis.get("opportunity_score"),
                    },
                    "dirty_since": accepted_cutoff,
                },
                {
                    "node_type": "claim",
                    "node_key": f"thesis:{thesis_id}:claim",
                    "accepted_cutoff": accepted_cutoff,
                    "metadata": {"claim": str(thesis.get("claim") or "")[:2000]},
                    "dirty_since": accepted_cutoff,
                },
            )
        )
    persisted_thesis_nodes = _upsert_dependency_nodes(session, thesis_specs)
    thesis_nodes = {
        str(thesis["id"]): persisted_thesis_nodes[("thesis", str(thesis["id"]))]
        for thesis in theses
    }
    claim_nodes = {
        str(thesis["id"]): persisted_thesis_nodes[
            ("claim", f"thesis:{thesis['id']}:claim")
        ]
        for thesis in theses
    }
    thesis_edges: list[tuple[uuid.UUID, uuid.UUID, str]] = []
    for thesis in theses:
        thesis_id = str(thesis["id"])
        thesis_node = thesis_nodes[thesis_id]
        thesis_edges.extend(
            (
                (claim_nodes[thesis_id], thesis_node, "supports"),
                (observation_node, thesis_node, "affects"),
            )
        )
        normalized_identity = {
            str(thesis.get("symbol") or "").casefold(),
            str(thesis.get("company") or "").casefold(),
        }
        thesis_edges.extend(
            (entity_nodes[target], thesis_node, "affects")
            for target in targets
            if target in normalized_identity
        )
    nodes_touched += len(persisted_thesis_nodes)
    edges_touched += _upsert_dependency_edges(session, thesis_edges)

    if thesis_nodes:
        children = result_rows(session.execute(
            text(
                """
                SELECT *
                FROM (
                    SELECT 'scenario'::TEXT AS node_type, s.id::TEXT AS node_key,
                           s.thesis_id::TEXT AS thesis_id,
                           JSONB_BUILD_OBJECT(
                               'name', s.name, 'description', s.description,
                               'probability', s.probability,
                               'expected_return', s.expected_return
                           ) AS metadata,
                           'derived_from'::TEXT AS edge_kind
                    FROM investment_thesis_scenarios s
                    WHERE s.thesis_id = ANY(CAST(:thesis_ids AS UUID[]))
                      AND s.created_at <= :cutoff
                      AND (s.superseded_at IS NULL OR s.superseded_at > :cutoff)
                    UNION ALL
                    SELECT 'forecast', f.id::TEXT, f.thesis_id::TEXT,
                           JSONB_BUILD_OBJECT(
                               'forecast_key', f.forecast_key,
                               'target_date', f.target_date,
                               'target_value', f.target_value
                           ), 'derived_from'
                    FROM investment_thesis_forecasts f
                    WHERE f.thesis_id = ANY(CAST(:thesis_ids AS UUID[]))
                      AND f.created_at <= :cutoff
                      AND (f.superseded_at IS NULL OR f.superseded_at > :cutoff)
                    UNION ALL
                    SELECT 'catalyst', c.id::TEXT, c.thesis_id::TEXT,
                           JSONB_BUILD_OBJECT(
                               'description', c.description,
                               'expected_at', c.expected_at, 'state', c.state
                           ), 'affects'
                    FROM investment_catalysts c
                    WHERE c.thesis_id = ANY(CAST(:thesis_ids AS UUID[]))
                      AND c.created_at <= :cutoff AND c.updated_at <= :cutoff
                    UNION ALL
                    SELECT 'risk', r.id::TEXT, r.thesis_id::TEXT,
                           JSONB_BUILD_OBJECT(
                               'description', r.description,
                               'kind', r.kind, 'severity', r.severity
                           ), 'invalidates'
                    FROM investment_risks r
                    WHERE r.thesis_id = ANY(CAST(:thesis_ids AS UUID[]))
                      AND r.created_at <= :cutoff AND r.updated_at <= :cutoff
                    UNION ALL
                    SELECT 'playbook', p.id::TEXT, p.thesis_id::TEXT,
                           JSONB_BUILD_OBJECT(
                               'playbook_key', p.playbook_key,
                               'version', p.version, 'horizon', p.horizon
                           ), 'depends_on'
                    FROM investment_thesis_event_playbooks p
                    WHERE p.thesis_id = ANY(CAST(:thesis_ids AS UUID[]))
                      AND p.created_at <= :cutoff
                      AND (p.superseded_at IS NULL OR p.superseded_at > :cutoff)
                    UNION ALL
                    SELECT 'watchlist', w.id::TEXT, w.thesis_id::TEXT,
                           JSONB_BUILD_OBJECT(
                               'label', w.label, 'source_kind', w.source_kind,
                               'source_id', w.source_id
                           ), 'depends_on'
                    FROM investment_watch_items w
                    WHERE w.thesis_id = ANY(CAST(:thesis_ids AS UUID[]))
                      AND w.created_at <= :cutoff
                    UNION ALL
                    SELECT 'evidence',
                           e.evidence_type || ':' || e.evidence_id,
                           e.thesis_id::TEXT,
                           JSONB_BUILD_OBJECT(
                               'evidence_type', e.evidence_type,
                               'evidence_id', e.evidence_id,
                               'relationship', e.relationship,
                               'source_family', e.source_family
                           ),
                           CASE
                               WHEN e.relationship = 'contradicts'
                               THEN 'contradicts'
                               WHEN e.relationship = 'supports'
                               THEN 'supports'
                               ELSE 'mentions'
                           END
                    FROM investment_thesis_evidence e
                    WHERE e.thesis_id = ANY(CAST(:thesis_ids AS UUID[]))
                      AND e.created_at <= :cutoff
                      AND COALESCE(
                          e.available_at, e.source_timestamp, e.created_at
                      ) <= :cutoff
                ) children
                ORDER BY node_type, node_key
                LIMIT 1000
                """
            ),
            {
                "thesis_ids": list(thesis_nodes),
                "cutoff": accepted_cutoff,
            },
        ))
        child_specs: list[Mapping[str, Any]] = []
        for child in children:
            node_type = str(child["node_type"])
            node_key = str(child["node_key"])
            metadata = _mapping(child.get("metadata"))
            child_specs.append(
                {
                    "node_type": node_type,
                    "node_key": node_key,
                    "accepted_cutoff": accepted_cutoff,
                    "metadata": metadata,
                    "dirty_since": accepted_cutoff,
                }
            )
            if node_type == "scenario" and metadata.get("description"):
                child_specs.append(
                    {
                        "node_type": "assumption",
                        "node_key": f"scenario:{node_key}:assumption",
                        "accepted_cutoff": accepted_cutoff,
                        "metadata": {"description": metadata["description"]},
                        "dirty_since": accepted_cutoff,
                    }
                )
        persisted_children = _upsert_dependency_nodes(session, child_specs)
        child_edges: list[tuple[uuid.UUID, uuid.UUID, str]] = []
        for child in children:
            thesis_id = str(child["thesis_id"])
            node_type = str(child["node_type"])
            node_key = str(child["node_key"])
            metadata = _mapping(child.get("metadata"))
            child_node = persisted_children[(node_type, node_key)]
            target_node = (
                claim_nodes[thesis_id]
                if node_type == "evidence"
                else thesis_nodes[thesis_id]
            )
            child_edges.append((child_node, target_node, str(child["edge_kind"])))
            if node_type == "scenario" and metadata.get("description"):
                assumption_node = persisted_children[
                    ("assumption", f"scenario:{node_key}:assumption")
                ]
                child_edges.append((assumption_node, child_node, "supports"))
        nodes_touched += len(persisted_children)
        edges_touched += _upsert_dependency_edges(session, child_edges)
    return {
        "nodes_touched": nodes_touched,
        "edges_touched": edges_touched,
        "theses_affected": len(theses),
    }


def record_effect_dependency(
    session: Any,
    *,
    work_order_id: uuid.UUID,
    question_id: uuid.UUID,
    target_kind: str,
    target_ref: str,
    accepted_cutoff: datetime,
    effect_type: str,
    material: bool,
    resolved: bool,
) -> None:
    """Link one persisted effect to its question and affected target."""
    effect_node = _upsert_dependency_node(
        session,
        node_type="effect",
        node_key=str(work_order_id),
        accepted_cutoff=accepted_cutoff,
        metadata={
            "effect_type": effect_type,
            "material": material,
            "question_id": str(question_id),
        },
    )
    question_node = _upsert_dependency_node(
        session,
        node_type="question",
        node_key=str(question_id),
        accepted_cutoff=accepted_cutoff,
        metadata={"question_id": str(question_id)},
    )
    target_type = (
        target_kind
        if target_kind in {"thesis", "forecast", "catalyst", "entity", "source"}
        else "entity"
    )
    target_node = _upsert_dependency_node(
        session,
        node_type=target_type,
        node_key=target_ref,
        accepted_cutoff=accepted_cutoff,
        metadata={"reference_kind": target_kind},
    )
    _upsert_dependency_edge(
        session,
        source_node_id=effect_node,
        target_node_id=question_node,
        edge_kind="resolves",
    )
    _upsert_dependency_edge(
        session,
        source_node_id=effect_node,
        target_node_id=target_node,
        edge_kind="affects",
    )
    if resolved:
        session.execute(
            text(
                """
                UPDATE research_dependency_nodes
                SET dirty_since = NULL,
                    last_refreshed_at = :accepted_cutoff,
                    updated_at = NOW()
                WHERE id IN (:question_node, :target_node)
                  AND (
                      accepted_cutoff IS NULL
                      OR accepted_cutoff <= :accepted_cutoff
                  )
                """
            ),
            {
                "question_node": question_node,
                "target_node": target_node,
                "accepted_cutoff": accepted_cutoff,
            },
        )


def upsert_question(session: Any, draft: QuestionDraft) -> Mapping[str, Any] | None:
    result = score_priority(draft.priority)
    params = {
        "fingerprint": question_fingerprint(draft.candidate),
        "question_key": question_key(draft.candidate),
        "origin_kind": draft.candidate.origin_kind,
        "question_type": draft.candidate.question_type,
        "atomic_question": draft.candidate.atomic_question,
        "target_kind": draft.candidate.target_kind,
        "target_ref": draft.candidate.target_ref,
        "accepted_cutoff": draft.candidate.accepted_cutoff,
        "required_evidence_shape": canonical_json(
            draft.candidate.required_evidence_shape
        ),
        "acceptable_source_families": list(draft.candidate.acceptable_source_families),
        "materiality": draft.priority.materiality,
        "uncertainty": draft.priority.uncertainty,
        "discrimination_power": draft.priority.discrimination_power,
        "urgency": draft.priority.urgency,
        "freshness_gap": draft.priority.freshness_gap,
        "resolvability": draft.priority.resolvability,
        "estimated_cost_usd": draft.priority.expected_cost_usd,
        "estimated_runtime_seconds": draft.priority.expected_runtime_seconds,
        "expected_human_review_minutes": draft.priority.expected_human_review_minutes,
        "priority_policy_version": result.policy_version,
        "priority_score": result.score,
        "priority_blockers": list(result.blockers),
        "not_before": draft.not_before,
        "due_at": draft.due_at,
        "expires_at": draft.expires_at,
        "dirty_since": draft.not_before,
        "source_event_id": draft.source_event_id,
    }
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": _QUESTION_UPSERT_LOCK_KEY},
    )
    existing = result_first(session.execute(
        text(
            """
            SELECT *
            FROM research_questions
            WHERE fingerprint = :fingerprint
            LIMIT 1
            FOR UPDATE
            """
        ),
        params,
    ))
    if existing is not None:
        return existing

    newer_active = result_first(session.execute(
        text(
            """
            SELECT *
            FROM research_questions
            WHERE question_key = :question_key
              AND status IN ('pending', 'planned', 'queued', 'running')
              AND accepted_cutoff >= :accepted_cutoff
            ORDER BY accepted_cutoff DESC, created_at DESC, id
            LIMIT 1
            FOR UPDATE
            """
        ),
        params,
    ))
    if newer_active is not None:
        return newer_active

    session.execute(
        text(
            """
            UPDATE research_questions
            SET status = 'cancelled',
                unresolved_reason = 'superseded by newer accepted cutoff'
            WHERE question_key = :question_key
              AND status = 'pending'
              AND accepted_cutoff < :accepted_cutoff
            """
        ),
        params,
    )

    row = result_first(session.execute(
        text(
            """
            INSERT INTO research_questions (
                fingerprint, question_key, origin_kind, question_type,
                atomic_question, target_kind, target_ref, accepted_cutoff,
                required_evidence_shape, acceptable_source_families,
                materiality, uncertainty, discrimination_power, urgency,
                freshness_gap, resolvability, estimated_cost_usd,
                estimated_runtime_seconds, expected_human_review_minutes,
                priority_policy_version, priority_score, priority_blockers,
                status, not_before, due_at, expires_at, dirty_since,
                latest_source_event_id
            ) VALUES (
                :fingerprint, :question_key, :origin_kind, :question_type,
                :atomic_question, :target_kind, :target_ref, :accepted_cutoff,
                CAST(:required_evidence_shape AS JSONB), :acceptable_source_families,
                :materiality, :uncertainty, :discrimination_power, :urgency,
                :freshness_gap, :resolvability, :estimated_cost_usd,
                :estimated_runtime_seconds, :expected_human_review_minutes,
                :priority_policy_version, :priority_score, :priority_blockers,
                'pending', :not_before, :due_at, :expires_at, :dirty_since,
                :source_event_id
            )
            ON CONFLICT DO NOTHING
            RETURNING *
            """
        ),
        params,
    ))
    if row is None:
        row = result_first(session.execute(
            text(
                """
                SELECT *
                FROM research_questions
                WHERE fingerprint = :fingerprint
                LIMIT 1
                FOR UPDATE
                """
            ),
            params,
        ))
    if row is None:
        return None
    question_node = _upsert_dependency_node(
        session,
        node_type="question",
        node_key=str(row["id"]),
        accepted_cutoff=draft.candidate.accepted_cutoff,
        metadata={
            "question_type": draft.candidate.question_type,
            "origin_kind": draft.candidate.origin_kind,
            "target_kind": draft.candidate.target_kind,
            "target_ref": draft.candidate.target_ref,
        },
        dirty_since=draft.not_before,
    )
    target_type = (
        draft.candidate.target_kind
        if draft.candidate.target_kind
        in {"thesis", "forecast", "catalyst", "entity", "source"}
        else "entity"
    )
    target_node = _upsert_dependency_node(
        session,
        node_type=target_type,
        node_key=draft.candidate.target_ref,
        accepted_cutoff=draft.candidate.accepted_cutoff,
        metadata={"reference_kind": draft.candidate.target_kind},
    )
    _upsert_dependency_edge(
        session,
        source_node_id=question_node,
        target_node_id=target_node,
        edge_kind="depends_on",
    )
    return row


def refresh_questions_from_state(
    session: Any,
    *,
    accepted_cutoff: datetime,
    catalyst_lookahead_days: int,
    stale_question_days: int,
    limit: int = MAX_GENERATED_QUESTIONS,
) -> tuple[Mapping[str, Any], ...]:
    """Generate questions from bounded persisted thesis/falsification state."""
    limit = max(1, min(int(limit), MAX_GENERATED_QUESTIONS))
    inserted: list[Mapping[str, Any]] = []

    falsification_rows = result_rows(session.execute(
        text(
            """
            SELECT r.thesis_id, r.findings, t.opportunity_score,
                   t.confidence_score
            FROM investment_thesis_falsification_runs r
            JOIN investment_theses t ON t.id = r.thesis_id
            WHERE r.created_at <= :cutoff
              AND r.findings ? 'required_data'
            ORDER BY r.created_at DESC, r.id DESC
            LIMIT :limit
            """
        ),
        {"cutoff": accepted_cutoff, "limit": limit},
    ))
    for row in falsification_rows:
        findings = row.get("findings")
        if isinstance(findings, str):
            try:
                findings = json.loads(findings)
            except ValueError:
                findings = {}
        confidence = _bounded_score(row.get("confidence_score"))
        for draft in questions_from_falsification(
            _mapping(findings),
            thesis_id=row["thesis_id"],
            accepted_cutoff=accepted_cutoff,
            materiality=row.get("opportunity_score"),
            uncertainty=(None if confidence is None else Decimal("1") - confidence),
        ):
            saved = upsert_question(session, draft)
            if saved is not None:
                inserted.append(saved)
            if len(inserted) >= limit:
                return tuple(inserted)

    source_rows = result_rows(session.execute(
        text(
            """
            SELECT kind, target_kind, target_ref, question_text, source_family,
                   materiality, uncertainty, discrimination_power, urgency,
                   freshness_gap, resolvability, estimated_cost_usd,
                   estimated_runtime_seconds, due_at, expires_at
            FROM (
                SELECT
                    'stale_dependency'::TEXT AS kind,
                    'source'::TEXT AS target_kind,
                    n.node_key AS target_ref,
                    COALESCE(
                        n.metadata->>'question',
                        'Which accepted evidence dependency for ' || n.node_key || ' is now stale?'
                    ) AS question_text,
                    COALESCE(n.metadata->>'source_family', 'unknown') AS source_family,
                    NULLIF(n.metadata->>'materiality', '')::NUMERIC AS materiality,
                    NULLIF(n.metadata->>'uncertainty', '')::NUMERIC AS uncertainty,
                    0.7::NUMERIC AS discrimination_power,
                    0.7::NUMERIC AS urgency,
                    1.0::NUMERIC AS freshness_gap,
                    0.7::NUMERIC AS resolvability,
                    0.0::NUMERIC AS estimated_cost_usd,
                    30::INTEGER AS estimated_runtime_seconds,
                    :cutoff + INTERVAL '3 days' AS due_at,
                    :cutoff + INTERVAL '30 days' AS expires_at
                FROM research_dependency_nodes n
                WHERE n.dirty_since IS NOT NULL
                  AND n.dirty_since <= :cutoff - (:stale_days * INTERVAL '1 day')
                  AND n.node_type IN ('source_observation', 'source', 'evidence')
                UNION ALL
                SELECT
                    'catalyst_confirmation', 'catalyst'::TEXT AS target_kind,
                    c.id::TEXT,
                    'Has the upcoming catalyst been independently confirmed: ' || c.description || '?',
                    'issuer_material', t.catalyst_score, 1.0 - t.confidence_score,
                    0.9, 1.0, 1.0, 0.8, 0.0, 30,
                    c.expected_at,
                    c.expected_at + INTERVAL '7 days'
                FROM investment_catalysts c
                JOIN investment_theses t ON t.id = c.thesis_id
                WHERE c.state = 'pending'
                  AND c.created_at <= :cutoff
                  AND c.expected_at > :cutoff
                  AND c.expected_at <= :cutoff + (:lookahead_days * INTERVAL '1 day')
                  AND c.updated_at <= :cutoff
                  AND t.created_at <= :cutoff
                  AND t.updated_at <= :cutoff
                UNION ALL
                SELECT
                    'forecast_resolution', 'forecast'::TEXT AS target_kind,
                    f.id::TEXT,
                    'Did forecast ' || f.forecast_key || ' resolve at its accepted target boundary?',
                    'market_price', t.opportunity_score, 1.0 - t.confidence_score,
                    1.0, 1.0, 1.0, 1.0, 0.0, 5,
                    :cutoff, :cutoff + INTERVAL '7 days'
                FROM investment_thesis_forecasts f
                JOIN investment_theses t ON t.id = f.thesis_id
                LEFT JOIN investment_forecast_outcomes o
                  ON o.forecast_id = f.id
                 AND o.created_at <= :cutoff
                 AND o.measured_at <= :cutoff
                WHERE f.created_at <= :cutoff
                  AND f.as_of <= :cutoff
                  AND (f.superseded_at IS NULL OR f.superseded_at > :cutoff)
                  AND f.target_date IS NOT NULL
                  AND f.target_date < (:cutoff AT TIME ZONE 'UTC')::DATE
                  AND t.created_at <= :cutoff
                  AND t.updated_at <= :cutoff
                  AND o.id IS NULL
                UNION ALL
                SELECT
                    'source_gap', g.target_kind,
                    g.target_ref,
                    'Can the repeated source gap be resolved: ' || g.bounded_summary || '?',
                    'unknown', NULL, NULL, 0.8, 0.6, 1.0, NULL, 0.0, 30,
                    :cutoff + INTERVAL '7 days',
                    :cutoff + INTERVAL '30 days'
                FROM research_source_gaps g
                WHERE g.active AND g.occurrence_count >= 2
                  AND g.last_observed_at <= :cutoff
            ) candidates
            ORDER BY due_at NULLS LAST, kind, target_ref
            LIMIT :limit
            """
        ),
        {
            "cutoff": accepted_cutoff,
            "lookahead_days": max(0, min(int(catalyst_lookahead_days), 3650)),
            "stale_days": max(1, min(int(stale_question_days), 3650)),
            "limit": limit - len(inserted),
        },
    ))
    type_by_kind = {
        "stale_dependency": "evidence_refresh",
        "catalyst_confirmation": "catalyst_confirmation",
        "forecast_resolution": "forecast_resolution",
        "source_gap": "source_gap",
    }
    for row in source_rows:
        kind = str(row["kind"])
        question_text = _atomic_question(
            row.get("question_text"), prefix="Can evidence resolve"
        )
        if question_text is None:
            continue
        draft = QuestionDraft(
            candidate=QuestionCandidate(
                origin_kind=kind,
                question_type=type_by_kind[kind],
                atomic_question=question_text,
                target_kind=str(row["target_kind"]),
                target_ref=str(row["target_ref"]),
                accepted_cutoff=accepted_cutoff,
                required_evidence_shape={"answer": "cited bounded resolution"},
                acceptable_source_families=(
                    str(row.get("source_family") or "unknown"),
                ),
            ),
            priority=PriorityInputs(
                materiality=_bounded_score(row.get("materiality")),
                uncertainty=_bounded_score(row.get("uncertainty")),
                discrimination_power=_bounded_score(row.get("discrimination_power")),
                urgency=_bounded_score(row.get("urgency")),
                freshness_gap=_bounded_score(row.get("freshness_gap")),
                resolvability=_bounded_score(row.get("resolvability")),
                expected_cost_usd=row.get("estimated_cost_usd"),
                expected_runtime_seconds=row.get("estimated_runtime_seconds"),
            ),
            not_before=accepted_cutoff,
            due_at=_utc(row.get("due_at"), accepted_cutoff),
            expires_at=_utc(
                row.get("expires_at"), accepted_cutoff + timedelta(days=30)
            ),
        )
        saved = upsert_question(session, draft)
        if saved is not None:
            inserted.append(saved)
    return tuple(inserted)


def load_planner_questions(
    session: Any, *, now: datetime, limit: int
) -> tuple[QuestionForPlanning, ...]:
    expired = session.execute(
        text(
            """
            UPDATE research_questions
            SET status = 'expired',
                unresolved_reason = 'question expired before planning'
            WHERE status IN ('pending', 'planned')
              AND expires_at IS NOT NULL
              AND expires_at <= :now
            """
        ),
        {"now": now},
    )
    if int(getattr(expired, "rowcount", 0) or 0) > 0:
        append_ui_invalidations(
            session,
            {"research_questions", "research_control_plane", "system_topology"},
        )
    rows = result_rows(session.execute(
        text(
            """
            SELECT q.*,
                   CASE WHEN EXISTS (
                       SELECT 1 FROM research_skill_versions s
                       WHERE s.promotion_status = 'active'
                         AND q.question_type = ANY(s.supported_question_types)
                   ) THEN FALSE ELSE TRUE END AS missing_skill,
                   EXISTS (
                       SELECT 1 FROM research_work_orders w
                       WHERE w.question_id = q.id
                         AND w.status IN (
                             'planned', 'queued', 'leased', 'running',
                             'failed_retryable'
                         )
                   ) AS active_work_exists,
                   CASE WHEN EXISTS (
                       SELECT 1 FROM research_skill_versions s
                       WHERE s.promotion_status = 'active'
                         AND q.question_type = ANY(s.supported_question_types)
                   ) AND NOT EXISTS (
                       SELECT 1
                       FROM research_skill_versions s
                       WHERE s.promotion_status = 'active'
                         AND q.question_type = ANY(s.supported_question_types)
                         AND EXISTS (
                             SELECT 1
                             FROM research_source_capabilities c
                             WHERE c.runtime_available
                               AND c.source_family = ANY(s.allowed_source_families)
                               AND q.question_type = ANY(c.supported_question_types)
                         )
                   ) THEN TRUE ELSE FALSE END AS source_unavailable
            FROM research_questions q
            WHERE q.status IN ('pending', 'planned')
              AND q.not_before <= :now
            ORDER BY q.priority_score DESC NULLS LAST,
                     q.due_at NULLS LAST, q.created_at, q.id
            LIMIT :limit
            FOR UPDATE OF q SKIP LOCKED
            """
        ),
        {"now": now, "limit": max(1, min(int(limit), 1000))},
    ))
    output: list[QuestionForPlanning] = []
    for row in rows:
        blockers = list(row.get("priority_blockers") or [])
        if row.get("missing_skill"):
            blockers.append("no_active_skill")
        if row.get("active_work_exists"):
            blockers.append("active_work_exists")
        if row.get("source_unavailable"):
            blockers.append("source_unavailable")
        output.append(
            QuestionForPlanning(
                id=uuid.UUID(str(row["id"])),
                accepted_cutoff=_utc(row.get("accepted_cutoff"), now),
                priority=PriorityInputs(
                    materiality=row.get("materiality"),
                    uncertainty=row.get("uncertainty"),
                    discrimination_power=row.get("discrimination_power"),
                    urgency=row.get("urgency"),
                    freshness_gap=row.get("freshness_gap"),
                    resolvability=row.get("resolvability"),
                    expected_cost_usd=row.get("estimated_cost_usd"),
                    expected_runtime_seconds=row.get("estimated_runtime_seconds"),
                    expected_human_review_minutes=row.get(
                        "expected_human_review_minutes"
                    ),
                ),
                status=QuestionStatus(str(row["status"])),
                not_before=_utc(row.get("not_before"), now),
                due_at=_utc(row.get("due_at"), now) if row.get("due_at") else None,
                expires_at=(
                    _utc(row.get("expires_at"), now) if row.get("expires_at") else None
                ),
                created_at=_utc(row.get("created_at"), now),
                blockers=tuple(str(item) for item in blockers),
            )
        )
    return tuple(output)


def _priority_snapshot(
    question: QuestionForPlanning, decision: PlanDecision, *, policy_version: str
) -> str:
    value = {
        "policy_version": policy_version,
        "materiality": question.priority.materiality,
        "uncertainty": question.priority.uncertainty,
        "discrimination_power": question.priority.discrimination_power,
        "urgency": question.priority.urgency,
        "freshness_gap": question.priority.freshness_gap,
        "resolvability": question.priority.resolvability,
        "expected_cost_usd": question.priority.expected_cost_usd,
        "expected_runtime_seconds": question.priority.expected_runtime_seconds,
        "expected_human_review_minutes": question.priority.expected_human_review_minutes,
        "score": decision.score,
        "blockers": decision.blockers,
    }
    return canonical_json(value)


def _persist_plan(
    session: Any,
    *,
    agenda: Agenda,
    questions: Sequence[QuestionForPlanning],
    policy: PlanPolicy,
    correlation_id: uuid.UUID,
    trigger_kind: str,
    trigger_ref: str | None,
    accepted_cutoff: datetime,
    materiality_policy_version: str,
) -> uuid.UUID:
    plan_id = uuid.uuid4()
    selected_count = len(agenda.selected)
    blocked_count = len(agenda.blocked)
    deferred_count = len(agenda.deferred)
    status = "noop" if selected_count == 0 else "completed"
    session.execute(
        text(
            """
            INSERT INTO research_plans (
                id, correlation_id, trigger_kind, trigger_ref, accepted_cutoff,
                priority_policy_version, materiality_policy_version,
                cost_budget_usd, runtime_budget_seconds, minimum_priority,
                status, considered_count, selected_count, blocked_count,
                deferred_count, reserved_cost_usd, reserved_runtime_seconds,
                no_op_reason, completed_at
            ) VALUES (
                :id, :correlation_id, :trigger_kind, :trigger_ref, :accepted_cutoff,
                :priority_policy_version, :materiality_policy_version,
                :cost_budget_usd, :runtime_budget_seconds, :minimum_priority,
                :status, :considered_count, :selected_count, :blocked_count,
                :deferred_count, :reserved_cost_usd, :reserved_runtime_seconds,
                :no_op_reason, NOW()
            )
            """
        ),
        {
            "id": plan_id,
            "correlation_id": correlation_id,
            "trigger_kind": trigger_kind,
            "trigger_ref": trigger_ref,
            "accepted_cutoff": accepted_cutoff,
            "priority_policy_version": agenda.policy_version,
            "materiality_policy_version": materiality_policy_version,
            "cost_budget_usd": policy.cost_budget_usd,
            "runtime_budget_seconds": policy.runtime_budget_seconds,
            "minimum_priority": policy.minimum_priority,
            "status": status,
            "considered_count": len(questions),
            "selected_count": selected_count,
            "blocked_count": blocked_count,
            "deferred_count": deferred_count,
            "reserved_cost_usd": agenda.reserved_cost_usd,
            "reserved_runtime_seconds": agenda.reserved_runtime_seconds,
            "no_op_reason": agenda.no_op_reason,
        },
    )
    by_id = {item.id: item for item in questions}
    decision_rows = []
    selected_question_ids = []
    for decision in agenda.decisions:
        question = by_id[decision.question_id]
        decision_rows.append(
            {
                "question_id": decision.question_id,
                "decision": decision.decision,
                "rank": decision.rank,
                "priority_score": decision.score,
                "blockers": list(decision.blockers),
                "reason_codes": list(decision.reason_codes),
                "priority_snapshot": _priority_snapshot(
                    question, decision, policy_version=agenda.policy_version
                ),
                "estimated_cost_usd": decision.estimated_cost_usd,
                "estimated_runtime_seconds": decision.estimated_runtime_seconds,
            }
        )
        if decision.decision == "selected":
            selected_question_ids.append(decision.question_id)
    session.execute(
        text(
            """
            INSERT INTO research_plan_decisions (
                plan_id, question_id, decision, rank, priority_score,
                blockers, reason_codes, priority_snapshot,
                estimated_cost_usd, estimated_runtime_seconds
            )
            SELECT
                :plan_id, d.question_id::UUID, d.decision, d.rank,
                d.priority_score,
                ARRAY(
                    SELECT JSONB_ARRAY_ELEMENTS_TEXT(d.blockers)
                ),
                ARRAY(
                    SELECT JSONB_ARRAY_ELEMENTS_TEXT(d.reason_codes)
                ),
                CAST(d.priority_snapshot AS JSONB),
                d.estimated_cost_usd, d.estimated_runtime_seconds
            FROM JSONB_TO_RECORDSET(CAST(:decisions AS JSONB)) AS d(
                question_id TEXT,
                decision TEXT,
                rank INTEGER,
                priority_score NUMERIC,
                blockers JSONB,
                reason_codes JSONB,
                priority_snapshot TEXT,
                estimated_cost_usd NUMERIC,
                estimated_runtime_seconds INTEGER
            )
            """
        ),
        {
            "plan_id": plan_id,
            "decisions": canonical_json(decision_rows),
        },
    )
    session.execute(
        text(
            """
            UPDATE research_questions
            SET status = 'planned'
            WHERE id = ANY(CAST(:question_ids AS UUID[]))
              AND status = 'pending'
            """
        ),
        {"question_ids": selected_question_ids},
    )
    return plan_id


def _create_work_orders(
    session: Any,
    *,
    plan_id: uuid.UUID,
    agenda: Agenda,
    questions: Sequence[QuestionForPlanning],
    correlation_id: uuid.UUID,
    budget_reservation_id: uuid.UUID | None,
) -> tuple[uuid.UUID, ...]:
    by_id = {item.id: item for item in questions}
    work_order_ids: list[uuid.UUID] = []
    for decision in agenda.selected:
        question = by_id[decision.question_id]
        question_row = result_first(session.execute(
            text("SELECT question_type FROM research_questions WHERE id = :id"),
            {"id": question.id},
        ))
        if question_row is None:
            continue
        skill = result_first(session.execute(
            text(
                """
                SELECT id, skill_key, version, content_fingerprint,
                       maximum_attempts
                FROM research_skill_versions
                WHERE promotion_status = 'active'
                  AND :question_type = ANY(supported_question_types)
                ORDER BY skill_key, version DESC
                LIMIT 1
                """
            ),
            {"question_type": question_row["question_type"]},
        ))
        if skill is None:
            continue
        work_order_id = uuid.uuid4()
        input_fingerprint = content_fingerprint(
            {
                "question_id": question.id,
                "skill_version_id": skill["id"],
                "skill_fingerprint": skill["content_fingerprint"],
                "accepted_cutoff": question.accepted_cutoff,
                "policy_version": agenda.policy_version,
            }
        )
        job_result = enqueue_job(
            session,
            job_type="research_skill",
            dedupe_key=f"research-skill:{question.id}",
            input_fingerprint=input_fingerprint,
            payload={"work_order_id": str(work_order_id)},
            correlation_id=correlation_id,
            priority=1000 - (decision.rank or 0),
            max_attempts=int(skill["maximum_attempts"]),
            not_before=question.not_before,
        )
        if job_result.job is None:
            continue
        job_id = job_result.job.id
        row = result_first(session.execute(
            text(
                """
                INSERT INTO research_work_orders (
                    id, question_id, plan_id, skill_version_id,
                    analysis_job_id, budget_reservation_id, accepted_cutoff,
                    planning_policy_version, priority_snapshot,
                    estimated_value, reserved_cost_usd,
                    reserved_runtime_seconds, input_fingerprint,
                    status, queued_at
                ) VALUES (
                    :id, :question_id, :plan_id, :skill_version_id,
                    :analysis_job_id, :budget_reservation_id, :accepted_cutoff,
                    :planning_policy_version, CAST(:priority_snapshot AS JSONB),
                    :estimated_value, :reserved_cost_usd,
                    :reserved_runtime_seconds, :input_fingerprint,
                    'queued', NOW()
                )
                ON CONFLICT DO NOTHING
                RETURNING id
                """
            ),
            {
                "id": work_order_id,
                "question_id": question.id,
                "plan_id": plan_id,
                "skill_version_id": skill["id"],
                "analysis_job_id": job_id,
                "budget_reservation_id": budget_reservation_id,
                "accepted_cutoff": question.accepted_cutoff,
                "planning_policy_version": agenda.policy_version,
                "priority_snapshot": _priority_snapshot(
                    question,
                    decision,
                    policy_version=agenda.policy_version,
                ),
                "estimated_value": decision.score,
                "reserved_cost_usd": decision.estimated_cost_usd,
                "reserved_runtime_seconds": decision.estimated_runtime_seconds,
                "input_fingerprint": input_fingerprint,
            },
        ))
        if row is None:
            existing = result_first(session.execute(
                text(
                    """
                    SELECT id FROM research_work_orders
                    WHERE question_id = :question_id
                      AND status IN ('planned', 'queued', 'leased', 'running', 'failed_retryable')
                    ORDER BY created_at DESC, id DESC LIMIT 1
                    """
                ),
                {"question_id": question.id},
            ))
            if existing is not None:
                work_order_ids.append(uuid.UUID(str(existing["id"])))
            continue
        session.execute(
            text(
                """
                UPDATE research_questions
                SET status = 'queued', attempt_count = attempt_count + 1
                WHERE id = :question_id
                  AND status = 'planned'
                  AND accepted_cutoff = :accepted_cutoff
                """
            ),
            {"question_id": question.id, "accepted_cutoff": question.accepted_cutoff},
        )
        work_order_ids.append(work_order_id)
    if work_order_ids:
        append_ui_invalidations(
            session,
            {
                "research_questions",
                "research_work_orders",
                "research_control_plane",
                "system_topology",
            },
        )
    return tuple(work_order_ids)


def _has_trusted_manual_budget_override(
    session: Any, correlation_id: uuid.UUID, now: datetime
) -> bool:
    row = result_first(session.execute(
        text(
            """
            SELECT summary -> 'budget_override' AS budget_override
            FROM cycle_runs
            WHERE correlation_id = :correlation_id
              AND triggered_by = 'api_manual_override'
              AND status = 'running'
              AND run_kind = 'research'
              AND requested_component = 'research_control_plane'
            """
        ),
        {"correlation_id": correlation_id},
    ))
    override = row.get("budget_override") if row else None
    if not isinstance(override, Mapping) or override.get("requested") is not True:
        return False
    expires_at = override.get("expires_at")
    if not isinstance(expires_at, str):
        return False
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return expiry.tzinfo is not None and expiry.astimezone(UTC) > now


def _defer_for_global_budget(agenda: Agenda) -> Agenda:
    decisions = tuple(
        PlanDecision(
            question_id=item.question_id,
            decision="deferred",
            rank=None,
            score=item.score,
            blockers=item.blockers,
            reason_codes=("global_daily_budget_exceeded",),
            estimated_cost_usd=item.estimated_cost_usd,
            estimated_runtime_seconds=item.estimated_runtime_seconds,
        )
        if item.decision == "selected"
        else item
        for item in agenda.decisions
    )
    return Agenda(
        policy_version=agenda.policy_version,
        decisions=decisions,
        reserved_cost_usd=Decimal("0"),
        reserved_runtime_seconds=0,
        no_op_reason="global_daily_budget_exceeded",
    )


def run_planner(
    session: Any,
    config: Mapping[str, Any],
    *,
    correlation_id: uuid.UUID,
    trigger_kind: str,
    trigger_ref: str | None = None,
    accepted_cutoff: datetime | None = None,
) -> PlannerRunResult:
    """Generate, rank and atomically enqueue a bounded agenda in one transaction."""
    cutoff = accepted_cutoff or datetime.now(UTC)
    admission_time = datetime.now(UTC)
    acquired = result_first(session.execute(
        text("SELECT pg_try_advisory_xact_lock(hashtext(:key)) AS acquired"),
        {"key": _PLANNER_LOCK_KEY},
    ))
    if not acquired or not acquired.get("acquired"):
        return PlannerRunResult(
            None, correlation_id, "coalesced", 0, 0, (), True, "planner_already_running"
        )
    settings = _mapping(config.get("research_control_plane"))
    refresh_questions_from_state(
        session,
        accepted_cutoff=cutoff,
        catalyst_lookahead_days=int(settings.get("catalyst_lookahead_days", 30)),
        stale_question_days=int(settings.get("stale_question_days", 14)),
        limit=int(settings.get("maximum_questions_per_plan", 20)),
    )
    questions = load_planner_questions(
        session,
        now=cutoff,
        limit=int(settings.get("maximum_questions_per_plan", 20)),
    )
    policy = PlanPolicy(
        now=cutoff,
        cost_budget_usd=settings.get("model_budget_usd_per_plan", 1),
        runtime_budget_seconds=int(
            settings.get("maximum_runtime_seconds_per_plan", 900)
        ),
        maximum_work_orders=int(settings.get("maximum_work_orders_per_plan", 8)),
        minimum_priority=settings.get("minimum_priority", 0),
        policy_version=str(settings.get("priority_policy_version", "v1")),
    )
    agenda = plan_questions(questions, policy)
    budget_reservation_id: uuid.UUID | None = None
    if agenda.selected:
        from budgets import BudgetExceeded, reserve_budget_quota

        trusted_override = (
            trigger_kind == "manual"
            and _has_trusted_manual_budget_override(
                session, correlation_id, admission_time
            )
        )
        try:
            reservation_id = reserve_budget_quota(
                dict(config),
                processor="research_control_plane",
                estimate_usd=(
                    0.0 if trusted_override else float(agenda.reserved_cost_usd)
                ),
                ttl_seconds=min(
                    86400,
                    max(600, policy.runtime_budget_seconds + 300),
                ),
                correlation_id=str(correlation_id),
                run_kind="research",
                component="research_control_plane",
                now=admission_time,
                session=session,
            )
            budget_reservation_id = uuid.UUID(reservation_id)
        except BudgetExceeded:
            agenda = _defer_for_global_budget(agenda)
    plan_id = _persist_plan(
        session,
        agenda=agenda,
        questions=questions,
        policy=policy,
        correlation_id=correlation_id,
        trigger_kind=trigger_kind,
        trigger_ref=trigger_ref,
        accepted_cutoff=cutoff,
        materiality_policy_version=str(
            settings.get("materiality_policy_version", "v1")
        ),
    )
    work_orders = _create_work_orders(
        session,
        plan_id=plan_id,
        agenda=agenda,
        questions=questions,
        correlation_id=correlation_id,
        budget_reservation_id=budget_reservation_id,
    )
    return PlannerRunResult(
        plan_id=plan_id,
        correlation_id=correlation_id,
        status="noop" if not work_orders else "completed",
        selected_count=len(work_orders),
        considered_count=len(questions),
        work_order_ids=work_orders,
        coalesced=False,
        no_op_reason=agenda.no_op_reason if not work_orders else None,
    )


def mark_work_order_running(
    session: Any, *, work_order_id: uuid.UUID, worker_id: str
) -> bool:
    result = session.execute(
        text(
            """
            UPDATE research_work_orders w
            SET status = 'running', worker_id = :worker_id,
                attempt_count = w.attempt_count + 1
            FROM research_questions q, research_skill_versions s
            WHERE w.id = :work_order_id
              AND q.id = w.question_id
              AND s.id = w.skill_version_id
              AND w.status IN ('queued', 'leased', 'running', 'failed_retryable')
              AND q.status IN ('queued', 'running')
              AND (q.expires_at IS NULL OR q.expires_at > NOW())
              AND w.attempt_count < s.maximum_attempts
            RETURNING w.question_id
            """
        ),
        {"work_order_id": work_order_id, "worker_id": worker_id[:200]},
    )
    row = result_first(result)
    if row is None:
        return False
    session.execute(
        text(
            """
            UPDATE research_questions
            SET status = 'running'
            WHERE id = :question_id AND status = 'queued'
            """
        ),
        {"question_id": row["question_id"]},
    )
    return True


def _settle_work_order_budget(session: Any, work_order_id: uuid.UUID) -> None:
    """Settle one shared plan reservation after every attached order is terminal."""
    session.execute(
        text(
            """
            UPDATE budget_reservations b
            SET status = 'settled',
                settled_usd = COALESCE((
                    SELECT SUM(e.cost_usd)
                    FROM research_work_orders w
                    JOIN research_effects e ON e.work_order_id = w.id
                    WHERE w.budget_reservation_id = b.id
                ), 0),
                settled_at = NOW()
            WHERE b.id = (
                SELECT budget_reservation_id
                FROM research_work_orders
                WHERE id = :work_order_id
            )
              AND b.status IN ('active', 'expired')
              AND NOT EXISTS (
                  SELECT 1 FROM research_work_orders active
                  WHERE active.budget_reservation_id = b.id
                    AND active.status IN (
                        'planned', 'queued', 'leased', 'running', 'failed_retryable'
                    )
              )
            """
        ),
        {"work_order_id": work_order_id},
    )


def complete_work_order(
    session: Any,
    *,
    work_order_id: uuid.UUID,
    accepted_cutoff: datetime,
    result: Mapping[str, Any],
    material_effect_summary: str,
    resolution_summary: str,
    resolution_evidence_refs: Sequence[str],
) -> str:
    """Accept completion only while the question still matches its cutoff."""
    matched = result_first(session.execute(
        text(
            """
            SELECT w.id
            FROM research_work_orders w
            JOIN research_questions q ON q.id = w.question_id
            WHERE w.id = :work_order_id
              AND w.status = 'running'
              AND q.status = 'running'
              AND w.accepted_cutoff = :accepted_cutoff
              AND q.accepted_cutoff = :accepted_cutoff
            FOR UPDATE OF w, q
            """
        ),
        {"work_order_id": work_order_id, "accepted_cutoff": accepted_cutoff},
    ))
    if matched is None:
        session.execute(
            text(
                """
                UPDATE research_work_orders
                SET status = 'stale', error_kind = 'accepted_cutoff_mismatch'
                WHERE id = :work_order_id
                  AND status IN ('queued', 'leased', 'running', 'failed_retryable')
                """
            ),
            {"work_order_id": work_order_id},
        )
        _settle_work_order_budget(session, work_order_id)
        return "stale"
    encoded_result = canonical_json(result)
    session.execute(
        text(
            """
            UPDATE research_work_orders
            SET status = 'completed', result = CAST(:result AS JSONB),
                material_effect_summary = :material_effect_summary
            WHERE id = :work_order_id AND status = 'running'
            """
        ),
        {
            "work_order_id": work_order_id,
            "result": encoded_result,
            "material_effect_summary": material_effect_summary[:4000],
        },
    )
    result_status = str(result.get("status") or "unresolved")
    question_status = (
        "resolved" if result_status in {"resolved", "noop"} else "unresolvable"
    )
    session.execute(
        text(
            """
            UPDATE research_questions q
            SET status = :question_status,
                resolution_summary = CASE
                    WHEN :question_status = 'resolved' THEN :resolution_summary
                    ELSE NULL
                END,
                unresolved_reason = CASE
                    WHEN :question_status = 'unresolvable' THEN :resolution_summary
                    ELSE NULL
                END,
                resolution_evidence_refs = :resolution_evidence_refs,
                dirty_since = NULL
            FROM research_work_orders w
            WHERE w.id = :work_order_id AND q.id = w.question_id
              AND q.status = 'running'
              AND q.accepted_cutoff = :accepted_cutoff
            """
        ),
        {
            "work_order_id": work_order_id,
            "accepted_cutoff": accepted_cutoff,
            "question_status": question_status,
            "resolution_summary": resolution_summary[:4000],
            "resolution_evidence_refs": list(resolution_evidence_refs)[:256],
        },
    )
    _settle_work_order_budget(session, work_order_id)
    return "completed"


def mark_work_order_failure(
    session: Any,
    *,
    analysis_job_id: uuid.UUID,
    retryable: bool,
    error_kind: str,
) -> bool:
    """Mirror durable-job failure without persisting exception text."""
    status = "failed_retryable" if retryable else "failed_terminal"
    row = result_first(session.execute(
        text(
            """
            UPDATE research_work_orders
            SET status = :status, error_kind = :error_kind
            WHERE analysis_job_id = :analysis_job_id
              AND status IN ('queued', 'leased', 'running', 'failed_retryable')
            RETURNING id, question_id
            """
        ),
        {
            "analysis_job_id": analysis_job_id,
            "status": status,
            "error_kind": error_kind[:200],
        },
    ))
    if row is None:
        return False
    if not retryable:
        session.execute(
            text(
                """
                UPDATE research_questions
                SET status = 'unresolvable',
                    unresolved_reason = :reason
                WHERE id = :question_id



                  AND status IN ('queued', 'running')
                """
            ),
            {
                "question_id": row["question_id"],
                "reason": f"research skill failed: {error_kind}"[:1000],
            },
        )
        _settle_work_order_budget(session, uuid.UUID(str(row["id"])))
    append_ui_invalidations(
        session,
        {
            "research_questions",
            "research_work_orders",
            "research_control_plane",
            "system_topology",
        },
    )
    return True


def reconcile_terminal_work_order_failures(session: Any, *, limit: int = 100) -> int:
    """Mirror bounded terminal durable-job recovery into research state."""
    bounded_limit = max(1, min(int(limit), 1000))
    rows = result_rows(session.execute(
        text(
            """
            SELECT j.id
            FROM analysis_jobs j
            JOIN research_work_orders w ON w.analysis_job_id = j.id
            WHERE j.job_type = 'research_skill'
              AND j.state = 'failed_terminal'
              AND w.status IN (
                  'queued', 'leased', 'running', 'failed_retryable'
              )
            ORDER BY j.completed_at NULLS LAST, j.created_at, j.id
            LIMIT :limit
            FOR UPDATE OF w SKIP LOCKED
            """
        ),
        {"limit": bounded_limit},
    ))
    mirrored = 0
    for row in rows:
        if mark_work_order_failure(
            session,
            analysis_job_id=uuid.UUID(str(row["id"])),
            retryable=False,
            error_kind="LeaseExpiredFinalAttempt",
        ):
            mirrored += 1
    return mirrored


def enqueue_planner_job(
    config: Mapping[str, Any],
    *,
    trigger_kind: str,
    trigger_ref: str | None = None,
    dedupe_ref: str | None = None,
    accepted_cutoff: datetime | None = None,
    force: bool = False,
    correlation_id: uuid.UUID | None = None,
) -> Mapping[str, Any]:
    """Enqueue one coalesced planner run through the durable analysis queue."""
    from db import get_session

    if trigger_kind not in {"scheduled", "event", "manual", "recovery"}:
        raise ValueError("unsupported planner trigger kind")
    cutoff = accepted_cutoff or datetime.now(UTC)
    correlation_id = correlation_id or uuid.uuid4()
    settings = _mapping(config.get("research_control_plane"))
    debounce_seconds = max(
        1, min(int(settings.get("event_debounce_seconds", 120)), 3600)
    )
    bucket = int(cutoff.timestamp()) // debounce_seconds
    identity_ref = trigger_ref if dedupe_ref is None else dedupe_ref
    identity = {
        "trigger_kind": trigger_kind,
        "trigger_ref": identity_ref,
        "accepted_cutoff_bucket": (str(correlation_id) if force else bucket),
        "priority_policy_version": settings.get("priority_policy_version", "v1"),
    }
    fingerprint = content_fingerprint(identity)
    with get_session(dict(config)) as session:
        enqueued = enqueue_job(
            session,
            job_type="research_planner",
            dedupe_key=f"research-planner:{trigger_kind}:{identity_ref or 'global'}",
            input_fingerprint=fingerprint,
            payload={
                "trigger_kind": trigger_kind,
                "trigger_ref": trigger_ref,
                "accepted_cutoff": cutoff.isoformat(),
            },
            correlation_id=correlation_id,
            priority=95 if trigger_kind == "event" else 80,
            max_attempts=5,
            not_before=cutoff,
        )
        return {
            "status": "accepted" if enqueued.inserted else "coalesced",
            "job_id": str(enqueued.job.id) if enqueued.job else None,
            "correlation_id": str(correlation_id),
            "created": enqueued.inserted,
            "coalesced": not enqueued.inserted,
        }


__all__ = [
    "PlannerRunResult",
    "QuestionDraft",
    "complete_work_order",
    "enqueue_planner_job",
    "load_planner_questions",
    "mark_work_order_running",
    "mark_work_order_failure",
    "reconcile_terminal_work_order_failures",
    "questions_from_event",
    "questions_from_falsification",
    "questions_from_promoted_candidate",
    "refresh_questions_from_state",
    "run_planner",
    "upsert_question",
]
