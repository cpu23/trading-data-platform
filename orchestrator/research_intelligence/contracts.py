"""Strict in-memory contracts for evidence-bounded research intelligence."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

_MAX_TITLE = 300
_MAX_EXCERPT = 1_500
_MAX_REFERENCE = 2_048
_MAX_ENTITY = 200
_ID_RE = re.compile(r"^[A-Za-z0-9._:/@+-]{1,240}$")


class EvidenceType(StrEnum):
    MACRO_OBSERVATION = "macro_observation"
    MACRO_RELEASE = "macro_release"
    MARKET_STATE = "market_state"
    OFFICIAL_DOCUMENT = "official_document"
    STORY_CLUSTER = "story_cluster"
    MARKET_CONFIRMATION = "market_confirmation"
    INVESTMENT_OBSERVATION = "investment_observation"
    FILING_DELTA = "filing_delta"
    INVESTMENT_ANALYSIS = "investment_analysis"
    SOURCE_CLAIM = "source_claim"

_DEDICATED_REFERENCE_FIELDS = frozenset(
    {
        "source_evidence_id",
        "evidence_ref",
        "evidence_ids",
        "supporting_evidence_ids",
        "contradicting_evidence_ids",
        "context_evidence_ids",
    }
)
_EMBEDDED_EVIDENCE_REF_RE = re.compile(
    rf"(?<![\w])(?:{'|'.join(re.escape(item.value) for item in EvidenceType)}):"
    r"[^\s\]\[(){}<>\"']+"
)


class EpistemicState(StrEnum):
    OBSERVED = "observed"
    SUPPORTED = "supported"
    HYPOTHESIS = "hypothesis"
    REJECTED = "rejected"


class CaseLifecycle(StrEnum):
    CANDIDATE = "candidate"
    FORMING = "forming"
    CORROBORATED = "corroborated"
    RESEARCH_READY = "research_ready"
    MATURE = "mature"
    WEAKENING = "weakening"
    ARCHIVED = "archived"


class CaseType(StrEnum):
    CYCLICAL = "cyclical"
    STRUCTURAL = "structural"
    EVENT_DRIVEN = "event_driven"
    UNCLEAR = "unclear"


class Horizon(StrEnum):
    INTRADAY = "intraday"
    DAYS = "days"
    WEEKS = "weeks"
    MONTHS = "months"
    MULTI_YEAR = "multi_year"
    UNKNOWN = "unknown"


class DriverDirection(StrEnum):
    SUPPORTIVE = "supportive"
    HEADWIND = "headwind"
    MIXED = "mixed"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"



class FactorState(StrEnum):
    RISING = "rising"
    FALLING = "falling"
    STABLE = "stable"
    MIXED = "mixed"
    UNKNOWN = "unknown"

class Strength(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    UNKNOWN = "unknown"


class EvidenceRelationship(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CONTEXT = "context"
    INVALIDATION = "invalidation"


def _bounded_text(value: Any, maximum: int, *, required: bool = False) -> str | None:
    text_value = " ".join(str(value or "").split())
    if required and not text_value:
        raise ValueError("required text is blank")
    return text_value[:maximum] if text_value else None


def _utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ValueError("timestamp must be datetime or ISO text")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        normalized = _utc(value)
        return normalized.isoformat().replace("+00:00", "Z") if normalized else None
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    return str(value)


def canonical_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        _json_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class NormalizedEntity:
    entity_type: str
    normalized_key: str
    display_name: str

    @classmethod
    def create(cls, entity_type: Any, normalized_key: Any, display_name: Any = None):
        kind = _bounded_text(entity_type, 40, required=True)
        key = _bounded_text(normalized_key, _MAX_ENTITY, required=True)
        if not _ID_RE.fullmatch(key):
            raise ValueError("entity key contains unsupported characters")
        display = _bounded_text(display_name or key, _MAX_ENTITY, required=True)
        return cls(kind.casefold(), key.casefold(), display)

    def to_dict(self) -> dict[str, str]:
        return {
            "entity_type": self.entity_type,
            "normalized_key": self.normalized_key,
            "display_name": self.display_name,
        }


@dataclass(frozen=True, slots=True)
class NormalizedEvidence:
    evidence_type: str
    evidence_id: str
    source_name: str
    source_timestamp: datetime
    available_at: datetime
    availability_basis: str
    acquired_at: datetime | None
    valid_from: datetime | None
    valid_to: datetime | None
    point_in_time_safe: bool
    title: str
    bounded_excerpt: str | None
    source_reference: str | None
    entities: tuple[NormalizedEntity, ...]
    structured_fields: Mapping[str, Any]
    provenance: Mapping[str, Any]
    freshness: str
    content_fingerprint: str

    @classmethod
    def create(
        cls,
        *,
        evidence_type: EvidenceType | str,
        evidence_id: Any,
        evidence_ref: Any = None,
        source_name: Any,
        source_timestamp: datetime | str,
        acquired_at: datetime | str | None = None,
        available_at: datetime | str | None = None,
        availability_basis: Any = "source_timestamp",
        valid_from: datetime | str | None = None,
        valid_to: datetime | str | None = None,
        point_in_time_safe: bool = True,
        title: Any,
        bounded_excerpt: Any = None,
        source_reference: Any = None,
        entities: Sequence[NormalizedEntity] = (),
        structured_fields: Mapping[str, Any] | None = None,
        provenance: Mapping[str, Any] | None = None,
        freshness: Any = "current",
        content_fingerprint: str | None = None,
    ) -> NormalizedEvidence:
        kind = str(evidence_type)
        if kind not in {item.value for item in EvidenceType}:
            raise ValueError("unsupported evidence type")
        identifier = _bounded_text(evidence_id, 240, required=True)
        if not _ID_RE.fullmatch(identifier):
            raise ValueError("evidence id contains unsupported characters")
        expected_ref = f"{kind}:{identifier}"
        if evidence_ref is not None and str(evidence_ref).strip() != expected_ref:
            raise ValueError("evidence ref does not match evidence identity")
        source = _bounded_text(source_name, 120, required=True)
        observed = _utc(source_timestamp)
        if observed is None:
            raise ValueError("source timestamp is required")
        availability = _utc(available_at) or observed
        basis = _bounded_text(availability_basis, 80, required=True)
        starts = _utc(valid_from)
        ends = _utc(valid_to)
        if starts is not None and ends is not None and starts >= ends:
            raise ValueError("evidence validity interval is invalid")
        if not isinstance(point_in_time_safe, bool):
            raise ValueError("point_in_time_safe must be boolean")
        safe_entities = tuple(entities[:50])
        fields = _json_value(dict(structured_fields or {}))
        source_provenance = _json_value(dict(provenance or {}))
        excerpt = _bounded_text(bounded_excerpt, _MAX_EXCERPT)
        reference = _bounded_text(source_reference, _MAX_REFERENCE)
        normalized_title = _bounded_text(title, _MAX_TITLE, required=True)
        fingerprint_payload = {
            "evidence_type": kind,
            "evidence_id": identifier,
            "source_timestamp": observed,
            "available_at": availability,
            "availability_basis": basis,
            "valid_from": starts,
            "valid_to": ends,
            "title": normalized_title,
            "excerpt": excerpt,
            "entities": [entity.to_dict() for entity in safe_entities],
            "structured_fields": fields,
        }
        fingerprint = content_fingerprint or canonical_fingerprint(fingerprint_payload)
        if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise ValueError("content fingerprint must be SHA-256 hex")
        return cls(
            evidence_type=kind,
            evidence_id=identifier,
            source_name=source,
            source_timestamp=observed,
            available_at=availability,
            availability_basis=basis,
            acquired_at=_utc(acquired_at),
            valid_from=starts,
            valid_to=ends,
            point_in_time_safe=point_in_time_safe,
            title=normalized_title,
            bounded_excerpt=excerpt,
            source_reference=reference,
            entities=safe_entities,
            structured_fields=MappingProxyType(fields),
            provenance=MappingProxyType(source_provenance),
            freshness=str(freshness or "unknown")[:40],
            content_fingerprint=fingerprint,
        )

    @property
    def ref(self) -> str:
        return f"{self.evidence_type}:{self.evidence_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_type": self.evidence_type,
            "evidence_id": self.evidence_id,
            "evidence_ref": self.ref,
            "source_name": self.source_name,
            "source_timestamp": self.source_timestamp.isoformat(),
            "available_at": self.available_at.isoformat(),
            "availability_basis": self.availability_basis,
            "acquired_at": self.acquired_at.isoformat() if self.acquired_at else None,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "point_in_time_safe": self.point_in_time_safe,
            "title": self.title,
            "bounded_excerpt": self.bounded_excerpt,
            "source_reference": self.source_reference,
            "entities": [entity.to_dict() for entity in self.entities],
            "structured_fields": dict(self.structured_fields),
            "provenance": dict(self.provenance),
            "freshness": self.freshness,
            "content_fingerprint": self.content_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class CandidateGroup:
    blocking_key: str
    evidence: tuple[NormalizedEvidence, ...]
    entities: tuple[NormalizedEntity, ...]
    industries: tuple[str, ...]
    source_names: tuple[str, ...]
    input_fingerprint: str

    @property
    def evidence_ids(self) -> frozenset[str]:
        return frozenset(item.ref for item in self.evidence)


@dataclass(frozen=True, slots=True)
class ModelProvenance:
    model_slug: str | None = None
    prompt_version: str | None = None
    generation_attempt_id: str | None = None
    input_fingerprint: str | None = None
    confidence_rationale: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CausalEdgeDraft:
    from_type: str
    from_key: str
    from_name: str
    relationship: str
    to_type: str
    to_key: str
    to_name: str
    mechanism: str
    epistemic_state: str
    evidence_ids: tuple[str, ...]
    confidence: float | None
    missing_evidence: tuple[str, ...]
    break_conditions: tuple[str, ...]
    depth: int
    valid_from: datetime | None = None
    valid_to: datetime | None = None


VALUE_CAPTURE_DIMENSIONS = (
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


@dataclass(frozen=True, slots=True)
class ValueCaptureDraft:
    node_type: str
    node_key: str
    node_name: str
    dimensions: Mapping[str, str | None]
    rationale: Mapping[str, str]
    evidence_ids: tuple[str, ...]
    unknowns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FactorTransmissionDraft:
    target: str
    direction: str
    mechanism: str
    invalidation_conditions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EconomicFactorDraft:
    factor_key: str
    factor_label: str
    state: str
    strength: str
    horizon: str
    mechanism: str
    evidence_ids: tuple[str, ...]
    confidence: float | None
    confidence_rationale: str
    invalidation_conditions: tuple[str, ...]
    transmissions: tuple[FactorTransmissionDraft, ...]


@dataclass(frozen=True, slots=True)
class MarketDriverDraft:
    target: str
    driver_key: str
    driver_label: str
    direction: str
    strength: str
    horizon: str
    mechanism: str
    evidence_ids: tuple[str, ...]
    changed_since_prior: bool
    invalidation_conditions: tuple[str, ...]
    confidence: float | None
    confidence_rationale: str


def evidence_catalog(evidence: Sequence[NormalizedEvidence]) -> dict[str, NormalizedEvidence]:
    catalog: dict[str, NormalizedEvidence] = {}
    for item in evidence:
        if item.ref in catalog and catalog[item.ref].content_fingerprint != item.content_fingerprint:
            raise ValueError(f"conflicting evidence identity: {item.ref}")
        catalog[item.ref] = item
    return catalog


def validate_evidence_references(
    references: Sequence[Any], supplied: Mapping[str, NormalizedEvidence]
) -> tuple[str, ...]:
    if isinstance(references, (str, bytes)):
        raise ValueError("evidence references must be an array")
    resolved: list[str] = []
    for raw in references:
        reference = str(raw or "").strip()
        if reference not in supplied:
            raise ValueError(f"unknown evidence id: {reference[:240]}")
        if reference not in resolved:
            resolved.append(reference)
    return tuple(resolved)

def reject_embedded_evidence_references(value: Any) -> None:
    """Require model citations to use validated, dedicated reference fields."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) not in _DEDICATED_REFERENCE_FIELDS:
                reject_embedded_evidence_references(item)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            reject_embedded_evidence_references(item)
        return
    if isinstance(value, str) and _EMBEDDED_EVIDENCE_REF_RE.search(value):
        raise ValueError("evidence references must use dedicated fields")


__all__ = [
    "CandidateGroup",
    "CaseLifecycle",
    "CaseType",
    "CausalEdgeDraft",
    "EconomicFactorDraft",
    "DriverDirection",
    "EpistemicState",
    "EvidenceRelationship",
    "FactorState",
    "FactorTransmissionDraft",
    "EvidenceType",
    "Horizon",
    "MarketDriverDraft",
    "ModelProvenance",
    "NormalizedEntity",
    "NormalizedEvidence",
    "Strength",
    "VALUE_CAPTURE_DIMENSIONS",
    "ValueCaptureDraft",
    "canonical_fingerprint",
    "evidence_catalog",
    "reject_embedded_evidence_references",
    "validate_evidence_references",
]
