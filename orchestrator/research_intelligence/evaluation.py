"""Deterministic research-quality metrics, scorecards, timelines, and comparisons."""

from __future__ import annotations

import json
import re
import statistics
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from research_intelligence.benchmarks import BenchmarkEpisode
from research_intelligence.contracts import canonical_fingerprint
from research_intelligence.replay import ReplayCaseResult, ReplayExecution

SCORECARD_VERSION = "research_quality_scorecard_v3"
METRIC_VERSION = "research_quality_metrics_v3"
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "and",
        "are",
        "from",
        "into",
        "the",
        "through",
        "while",
        "with",
        "without",
    }
)


def _tokens(value: Any) -> set[str]:
    return {
        token
        for token in _TOKEN_RE.findall(str(value or "").casefold())
        if len(token) >= 4 and token not in _STOPWORDS
    }


def _coverage(expected: Sequence[str], corpus: str) -> tuple[int, list[str], list[str]]:
    corpus_tokens = _tokens(corpus)
    matched: list[str] = []
    missed: list[str] = []
    for item in expected:
        terms = _tokens(item)
        overlap = len(terms & corpus_tokens) / len(terms) if terms else 0.0
        (matched if overlap >= 0.4 else missed).append(item)
    return len(matched), matched, missed


def _dimension(status: str, rationale: str, **measures: Any) -> dict[str, Any]:
    return {"status": status, "rationale": rationale, "measures": measures}


def _case_corpus(cases: Sequence[ReplayCaseResult]) -> str:
    """Return analytical output text only; source packets cannot self-score."""
    analytical = []
    for case in cases:
        payload = case.payload
        analytical.append(
            {
                "title": case.title,
                "definition": case.definition,
                "case": payload.get("case"),
                "causal_edges": payload.get("causal_edges"),
                "value_capture": payload.get("value_capture"),
                "adversarial": payload.get("adversarial"),
                "deliverable": payload.get("deliverable"),
                "data_requests": payload.get("data_requests"),
            }
        )
    return " ".join(
        json.dumps(item, sort_keys=True, default=str) for item in analytical
    )


def _case_metrics(case: ReplayCaseResult) -> dict[str, Any]:
    edges = list(case.payload.get("causal_edges", []))
    values = list(case.payload.get("value_capture", []))
    counters = (case.payload.get("adversarial") or {}).get("counterevidence", [])
    requests = list(case.payload.get("data_requests", []))
    supported_edges = [
        edge for edge in edges if edge.get("epistemic_state") in {"observed", "supported"}
    ]
    hypothesis_edges = [
        edge for edge in edges if edge.get("epistemic_state") == "hypothesis"
    ]
    counter_hypotheses = [
        item for item in counters if item.get("epistemic_state") == "hypothesis"
    ]
    dimensions_known = sum(
        1
        for value in values
        for dimension in (value.get("dimensions") or {}).values()
        if dimension is not None
    )
    dimensions_unknown = sum(
        1
        for value in values
        for dimension in (value.get("dimensions") or {}).values()
        if dimension is None
    )
    edge_sources: set[str] = set()
    evidence_by_ref = {
        f"{item.get('evidence_type')}:{item.get('evidence_id')}": item
        for item in case.payload.get("evidence", [])
    }
    for edge in supported_edges:
        for reference in edge.get("evidence_ids", []):
            item = evidence_by_ref.get(reference)
            if item:
                edge_sources.add(str(item.get("source_name") or ""))
    return {
        "case_is_economic_proposition": case.case_is_economic_proposition,
        "lifecycle_state": case.lifecycle_state,
        "evidence_count": case.evidence_count,
        "source_diversity": case.source_diversity,
        "edge_count": len(edges),
        "supported_edge_count": len(supported_edges),
        "hypothesis_edge_count": len(hypothesis_edges),
        "rejected_edge_count": sum(
            1 for edge in edges if edge.get("epistemic_state") == "rejected"
        ),
        "maximum_graph_depth": case.maximum_graph_depth,
        "cross_source_edge_evidence_count": len(edge_sources),
        "value_capture_node_count": len(values),
        "value_capture_dimensions_known": dimensions_known,
        "value_capture_dimensions_unknown": dimensions_unknown,
        "counterevidence_count": len(counters),
        "counter_hypothesis_count": len(counter_hypotheses),
        "data_request_count": len(requests),
        "unresolved_hypothesis_requests": sum(
            1
            for request in requests
            if request.get("causal_edge_fingerprint")
            and request.get("status")
            in {"unresolved", "in_progress", "partially_satisfied"}
        ),
    }


