"""Bounded end-to-end macro and dynamic research intelligence workflows."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import fields, is_dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from budgets import BudgetContext
from logging_config import get_logger
from research_intelligence.adversarial import validate_adversarial_output
from research_intelligence.claims import (
    CLAIM_ELIGIBLE_EVIDENCE_TYPES,
    claim_evidence,
    persist_source_claims,
    validate_claim_output,
)
from research_intelligence.config import ResearchSettings
from research_intelligence.contracts import (
    CandidateGroup,
    ModelProvenance,
    NormalizedEntity,
    NormalizedEvidence,
    canonical_fingerprint,
)
from research_intelligence.deliverable import validate_deliverable_output
from research_intelligence.discovery import (
    PatternAssessment,
    build_candidate_groups,
    pattern_prompt_payload,
    select_case_match,
    token_similarity,
    validate_pattern_output,
)
from research_intelligence.evidence import (
    EvidenceRegistry,
    MacroObservationAdapter,
    MacroReleaseAdapter,
    MarketConfirmationAdapter,
    MarketStateAdapter,
    OfficialDocumentAdapter,
    StoryClusterAdapter,
)
from research_intelligence.graph import validate_causal_output
from research_intelligence.lifecycle import next_lifecycle_state
from research_intelligence.market_drivers import (
    FactorMarketAssessment,
    market_driver_input_fingerprint,
    validate_factor_market_output,
)
from research_intelligence.models import (
    STAGE_VERSIONS,
    ModelStageResult,
    ResearchModelRunner,
    ResearchRunBudgetExceeded,
)
from research_intelligence.queries import get_case
from research_intelligence.relationships import causal_edge_fingerprint
from research_intelligence.repository import (
    current_market_drivers,
    ensure_hypothesis_data_requests,
    find_case_match_rows,
    load_case_stats,
    persist_adversarial,
    persist_causal_edges,
    persist_economic_factors,
    persist_market_drivers,
    persist_value_capture,
    promote_case_to_theme,
    publish_case_snapshot,
    refresh_case_lifecycles,
    unresolved_material_hypotheses,
    upsert_case,
)
from research_intelligence.value_capture import validate_value_capture_output

logger = get_logger("research_intelligence.service")



def _savepoint(session: Any):
    begin = getattr(session, "begin_nested", None)
    return begin() if callable(begin) else nullcontext()


def _clean(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _clean(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, datetime):
        normalized = value if value.tzinfo else value.replace(tzinfo=UTC)
        return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_clean(item) for item in value]
    return value


def _parse_cached(value: Any) -> Any:
    if not isinstance(value, str):
        raise ValueError("cached model response is not text")
    content = value.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(content)


def run_model_stage[T](
    session: Any,
    runner: ResearchModelRunner,
    stage: str,
    payload: Any,
    validator: Callable[[Any], T],
    input_fingerprint: str,
    *,
    force: bool = False,
) -> ModelStageResult:
    cache_identity = runner.cache_identity(stage)
    if not force:
        row = None
        try:
            result = session.execute(
                text(
                    """
                    SELECT attempt_id, raw_response, model_used
                    FROM generation_attempts
                    WHERE stage = :stage AND status = 'validated'
                      AND request_metadata->>'input_fingerprint' = :input_fingerprint
                      AND request_metadata->'cache_identity' =
                          CAST(:cache_identity AS JSONB)
                    ORDER BY created_at DESC LIMIT 1
                    """
                ),
                {
                    "stage": STAGE_VERSIONS[stage],
                    "input_fingerprint": input_fingerprint,
                    "cache_identity": json.dumps(cache_identity, sort_keys=True),
                },
            )
            try:
                mapped = result.mappings().first()
                row = dict(mapped) if mapped is not None else None
            except (AttributeError, TypeError):
                raw = result.first()
                row = dict(raw._mapping) if raw is not None else None
        except Exception:
            row = None
        if row and row.get("raw_response"):
            try:
                value = validator(_parse_cached(row["raw_response"]))
            except (TypeError, ValueError):
                pass
            else:
                return ModelStageResult(
                    value=value,
                    provenance=ModelProvenance(
                        model_slug=str(row.get("model_used") or "") or None,
                        prompt_version=STAGE_VERSIONS[stage],
                        generation_attempt_id=str(row["attempt_id"]),
                        input_fingerprint=input_fingerprint,
                        metadata={"reused": True, "cache_identity": cache_identity},
                    ),
                    cost_usd=0.0,
                    tokens_input=0,
                    tokens_output=0,
                    duration_ms=0,
                )
    return runner.run(stage, payload, validator, input_fingerprint=input_fingerprint)


def _stage_fingerprint(stage: str, payload: Any) -> str:
    return canonical_fingerprint(
        {"stage": STAGE_VERSIONS[stage], "input": payload}
    )




def _extract_claims(
    session: Any,
    runner: ResearchModelRunner,
    evidence: Sequence[NormalizedEvidence],
    *,
    force: bool,
) -> tuple[NormalizedEvidence, ...]:
    eligible = tuple(
        item
        for item in evidence
        if item.evidence_type in CLAIM_ELIGIBLE_EVIDENCE_TYPES
        and item.bounded_excerpt
    )[: runner.settings.maximum_claim_documents_per_run]
    if not eligible or not runner.settings.claim_extraction_enabled:
        return ()
    payload = {"evidence": [item.to_dict() for item in eligible]}
    fingerprint = _stage_fingerprint("claim_extraction", payload)
    result = run_model_stage(session,
    runner,
    "claim_extraction",
    payload,
    lambda output: validate_claim_output(output, eligible),
    fingerprint,
    force=force,)
    claims = result.value or ()
    if claims:
        with _savepoint(session):
            persist_source_claims(session, claims, result.provenance)
    return claim_evidence(claims, result.provenance)


def _pattern_payload(group: CandidateGroup) -> dict[str, Any]:
    return json.loads(pattern_prompt_payload(group))


def _pipeline_fingerprint(group: CandidateGroup, settings: ResearchSettings) -> str:
    return canonical_fingerprint(
        {
            "evidence": group.input_fingerprint,
            "versions": STAGE_VERSIONS,
            "bounds": {
                "graph_depth": settings.graph_depth,
                "maximum_graph_edges": settings.maximum_graph_edges,
                "evidence_per_candidate": settings.evidence_per_candidate,
            },
        }
    )


def _edge_fingerprints(edges: Sequence[Any]) -> tuple[str, ...]:
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


def _validate_value_nodes(output: Any, evidence: Sequence[NormalizedEvidence], nodes: set[tuple[str, str]]):
    assessments = validate_value_capture_output(output, evidence)
    unsupported = [
        (item.node_type, item.node_key)
        for item in assessments
        if nodes and (item.node_type, item.node_key) not in nodes
    ]
    if unsupported:
        raise ValueError("value-capture output references a node outside the causal graph")
    return assessments


def _current_lifecycle(session: Any, case_id: str) -> str:
    result = session.execute(
        text("SELECT lifecycle_state FROM research_cases WHERE id = :case_id LIMIT 1"),
        {"case_id": case_id},
    )
    try:
        row = result.mappings().first()
        return str(row["lifecycle_state"]) if row is not None else "candidate"
    except (AttributeError, TypeError):
        row = result.first()
        return str(row._mapping["lifecycle_state"]) if row is not None else "candidate"


def _process_group(
    session: Any,
    runner: ResearchModelRunner,
    group: CandidateGroup,
    existing_cases: list[dict[str, Any]],
    *,
    correlation_id: str | None,
    forced_match: Mapping[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    settings = runner.settings
    pipeline_fingerprint = _pipeline_fingerprint(group, settings)
    if not force:
        completed = next(
            (
                row
                for row in existing_cases
                if row.get("blocking_key") == group.blocking_key
                and row.get("pipeline_input_fingerprint") == pipeline_fingerprint
                and str(row.get("pipeline_complete") or "").casefold() == "true"
            ),
            None,
        )
        if completed:
            return {
                "case_id": str(completed["id"]),
                "status": "unchanged",
                "pipeline_input_fingerprint": pipeline_fingerprint,
            }
    pattern_payload = _pattern_payload(group)
    pattern_fingerprint = _stage_fingerprint("pattern_discovery", pattern_payload)
    pattern_result = run_model_stage(session,
    runner,
    "pattern_discovery",
    pattern_payload,
    lambda output: validate_pattern_output(output, group),
    pattern_fingerprint,
    force=force,)
    assessment: PatternAssessment | None = pattern_result.value
    if assessment is None:
        return {"case_id": None, "status": "abstained"}
    matched = forced_match or select_case_match(
        assessment, existing_cases, settings.merge_similarity_threshold
    )
    with _savepoint(session):
        mutation = upsert_case(
            session,
            assessment,
            group.evidence,
            evidence_input_fingerprint=group.input_fingerprint,
            provenance=pattern_result.provenance,
            correlation_id=correlation_id,
            matched_case=matched,
        )
    case_id = mutation.case_id
    case_context = {
        "case_id": case_id,
        "case": _clean(assessment),
        "evidence": [item.to_dict() for item in group.evidence],
    }

    causal_payload = {
        **case_context,
        "bounds": {
            "maximum_depth": settings.graph_depth,
            "maximum_edges": settings.maximum_graph_edges,
        },
    }
    causal_fp = _stage_fingerprint("causal_chain", causal_payload)
    causal_result = run_model_stage(session,
    runner,
    "causal_chain",
    causal_payload,
    lambda output: validate_causal_output(
        output, group.evidence, settings, seed_entities=assessment.entities
    ),
    causal_fp,
    force=force,)
    edges = causal_result.value or ()
    with _savepoint(session):
        persist_causal_edges(
            session, case_id, edges, group.evidence, causal_result.provenance
        )
    with _savepoint(session):
        hypothesis_request_counts = ensure_hypothesis_data_requests(
            session, case_id, edges, group.evidence
        )
    edge_fingerprints = _edge_fingerprints(edges)
    allowed_nodes = {
        (edge.from_type, edge.from_key) for edge in edges
    } | {(edge.to_type, edge.to_key) for edge in edges}

    value_payload = {
        **case_context,
        "causal_edges": _clean(edges),
        "dimensions": [
            "demand_impulse",
            "revenue_exposure",
            "volume_sensitivity",
            "supply_responsiveness",
            "scarcity",
            "pricing_power",
            "cost_pass_through",
            "margin_sensitivity",
            "capital_intensity",
            "competitive_intensity",
            "barriers_to_entry",
            "capacity_lead_time",
            "substitution_risk",
            "balance_sheet_capacity",
            "capital_allocation",
            "public_market_investability",
            "valuation",
            "crowding",
            "evidence_strength",
        ],
    }
    value_fp = _stage_fingerprint("value_capture", value_payload)
    value_result = run_model_stage(session,
    runner,
    "value_capture",
    value_payload,
    lambda output: _validate_value_nodes(output, group.evidence, allowed_nodes),
    value_fp,
    force=force,)
    value_assessments = value_result.value or ()
    with _savepoint(session):
        assessment_nodes = persist_value_capture(
            session,
            case_id,
            value_assessments,
            group.evidence,
            value_result.provenance,
        )

    current_detail = get_case(session, case_id, detail_limit=100) or {}
    adversarial_payload = {
        **case_context,
        "causal_edges": _clean(edges),
        "edge_fingerprints": edge_fingerprints,
        "value_capture": _clean(value_assessments),
        "existing_data_requests": current_detail.get("data_requests", []),
    }
    adversarial_fp = _stage_fingerprint("adversarial", adversarial_payload)
    adversarial_result = run_model_stage(session,
    runner,
    "adversarial",
    adversarial_payload,
    lambda output: validate_adversarial_output(
        output,
        group.evidence,
        edge_fingerprints=edge_fingerprints,
        maximum_counterevidence=30,
        maximum_requests=20,
    ),
    adversarial_fp,
    force=force,)
    adversarial = adversarial_result.value
    adversarial_counts = {"counterevidence": 0, "data_requests": 0}
    if adversarial is not None:
        with _savepoint(session):
            adversarial_counts = persist_adversarial(
                session,
                case_id,
                adversarial,
                group.evidence,
                adversarial_result.provenance,
            )

    deliverable_payload = {
        **case_context,
        "causal_edges": _clean(edges),
        "edge_fingerprints": edge_fingerprints,
        "value_capture": _clean(value_assessments),
        "counterevidence": _clean(adversarial),
    }
    deliverable_fp = _stage_fingerprint("deliverable", deliverable_payload)
    deliverable_result = run_model_stage(session,
    runner,
    "deliverable",
    deliverable_payload,
    lambda output: validate_deliverable_output(
        output,
        group.evidence,
        edge_fingerprints=edge_fingerprints,
        assessment_nodes=assessment_nodes,
    ),
    deliverable_fp,
    force=force,)
    deliverable = deliverable_result.value

    stats = load_case_stats(session, case_id)
    stats = replace(
        stats,
        snapshot_count=stats.snapshot_count + 1,
        has_causal_chain=bool(edges),
        has_value_capture=bool(value_assessments),
        has_adversarial_review=adversarial is not None,
        has_deliverable=deliverable is not None,
    )
    lifecycle = next_lifecycle_state(
        _current_lifecycle(session, case_id), stats, settings
    ).value
    unresolved_hypotheses = unresolved_material_hypotheses(session, case_id)
    if unresolved_hypotheses and lifecycle in {"research_ready", "mature"}:
        lifecycle = "corroborated"
    payload = {
        "pipeline_complete": True,
        "pipeline_input_fingerprint": pipeline_fingerprint,
        "evidence_input_fingerprint": group.input_fingerprint,
        "blocking_key": group.blocking_key,
        "case": _clean(assessment),
        "lifecycle_state": lifecycle,
        "evidence": [item.to_dict() for item in group.evidence],
        "causal_edges": _clean(edges),
        "value_capture": _clean(value_assessments),
        "adversarial": _clean(adversarial),
        "adversarial_counts": adversarial_counts,
        "deliverable": _clean(deliverable),
        "macro_drivers": list(assessment.macro_drivers),
        "invalidation_conditions": list(
            adversarial.invalidation_conditions if adversarial is not None else ()
        ),
        "unresolved_material_hypotheses": unresolved_hypotheses,
        "hypothesis_request_counts": hypothesis_request_counts,
        "model_provenance": {
            "pattern_discovery": _clean(pattern_result.provenance),
            "causal_chain": _clean(causal_result.provenance),
            "value_capture": _clean(value_result.provenance),
            "adversarial": _clean(adversarial_result.provenance),
            "deliverable": _clean(deliverable_result.provenance),
        },
    }
    snapshot_fingerprint = canonical_fingerprint(
        {
            "case": payload["case"],
            "lifecycle_state": lifecycle,
            "evidence_input_fingerprint": group.input_fingerprint,
            "causal_edges": payload["causal_edges"],
            "value_capture": payload["value_capture"],
            "adversarial": payload["adversarial"],
            "deliverable": payload["deliverable"],
            "unresolved_material_hypotheses": unresolved_hypotheses,
        }
    )
    change_summary = (
        deliverable.what_changed.text
        if deliverable is not None
        else assessment.what_changed
    )
    with _savepoint(session):
        snapshot = publish_case_snapshot(
            session,
            case_id,
            lifecycle_state=lifecycle,
            payload=payload,
            input_fingerprint=snapshot_fingerprint,
            change_summary=change_summary,
            provenance=deliverable_result.provenance,
            correlation_id=correlation_id,
        )
    theme = None
    if lifecycle in {"research_ready", "mature"}:
        with _savepoint(session):
            theme = promote_case_to_theme(
                session,
                case_id,
                similarity_threshold=settings.merge_similarity_threshold,
            )
    return {
        "case_id": case_id,
        "semantic_fingerprint": assessment.semantic_fingerprint,
        "title": assessment.label,
        "status": "created"
        if mutation.created
        else ("updated" if snapshot.changed else "unchanged"),
        "lifecycle_state": lifecycle,
        "snapshot_version": snapshot.version,
        "snapshot_changed": snapshot.changed,
        "theme": theme,
        "evidence_count": len(group.evidence),
        "causal_edge_count": len(edges),
        "value_capture_count": len(value_assessments),
        "counterevidence_count": adversarial_counts["counterevidence"],
        "data_request_count": (
            adversarial_counts["data_requests"]
            + hypothesis_request_counts["requests"]
        ),
        "unresolved_material_hypotheses": unresolved_hypotheses,
        "model_cost_usd": runner.cost_usd,
    }


def run_discovery(
    session: Any,
    config: dict[str, Any],
    *,
    correlation_id: str | None = None,
    budget_context: BudgetContext | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Discover and fully investigate bounded dynamic case candidates."""
    settings = ResearchSettings.from_config(config)
    if not settings.enabled:
        return {"status": "disabled", "cases": [], "errors": []}
    errors: list[dict[str, str]] = []
    try:
        with _savepoint(session):
            lifecycle_transitions = refresh_case_lifecycles(
                session,
                settings,
                correlation_id=correlation_id,
                now=now,
            )
    except Exception as exc:
        lifecycle_transitions = []
        errors.append(
            {"stage": "lifecycle_refresh", "error": type(exc).__name__}
        )
        logger.warning(
            "research_lifecycle_refresh_failed", error_type=type(exc).__name__
        )
    registry = EvidenceRegistry()
    collection = registry.collect(
        session,
        rolling_window_days=settings.rolling_window_days,
        limit=settings.maximum_candidate_evidence,
        now=now,
    )
    runner = ResearchModelRunner(
        config,
        correlation_id=correlation_id,
        session=session,
        budget_context=budget_context,
    )
    evidence = list(collection.items)
    # Adapter and candidate failures remain isolated from lifecycle maintenance.
    try:
        extracted = _extract_claims(
            session, runner, collection.items, force=force
        )
        known = {item.ref for item in evidence}
        evidence.extend(item for item in extracted if item.ref not in known)
    except Exception as exc:
        errors.append({"stage": "claim_extraction", "error": type(exc).__name__})
        logger.warning("research_claim_extraction_failed", error_type=type(exc).__name__)
    groups = build_candidate_groups(
        evidence,
        settings,
        maximum_groups=settings.maximum_cases_per_run,
    )
    existing = find_case_match_rows(session)
    outcomes: list[dict[str, Any]] = []
    for group in groups[: settings.maximum_cases_per_run]:
        try:
            outcome = _process_group(
                session,
                runner,
                group,
                existing,
                correlation_id=correlation_id,
                force=force,
            )
        except ResearchRunBudgetExceeded as exc:
            errors.append({"stage": "budget", "error": type(exc).__name__})
            break
        except Exception as exc:
            errors.append(
                {
                    "stage": "case_pipeline",
                    "blocking_key": group.blocking_key,
                    "error": type(exc).__name__,
                }
            )
            logger.warning(
                "research_case_pipeline_failed",
                blocking_key=group.blocking_key,
                error_type=type(exc).__name__,
            )
            continue
        outcomes.append(outcome)
        if outcome.get("case_id"):
            existing.append(
                {
                    "id": outcome["case_id"],
                    "title": outcome.get("title", ""),
                    "aliases": [],
                    "semantic_fingerprint": outcome.get("semantic_fingerprint"),
                    "blocking_key": group.blocking_key,
                    "pipeline_input_fingerprint": _pipeline_fingerprint(group, settings),
                    "pipeline_complete": "true",
                }
            )
    return {
        "status": "completed" if not errors else "completed_with_errors",
        "evidence_count": len(evidence),
        "candidate_count": len(groups),
        "cases": outcomes,
        "adapter_failures": dict(collection.failures),
        "errors": errors,
        "lifecycle_transitions": lifecycle_transitions,
        "model_cost_usd": runner.cost_usd,
    }


