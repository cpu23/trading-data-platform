"""Versioned strict schemas and budgeted model-stage runner."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar
from uuid import UUID, uuid4

from budgets import BudgetContext
from llm_client import LLMStage, resolve_model
from processors.base import load_prompt_template
from sqlalchemy import text

from contracts.runtime_config import AppConfig
from research_intelligence.adversarial import RESEARCH_DATA_TYPES
from research_intelligence.claims import MAX_SOURCE_CLAIMS_PER_BATCH
from research_intelligence.config import ResearchSettings
from research_intelligence.contracts import (
    ModelProvenance,
    canonical_fingerprint,
    parse_json_payload,
)
from research_intelligence.relationships import ENTITY_TYPES, RELATIONSHIPS

T = TypeVar("T")

STAGE_VERSIONS = {
    "claim_extraction": "research_claim_extraction_v2",
    "pattern_discovery": "research_pattern_discovery_v2",
    "causal_chain": "research_causal_chain_v2",
    "value_capture": "research_value_capture_v2",
    "adversarial": "research_adversarial_v2",
    "deliverable": "research_deliverable_v2",
    "macro_transmission": "macro_transmission_v3",
}


def _object(
    properties: Mapping[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": dict(properties),
        "required": required or list(properties),
    }


def _array(items: Mapping[str, Any], maximum: int) -> dict[str, Any]:
    return {"type": "array", "items": dict(items), "maxItems": maximum}


def _nullable(kind: str, **kwargs: Any) -> dict[str, Any]:
    return {"anyOf": [{"type": kind, **kwargs}, {"type": "null"}]}


_STRING = {"type": "string"}
_NULLABLE_STRING = _nullable("string")
_EVIDENCE_IDS = _array(_STRING, 100)
_ENTITY = _object(
    {
        "entity_type": {"type": "string", "enum": sorted(ENTITY_TYPES)},
        "name": _STRING,
    }
)
_IMPORTANCE = _object(
    {
        name: _nullable("string", enum=["low", "moderate", "high"])
        for name in (
            "economic_significance",
            "market_sensitivity",
            "persistence",
            "breadth",
            "investability",
            "evidence_strength",
            "time_sensitivity",
        )
    }
)
_IMPORTANCE_RATIONALE = _object({name: _STRING for name in _IMPORTANCE["properties"]})
_VALUE_DIMENSIONS = (
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
)

CLAIM_SCHEMA = {
    "name": STAGE_VERSIONS["claim_extraction"],
    "strict": True,
    "schema": _object(
        {
            "abstained": {"type": "boolean"},
            "claims": _array(
                _object(
                    {
                        "source_evidence_id": _STRING,
                        "subject": _STRING,
                        "predicate": _STRING,
                        "object_value": _NULLABLE_STRING,
                        "unit": _NULLABLE_STRING,
                        "period": _NULLABLE_STRING,
                        "geography": _NULLABLE_STRING,
                        "direction": _nullable(
                            "string",
                            enum=[
                                "increase",
                                "decrease",
                                "higher",
                                "lower",
                                "flat",
                                "mixed",
                                "unknown",
                            ],
                        ),
                        "claim_kind": {
                            "type": "string",
                            "enum": [
                                "reported_fact",
                                "company_guidance",
                                "estimate",
                                "opinion",
                            ],
                        },
                        "source_span": _STRING,
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "entities": _array(_ENTITY, 5),
                    }
                ),
                min(MAX_SOURCE_CLAIMS_PER_BATCH, 4),
            ),
        }
    ),
}

PATTERN_SCHEMA = {
    "name": STAGE_VERSIONS["pattern_discovery"],
    "strict": True,
    "schema": _object(
        {
            "abstained": {"type": "boolean"},
            "coherent": {"type": "boolean"},
            "label": _NULLABLE_STRING,
            "definition": _NULLABLE_STRING,
            "case_type": {
                "type": "string",
                "enum": ["cyclical", "structural", "event_driven", "unclear"],
            },
            "horizon": {
                "type": "string",
                "enum": [
                    "intraday",
                    "days",
                    "weeks",
                    "months",
                    "multi_year",
                    "unknown",
                ],
            },
            "what_changed": _NULLABLE_STRING,
            "supporting_evidence_ids": _array(_STRING, 8),
            "contradicting_evidence_ids": _array(_STRING, 8),
            "context_evidence_ids": _array(_STRING, 8),
            "entities": _array(_ENTITY, 12),
            "industries": _array(_STRING, 8),
            "macro_drivers": _array(_STRING, 8),
            "missing_information": _array(_STRING, 8),
            "importance": _IMPORTANCE,
            "importance_rationale": _IMPORTANCE_RATIONALE,
            "aliases": _array(_STRING, 8),
        }
    ),
}

CAUSAL_SCHEMA = {
    "name": STAGE_VERSIONS["causal_chain"],
    "strict": True,
    "schema": _object(
        {
            "abstained": {"type": "boolean"},
            "edges": _array(
                _object(
                    {
                        "from_entity": _ENTITY,
                        "relationship": {"type": "string", "enum": list(RELATIONSHIPS)},
                        "to_entity": _ENTITY,
                        "mechanism": _STRING,
                        "epistemic_state": {
                            "type": "string",
                            "enum": ["observed", "supported", "hypothesis", "rejected"],
                        },
                        "evidence_ids": _array(_STRING, 4),
                        "confidence": _nullable("number", minimum=0, maximum=1),
                        "missing_evidence": _array(_STRING, 3),
                        "break_conditions": _array(_STRING, 3),
                        "depth": {"type": "integer", "minimum": 1, "maximum": 8},
                        "valid_from": _NULLABLE_STRING,
                        "valid_to": _NULLABLE_STRING,
                    }
                ),
                12,
            ),
        }
    ),
}

VALUE_CAPTURE_SCHEMA = {
    "name": STAGE_VERSIONS["value_capture"],
    "strict": True,
    "schema": _object(
        {
            "abstained": {"type": "boolean"},
            "assessments": _array(
                _object(
                    {
                        "node": _ENTITY,
                        "dimensions": _object(
                            {
                                name: _nullable(
                                    "string",
                                    enum=["low", "moderate", "high", "unknown"],
                                )
                                for name in _VALUE_DIMENSIONS
                            }
                        ),
                        "rationale": _object(
                            {name: _STRING for name in _VALUE_DIMENSIONS}
                        ),
                        "evidence_ids": _array(_STRING, 4),
                        "unknowns": _array(_STRING, 5),
                    }
                ),
                3,
            ),
        }
    ),
}

ADVERSARIAL_SCHEMA = {
    "name": STAGE_VERSIONS["adversarial"],
    "strict": True,
    "schema": _object(
        {
            "abstained": {"type": "boolean"},
            "counterevidence": _array(
                _object(
                    {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "alternative_explanation",
                                "contradicting_evidence",
                                "weak_edge",
                                "assumption",
                                "invalidation",
                            ],
                        },
                        "statement": _STRING,
                        "epistemic_state": {
                            "type": "string",
                            "enum": ["supported", "hypothesis", "rejected"],
                        },
                        "evidence_ids": _EVIDENCE_IDS,
                        "edge_fingerprint": _NULLABLE_STRING,
                        "rationale": _NULLABLE_STRING,
                    }
                ),
                5,
            ),
            "data_requests": _array(
                _object(
                    {
                        "subject": _STRING,
                        "requested_evidence_type": {
                            "type": "string",
                            "enum": list(RESEARCH_DATA_TYPES),
                        },
                        "reason": _STRING,
                        "desired_frequency": {
                            "type": "string",
                            "enum": [
                                "one_off",
                                "event_driven",
                                "daily",
                                "weekly",
                                "monthly",
                                "quarterly",
                                "annual",
                                "unknown",
                            ],
                        },
                        "priority": {
                            "type": "string",
                            "enum": ["low", "moderate", "high"],
                        },
                        "candidate_source_class": {
                            "type": "string",
                            "enum": [
                                "official",
                                "industry",
                                "company",
                                "market",
                                "academic",
                                "other",
                            ],
                        },
                    }
                ),
                3,
            ),
            "invalidation_conditions": _array(_STRING, 3),
            "strengthening_observations": _array(_STRING, 3),
            "weakest_edge_fingerprint": _NULLABLE_STRING,
        }
    ),
}

_BULLET = _object({"text": _STRING, "evidence_ids": _array(_STRING, 6)})
DELIVERABLE_SCHEMA = {
    "name": STAGE_VERSIONS["deliverable"],
    "strict": True,
    "schema": _object(
        {
            "abstained": {"type": "boolean"},
            "what_changed": _BULLET,
            "why_it_matters": _BULLET,
            "transmission": _object(
                {
                    "text": _STRING,
                    "edge_fingerprints": _array(_STRING, 12),
                }
            ),
            "potential_capture": _array(
                _object(
                    {
                        "node_type": {"type": "string", "enum": sorted(ENTITY_TYPES)},
                        "node_key": _STRING,
                        "node_name": _STRING,
                        "text": _STRING,
                        "evidence_ids": _array(_STRING, 4),
                    }
                ),
                4,
            ),
            "evidence_for": _array(_BULLET, 6),
            "evidence_against": _array(_BULLET, 6),
            "weak_links_unknowns": _array(_STRING, 6),
            "what_to_watch": _array(_STRING, 6),
        }
    ),
}

_FACTOR_TRANSMISSION = _object(
    {
        "target": _STRING,
        "direction": {
            "type": "string",
            "enum": ["supportive", "headwind", "mixed", "neutral", "unknown"],
        },
        "mechanism": _STRING,
        "invalidation_conditions": _array(_STRING, 2),
    }
)
MACRO_TRANSMISSION_SCHEMA = {
    "name": STAGE_VERSIONS["macro_transmission"],
    "strict": True,
    "schema": _object(
        {
            "abstained": {"type": "boolean"},
            "factors": _array(
                _object(
                    {
                        "factor_key": _STRING,
                        "factor_label": _STRING,
                        "state": {
                            "type": "string",
                            "enum": ["rising", "falling", "stable", "mixed", "unknown"],
                        },
                        "strength": {
                            "type": "string",
                            "enum": ["low", "moderate", "high", "unknown"],
                        },
                        "horizon": {
                            "type": "string",
                            "enum": [
                                "intraday",
                                "days",
                                "weeks",
                                "months",
                                "multi_year",
                                "unknown",
                            ],
                        },
                        "mechanism": _STRING,
                        "evidence_ids": _array(_STRING, 4),
                        "confidence": _nullable("number", minimum=0, maximum=1),
                        "confidence_rationale": _STRING,
                        "invalidation_conditions": _array(_STRING, 3),
                        "transmissions": _array(_FACTOR_TRANSMISSION, 8),
                    }
                ),
                8,
            ),
        }
    ),
}

STAGE_SCHEMAS = {
    "claim_extraction": CLAIM_SCHEMA,
    "pattern_discovery": PATTERN_SCHEMA,
    "causal_chain": CAUSAL_SCHEMA,
    "value_capture": VALUE_CAPTURE_SCHEMA,
    "adversarial": ADVERSARIAL_SCHEMA,
    "deliverable": DELIVERABLE_SCHEMA,
    "macro_transmission": MACRO_TRANSMISSION_SCHEMA,
}


class ResearchModelValidationError(ValueError):
    pass


class ResearchRunBudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModelStageResult:
    value: Any
    provenance: ModelProvenance
    cost_usd: float
    tokens_input: int
    tokens_output: int
    duration_ms: int


def _uuid_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


class ResearchModelRunner:
    """Run focused strict stages; deterministic validators own persistence eligibility."""

    def __init__(
        self,
        config: AppConfig,
        *,
        correlation_id: str | None,
        session: Any,
        budget_context: BudgetContext | None = None,
        execution_metadata: Mapping[str, Any] | None = None,
    ):
        self.config = config
        self.settings = ResearchSettings.from_config(config.research_intelligence)
        self.correlation_id = correlation_id
        self.session = session
        self.budget_context = budget_context or BudgetContext()
        self.cost_usd = 0.0
        self.execution_metadata = dict(execution_metadata or {})

    def _render_prompt(
        self, stage: str, input_payload: Any
    ) -> tuple[str, dict[str, str]]:
        template, identity = load_prompt_template(self.settings.prompt_templates[stage])
        payload = json.dumps(
            input_payload, sort_keys=True, ensure_ascii=False, default=str
        )
        prompt = template.replace("{{input_json}}", payload).replace(
            "{{relationship_vocabulary}}", json.dumps(RELATIONSHIPS)
        )
        return prompt, identity

    def cache_identity(self, stage: str) -> dict[str, Any]:
        """Identity of every model/prompt choice that can change a result."""
        _, prompt_identity = self._render_prompt(stage, {})
        model_override = self.settings.model_overrides.get(stage)
        return {
            "stage": STAGE_VERSIONS[stage],
            "prompt": dict(prompt_identity),
            "model": resolve_model(
                self.config,
                f"research_{stage}",
                model=model_override,
            ),
            "reasoning_effort": self.settings.reasoning_effort.get(stage),
            "maximum_output_tokens": self.settings.stage_max_output_tokens[stage],
        }

    def _record_attempt(
        self,
        *,
        stage: str,
        attempt_number: int,
        prompt: str,
        result: Mapping[str, Any],
        issues: list[str],
        prompt_identity: Mapping[str, str],
        input_fingerprint: str,
    ) -> str:
        attempt_id = str(uuid4())
        self.session.execute(
            text(
                """
                INSERT INTO generation_attempts (
                    attempt_id, correlation_id, processor, stage,
                    attempt_number, status, prompt_text, raw_response,
                    validation_issues, model_used, tokens_input,
                    tokens_output, cost_usd, duration_ms, request_metadata
                ) VALUES (
                    :attempt_id, :correlation_id, :processor, :stage,
                    :attempt_number, :status, :prompt_text, :raw_response,
                    CAST(:validation_issues AS JSONB), :model_used,
                    :tokens_input, :tokens_output, :cost_usd, :duration_ms,
                    CAST(:request_metadata AS JSONB)
                )
                """
            ),
            {
                "attempt_id": attempt_id,
                "correlation_id": _uuid_or_none(self.correlation_id),
                "processor": f"research_{stage}",
                "stage": STAGE_VERSIONS[stage],
                "attempt_number": attempt_number,
                "status": "validated" if not issues else "validation_failed",
                "prompt_text": prompt,
                "raw_response": result.get("content"),
                "validation_issues": json.dumps(issues[:20]),
                "model_used": result.get("model"),
                "tokens_input": int(result.get("tokens_input") or 0),
                "tokens_output": int(result.get("tokens_output") or 0),
                "cost_usd": float(result.get("cost_usd") or 0),
                "duration_ms": int(result.get("duration_ms") or 0),
                "request_metadata": json.dumps(
                    {
                        **self.execution_metadata,
                        "prompt": dict(prompt_identity),
                        "cache_identity": self.cache_identity(stage),
                        "input_fingerprint": input_fingerprint,
                        "requested_model": result.get("requested_model"),
                        "provider": result.get("provider"),
                        "generation_id": result.get("generation_id"),
                        "tokens_reasoning": result.get("tokens_reasoning"),
                        "tokens_cached": result.get("tokens_cached"),
                        "retry_count": result.get("retry_count"),
                    },
                    sort_keys=True,
                ),
            },
        )
        return attempt_id

    @staticmethod
    def _repair_prompt(prompt: str, invalid: Any, issue: Exception) -> str:
        return (
            "Repair the JSON once. Return only a complete replacement object that "
            "matches the original strict schema. Reduce item counts and shorten text "
            "aggressively so the replacement is complete; explicit unknowns and "
            "abstention are preferable to exhaustive prose. Keep valid evidence IDs "
            "and do not introduce claims, numbers, entities, or evidence.\n"
            f"Validation error: {type(issue).__name__}: {str(issue)[:300]}\n"
            f"Invalid response:\n{str(invalid or '')[:12000]}\n"
            f"Original request:\n{prompt}"
        )

    def run(
        self,
        stage: str,
        input_payload: Any,
        validator: Callable[[Any], T],
        *,
        input_fingerprint: str | None = None,
    ) -> ModelStageResult:
        if stage not in STAGE_SCHEMAS:
            raise ValueError("unknown research model stage")
        if not self.settings.stage_enabled.get(stage, False):
            raise ResearchModelValidationError(f"research stage disabled: {stage}")
        if (
            self.settings.model_budget_usd_per_run <= 0
            or self.cost_usd >= self.settings.model_budget_usd_per_run
        ):
            raise ResearchRunBudgetExceeded("research run model budget exhausted")
        prompt, prompt_identity = self._render_prompt(stage, input_payload)
        fingerprint = input_fingerprint or canonical_fingerprint(
            {
                "stage": STAGE_VERSIONS[stage],
                "input": input_payload,
                "prompt": prompt_identity,
            }
        )
        stage_runner = LLMStage(
            self.config,
            f"research_{stage}",
            correlation_id=self.correlation_id,
            budget_context=self.budget_context,
            response_schema=STAGE_SCHEMAS[stage],
            model=self.settings.model_overrides.get(stage),
            reasoning_effort=self.settings.reasoning_effort.get(stage),
            max_output_tokens=self.settings.stage_max_output_tokens[stage],
        )
        attempts: list[Mapping[str, Any]] = []
        validated_value: T | None = None
        validated_attempt_id: str | None = None
        current_prompt = prompt
        for attempt_number in (1, 2):
            if self.cost_usd >= self.settings.model_budget_usd_per_run:
                raise ResearchRunBudgetExceeded("research run model budget exhausted")
            try:
                result = stage_runner.call(current_prompt)
            except Exception as exc:
                result = {
                    "content": None,
                    "model": stage_runner.policy.model,
                    "tokens_input": 0,
                    "tokens_output": 0,
                    "cost_usd": 0,
                    "duration_ms": 0,
                }
                self._record_attempt(
                    stage=stage,
                    attempt_number=attempt_number,
                    prompt=current_prompt,
                    result=result,
                    issues=[f"request failed: {type(exc).__name__}"],
                    prompt_identity=prompt_identity,
                    input_fingerprint=fingerprint,
                )
                raise
            attempts.append(result)
            self.cost_usd += float(result.get("cost_usd") or 0)
            if self.cost_usd > self.settings.model_budget_usd_per_run:
                self._record_attempt(
                    stage=stage,
                    attempt_number=attempt_number,
                    prompt=current_prompt,
                    result=result,
                    issues=["research run model budget exceeded"],
                    prompt_identity=prompt_identity,
                    input_fingerprint=fingerprint,
                )
                raise ResearchRunBudgetExceeded("research run model budget exhausted")
            try:
                parsed = parse_json_payload(result.get("content"))
                validated_value = validator(parsed)
            except Exception as exc:
                self._record_attempt(
                    stage=stage,
                    attempt_number=attempt_number,
                    prompt=current_prompt,
                    result=result,
                    issues=[f"{type(exc).__name__}: {str(exc)[:300]}"],
                    prompt_identity=prompt_identity,
                    input_fingerprint=fingerprint,
                )
                if attempt_number == 2:
                    raise ResearchModelValidationError(
                        f"{STAGE_VERSIONS[stage]} validation failed"
                    ) from exc
                current_prompt = self._repair_prompt(prompt, result.get("content"), exc)
                continue
            validated_attempt_id = self._record_attempt(
                stage=stage,
                attempt_number=attempt_number,
                prompt=current_prompt,
                result=result,
                issues=[],
                prompt_identity=prompt_identity,
                input_fingerprint=fingerprint,
            )
            break
        if validated_attempt_id is None:
            raise ResearchModelValidationError(
                f"{STAGE_VERSIONS[stage]} did not validate"
            )
        total_cost = sum(float(result.get("cost_usd") or 0) for result in attempts)
        # Actual attempt cost is charged immediately, including invalid attempts.
        final = attempts[-1]
        fingerprint = str(fingerprint)
        return ModelStageResult(
            value=validated_value,
            provenance=ModelProvenance(
                model_slug=str(final.get("model") or stage_runner.policy.model),
                prompt_version=STAGE_VERSIONS[stage],
                generation_attempt_id=validated_attempt_id,
                input_fingerprint=fingerprint,
                metadata={
                    "prompt_identity": dict(prompt_identity),
                    "attempt_count": len(attempts),
                    "repair_required": len(attempts) > 1,
                },
            ),
            cost_usd=total_cost,
            tokens_input=sum(
                int(result.get("tokens_input") or 0) for result in attempts
            ),
            tokens_output=sum(
                int(result.get("tokens_output") or 0) for result in attempts
            ),
            duration_ms=sum(int(result.get("duration_ms") or 0) for result in attempts),
        )


__all__ = [
    "ADVERSARIAL_SCHEMA",
    "CAUSAL_SCHEMA",
    "CLAIM_SCHEMA",
    "DELIVERABLE_SCHEMA",
    "MACRO_TRANSMISSION_SCHEMA",
    "ModelStageResult",
    "PATTERN_SCHEMA",
    "ResearchModelRunner",
    "ResearchModelValidationError",
    "ResearchRunBudgetExceeded",
    "STAGE_SCHEMAS",
    "STAGE_VERSIONS",
    "VALUE_CAPTURE_SCHEMA",
]