def benchmark_lifecycle_timeline(
    session: Any,
    replay_run_id: str,
) -> dict[str, Any]:
    """Return milestones only from the anchor run's benchmark experiment variant."""
    result = session.execute(
        text(
            """
            WITH anchor AS (
                SELECT benchmark_id, replay_as_of, comparison_group,
                       variant_fingerprint
                FROM research_replay_runs
                WHERE id = :replay_run_id
            )
            SELECT e.semantic_fingerprint, e.event_type,
                   MIN(e.occurred_at) AS first_at,
                   MAX(e.occurred_at) AS last_at,
                   COUNT(DISTINCT r.replay_as_of) AS observation_count,
                   MAX(a.replay_as_of) AS latest_replay_at
            FROM anchor a
            JOIN research_replay_runs r
              ON r.benchmark_id = a.benchmark_id
             AND r.replay_as_of <= a.replay_as_of
             AND r.status IN ('completed', 'completed_with_errors')
             AND r.variant_fingerprint = a.variant_fingerprint
             AND r.comparison_group IS NOT DISTINCT FROM a.comparison_group
            JOIN research_replay_timeline_events e ON e.replay_run_id = r.id
            GROUP BY e.semantic_fingerprint, e.event_type
            ORDER BY e.semantic_fingerprint, e.event_type
            LIMIT 5000
            """
        ),
        {"replay_run_id": replay_run_id},
    )
    try:
        rows = [dict(row) for row in result.mappings().all()]
    except (AttributeError, TypeError):
        rows = [dict(row._mapping) for row in result.all()]
    event_to_milestone = {
        "evidence_started": "first_qualifying_evidence",
        "candidate_generated": "first_candidate",
        "case_formed": "first_forming",
        "case_corroborated": "first_corroborated",
        "research_ready": "first_research_ready",
        "case_mature": "first_mature",
    }
    cases: dict[str, dict[str, Any]] = {}
    episode: dict[str, datetime] = {}
    latest_replay_at: datetime | None = None
    for row in rows:
        event_type = str(row.get("event_type") or "")
        milestone = event_to_milestone.get(event_type)
        first_at = row.get("first_at")
        latest = row.get("latest_replay_at")
        if isinstance(latest, datetime):
            latest_replay_at = max(latest_replay_at, latest) if latest_replay_at else latest
        if milestone is None or not isinstance(first_at, datetime):
            continue
        fingerprint = str(row.get("semantic_fingerprint") or "")
        if fingerprint != "__episode__":
            case = cases.setdefault(
                fingerprint,
                {
                    "semantic_fingerprint": fingerprint,
                    "milestones": {},
                    "replay_observations": 0,
                },
            )
            case["milestones"][milestone] = first_at.isoformat()
            if event_type == "candidate_generated":
                case["replay_observations"] = int(row.get("observation_count") or 0)
                last_at = row.get("last_at")
                case["last_seen_at"] = (
                    last_at.isoformat() if isinstance(last_at, datetime) else None
                )
        current = episode.get(milestone)
        if current is None or first_at < current:
            episode[milestone] = first_at
    for case in cases.values():
        last_seen = case.get("last_seen_at")
        case["survived_to_latest_replay"] = bool(
            latest_replay_at
            and last_seen
            and datetime.fromisoformat(last_seen) == latest_replay_at
        )
    return {
        "replay_run_id": replay_run_id,
        "latest_replay_at": (
            latest_replay_at.isoformat() if latest_replay_at else None
        ),
        "episode_milestones": {
            key: value.isoformat() for key, value in sorted(episode.items())
        },
        "cases": list(cases.values()),
    }


