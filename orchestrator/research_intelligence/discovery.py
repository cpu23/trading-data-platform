"""Deterministic candidate blocking, model-output validation and case matching."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from processors._validators import scan_prohibited_language

from research_intelligence.config import ResearchSettings
from research_intelligence.contracts import (
    CandidateGroup,
    CaseType,
    Horizon,
    NormalizedEntity,
    NormalizedEvidence,
    canonical_fingerprint,
    evidence_catalog,
    reject_embedded_evidence_references,
    validate_evidence_references,
)
from research_intelligence.relationships import normalize_entity

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_NUMERIC_RE = re.compile(r"(?<![A-Za-z])[-+]?\d[\d,.]*(?:%|bp|bps)?", re.IGNORECASE)
_STOPWORDS = frozenset(
    {
        "about",
        "after",
        "against",
        "again",
        "also",
        "among",
        "been",
        "because",
        "being",
        "both",
        "before",
        "between",
        "could",
        "from",
        "does",
        "during",
        "each",
        "following",
        "have",
        "here",
        "however",
        "into",
        "market",
        "itself",
        "just",
        "many",
        "markets",
        "more",
        "most",
        "much",
        "only",
        "other",
        "over",
        "said",
        "says",
        "source",
        "same",
        "some",
        "such",
        "than",
        "that",
        "their",
        "them",
        "then",
        "they",
        "there",
        "these",
        "this",
        "through",
        "very",
        "what",
        "when",
        "where",
        "which",
        "while",
        "were",
        "under",
        "will",
        "with",
        "would",
    }
)
_BLOCKING_STOPWORDS = frozenset(
    {
        "actual",
        "analysis",
        "annual",
        "applicable",
        "assess",
        "available",
        "april",
        "august",
        "bank",
        "billion",
        "business",
        "basis",
        "company",
        "companies",
        "consensus",
        "contains",
        "change",
        "changes",
        "comes",
        "current",
        "daily",
        "data",
        "date",
        "december",
        "deterministic",
        "earnings",
        "evidence",
        "excerpt",
        "equities",
        "equity",
        "february",
        "first",
        "filing",
        "financial",
        "full",
        "form",
        "high",
        "index",
        "information",
        "include",
        "includes",
        "including",
        "initial",
        "insufficient",
        "investment",
        "latest",
        "january",
        "july",
        "june",
        "meanwhile",
        "month",
        "monthly",
        "march",
        "may",
        "million",
        "narrative",
        "operating",
        "qualitative",
        "performance",
        "previous",
        "provided",
        "november",
        "october",
        "quarter",
        "quarterly",
        "record",
        "release",
        "report",
        "reported",
        "relevant",
        "remains",
        "reporting",
        "results",
        "september",
        "risks",
        "securities",
        "share",
        "since",
        "state",
        "thesis",
        "strategy",
        "stock",
        "stocks",
        "support",
        "supports",
        "total",
        "treasury",
        "trading",
        "uncertain",
        "unknown",
        "unclassified",
        "week",
        "weekly",
        "year",
        "yearly",
    }
)
_NON_SPECIFIC_ENTITY_KEYS = frozenset({"n-a", "other", "unclassified", "unknown"})
_IMPORTANCE_DIMENSIONS = (
    "economic_significance",
    "market_sensitivity",
    "persistence",
    "breadth",
    "investability",
    "evidence_strength",
    "time_sensitivity",
)
_ECONOMIC_PROPOSITION_TERMS = frozenset(
    {
        "backlog",
        "capacity",
        "capex",
        "cost",
        "costs",
        "credit",
        "demand",
        "employment",
        "growth",
        "inflation",
        "inventory",
        "labour",
        "liquidity",
        "margin",
        "margins",
        "orders",
        "policy",
        "price",
        "prices",
        "pricing",
        "production",
        "revenue",
        "revenues",
        "scarcity",
        "spending",
        "supply",
        "yield",
        "yields",
    }
)
_PROPOSITION_CHANGE_TERMS = frozenset(
    {
        "accelerate",
        "accelerates",
        "accelerating",
        "bottleneck",
        "bottlenecks",
        "compress",
        "compresses",
        "compressing",
        "constraint",
        "constraints",
        "constrain",
        "constrained",
        "constrains",
        "decline",
        "declines",
        "declining",
        "decrease",
        "decreases",
        "decreasing",
        "expand",
        "expands",
        "expanding",
        "fall",
        "falling",
        "falls",
        "improve",
        "improves",
        "improving",
        "increase",
        "increases",
        "increasing",
        "outpace",
        "outpaces",
        "pressure",
        "pressures",
        "pressuring",
        "prioritize",
        "prioritizes",
        "recovery",
        "reduce",
        "reduces",
        "reducing",
        "rise",
        "rises",
        "rising",
        "scarcity",
        "shift",
        "shifts",
        "shifting",
        "shortage",
        "shortages",
        "slow",
        "slowing",
        "soften",
        "softens",
        "tighten",
        "tightening",
        "weaken",
        "weakens",
        "weakening",
    }
)
_PATTERN_KEYS = frozenset(
    {
        "abstained",
        "coherent",
        "label",
        "definition",
        "case_type",
        "horizon",
        "what_changed",
        "supporting_evidence_ids",
        "contradicting_evidence_ids",
        "context_evidence_ids",
        "entities",
        "industries",
        "macro_drivers",
        "missing_information",
        "importance",
        "importance_rationale",
        "aliases",
    }
)


@dataclass(frozen=True, slots=True)
class PatternAssessment:
    label: str
    definition: str
    case_type: str
    horizon: str
    what_changed: str
    supporting_evidence_ids: tuple[str, ...]
    contradicting_evidence_ids: tuple[str, ...]
    context_evidence_ids: tuple[str, ...]
    entities: tuple[NormalizedEntity, ...]
    industries: tuple[str, ...]
    macro_drivers: tuple[str, ...]
    missing_information: tuple[str, ...]
    importance: Mapping[str, str | None]
    importance_rationale: Mapping[str, str]
    aliases: tuple[str, ...]
    semantic_fingerprint: str
    case_is_economic_proposition: bool
    proposition_rationale: str


def _tokens(value: Any) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return tuple(
        token
        for token in _TOKEN_RE.findall(normalized)
        if len(token) >= 4 and token not in _STOPWORDS and not token.isdigit()
    )


def token_similarity(left: Any, right: Any) -> float:
    left_tokens, right_tokens = set(_tokens(left)), set(_tokens(right))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def normalize_case_label(value: Any) -> str:
    return "-".join(_tokens(value))[:200]


def semantic_case_fingerprint(
    label: Any,
    entities: Sequence[NormalizedEntity],
    industries: Sequence[str],
) -> str:
    normalized_label = normalize_case_label(label)
    if not normalized_label:
        raise ValueError("case label has no meaningful terms")
    entity_keys = sorted(
        f"{item.entity_type}:{item.normalized_key}" for item in entities
    )
    industry_keys = sorted(normalize_case_label(item) for item in industries if item)
    return canonical_fingerprint(
        {
            "label_terms": sorted(set(normalized_label.split("-")))[:12],
            "entities": entity_keys[:20],
            "industries": industry_keys[:10],
        }
    )


def assess_economic_proposition(
    label: Any,
    definition: Any,
    what_changed: Any,
) -> tuple[bool, str]:
    """Reject topic labels that do not make a falsifiable economic-state claim."""
    label_tokens = set(_tokens(label))
    combined_tokens = set(_tokens(f"{label} {definition} {what_changed}"))
    economic_terms = sorted(combined_tokens & _ECONOMIC_PROPOSITION_TERMS)
    change_terms = sorted(combined_tokens & _PROPOSITION_CHANGE_TERMS)
    label_change_terms = sorted(label_tokens & _PROPOSITION_CHANGE_TERMS)
    accepted = bool(economic_terms and change_terms and label_change_terms)
    if accepted:
        rationale = (
            f"The case label states a changing economic condition ({label_change_terms[0]}) "
            f"and the supplied proposition concerns {economic_terms[0]}."
        )
        return True, rationale
    missing = []
    if not economic_terms:
        missing.append("economic variable")
    if not change_terms:
        missing.append("directional or constraining change")
    if not label_change_terms:
        missing.append("change stated in the case label")
    return False, f"Topic-only candidate lacks {', '.join(missing)}."


def _source_names(item: NormalizedEvidence) -> set[str]:
    names = {item.source_name.casefold()}
    provenance_names = item.provenance.get("source_names")
    if isinstance(provenance_names, list):
        names.update(
            str(name).strip().casefold()
            for name in provenance_names
            if str(name).strip()
        )
    return names


def _evidence_origin(item: NormalizedEvidence) -> str:
    if item.evidence_type == "source_claim":
        origin = item.structured_fields.get("source_evidence_id")
        if isinstance(origin, str) and origin.strip():
            return origin.strip()
    return item.ref


def _story_block_key(item: NormalizedEvidence) -> str | None:
    story_id: Any = None
    if item.evidence_type == "story_cluster":
        story_id = item.evidence_id
    elif item.evidence_type == "market_confirmation":
        story_id = item.structured_fields.get("cluster_id")
    elif item.evidence_type == "source_claim":
        origin = _evidence_origin(item)
        if origin.startswith("story_cluster:"):
            story_id = origin.partition(":")[2]
    normalized = str(story_id or "").strip().casefold()
    return f"story:{normalized[:200]}" if normalized else None


def _industry_names(item: NormalizedEvidence) -> set[str]:
    names = {
        entity.display_name
        for entity in item.entities
        if entity.entity_type == "industry"
    }
    structured = item.structured_fields
    if isinstance(structured.get("industry"), str):
        names.add(structured["industry"])
    return names


def _candidate_terms(evidence: Sequence[NormalizedEvidence]) -> set[str]:
    terms_by_origin: dict[str, set[str]] = defaultdict(set)
    for item in evidence:
        terms_by_origin[_evidence_origin(item)].update(
            token
            for token in _tokens(f"{item.title} {item.bounded_excerpt or ''}")
            if token not in _BLOCKING_STOPWORDS
        )
    document_frequency: Counter[str] = Counter()
    for values in terms_by_origin.values():
        document_frequency.update(values)
    return {token for token, count in document_frequency.most_common(120) if count >= 2}


def _phrases(value: Any) -> tuple[str, ...]:
    tokens = tuple(
        token for token in _tokens(value) if token not in _BLOCKING_STOPWORDS
    )
    return tuple(
        f"{left}-{right}"
        for left, right in zip(tokens, tokens[1:], strict=False)
        if left != right
    )


def _candidate_phrases(evidence: Sequence[NormalizedEvidence]) -> set[str]:
    phrases_by_origin: dict[str, set[str]] = defaultdict(set)
    for item in evidence:
        values = set(_phrases(item.title))
        values.update(_phrases(item.bounded_excerpt))
        phrases_by_origin[_evidence_origin(item)].update(values)
    document_frequency: Counter[str] = Counter()
    for values in phrases_by_origin.values():
        document_frequency.update(values)
    return {
        phrase for phrase, count in document_frequency.most_common(120) if count >= 2
    }


def build_candidate_groups(
    evidence: Sequence[NormalizedEvidence],
    settings: ResearchSettings,
    *,
    maximum_groups: int | None = None,
) -> tuple[CandidateGroup, ...]:
    """Block a bounded evidence window before any model assessment."""
    bounded = tuple(evidence[: settings.maximum_candidate_evidence])
    frequent_terms = _candidate_terms(bounded)
    frequent_phrases = _candidate_phrases(bounded)
    blocks: dict[str, dict[str, NormalizedEvidence]] = defaultdict(dict)
    for item in bounded:
        keys: set[str] = set()
        if story_key := _story_block_key(item):
            keys.add(story_key)
        for entity in item.entities:
            if (
                entity.entity_type
                in {
                    "company",
                    "industry",
                    "product",
                    "technology",
                    "commodity",
                    "concept",
                }
                and entity.normalized_key not in _NON_SPECIFIC_ENTITY_KEYS
            ):
                keys.add(f"entity:{entity.entity_type}:{entity.normalized_key}")
        keys.update(
            f"term:{token}"
            for token in set(_tokens(f"{item.title} {item.bounded_excerpt or ''}"))
            if token in frequent_terms
        )
        phrases = set(_phrases(item.title))
        phrases.update(_phrases(item.bounded_excerpt))
        keys.update(
            f"phrase:{phrase}" for phrase in phrases if phrase in frequent_phrases
        )
        for industry in _industry_names(item):
            normalized = normalize_case_label(industry)
            if normalized and normalized not in _NON_SPECIFIC_ENTITY_KEYS:
                keys.add(f"industry:{normalized}")
        for key in sorted(keys)[:20]:
            origin = _evidence_origin(item)
            current = blocks[key].get(origin)
            if current is None or (
                current.evidence_type == "source_claim"
                and item.evidence_type != "source_claim"
            ):
                blocks[key][origin] = item
    candidates: list[CandidateGroup] = []
    for key, item_map in blocks.items():
        items = sorted(
            item_map.values(),
            key=lambda item: (item.source_timestamp, item.ref),
            reverse=True,
        )[: settings.evidence_per_candidate]
        if len(items) < settings.minimum_evidence_count:
            continue
        sources = sorted({name for item in items for name in _source_names(item)})
        if len(sources) < settings.minimum_source_diversity:
            continue
        entities: list[NormalizedEntity] = []
        for item in items:
            for entity in item.entities:
                if entity not in entities:
                    entities.append(entity)
        industries = sorted({name for item in items for name in _industry_names(item)})
        candidates.append(
            CandidateGroup(
                blocking_key=key,
                evidence=tuple(items),
                entities=tuple(entities[:50]),
                industries=tuple(industries[:20]),
                source_names=tuple(sources[:20]),
                input_fingerprint=canonical_fingerprint(
                    {
                        "blocking_key": key,
                        "evidence": [item.content_fingerprint for item in items],
                    }
                ),
            )
        )
    cap = maximum_groups or settings.maximum_cases_per_run * 3

    def rank(group: CandidateGroup) -> tuple[Any, ...]:
        key_type = group.blocking_key.partition(":")[0]
        kind_rank = {
            "story": 5,
            "entity": 4,
            "industry": 3,
            "phrase": 2,
            "term": 1,
        }.get(key_type, 0)
        size_rank = (
            len(group.evidence)
            if key_type not in {"term", "phrase"}
            else -len(group.evidence)
        )
        return (
            kind_rank,
            len(group.source_names),
            size_rank,
            max(item.source_timestamp for item in group.evidence),
            group.blocking_key,
        )

    candidates.sort(key=rank, reverse=True)
    selected: list[CandidateGroup] = []
    covered: list[frozenset[str]] = []
    for group in candidates:
        identity = frozenset(_evidence_origin(item) for item in group.evidence)
        if any(identity <= prior for prior in covered):
            continue
        selected.append(group)
        covered.append(identity)
    return tuple(selected[: max(1, min(cap, 300))])


def candidate_metrics(
    group: CandidateGroup, now: datetime | None = None
) -> dict[str, Any]:
    effective_now = now or datetime.now(UTC)
    if effective_now.tzinfo is None:
        effective_now = effective_now.replace(tzinfo=UTC)
    timestamps = sorted(item.source_timestamp for item in group.evidence)
    midpoint = timestamps[0] + (timestamps[-1] - timestamps[0]) / 2
    earlier = sum(1 for value in timestamps if value < midpoint)
    later = len(timestamps) - earlier
    return {
        "evidence_count": len(group.evidence),
        "source_diversity": len(group.source_names),
        "first_seen_at": timestamps[0].isoformat(),
        "last_seen_at": timestamps[-1].isoformat(),
        "mention_acceleration": later - earlier,
        "age_days": max(0, (effective_now - timestamps[0]).days),
    }


def _text(value: Any, maximum: int, field: str, required: bool = False) -> str | None:
    cleaned = " ".join(str(value or "").split())
    if required and not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned[:maximum] if cleaned else None


def _strings(
    value: Any, maximum: int, field: str, entry_limit: int = 300
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{field} must be an array of at most {maximum} items")
    output: list[str] = []
    for item in value:
        cleaned = _text(item, entry_limit, field, required=True)
        if cleaned not in output:
            output.append(cleaned)
    return tuple(output)


def _normalize_numeric_token(value: Any) -> str | None:
    raw = re.sub(r"(?:%|bps?)$", "", str(value).strip(), flags=re.IGNORECASE)
    raw = raw.replace(",", "")
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    if number == 0:
        return "0"
    return format(number.normalize(), "f")


def _numeric_tokens(value: Any) -> set[str]:
    if isinstance(value, bool) or value is None:
        return set()
    if isinstance(value, (int, float, Decimal)):
        token = _normalize_numeric_token(value)
        return {token} if token is not None else set()
    if isinstance(value, str):
        return {
            token
            for match in _NUMERIC_RE.findall(value)
            if (token := _normalize_numeric_token(match)) is not None
        }
    if isinstance(value, Mapping):
        output: set[str] = set()
        for item in value.values():
            output.update(_numeric_tokens(item))
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        output = set()
        for item in value:
            output.update(_numeric_tokens(item))
        return output
    return set()


def reject_unsupported_numeric_text(
    value: Any, evidence: Sequence[NormalizedEvidence]
) -> None:
    supplied = _numeric_tokens([item.to_dict() for item in evidence])
    unsupported = _numeric_tokens(value) - supplied
    if unsupported:
        raise ValueError(f"unsupported numeric model claim: {sorted(unsupported)[0]}")


def _validate_importance(value: Any) -> tuple[dict[str, str | None], dict[str, str]]:
    if not isinstance(value, Mapping) or set(value) != set(_IMPORTANCE_DIMENSIONS):
        raise ValueError("importance dimensions do not match the strict contract")
    result: dict[str, str | None] = {}
    for dimension in _IMPORTANCE_DIMENSIONS:
        raw = value.get(dimension)
        if raw is None:
            result[dimension] = None
            continue
        normalized = str(raw).strip().casefold()
        if normalized not in {"low", "moderate", "high"}:
            raise ValueError(f"importance dimension {dimension} is invalid")
        result[dimension] = normalized
    return result, {}


def validate_pattern_output(
    output: Any, group: CandidateGroup
) -> PatternAssessment | None:
    if not isinstance(output, Mapping) or set(output) != _PATTERN_KEYS:
        raise ValueError("pattern output keys do not match the strict contract")
    reject_embedded_evidence_references(output)
    if not isinstance(output.get("abstained"), bool) or not isinstance(
        output.get("coherent"), bool
    ):
        raise ValueError("pattern flags must be boolean")
    if output["abstained"] or not output["coherent"]:
        return None
    catalog = evidence_catalog(group.evidence)
    supporting = validate_evidence_references(
        output.get("supporting_evidence_ids"), catalog
    )
    contradicting = validate_evidence_references(
        output.get("contradicting_evidence_ids"), catalog
    )
    context = validate_evidence_references(output.get("context_evidence_ids"), catalog)
    if not supporting:
        raise ValueError("a coherent pattern requires supporting evidence")
    label = _text(output.get("label"), 160, "label", required=True)
    definition = _text(output.get("definition"), 1_000, "definition", required=True)
    what_changed = _text(
        output.get("what_changed"), 1_000, "what_changed", required=True
    )
    case_type = str(output.get("case_type") or "").strip().casefold()
    if case_type not in {item.value for item in CaseType}:
        raise ValueError("case type is invalid")
    horizon = str(output.get("horizon") or "").strip().casefold()
    if horizon not in {item.value for item in Horizon}:
        raise ValueError("case horizon is invalid")
    raw_entities = output.get("entities")
    if not isinstance(raw_entities, list) or len(raw_entities) > 50:
        raise ValueError("pattern entities must be an array of at most 50 items")
    entities: list[NormalizedEntity] = []
    supplied_entity_keys = {
        (entity.entity_type, entity.normalized_key) for entity in group.entities
    }
    for raw in raw_entities:
        if not isinstance(raw, Mapping) or set(raw) != {"entity_type", "name"}:
            raise ValueError("pattern entity keys are invalid")
        entity = normalize_entity(raw.get("entity_type"), raw.get("name"))
        if (
            supplied_entity_keys
            and (entity.entity_type, entity.normalized_key) not in supplied_entity_keys
        ):
            raise ValueError(f"unsupported entity invention: {entity.display_name}")
        if entity not in entities:
            entities.append(entity)
    industries = _strings(output.get("industries"), 20, "industries", 160)
    supplied_industries = {normalize_case_label(item) for item in group.industries}
    for industry in industries:
        if (
            supplied_industries
            and normalize_case_label(industry) not in supplied_industries
        ):
            raise ValueError(f"unsupported industry invention: {industry}")
    macro_drivers = _strings(output.get("macro_drivers"), 20, "macro_drivers")
    missing = _strings(output.get("missing_information"), 30, "missing_information")
    aliases = _strings(output.get("aliases"), 20, "aliases", 160)
    importance, _ = _validate_importance(output.get("importance"))
    rationale_raw = output.get("importance_rationale")
    if not isinstance(rationale_raw, Mapping) or set(rationale_raw) != set(
        _IMPORTANCE_DIMENSIONS
    ):
        raise ValueError("importance rationale does not match the strict contract")
    rationale = {
        key: _text(rationale_raw.get(key), 400, f"importance_rationale.{key}") or ""
        for key in _IMPORTANCE_DIMENSIONS
    }
    policy_findings = scan_prohibited_language(output)
    if policy_findings:
        raise ValueError(
            f"research output violates advisory policy: {policy_findings[0]}"
        )
    reject_unsupported_numeric_text(
        {
            "label": label,
            "definition": definition,
            "what_changed": what_changed,
            "macro_drivers": macro_drivers,
            "importance_rationale": rationale,
        },
        group.evidence,
    )
    proposition, proposition_rationale = assess_economic_proposition(
        label, definition, what_changed
    )
    if not proposition:
        raise ValueError(
            f"candidate is not an economic proposition: {proposition_rationale}"
        )
    return PatternAssessment(
        label=label,
        definition=definition,
        case_type=case_type,
        horizon=horizon,
        what_changed=what_changed,
        supporting_evidence_ids=supporting,
        contradicting_evidence_ids=contradicting,
        context_evidence_ids=context,
        entities=tuple(entities),
        industries=industries,
        macro_drivers=macro_drivers,
        missing_information=missing,
        importance=importance,
        importance_rationale=rationale,
        aliases=aliases,
        case_is_economic_proposition=proposition,
        proposition_rationale=proposition_rationale,
        semantic_fingerprint=semantic_case_fingerprint(label, entities, industries),
    )


def select_case_match(
    assessment: PatternAssessment,
    existing_cases: Sequence[Mapping[str, Any]],
    threshold: float,
) -> Mapping[str, Any] | None:
    for row in existing_cases:
        if row.get("semantic_fingerprint") == assessment.semantic_fingerprint:
            return row
    best: Mapping[str, Any] | None = None
    best_score = 0.0
    candidate_text = " ".join((assessment.label, *assessment.aliases))
    for row in existing_cases[:200]:
        values = [str(row.get("title") or "")]
        aliases = row.get("aliases")
        if isinstance(aliases, list):
            values.extend(str(alias) for alias in aliases[:20])
        score = max(token_similarity(candidate_text, value) for value in values)
        if score > best_score:
            best, best_score = row, score
    return best if best is not None and best_score >= threshold else None


def pattern_prompt_payload(group: CandidateGroup) -> str:
    return json.dumps(
        {
            "blocking_key": group.blocking_key,
            "metrics": candidate_metrics(group),
            "source_names": group.source_names,
            "industries": group.industries,
            "evidence": [item.to_dict() for item in group.evidence],
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )


__all__ = [
    "PatternAssessment",
    "assess_economic_proposition",
    "build_candidate_groups",
    "candidate_metrics",
    "normalize_case_label",
    "pattern_prompt_payload",
    "reject_unsupported_numeric_text",
    "select_case_match",
    "semantic_case_fingerprint",
    "token_similarity",
    "validate_pattern_output",
]
