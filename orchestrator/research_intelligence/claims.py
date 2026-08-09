"""Validation and immutable persistence for atomic source claims."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from research_intelligence.contracts import (
    ModelProvenance,
    NormalizedEntity,
    NormalizedEvidence,
    canonical_fingerprint,
    evidence_catalog,
)
from research_intelligence.relationships import normalize_entity

CLAIM_KINDS = frozenset({"reported_fact", "company_guidance", "estimate", "opinion"})
DIRECTIONS = frozenset(
    {"increase", "decrease", "higher", "lower", "flat", "mixed", "unknown"}
)
MAX_SOURCE_CLAIMS_PER_BATCH = 8
CLAIM_ELIGIBLE_EVIDENCE_TYPES = frozenset(
    {
        "official_document",
        "story_cluster",
        "investment_observation",
        "filing_delta",
        "investment_analysis",
    }
)
_NUMERIC_TOKEN_RE = re.compile(r"(?<![A-Za-z])[-+]?\d[\d,.]*(?:%|bp|bps)?", re.IGNORECASE)
_REQUIRED_KEYS = frozenset(
    {
        "source_evidence_id",
        "subject",
        "predicate",
        "object_value",
        "unit",
        "period",
        "geography",
        "direction",
        "claim_kind",
        "source_span",
        "confidence",
        "entities",
    }
)


@dataclass(frozen=True, slots=True)
class SourceClaimDraft:
    source_evidence: NormalizedEvidence
    subject: str
    predicate: str
    object_value: str | None
    unit: str | None
    period: str | None
    geography: str | None
    direction: str | None
    claim_kind: str
    source_span: str
    confidence: float
    entities: tuple[NormalizedEntity, ...]
    claim_fingerprint: str

def claim_evidence(
    claims: Sequence[SourceClaimDraft],
    provenance: ModelProvenance,
) -> tuple[NormalizedEvidence, ...]:
    """Project validated claims into the shared evidence contract."""
    items: list[NormalizedEvidence] = []
    for claim in claims:
        source = claim.source_evidence
        title = " ".join(
            part
            for part in (claim.subject, claim.predicate, claim.object_value)
            if part
        )
        items.append(
            NormalizedEvidence.create(
                evidence_type="source_claim",
                evidence_id=claim.claim_fingerprint,
                source_name=source.source_name,
                source_timestamp=source.source_timestamp,
                acquired_at=source.acquired_at,
                available_at=source.available_at,
                availability_basis=f"derived_from_{source.availability_basis}",
                valid_from=source.valid_from,
                valid_to=source.valid_to,
                point_in_time_safe=source.point_in_time_safe,
                title=title,
                bounded_excerpt=claim.source_span,
                source_reference=source.source_reference,
                entities=claim.entities,
                structured_fields={
                    "subject": claim.subject,
                    "predicate": claim.predicate,
                    "object_value": claim.object_value,
                    "unit": claim.unit,
                    "period": claim.period,
                    "geography": claim.geography,
                    "direction": claim.direction,
                    "claim_kind": claim.claim_kind,
                    "confidence": claim.confidence,
                    "source_evidence_id": source.ref,
                },
                provenance={
                    "source_evidence_type": source.evidence_type,
                    "source_evidence_id": source.evidence_id,
                    "model_slug": provenance.model_slug,
                    "prompt_version": provenance.prompt_version,
                    "generation_attempt_id": provenance.generation_attempt_id,
                    "input_fingerprint": provenance.input_fingerprint,
                },
                freshness=source.freshness,
            )
        )
    return tuple(items)


def _text(value: Any, maximum: int, field: str, *, required: bool = False) -> str | None:
    cleaned = " ".join(str(value or "").split())
    if required and not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned[:maximum] if cleaned else None


def _confidence(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("confidence must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError("confidence must be numeric") from None
    if not math.isfinite(parsed) or not 0 <= parsed <= 1:
        raise ValueError("confidence must be between 0 and 1")
    return parsed


def _source_text(evidence: NormalizedEvidence) -> str:
    return " ".join(
        part for part in (evidence.title, evidence.bounded_excerpt or "") if part
    )


def _numeric_tokens(value: str | None) -> set[str]:
    return {match.casefold().replace(",", "") for match in _NUMERIC_TOKEN_RE.findall(value or "")}


def _validate_source_span(span: str, evidence: NormalizedEvidence) -> None:
    normalized_span = " ".join(span.split()).casefold()
    normalized_source = " ".join(_source_text(evidence).split()).casefold()
    if normalized_span not in normalized_source:
        raise ValueError("source span is not present in supplied evidence")


def _validate_numeric_object(object_value: str | None, source_span: str) -> None:
    supplied = _numeric_tokens(source_span)
    for token in _numeric_tokens(object_value):
        if token not in supplied:
            raise ValueError("numeric claim value is absent from exact source span")


def validate_claim_output(
    output: Any,
    supplied_evidence: Sequence[NormalizedEvidence],
    maximum_claims: int = MAX_SOURCE_CLAIMS_PER_BATCH,
) -> tuple[SourceClaimDraft, ...]:
    """Validate one strict claim-extraction response; empty claims are valid."""
    if not isinstance(output, Mapping) or set(output) != {"abstained", "claims"}:
        raise ValueError("claim output must contain exactly abstained and claims")
    if not isinstance(output.get("abstained"), bool):
        raise ValueError("abstained must be boolean")
    raw_claims = output.get("claims")
    if not isinstance(raw_claims, list) or len(raw_claims) > maximum_claims:
        raise ValueError(f"claims must be an array of at most {maximum_claims} items")
    if output["abstained"] and raw_claims:
        raise ValueError("abstained output cannot include claims")
    catalog = evidence_catalog(supplied_evidence)
    drafts: list[SourceClaimDraft] = []
    seen: set[str] = set()
    for raw in raw_claims:
        if not isinstance(raw, Mapping) or set(raw) != _REQUIRED_KEYS:
            raise ValueError("claim keys do not match the strict contract")
        reference = str(raw.get("source_evidence_id") or "").strip()
        evidence = catalog.get(reference)
        if evidence is None:
            raise ValueError(f"unknown evidence id: {reference[:240]}")
        subject = _text(raw.get("subject"), 240, "subject", required=True)
        predicate = _text(raw.get("predicate"), 160, "predicate", required=True)
        object_value = _text(raw.get("object_value"), 240, "object_value")
        unit = _text(raw.get("unit"), 80, "unit")
        period = _text(raw.get("period"), 120, "period")
        geography = _text(raw.get("geography"), 120, "geography")
        direction = _text(raw.get("direction"), 40, "direction")
        if direction is not None and direction.casefold() not in DIRECTIONS:
            raise ValueError("claim direction is invalid")
        claim_kind = str(raw.get("claim_kind") or "").strip().casefold()
        if claim_kind not in CLAIM_KINDS:
            raise ValueError("claim kind is invalid")
        source_span = _text(raw.get("source_span"), 1_500, "source_span", required=True)
        _validate_source_span(source_span, evidence)
        _validate_numeric_object(object_value, source_span)
        if object_value is None and unit is not None:
            raise ValueError("a claim unit requires an explicit object value")
        raw_entities = raw.get("entities")
        if not isinstance(raw_entities, list) or len(raw_entities) > 30:
            raise ValueError("claim entities must be an array of at most 30 items")
        entities: list[NormalizedEntity] = []
        for item in raw_entities:
            if not isinstance(item, Mapping) or set(item) != {"entity_type", "name"}:
                raise ValueError("claim entity keys are invalid")
            entity = normalize_entity(item.get("entity_type"), item.get("name"))
            if entity not in entities:
                entities.append(entity)
        fingerprint = canonical_fingerprint(
            {
                "source": reference,
                "subject": subject.casefold(),
                "predicate": predicate.casefold(),
                "object_value": object_value,
                "unit": unit,
                "period": period,
                "geography": geography,
                "direction": direction,
                "claim_kind": claim_kind,
                "source_span": source_span,
            }
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        drafts.append(
            SourceClaimDraft(
                source_evidence=evidence,
                subject=subject,
                predicate=predicate,
                object_value=object_value,
                unit=unit,
                period=period,
                geography=geography,
                direction=direction.casefold() if direction else None,
                claim_kind=claim_kind,
                source_span=source_span,
                confidence=_confidence(raw.get("confidence")),
                entities=tuple(entities),
                claim_fingerprint=fingerprint,
            )
        )
    return tuple(drafts)


def persist_source_claims(
    session: Any,
    claims: Sequence[SourceClaimDraft],
    provenance: ModelProvenance,
) -> int:
    """Insert immutable claims idempotently without taking caller transaction ownership."""
    inserted = 0
    for claim in claims[:50]:
        result = session.execute(
            text(
                """
                INSERT INTO research_source_claims (
                    evidence_type, evidence_id, claim_fingerprint, subject,
                    predicate, object_value, unit, period, geography, direction,
                    claim_kind, source_span, observed_at, confidence, entities,
                    model_slug, prompt_version, generation_attempt_id,
                    input_fingerprint, provenance
                ) VALUES (
                    :evidence_type, :evidence_id, :claim_fingerprint, :subject,
                    :predicate, :object_value, :unit, :period, :geography,
                    :direction, :claim_kind, :source_span, :observed_at,
                    :confidence, CAST(:entities AS JSONB), :model_slug,
                    :prompt_version, :generation_attempt_id, :input_fingerprint,
                    CAST(:provenance AS JSONB)
                ) ON CONFLICT DO NOTHING
                """
            ),
            {
                "evidence_type": claim.source_evidence.evidence_type,
                "evidence_id": claim.source_evidence.evidence_id,
                "claim_fingerprint": claim.claim_fingerprint,
                "subject": claim.subject,
                "predicate": claim.predicate,
                "object_value": claim.object_value,
                "unit": claim.unit,
                "period": claim.period,
                "geography": claim.geography,
                "direction": claim.direction,
                "claim_kind": claim.claim_kind,
                "source_span": claim.source_span,
                "observed_at": claim.source_evidence.source_timestamp,
                "confidence": claim.confidence,
                "entities": json.dumps([entity.to_dict() for entity in claim.entities]),
                "model_slug": provenance.model_slug,
                "prompt_version": provenance.prompt_version or "research_claim_extraction_v2",
                "generation_attempt_id": provenance.generation_attempt_id,
                "input_fingerprint": provenance.input_fingerprint or claim.source_evidence.content_fingerprint,
                "provenance": json.dumps(
                    {
                        "source_reference": claim.source_evidence.source_reference,
                        "confidence_rationale": provenance.confidence_rationale,
                        **dict(provenance.metadata),
                    },
                    sort_keys=True,
                ),
            },
        )
        inserted += int(getattr(result, "rowcount", 0) or 0)
    return inserted


__all__ = [
    "CLAIM_ELIGIBLE_EVIDENCE_TYPES",
    "CLAIM_KINDS",
    "DIRECTIONS",
    "SourceClaimDraft",
    "claim_evidence",
    "persist_source_claims",
    "validate_claim_output",
]
