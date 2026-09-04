"""Counter-thesis validation and structured cold-data requests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from processors._validators import scan_prohibited_language

from research_intelligence.contracts import (
    EpistemicState,
    NormalizedEvidence,
    canonical_fingerprint,
    evidence_catalog,
    reject_embedded_evidence_references,
    validate_evidence_references,
)
from research_intelligence.discovery import reject_unsupported_numeric_text

RESEARCH_DATA_TYPES = (
    "academic_research",
    "capital_expenditure",
    "capacity_utilization",
    "company_disclosure",
    "company_financials",
    "company_guidance",
    "independent_forecast",
    "industry_capacity",
    "industry_contracts",
    "industry_demand",
    "industry_pricing",
    "inventory",
    "lead_times",
    "market_data",
    "market_share",
    "official_statistics",
    "orders_backlog",
    "regulatory_data",
    "supply_chain",
    "unit_economics",
)

_COUNTER_KINDS = frozenset(
    {
        "alternative_explanation",
        "contradicting_evidence",
        "weak_edge",
        "assumption",
        "invalidation",
    }
)
_PRIORITIES = frozenset({"low", "moderate", "high"})
_SOURCE_CLASSES = frozenset(
    {"official", "industry", "company", "market", "academic", "other"}
)
_FREQUENCIES = frozenset(
    {
        "one_off",
        "event_driven",
        "daily",
        "weekly",
        "monthly",
        "quarterly",
        "annual",
        "unknown",
    }
)
_RESEARCH_DATA_TYPE_SET = frozenset(RESEARCH_DATA_TYPES)
_COUNTER_KEYS = frozenset(
    {
        "kind",
        "statement",
        "epistemic_state",
        "evidence_ids",
        "edge_fingerprint",
        "rationale",
    }
)
_REQUEST_KEYS = frozenset(
    {
        "subject",
        "requested_evidence_type",
        "reason",
        "desired_frequency",
        "priority",
        "candidate_source_class",
    }
)
_OUTPUT_KEYS = frozenset(
    {
        "abstained",
        "counterevidence",
        "data_requests",
        "invalidation_conditions",
        "strengthening_observations",
        "weakest_edge_fingerprint",
    }
)


@dataclass(frozen=True, slots=True)
class CounterEvidenceDraft:
    kind: str
    statement: str
    epistemic_state: str
    evidence_ids: tuple[str, ...]
    edge_fingerprint: str | None
    rationale: str | None
    counter_fingerprint: str


@dataclass(frozen=True, slots=True)
class DataRequestDraft:
    subject: str
    requested_evidence_type: str
    reason: str
    desired_frequency: str
    priority: str
    candidate_source_class: str
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class AdversarialAssessment:
    counterevidence: tuple[CounterEvidenceDraft, ...]
    data_requests: tuple[DataRequestDraft, ...]
    invalidation_conditions: tuple[str, ...]
    strengthening_observations: tuple[str, ...]
    weakest_edge_fingerprint: str | None


def _text(value: Any, maximum: int, field: str, required: bool = False) -> str | None:
    cleaned = " ".join(str(value or "").split())
    if required and not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned[:maximum] if cleaned else None


def _strings(value: Any, maximum: int, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{field} exceeds configured bound")
    output: list[str] = []
    for item in value:
        cleaned = _text(item, 500, field, required=True)
        if cleaned not in output:
            output.append(cleaned)
    return tuple(output)


def validate_adversarial_output(
    output: Any,
    evidence: Sequence[NormalizedEvidence],
    *,
    edge_fingerprints: Sequence[str] = (),
    maximum_counterevidence: int = 30,
    maximum_requests: int = 20,
) -> AdversarialAssessment | None:
    if not isinstance(output, Mapping) or set(output) != _OUTPUT_KEYS:
        raise ValueError("adversarial output keys do not match the strict contract")
    reject_embedded_evidence_references(output)
    if not isinstance(output.get("abstained"), bool):
        raise ValueError("adversarial abstained flag must be boolean")
    raw_counters = output.get("counterevidence")
    raw_requests = output.get("data_requests")
    if (
        not isinstance(raw_counters, list)
        or len(raw_counters) > maximum_counterevidence
    ):
        raise ValueError("counterevidence exceeds configured bound")
    if not isinstance(raw_requests, list) or len(raw_requests) > maximum_requests:
        raise ValueError("data requests exceed configured bound")
    if output["abstained"]:
        if raw_counters or raw_requests:
            raise ValueError("abstained adversarial output cannot include objects")
        return None
    catalog = evidence_catalog(evidence)
    allowed_edges = set(edge_fingerprints)
    counters: list[CounterEvidenceDraft] = []
    for raw in raw_counters:
        if not isinstance(raw, Mapping) or set(raw) != _COUNTER_KEYS:
            raise ValueError("counterevidence keys are invalid")
        kind = str(raw.get("kind") or "").strip().casefold()
        if kind not in _COUNTER_KINDS:
            raise ValueError("counterevidence kind is invalid")
        state = str(raw.get("epistemic_state") or "").strip().casefold()
        if state not in {
            EpistemicState.SUPPORTED.value,
            EpistemicState.HYPOTHESIS.value,
            EpistemicState.REJECTED.value,
        }:
            raise ValueError("counterevidence epistemic state is invalid")
        references = validate_evidence_references(raw.get("evidence_ids"), catalog)
        if state == EpistemicState.SUPPORTED.value and not references:
            raise ValueError("supported counterevidence requires evidence")
        edge_fingerprint = _text(raw.get("edge_fingerprint"), 64, "edge fingerprint")
        if edge_fingerprint and edge_fingerprint not in allowed_edges:
            raise ValueError("counterevidence references an unknown edge")
        statement = _text(
            raw.get("statement"), 1_000, "counter statement", required=True
        )
        rationale = _text(raw.get("rationale"), 800, "counter rationale")
        fingerprint = canonical_fingerprint(
            {
                "kind": kind,
                "statement": statement.casefold(),
                "state": state,
                "edge": edge_fingerprint,
            }
        )
        counters.append(
            CounterEvidenceDraft(
                kind=kind,
                statement=statement,
                epistemic_state=state,
                evidence_ids=references,
                edge_fingerprint=edge_fingerprint,
                rationale=rationale,
                counter_fingerprint=fingerprint,
            )
        )
    requests: list[DataRequestDraft] = []
    for raw in raw_requests:
        if not isinstance(raw, Mapping) or set(raw) != _REQUEST_KEYS:
            raise ValueError("data request keys are invalid")
        subject = _text(raw.get("subject"), 240, "request subject", required=True)
        evidence_type = str(raw.get("requested_evidence_type") or "").strip().casefold()
        if evidence_type not in _RESEARCH_DATA_TYPE_SET:
            raise ValueError("requested evidence type is invalid")
        reason = _text(raw.get("reason"), 800, "request reason", required=True)
        frequency = str(raw.get("desired_frequency") or "unknown").strip().casefold()
        if frequency not in _FREQUENCIES:
            raise ValueError("requested frequency is invalid")
        priority = str(raw.get("priority") or "").strip().casefold()
        if priority not in _PRIORITIES:
            raise ValueError("request priority is invalid")
        source_class = str(raw.get("candidate_source_class") or "").strip().casefold()
        if source_class not in _SOURCE_CLASSES:
            raise ValueError("candidate source class is invalid")
        fingerprint = canonical_fingerprint(
            {
                "subject": subject.casefold(),
                "requested_evidence_type": evidence_type,
                "reason": reason.casefold(),
            }
        )
        requests.append(
            DataRequestDraft(
                subject=subject,
                requested_evidence_type=evidence_type,
                reason=reason,
                desired_frequency=frequency,
                priority=priority,
                candidate_source_class=source_class,
                request_fingerprint=fingerprint,
            )
        )
    invalidation = _strings(
        output.get("invalidation_conditions"), 30, "invalidation conditions"
    )
    strengthening = _strings(
        output.get("strengthening_observations"), 30, "strengthening observations"
    )
    weakest = _text(
        output.get("weakest_edge_fingerprint"), 64, "weakest edge fingerprint"
    )
    if weakest and weakest not in allowed_edges:
        raise ValueError("weakest edge fingerprint is unknown")
    if scan_prohibited_language(output):
        raise ValueError("adversarial output contains prohibited advisory language")
    reject_unsupported_numeric_text(
        {
            "counterevidence": [
                {"statement": item.statement, "rationale": item.rationale}
                for item in counters
            ],
            "data_requests": [
                {"subject": item.subject, "reason": item.reason} for item in requests
            ],
            "invalidation_conditions": invalidation,
            "strengthening_observations": strengthening,
        },
        evidence,
    )
    return AdversarialAssessment(
        counterevidence=tuple(counters),
        data_requests=tuple(requests),
        invalidation_conditions=invalidation,
        strengthening_observations=strengthening,
        weakest_edge_fingerprint=weakest,
    )


__all__ = [
    "AdversarialAssessment",
    "CounterEvidenceDraft",
    "DataRequestDraft",
    "validate_adversarial_output",
]
