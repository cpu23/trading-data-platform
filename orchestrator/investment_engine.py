"""Pure, deterministic investment-report analysis rules.

The module deliberately contains no I/O.  Model output is treated as untrusted
input: only finite numeric values are allowed into arithmetic, and all missing
inputs remain explicit ``None`` values in the result.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any

CURRENCY_CODES = ("USD", "EUR", "GBP", "JPY", "CNY", "KRW", "TWD", "HKD", "SGD", "INR")

MATERIAL_RELATIONSHIP_KINDS = (
    "same_period_top_bottom_growth",
    "external_effect_on_recipient",
    "cash_generation_vs_investment",
)
MAX_MATERIAL_RELATIONSHIPS = 3
MAX_RELATIONSHIP_FACT_REFS = 8
MAX_NORMALIZED_RELATIONSHIP_FACTS = 24
MAX_RELATIONSHIP_SOURCE_PATHS = 2
MAX_RELATIONSHIP_QUALIFIERS = 8

_RELATIONSHIP_ROLES = {
    "top_line",
    "bottom_line",
    "cash_generation",
    "cash_investment",
    "external_effect",
    "external_recipient",
}
_RELATIONSHIP_LEAVES = {"standard_metric", "level", "growth", "external_effect"}
_RELATIONSHIP_METRIC_FAMILIES = {
    "revenue",
    "net_income",
    "diluted_eps",
    "gross_profit",
    "gross_margin",
    "operating_cash_flow",
    "free_cash_flow",
    "capital_investment",
    "capex",
    "lease_inclusive_capex",
    "external_effect",
}
_RELATIONSHIP_ROLE_BY_FAMILY = {
    "revenue": "top_line",
    "net_income": "bottom_line",
    "diluted_eps": "bottom_line",
    "gross_profit": "external_recipient",
    "gross_margin": "external_recipient",
    "operating_cash_flow": "cash_generation",
    "free_cash_flow": "cash_generation",
    "capital_investment": "cash_investment",
    "capex": "cash_investment",
    "lease_inclusive_capex": "cash_investment",
    "external_effect": "external_effect",
}
_RELATIONSHIP_COMPARISON_BASES = {
    "year_over_year_gaap",
    "year_over_year_constant_currency",
    "sequential",
    "none",
}
_RELATIONSHIP_TEMPORAL_BASES = {
    "period_flow",
    "point_in_time_stock",
    "rate_over_period",
    "guidance",
}
_RELATIONSHIP_CASH_BASES = {
    "cash",
    "cash_plus_finance_leases",
    "lease_only",
    "noncash",
    "not_applicable",
}


def _plain_decimal(value: Decimal) -> int | float:
    integral = value.to_integral_value()
    return int(integral) if value == integral else float(value)


@dataclass(frozen=True, slots=True)
class NormalizedRelationshipFact:
    fact_id: str
    metric_key: str
    metric_label: str
    value: Decimal
    unit: str | None
    currency: str | None
    period: str | None
    scope: str | None
    comparison_basis: str | None
    temporal_basis: str | None
    cash_basis: str | None
    source_paths: tuple[str, ...]
    derivation: str | None
    qualifiers: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "metric_key": self.metric_key,
            "metric_label": self.metric_label,
            "value": _plain_decimal(self.value),
            "unit": self.unit,
            "currency": self.currency,
            "period": self.period,
            "scope": self.scope,
            "comparison_basis": self.comparison_basis,
            "temporal_basis": self.temporal_basis,
            "cash_basis": self.cash_basis,
            "source_paths": list(self.source_paths),
            "derivation": self.derivation,
            "qualifiers": list(self.qualifiers),
        }


@dataclass(frozen=True, slots=True)
class RelationshipFactRef:
    fact_path: str
    role: str

    def to_payload(self) -> dict[str, str]:
        return {"fact_path": self.fact_path, "role": self.role}


@dataclass(frozen=True, slots=True)
class MaterialRelationship:
    relationship_id: str
    kind: str
    priority: int
    compatibility: str
    incompatibility_reasons: tuple[str, ...]
    required_facts: tuple[RelationshipFactRef, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "relationship_id": self.relationship_id,
            "kind": self.kind,
            "priority": self.priority,
            "compatibility": self.compatibility,
            "incompatibility_reasons": list(self.incompatibility_reasons),
            "required_facts": [ref.to_payload() for ref in self.required_facts],
        }


@dataclass(frozen=True, slots=True)
class MaterialRelationshipContract:
    relationship_facts: tuple[NormalizedRelationshipFact, ...]
    material_relationships: tuple[MaterialRelationship, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "relationship_facts": {
                fact.fact_id: fact.to_payload() for fact in self.relationship_facts
            },
            "material_relationships": [
                relationship.to_payload()
                for relationship in self.material_relationships
            ],
        }


@dataclass(frozen=True, slots=True)
class _TaggedRelationshipFact:
    fact: NormalizedRelationshipFact
    role: str
    leaf: str
    metric_family: str
    tags: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _RelationshipCandidate:
    kind: str
    compatibility: str
    incompatibility_reasons: tuple[str, ...]
    required: tuple[tuple[_TaggedRelationshipFact, str], ...]


def _normalized_tag(raw: Any, allowed: set[str]) -> str | None:
    if not isinstance(raw, str):
        return None
    value = re.sub(r"[\s-]+", "_", raw.strip().casefold())
    return value if value in allowed else None


def _normalized_text(raw: Any, *, limit: int = 160) -> str | None:
    if not isinstance(raw, str):
        return None
    value = " ".join(raw.split())
    return value[:limit] if value else None


def _decimal_value(raw: Any) -> Decimal | None:
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = Decimal(str(raw))
        finite_float = float(value)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return None
    return value if value.is_finite() and math.isfinite(finite_float) else None


def _opaque_id(domain: str, identity: Any) -> str:
    encoded = json.dumps(
        identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(domain.encode("ascii") + b"\0" + encoded).hexdigest()[:20]


def _fact_identity(
    *,
    metric_key: str,
    value: Decimal,
    unit: str | None,
    currency: str | None,
    period: str | None,
    scope: str | None,
    comparison_basis: str | None,
    temporal_basis: str | None,
    cash_basis: str | None,
    source_paths: tuple[str, ...],
    derivation: str | None,
) -> tuple[Any, ...]:
    return (
        metric_key,
        str(value),
        unit,
        currency,
        period,
        scope,
        comparison_basis,
        temporal_basis,
        cash_basis,
        source_paths,
        derivation,
    )


def _qualifiers(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        return ()
    values = {
        value
        for item in raw
        if (value := _normalized_text(item, limit=80)) is not None
    }
    return tuple(sorted(values))[:MAX_RELATIONSHIP_QUALIFIERS]


def _tagged_fact(
    record: Mapping[str, Any], path: str
) -> _TaggedRelationshipFact | None:
    tags = record.get("relationship_tags")
    if not isinstance(tags, Mapping):
        return None
    leaf = _normalized_tag(tags.get("leaf"), _RELATIONSHIP_LEAVES)
    family = _normalized_tag(
        tags.get("metric_family"), _RELATIONSHIP_METRIC_FAMILIES
    )
    role = _normalized_tag(tags.get("role"), _RELATIONSHIP_ROLES)
    if role is None and family is not None:
        role = _RELATIONSHIP_ROLE_BY_FAMILY.get(family)
    if role is None or leaf is None or family is None:
        return None
    value = _decimal_value(record.get("value"))
    if value is None:
        return None
    comparison_basis = _normalized_tag(
        tags.get("comparison_basis"), _RELATIONSHIP_COMPARISON_BASES
    )
    temporal_basis = _normalized_tag(
        tags.get("temporal_basis"), _RELATIONSHIP_TEMPORAL_BASES
    )
    cash_basis = _normalized_tag(tags.get("cash_basis"), _RELATIONSHIP_CASH_BASES)
    scope = _normalized_text(tags.get("scope"), limit=80)
    period = _normalized_text(record.get("period"))
    unit = _normalized_text(record.get("unit"), limit=80)
    currency = _normalized_text(record.get("currency"), limit=8)
    if currency is not None:
        currency = currency.upper()
    elif unit is not None:
        parsed_currency, _, _ = _canonical_unit(unit)
        currency = parsed_currency
    metric_key = _normalized_text(tags.get("metric_key"), limit=120)
    if metric_key is None:
        metric_key = path.rsplit(".", 1)[-1]
    metric_label = _normalized_text(tags.get("metric_label"), limit=160)
    if metric_label is None:
        metric_label = _normalized_text(record.get("concept"), limit=160) or metric_key
    explicit_paths = tags.get("source_paths")
    paths = [path]
    if isinstance(explicit_paths, Sequence) and not isinstance(explicit_paths, str):
        paths.extend(
            item
            for item in explicit_paths
            if isinstance(item, str) and item.strip()
        )
    source_paths = tuple(dict.fromkeys(paths))[:MAX_RELATIONSHIP_SOURCE_PATHS]
    derivation = "reported"
    identity = _fact_identity(
        metric_key=metric_key,
        value=value,
        unit=unit,
        currency=currency,
        period=period,
        scope=scope,
        comparison_basis=comparison_basis,
        temporal_basis=temporal_basis,
        cash_basis=cash_basis,
        source_paths=source_paths,
        derivation=derivation,
    )
    fact = NormalizedRelationshipFact(
        fact_id=f"rf_{_opaque_id('relationship-fact', identity)}",
        metric_key=metric_key,
        metric_label=metric_label,
        value=value,
        unit=unit,
        currency=currency,
        period=period,
        scope=scope,
        comparison_basis=comparison_basis,
        temporal_basis=temporal_basis,
        cash_basis=cash_basis,
        source_paths=source_paths,
        derivation=derivation,
        qualifiers=_qualifiers(tags.get("qualifiers")),
    )
    return _TaggedRelationshipFact(fact, role, leaf, family, tags)


def _walk_tagged_facts(value: Any, path: str) -> tuple[_TaggedRelationshipFact, ...]:
    found: list[_TaggedRelationshipFact] = []

    def visit(item: Any, item_path: str) -> None:
        if isinstance(item, Mapping):
            tagged = _tagged_fact(item, item_path)
            if tagged is not None:
                found.append(tagged)
                return
            for key in sorted(item, key=lambda candidate: str(candidate)):
                visit(item[key], f"{item_path}.{key}")
        elif isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                visit(child, f"{item_path}.{index}")

    if isinstance(value, Mapping):
        visit(value, path)
    return tuple(
        sorted(
            found,
            key=lambda tagged: (
                tagged.fact.source_paths,
                tagged.fact.fact_id,
            ),
        )
    )


def _same_known(left: str | None, right: str | None) -> bool:
    return left is not None and right is not None and left == right


def _strict_same_period(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return False
    left_years, left_basis, left_quarter, left_duration = _period_dimensions(left)
    right_years, right_basis, right_quarter, right_duration = _period_dimensions(right)
    if len(left_years) != 1 or left_years != right_years:
        return False
    normalized_left = " ".join(left.casefold().split())
    normalized_right = " ".join(right.casefold().split())
    if normalized_left == normalized_right:
        return True
    return (
        left_basis is not None
        and left_basis == right_basis
        and left_quarter == right_quarter
        and left_duration == right_duration
    )


def _strict_prior_period(
    current: str | None,
    prior: str | None,
    *,
    current_duration_days: Any = None,
    prior_duration_days: Any = None,
) -> bool:
    if current is None or prior is None:
        return False
    current_years, current_basis, current_quarter, current_duration = (
        _period_dimensions(current)
    )
    prior_years, prior_basis, prior_quarter, prior_duration = _period_dimensions(prior)
    if len(current_years) != 1 or len(prior_years) != 1:
        return False
    if next(iter(current_years)) - next(iter(prior_years)) != 1:
        return False
    if current_basis != prior_basis:
        return False
    if current_basis is None:
        current_text = current.strip()
        prior_text = prior.strip()
        bare_years = (
            re.fullmatch(r"(?:19|20)\d{2}", current_text)
            and re.fullmatch(r"(?:19|20)\d{2}", prior_text)
        )
        iso_dates = (
            re.fullmatch(r"(?:19|20)\d{2}-\d{2}-\d{2}", current_text)
            and re.fullmatch(r"(?:19|20)\d{2}-\d{2}-\d{2}", prior_text)
        )
        if bare_years:
            pass
        elif iso_dates:
            if current_text[4:] != prior_text[4:]:
                return False
            if (
                isinstance(current_duration_days, bool)
                or isinstance(prior_duration_days, bool)
            ):
                return False
            try:
                current_days = int(current_duration_days)
                prior_days = int(prior_duration_days)
            except (TypeError, ValueError, OverflowError):
                return False
            if not (
                350 <= current_days <= 380
                and 350 <= prior_days <= 380
                and abs(current_days - prior_days) <= 7
            ):
                return False
        else:
            return False
    return (
        current_quarter == prior_quarter
        and current_duration == prior_duration
    )


def _strict_monetary_factor(
    left: NormalizedRelationshipFact, right: NormalizedRelationshipFact
) -> Decimal | None:
    if left.currency is None or right.currency is None:
        return None
    if left.currency != right.currency or left.unit is None or right.unit is None:
        return None
    left_currency, left_scale, left_typed = _canonical_unit(left.unit)
    right_currency, right_scale, right_typed = _canonical_unit(right.unit)
    if not left_typed or not right_typed:
        return None
    if left_currency != left.currency or right_currency != right.currency:
        return None
    left_factor = Decimal(str(left_scale or 1))
    right_factor = Decimal(str(right_scale or 1))
    return right_factor / left_factor

def _rate_units_compatible(left: str | None, right: str | None) -> bool:
    percent_units = {"%", "percent", "percentage"}
    left_unit = _unit_text(left)
    right_unit = _unit_text(right)
    if not left_unit or not right_unit:
        return False
    if left_unit in percent_units and right_unit in percent_units:
        return True
    return left_unit == right_unit


def _derive_growth_facts(
    current: tuple[_TaggedRelationshipFact, ...],
    prior: tuple[_TaggedRelationshipFact, ...],
) -> tuple[_TaggedRelationshipFact, ...]:
    derived: list[_TaggedRelationshipFact] = []
    current_levels = [
        item
        for item in current
        if item.role in {"top_line", "bottom_line"}
        and item.leaf in {"standard_metric", "level"}
    ]
    prior_levels = [
        item
        for item in prior
        if item.role in {"top_line", "bottom_line"}
        and item.leaf in {"standard_metric", "level"}
    ]
    for current_item in current_levels:
        matches = [
            item
            for item in prior_levels
            if item.role == current_item.role
            and item.metric_family == current_item.metric_family
        ]
        for prior_item in matches:
            current_fact = current_item.fact
            prior_fact = prior_item.fact
            if prior_fact.value == 0:
                continue
            if (
                current_fact.scope != "consolidated"
                or prior_fact.scope != "consolidated"
            ):
                continue
            if not _strict_prior_period(
                current_fact.period,
                prior_fact.period,
                current_duration_days=current_item.tags.get("duration_days"),
                prior_duration_days=prior_item.tags.get("duration_days"),
            ):
                continue
            if (
                current_fact.temporal_basis != "period_flow"
                or prior_fact.temporal_basis != "period_flow"
                or current_fact.comparison_basis != "none"
                or prior_fact.comparison_basis != "none"
                or not _same_known(current_fact.cash_basis, prior_fact.cash_basis)
            ):
                continue
            factor = _strict_monetary_factor(current_fact, prior_fact)
            if factor is None:
                continue
            try:
                aligned_prior = prior_fact.value * factor
                growth = (
                    (current_fact.value - aligned_prior)
                    / abs(aligned_prior)
                    * Decimal(100)
                ).quantize(Decimal("0.1"), rounding=ROUND_HALF_EVEN)
            except (InvalidOperation, OverflowError, ZeroDivisionError):
                continue
            if not growth.is_finite():
                continue
            source_paths = tuple(
                dict.fromkeys(current_fact.source_paths + prior_fact.source_paths)
            )[:MAX_RELATIONSHIP_SOURCE_PATHS]
            metric_key = f"{current_fact.metric_key}_derived_growth"
            derivation = "current_and_prior_percent_change"
            identity = _fact_identity(
                metric_key=metric_key,
                value=growth,
                unit="percent",
                currency=None,
                period=current_fact.period,
                scope=current_fact.scope,
                comparison_basis="year_over_year_gaap",
                temporal_basis="rate_over_period",
                cash_basis=current_fact.cash_basis,
                source_paths=source_paths,
                derivation=derivation,
            )
            fact = NormalizedRelationshipFact(
                fact_id=f"rf_{_opaque_id('relationship-fact', identity)}",
                metric_key=metric_key,
                metric_label=f"{current_fact.metric_label} growth",
                value=growth,
                unit="percent",
                currency=None,
                period=current_fact.period,
                scope=current_fact.scope,
                comparison_basis="year_over_year_gaap",
                temporal_basis="rate_over_period",
                cash_basis=current_fact.cash_basis,
                source_paths=source_paths,
                derivation=derivation,
                qualifiers=(
                    "derived_from_current_and_prior",
                    "rounded_to_one_decimal",
                ),
            )
            derived.append(
                _TaggedRelationshipFact(
                    fact,
                    current_item.role,
                    "growth",
                    current_item.metric_family,
                    {"duration_days": current_item.tags.get("duration_days")},
                )
            )
            break
    return tuple(sorted(derived, key=lambda item: item.fact.fact_id))


def _common_reasons(
    left: NormalizedRelationshipFact,
    right: NormalizedRelationshipFact,
    *,
    require_same_period: bool = True,
) -> list[str]:
    reasons: list[str] = []
    if (
        left.scope == "other"
        or right.scope == "other"
        or not _same_known(left.scope, right.scope)
    ):
        reasons.append("scope_mismatch")
    if require_same_period and not _strict_same_period(left.period, right.period):
        reasons.append("period_mismatch")
    if not _same_known(left.comparison_basis, right.comparison_basis):
        reasons.append("comparison_basis_mismatch")
    if not _same_known(left.temporal_basis, right.temporal_basis):
        reasons.append("temporal_basis_mismatch")
    return reasons

def _tagged_duration_compatible(
    left: _TaggedRelationshipFact, right: _TaggedRelationshipFact
) -> bool:
    left_period = left.fact.period or ""
    right_period = right.fact.period or ""
    iso_pattern = r"(?:19|20)\d{2}-\d{2}-\d{2}"
    if not (
        re.fullmatch(iso_pattern, left_period)
        or re.fullmatch(iso_pattern, right_period)
    ):
        return True
    left_days = left.tags.get("duration_days")
    right_days = right.tags.get("duration_days")
    if (
        isinstance(left_days, bool)
        or isinstance(right_days, bool)
        or not isinstance(left_days, int)
        or not isinstance(right_days, int)
    ):
        return False
    return left_days > 0 and left_days == right_days


def _candidate(
    kind: str,
    required: Sequence[tuple[_TaggedRelationshipFact, str]],
    reasons: Sequence[str],
) -> _RelationshipCandidate:
    normalized_reasons = tuple(dict.fromkeys(reasons))
    return _RelationshipCandidate(
        kind=kind,
        compatibility="compatible" if not normalized_reasons else "incompatible",
        incompatibility_reasons=normalized_reasons,
        required=tuple(required)[:MAX_RELATIONSHIP_FACT_REFS],
    )


def _growth_candidates(
    facts: tuple[_TaggedRelationshipFact, ...],
) -> tuple[_RelationshipCandidate, ...]:
    top = [
        item
        for item in facts
        if item.role == "top_line" and item.leaf == "growth"
    ]
    bottom = [
        item
        for item in facts
        if item.role == "bottom_line" and item.leaf == "growth"
    ]
    candidates: list[_RelationshipCandidate] = []
    if not top and not bottom:
        return ()
    if not top or not bottom:
        present = top or bottom
        role = "top_line_growth" if top else "bottom_line_growth"
        return (
            _candidate(
                MATERIAL_RELATIONSHIP_KINDS[0],
                [(present[0], role)],
                ["missing_required_role"],
            ),
        )
    for top_item in top:
        for bottom_item in bottom:
            reasons = _common_reasons(top_item.fact, bottom_item.fact)
            if not _tagged_duration_compatible(top_item, bottom_item):
                reasons.append("period_mismatch")
            if not _rate_units_compatible(
                top_item.fact.unit, bottom_item.fact.unit
            ):
                reasons.append("unit_mismatch")
            candidates.append(
                _candidate(
                    MATERIAL_RELATIONSHIP_KINDS[0],
                    (
                        (top_item, "top_line_growth"),
                        (bottom_item, "bottom_line_growth"),
                    ),
                    reasons,
                )
            )
    return tuple(candidates)


def _recipient_lookup_keys(item: _TaggedRelationshipFact) -> tuple[str, ...]:
    keys: list[str] = []
    for path in item.fact.source_paths:
        keys.append(path)
        if path.startswith("current."):
            keys.append(path[len("current."):])
        if path.startswith("current.metrics."):
            keys.append(f"current.{path[len('current.metrics.'):]}")
    return tuple(dict.fromkeys(keys))


def _external_dimension_reasons(
    effect: _TaggedRelationshipFact, recipient: _TaggedRelationshipFact
) -> list[str]:
    reasons = _common_reasons(effect.fact, recipient.fact)
    if not _tagged_duration_compatible(effect, recipient):
        reasons.append("period_mismatch")
    effect_kind = _normalized_tag(
        effect.tags.get("effect_kind"),
        {"contribution", "drag", "reclassification"},
    )
    effect_basis = _normalized_tag(
        effect.tags.get("effect_basis"),
        {"percentage_points", "per_share", "monetary"},
    )
    recipient_unit = _unit_text(recipient.fact.unit)
    if effect_kind is None:
        reasons.append("effect_kind_unsupported")
    if effect_basis == "percentage_points":
        if recipient.fact.temporal_basis != "rate_over_period":
            reasons.append("effect_dimension_mismatch")
    elif effect_basis == "per_share":
        if "pershare" not in recipient_unit:
            reasons.append("effect_dimension_mismatch")
    elif effect_basis == "monetary":
        if _strict_monetary_factor(effect.fact, recipient.fact) is None:
            reasons.append("effect_dimension_mismatch")
    else:
        reasons.append("effect_basis_unsupported")
    declared = _normalized_text(effect.tags.get("compatibility"), limit=40)
    if declared == "incompatible":
        raw_reasons = effect.tags.get("incompatibility_reasons")
        if isinstance(raw_reasons, Sequence) and not isinstance(raw_reasons, str):
            reasons.extend(
                reason
                for item in raw_reasons
                if (reason := _normalized_text(item, limit=80)) is not None
            )
        else:
            reasons.append("effect_semantics_unresolved")
    return reasons

def _resolve_external_recipient(
    effect: _TaggedRelationshipFact,
    recipients: Mapping[str, Sequence[_TaggedRelationshipFact]],
) -> _TaggedRelationshipFact | None:
    recipient_path = _normalized_text(effect.tags.get("recipient_path"), limit=240)
    if recipient_path is None:
        return None
    candidates = list(recipients.get(recipient_path, ()))
    effect_basis = _normalized_tag(
        effect.tags.get("effect_basis"),
        {"percentage_points", "per_share", "monetary"},
    )
    if effect_basis == "percentage_points":
        candidates = [
            item
            for item in candidates
            if (
                item.leaf == "growth"
                or (
                    item.metric_family == "gross_margin"
                    and item.leaf in {"standard_metric", "level"}
                )
            )
            and item.fact.temporal_basis == "rate_over_period"
        ]
    elif effect_basis == "per_share":
        candidates = [
            item
            for item in candidates
            if item.leaf in {"standard_metric", "level"}
            and "pershare" in _unit_text(item.fact.unit)
        ]
    elif effect_basis == "monetary":
        candidates = [
            item
            for item in candidates
            if item.leaf in {"standard_metric", "level"}
            and _strict_monetary_factor(effect.fact, item.fact) is not None
        ]
    else:
        return None
    return candidates[0] if len(candidates) == 1 else None


def _external_candidates(
    facts: tuple[_TaggedRelationshipFact, ...],
) -> tuple[_RelationshipCandidate, ...]:
    effects = [item for item in facts if item.role == "external_effect"]
    recipients: dict[str, list[_TaggedRelationshipFact]] = {}
    for item in facts:
        if item.role == "external_effect":
            continue
        for key in _recipient_lookup_keys(item):
            recipients.setdefault(key, []).append(item)
    candidates: list[_RelationshipCandidate] = []
    groups: dict[str, list[_TaggedRelationshipFact]] = {}
    for effect in effects:
        group_id = _normalized_text(effect.tags.get("group_id"), limit=120)
        groups.setdefault(group_id or effect.fact.fact_id, []).append(effect)
    for group_id in sorted(groups):
        grouped_effects = sorted(
            groups[group_id], key=lambda item: item.fact.fact_id
        )
        resolved = [
            (effect, _resolve_external_recipient(effect, recipients))
            for effect in grouped_effects
        ]
        reasons: list[str] = []
        for effect, recipient in resolved:
            if recipient is not None:
                reasons.extend(_external_dimension_reasons(effect, recipient))
                continue
            raw_reasons = effect.tags.get("incompatibility_reasons")
            reason_count = len(reasons)
            if isinstance(raw_reasons, Sequence) and not isinstance(raw_reasons, str):
                reasons.extend(
                    reason
                    for item in raw_reasons
                    if (reason := _normalized_text(item, limit=80)) is not None
                )
            if len(reasons) == reason_count:
                reasons.append("unresolved_recipient")
        selected_pairs: list[
            tuple[_TaggedRelationshipFact, _TaggedRelationshipFact]
        ] = []
        selected_recipient_ids: set[str] = set()
        has_unresolved = any(recipient is None for _, recipient in resolved)
        for effect, recipient in (() if has_unresolved else resolved):
            if recipient is None:
                continue
            added_recipient = recipient.fact.fact_id not in selected_recipient_ids
            prospective_size = (
                len(selected_pairs)
                + 1
                + len(selected_recipient_ids)
                + int(added_recipient)
            )
            if prospective_size > MAX_RELATIONSHIP_FACT_REFS:
                break
            selected_pairs.append((effect, recipient))
            selected_recipient_ids.add(recipient.fact.fact_id)
        if selected_pairs:
            required = [
                *((effect, "effect") for effect, _ in selected_pairs),
                *(
                    (recipient, "recipient")
                    for recipient in sorted(
                        {
                            recipient.fact.fact_id: recipient
                            for _, recipient in selected_pairs
                        }.values(),
                        key=lambda item: item.fact.fact_id,
                    )
                ),
            ]
        else:
            required = [
                (effect, "effect")
                for effect in grouped_effects[:MAX_RELATIONSHIP_FACT_REFS]
            ]
        candidates.append(
            _candidate(
                MATERIAL_RELATIONSHIP_KINDS[1],
                required,
                reasons,
            )
        )
    return tuple(candidates)

def _cash_pair_reasons(
    generation: _TaggedRelationshipFact,
    investment: _TaggedRelationshipFact,
) -> list[str]:
    reasons = _common_reasons(generation.fact, investment.fact)
    if not _tagged_duration_compatible(generation, investment):
        reasons.append("period_mismatch")
    if not _same_known(generation.fact.cash_basis, investment.fact.cash_basis):
        reasons.append("cash_basis_ambiguous")
    if generation.fact.currency != investment.fact.currency:
        reasons.append("currency_mismatch")
    elif _strict_monetary_factor(generation.fact, investment.fact) is None:
        reasons.append("unit_mismatch")
    return reasons


def _cash_fact_sort_key(item: _TaggedRelationshipFact) -> tuple[Any, ...]:
    family_order = {
        "operating_cash_flow": 0,
        "free_cash_flow": 1,
    }
    return (
        family_order.get(item.metric_family, len(family_order)),
        item.fact.source_paths,
        item.fact.fact_id,
    )


def _selected_cash_generation(
    generation: Sequence[_TaggedRelationshipFact],
    anchor: _TaggedRelationshipFact | None,
) -> tuple[_TaggedRelationshipFact, ...]:
    selected: list[_TaggedRelationshipFact] = []
    for family in ("operating_cash_flow", "free_cash_flow"):
        candidates = [item for item in generation if item.metric_family == family]
        if not candidates:
            continue
        selected.append(
            min(
                candidates,
                key=lambda item: (
                    bool(anchor is not None and _cash_pair_reasons(item, anchor)),
                    _cash_fact_sort_key(item),
                ),
            )
        )
    return tuple(selected)


def _matching_lease_inclusive_investment(
    anchor: _TaggedRelationshipFact,
    investment: Sequence[_TaggedRelationshipFact],
) -> _TaggedRelationshipFact | None:
    matches = []
    for item in investment:
        if item.fact.cash_basis != "cash_plus_finance_leases":
            continue
        if _common_reasons(anchor.fact, item.fact):
            continue
        if not _tagged_duration_compatible(anchor, item):
            continue
        if anchor.fact.currency != item.fact.currency:
            continue
        if _strict_monetary_factor(anchor.fact, item.fact) is None:
            continue
        matches.append(item)
    return min(matches, key=_cash_fact_sort_key) if matches else None


def _cash_candidates(
    facts: tuple[_TaggedRelationshipFact, ...],
) -> tuple[_RelationshipCandidate, ...]:
    generation = sorted(
        (item for item in facts if item.role == "cash_generation"),
        key=_cash_fact_sort_key,
    )
    investment = sorted(
        (item for item in facts if item.role == "cash_investment"),
        key=_cash_fact_sort_key,
    )
    if not generation and not investment:
        return ()

    cash_investment = [
        item for item in investment if item.fact.cash_basis == "cash"
    ]
    if not generation or not investment:
        if generation:
            required = [
                (item, "cash_generation")
                for item in _selected_cash_generation(generation, None)
            ]
        else:
            anchor = cash_investment[0] if cash_investment else investment[0]
            required = [(anchor, "cash_investment")]
            if anchor.fact.cash_basis == "cash":
                supplemental = _matching_lease_inclusive_investment(anchor, investment)
                if supplemental is not None:
                    required.append((supplemental, "cash_investment_supplemental"))
        return (
            _candidate(
                MATERIAL_RELATIONSHIP_KINDS[2],
                required,
                ["missing_required_role"],
            ),
        )

    if not cash_investment:
        selected_generation = _selected_cash_generation(generation, None)
        selected_investment = investment[0]
        reasons = [
            reason
            for generation_item in selected_generation
            for reason in _cash_pair_reasons(generation_item, selected_investment)
        ]
        if "cash_basis_ambiguous" not in reasons:
            reasons.append("cash_basis_ambiguous")
        return (
            _candidate(
                MATERIAL_RELATIONSHIP_KINDS[2],
                [
                    *((item, "cash_generation") for item in selected_generation),
                    (selected_investment, "cash_investment"),
                ],
                reasons,
            ),
        )

    candidates: list[_RelationshipCandidate] = []
    for anchor in cash_investment:
        selected_generation = _selected_cash_generation(generation, anchor)
        required = [
            *((item, "cash_generation") for item in selected_generation),
            (anchor, "cash_investment"),
        ]
        supplemental = _matching_lease_inclusive_investment(anchor, investment)
        if supplemental is not None:
            required.append((supplemental, "cash_investment_supplemental"))
        reasons = [
            reason
            for generation_item in selected_generation
            for reason in _cash_pair_reasons(generation_item, anchor)
        ]
        candidates.append(
            _candidate(MATERIAL_RELATIONSHIP_KINDS[2], required, reasons)
        )
    return tuple(candidates)


def _candidate_rank(candidate: _RelationshipCandidate) -> tuple[Any, ...]:
    consolidated = sum(
        1 for tagged, _ in candidate.required if tagged.fact.scope == "consolidated"
    )
    reported = sum(
        1
        for tagged, _ in candidate.required
        if tagged.fact.derivation == "reported"
    )
    paths = tuple(
        path
        for tagged, _ in candidate.required
        for path in tagged.fact.source_paths[:1]
    )
    return (
        0 if candidate.compatibility == "compatible" else 1,
        -consolidated,
        -len(candidate.required),
        -reported,
        paths,
    )


def _materialize_relationship(
    candidate: _RelationshipCandidate, priority: int
) -> MaterialRelationship:
    required_facts = tuple(
        RelationshipFactRef(
            fact_path=f"deterministic_current.relationship_facts.{tagged.fact.fact_id}",
            role=role,
        )
        for tagged, role in candidate.required
    )
    identity = (
        candidate.kind,
        tuple(
            sorted(
                (tagged.fact.fact_id, role) for tagged, role in candidate.required
            )
        ),
    )
    return MaterialRelationship(
        relationship_id=f"mr_{_opaque_id('material-relationship', identity)}",
        kind=candidate.kind,
        priority=priority,
        compatibility=candidate.compatibility,
        incompatibility_reasons=candidate.incompatibility_reasons,
        required_facts=required_facts,
    )


def build_material_relationship_contract(
    current_facts: Mapping[str, Any] | None,
    prior_facts: Mapping[str, Any] | None = None,
) -> MaterialRelationshipContract:
    """Build bounded material relationships solely from production-owned tags."""
    current = _walk_tagged_facts(
        current_facts if isinstance(current_facts, Mapping) else {}, "current"
    )
    prior = _walk_tagged_facts(
        prior_facts if isinstance(prior_facts, Mapping) else {}, "prior"
    )
    relationship_facts = current + _derive_growth_facts(current, prior)
    groups = (
        _growth_candidates(relationship_facts),
        _external_candidates(relationship_facts),
        _cash_candidates(relationship_facts),
    )
    selected: list[_RelationshipCandidate] = []
    for candidates in groups:
        if candidates:
            selected.append(min(candidates, key=_candidate_rank))
    selected = selected[:MAX_MATERIAL_RELATIONSHIPS]
    relationships = tuple(
        _materialize_relationship(candidate, priority)
        for priority, candidate in enumerate(selected, start=1)
    )
    used_ids = {
        ref.fact_path.rsplit(".", 1)[-1]
        for relationship in relationships
        for ref in relationship.required_facts
    }
    used_facts = tuple(
        sorted(
            {
                item.fact.fact_id: item.fact
                for item in relationship_facts
                if item.fact.fact_id in used_ids
            }.values(),
            key=lambda fact: fact.fact_id,
        )
    )[:MAX_NORMALIZED_RELATIONSHIP_FACTS]
    return MaterialRelationshipContract(used_facts, relationships)


_UNIT_CURRENCY_CODES = {
    "us$": "USD",
    "hk$": "HKD",
    "nt$": "TWD",
    "c$": "CAD",
    "a$": "AUD",
    "$": "USD",
    "usd": "USD",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "gbp": "GBP",
    "¥": "JPY",
    "jpy": "JPY",
    "chf": "CHF",
    "cad": "CAD",
    "aud": "AUD",
    "cny": "CNY",
    "krw": "KRW",
    "twd": "TWD",
    "hkd": "HKD",
    "sgd": "SGD",
    "inr": "INR",
}
_UNIT_SCALE_FACTORS = (
    ("thousands", 1_000.0),
    ("thousand", 1_000.0),
    ("billions", 1e9),
    ("billion", 1e9),
    ("millions", 1e6),
    ("million", 1e6),
)
_UNIT_SCALE_TOKENS = {
    "k": 1_000.0,
    "m": 1e6,
    "mm": 1e6,
    "mn": 1e6,
    "bn": 1e9,
    "b": 1e9,
}


def _canonical_unit(raw: Any) -> tuple[str | None, float | None, bool]:
    """Parse a reported unit into ``(currency, scale factor, typed)``.

    Unknown units return ``typed=True`` with a ``None`` currency so monetary
    arithmetic can fail closed instead of mixing incompatible operands. An
    absent or empty unit is legacy untyped data: it pairs only with other
    untyped values.
    """
    if raw is None:
        return None, None, False
    text = re.sub(r"[\s_\-]+", "", str(raw)).casefold()
    if not text:
        return None, None, False
    if text == "reportmillions":
        return None, 1e6, True
    currency = None
    remainder = text
    for token, code in _UNIT_CURRENCY_CODES.items():
        index = remainder.find(token)
        if index >= 0:
            currency = code
            remainder = remainder[:index] + remainder[index + len(token) :]
            break
    scale = None
    if remainder:
        for name, factor in _UNIT_SCALE_FACTORS:
            if name in remainder:
                scale = factor
                remainder = remainder.replace(name, "", 1)
                break
        else:
            if remainder in _UNIT_SCALE_TOKENS:
                scale = _UNIT_SCALE_TOKENS[remainder]
                remainder = ""
    remainder = re.sub(r"(?:per)?/?shares?$", "", remainder)
    if remainder:
        return None, None, True
    return currency, (scale if scale != 1.0 else None), True


def _unit_text(raw: Any) -> str:
    """Separator-insensitive unit label used for unknown-definition equality."""
    if raw is None:
        return ""
    return re.sub(r"[\s_\-]+", "", str(raw)).casefold()


def _monetary_compatible(unit_a: Any, unit_b: Any) -> float | None:
    """Scale factor aligning two monetary units, or ``None`` when incompatible.

    Compatible units share a known currency (or are both legacy untyped) and
    carry a supported scale; the returned factor converts A's value into B's
    scale. Units of unknown definition pair only with an identical label, and
    unknown units never yield a number against a known one.
    """
    currency_a, scale_a, typed_a = _canonical_unit(unit_a)
    currency_b, scale_b, typed_b = _canonical_unit(unit_b)
    if not typed_a and not typed_b:
        return 1.0
    if not typed_a or not typed_b:
        return None
    if currency_a is None and currency_b is None:
        if _unit_text(unit_a) != _unit_text(unit_b):
            return None
    elif currency_a != currency_b:
        return None
    # Legacy untyped monetary labels carry the report's own scale; ratios
    # normalize through it so identical report-scale operands stay
    # comparable while cross-scale mixing still fails closed above.
    if scale_a is None:
        scale_a = 1.0
    if scale_b is None:
        scale_b = 1.0
    return scale_b / scale_a


def _period_dimensions(
    raw: Any,
) -> tuple[set[int], str | None, int | None, int | None]:
    """Return years, reporting basis, fiscal quarter, and duration in months."""
    if not raw:
        return set(), None, None, None
    text = str(raw).casefold()
    fiscal_year = re.search(
        r"\b(?:fy|fiscal(?:\s+year)?)\s*[-_/]?\s*((?:19|20)\d{2})\b",
        text,
    )
    calendar_year = re.search(
        r"\b(?:cy|calendar(?:\s+year)?)\s*[-_/]?\s*((?:19|20)\d{2})\b",
        text,
    )
    if fiscal_year is not None:
        years = {int(fiscal_year.group(1))}
        basis = "fiscal"
    elif calendar_year is not None:
        years = {int(calendar_year.group(1))}
        basis = "calendar"
    else:
        years = {int(year) for year in re.findall(r"(?:19|20)\d{2}", text)}
        basis = None
    quarter_match = re.search(r"\bq([1-4])\b", text)
    if quarter_match is None:
        quarter_match = re.search(
            r"\b(first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter\b",
            text,
        )
    quarter = None
    if quarter_match is not None:
        quarter_token = quarter_match.group(1)
        quarter = {
            "first": 1,
            "1st": 1,
            "second": 2,
            "2nd": 2,
            "third": 3,
            "3rd": 3,
            "fourth": 4,
            "4th": 4,
        }.get(quarter_token, int(quarter_token) if quarter_token.isdigit() else None)
    duration_match = re.search(
        r"\b(3|6|9|12|three|six|nine|twelve)[-\s]+months?\b", text
    )
    duration = None
    if duration_match is not None:
        duration_token = duration_match.group(1)
        duration = {
            "three": 3,
            "six": 6,
            "nine": 9,
            "twelve": 12,
        }.get(duration_token, int(duration_token) if duration_token.isdigit() else None)
    annual = bool(
        re.search(r"\b(?:annual|full[-\s]?year|year\s+ended|twelve\s+months)\b", text)
    )
    if duration is None:
        if annual or (basis is not None and quarter is None):
            duration = 12
        elif quarter is not None:
            duration = 3
    return years, basis, quarter, duration

def _annual_fcf_basis(period: Any) -> str | None:
    """Return the explicitly reported 12-month basis eligible for a DCF."""
    if not period:
        return None
    text = str(period).strip().casefold()
    if not text:
        return None
    _, _, quarter, duration = _period_dimensions(text)
    if quarter is not None or (duration is not None and duration != 12):
        return None
    if re.search(
        r"\b(?:ytd|qtd|year[-\s]+to[-\s]+date|quarter[-\s]+to[-\s]+date|"
        r"h[12]|first\s+half|second\s+half|\d+\s*(?:m|weeks?))\b",
        text,
    ):
        return None
    if re.search(r"\b(?:ttm|trailing\s+(?:twelve|12)\s+months?)\b", text):
        return "ttm"
    if re.search(r"\b(?:ltm|last\s+(?:twelve|12)\s+months?)\b", text):
        return "ttm"
    if re.search(
        r"\b(?:annual|full[-\s]?year|year\s+ended|"
        r"(?:twelve|12)[-\s]+months?)\b",
        text,
    ):
        return "annual"
    if re.fullmatch(
        r"(?:fy|cy)\s*[-_/]?\s*(?:19|20)\d{2}|"
        r"(?:fiscal|calendar)(?:\s+year)?\s*[-_/]?\s*(?:19|20)\d{2}",
        text,
    ):
        return "annual"
    return None



def _period_shape_compatible(period_a: Any, period_b: Any) -> bool:
    """Match known reporting basis, fiscal quarter, and duration dimensions."""
    _, basis_a, quarter_a, duration_a = _period_dimensions(period_a)
    _, basis_b, quarter_b, duration_b = _period_dimensions(period_b)
    if basis_a is not None and basis_b is not None and basis_a != basis_b:
        return False
    if duration_a is not None and duration_b is not None and duration_a != duration_b:
        return False
    if quarter_a is not None and quarter_b is not None and quarter_a != quarter_b:
        return False
    # A stated quarter cannot be treated as an annual/YTD period merely
    # because both labels mention the same fiscal year.
    if (quarter_a is None) != (quarter_b is None):
        other_duration = duration_a if quarter_a is None else duration_b
        if other_duration is not None and other_duration != 3:
            return False
    return True


def _period_compatible(period_a: Any, period_b: Any) -> bool:
    """True when two metric records describe the same reporting period.

    Missing or unparseable labels stay compatible so legacy records without
    periods keep working. Parseable labels must share a year and reporting
    shape, preventing quarter-to-annual same-period arithmetic.
    """
    if not period_a or not period_b:
        return True
    years_a, _, _, _ = _period_dimensions(period_a)
    years_b, _, _, _ = _period_dimensions(period_b)
    if years_a and years_b and not (years_a & years_b):
        return False
    return _period_shape_compatible(period_a, period_b)


def _prior_period_compatible(current_period: Any, prior_period: Any) -> bool:
    """True when current/prior labels identify comparable reporting periods.

    Legacy scalar facts carry no period metadata and remain comparable.
    Quarterly records may be consecutive quarters or the same quarter in the
    immediately preceding year. Other typed periods retain historical exact-
    label controls or adjacent-year comparison, subject to reporting shape.
    """
    if not current_period or not prior_period:
        return True
    (
        current_years,
        current_basis,
        current_quarter,
        current_duration,
    ) = _period_dimensions(current_period)
    prior_years, prior_basis, prior_quarter, prior_duration = _period_dimensions(
        prior_period
    )
    if (
        current_basis is not None
        and prior_basis is not None
        and current_basis != prior_basis
    ):
        return False
    if (
        current_duration is not None
        and prior_duration is not None
        and current_duration != prior_duration
    ):
        return False
    if (current_quarter is None) != (prior_quarter is None):
        return False

    if current_quarter is not None and prior_quarter is not None:
        if (
            len(current_years) != 1
            or len(prior_years) != 1
            or current_basis != prior_basis
        ):
            return False
        current_year = next(iter(current_years))
        prior_year = next(iter(prior_years))
        sequential = (
            current_year == prior_year and current_quarter == prior_quarter + 1
        ) or (
            current_year == prior_year + 1
            and current_quarter == 1
            and prior_quarter == 4
        )
        year_over_year = (
            current_year == prior_year + 1 and current_quarter == prior_quarter
        )
        return sequential or year_over_year

    # Historical control inputs may repeat one generic non-quarter label on
    # both sides (for example ``FY2025``). Preserve that exact same-period
    # comparison without admitting identical quarterly records, whose
    # current/prior relationship must be established by the rules above.
    if (
        str(current_period).strip().casefold()
        == str(prior_period).strip().casefold()
    ):
        return _period_shape_compatible(current_period, prior_period)

    if current_years and prior_years:
        if len(current_years) != 1 or len(prior_years) != 1:
            return False
        if next(iter(current_years)) != next(iter(prior_years)) + 1:
            return False
    return _period_shape_compatible(current_period, prior_period)


def _prior_monetary_value(
    current_record: Mapping[str, Any], prior_record: Mapping[str, Any]
) -> float | None:
    """Prior value aligned to the current record's scale when comparable."""
    prior_value = _finite(prior_record.get("value"))
    if prior_value is None or _finite(current_record.get("value")) is None:
        return None
    if not _prior_period_compatible(
        current_record.get("period"), prior_record.get("period")
    ):
        return None
    current_unit_currency, _, _ = _canonical_unit(current_record.get("unit"))
    prior_unit_currency, _, _ = _canonical_unit(prior_record.get("unit"))
    current_currency_raw = str(current_record.get("currency") or "").strip()
    prior_currency_raw = str(prior_record.get("currency") or "").strip()
    current_currency = (
        _UNIT_CURRENCY_CODES.get(
            current_currency_raw.casefold(), current_currency_raw.upper()
        )
        if current_currency_raw
        else current_unit_currency
    )
    prior_currency = (
        _UNIT_CURRENCY_CODES.get(
            prior_currency_raw.casefold(), prior_currency_raw.upper()
        )
        if prior_currency_raw
        else prior_unit_currency
    )
    if (
        current_currency_raw
        and current_unit_currency is not None
        and current_currency != current_unit_currency
    ) or (
        prior_currency_raw
        and prior_unit_currency is not None
        and prior_currency != prior_unit_currency
    ):
        return None
    if (current_currency is None) != (prior_currency is None):
        return None
    if current_currency is not None and current_currency != prior_currency:
        return None
    factor = _monetary_compatible(
        current_record.get("unit"), prior_record.get("unit")
    )
    if factor is None:
        return None
    aligned = prior_value * factor
    return aligned if math.isfinite(aligned) else None

