"""Point-in-time research execution with immutable replay outputs."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

from budgets import BudgetContext
from contracts.db_results import result_first, result_rows
from contracts.runtime_config import DEFAULT_STAGE_NAMES, AppConfig
from research_intelligence.adversarial import validate_adversarial_output
from research_intelligence.benchmarks import BenchmarkEpisode
from research_intelligence.claims import (
    CLAIM_ELIGIBLE_EVIDENCE_TYPES,
    claim_evidence,
    validate_claim_output,
)
from research_intelligence.config import ResearchSettings
from research_intelligence.context import ReplayLeakageError, ResearchContext
from research_intelligence.contracts import (
    CausalEdgeDraft,
    NormalizedEvidence,
    canonical_fingerprint,
    clean_payload,
)
from research_intelligence.deliverable import validate_deliverable_output
from research_intelligence.discovery import (
    PatternAssessment,
    build_candidate_groups,
    validate_pattern_output,
)
from research_intelligence.evidence import EvidenceRegistry
from research_intelligence.graph import validate_causal_output
from research_intelligence.lifecycle import CaseStats, next_lifecycle_state
from research_intelligence.models import ModelStageResult, ResearchModelRunner
from research_intelligence.relationships import causal_edge_fingerprint
from research_intelligence.service import run_model_stage
from research_intelligence.value_capture import validate_value_capture_output

StageExecutor = Callable[[str, Any, Callable[[Any], Any], str], ModelStageResult]

_REPLAY_STAGES = (
    "claim_extraction",
    "pattern_discovery",
    "causal_chain",
    "value_capture",
    "adversarial",
    "deliverable",
)


def _edge_fingerprints(edges: Sequence[CausalEdgeDraft]) -> tuple[str, ...]:
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


def _hypothesis_requests(edges: Sequence[CausalEdgeDraft]) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for edge, fingerprint in zip(edges, _edge_fingerprints(edges), strict=True):
        if edge.epistemic_state != "hypothesis":
            continue
        missing = list(edge.missing_evidence) or [
            f"Independent evidence directly testing whether {edge.from_name} "
            f"{edge.relationship.replace('_', ' ')} {edge.to_name}."
        ]
        weakening = list(edge.break_conditions) or [
            f"Evidence that {edge.to_name} changes independently of {edge.from_name}."
        ]
        requests.append(
            {
                "request_fingerprint": canonical_fingerprint(
                    {"edge": fingerprint, "missing": missing}
                ),
                "causal_edge_fingerprint": fingerprint,
                "subject": f"{edge.from_name} -> {edge.to_name}",
                "requested_evidence_type": "industry_capacity"
                if edge.relationship
                in {"constrains", "raises_supply_of", "reduces_supply_of"}
                else "supply_chain",
                "reason": f"Resolve the hypothesis edge: {edge.mechanism}",
                "desired_frequency": "monthly",
                "priority": "high" if edge.depth <= 2 else "moderate",
                "candidate_source_class": "industry",
                "status": "unresolved",
                "support_criteria": missing,
                "weakening_criteria": weakening,
                "minimum_independent_sources": 2,
            }
        )
    return requests


def _deterministic_settings(settings: ResearchSettings) -> dict[str, Any]:
    return {
        "rolling_window_days": settings.rolling_window_days,
        "maximum_candidate_evidence": settings.maximum_candidate_evidence,
        "maximum_cases_per_run": settings.maximum_cases_per_run,
        "maximum_claim_documents_per_run": (settings.maximum_claim_documents_per_run),
        "evidence_per_candidate": settings.evidence_per_candidate,
        "minimum_evidence_count": settings.minimum_evidence_count,
        "minimum_source_diversity": settings.minimum_source_diversity,
        "graph_depth": settings.graph_depth,
        "hard_graph_depth": settings.hard_graph_depth,
        "maximum_graph_nodes": settings.maximum_graph_nodes,
        "maximum_graph_edges": settings.maximum_graph_edges,
        "lifecycle_thresholds": dict(settings.lifecycle_thresholds),
        "claim_extraction_enabled": settings.claim_extraction_enabled,
        "stage_enabled": dict(settings.stage_enabled),
    }


def config_with_replay_overrides(
    config: AppConfig | Mapping[str, Any],
    *,
    model_overrides: Mapping[str, str] | None = None,
    prompt_overrides: Mapping[str, str] | None = None,
) -> AppConfig:
    """Apply bounded experiment overrides without mutating operator configuration."""
    raw = (
        config.model_dump(mode="json")
        if isinstance(config, AppConfig)
        else deepcopy(dict(config))
    )
    root = raw.setdefault("research_intelligence", {})
    if not isinstance(root, dict):
        raise ValueError("research_intelligence configuration must be an object")
    known = set(DEFAULT_STAGE_NAMES)
    models = root.setdefault("model_overrides", {})
    stages = root.setdefault("stages", {})
    if not isinstance(models, dict) or not isinstance(stages, dict):
        raise ValueError("research model and stage configuration must be objects")
    for stage, model in (model_overrides or {}).items():
        if stage not in known or not str(model).strip():
            raise ValueError("invalid research model override")
        models[stage] = str(model).strip()
    for stage, prompt in (prompt_overrides or {}).items():
        if stage not in known or not str(prompt).strip():
            raise ValueError("invalid research prompt override")
        stage_config = stages.setdefault(stage, {})
        if not isinstance(stage_config, dict):
            raise ValueError("research stage configuration must be an object")
        stage_config["prompt_template"] = str(prompt).strip()
    return AppConfig.model_validate(raw)


@dataclass(frozen=True, slots=True)
class ReplayCaseResult:
    semantic_fingerprint: str
    title: str
    definition: str
    case_is_economic_proposition: bool
    proposition_rationale: str
    lifecycle_state: str
    first_qualifying_evidence_at: datetime | None
    first_detection_at: datetime
    evidence_count: int
    source_diversity: int
    maximum_graph_depth: int
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ReplayExecution:
    replay_as_of: datetime
    evidence_count: int
    candidate_count: int
    cases: tuple[ReplayCaseResult, ...]
    abstention_count: int
    errors: tuple[Mapping[str, str], ...]
    stage_metrics: tuple[Mapping[str, Any], ...]
    audit: Mapping[str, Any]
    deterministic_metrics: Mapping[str, Any]
    cost_usd: float


def execute_replay_research(
    evidence: Sequence[NormalizedEvidence],
    context: ResearchContext,
    settings: ResearchSettings,
    *,
    runner: ResearchModelRunner | None = None,
    stage_executor: StageExecutor | None = None,
    force: bool = False,
    prior_cases: Mapping[str, Mapping[str, Any]] | None = None,
) -> ReplayExecution:
    """Execute the normal analytical stages without mutating live research cases."""
    if not context.is_replay:
        raise ValueError("replay execution requires replay context")
    visible = context.filter_evidence(evidence)
    context.assert_no_leakage()
    if stage_executor is None:
        if runner is None:
            raise ValueError("replay execution requires a model runner")

        def stage_executor(
            stage: str,
            payload: Any,
            validator: Callable[[Any], Any],
            fingerprint: str,
        ) -> ModelStageResult:
            return run_model_stage(
                runner.session,
                runner,
                stage,
                payload,
                validator,
                fingerprint,
                force=force,
            )

    stage_metrics: list[Mapping[str, Any]] = []
    cases: dict[str, ReplayCaseResult] = {}
    errors: list[Mapping[str, str]] = []
    abstentions = 0
    prior_cases = prior_cases or {}

    def run_stage(
        stage: str,
        payload: Any,
        validator: Callable[[Any], Any],
    ) -> ModelStageResult:
        fingerprint = canonical_fingerprint(
            {
                "stage": stage,
                "replay_as_of": context.effective_time.isoformat(),
                "input": payload,
            }
        )
        context.guard_model_input(payload, stage=stage)
        result = stage_executor(stage, payload, validator, fingerprint)
        context.guard_model_output(result.value, stage=stage)
        context.record_stage(stage, result)
        stage_metrics.append(
            {
                "stage": stage,
                "prompt_version": result.provenance.prompt_version,
                "model_slug": result.provenance.model_slug,
                "generation_attempt_id": result.provenance.generation_attempt_id,
                "input_fingerprint": fingerprint,
                "tokens_input": result.tokens_input,
                "tokens_output": result.tokens_output,
                "cost_usd": result.cost_usd,
                "duration_ms": result.duration_ms,
                "reused": bool(result.provenance.metadata.get("reused")),
                "attempt_count": int(
                    result.provenance.metadata.get("attempt_count") or 0
                ),
                "repair_required": bool(
                    result.provenance.metadata.get("repair_required")
                ),
            }
        )
        return result

    eligible_claim_sources = tuple(
        item
        for item in visible
        if item.evidence_type in CLAIM_ELIGIBLE_EVIDENCE_TYPES and item.bounded_excerpt
    )[: settings.maximum_claim_documents_per_run]
    if settings.claim_extraction_enabled and eligible_claim_sources:
        try:
            claim_result = run_stage(
                "claim_extraction",
                {"evidence": [item.to_dict() for item in eligible_claim_sources]},
                lambda output: validate_claim_output(output, eligible_claim_sources),
            )
            derived_claims = claim_evidence(
                claim_result.value or (), claim_result.provenance
            )
            visible = context.filter_evidence((*visible, *derived_claims))
        except Exception as exc:
            errors.append(
                {
                    "stage": "claim_extraction",
                    "blocking_key": "__evidence__",
                    "error": type(exc).__name__,
                    "detail": str(exc)[:240],
                }
            )
    groups = build_candidate_groups(
        visible,
        settings,
        maximum_groups=settings.maximum_cases_per_run,
    )

    for group in groups[: settings.maximum_cases_per_run]:
        pattern_payload = {
            "replay": context.to_prompt_metadata(),
            "blocking_key": group.blocking_key,
            "evidence": [item.to_dict() for item in group.evidence],
            "source_names": list(group.source_names),
            "industries": list(group.industries),
        }
        try:
            pattern_result = run_stage(
                "pattern_discovery",
                pattern_payload,
                lambda output, selected=group: validate_pattern_output(
                    output, selected
                ),
            )
        except Exception as exc:
            errors.append(
                {
                    "stage": "pattern_discovery",
                    "blocking_key": group.blocking_key,
                    "error": type(exc).__name__,
                    "detail": str(exc)[:240],
                }
            )
            continue
        assessment: PatternAssessment | None = pattern_result.value
        if assessment is None:
            abstentions += 1
            continue
        if assessment.semantic_fingerprint in cases:
            continue
        case_context = {
            "case_id": f"replay:{assessment.semantic_fingerprint}",
            "case": clean_payload(assessment),
            "replay": context.to_prompt_metadata(),
            "evidence": [item.to_dict() for item in group.evidence],
        }
        edges: tuple[CausalEdgeDraft, ...] = ()
        causal_result: ModelStageResult | None = None
        try:
            causal_payload = {
                **case_context,
                "bounds": {
                    "maximum_depth": settings.graph_depth,
                    "maximum_edges": settings.maximum_graph_edges,
                },
            }
            causal_result = run_stage(
                "causal_chain",
                causal_payload,
                lambda output,
                selected=group,
                seed=assessment.entities: validate_causal_output(
                    output, selected.evidence, settings, seed_entities=seed
                ),
            )
            edges = causal_result.value or ()
        except Exception as exc:
            errors.append(
                {
                    "stage": "causal_chain",
                    "blocking_key": group.blocking_key,
                    "error": type(exc).__name__,
                    "detail": str(exc)[:240],
                }
            )
        edge_fingerprints = _edge_fingerprints(edges)
        allowed_nodes = {(edge.from_type, edge.from_key) for edge in edges} | {
            (edge.to_type, edge.to_key) for edge in edges
        }
        value_assessments: tuple[Any, ...] = ()
        value_result: ModelStageResult | None = None
        if edges:
            try:
                value_payload = {
                    **case_context,
                    "causal_edges": clean_payload(edges),
                }

                def validate_values(
                    output: Any,
                    *,
                    evidence: Sequence[NormalizedEvidence] = group.evidence,
                    graph_nodes: set[tuple[str, str]] = allowed_nodes,
                ) -> tuple[Any, ...]:
                    values = validate_value_capture_output(output, evidence)
                    if any(
                        graph_nodes
                        and (item.node_type, item.node_key) not in graph_nodes
                        for item in values
                    ):
                        raise ValueError(
                            "value capture references a node outside the graph"
                        )
                    return values

                value_result = run_stage(
                    "value_capture", value_payload, validate_values
                )
                value_assessments = value_result.value or ()
            except Exception as exc:
                errors.append(
                    {
                        "stage": "value_capture",
                        "blocking_key": group.blocking_key,
                        "error": type(exc).__name__,
                        "detail": str(exc)[:240],
                    }
                )
        adversarial = None
        adversarial_result: ModelStageResult | None = None
        try:
            adversarial_payload = {
                **case_context,
                "causal_edges": clean_payload(edges),
                "edge_fingerprints": edge_fingerprints,
                "value_capture": clean_payload(value_assessments),
                "existing_data_requests": [],
            }
            adversarial_result = run_stage(
                "adversarial",
                adversarial_payload,
                lambda output,
                evidence=group.evidence,
                fingerprints=edge_fingerprints: validate_adversarial_output(
                    output,
                    evidence,
                    edge_fingerprints=fingerprints,
                    maximum_counterevidence=30,
                    maximum_requests=20,
                ),
            )
            adversarial = adversarial_result.value
        except Exception as exc:
            errors.append(
                {
                    "stage": "adversarial",
                    "blocking_key": group.blocking_key,
                    "error": type(exc).__name__,
                    "detail": str(exc)[:240],
                }
            )
        deliverable = None
        deliverable_result: ModelStageResult | None = None
        try:
            deliverable_payload = {
                **case_context,
                "causal_edges": clean_payload(edges),
                "edge_fingerprints": edge_fingerprints,
                "value_capture": clean_payload(value_assessments),
                "counterevidence": clean_payload(adversarial),
            }
            assessment_nodes = tuple(
                (item.node_type, item.node_key) for item in value_assessments
            )
            deliverable_result = run_stage(
                "deliverable",
                deliverable_payload,
                lambda output,
                evidence=group.evidence,
                fingerprints=edge_fingerprints,
                nodes=assessment_nodes: validate_deliverable_output(
                    output,
                    evidence,
                    edge_fingerprints=fingerprints,
                    assessment_nodes=nodes,
                ),
            )
            deliverable = deliverable_result.value
        except Exception as exc:
            errors.append(
                {
                    "stage": "deliverable",
                    "blocking_key": group.blocking_key,
                    "error": type(exc).__name__,
                    "detail": str(exc)[:240],
                }
            )
        current_first_evidence = min(
            (item.available_at for item in group.evidence),
            default=context.effective_time,
        )
        prior_case = prior_cases.get(assessment.semantic_fingerprint, {})
        prior_first_evidence = prior_case.get("first_qualifying_evidence_at")
        first_evidence = min(
            value
            for value in (current_first_evidence, prior_first_evidence)
            if isinstance(value, datetime)
        )
        persistence_days = max(0, (context.effective_time - first_evidence).days)
        stats = CaseStats(
            evidence_count=len(group.evidence),
            source_diversity=len(group.source_names),
            persistence_days=persistence_days,
            snapshot_count=int(prior_case.get("snapshot_count") or 0) + 1,
            has_causal_chain=bool(edges),
            has_value_capture=bool(value_assessments),
            has_adversarial_review=adversarial is not None,
            has_deliverable=deliverable is not None,
            last_evidence_at=max(item.available_at for item in group.evidence),
        )
        lifecycle = next_lifecycle_state(
            str(prior_case.get("lifecycle_state") or "candidate"),
            stats,
            settings,
            now=context.effective_time,
        ).value
        hypotheses = [edge for edge in edges if edge.epistemic_state == "hypothesis"]
        if hypotheses and lifecycle in {"research_ready", "mature"}:
            lifecycle = "corroborated"
        requests = _hypothesis_requests(edges)
        if adversarial is not None:
            requests.extend(
                {
                    **clean_payload(item),
                    "status": "unresolved",
                    "causal_edge_fingerprint": adversarial.weakest_edge_fingerprint,
                    "support_criteria": [],
                    "weakening_criteria": list(adversarial.invalidation_conditions),
                    "minimum_independent_sources": 2,
                }
                for item in adversarial.data_requests
            )
        payload = {
            "replay": context.to_prompt_metadata(),
            "blocking_key": group.blocking_key,
            "case": clean_payload(assessment),
            "evidence": [item.to_dict() for item in group.evidence],
            "causal_edges": clean_payload(edges),
            "value_capture": clean_payload(value_assessments),
            "adversarial": clean_payload(adversarial),
            "deliverable": clean_payload(deliverable),
            "data_requests": requests,
            "model_provenance": {
                "pattern_discovery": clean_payload(pattern_result.provenance),
                "causal_chain": clean_payload(causal_result.provenance)
                if causal_result
                else None,
                "value_capture": clean_payload(value_result.provenance)
                if value_result
                else None,
                "adversarial": clean_payload(adversarial_result.provenance)
                if adversarial_result
                else None,
                "deliverable": clean_payload(deliverable_result.provenance)
                if deliverable_result
                else None,
            },
        }
        cases[assessment.semantic_fingerprint] = ReplayCaseResult(
            semantic_fingerprint=assessment.semantic_fingerprint,
            title=assessment.label,
            definition=assessment.definition,
            case_is_economic_proposition=assessment.case_is_economic_proposition,
            proposition_rationale=assessment.proposition_rationale,
            lifecycle_state=lifecycle,
            first_qualifying_evidence_at=first_evidence,
            first_detection_at=(
                prior_case.get("first_detection_at")
                if isinstance(prior_case.get("first_detection_at"), datetime)
                else context.effective_time
            ),
            evidence_count=len(group.evidence),
            source_diversity=len(group.source_names),
            maximum_graph_depth=max((edge.depth for edge in edges), default=0),
            payload=payload,
        )
        context.record_case(assessment.semantic_fingerprint)
    first_visible_evidence = min(
        (item.available_at for item in visible),
        default=None,
    )
    deterministic_metrics = {
        "visible_evidence_count": len(visible),
        "first_visible_evidence_at": (
            first_visible_evidence.isoformat() if first_visible_evidence else None
        ),
        "visible_source_count": len({item.source_name for item in visible}),
        "visible_evidence_type_count": len({item.evidence_type for item in visible}),
        "future_evidence_excluded": context.audit.future_evidence_excluded,
        "future_revisions_excluded": context.audit.future_revisions_excluded,
        "future_reaction_windows_excluded": context.audit.future_reaction_windows_excluded,
        "future_model_outputs_excluded": context.audit.future_model_outputs_excluded,
        "candidate_count": len(groups),
        "case_count": len(cases),
        "abstention_count": abstentions,
        "economic_proposition_count": sum(
            1 for item in cases.values() if item.case_is_economic_proposition
        ),
        "hypothesis_edge_count": sum(
            1
            for item in cases.values()
            for edge in item.payload.get("causal_edges", [])
            if edge.get("epistemic_state") == "hypothesis"
        ),
        "unresolved_data_request_count": sum(
            len(item.payload.get("data_requests", [])) for item in cases.values()
        ),
    }
    return ReplayExecution(
        replay_as_of=context.effective_time,
        evidence_count=len(visible),
        candidate_count=len(groups),
        cases=tuple(cases.values()),
        abstention_count=abstentions,
        errors=tuple(errors),
        stage_metrics=tuple(stage_metrics),
        audit=context.audit.to_dict(),
        deterministic_metrics=deterministic_metrics,
        cost_usd=sum(float(item.get("cost_usd") or 0) for item in stage_metrics),
    )



def _load_prior_benchmark_cases(
    session: Any,
    *,
    benchmark_id: str,
    replay_as_of: datetime,
    variant_fingerprint: str,
    comparison_group: str | None,
) -> dict[str, dict[str, Any]]:
    result = session.execute(
        text(
            """
            WITH dated AS (
                SELECT DISTINCT ON (
                    c.semantic_fingerprint, r.replay_as_of
                )
                    c.semantic_fingerprint,
                    c.lifecycle_state,
                    c.first_qualifying_evidence_at,
                    c.first_detection_at,
                    r.replay_as_of,
                    c.created_at
                FROM research_replay_cases c
                JOIN research_replay_runs r ON r.id = c.replay_run_id
                WHERE r.benchmark_id = :benchmark_id
                  AND r.replay_as_of < :replay_as_of
                  AND r.status IN ('completed', 'completed_with_errors')
                  AND r.variant_fingerprint = :variant_fingerprint
                  AND r.comparison_group IS NOT DISTINCT FROM :comparison_group
                ORDER BY c.semantic_fingerprint, r.replay_as_of, c.created_at DESC
            )
            SELECT semantic_fingerprint,
                   MIN(first_qualifying_evidence_at) AS first_qualifying_evidence_at,
                   MIN(first_detection_at) AS first_detection_at,
                   COUNT(*) AS snapshot_count,
                   (ARRAY_AGG(
                       lifecycle_state ORDER BY replay_as_of DESC
                   ))[1] AS lifecycle_state
            FROM dated
            GROUP BY semantic_fingerprint
            LIMIT 500
            """
        ),
        {
            "benchmark_id": benchmark_id,
            "replay_as_of": replay_as_of,
            "variant_fingerprint": variant_fingerprint,
            "comparison_group": comparison_group,
        },
    )
    return {
        str(row["semantic_fingerprint"]): row
        for row in result_rows(result)
        if row.get("semantic_fingerprint")
    }


def begin_replay_run(
    session: Any,
    context: ResearchContext,
    *,
    evidence_source: str,
    benchmark_id: str | None,
    deterministic_input_fingerprint: str,
    execution_fingerprint: str,
    variant_fingerprint: str,
    variant_identity: Mapping[str, Any],
    model_overrides: Mapping[str, Any],
    prompt_overrides: Mapping[str, Any],
    comparison_group: str | None = None,
    correlation_id: str | None = None,
) -> str:
    row = result_first(
        session.execute(
            text(
                """
                INSERT INTO research_replay_runs (
                    benchmark_id, replay_as_of, evidence_source, status,
                    evidence_fingerprint, deterministic_input_fingerprint,
                    variant_fingerprint, variant_identity, execution_fingerprint,
                    comparison_group, model_overrides, prompt_overrides, audit,
                    correlation_id
                ) VALUES (
                    :benchmark_id, :replay_as_of, :evidence_source, 'running',
                    :evidence_fingerprint, :deterministic_input_fingerprint,
                    :variant_fingerprint, CAST(:variant_identity AS JSONB),
                    :execution_fingerprint, :comparison_group,
                    CAST(:model_overrides AS JSONB), CAST(:prompt_overrides AS JSONB),
                    CAST(:audit AS JSONB), :correlation_id
                ) RETURNING id
                """
            ),
            {
                "benchmark_id": benchmark_id,
                "replay_as_of": context.effective_time,
                "evidence_source": evidence_source,
                "evidence_fingerprint": context.audit.evidence_fingerprint,
                "deterministic_input_fingerprint": deterministic_input_fingerprint,
                "variant_fingerprint": variant_fingerprint,
                "execution_fingerprint": execution_fingerprint,
                "variant_identity": json.dumps(dict(variant_identity), sort_keys=True),
                "comparison_group": comparison_group,
                "model_overrides": json.dumps(dict(model_overrides), sort_keys=True),
                "prompt_overrides": json.dumps(dict(prompt_overrides), sort_keys=True),
                "audit": json.dumps(context.audit.to_dict(), sort_keys=True),
                "correlation_id": correlation_id,
            },
        )
    )
    if row is None:
        raise RuntimeError("replay run insert did not return an identity")
    return str(row["id"])


def persist_replay_execution(
    session: Any,
    replay_run_id: str,
    execution: ReplayExecution,
    *,
    benchmark_id: str | None,
) -> None:
    first_visible = execution.deterministic_metrics.get("first_visible_evidence_at")
    if first_visible:
        session.execute(
            text(
                """
                INSERT INTO research_replay_timeline_events (
                    replay_run_id, benchmark_id, semantic_fingerprint,
                    event_type, occurred_at, detail
                ) VALUES (
                    :replay_run_id, :benchmark_id, '__episode__',
                    'evidence_started', :occurred_at, '{}'::JSONB
                ) ON CONFLICT DO NOTHING
                """
            ),
            {
                "replay_run_id": replay_run_id,
                "benchmark_id": benchmark_id,
                "occurred_at": datetime.fromisoformat(str(first_visible)),
            },
        )
    for case in execution.cases:
        session.execute(
            text(
                """
                INSERT INTO research_replay_cases (
                    replay_run_id, semantic_fingerprint, title, definition,
                    case_is_economic_proposition, proposition_rationale,
                    lifecycle_state, first_qualifying_evidence_at,
                    first_detection_at, evidence_count, source_diversity,
                    maximum_graph_depth, payload
                ) VALUES (
                    :replay_run_id, :semantic_fingerprint, :title, :definition,
                    :case_is_economic_proposition, :proposition_rationale,
                    :lifecycle_state, :first_qualifying_evidence_at,
                    :first_detection_at, :evidence_count, :source_diversity,
                    :maximum_graph_depth, CAST(:payload AS JSONB)
                ) ON CONFLICT (replay_run_id, semantic_fingerprint) DO NOTHING
                """
            ),
            {
                **asdict(case),
                "replay_run_id": replay_run_id,
                "payload": json.dumps(clean_payload(case.payload), sort_keys=True),
            },
        )
        event_types = ["candidate_generated"]
        if case.lifecycle_state != "candidate":
            event_types.append("case_formed")
        if any(
            edge.get("epistemic_state") == "hypothesis"
            for edge in case.payload.get("causal_edges", [])
        ):
            event_types.append("hypothesis_generated")
        if case.lifecycle_state in {"corroborated", "research_ready", "mature"}:
            event_types.append("case_corroborated")
        if case.lifecycle_state in {"research_ready", "mature"}:
            event_types.append("research_ready")
        if case.lifecycle_state == "mature":
            event_types.append("case_mature")
        for event_type in event_types:
            session.execute(
                text(
                    """
                    INSERT INTO research_replay_timeline_events (
                        replay_run_id, benchmark_id, semantic_fingerprint,
                        event_type, occurred_at, detail
                    ) VALUES (
                        :replay_run_id, :benchmark_id, :semantic_fingerprint,
                        :event_type, :occurred_at, CAST(:detail AS JSONB)
                    ) ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "replay_run_id": replay_run_id,
                    "benchmark_id": benchmark_id,
                    "semantic_fingerprint": case.semantic_fingerprint,
                    "event_type": event_type,
                    "occurred_at": execution.replay_as_of,
                    "detail": json.dumps(
                        {
                            "title": case.title,
                            "lifecycle_state": case.lifecycle_state,
                            "evidence_count": case.evidence_count,
                        },
                        sort_keys=True,
                    ),
                },
            )
    status = "completed" if not execution.errors else "completed_with_errors"
    session.execute(
        text(
            """
            UPDATE research_replay_runs SET
                status = :status,
                audit = CAST(:audit AS JSONB),
                deterministic_metrics = CAST(:deterministic_metrics AS JSONB),
                stage_metrics = CAST(:stage_metrics AS JSONB),
                result_summary = CAST(:result_summary AS JSONB),
                cost_usd = :cost_usd,
                completed_at = NOW()
            WHERE id = :replay_run_id AND status = 'running'
            """
        ),
        {
            "replay_run_id": replay_run_id,
            "status": status,
            "audit": json.dumps(dict(execution.audit), sort_keys=True),
            "deterministic_metrics": json.dumps(
                dict(execution.deterministic_metrics), sort_keys=True
            ),
            "stage_metrics": json.dumps(list(execution.stage_metrics), sort_keys=True),
            "result_summary": json.dumps(
                {
                    "case_count": len(execution.cases),
                    "cases": [
                        {
                            "semantic_fingerprint": case.semantic_fingerprint,
                            "title": case.title,
                            "lifecycle_state": case.lifecycle_state,
                        }
                        for case in execution.cases
                    ],
                    "errors": list(execution.errors),
                },
                sort_keys=True,
            ),
            "cost_usd": execution.cost_usd,
        },
    )