def _research_quality_metrics(
    execution: ReplayExecution,
    case_metrics: Sequence[Mapping[str, Any]],
    timeline: Mapping[str, Any],
) -> dict[str, Any]:
    case_count = len(execution.cases)
    candidate_count = execution.candidate_count
    ready_count = sum(
        1
        for case in execution.cases
        if case.lifecycle_state in {"research_ready", "mature"}
    )
    evidence_types = {
        str(item.get("evidence_type"))
        for case in execution.cases
        for item in case.payload.get("evidence", [])
        if item.get("evidence_type")
    }
    supported_edges = sum(
        int(item.get("supported_edge_count") or 0) for item in case_metrics
    )
    hypothesis_edges = sum(
        int(item.get("hypothesis_edge_count") or 0) for item in case_metrics
    )
    counter_hypotheses = sum(
        int(item.get("counter_hypothesis_count") or 0) for item in case_metrics
    )
    hypothesis_objects = hypothesis_edges + counter_hypotheses
    data_requests = sum(
        int(item.get("data_request_count") or 0) for item in case_metrics
    )
    rejected_edges = sum(
        int(item.get("rejected_edge_count") or 0) for item in case_metrics
    )
    all_edges = sum(int(item.get("edge_count") or 0) for item in case_metrics)
    known_dimensions = sum(
        int(item.get("value_capture_dimensions_known") or 0) for item in case_metrics
    )
    unknown_dimensions = sum(
        int(item.get("value_capture_dimensions_unknown") or 0) for item in case_metrics
    )
    dimension_total = known_dimensions + unknown_dimensions
    stage_successes = len(execution.stage_metrics)
    stage_failures = len(execution.errors)
    repair_count = sum(
        1 for item in execution.stage_metrics if item.get("repair_required")
    )
    attempt_count = sum(
        max(1, int(item.get("attempt_count") or 0))
        for item in execution.stage_metrics
    )
    error_details = " ".join(
        str(item.get("detail") or "") for item in execution.errors
    ).casefold()
    timeline_cases = list(timeline.get("cases") or [])
    return {
        "discovery": {
            "candidate_count": candidate_count,
            "case_count": case_count,
            "case_yield": case_count / candidate_count if candidate_count else 0.0,
            "abstention_count": execution.abstention_count,
            "economic_proposition_count": sum(
                1 for case in execution.cases if case.case_is_economic_proposition
            ),
        },
        "compression": {
            "input_evidence_count": execution.evidence_count,
            "candidate_count": candidate_count,
            "published_case_count": ready_count,
            "evidence_per_case": (
                execution.evidence_count / case_count if case_count else None
            ),
            "evidence_per_published_case": (
                execution.evidence_count / ready_count if ready_count else None
            ),
        },
        "persistence": {
            "tracked_case_count": len(timeline_cases),
            "repeat_case_count": sum(
                1
                for item in timeline_cases
                if int(item.get("replay_observations") or 0) > 1
            ),
            "survived_to_latest_count": sum(
                1 for item in timeline_cases if item.get("survived_to_latest_replay")
            ),
        },
        "evidence": {
            "independent_source_counts": [
                int(item.get("source_diversity") or 0) for item in case_metrics
            ],
            "evidence_type_count": len(evidence_types),
            "supporting_edge_count": supported_edges,
            "contradicting_evidence_count": sum(
                int(item.get("counterevidence_count") or 0)
                for item in case_metrics
            ),
            "unknown_dimension_rate": (
                unknown_dimensions / dimension_total if dimension_total else None
            ),
        },
        "graph": {
            "edge_count": all_edges,
            "supported_edge_count": supported_edges,
            "hypothesis_edge_count": hypothesis_edges,
            "counter_hypothesis_count": counter_hypotheses,
            "hypothesis_object_count": hypothesis_objects,
            "testable_hypothesis_rate": (
                min(1.0, data_requests / hypothesis_objects)
                if hypothesis_objects
                else None
            ),
            "rejected_edge_count": rejected_edges,
            "hypothesis_to_supported_ratio": (
                supported_edges / hypothesis_edges if hypothesis_edges else None
            ),
            "average_maximum_depth": (
                statistics.fmean(
                    int(item.get("maximum_graph_depth") or 0)
                    for item in case_metrics
                )
                if case_metrics
                else 0.0
            ),
            "unresolved_weak_links": sum(
                int(item.get("unresolved_hypothesis_requests") or 0)
                for item in case_metrics
            ),
        },
        "value_capture": {
            "node_count": sum(
                int(item.get("value_capture_node_count") or 0)
                for item in case_metrics
            ),
            "supported_dimension_count": known_dimensions,
            "unknown_dimension_count": unknown_dimensions,
        },
        "model_quality": {
            "stage_success_count": stage_successes,
            "stage_failure_count": stage_failures,
            "stage_success_rate": (
                stage_successes / (stage_successes + stage_failures)
                if stage_successes + stage_failures
                else None
            ),
            "attempt_count": attempt_count,
            "repair_count": repair_count,
            "hard_failure_count": stage_failures,
            "unknown_evidence_id_rejection_count": error_details.count(
                "unknown evidence"
            ),
            "unsupported_numeric_rejection_count": error_details.count(
                "unsupported numeric"
            ),
        },
        "operations": {
            "tokens_input": sum(
                int(item.get("tokens_input") or 0)
                for item in execution.stage_metrics
            ),
            "tokens_output": sum(
                int(item.get("tokens_output") or 0)
                for item in execution.stage_metrics
            ),
            "latency_ms": sum(
                int(item.get("duration_ms") or 0)
                for item in execution.stage_metrics
            ),
            "cost_usd": execution.cost_usd,
        },
    }