_DERIVED_FCF_DEFINITIONS = {
    "operatingcashflowminuscapex",
    "operatingcashflowlesscapex",
    "operatingcashflowminuscapitalexpenditures",
    "operatingcashflowlesscapitalexpenditures",
    "cashfromoperationsminuscapex",
    "cashfromoperationslesscapex",
    "cashfromoperationsminuscapitalexpenditures",
    "cashfromoperationslesscapitalexpenditures",
}

_DERIVED_FCF_CONCEPT_DEFINITIONS = {
    "operatingcashflowminuscapex",
    "operatingcashflowminuscashpaidforpropertyandequipment",
}


def _definition_token(raw: Any) -> str:
    """Canonical token for exact comparison of definition metadata."""
    if not isinstance(raw, str):
        return ""
    text = re.sub(r"\s+[−-]\s+", " minus ", raw.casefold())
    return re.sub(r"[^a-z0-9]+", "", text)


def _declares_derived_fcf(record: Mapping[str, Any]) -> bool:
    """Whether an explicit record declares the canonical OCF-minus-capex basis."""
    concept = record.get("concept")
    if isinstance(concept, str):
        prefix, separator, expression = concept.partition(":")
        if (
            separator
            and prefix.strip().casefold() == "derived"
            and _definition_token(expression) in _DERIVED_FCF_CONCEPT_DEFINITIONS
        ):
            return True
    return any(
        _definition_token(record.get(field)) in _DERIVED_FCF_DEFINITIONS
        for field in ("definition", "derivation", "calculation")
    )