def _stored_evidence(detail: Mapping[str, Any]) -> list[NormalizedEvidence]:
    # Case-level entity links are not evidence-level attribution. They are added
    # to the candidate seed separately, never copied onto each historical source.
    items: list[NormalizedEvidence] = []
    for row in detail.get("evidence", [])[:200]:
        if not isinstance(row, Mapping):
            continue
        try:
            items.append(
                NormalizedEvidence.create(
                    evidence_type=row.get("evidence_type"),
                    evidence_id=row.get("evidence_id"),
                    source_name=row.get("source_name"),
                    source_timestamp=row.get("source_timestamp"),
                    acquired_at=row.get("created_at"),
                    title=row.get("title"),
                    bounded_excerpt=row.get("excerpt"),
                    source_reference=row.get("source_reference"),
                    entities=(),
                    structured_fields={},
                    provenance={"adapter": "research_case_evidence"},
                    freshness="historical",
                    content_fingerprint=row.get("evidence_fingerprint"),
                )
            )
        except (TypeError, ValueError):
            continue
    return items


def run_case_update(
    session: Any,
    config: dict[str, Any],
    case_id: str,
    *,
    correlation_id: str | None = None,
    budget_context: BudgetContext | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Update one case with linked history and newly related bounded evidence."""
    settings = ResearchSettings.from_config(config)
    detail = get_case(session, case_id, detail_limit=200)
    if detail is None:
        raise ValueError("research case not found")
    collection = EvidenceRegistry().collect(
        session,
        rolling_window_days=settings.rolling_window_days,
        limit=settings.maximum_candidate_evidence,
        now=now,
    )
    case = detail["case"]
    entity_keys = {
        (row.get("entity_type"), row.get("normalized_key"))
        for row in detail.get("entities", [])
        if isinstance(row, Mapping)
    }
    related = []
    for item in collection.items:
        item_keys = {(entity.entity_type, entity.normalized_key) for entity in item.entities}
        if entity_keys & item_keys or token_similarity(case.get("title"), item.title) >= 0.2:
            related.append(item)
    merged: dict[str, NormalizedEvidence] = {
        item.ref: item for item in _stored_evidence(detail)
    }
    for item in related:
        merged[item.ref] = item
    evidence = tuple(
        sorted(
            merged.values(),
            key=lambda item: (item.source_timestamp, item.ref),
            reverse=True,
        )[: settings.evidence_per_candidate]
    )
    if not evidence:
        return {"status": "no_evidence", "case_id": case_id}
    entities: list[NormalizedEntity] = [
        NormalizedEntity.create(
            row.get("entity_type"),
            row.get("normalized_key"),
            row.get("display_name"),
        )
        for row in detail.get("entities", [])[:100]
        if isinstance(row, Mapping)
    ]
    for item in evidence:
        for entity in item.entities:
            if entity not in entities:
                entities.append(entity)
    industries = tuple(
        entity.display_name for entity in entities if entity.entity_type == "industry"
    )
    group = CandidateGroup(
        blocking_key=f"case:{case_id}",
        evidence=evidence,
        entities=tuple(entities[:50]),
        industries=industries[:20],
        source_names=tuple(sorted({item.source_name for item in evidence}))[:20],
        input_fingerprint=canonical_fingerprint(
            [item.content_fingerprint for item in evidence]
        ),
    )
    runner = ResearchModelRunner(
        config,
        correlation_id=correlation_id,
        session=session,
        budget_context=budget_context,
    )
    forced = {
        "id": case_id,
        "title": case.get("title"),
        "aliases": detail.get("aliases", []),
        "semantic_fingerprint": case.get("semantic_fingerprint"),
        "lifecycle_state": case.get("lifecycle_state"),
    }
    return _process_group(
        session,
        runner,
        group,
        [forced],
        correlation_id=correlation_id,
        forced_match=forced,
        force=force,
    )


def run_macro_transmission(
    session: Any,
    config: dict[str, Any],
    *,
    correlation_id: str | None = None,
    budget_context: BudgetContext | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Synthesize configured major-market context without changing release semantics."""
    settings = ResearchSettings.from_config(config)
    if not settings.enabled or not settings.macro_drivers_enabled:
        return {"status": "disabled", "driver_count": 0}
    registry = EvidenceRegistry(
        (
            MacroObservationAdapter(),
            MacroReleaseAdapter(),
            MarketStateAdapter(),
            OfficialDocumentAdapter(),
            StoryClusterAdapter(),
            MarketConfirmationAdapter(),
        )
    )
    collection = registry.collect(
        session,
        rolling_window_days=settings.rolling_window_days,
        limit=min(
            settings.maximum_candidate_evidence, settings.maximum_macro_evidence
        ),
        now=now,
    )
    if not collection.items:
        return {
            "status": "no_evidence",
            "driver_count": 0,
            "adapter_failures": dict(collection.failures),
        }
    prior = current_market_drivers(session, limit=200)
    fingerprint = market_driver_input_fingerprint(
        collection.items, settings.hot_market_universe
    )
    if not force and prior and all(
        row.get("input_fingerprint") == fingerprint for row in prior
    ):
        return {"status": "unchanged", "driver_count": len(prior)}
    payload = {
        "market_universe": list(settings.hot_market_universe),
        "region_universe": list(settings.region_universe),
        "evidence": [item.to_dict() for item in collection.items],
        "prior_drivers": prior,
    }
    runner = ResearchModelRunner(
        config,
        correlation_id=correlation_id,
        session=session,
        budget_context=budget_context,
    )
    result = run_model_stage(
        session,
        runner,
        "macro_transmission",
        payload,
        lambda output: validate_factor_market_output(
            output,
            collection.items,
            settings.hot_market_universe,
            prior_drivers=prior,
            maximum_factors=settings.maximum_market_drivers,
            maximum_drivers=settings.maximum_market_drivers,
        ),
        fingerprint,
        force=force,
    )
    assessment = result.value or FactorMarketAssessment((), ())
    with _savepoint(session):
        factor_ids, factor_changes = persist_economic_factors(
            session, assessment.factors, collection.items, result.provenance
        )
        changed = persist_market_drivers(
            session,
            assessment.drivers,
            collection.items,
            result.provenance,
            factor_ids=factor_ids,
        )
    return {
        "status": "updated" if changed or factor_changes else "unchanged",
        "factor_count": len(assessment.factors),
        "factor_changed_count": factor_changes,
        "driver_count": len(assessment.drivers),
        "changed_count": changed,
        "adapter_failures": dict(collection.failures),
        "model_cost_usd": runner.cost_usd,
        "input_fingerprint": fingerprint,
    }


__all__ = [
    "run_case_update",
    "run_discovery",
    "run_macro_transmission",
    "run_model_stage",
]
