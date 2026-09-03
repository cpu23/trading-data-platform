"""Validated, bounded settings for research intelligence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from contracts.runtime_config import (
    DEFAULT_PROMPT_TEMPLATES,
    DEFAULT_STAGE_MAX_TOKENS,
    DEFAULT_STAGE_NAMES,
    ResearchIntelligenceConfig,
)


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
    def from_config(cls, config: ResearchIntelligenceConfig) -> ResearchSettings:
        if not isinstance(config, ResearchIntelligenceConfig):
            raise TypeError(
                f"ResearchSettings requires ResearchIntelligenceConfig, got {type(config).__name__}"
            )
        stage_enabled = {
            stage: config.stages[stage].enabled if stage in config.stages else True
            for stage in DEFAULT_STAGE_NAMES
        }
        prompt_templates = {
            stage: (
                config.stages[stage].prompt_template
                if stage in config.stages and config.stages[stage].prompt_template
                else DEFAULT_PROMPT_TEMPLATES[stage]
            )
            for stage in DEFAULT_STAGE_NAMES
        }
        stage_max_output_tokens = {
            stage: (
                config.stages[stage].max_output_tokens
                if stage in config.stages
                and config.stages[stage].max_output_tokens is not None
                else DEFAULT_STAGE_MAX_TOKENS[stage]
            )
            for stage in DEFAULT_STAGE_NAMES
        }
        lifecycle_thresholds = MappingProxyType(
            config.lifecycle_thresholds.model_dump()
        )
        return cls(
            enabled=config.enabled,
            schedule_enabled=config.schedule_enabled,
            schedule=config.schedule,
            rolling_window_days=config.rolling_window_days,
            maximum_candidate_evidence=config.limits.maximum_candidate_evidence,
            maximum_macro_evidence=config.limits.maximum_macro_evidence,
            maximum_market_drivers=config.limits.maximum_market_drivers,
            maximum_cases_per_run=config.limits.maximum_cases_per_run,
            maximum_claim_documents_per_run=config.limits.maximum_claim_documents_per_run,
            evidence_per_candidate=config.limits.evidence_per_candidate,
            minimum_evidence_count=config.discovery.minimum_evidence_count,
            minimum_source_diversity=config.discovery.minimum_source_diversity,
            graph_depth=config.graph.depth,
            hard_graph_depth=config.graph.hard_depth,
            maximum_graph_nodes=config.graph.maximum_nodes,
            maximum_graph_edges=config.graph.maximum_edges,
            candidate_similarity_threshold=config.discovery.candidate_similarity_threshold,
            merge_similarity_threshold=config.discovery.merge_similarity_threshold,
            publication_limit=config.limits.publication_limit,
            history_limit=config.limits.history_limit,
            model_budget_usd_per_run=config.model_budget_usd_per_run,
            hot_market_universe=tuple(config.hot_market_universe),
            region_universe=tuple(config.region_universe),
            stage_enabled=MappingProxyType(stage_enabled),
            prompt_templates=MappingProxyType(prompt_templates),
            stage_max_output_tokens=MappingProxyType(stage_max_output_tokens),
            model_overrides=MappingProxyType(dict(config.model_overrides)),
            reasoning_effort=MappingProxyType(dict(config.reasoning_effort)),
            lifecycle_thresholds=lifecycle_thresholds,
            promote_discovered_themes=config.promote_discovered_themes,
            claim_extraction_enabled=config.claim_extraction_enabled,
            macro_drivers_enabled=config.macro_drivers_enabled,
        )


__all__ = ["ResearchSettings"]