def _fcf_definitions_compatible(
    current_record: Mapping[str, Any],
    prior_record: Mapping[str, Any],
    *,
    current_derived: bool,
    prior_derived: bool,
) -> bool:
    """Match reported/derived FCF definitions without inferring equivalence."""
    if current_derived and prior_derived:
        return True
    if current_derived != prior_derived:
        explicit_record = prior_record if current_derived else current_record
        return _declares_derived_fcf(explicit_record)
    for field in ("concept", "definition", "derivation", "calculation"):
        current_definition = _definition_token(current_record.get(field))
        prior_definition = _definition_token(prior_record.get(field))
        if (
            current_definition
            and prior_definition
            and current_definition != prior_definition
        ):
            return False
    return True


def _monetary_pair(
    record_a: Mapping[str, Any], record_b: Mapping[str, Any]
) -> float | None:
    """B's value converted into A's unit scale, or ``None`` when incompatible.

    Compatibility requires the same known currency (or both legacy untyped),
    a supported unit scale on both sides, and matching reporting periods;
    incompatible or unknown operands yield unknown instead of a number.
    """
    value_b = _finite(record_b.get("value"))
    if value_b is None:
        return None
    factor = _monetary_compatible(
        record_a.get("unit"), record_b.get("unit")
    )
    if factor is None:
        return None
    if not _period_compatible(record_a.get("period"), record_b.get("period")):
        return None
    aligned = value_b * factor
    return aligned if math.isfinite(aligned) else None