def build_benchmark_scorecard(
    execution: ReplayExecution,
    episode: BenchmarkEpisode,
    *,
    timeline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Score one completed run. Benchmark answers are consumed only here."""
    cases = execution.cases
    case_metrics = [_case_metrics(case) for case in cases]
    timeline = dict(timeline or {})
    timeline_milestones = timeline.get("episode_milestones") or {}

    def milestone_time(name: str) -> datetime | None:
        value = timeline_milestones.get(name)
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    corpus = _case_corpus(cases)
    expected_count, expected_matched, expected_missed = _coverage(
        episode.expected_developments, corpus
    )
    second_count, second_matched, second_missed = _coverage(
        episode.plausible_second_order_areas, corpus
    )
    unknown_count, unknown_matched, unknown_missed = _coverage(
        episode.expected_unknowns, corpus
    )
    expected_total = len(episode.expected_developments)
    second_total = len(episode.plausible_second_order_areas)
    unknown_total = len(episode.expected_unknowns)
    is_noise = episode.episode_kind == "noise"
    has_ready = any(
        case.lifecycle_state in {"research_ready", "mature"} for case in cases
    )
    proposition_count = sum(
        1 for case in cases if case.case_is_economic_proposition
    )
    edges = sum(item["edge_count"] for item in case_metrics)
    supported_edges = sum(item["supported_edge_count"] for item in case_metrics)
    hypothesis_edges = sum(item["hypothesis_edge_count"] for item in case_metrics)
    counter_hypotheses = sum(
        item["counter_hypothesis_count"] for item in case_metrics
    )
    value_nodes = sum(item["value_capture_node_count"] for item in case_metrics)
    known_dimensions = sum(
        item["value_capture_dimensions_known"] for item in case_metrics
    )
    unknown_dimensions = sum(
        item["value_capture_dimensions_unknown"] for item in case_metrics
    )
    counters = sum(item["counterevidence_count"] for item in case_metrics)
    requests = sum(item["data_request_count"] for item in case_metrics)
    maximum_depth = max(
        (item["maximum_graph_depth"] for item in case_metrics), default=0
    )
    first_detection = milestone_time("first_candidate") or min(
        (case.first_detection_at for case in cases), default=None
    )
    first_qualifying_evidence = milestone_time("first_qualifying_evidence")
    evidence_start = episode.manual_milestones.get("evidence_begins")
    analyst_detection = episode.manual_milestones.get("competent_analyst_detection")
    lead_days = (
        (analyst_detection - first_detection).total_seconds() / 86_400
        if first_detection is not None and analyst_detection is not None
        else None
    )
    lifecycle_milestones = {
        key: milestone_time(key)
        for key in (
            "first_candidate",
            "first_forming",
            "first_corroborated",
            "first_research_ready",
            "first_mature",
        )
    }
    days_from_evidence = {
        key: (
            (value - first_qualifying_evidence).total_seconds() / 86_400
            if value is not None and first_qualifying_evidence is not None
            else None
        )
        for key, value in lifecycle_milestones.items()
    }
    future_refs = {
        item.ref
        for item in episode.evidence
        if item.available_at > execution.replay_as_of
    }
    leaked_refs = sorted(
        reference for reference in future_refs if reference in corpus
    )
    if is_noise:
        discovery_status = "pass" if not cases else ("partial" if not has_ready else "fail")
        discovery_rationale = (
            "The repeated topical language did not become a research-ready case."
            if not has_ready
            else "A noise episode was promoted despite missing economic evidence."
        )
    else:
        ratio = expected_count / expected_total if expected_total else 1.0
        discovery_status = "pass" if cases and ratio >= 0.5 else ("partial" if cases else "fail")
        discovery_rationale = (
            "The run formed at least one economic case and covered the expected development."
            if discovery_status == "pass"
            else "The run did not yet cover enough of the expected economic development."
        )
    causal_ratio = supported_edges / edges if edges else 0.0
    causal_status = (
        "not_applicable"
        if not cases
        else "pass"
        if edges and causal_ratio >= 0.5
        else "partial"
        if edges
        else "fail"
    )
    value_status = (
        "not_applicable"
        if not cases
        else "pass"
        if value_nodes and known_dimensions
        else "partial"
        if value_nodes
        else "fail"
    )
    counter_status = (
        "not_applicable"
        if not cases
        else "pass"
        if counters
        else "partial"
    )
    unknown_status = (
        "not_applicable"
        if not cases
        else "pass"
        if (hypothesis_edges == 0 or requests >= hypothesis_edges)
        and (unknown_dimensions > 0 or requests > 0)
        else "partial"
    )
    hypothesis_objects = hypothesis_edges + counter_hypotheses
    hypothesis_status = (
        "pass"
        if is_noise and hypothesis_objects == 0 and requests == 0
        else "fail"
        if is_noise or not cases
        else "not_applicable"
        if not unknown_total
        else "pass"
        if unknown_count >= 1
        and hypothesis_objects >= 1
        and requests >= max(1, hypothesis_edges)
        else "partial"
        if unknown_count >= 1 or hypothesis_objects >= 1 or requests >= 1
        else "fail"
    )
    second_status = (
        "not_applicable"
        if not cases or not second_total
        else "pass"
        if second_count >= 1 and maximum_depth >= 2
        else "partial"
        if second_count >= 1 or maximum_depth >= 2
        else "fail"
    )
    specificity_status = (
        "pass"
        if (is_noise and not has_ready) or (not is_noise and proposition_count == len(cases))
        else "fail"
    )
    dimensions = {
        "discovery": _dimension(
            discovery_status,
            discovery_rationale,
            candidate_count=execution.candidate_count,
            case_count=len(cases),
            expected_developments_matched=expected_matched,
            expected_developments_missed=expected_missed,
        ),
        "lead_time": _dimension(
            "not_applicable"
            if analyst_detection is None
            else "fail"
            if first_detection is None
            else "pass"
            if lead_days is not None and lead_days >= 0
            else "late",
            "First lifecycle milestones are compared with evidence availability and the human-authored analyst date.",
            evidence_begins=evidence_start.isoformat() if evidence_start else None,
            first_qualifying_evidence=(
                first_qualifying_evidence.isoformat()
                if first_qualifying_evidence
                else None
            ),
            first_detection=first_detection.isoformat() if first_detection else None,
            first_forming=(
                lifecycle_milestones["first_forming"].isoformat()
                if lifecycle_milestones["first_forming"]
                else None
            ),
            first_corroborated=(
                lifecycle_milestones["first_corroborated"].isoformat()
                if lifecycle_milestones["first_corroborated"]
                else None
            ),
            first_research_ready=(
                lifecycle_milestones["first_research_ready"].isoformat()
                if lifecycle_milestones["first_research_ready"]
                else None
            ),
            competent_analyst_detection=(
                analyst_detection.isoformat() if analyst_detection else None
            ),
            lead_days=lead_days,
            days_from_first_qualifying_evidence=days_from_evidence,
        ),
        "specificity": _dimension(
            specificity_status,
            "Noise must not reach research-ready; non-noise cases must be economic propositions.",
            noise_episode=is_noise,
            proposition_count=proposition_count,
            research_ready_count=sum(
                1
                for case in cases
                if case.lifecycle_state in {"research_ready", "mature"}
            ),
        ),
        "causal_quality": _dimension(
            causal_status,
            "Observed/supported edges require supplied evidence; hypotheses remain separate.",
            edge_count=edges,
            supported_edge_count=supported_edges,
            hypothesis_edge_count=hypothesis_edges,
            supported_edge_ratio=causal_ratio,
            maximum_graph_depth=maximum_depth,
        ),
        "second_order_reasoning": _dimension(
            second_status,
            "Second-order coverage requires both relevant concepts and graph depth where applicable.",
            areas_matched=second_matched,
            areas_missed=second_missed,
            maximum_graph_depth=maximum_depth,
        ),
        "value_capture_reasoning": _dimension(
            value_status,
            "Value-capture dimensions remain separate; unknowns are not collapsed into a score.",
            node_count=value_nodes,
            supported_dimensions=known_dimensions,
            unknown_dimensions=unknown_dimensions,
        ),
        "evidence_quality": _dimension(
            "pass" if cases and all(case.source_diversity >= 2 for case in cases) else "partial",
            "Strict validators reject unknown citations; this dimension measures remaining breadth.",
            case_source_diversities=[case.source_diversity for case in cases],
            future_evidence_excluded=execution.audit.get("future_evidence_excluded", 0),
            leaked_future_evidence_ids=leaked_refs,
        ),
        "counter_thesis_quality": _dimension(
            counter_status,
            "Developed cases should retain counterevidence or explicit alternative explanations.",
            counterevidence_count=counters,
        ),
        "unknown_handling": _dimension(
            unknown_status,
            "Hypotheses require resolvable data requests and unsupported dimensions remain unknown.",
            hypothesis_edge_count=hypothesis_edges,
            data_request_count=requests,
            unknown_value_capture_dimensions=unknown_dimensions,
        ),
        "hypothesis_discovery": _dimension(
            hypothesis_status,
            "Useful hypotheses must be explicit, testable through a data request, and relevant to a benchmark unknown.",
            hypothesis_edge_count=hypothesis_edges,
            counter_hypothesis_count=counter_hypotheses,
            data_request_count=requests,
            expected_unknowns_matched=unknown_matched,
            expected_unknowns_missed=unknown_missed,
        ),
        "novelty": _dimension(
            "not_applicable"
            if is_noise or not second_total
            else "pass"
            if second_count >= 2
            else "partial"
            if second_count == 1
            else "fail",
            "Novelty is proxied by useful second-order areas found without treating case count as quality.",
            second_order_areas_matched=second_matched,
            second_order_areas_missed=second_missed,
        ),
        "point_in_time_integrity": _dimension(
            "pass"
            if not leaked_refs and not execution.audit.get("leakage_violations")
            else "fail",
            "No evidence or model output available after the replay cutoff may enter the result.",
            leaked_future_evidence_ids=leaked_refs,
            leakage_violations=execution.audit.get("leakage_violations", []),
        ),
    }
    return {
        "scorecard_version": SCORECARD_VERSION,
        "benchmark_id": episode.episode_id,
        "benchmark_version": episode.version,
        "episode_kind": episode.episode_kind,
        "replay_as_of": execution.replay_as_of.isoformat(),
        "timeline": timeline,
        "dimensions": dimensions,
        "case_metrics": {
            case.semantic_fingerprint: metrics
            for case, metrics in zip(cases, case_metrics, strict=True)
        },
        "run_metrics": {
            "quality": _research_quality_metrics(
                execution, case_metrics, timeline
            ),
            **dict(execution.deterministic_metrics),
            "model_cost_usd": execution.cost_usd,
            "tokens_input": sum(
                int(item.get("tokens_input") or 0) for item in execution.stage_metrics
            ),
            "tokens_output": sum(
                int(item.get("tokens_output") or 0) for item in execution.stage_metrics
            ),
            "latency_ms": sum(
                int(item.get("duration_ms") or 0) for item in execution.stage_metrics
            ),
            "stage_failure_count": len(execution.errors),
        },
    }


def persist_benchmark_scorecard(
    session: Any,
    replay_run_id: str,
    scorecard: Mapping[str, Any],
) -> None:
    benchmark_id = str(scorecard["benchmark_id"])
    dimensions = scorecard["dimensions"]
    session.execute(
        text(
            """
            INSERT INTO research_benchmark_scorecards (
                replay_run_id, benchmark_id, scorecard_version, dimensions
            ) VALUES (
                :replay_run_id, :benchmark_id, :scorecard_version,
                CAST(:dimensions AS JSONB)
            ) ON CONFLICT (replay_run_id) DO NOTHING
            """
        ),
        {
            "replay_run_id": replay_run_id,
            "benchmark_id": benchmark_id,
            "scorecard_version": scorecard["scorecard_version"],
            "dimensions": json.dumps(dimensions, sort_keys=True),
        },
    )
    metric_rows = [
        ("replay", replay_run_id, scorecard.get("run_metrics", {})),
        (
            "benchmark",
            benchmark_id,
            {
                "timeline": scorecard.get("timeline", {}),
                "dimensions": dimensions,
            },
        ),
        *(
            ("case", case_id, metrics)
            for case_id, metrics in scorecard.get("case_metrics", {}).items()
        ),
    ]
    for scope, subject_id, metrics in metric_rows:
        session.execute(
            text(
                """
                INSERT INTO research_quality_metrics (
                    replay_run_id, benchmark_id, metric_scope, subject_id,
                    metric_version, metrics
                ) VALUES (
                    :replay_run_id, :benchmark_id, :metric_scope, :subject_id,
                    :metric_version, CAST(:metrics AS JSONB)
                ) ON CONFLICT (
                    replay_run_id, metric_scope, subject_id, metric_version
                ) DO NOTHING
                """
            ),
            {
                "replay_run_id": replay_run_id,
                "benchmark_id": benchmark_id,
                "metric_scope": scope,
                "subject_id": subject_id,
                "metric_version": METRIC_VERSION,
                "metrics": json.dumps(metrics, sort_keys=True),
            },
        )


def compare_replay_runs(
    session: Any,
    left_run_id: str,
    right_run_id: str,
) -> dict[str, Any]:
    rows = session.execute(
        text(
            """
            SELECT r.id, r.deterministic_input_fingerprint,
                   r.variant_fingerprint, r.variant_identity,
                   r.execution_fingerprint, r.cost_usd, r.stage_metrics,
                   r.result_summary, s.dimensions, s.scorecard_version
            FROM research_replay_runs r
            LEFT JOIN research_benchmark_scorecards s ON s.replay_run_id = r.id
            WHERE r.id IN (:left_id, :right_id)
            """
        ),
        {"left_id": left_run_id, "right_id": right_run_id},
    )
    try:
        mapped = [dict(row) for row in rows.mappings().all()]
    except (AttributeError, TypeError):
        mapped = [dict(row._mapping) for row in rows.all()]
    by_id = {str(row["id"]): row for row in mapped}
    if left_run_id not in by_id or right_run_id not in by_id:
        raise ValueError("replay run not found")
    left, right = by_id[left_run_id], by_id[right_run_id]
    if left["deterministic_input_fingerprint"] != right["deterministic_input_fingerprint"]:
        raise ValueError("replay comparisons require identical deterministic inputs")

    def stage_totals(row: Mapping[str, Any]) -> dict[str, int | float]:
        stages = row.get("stage_metrics") or []
        return {
            "tokens_input": sum(int(item.get("tokens_input") or 0) for item in stages),
            "tokens_output": sum(int(item.get("tokens_output") or 0) for item in stages),
            "latency_ms": sum(int(item.get("duration_ms") or 0) for item in stages),
            "cost_usd": float(row.get("cost_usd") or 0),
        }

    left_totals, right_totals = stage_totals(left), stage_totals(right)
    left_dimensions = left.get("dimensions") or {}
    right_dimensions = right.get("dimensions") or {}
    dimension_changes = {
        key: {
            "left": (left_dimensions.get(key) or {}).get("status"),
            "right": (right_dimensions.get(key) or {}).get("status"),
        }
        for key in sorted(set(left_dimensions) | set(right_dimensions))
        if (left_dimensions.get(key) or {}).get("status")
        != (right_dimensions.get(key) or {}).get("status")
    }
    dimension_comparison = {
        key: {
            "left": left_dimensions.get(key),
            "right": right_dimensions.get(key),
        }
        for key in sorted(set(left_dimensions) | set(right_dimensions))
    }
    left_summary = dict(left.get("result_summary") or {})
    right_summary = dict(right.get("result_summary") or {})
    comparison = {
        "comparison_fingerprint": canonical_fingerprint(
            {
                "left": left_run_id,
                "right": right_run_id,
                "input": left["deterministic_input_fingerprint"],
            }
        ),
        "left_run_id": left_run_id,
        "right_run_id": right_run_id,
        "deterministic_input_fingerprint": (
            left["deterministic_input_fingerprint"]
        ),
        "variant_fingerprints": {
            "left": left["variant_fingerprint"],
            "right": right["variant_fingerprint"],
        },
        "variant_identity": {
            "left": left.get("variant_identity") or {},
            "right": right.get("variant_identity") or {},
        },
        "execution_fingerprints": {
            "left": left["execution_fingerprint"],
            "right": right["execution_fingerprint"],
        },
        "dimension_status_changes": dimension_changes,
        "quality": {
            "dimensions": dimension_comparison,
            "result_summary": {
                "left": left_summary,
                "right": right_summary,
            },
        },
        "resource_usage": {
            "left": left_totals,
            "right": right_totals,
            "delta": {
                key: right_totals[key] - left_totals[key]
                for key in left_totals
            },
        },
    }
    session.execute(
        text(
            """
            INSERT INTO research_quality_metrics (
                replay_run_id, metric_scope, subject_id, metric_version, metrics
            ) VALUES (
                :replay_run_id, 'comparison', :subject_id, :metric_version,
                CAST(:metrics AS JSONB)
            ) ON CONFLICT (
                replay_run_id, metric_scope, subject_id, metric_version
            ) DO NOTHING
            """
        ),
        {
            "replay_run_id": right_run_id,
            "subject_id": comparison["comparison_fingerprint"],
            "metric_version": METRIC_VERSION,
            "metrics": json.dumps(comparison, sort_keys=True),
        },
    )
    return comparison




def persist_live_case_cohorts(
    session: Any,
    cohorts: Sequence[Mapping[str, Any]],
    *,
    as_of: datetime | None = None,
) -> int:
    observed_at = as_of or datetime.now(UTC)
    inserted = 0
    for cohort in cohorts[:120]:
        subject_id = f"{cohort.get('cohort')}:{observed_at.date().isoformat()}"
        result = session.execute(
            text(
                """
                INSERT INTO research_quality_metrics (
                    replay_run_id, metric_scope, subject_id,
                    metric_version, metrics
                )
                SELECT NULL, 'cohort', :subject_id, :metric_version,
                       CAST(:metrics AS JSONB)
                WHERE NOT EXISTS (
                    SELECT 1 FROM research_quality_metrics
                    WHERE replay_run_id IS NULL
                      AND metric_scope = 'cohort'
                      AND subject_id = :subject_id
                      AND metric_version = :metric_version
                )
                ON CONFLICT DO NOTHING
                """
            ),
            {
                "subject_id": subject_id,
                "metric_version": METRIC_VERSION,
                "metrics": json.dumps(
                    {**dict(cohort), "as_of": observed_at.isoformat()},
                    sort_keys=True,
                ),
            },
        )
        inserted += max(0, int(getattr(result, "rowcount", 0) or 0))
    return inserted


__all__ = [
    "METRIC_VERSION",
    "SCORECARD_VERSION",
    "benchmark_lifecycle_timeline",
    "build_benchmark_scorecard",
    "compare_replay_runs",
    "persist_live_case_cohorts",
    "persist_benchmark_scorecard",
]
