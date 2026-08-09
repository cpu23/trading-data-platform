"""Canonical entity normalization and economic relationship grammar."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from research_intelligence.contracts import NormalizedEntity, canonical_fingerprint

RELATIONSHIPS = (
    "supplies",
    "purchases_from",
    "consumes",
    "depends_on",
    "raises_demand_for",
    "reduces_demand_for",
    "raises_supply_of",
    "reduces_supply_of",
    "raises_cost_for",
    "passes_cost_to",
    "constrains",
    "substitutes_for",
    "complements",
    "increases_capex_for",
    "derives_revenue_from",
    "exposed_to",
    "regulates",
    "finances",
)
RELATIONSHIP_SET = frozenset(RELATIONSHIPS)
ENTITY_TYPES = frozenset(
    {
        "company",
        "industry",
        "product",
        "technology",
        "commodity",
        "concept",
        "macro_region",
        "market",
        "symbol",
        "country",
    }
)
_ENTITY_ALIASES: dict[str, str] = {
    "business": "company",
    "corporation": "company",
    "sector": "industry",
    "region": "macro_region",
    "asset": "market",
    "instrument": "market",
    "ticker": "symbol",
}
_NAME_ALIASES: Mapping[str, str] = {
    "united states": "us",
    "united states of america": "us",
    "u.s.": "us",
    "eurozone": "euro-area",
    "euro area": "euro-area",
    "united kingdom": "uk",
    "great britain": "uk",
}
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def normalize_entity_type(value: Any) -> str:
    normalized = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    normalized = _ENTITY_ALIASES.get(normalized, normalized)
    if normalized not in ENTITY_TYPES:
        raise ValueError(f"unsupported entity type: {normalized[:40]}")
    return normalized


def normalize_entity_key(value: Any) -> str:
    text_value = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    text_value = _NAME_ALIASES.get(text_value, text_value)
    tokens = _TOKEN_RE.findall(text_value)
    if not tokens:
        raise ValueError("entity name is blank after normalization")
    return "-".join(tokens)[:180]


def normalize_entity(entity_type: Any, name: Any, key: Any = None) -> NormalizedEntity:
    kind = normalize_entity_type(entity_type)
    display = " ".join(str(name or "").split())[:200]
    if not display:
        raise ValueError("entity display name is required")
    normalized_key = normalize_entity_key(key if key is not None else display)
    return NormalizedEntity.create(kind, normalized_key, display)


def validate_relationship(value: Any) -> str:
    relationship = str(value or "").strip().casefold()
    if relationship not in RELATIONSHIP_SET:
        raise ValueError(f"unsupported causal relationship: {relationship[:80]}")
    return relationship


def causal_edge_fingerprint(
    *,
    from_type: Any,
    from_key: Any,
    relationship: Any,
    to_type: Any,
    to_key: Any,
) -> str:
    source_type = normalize_entity_type(from_type)
    target_type = normalize_entity_type(to_type)
    source_key = normalize_entity_key(from_key)
    target_key = normalize_entity_key(to_key)
    relation = validate_relationship(relationship)
    if source_type == target_type and source_key == target_key:
        raise ValueError("causal self-edge is not allowed")
    return canonical_fingerprint(
        {
            "from_type": source_type,
            "from_key": source_key,
            "relationship": relation,
            "to_type": target_type,
            "to_key": target_key,
        }
    )


__all__ = [
    "ENTITY_TYPES",
    "RELATIONSHIPS",
    "RELATIONSHIP_SET",
    "causal_edge_fingerprint",
    "normalize_entity",
    "normalize_entity_key",
    "normalize_entity_type",
    "validate_relationship",
]