def run_benchmark_replay_date(
    session: Any,
    config: AppConfig | Mapping[str, Any],
    episode: BenchmarkEpisode,
    replay_as_of: datetime,
    *,
    model_overrides: Mapping[str, str] | None = None,
    prompt_overrides: Mapping[str, str] | None = None,
    comparison_group: str | None = None,
    correlation_id: str | None = None,
    budget_context: BudgetContext | None = None,
    force: bool = False,
) -> tuple[str, ReplayExecution]:
    """Run one benchmark date; benchmark answers load only after execution."""
    effective_config = config_with_replay_overrides(
        config,
        model_overrides=model_overrides,
        prompt_overrides=prompt_overrides,
    )
    context = ResearchContext.replay(
        replay_as_of,
        correlation_id=correlation_id,
        benchmark_id=episode.episode_id,
    )
    visible = episode.evidence_as_of(context)
    context.assert_no_leakage()
    settings = ResearchSettings.from_config(effective_config.research_intelligence)
    temporary_runner = ResearchModelRunner(
        effective_config,
        correlation_id=correlation_id,
        session=session,
        budget_context=budget_context,
    )
    stage_identities = {
        stage: temporary_runner.cache_identity(stage) for stage in _REPLAY_STAGES
    }
    variant_fingerprint = canonical_fingerprint(stage_identities)
    deterministic_input = context.deterministic_fingerprint(
        extra={
            "benchmark_id": episode.episode_id,
            "benchmark_version": episode.version,
            "settings": _deterministic_settings(settings),
        }
    )
    prior_cases = _load_prior_benchmark_cases(
        session,
        benchmark_id=episode.episode_id,
        replay_as_of=context.effective_time,
        variant_fingerprint=variant_fingerprint,
        comparison_group=comparison_group,
    )
    execution_fingerprint = canonical_fingerprint(
        {
            "deterministic_input_fingerprint": deterministic_input,
            "variant_fingerprint": variant_fingerprint,
        }
    )
    run_id = begin_replay_run(
        session,
        context,
        evidence_source="synthetic_benchmark",
        benchmark_id=episode.episode_id,
        deterministic_input_fingerprint=deterministic_input,
        execution_fingerprint=execution_fingerprint,
        variant_fingerprint=variant_fingerprint,
        model_overrides=model_overrides or {},
        prompt_overrides=prompt_overrides or {},
        variant_identity=stage_identities,
        comparison_group=comparison_group,
        correlation_id=correlation_id,
    )
    runner = ResearchModelRunner(
        effective_config,
        correlation_id=correlation_id,
        session=session,
        budget_context=budget_context,
        execution_metadata={
            "research_mode": "replay",
            "replay_run_id": run_id,
            "replay_as_of": context.effective_time.isoformat(),
            "benchmark_id": episode.episode_id,
            "deterministic_input_fingerprint": deterministic_input,
        },
    )
    try:
        execution = execute_replay_research(
            visible,
            context,
            runner.settings,
            runner=runner,
            force=force,
            prior_cases=prior_cases,
        )
        context.assert_no_leakage()
        persist_replay_execution(
            session, run_id, execution, benchmark_id=episode.episode_id
        )
        from research_intelligence.evaluation import (
            benchmark_lifecycle_timeline,
            build_benchmark_scorecard,
            persist_benchmark_scorecard,
        )

        timeline = benchmark_lifecycle_timeline(session, run_id)
        scorecard = build_benchmark_scorecard(execution, episode, timeline=timeline)
        persist_benchmark_scorecard(session, run_id, scorecard)
    except ReplayLeakageError:
        session.execute(
            text(
                """
                UPDATE research_replay_runs SET status = 'leakage_failed',
                    audit = CAST(:audit AS JSONB), completed_at = NOW()
                WHERE id = :run_id
                """
            ),
            {"run_id": run_id, "audit": json.dumps(context.audit.to_dict())},
        )
        raise
    except Exception:
        session.execute(
            text(
                """
                UPDATE research_replay_runs SET status = 'failed', completed_at = NOW()
                WHERE id = :run_id
                """
            ),
            {"run_id": run_id},
        )
        raise
    return run_id, execution


