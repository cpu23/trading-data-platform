"""Bounded read models for research cases, drivers and operator status."""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text

_MAX_LIST = 100
_MAX_OFFSET = 1_000
_MAX_DETAIL = 200
_MAX_HISTORY = 100


def _rows(result: Any) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in result.mappings().all()]
    except (AttributeError, TypeError):
        try:
            return [dict(row._mapping) for row in result]
        except (AttributeError, TypeError):
            return []


def _first(result: Any) -> dict[str, Any] | None:
    try:
        row = result.mappings().first()
    except (AttributeError, TypeError):
        try:
            row = result.first()
        except (AttributeError, TypeError):
            return None
    if row is None:
        return None
    try:
        return dict(row)
    except (TypeError, ValueError):
        return dict(row._mapping)


def _uuid(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("invalid case id") from None


def list_cases(
    session: Any,
    *,
    limit: int = 50,
    offset: int = 0,
    lifecycle_state: str | None = None,
    changed_only: bool = False,
) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(int(limit), _MAX_LIST))
    bounded_offset = max(0, min(int(offset), _MAX_OFFSET))
    states = {
        "candidate",
        "forming",
        "corroborated",
        "research_ready",
        "mature",
        "weakening",
        "archived",
    }
    if lifecycle_state is not None and lifecycle_state not in states:
        raise ValueError("invalid lifecycle state")
    rows = _rows(
        session.execute(
            text(
                """
                SELECT c.id, c.title, c.definition, c.horizon, c.case_type,
                       c.lifecycle_state, c.origin, c.economic_significance,
                       c.market_sensitivity, c.persistence, c.breadth,
                       c.investability, c.evidence_strength, c.time_sensitivity,
                       c.first_seen_at, c.last_evidence_at, c.last_changed_at,
                       c.current_version, c.updated_at,
                       s.change_summary,
                       s.payload->'deliverable'->'what_changed'->>'text' AS what_changed,
                       s.payload->'deliverable'->'why_it_matters'->>'text' AS why_it_matters,
                       (SELECT COUNT(*) FROM research_case_evidence e WHERE e.case_id = c.id) AS evidence_count,
                       (SELECT COUNT(*) FROM research_counterevidence ce WHERE ce.case_id = c.id) AS counterevidence_count,
                       (SELECT COUNT(*) FROM research_data_requests r WHERE r.case_id = c.id AND r.status = 'unresolved') AS open_request_count
                FROM research_cases c
                LEFT JOIN research_case_snapshots s
                  ON s.case_id = c.id AND s.version = c.current_version
                WHERE (:lifecycle_state IS NULL OR c.lifecycle_state = :lifecycle_state)
                  AND (NOT :changed_only OR c.last_changed_at >= NOW() - INTERVAL '7 days')
                ORDER BY
                    CASE c.lifecycle_state
                        WHEN 'research_ready' THEN 1 WHEN 'mature' THEN 2
                        WHEN 'corroborated' THEN 3 WHEN 'forming' THEN 4
                        WHEN 'candidate' THEN 5 WHEN 'weakening' THEN 6 ELSE 7
                    END,
                    c.last_changed_at DESC, c.id
                LIMIT :limit OFFSET :offset
                """
            ),
            {
                "lifecycle_state": lifecycle_state,
                "changed_only": bool(changed_only),
                "limit": bounded_limit,
                "offset": bounded_offset,
            },
        )
    )
    return rows