_CANONICAL_ALIASES: dict[str, tuple[str, ...]] = {
    # Cash paid for property and equipment is the canonical cash capex;
    # finance-lease-inclusive capex is a different definition and never
    # substitutes for it.
    "capex": ("capex", "cash_paid_for_property_and_equipment"),
    # Company gross profit in currency units; segment or cloud margins are
    # different definitions and stay supplemental.
    "gross_profit": ("gross_profit", "gross_margin_dollars"),
}
_LEASE_INCLUSIVE_TOKENS = ("finance_lease", "finance_leases", "lease", "leases")


def _alias_blocked(name: str) -> bool:
    lowered = name.casefold()
    return any(token in lowered for token in _LEASE_INCLUSIVE_TOKENS)


def _canonical_record(facts: Any, name: str) -> Mapping[str, Any]:
    """First valid record among the canonical aliases for ``name``.

    Finance-lease-inclusive names are excluded from every candidate list so
    lease-inclusive capex can never masquerade as cash capex.
    """
    for candidate in _CANONICAL_ALIASES.get(name, (name,)):
        if _alias_blocked(candidate):
            continue
        record = _mapping(facts).get("metrics", {})
        item = record.get(candidate) if isinstance(record, Mapping) else None
        if isinstance(item, Mapping):
            value = _finite(item.get("value"))
            if value is not None:
                return item
        else:
            value = _finite(item)
            if value is not None:
                return {"value": value}
    return {}


