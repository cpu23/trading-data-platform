"""Validated, bounded settings for research intelligence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

DEFAULT_STAGE_NAMES = (
    "claim_extraction",
    "pattern_discovery",
    "causal_chain",
    "value_capture",
    "adversarial",
    "deliverable",
    "macro_transmission",
)
DEFAULT_PROMPT_TEMPLATES = {
    "claim_extraction": "prompts/research_claim_extraction_v2.txt",
    "pattern_discovery": "prompts/research_pattern_discovery_v2.txt",
    "causal_chain": "prompts/research_causal_chain_v2.txt",
    "value_capture": "prompts/research_value_capture_v2.txt",
    "adversarial": "prompts/research_adversarial_v2.txt",
    "deliverable": "prompts/research_deliverable_v2.txt",
    "macro_transmission": "prompts/macro_transmission_v3.txt",
}
DEFAULT_STAGE_MAX_TOKENS = {
    "claim_extraction": 4096,
    "pattern_discovery": 4096,
    "causal_chain": 4096,
    "value_capture": 4096,
    "adversarial": 4096,
    "deliverable": 4096,
    "macro_transmission": 4096,
}
DEFAULT_LIFECYCLE_THRESHOLDS = {
    "forming_evidence": 3,
    "corroborated_evidence": 5,
    "corroborated_days": 7,
    "research_ready_evidence": 6,
    "mature_evidence": 10,
    "mature_snapshots": 3,
    "weakening_days": 45,
    "archive_days": 120,
}
DEFAULT_MARKETS = (
    "EURUSD",
    "DXY",
    "AUDJPY",
    "USDJPY",
    "SP500",
    "XAUUSD",
    "XPTUSD",
    "GER40",
    "UK100",
)
DEFAULT_REGIONS = ("US", "euro_area", "UK", "Japan")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _boolean(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _integer(value: Any, default: int, low: int, high: int, field: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"research_intelligence.{field} must be {low}..{high}")
    return value


def _number(value: Any, default: float, low: float, high: float, field: str) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"research_intelligence.{field} must be numeric")
    parsed = float(value)
    if not low <= parsed <= high:
        raise ValueError(f"research_intelligence.{field} must be {low}..{high}")
    return parsed


def _strings(value: Any, default: tuple[str, ...], maximum: int, field: str) -> tuple[str, ...]:
    if value is None:
        return default
    if not isinstance(value, list):
        raise ValueError(f"research_intelligence.{field} must be an array")
    cleaned: list[str] = []
    for item in value[:maximum]:
        text = str(item or "").strip()
        if text and text not in cleaned:
            cleaned.append(text[:80])
    return tuple(cleaned)


@dataclass(frozen=True, slots=True)
class ResearchSettings:
    enabled: bool
    schedule_enabled: bool
    schedule: str
    rolling_window_days: int
    maximum_candidate_evidence: int
    maximum_macro_evidence: int
    maximum_market_drivers: int
    maximum_cases_per_run: int
    maximum_claim_documents_per_run: int
    evidence_per_candidate: int
    minimum_evidence_count: int
    minimum_source_diversity: int
    graph_depth: int
    hard_graph_depth: int
    maximum_graph_nodes: int
    maximum_graph_edges: int
    candidate_similarity_threshold: float
    merge_similarity_threshold: float
    publication_limit: int
    history_limit: int
    model_budget_usd_per_run: float
    hot_market_universe: tuple[str, ...]
    region_universe: tuple[str, ...]
    stage_enabled: Mapping[str, bool]
    prompt_templates: Mapping[str, str]
    stage_max_output_tokens: Mapping[str, int]
    model_overrides: Mapping[str, str]
    reasoning_effort: Mapping[str, str]
    lifecycle_thresholds: Mapping[str, int]
    promote_discovered_themes: bool
    claim_extraction_enabled: bool
    macro_drivers_enabled: bool

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> ResearchSettings:
        root = _mapping(config)
        values = _mapping(root.get("research_intelligence"))
        graph = _mapping(values.get("graph"))
        limits = _mapping(values.get("limits"))
        discovery = _mapping(values.get("discovery"))
        lifecycle = _mapping(values.get("lifecycle_thresholds"))
        stages = _mapping(values.get("stages"))
        models = _mapping(values.get("model_overrides"))
        reasoning = _mapping(values.get("reasoning_effort"))

        hard_depth = _integer(
            graph.get("hard_depth"), 5, 1, 8, "graph.hard_depth"
        )
        depth = _integer(graph.get("depth"), 3, 1, hard_depth, "graph.depth")
        stage_enabled = {
            stage: _boolean(_mapping(stages.get(stage)).get("enabled"), True)
            for stage in DEFAULT_STAGE_NAMES
        }
        prompt_templates: dict[str, str] = {}
        stage_max_output_tokens: dict[str, int] = {}
        for stage in DEFAULT_STAGE_NAMES:
            stage_values = _mapping(stages.get(stage))
            prompt_path = str(
                stage_values.get("prompt_template") or DEFAULT_PROMPT_TEMPLATES[stage]
            ).strip()
            if not prompt_path or len(prompt_path) > 500:
                raise ValueError(
                    f"research_intelligence.stages.{stage}.prompt_template is invalid"
                )
            prompt_templates[stage] = prompt_path
            stage_max_output_tokens[stage] = _integer(
                stage_values.get("max_output_tokens"),
                DEFAULT_STAGE_MAX_TOKENS[stage],
                1,
                4096,
                f"stages.{stage}.max_output_tokens",
            )
        model_overrides = {
            stage: str(model).strip()
            for stage, model in models.items()
            if stage in DEFAULT_STAGE_NAMES and isinstance(model, str) and model.strip()
        }
        reasoning_effort: dict[str, str] = {}
        for stage, effort in reasoning.items():
            if stage not in DEFAULT_STAGE_NAMES or effort is None:
                continue
            normalized = str(effort).strip().casefold()
            if normalized not in {"minimal", "low", "medium", "high", "xhigh"}:
                raise ValueError(
                    f"research_intelligence.reasoning_effort.{stage} is invalid"
                )
            reasoning_effort[stage] = normalized

        lifecycle_values: dict[str, int] = {}
        for key, default in DEFAULT_LIFECYCLE_THRESHOLDS.items():
            high = 3650 if key.endswith("_days") else 10_000
            lifecycle_values[key] = _integer(
                lifecycle.get(key), default, 1, high, f"lifecycle_thresholds.{key}"
            )
        if lifecycle_values["archive_days"] <= lifecycle_values["weakening_days"]:
            raise ValueError("research lifecycle archive_days must exceed weakening_days")

        schedule = str(values.get("schedule") or "15 8 * * 1-5").strip()
        if len(schedule.split()) != 5:
            raise ValueError("research_intelligence.schedule must be a five-field cron")

        return cls(
            enabled=_boolean(values.get("enabled"), True),
            schedule_enabled=_boolean(values.get("schedule_enabled"), True),
            schedule=schedule,
            rolling_window_days=_integer(
                values.get("rolling_window_days"), 45, 1, 730, "rolling_window_days"
            ),
            maximum_candidate_evidence=_integer(
                limits.get("maximum_candidate_evidence"),
                240,
                10,
                2_000,
                "limits.maximum_candidate_evidence",
            ),
            maximum_macro_evidence=_integer(
                limits.get("maximum_macro_evidence"),
                48,
                10,
                200,
                "limits.maximum_macro_evidence",
            ),
            maximum_market_drivers=_integer(
                limits.get("maximum_market_drivers"),
                8,
                1,
                8,
                "limits.maximum_market_drivers",
            ),
            maximum_cases_per_run=_integer(
                limits.get("maximum_cases_per_run"),
                8,
                1,
                100,
                "limits.maximum_cases_per_run",
            ),
            maximum_claim_documents_per_run=_integer(
                limits.get("maximum_claim_documents_per_run"),
                8,
                0,
                100,
                "limits.maximum_claim_documents_per_run",
            ),
            evidence_per_candidate=_integer(
                limits.get("evidence_per_candidate"),
                24,
                3,
                100,
                "limits.evidence_per_candidate",
            ),
            minimum_evidence_count=_integer(
                discovery.get("minimum_evidence_count"),
                3,
                2,
                50,
                "discovery.minimum_evidence_count",
            ),
            minimum_source_diversity=_integer(
                discovery.get("minimum_source_diversity"),
                2,
                1,
                10,
                "discovery.minimum_source_diversity",
            ),
            graph_depth=depth,
            hard_graph_depth=hard_depth,
            maximum_graph_nodes=_integer(
                graph.get("maximum_nodes"), 40, 2, 200, "graph.maximum_nodes"
            ),
            maximum_graph_edges=_integer(
                graph.get("maximum_edges"), 60, 1, 400, "graph.maximum_edges"
            ),
            candidate_similarity_threshold=_number(
                discovery.get("candidate_similarity_threshold"),
                0.35,
                0.05,
                1,
                "discovery.candidate_similarity_threshold",
            ),
            merge_similarity_threshold=_number(
                discovery.get("merge_similarity_threshold"),
                0.72,
                0.1,
                1,
                "discovery.merge_similarity_threshold",
            ),
            publication_limit=_integer(
                limits.get("publication_limit"), 20, 1, 100, "limits.publication_limit"
            ),
            history_limit=_integer(
                limits.get("history_limit"), 50, 1, 200, "limits.history_limit"
            ),
            model_budget_usd_per_run=_number(
                values.get("model_budget_usd_per_run"),
                0.75,
                0,
                100,
                "model_budget_usd_per_run",
            ),
            hot_market_universe=_strings(
                values.get("hot_market_universe"),
                DEFAULT_MARKETS,
                50,
                "hot_market_universe",
            ),
            region_universe=_strings(
                values.get("region_universe"), DEFAULT_REGIONS, 20, "region_universe"
            ),
            stage_enabled=MappingProxyType(stage_enabled),
            prompt_templates=MappingProxyType(prompt_templates),
            stage_max_output_tokens=MappingProxyType(stage_max_output_tokens),
            model_overrides=MappingProxyType(model_overrides),
            reasoning_effort=MappingProxyType(reasoning_effort),
            lifecycle_thresholds=MappingProxyType(lifecycle_values),
            promote_discovered_themes=_boolean(
                values.get("promote_discovered_themes"), True
            ),
            claim_extraction_enabled=_boolean(
                values.get("claim_extraction_enabled"), True
            ),
            macro_drivers_enabled=_boolean(values.get("macro_drivers_enabled"), True),
        )


__all__ = [
    "DEFAULT_PROMPT_TEMPLATES",
    "DEFAULT_STAGE_MAX_TOKENS",
    "DEFAULT_LIFECYCLE_THRESHOLDS",
    "DEFAULT_MARKETS",
    "DEFAULT_REGIONS",
    "DEFAULT_STAGE_NAMES",
    "ResearchSettings",
]