def get_case(session: Any, case_id: str, *, detail_limit: int = 100) -> dict[str, Any] | None:
    parsed = _uuid(case_id)
    bounded = max(1, min(int(detail_limit), _MAX_DETAIL))
    case = _first(
        session.execute(
            text(
                """
                SELECT c.*, s.id AS snapshot_id, s.change_summary,
                       s.payload AS current_snapshot, s.created_at AS snapshot_created_at,
                       t.id AS theme_id, t.name AS theme_name, t.origin AS theme_origin
                FROM research_cases c
                LEFT JOIN research_case_snapshots s
                  ON s.case_id = c.id AND s.version = c.current_version
                LEFT JOIN investment_themes t ON t.source_case_id = c.id
                WHERE c.id = :case_id LIMIT 1
                """
            ),
            {"case_id": parsed},
        )
    )
    if case is None:
        return None
    entities = _rows(
        session.execute(
            text(
                """
                SELECT entity_type, normalized_key, display_name, role,
                       first_seen_at, last_seen_at
                FROM research_case_entities WHERE case_id = :case_id
                ORDER BY entity_type, display_name LIMIT :limit
                """
            ),
            {"case_id": parsed, "limit": bounded},
        )
    )
    evidence = _rows(
        session.execute(
            text(
                """
                SELECT evidence_type, evidence_id, source_name, title,
                       source_reference, relationship, evidence_fingerprint,
                       source_timestamp, excerpt, created_at
                FROM research_case_evidence WHERE case_id = :case_id
                ORDER BY source_timestamp DESC, evidence_type, evidence_id
                LIMIT :limit
                """
            ),
            {"case_id": parsed, "limit": bounded},
        )
    )
    edges = _rows(
        session.execute(
            text(
                """
                SELECT x.*,
                       COALESCE(
                           ARRAY_AGG(ee.evidence_type || ':' || ee.evidence_id)
                               FILTER (WHERE ee.evidence_id IS NOT NULL),
                           '{}'::TEXT[]
                       ) AS evidence_ids
                FROM research_causal_edges x
                LEFT JOIN research_causal_edge_evidence ee ON ee.edge_id = x.id
                WHERE x.case_id = :case_id AND x.superseded_at IS NULL
                GROUP BY x.id
                ORDER BY x.depth, x.created_at, x.id LIMIT :limit
                """
            ),
            {"case_id": parsed, "limit": bounded},
        )
    )
    value_capture = _rows(
        session.execute(
            text(
                """
                SELECT v.*,
                       COALESCE(
                           ARRAY_AGG(ve.evidence_type || ':' || ve.evidence_id)
                               FILTER (WHERE ve.evidence_id IS NOT NULL),
                           '{}'::TEXT[]
                       ) AS evidence_ids
                FROM research_value_capture_assessments v
                LEFT JOIN research_value_capture_evidence ve ON ve.assessment_id = v.id
                WHERE v.case_id = :case_id AND v.superseded_at IS NULL
                GROUP BY v.id
                ORDER BY v.node_type, v.node_name LIMIT :limit
                """
            ),
            {"case_id": parsed, "limit": bounded},
        )
    )
    counterevidence = _rows(
        session.execute(
            text(
                """
                SELECT ce.*,
                       COALESCE(
                           ARRAY_AGG(cee.evidence_type || ':' || cee.evidence_id)
                               FILTER (WHERE cee.evidence_id IS NOT NULL),
                           '{}'::TEXT[]
                       ) AS evidence_ids
                FROM research_counterevidence ce
                LEFT JOIN research_counterevidence_evidence cee
                  ON cee.counterevidence_id = ce.id
                WHERE ce.case_id = :case_id
                GROUP BY ce.id
                ORDER BY ce.created_at DESC, ce.id LIMIT :limit
                """
            ),
            {"case_id": parsed, "limit": bounded},
        )
    )
    requests = _rows(
        session.execute(
            text(
                """
                SELECT id, subject, requested_evidence_type, reason,
                       desired_frequency, priority, status,
                       candidate_source_class, linked_evidence_type,
                       linked_evidence_id, input_fingerprint, model_slug,
                       prompt_version, generation_attempt_id,
                       created_at, updated_at, resolved_at
                FROM research_data_requests WHERE case_id = :case_id
                ORDER BY
                    CASE priority WHEN 'high' THEN 1 WHEN 'moderate' THEN 2 ELSE 3 END,
                    created_at DESC, id LIMIT :limit
                """
            ),
            {"case_id": parsed, "limit": bounded},
        )
    )
    aliases = _rows(
        session.execute(
            text(
                "SELECT alias FROM research_case_aliases WHERE case_id = :case_id ORDER BY alias LIMIT 50"
            ),
            {"case_id": parsed},
        )
    )
    return {
        "case": case,
        "aliases": [row["alias"] for row in aliases],
        "entities": entities,
        "evidence": evidence,
        "causal_edges": edges,
        "value_capture": value_capture,
        "counterevidence": counterevidence,
        "data_requests": requests,
        "bounds": {
            "detail_limit": bounded,
            "evidence_truncated": len(evidence) >= bounded,
            "edges_truncated": len(edges) >= bounded,
        },
    }