def _metric_record(facts: Any, name: str) -> Mapping[str, Any]:
    """Record for a canonical metric, resolving documented source aliases."""
    return _canonical_record(facts, name)


def _canonical_metric_value(facts: Any, name: str) -> float | None:
    return _finite(_metric_record(facts, name).get("value"))


STANDARD_METRICS = (
    "revenue",
    "operating_cash_flow",
    "capex",
    "net_income",
    "diluted_eps",
    "shares_outstanding",
    "net_debt",
    "gross_margin",
    "inventory",
    "backlog",
    "gross_profit",
    "cash",
    "total_debt",
    "total_assets",
    "total_liabilities",
    "equity",
    "current_assets",
    "current_liabilities",
)


def _supplemental_metric_records(facts: Any) -> list[tuple[str, Mapping[str, Any]]]:
    """Valid finite non-standard records worth carrying into the analysis.

    A record qualifies when it holds a finite numeric value, carries any
    provenance (unit, period, evidence, source, concept, currency, or
    disclosure metadata such as ``source_url``, ``source_location``, and
    ``available_at``), and is not a canonical standard metric or one of
    its documented aliases.
    """
    metrics = _mapping(facts).get("metrics", {})
    if not isinstance(metrics, Mapping):
        return []
    alias_names = {
        name
        for candidates in _CANONICAL_ALIASES.values()
        for name in candidates
    }
    reserved = set(STANDARD_METRICS) | {"fcf", "free_cash_flow", "fcf_margin"}
    kept: list[tuple[str, Mapping[str, Any]]] = []
    for key, item in metrics.items():
        name = str(key)
        if not isinstance(item, Mapping) or name in reserved:
            continue
        if name in alias_names:
            continue
        if _finite(item.get("value")) is None:
            continue
        if not any(
            item.get(field)
            for field in (
                "evidence",
                "source",
                "concept",
                "unit",
                "period",
                "currency",
                "source_url",
                "source_location",
                "available_at",
            )
        ):
            continue
        kept.append((name, item))
    return sorted(kept, key=lambda entry: entry[0])



def _monetary_ratio(
    numerator_record: Mapping[str, Any], denominator_record: Mapping[str, Any]
) -> float | None:
    """Percent ratio of two monetary records, or ``None`` when incompatible.

    The numerator is first normalized into the denominator's currency and
    unit scale; unknown or incompatible operands yield unknown instead of a
    dimensionally invalid number.
    """
    aligned_numerator = _monetary_pair(denominator_record, numerator_record)
    if aligned_numerator is None:
        return None
    return _ratio(aligned_numerator, _finite(denominator_record.get("value")))


# Weights are intentionally part of the public deterministic table.  A score
# is a weighted contribution (not a probability or an LLM confidence value).
SIGNAL_RULES = OrderedDict(
    (
        (
            "revenue",
            {
                "weight": 2,
                "rule": "change >= 10%: +2; change > 0%: +1; change < -10%: -2; otherwise -1",
            },
        ),
        (
            "capex",
            {
                "weight": 2,
                "rule": "capex growth >= 15%: +2; growth > 0%: +1; decline <= -10%: -2; otherwise -1",
            },
        ),
        (
            "backlog",
            {
                "weight": 1,
                "rule": "change >= 10%: +1; positive change: +1; negative change: -1",
            },
        ),
        (
            "inventory_vs_revenue",
            {
                "weight": 2,
                "rule": "inventory growth above revenue growth: -2 (above 5pp), otherwise -1; below: +1",
            },
        ),
        (
            "fcf",
            {
                "weight": 2,
                "rule": "positive FCF growth/margin: +2; negative FCF or deterioration: -2",
            },
        ),
        (
            "gross_margin_delta",
            {
                "weight": 2,
                "rule": "margin expansion >= 1pp: +2; contraction <= -1pp: -2; otherwise +/-1",
            },
        ),
        (
            "ai_demand",
            {
                "weight": 1,
                "rule": "reported AI demand present; strength maps weak/moderate/strong to +1",
            },
        ),
        (
            "data_centre_demand",
            {
                "weight": 1,
                "rule": "reported data-centre demand present; strength maps weak/moderate/strong to +1",
            },
        ),
        (
            "supply_constraints",
            {
                "weight": 1,
                "rule": "reported supply constraint supports cycle pricing: +1; also retained as an operating risk",
            },
        ),
        (
            "pricing_power",
            {
                "weight": 1,
                "rule": "reported pricing power present; strength maps weak/moderate/strong to +1",
            },
        ),
        (
            "guidance_direction",
            {"weight": 2, "rule": "up/raised: +2; down/cut: -2; maintained/flat: 0"},
        ),
    )
)


# Material score-bearing finance signals: every one must carry valid evidence
# before the state may rise to confirmed/accelerating. Missing or invalid
# material finance is never confidence-neutral.
MATERIAL_FINANCE_SIGNALS = OrderedDict(
    (
        ("revenue", "current and prior revenue comparable"),
        ("capex", "current and prior cash capex comparable"),
        ("fcf", "valid explicit free cash flow or compatible derivation"),
        (
            "gross_margin_delta",
            "current and prior company gross margin comparable",
        ),
    )
)


_MISSING = object()


def _material_signal_coverage(current_facts: Any, previous_facts: Any) -> dict[str, bool]:
    """Deterministic per-signal validity map used to gate high states.

    Revenue and capex need comparable current/prior values; FCF needs a
    valid explicit level (or a compatible OCF/cash-capex derivation);
    gross_margin_delta needs a comparable margin pair from either a
    reported gross-margin percentage or canonical gross-profit dollars
    divided by compatible revenue.
    """
    metrics_current = _mapping(current_facts).get("metrics", {})
    metrics_prior = _mapping(previous_facts).get("metrics", {})
    if not isinstance(metrics_current, Mapping):
        metrics_current = {}
    if not isinstance(metrics_prior, Mapping):
        metrics_prior = {}

    def record(metrics: Any, name: str) -> Mapping[str, Any]:
        item = metrics.get(name) if isinstance(metrics, Mapping) else None
        return item if isinstance(item, Mapping) else {}

    def comparable(
        current_record: Mapping[str, Any], prior_record: Mapping[str, Any]
    ) -> bool:
        return _prior_monetary_value(current_record, prior_record) is not None

    explicit_record = record(metrics_current, "free_cash_flow")
    if _finite(explicit_record.get("value")) is not None:
        fcf_valid = True
    else:
        ocf_record = record(metrics_current, "operating_cash_flow")
        capex_canonical = _canonical_record(current_facts, "capex")
        fcf_valid = (
            _finite(ocf_record.get("value")) is not None
            and _monetary_pair(ocf_record, capex_canonical) is not None
        )

    margin_current = _finite(record(metrics_current, "gross_margin").get("value"))
    margin_prior = _finite(record(metrics_prior, "gross_margin").get("value"))
    gross_margin_valid = margin_current is not None and margin_prior is not None
    if not gross_margin_valid:
        profit_record = _canonical_record(current_facts, "gross_profit")
        profit_prior_record = _canonical_record(previous_facts, "gross_profit")
        gross_margin_valid = (
            _monetary_pair(profit_record, record(metrics_current, "revenue"))
            is not None
            and _monetary_pair(profit_prior_record, record(metrics_prior, "revenue"))
            is not None
        )
    return {
        "revenue": comparable(
            _canonical_record(current_facts, "revenue"),
            _canonical_record(previous_facts, "revenue"),
        ),
        "capex": comparable(
            _canonical_record(current_facts, "capex"),
            _canonical_record(previous_facts, "capex"),
        ),
        "fcf": fcf_valid,
        "gross_margin_delta": gross_margin_valid,
    }