def run_database_replay(
    session: Any,
    config: AppConfig | Mapping[str, Any],
    replay_as_of: datetime,
    model_overrides: Mapping[str, str] | None = None,
    prompt_overrides: Mapping[str, str] | None = None,
    comparison_group: str | None = None,
    correlation_id: str | None = None,
    budget_context: BudgetContext | None = None,
    force: bool = False,
) -> tuple[str, ReplayExecution, Mapping[str, str]]:
    """Replay source-owned database evidence without reading future live cases."""
    effective_config = config_with_replay_overrides(
        config,
        model_overrides=model_overrides,
        prompt_overrides=prompt_overrides,
    )
    settings = ResearchSettings.from_config(effective_config.research_intelligence)
    context = ResearchContext.replay(
        replay_as_of,
        correlation_id=correlation_id,
    )
    collection = EvidenceRegistry().collect(
        session,
        rolling_window_days=settings.rolling_window_days,
        limit=settings.maximum_candidate_evidence,
        context=context,
    )
    context.assert_no_leakage()
    deterministic_input = context.deterministic_fingerprint(
        extra={
            "evidence_source": "database",
            "settings": _deterministic_settings(settings),
        }
    )
    temporary_runner = ResearchModelRunner(
        effective_config,
        correlation_id=correlation_id,
        session=session,
        budget_context=budget_context,
    )
    stage_identities = {
        stage: temporary_runner.cache_identity(stage) for stage in _REPLAY_STAGES
    }
    variant_fingerprint = canonical_fingerprint(stage_identities)
    execution_fingerprint = canonical_fingerprint(
        {
            "deterministic_input_fingerprint": deterministic_input,
            "variant_fingerprint": variant_fingerprint,
        }
    )
    run_id = begin_replay_run(
        session,
        context,
        evidence_source="database",
        benchmark_id=None,
        deterministic_input_fingerprint=deterministic_input,
        execution_fingerprint=execution_fingerprint,
        variant_fingerprint=variant_fingerprint,
        model_overrides=model_overrides or {},
        prompt_overrides=prompt_overrides or {},
        variant_identity=stage_identities,
        comparison_group=comparison_group,
        correlation_id=correlation_id,
    )
    runner = ResearchModelRunner(
        effective_config,
        correlation_id=correlation_id,
        session=session,
        budget_context=budget_context,
        execution_metadata={
            "research_mode": "replay",
            "replay_run_id": run_id,
            "replay_as_of": context.effective_time.isoformat(),
            "deterministic_input_fingerprint": deterministic_input,
        },
    )
    try:
        execution = execute_replay_research(
            collection.items,
            context,
            settings,
            runner=runner,
            force=force,
        )
        context.assert_no_leakage()
        persist_replay_execution(session, run_id, execution, benchmark_id=None)
        session.execute(
            text(
                """
                INSERT INTO research_quality_metrics (
                    replay_run_id, metric_scope, subject_id, metric_version, metrics
                ) VALUES (
                    :replay_run_id, 'replay', :subject_id,
                    'research_quality_metrics_v1', CAST(:metrics AS JSONB)
                ) ON CONFLICT (
                    replay_run_id, metric_scope, subject_id, metric_version
                ) DO NOTHING
                """
            ),
            {
                "replay_run_id": run_id,
                "subject_id": run_id,
                "metrics": json.dumps(
                    {
                        **dict(execution.deterministic_metrics),
                        "adapter_failures": dict(collection.failures),
                        "model_cost_usd": execution.cost_usd,
                    },
                    sort_keys=True,
                ),
            },
        )
    except ReplayLeakageError:
        session.execute(
            text(
                """
                UPDATE research_replay_runs SET status = 'leakage_failed',
                    audit = CAST(:audit AS JSONB), completed_at = NOW()
                WHERE id = :run_id
                """
            ),
            {"run_id": run_id, "audit": json.dumps(context.audit.to_dict())},
        )
        raise
    except Exception:
        session.execute(
            text(
                """
                UPDATE research_replay_runs SET status = 'failed', completed_at = NOW()
                WHERE id = :run_id
                """
            ),
            {"run_id": run_id},
        )
        raise
    return run_id, execution, collection.failures


__all__ = [
    "ReplayCaseResult",
    "ReplayExecution",
    "config_with_replay_overrides",
    "begin_replay_run",
    "execute_replay_research",
    "persist_replay_execution",
    "run_benchmark_replay_date",
    "run_database_replay",
]