def case_history(session: Any, case_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    parsed = _uuid(case_id)
    bounded = max(1, min(int(limit), _MAX_HISTORY))
    return _rows(
        session.execute(
            text(
                """
                SELECT id, case_id, version, input_fingerprint, lifecycle_state,
                       change_summary, payload, model_slug, prompt_version,
                       generation_attempt_id, correlation_id, created_at
                FROM research_case_snapshots WHERE case_id = :case_id
                ORDER BY version DESC LIMIT :limit
                """
            ),
            {"case_id": parsed, "limit": bounded},
        )
    )


def current_market_drivers(
    session: Any, *, changed_only: bool = False, limit: int = 50
) -> list[dict[str, Any]]:
    bounded = max(1, min(int(limit), _MAX_LIST))
    return _rows(
        session.execute(
            text(
                """
                SELECT d.id, d.target, d.driver_key, d.driver_label, d.direction,
                       d.strength, d.horizon, d.mechanism,
                       d.changed_since_prior, d.invalidation_conditions,
                       d.confidence, d.confidence_rationale, d.model_slug,
                       d.prompt_version, d.generation_attempt_id, d.valid_from,
                       f.factor_key, f.factor_label, f.state AS factor_state,
                       f.strength AS factor_strength, f.horizon AS factor_horizon,
                       f.mechanism AS factor_mechanism,
                       f.invalidation_conditions AS factor_invalidation_conditions,
                       f.confidence AS factor_confidence,
                       COALESCE(
                           ARRAY_AGG(e.evidence_type || ':' || e.evidence_id)
                               FILTER (WHERE e.evidence_id IS NOT NULL),
                           '{}'::TEXT[]
                       ) AS evidence_ids
                FROM research_market_drivers d
                LEFT JOIN research_economic_factors f ON f.id = d.factor_id
                LEFT JOIN research_market_driver_evidence e ON e.driver_id = d.id
                WHERE d.superseded_at IS NULL
                  AND (NOT :changed_only OR d.changed_since_prior)
                GROUP BY d.id, f.id
                ORDER BY d.changed_since_prior DESC, d.target, d.driver_key
                LIMIT :limit
                """
            ),
            {"changed_only": bool(changed_only), "limit": bounded},
        )
    )


def list_replay_runs(
    session: Any,
    *,
    benchmark_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(int(limit), _MAX_LIST))
    bounded_offset = max(0, min(int(offset), _MAX_OFFSET))
    return _rows(
        session.execute(
            text(
                """
                SELECT r.id, r.benchmark_id, r.replay_as_of, r.evidence_source,
                       r.status, r.comparison_group, r.variant_fingerprint,
                       r.variant_identity, r.model_overrides, r.prompt_overrides,
                       r.audit,
                       r.deterministic_metrics, r.stage_metrics,
                       r.result_summary, r.cost_usd, r.started_at,
                       r.completed_at, r.created_at, s.scorecard_version,
                       s.dimensions, s.human_annotations, s.annotation_version,
                       s.annotated_by, s.annotated_at
                FROM research_replay_runs r
                LEFT JOIN research_benchmark_scorecards s
                  ON s.replay_run_id = r.id
                WHERE (:benchmark_id IS NULL OR r.benchmark_id = :benchmark_id)
                ORDER BY r.replay_as_of DESC, r.created_at DESC, r.id
                LIMIT :limit OFFSET :offset
                """
            ),
            {
                "benchmark_id": benchmark_id,
                "limit": bounded_limit,
                "offset": bounded_offset,
            },
        )
    )


def get_replay_run(
    session: Any,
    replay_run_id: str,
    *,
    detail_limit: int = 100,
) -> dict[str, Any] | None:
    parsed = _uuid(replay_run_id)
    bounded = max(1, min(int(detail_limit), _MAX_DETAIL))
    run = _first(
        session.execute(
            text(
                """
                SELECT r.*, s.scorecard_version, s.dimensions,
                       s.human_annotations, s.annotation_version,
                       s.evaluator_judgment
                FROM research_replay_runs r
                LEFT JOIN research_benchmark_scorecards s
                  ON s.replay_run_id = r.id
                WHERE r.id = :run_id
                LIMIT 1
                """
            ),
            {"run_id": parsed},
        )
    )
    if run is None:
        return None
    cases = _rows(
        session.execute(
            text(
                """
                SELECT semantic_fingerprint, title, definition,
                       case_is_economic_proposition, proposition_rationale,
                       lifecycle_state, first_qualifying_evidence_at,
                       first_detection_at, evidence_count, source_diversity,
                       maximum_graph_depth, payload, created_at
                FROM research_replay_cases
                WHERE replay_run_id = :run_id
                ORDER BY lifecycle_state, title, semantic_fingerprint
                LIMIT :limit
                """
            ),
            {"run_id": parsed, "limit": bounded},
        )
    )
    timeline = _rows(
        session.execute(
            text(
                """
                SELECT semantic_fingerprint, event_type, occurred_at, detail
                FROM research_replay_timeline_events
                WHERE replay_run_id = :run_id
                ORDER BY occurred_at, id
                LIMIT :limit
                """
            ),
            {"run_id": parsed, "limit": bounded},
        )
    )
    metrics = _rows(
        session.execute(
            text(
                """
                SELECT benchmark_id, metric_scope, subject_id,
                       metric_version, metrics, created_at
                FROM research_quality_metrics
                WHERE replay_run_id = :run_id
                ORDER BY metric_scope, subject_id, created_at
                LIMIT :limit
                """
            ),
            {"run_id": parsed, "limit": bounded},
        )
    )
    annotations = _rows(
        session.execute(
            text(
                """
                SELECT a.annotation_version, a.annotations, a.annotated_by,
                       a.created_at
                FROM research_benchmark_annotations a
                JOIN research_benchmark_scorecards s ON s.id = a.scorecard_id
                WHERE s.replay_run_id = :run_id
                ORDER BY a.annotation_version DESC
                LIMIT :limit
                """
            ),
            {"run_id": parsed, "limit": bounded},
        )
    )
    return {
        "run": run,
        "cases": cases,
        "timeline": timeline,
        "metrics": metrics,
        "annotation_history": annotations,
        "bounds": {
            "detail_limit": bounded,
            "cases_truncated": len(cases) >= bounded,
            "timeline_truncated": len(timeline) >= bounded,
            "annotations_truncated": len(annotations) >= bounded,
        },
    }


def _median_days_to(
    members: Sequence[Mapping[str, Any]],
    field: str,
) -> float | None:
    values = [
        max(0.0, (row[field] - row["first_seen_at"]).total_seconds() / 86_400)
        for row in members
        if isinstance(row.get(field), datetime)
    ]
    return statistics.median(values) if values else None


def live_case_cohorts(
    session: Any,
    *,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    cutoff = since or datetime.now(UTC) - timedelta(days=730)
    result = session.execute(
        text(
            """
            SELECT c.id, c.first_seen_at, c.last_evidence_at, c.lifecycle_state,
                   MIN(s.created_at) FILTER (
                       WHERE s.lifecycle_state IN (
                           'forming', 'corroborated', 'research_ready', 'mature'
                       )
                   ) AS first_forming_at,
                   MIN(s.created_at) FILTER (
                       WHERE s.lifecycle_state = 'corroborated'
                   ) AS first_corroborated_at,
                   MIN(s.created_at) FILTER (
                       WHERE s.lifecycle_state IN ('research_ready', 'mature')
                   ) AS first_research_ready_at,
                   MIN(s.created_at) FILTER (
                       WHERE s.lifecycle_state = 'mature'
                   ) AS first_mature_at,
                   COALESCE(BOOL_OR(
                       s.lifecycle_state IN ('corroborated', 'research_ready', 'mature')
                   ), FALSE) AS ever_corroborated,
                   COALESCE(BOOL_OR(
                       s.lifecycle_state IN ('research_ready', 'mature')
                   ), FALSE) AS ever_research_ready,
                   COALESCE(BOOL_OR(s.lifecycle_state = 'mature'), FALSE) AS ever_mature
            FROM research_cases c
            LEFT JOIN research_case_snapshots s ON s.case_id = c.id
            WHERE c.first_seen_at >= :cutoff
            GROUP BY c.id
            ORDER BY c.first_seen_at, c.id
            LIMIT 5000
            """
        ),
        {"cutoff": cutoff},
    )
    try:
        rows = [dict(row) for row in result.mappings().all()]
    except (AttributeError, TypeError):
        rows = [dict(row._mapping) for row in result.all()]
    cohorts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        first = row["first_seen_at"]
        if first.tzinfo is None:
            first = first.replace(tzinfo=UTC)
        cohorts[first.astimezone(UTC).strftime("%Y-%m")].append(row)
    output: list[dict[str, Any]] = []
    for cohort, members in sorted(cohorts.items()):
        durations = [
            max(0, (row["last_evidence_at"] - row["first_seen_at"]).days)
            for row in members
        ]


        formed = sum(1 for row in members if row.get("first_forming_at"))
        corroborated = sum(1 for row in members if row.get("ever_corroborated"))
        ready = sum(1 for row in members if row.get("ever_research_ready"))
        mature = sum(1 for row in members if row.get("ever_mature"))
        archived = sum(1 for row in members if row["lifecycle_state"] == "archived")
        weak_archived = sum(
            1
            for row in members
            if row["lifecycle_state"] == "archived"
            and not row.get("ever_corroborated")
        )
        ready_then_archived = sum(
            1
            for row in members
            if row["lifecycle_state"] == "archived"
            and row.get("ever_research_ready")
        )
        state_counts = {
            state: sum(1 for row in members if row["lifecycle_state"] == state)
            for state in (
                "candidate",
                "forming",
                "corroborated",
                "research_ready",
                "mature",
                "weakening",
                "archived",
            )
        }
        output.append(
            {
                "cohort": cohort,
                "case_count": len(members),
                "formed_count": formed,
                "forming_to_corroborated_rate": (
                    corroborated / formed if formed else None
                ),
                "corroborated_count": corroborated,
                "corroborated_to_research_ready_rate": (
                    ready / corroborated if corroborated else None
                ),
                "research_ready_count": ready,
                "research_ready_rate": ready / len(members),
                "mature_count": mature,
                "mature_rate": mature / len(members),
                "archived_count": archived,
                "archived_rate": archived / len(members),
                "weak_case_archived_count": weak_archived,
                "weak_case_archived_rate": weak_archived / len(members),
                "research_ready_then_archived_count": ready_then_archived,
                "median_survival_days": (
                    statistics.median(durations) if durations else 0
                ),
                "median_days_to_forming": _median_days_to(
                    members, "first_forming_at"
                ),
                "median_days_to_corroborated": _median_days_to(
                    members, "first_corroborated_at"
                ),
                "median_days_to_research_ready": _median_days_to(
                    members, "first_research_ready_at"
                ),
                "median_days_to_mature": _median_days_to(
                    members, "first_mature_at"
                ),
                "current_state_counts": state_counts,
            }
        )
    return output
def list_quality_metrics(
    session: Any,
    *,
    metric_scope: str | None = None,
    benchmark_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    allowed_scopes = {"replay", "case", "benchmark", "cohort", "comparison"}
    if metric_scope is not None and metric_scope not in allowed_scopes:
        raise ValueError("invalid metric scope")
    bounded = max(1, min(int(limit), _MAX_DETAIL))
    return _rows(
        session.execute(
            text(
                """
                SELECT id, replay_run_id, benchmark_id, metric_scope,
                       subject_id, metric_version, metrics, created_at
                FROM research_quality_metrics
                WHERE (:metric_scope IS NULL OR metric_scope = :metric_scope)
                  AND (:benchmark_id IS NULL OR benchmark_id = :benchmark_id)
                ORDER BY created_at DESC, id
                LIMIT :limit
                """
            ),
            {
                "metric_scope": metric_scope,
                "benchmark_id": benchmark_id,
                "limit": bounded,
            },
        )
    )


def research_status(session: Any, *, limit: int = 20) -> dict[str, Any]:
    bounded = max(1, min(int(limit), 100))
    state_rows = _rows(
        session.execute(
            text(
                """
                SELECT lifecycle_state, COUNT(*) AS count
                FROM research_cases GROUP BY lifecycle_state ORDER BY lifecycle_state
                """
            )
        )
    )
    counts = _first(
        session.execute(
            text(
                """
                SELECT
                    (SELECT COUNT(*) FROM research_data_requests WHERE status IN ('unresolved', 'in_progress', 'partially_satisfied')) AS unresolved_requests,
                    (SELECT COUNT(*) FROM research_market_drivers WHERE superseded_at IS NULL) AS current_drivers,
                    (SELECT COUNT(*) FROM research_economic_factors WHERE superseded_at IS NULL) AS current_factors,
                    (SELECT COUNT(*) FROM research_replay_runs WHERE status IN ('completed', 'completed_with_errors')) AS completed_replays,
                    (SELECT COALESCE(SUM(cost_usd), 0) FROM generation_attempts WHERE processor LIKE 'research_%' AND created_at >= CURRENT_DATE) AS today_cost_usd
                """
            )
        )
    ) or {}
    jobs = _rows(
        session.execute(
            text(
                """
                SELECT id, job_type, state, attempt_count, max_attempts, correlation_id,
                       result_ref, created_at, started_at, completed_at
                FROM analysis_jobs
                WHERE job_type IN ('research_discovery', 'research_case_update')
                ORDER BY created_at DESC, id LIMIT :limit
                """
            ),
            {"limit": bounded},
        )
    )
    return {
        "enabled_objects": True,
        "cases_by_state": {
            str(row.get("lifecycle_state")): int(row.get("count") or 0)
            for row in state_rows
        },
        "unresolved_data_requests": int(counts.get("unresolved_requests") or 0),
        "current_market_drivers": int(counts.get("current_drivers") or 0),
        "current_economic_factors": int(counts.get("current_factors") or 0),
        "completed_replays": int(counts.get("completed_replays") or 0),
        "today_model_cost_usd": float(counts.get("today_cost_usd") or 0),
        "recent_jobs": jobs,
        "limit": bounded,
    }


def snapshot_payload(case: Mapping[str, Any]) -> Mapping[str, Any]:
    value = case.get("current_snapshot")
    return value if isinstance(value, Mapping) else {}


__all__ = [
    "case_history",
    "current_market_drivers",
    "get_case",
    "get_replay_run",
    "list_cases",
    "list_quality_metrics",
    "list_replay_runs",
    "research_status",
    "snapshot_payload",
]