def _finite(value: Any) -> float | None:
    """Convert a scalar to a finite float, rejecting booleans and junk."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _record(facts: Any, name: str) -> Mapping[str, Any]:
    metrics = _mapping(facts).get("metrics", {})
    value = metrics.get(name, {}) if isinstance(metrics, Mapping) else {}
    return value if isinstance(value, Mapping) else {"value": value}


def _metric_value(facts: Any, name: str) -> float | None:
    item = _record(facts, name)
    value = item.get("value", _MISSING)
    if value is _MISSING:
        # A few extraction versions emitted a scalar under the metric name.
        return _finite(item) if not isinstance(item, Mapping) else None
    return _finite(value)

def _direct_observation_coverage(current_facts: Any) -> dict[str, bool]:
    """Current facts that answer a watch topic without implying a comparison."""
    metrics = _mapping(current_facts).get("metrics", {})
    if not isinstance(metrics, Mapping):
        metrics = {}

    margin_record = _canonical_record(current_facts, "gross_margin")
    margin_tags = margin_record.get("relationship_tags")
    margin_temporal_basis = (
        _normalized_tag(
            margin_tags.get("temporal_basis"), _RELATIONSHIP_TEMPORAL_BASES
        )
        if isinstance(margin_tags, Mapping)
        else None
    )
    margin_observed = (
        _finite(margin_record.get("value")) is not None
        and margin_temporal_basis != "guidance"
    )
    guidance_observed = False
    for item in metrics.values():
        if not isinstance(item, Mapping) or _finite(item.get("value")) is None:
            continue
        tags = item.get("relationship_tags")
        if not isinstance(tags, Mapping):
            continue
        temporal_basis = _normalized_tag(
            tags.get("temporal_basis"), _RELATIONSHIP_TEMPORAL_BASES
        )
        if temporal_basis == "guidance":
            guidance_observed = True
        if (
            _normalized_tag(
                tags.get("metric_family"), _RELATIONSHIP_METRIC_FAMILIES
            )
            == "gross_margin"
            and temporal_basis == "rate_over_period"
            and _normalized_text(tags.get("scope")) in {None, "consolidated"}
        ):
            margin_observed = True

    gross_profit = _canonical_record(current_facts, "gross_profit")
    revenue = _canonical_record(current_facts, "revenue")
    derivation_records = (gross_profit, revenue)
    derivation_is_current = all(
        not isinstance(tags := record.get("relationship_tags"), Mapping)
        or _normalized_tag(
            tags.get("temporal_basis"), _RELATIONSHIP_TEMPORAL_BASES
        )
        != "guidance"
        for record in derivation_records
    )
    if derivation_is_current and _monetary_ratio(gross_profit, revenue) is not None:
        margin_observed = True

    qualitative = _mapping(current_facts).get("qualitative", {})
    if isinstance(qualitative, Mapping):
        for name in ("guidance_up", "guidance_down"):
            item = qualitative.get(name)
            if isinstance(item, Mapping):
                if item.get("present") or (isinstance(item.get("evidence"), str) and item["evidence"].strip()) or (isinstance(item.get("evidence"), list) and item["evidence"]):
                    guidance_observed = True

    materiality = _mapping(current_facts).get("materiality_assessment", {})
    if isinstance(materiality, Mapping):
        forward_guidance = materiality.get("forward_guidance")
        if isinstance(forward_guidance, Mapping) and forward_guidance.get("status") == "addressed":
            guidance_observed = True
        margin_economics = materiality.get("margin_economics")
        if isinstance(margin_economics, Mapping) and margin_economics.get("status") == "addressed":
            margin_observed = True

    return {
        "gross_margin_delta": margin_observed,
        "guidance_direction": guidance_observed,
    }


def _effective_value(
    current_facts: Any, name: str, market_inputs: Mapping[str, Any]
) -> float | None:
    override = market_inputs.get(name, _MISSING)
    if override is not _MISSING:
        converted = _finite(override)
        if converted is not None:
            return converted
    return _metric_value(current_facts, name)


def _change(current: float | None, prior: float | None) -> float | None:
    if current is None or prior is None:
        return None
    result = current - prior
    return result if math.isfinite(result) else None


def _pct_change(current: float | None, prior: float | None) -> float | None:
    if current is None or prior is None or prior == 0:
        return None
    result = (current - prior) / abs(prior) * 100.0
    return result if math.isfinite(result) else None


def _clean_evidence(item: Mapping[str, Any]) -> Any:
    evidence = item.get("evidence", [])
    return evidence if evidence is not None else []


def _metric_output(
    current_facts: Any, previous_facts: Any, name: str
) -> dict[str, Any]:
    current_item = _metric_record(current_facts, name)
    current = _finite(current_item.get("value"))
    prior_item = (
        _metric_record(previous_facts, name) if previous_facts is not None else {}
    )
    prior = _finite(prior_item.get("value"))
    return {
        "value": current,
        "unit": current_item.get("unit"),
        "period": current_item.get("period"),
        "evidence": _clean_evidence(current_item),
        "source": current_item.get("source"),
        "concept": current_item.get("concept"),
        "prior_value": prior,
        "change": _change(current, prior),
        "change_pct": _pct_change(current, prior),
    }


def _derived_metric(
    value: float | None, unit: str, evidence: Any, prior: float | None = None
) -> dict[str, Any]:
    return {
        "value": value,
        "unit": unit,
        "period": None,
        "evidence": evidence,
        "source": "derived",
        "prior_value": prior,
        "change": _change(value, prior),
        "change_pct": _pct_change(value, prior),
    }


def _direction_score(change_pct: float | None, weight: int) -> int:
    if change_pct is None:
        return 0
    half = max(1, weight // 2)
    if change_pct >= 10:
        return weight
    if change_pct > 0:
        return half
    if change_pct <= -10:
        return -weight
    return -half


def _ratio(value: float | None, denominator: float | None) -> float | None:
    if value is None or denominator is None or denominator == 0:
        return None
    result = value / denominator * 100.0
    return result if math.isfinite(result) else None


def _qualitative(facts: Any, names: tuple[str, ...]) -> tuple[Any, Any, Any]:
    qualitative = _mapping(facts).get("qualitative", {})
    if not isinstance(qualitative, Mapping):
        return None, None, []
    found: Mapping[str, Any] | None = None
    for wanted in names:
        for key, value in qualitative.items():
            if str(key).strip().lower().replace("-", "_").replace(" ", "_") == wanted:
                found = value if isinstance(value, Mapping) else {"present": value}
                break
        if found is not None:
            break
    if found is None:
        return None, None, []
    present = found.get("present")
    strength = found.get("strength")
    return present, strength, _clean_evidence(found)


def _qual_score(present: Any, strength: Any, *, negative: bool = False) -> int:
    if present is None and strength is None:
        return 0
    if present is False and strength is None:
        return 0
    direction = str(strength).strip().lower() if strength is not None else ""
    if direction in {"negative", "down", "declining", "weakening", "weak", "-1", "-2"}:
        score = -1
    elif direction in {
        "strong",
        "high",
        "positive",
        "up",
        "raised",
        "accelerating",
        "+2",
    }:
        score = 1
    elif direction in {
        "moderate",
        "medium",
        "flat",
        "maintained",
        "stable",
        "unchanged",
        "+1",
    }:
        score = (
            1 if direction not in {"flat", "maintained", "stable", "unchanged"} else 0
        )
    else:
        numeric = _finite(strength)
        if numeric is not None:
            score = 1 if numeric > 0 else -1 if numeric < 0 else 0
        else:
            score = 1 if bool(present) else 0
    if isinstance(present, str) and present.strip().lower() in {
        "false",
        "no",
        "absent",
        "none",
    }:
        score = 0
    if negative:
        score = -score
    return score


def _signal(
    rule_name: str,
    score: int,
    observed: Any,
    prior: Any,
    evidence: Any,
    *,
    basis: str = "deterministic_metric",
    comparable: bool | None = None,
) -> dict[str, Any]:
    rule = SIGNAL_RULES[rule_name]
    observed_number = _finite(observed) if basis == "deterministic_metric" else None
    prior_number = _finite(prior) if basis == "deterministic_metric" else None
    if comparable is None:
        comparable = observed is not None and prior is not None
    return {
        "rule": rule["rule"],
        "weight": rule["weight"],
        "score": score,
        "observed_value": observed,
        "prior_value": prior,
        "change": _change(observed_number, prior_number),
        "change_pct": _pct_change(observed_number, prior_number),
        "basis": basis,
        "comparable": bool(comparable),
        "evidence": evidence,
    }


def _infer_growth(
    fcf: float | None,
    prior_fcf: float | None,
) -> float | None:
    fcf_growth = _pct_change(fcf, prior_fcf)
    if fcf_growth is not None:
        return max(-0.20, min(0.20, fcf_growth / 100.0))
    return None


def _rate_input(value: Any, default: float) -> float | None:
    parsed = _finite(default if value is None else value)
    if parsed is not None and parsed > 1:
        parsed /= 100.0
    return parsed


def _dcf_case(
    starting_fcf: float,
    annual_growth: float,
    discount_rate: float,
    terminal_growth: float,
    *,
    forecast_years: int = 5,
) -> dict[str, Any] | None:
    if (
        starting_fcf <= 0
        or discount_rate <= 0
        or discount_rate <= terminal_growth
        or not all(
            math.isfinite(value)
            for value in (
                starting_fcf,
                annual_growth,
                discount_rate,
                terminal_growth,
            )
        )
    ):
        return None
    projected = starting_fcf
    present_value = 0.0
    forecast: list[dict[str, Any]] = []
    for year in range(1, forecast_years + 1):
        projected *= 1.0 + annual_growth
        discounted = projected / ((1.0 + discount_rate) ** year)
        forecast.append({"year": year, "fcf": projected, "present_value": discounted})
        present_value += discounted
    terminal_value = (
        projected * (1.0 + terminal_growth) / (discount_rate - terminal_growth)
    )
    present_value_of_terminal = terminal_value / (
        (1.0 + discount_rate) ** forecast_years
    )
    return {
        "forecast": forecast,
        "terminal_value": terminal_value,
        "present_value_of_terminal": present_value_of_terminal,
        "enterprise_value": present_value + present_value_of_terminal,
    }


def _valuation_sensitivity(
    *,
    starting_fcf: float | None,
    annual_growth: float | None,
    discount_rate: float | None,
    terminal_growth: float | None,
    net_debt: float | None,
    shares: float | None,
    reason: str | None,
) -> dict[str, Any]:
    if (
        starting_fcf is None
        or annual_growth is None
        or discount_rate is None
        or terminal_growth is None
        or _dcf_case(
            starting_fcf,
            annual_growth,
            discount_rate,
            terminal_growth,
        )
        is None
    ):
        return {
            "status": "unavailable",
            "reason": reason or "base DCF assumptions are unavailable",
            "wacc_terminal_grid": [],
            "drivers": {},
            "range": {
                "enterprise_value_min": None,
                "enterprise_value_max": None,
                "per_share_min": None,
                "per_share_max": None,
            },
            "largest_range_driver": None,
        }

    def values(*items: float) -> list[float]:
        return sorted({round(item, 10) for item in items if math.isfinite(item)})

    def case(
        fcf_value: float,
        growth_value: float,
        wacc_value: float,
        terminal_value: float,
    ) -> dict[str, Any] | None:
        result = _dcf_case(
            fcf_value,
            growth_value,
            wacc_value,
            terminal_value,
        )
        if result is None:
            return None
        enterprise_value = result["enterprise_value"]
        equity_value = enterprise_value - net_debt if net_debt is not None else None
        per_share = (
            equity_value / shares
            if equity_value is not None and shares is not None and shares > 0
            else None
        )
        return {
            "starting_fcf": fcf_value,
            "annual_growth": growth_value,
            "discount_rate": wacc_value,
            "terminal_growth": terminal_value,
            "enterprise_value": enterprise_value,
            "equity_value": equity_value,
            "per_share": per_share,
        }

    wacc_values = values(
        max(0.04, terminal_growth + 0.005, discount_rate - 0.02),
        discount_rate,
        min(0.25, discount_rate + 0.02),
    )
    terminal_values = values(
        max(-0.02, terminal_growth - 0.01),
        terminal_growth,
        min(discount_rate - 0.005, 0.08, terminal_growth + 0.01),
    )
    fcf_values = values(starting_fcf * 0.8, starting_fcf, starting_fcf * 1.2)
    growth_values = values(
        max(-0.20, annual_growth - 0.05),
        annual_growth,
        min(0.20, annual_growth + 0.05),
    )

    grid = [
        output
        for wacc_value in wacc_values
        for terminal_value in terminal_values
        if (
            output := case(
                starting_fcf,
                annual_growth,
                wacc_value,
                terminal_value,
            )
        )
        is not None
    ]
    driver_inputs = {
        "discount_rate": [
            (starting_fcf, annual_growth, value, terminal_growth)
            for value in wacc_values
        ],
        "terminal_growth": [
            (starting_fcf, annual_growth, discount_rate, value)
            for value in terminal_values
        ],
        "starting_fcf": [
            (value, annual_growth, discount_rate, terminal_growth)
            for value in fcf_values
        ],
        "annual_growth": [
            (starting_fcf, value, discount_rate, terminal_growth)
            for value in growth_values
        ],
    }
    drivers: dict[str, list[dict[str, Any]]] = {}
    spreads: dict[str, float] = {}
    for driver, inputs in driver_inputs.items():
        outputs = [
            output
            for input_values in inputs
            if (output := case(*input_values)) is not None
        ]
        drivers[driver] = outputs
        enterprise_values = [output["enterprise_value"] for output in outputs]
        spreads[driver] = (
            max(enterprise_values) - min(enterprise_values)
            if enterprise_values
            else 0.0
        )

    all_cases = grid + [item for outputs in drivers.values() for item in outputs]
    enterprise_values = [item["enterprise_value"] for item in all_cases]
    per_share_values = [
        item["per_share"] for item in all_cases if item["per_share"] is not None
    ]
    return {
        "status": "calculated" if per_share_values else "enterprise_value_only",
        "reason": (
            None
            if per_share_values
            else "net debt and positive shares are required for per-share sensitivity"
        ),
        "method": (
            "base assumptions varied independently across starting FCF, annual "
            "growth, discount rate, and terminal growth; grid combines discount "
            "rate and terminal growth"
        ),
        "wacc_terminal_grid": grid,
        "drivers": drivers,
        "range": {
            "enterprise_value_min": min(enterprise_values),
            "enterprise_value_max": max(enterprise_values),
            "per_share_min": min(per_share_values) if per_share_values else None,
            "per_share_max": max(per_share_values) if per_share_values else None,
        },
        "largest_range_driver": (
            max(spreads, key=lambda name: (spreads[name], name)) if spreads else None
        ),
    }


def _valuation(
    current_facts: Any,
    market_inputs: Mapping[str, Any],
    fcf: float | None,
    prior_fcf: float | None,
    fcf_record: Mapping[str, Any],
    prior_fcf_record: Mapping[str, Any],
) -> dict[str, Any]:
    raw_price = _effective_value(current_facts, "market_price", market_inputs)
    price = raw_price if raw_price is not None and raw_price > 0 else None
    raw_shares = _effective_value(current_facts, "shares_outstanding", market_inputs)
    shares = raw_shares if raw_shares is not None and raw_shares > 0 else None
    net_debt = _effective_value(current_facts, "net_debt", market_inputs)
    eps = _metric_value(current_facts, "diluted_eps")
    net_income = _metric_value(current_facts, "net_income")
    market_cap_override = _finite(market_inputs.get("market_cap"))
    market_cap = (
        market_cap_override
        if market_cap_override is not None
        else (price * shares if price is not None and shares is not None else None)
    )
    pe = (
        price / eps
        if price is not None and eps is not None and eps > 0 and price > 0
        else None
    )
    pe_method = "price_eps" if pe is not None else None
    if (
        pe is None
        and market_cap is not None
        and net_income is not None
        and net_income > 0
    ):
        pe = market_cap / net_income
        pe_method = "market_cap_net_income"

    wacc = _rate_input(
        market_inputs.get("discount_rate", market_inputs.get("wacc")), 0.10
    )
    terminal_growth = _rate_input(market_inputs.get("terminal_growth"), 0.03)
    growth_cap = 0.20
    fcf_period = fcf_record.get("period")
    prior_fcf_period = prior_fcf_record.get("period")
    fcf_basis = _annual_fcf_basis(fcf_period)
    prior_fcf_basis = _annual_fcf_basis(prior_fcf_period)
    same_reported_period = (
        str(fcf_period).strip().casefold()
        == str(prior_fcf_period).strip().casefold()
    )
    eligible_fcf = fcf if fcf_basis is not None else None
    eligible_prior_fcf = (
        prior_fcf
        if fcf_basis is not None
        and prior_fcf_basis == fcf_basis
        and not same_reported_period
        else None
    )
    inferred_growth = _infer_growth(eligible_fcf, eligible_prior_fcf)
    growth_basis = (
        f"{fcf_basis}_fcf"
        if inferred_growth is not None and fcf_basis is not None
        else None
    )
    forecast: list[dict[str, Any]] = []
    enterprise_value = None
    terminal_value = None
    present_value_of_terminal = None
    if fcf is None:
        dcf_reason = "starting FCF unavailable"
    elif fcf_basis is None:
        dcf_reason = "starting FCF must be annual, TTM, LTM, or 12-month"
    elif fcf <= 0:
        dcf_reason = "starting FCF is not positive"
    elif inferred_growth is None:
        dcf_reason = "comparable annual FCF growth unavailable"
    elif (
        wacc is None or terminal_growth is None or wacc <= terminal_growth or wacc <= 0
    ):
        dcf_reason = "discount rate must exceed terminal growth"
    else:
        dcf_reason = None
    model_reason = dcf_reason
    if dcf_reason is None:
        base_case = _dcf_case(eligible_fcf, inferred_growth, wacc, terminal_growth)
        if base_case is None:
            dcf_reason = "base DCF assumptions are invalid"
            model_reason = dcf_reason
        else:
            forecast = base_case["forecast"]
            terminal_value = base_case["terminal_value"]
            present_value_of_terminal = base_case["present_value_of_terminal"]
            enterprise_value = base_case["enterprise_value"]
    equity_value = (
        enterprise_value - net_debt
        if enterprise_value is not None and net_debt is not None
        else None
    )
    per_share = (
        equity_value / shares
        if equity_value is not None and shares is not None and shares != 0
        else None
    )
    if enterprise_value is not None and per_share is None:
        dcf_reason = "net debt and positive shares are required for per-share value"
    sensitivity = _valuation_sensitivity(
        starting_fcf=eligible_fcf,
        annual_growth=inferred_growth,
        discount_rate=wacc,
        terminal_growth=terminal_growth,
        net_debt=net_debt,
        shares=shares,
        reason=model_reason,
    )
    assumptions = {
        "forecast_years": 5,
        "wacc": wacc,
        "discount_rate": wacc,
        "terminal_growth": terminal_growth,
        "growth_cap": growth_cap,
        "inferred_growth": inferred_growth,
        "growth_basis": growth_basis,
        "starting_fcf": eligible_fcf,
        "starting_fcf_period": fcf_period,
        "starting_fcf_basis": fcf_basis,
        "net_debt": net_debt,
        "shares_outstanding": shares,
    }
    dcf = {
        "status": (
            "calculated"
            if per_share is not None
            else "enterprise_value_only"
            if enterprise_value is not None
            else "unavailable"
        ),
        "reason": dcf_reason,
        "forecast": forecast,
        "terminal_value": terminal_value,
        "present_value_of_terminal": present_value_of_terminal,
        "enterprise_value": enterprise_value,
        "equity_value": equity_value,
        "per_share": per_share,
        "assumptions": assumptions,
        "sensitivity": sensitivity,
    }
    margin_of_safety = (
        1.0 - price / per_share
        if price is not None and per_share is not None and per_share > 0
        else None
    )
    return {
        "fcf": fcf,
        "fcf_period": fcf_period,
        "fcf_basis": fcf_basis,
        "pe": pe,
        "pe_ratio": pe,
        "pe_method": pe_method,
        "market_cap": market_cap,
        "market_price": price,
        "dcf": dcf,
        "dcf_per_share": per_share,
        "intrinsic_value": per_share,
        "margin_of_safety": margin_of_safety,
        "assumptions": assumptions,
    }


def _news_crowding(news_items: Any) -> tuple[int, bool]:
    if not isinstance(news_items, (list, tuple)):
        return 0, False
    count = len(news_items)
    # Three or more contemporaneous items is a deliberately simple, auditable
    # proxy for crowded attention; no sentiment is inferred from headlines.
    return count, count >= 3


def _state(
    score: int,
    previous_state: Any,
    prior_analysis_count: Any,
    valuation: Mapping[str, Any],
    news_items: Any,
) -> str:
    if score <= -2:
        return "weakening"
    if score < 2:
        base = "monitor"
    elif score < 5:
        base = "forming"
    elif score < 8:
        base = "confirmed"
    else:
        base = "accelerating"
    previous_mapping = _mapping(previous_state)
    previous = str(previous_mapping.get("stage") or previous_state or "").lower()
    count = _finite(prior_analysis_count) or 0
    _, crowded_news = _news_crowding(news_items)
    pe = _finite(valuation.get("pe"))
    dcf = _mapping(valuation.get("dcf"))
    dcf_per_share = _finite(dcf.get("per_share"))
    market_price = _finite(valuation.get("market_price"))
    valuation_crowded = (pe is not None and pe >= 25) or (
        dcf_per_share is not None
        and dcf_per_share > 0
        and market_price is not None
        and market_price >= dcf_per_share * 1.20
    )
    if (
        score >= 5
        and previous in {"confirmed", "accelerating", "mature"}
        and count >= 2
        and valuation_crowded
        and crowded_news
    ):
        return "mature"
    if previous == "weakening" and score < 2:
        return "weakening"
    return base


def build_deterministic_analysis(
    current_facts: Mapping[str, Any] | None,
    previous_facts: Mapping[str, Any] | None = None,
    market_inputs: Mapping[str, Any] | None = None,
    previous_state: str | None = None,
    prior_analysis_count: int = 0,
    news_items: Any = None,
) -> dict[str, Any]:
    """Build the complete deterministic analysis object from extracted facts."""
    current_facts = current_facts if isinstance(current_facts, Mapping) else {}
    previous_facts = previous_facts if isinstance(previous_facts, Mapping) else None
    market_inputs = market_inputs if isinstance(market_inputs, Mapping) else {}

    metrics: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for name in STANDARD_METRICS:
        metrics[name] = _metric_output(current_facts, previous_facts, name)
    override_units = {
        "market_price": "per share",
        "shares_outstanding": "report scale",
        "net_debt": "report currency",
    }
    for name, fallback_unit in override_units.items():
        override = _finite(market_inputs.get(name))
        if override is None:
            continue
        prior_value = metrics.setdefault(
            name, _metric_output(current_facts, previous_facts, name)
        )["prior_value"]
        source = str(market_inputs.get(f"{name}_source") or "manual_input").strip()
        period = str(market_inputs.get(f"{name}_period") or "valuation input").strip()
        evidence = str(
            market_inputs.get(f"{name}_evidence") or "manual valuation override"
        ).strip()
        unit = str(market_inputs.get(f"{name}_unit") or "").strip()
        metrics[name] = {
            **metrics[name],
            "value": override,
            "unit": unit or metrics[name]["unit"] or fallback_unit,
            "period": period[:120],
            "evidence": evidence[:500],
            "source": source[:120],
            "concept": None,
            "change": _change(override, prior_value),
            "change_pct": _pct_change(override, prior_value),
        }
    # Explicit reported free cash flow wins; OCF minus cash capex is the
    # fallback derivation, valid only when currency, scale, definition, and
    # period are compatible.
    explicit_fcf_record = _metric_record(current_facts, "free_cash_flow")
    revenue_record = _metric_record(current_facts, "revenue")
    revenue = _finite(revenue_record.get("value"))
    net_income_record = _metric_record(current_facts, "net_income")
    prior_revenue_record = (
        _metric_record(previous_facts, "revenue") if previous_facts else {}
    )
    prior_revenue = _finite(prior_revenue_record.get("value"))
    explicit_fcf = _finite(explicit_fcf_record.get("value"))
    ocf_record = _metric_record(current_facts, "operating_cash_flow")
    capex_record = _metric_record(current_facts, "capex")
    prior_ocf_record = (
        _metric_record(previous_facts, "operating_cash_flow")
        if previous_facts
        else {}
    )
    prior_capex_record = (
        _metric_record(previous_facts, "capex") if previous_facts else {}
    )
    comparable_prior_revenue = _prior_monetary_value(
        revenue_record, prior_revenue_record
    )
    comparable_prior_capex = _prior_monetary_value(
        capex_record, prior_capex_record
    )
    for name, current_value, comparable_prior in (
        ("revenue", revenue, comparable_prior_revenue),
        ("capex", _finite(capex_record.get("value")), comparable_prior_capex),
    ):
        metrics[name]["prior_value"] = comparable_prior
        metrics[name]["change"] = _change(current_value, comparable_prior)
        metrics[name]["change_pct"] = _pct_change(current_value, comparable_prior)
    prior_explicit_fcf_record = (
        _metric_record(previous_facts, "free_cash_flow") if previous_facts else {}
    )
    prior_explicit_fcf = _finite(prior_explicit_fcf_record.get("value"))

    def _aligned_difference(
        first: Mapping[str, Any], second: Mapping[str, Any]
    ) -> float | None:
        # Subtraction is only meaningful between identically-scaled operands:
        # a scale (or currency) mismatch fails closed instead of silently
        # mixing magnitudes. Ratio arithmetic normalizes; differences do not.
        factor = _monetary_compatible(
            first.get("unit"), second.get("unit")
        )
        first_value = _finite(first.get("value"))
        second_value = _finite(second.get("value"))
        if (
            factor is None
            or factor != 1.0
            or not _period_compatible(first.get("period"), second.get("period"))
            or first_value is None
            or second_value is None
        ):
            return None
        difference = first_value - second_value
        return difference if math.isfinite(difference) else None

    derived_fcf = _aligned_difference(ocf_record, capex_record)
    prior_derived_fcf = _aligned_difference(prior_ocf_record, prior_capex_record)
    fcf = explicit_fcf if explicit_fcf is not None else derived_fcf
    current_fcf_derived = explicit_fcf is None and derived_fcf is not None
    prior_fcf_derived = prior_explicit_fcf is None and prior_derived_fcf is not None

    # Explicit FCF keeps its reported metadata. Derived FCF inherits the OCF
    # dimensions because the subtraction above requires capex to match them.
    fcf_record = (
        explicit_fcf_record
        if explicit_fcf is not None and explicit_fcf_record
        else (
            {
                "value": derived_fcf,
                "unit": str(ocf_record.get("unit") or ""),
                "currency": ocf_record.get("currency"),
                "period": ocf_record.get("period"),
            }
            if derived_fcf is not None
            else {}
        )
    )
    prior_fcf_record = (
        prior_explicit_fcf_record
        if prior_explicit_fcf is not None
        else (
            {
                "value": prior_derived_fcf,
                "unit": str(prior_ocf_record.get("unit") or ""),
                "currency": prior_ocf_record.get("currency"),
                "period": prior_ocf_record.get("period"),
            }
            if prior_derived_fcf is not None
            else {}
        )
    )
    prior_fcf_candidate = (
        prior_explicit_fcf
        if prior_explicit_fcf is not None
        else prior_derived_fcf
    )
    prior_fcf = None
    if (
        fcf is not None
        and prior_fcf_candidate is not None
        and _fcf_definitions_compatible(
            fcf_record,
            prior_fcf_record,
            current_derived=current_fcf_derived,
            prior_derived=prior_fcf_derived,
        )
    ):
        prior_fcf = _prior_monetary_value(fcf_record, prior_fcf_record)
    prior_fcf_incompatible = (
        fcf is not None and prior_fcf_candidate is not None and prior_fcf is None
    )

    # Margin arithmetic never mutates reported facts: the raw FCF record is
    # aligned into the revenue record's scale through a throwaway local copy.
    fcf_margin = _ratio(_monetary_pair(revenue_record, fcf_record), revenue)
    prior_fcf_margin = (
        _ratio(
            _monetary_pair(prior_revenue_record, prior_fcf_record),
            prior_revenue,
        )
        if prior_fcf is not None
        else None
    )
    if explicit_fcf is not None and explicit_fcf_record:
        # Raw explicit FCF keeps the source value, unit, and provenance;
        # nothing overwrites the reported fact with a normalized copy.
        metrics["fcf"] = {
            "value": explicit_fcf,
            "unit": explicit_fcf_record.get("unit"),
            "period": explicit_fcf_record.get("period"),
            "evidence": _clean_evidence(explicit_fcf_record),
            "source": explicit_fcf_record.get("source"),
            "concept": explicit_fcf_record.get("concept"),
            "prior_value": prior_fcf,
            "change": _change(explicit_fcf, prior_fcf),
            "change_pct": _pct_change(explicit_fcf, prior_fcf),
        }
    else:
        metrics["fcf"] = _derived_metric(
            fcf,
            str(fcf_record.get("unit") or ""),
            [
                metrics["operating_cash_flow"]["evidence"],
                metrics["capex"]["evidence"],
            ],
            prior_fcf,
        )
        metrics["fcf"]["period"] = fcf_record.get("period")
    metrics["free_cash_flow"] = metrics["fcf"].copy()
    metrics["fcf_margin"] = _derived_metric(
        fcf_margin, "percent", metrics["fcf"]["evidence"], prior_fcf_margin
    )

    net_income = _canonical_metric_value(current_facts, "net_income")
    assets = _canonical_metric_value(current_facts, "total_assets")
    equity = _canonical_metric_value(current_facts, "equity")
    debt = _canonical_metric_value(current_facts, "total_debt")
    current_assets = _canonical_metric_value(current_facts, "current_assets")
    current_liabilities = _canonical_metric_value(current_facts, "current_liabilities")

    net_margin = _ratio(net_income, revenue)
    roa = _ratio(net_income, assets)
    roe = _ratio(net_income, equity)
    debt_to_equity = _ratio(debt, equity)
    current_ratio = _ratio(current_assets, current_liabilities)
    capex_intensity = _monetary_ratio(capex_record, revenue_record)
    cash_conversion = _monetary_ratio(ocf_record, net_income_record)
    fundamentals = {
        "net_margin_pct": net_margin,
        "return_on_assets_pct": roa,
        "return_on_equity_pct": roe,
        "debt_to_equity": (
            debt_to_equity / 100.0 if debt_to_equity is not None else None
        ),
        "current_ratio": (current_ratio / 100.0 if current_ratio is not None else None),
        "capex_to_revenue_pct": capex_intensity,
        "operating_cash_conversion": (
            cash_conversion / 100.0 if cash_conversion is not None else None
        ),
    }
    metrics["net_margin"] = _derived_metric(
        fundamentals["net_margin_pct"],
        "percent",
        [metrics["net_income"]["evidence"], metrics["revenue"]["evidence"]],
    )
    metrics["return_on_equity"] = _derived_metric(
        fundamentals["return_on_equity_pct"],
        "percent",
        [metrics["net_income"]["evidence"], metrics["equity"]["evidence"]],
    )

    signals: OrderedDict[str, dict[str, Any]] = OrderedDict()
    revenue_comparable = comparable_prior_revenue is not None
    revenue_change = metrics["revenue"]["change_pct"]
    signals["revenue"] = _signal(
        "revenue",
        _direction_score(revenue_change, 2),
        revenue,
        comparable_prior_revenue,
        metrics["revenue"]["evidence"],
        comparable=revenue_comparable,
    )

    capex = _canonical_metric_value(current_facts, "capex")
    capex_comparable = comparable_prior_capex is not None
    capex_change = metrics["capex"]["change_pct"]
    signals["capex"] = _signal(
        "capex",
        _direction_score(capex_change, 2),
        capex,
        comparable_prior_capex,
        metrics["capex"]["evidence"],
        comparable=capex_comparable,
    )

    backlog = _metric_value(current_facts, "backlog")
    prior_backlog = _metric_value(previous_facts, "backlog") if previous_facts else None
    signals["backlog"] = _signal(
        "backlog",
        _direction_score(_pct_change(backlog, prior_backlog), 1),
        backlog,
        prior_backlog,
        metrics["backlog"]["evidence"],
    )

    inventory = _metric_value(current_facts, "inventory")
    prior_inventory = (
        _metric_value(previous_facts, "inventory") if previous_facts else None
    )
    inventory_growth = _pct_change(inventory, prior_inventory)
    revenue_growth = revenue_change
    inventory_gap = _change(inventory_growth, revenue_growth)
    inventory_score = (
        0
        if inventory_gap is None
        else (-2 if inventory_gap > 5 else -1 if inventory_gap > 0 else 1)
    )
    signals["inventory_vs_revenue"] = _signal(
        "inventory_vs_revenue",
        inventory_score,
        inventory,
        prior_inventory,
        metrics["inventory"]["evidence"],
    )

    fcf_comparable = prior_fcf is not None
    fcf_score = _direction_score(_pct_change(fcf, prior_fcf), 2)
    if fcf_score == 0 and fcf is not None and not prior_fcf_incompatible:
        fcf_score = 2 if fcf > 0 else -2 if fcf < 0 else 0
    signals["fcf"] = _signal(
        "fcf",
        fcf_score,
        fcf,
        prior_fcf,
        metrics["fcf"]["evidence"],
        comparable=fcf_comparable,
    )

    margin = _metric_value(current_facts, "gross_margin")
    prior_margin = (
        _metric_value(previous_facts, "gross_margin") if previous_facts else None
    )
    margin_delta = _change(margin, prior_margin)
    margin_threshold = (
        1.0
        if (margin is not None and abs(margin) > 1)
        or (prior_margin is not None and abs(prior_margin) > 1)
        else 0.02
    )
    margin_score = (
        0
        if margin_delta is None
        else 2
        if margin_delta >= margin_threshold
        else -2
        if margin_delta <= -margin_threshold
        else 1
        if margin_delta > 0
        else -1
    )
    signals["gross_margin_delta"] = _signal(
        "gross_margin_delta",
        margin_score,
        margin,
        prior_margin,
        metrics["gross_margin"]["evidence"],
    )

    qualitative_specs = (
        ("ai_demand", ("ai_demand", "ai"), False),
        (
            "data_centre_demand",
            (
                "data_centre_demand",
                "data_center_demand",
                "datacenter_demand",
                "datacentre_demand",
            ),
            False,
        ),
        ("supply_constraints", ("supply_constraints", "supply_constraint"), False),
        ("pricing_power", ("pricing_power",), False),
    )
    for name, aliases, negative in qualitative_specs:
        present, strength, evidence = _qualitative(current_facts, aliases)
        prior_present, prior_strength, _ = (
            _qualitative(previous_facts, aliases)
            if previous_facts
            else (None, None, [])
        )
        observed = present if present is not None else strength
        prior_observed = prior_present if prior_present is not None else prior_strength
        signals[name] = _signal(
            name,
            _qual_score(present, strength, negative=negative),
            observed,
            prior_observed,
            evidence,
            basis="report_qualitative",
        )

    guidance_up, _, guidance_up_evidence = _qualitative(current_facts, ("guidance_up",))
    guidance_down, _, guidance_down_evidence = _qualitative(
        current_facts, ("guidance_down",)
    )
    prior_guidance_up, _, _ = (
        _qualitative(previous_facts, ("guidance_up",))
        if previous_facts
        else (None, None, [])
    )
    prior_guidance_down, _, _ = (
        _qualitative(previous_facts, ("guidance_down",))
        if previous_facts
        else (None, None, [])
    )
    prior_direction = (
        "up" if prior_guidance_up else "down" if prior_guidance_down else None
    )
    direction = "up" if guidance_up else "down" if guidance_down else None
    if direction == "up":
        evidence = guidance_up_evidence
    elif direction == "down":
        evidence = guidance_down_evidence
    else:
        evidence = guidance_up_evidence or guidance_down_evidence or []
    direction_text = str(direction or "").lower()
    guidance_score = (
        2
        if direction_text in {"up", "raised", "raise", "positive", "higher"}
        else -2
        if direction_text in {"down", "cut", "lower", "negative", "reduced"}
        else 0
    )
    signals["guidance_direction"] = _signal(
        "guidance_direction",
        guidance_score,
        direction,
        prior_direction,
        evidence,
        basis="report_qualitative",
    )

    coverage_valid = _material_signal_coverage(current_facts, previous_facts)

    score = sum(item["score"] for item in signals.values())
    valuation = _valuation(
        current_facts,
        market_inputs,
        fcf,
        prior_fcf,
        fcf_record,
        prior_fcf_record,
    )
    currency_unit = str(metrics["fcf"]["unit"] or "")
    currency_code = next(
        (code for code in CURRENCY_CODES if currency_unit.upper().startswith(code)),
        "",
    )
    valuation["currency_unit"] = currency_unit
    valuation["per_share_unit"] = f"{currency_code}/share" if currency_code else None
    valuation["dcf"]["unit"] = currency_unit
    state = _state(score, previous_state, prior_analysis_count, valuation, news_items)
    previous_stage = (
        _mapping(previous_state).get("stage")
        if isinstance(previous_state, Mapping)
        else previous_state
    )

    drivers: list[str] = []
    risks: list[str] = []
    watch_items: list[str] = []
    labels = {
        "revenue": "revenue growth",
        "capex": "capital investment",
        "backlog": "backlog",
        "inventory_vs_revenue": "inventory relative to revenue",
        "fcf": "free cash flow",
        "gross_margin_delta": "gross margin",
        "ai_demand": "AI demand",
        "data_centre_demand": "data-centre demand",
        "supply_constraints": "supply constraints",
        "pricing_power": "pricing power",
        "guidance_direction": "guidance",
    }
    direct_observation_coverage = _direct_observation_coverage(current_facts)
    for name, item in signals.items():
        label = labels[name]
        if item["score"] > 0:
            drivers.append(label)
        elif item["score"] < 0:
            risks.append(label)
        if (
            item["observed_value"] is None
            and not direct_observation_coverage.get(name, False)
        ):
            watch_items.append(f"{label}: missing comparable evidence")
    supply_signal = signals["supply_constraints"]
    if supply_signal["score"] > 0 and "supply constraints" not in risks:
        risks.append("supply constraints")
    if valuation["pe"] is None:
        watch_items.append("valuation: P/E unavailable")
    if _mapping(valuation["dcf"]).get("per_share") is None:
        watch_items.append("valuation: DCF per-share value unavailable")
    news_count, crowded = _news_crowding(news_items)
    if crowded:
        watch_items.append(
            f"news attention: {news_count} items may indicate crowded expectations"
        )
    for name, record in _supplemental_metric_records(current_facts):
        if name in metrics:
            continue
        prior_record = (
            _mapping(previous_facts).get("metrics", {}).get(name, {})
            if previous_facts
            else {}
        )
        prior_value = _finite(
            prior_record.get("value") if isinstance(prior_record, Mapping) else None
        )
        metrics[name] = {
            "value": _finite(record.get("value")),
            "unit": record.get("unit"),
            "period": record.get("period"),
            "currency": record.get("currency"),
            "evidence": _clean_evidence(record),
            "source": record.get("source"),
            "concept": record.get("concept"),
            "source_url": record.get("source_url"),
            "source_location": record.get("source_location"),
            "available_at": record.get("available_at"),
            "change": _change(_finite(record.get("value")), prior_value),
            "change_pct": _pct_change(_finite(record.get("value")), prior_value),
        }

    coverage = {
        "eligible_for_high_states": all(coverage_valid.values()),
        "material_signals": dict(MATERIAL_FINANCE_SIGNALS),
        "covered": sorted(
            name for name, valid in coverage_valid.items() if valid
        ),
        "uncovered": sorted(
            name for name, valid in coverage_valid.items() if not valid
        ),
        "signals_covered": sum(1 for valid in coverage_valid.values() if valid),
        "signals_total": len(coverage_valid),
    }
    if state in {"confirmed", "accelerating", "mature"} and not coverage[
        "eligible_for_high_states"
    ]:
        state = "forming"
        coverage["cap_applied"] = True
    else:
        coverage["cap_applied"] = False
    transition = (
        "initial"
        if not previous_stage
        else "unchanged"
        if str(previous_stage).lower() == state
        else f"{str(previous_stage).lower()} -> {state}"
    )

    return {
        "metrics": metrics,
        "valuation": valuation,
        "fundamentals": fundamentals,
        "signals": signals,
        "score": score,
        "state": {
            "stage": state,
            "previous_stage": previous_stage,
            "score": score,
            "transition": transition,
            "coverage": coverage,
            "rule_version": "2",
        },
        "drivers": drivers,
        "risks": risks,
        "watch_items": watch_items,
    }
