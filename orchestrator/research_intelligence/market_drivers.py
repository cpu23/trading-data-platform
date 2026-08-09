"""Evidence-linked major-market driver validation and deterministic change detection."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from processors._validators import scan_prohibited_language
from research_intelligence.contracts import (
    DriverDirection,
    EconomicFactorDraft,
    FactorState,
    FactorTransmissionDraft,
    Horizon,
    MarketDriverDraft,
    NormalizedEvidence,
    Strength,
    canonical_fingerprint,
    evidence_catalog,
    reject_embedded_evidence_references,
    validate_evidence_references,
)
from research_intelligence.discovery import reject_unsupported_numeric_text

_DRIVER_KEYS = frozenset(
    {
        "target",
        "driver_key",
        "driver_label",
        "direction",
        "strength",
        "horizon",
        "mechanism",
        "evidence_ids",
        "invalidation_conditions",
        "confidence",
        "confidence_rationale",
    }
)
_KEY_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_FACTOR_KEYS = frozenset(
    {
        "factor_key",
        "factor_label",
        "state",
        "strength",
        "horizon",
        "mechanism",
        "evidence_ids",
        "confidence",
        "confidence_rationale",
        "invalidation_conditions",
        "transmissions",
    }
)
_TRANSMISSION_KEYS = frozenset(
    {"target", "direction", "mechanism", "invalidation_conditions"}
)


@dataclass(frozen=True, slots=True)
class FactorMarketAssessment:
    factors: tuple[EconomicFactorDraft, ...]
    drivers: tuple[MarketDriverDraft, ...]


def _text(value: Any, maximum: int, field: str, required: bool = False) -> str | None:
    cleaned = " ".join(str(value or "").split())
    if required and not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned[:maximum] if cleaned else None


def _confidence(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("driver confidence must be numeric or null")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError("driver confidence must be numeric or null") from None
    if not math.isfinite(parsed) or not 0 <= parsed <= 1:
        raise ValueError("driver confidence must be between 0 and 1")
    return parsed


def _conditions(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError("driver invalidation conditions exceed bound")
    output: list[str] = []
    for item in value:
        condition = _text(item, 400, "invalidation condition", required=True)
        if condition not in output:
            output.append(condition)
    return tuple(output)


def _prior_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    evidence_ids = row.get("evidence_ids")
    if not isinstance(evidence_ids, (list, tuple)):
        evidence_ids = []
    conditions = row.get("invalidation_conditions")
    if not isinstance(conditions, (list, tuple)):
        conditions = []
    return (
        row.get("direction"),
        row.get("strength"),
        row.get("horizon"),
        " ".join(str(row.get("mechanism") or "").split()).casefold(),
        tuple(sorted(str(value) for value in evidence_ids)),
        tuple(" ".join(str(value).split()).casefold() for value in conditions),
    )


def validate_market_driver_output(
    output: Any,
    evidence: Sequence[NormalizedEvidence],
    market_universe: Sequence[str],
    *,
    prior_drivers: Sequence[Mapping[str, Any]] = (),
    maximum_drivers: int = 24,
) -> tuple[MarketDriverDraft, ...]:
    if not isinstance(output, Mapping) or set(output) != {"abstained", "drivers"}:
        raise ValueError("market-driver output must contain exactly abstained and drivers")
    reject_embedded_evidence_references(output)
    if not isinstance(output.get("abstained"), bool):
        raise ValueError("market-driver abstained flag must be boolean")
    raw_drivers = output.get("drivers")
    if not isinstance(raw_drivers, list) or len(raw_drivers) > maximum_drivers:
        raise ValueError("market-driver count exceeds configured bound")
    if output["abstained"]:
        if raw_drivers:
            raise ValueError("abstained market-driver output cannot include drivers")
        return ()
    catalog = evidence_catalog(evidence)
    allowed_targets = {str(value).strip().upper() for value in market_universe}
    prior = {
        (str(row.get("target") or "").upper(), str(row.get("driver_key") or "")): row
        for row in prior_drivers[:200]
    }
    drafts: list[MarketDriverDraft] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_drivers:
        if not isinstance(raw, Mapping) or set(raw) != _DRIVER_KEYS:
            raise ValueError("market-driver keys do not match the strict contract")
        target = str(raw.get("target") or "").strip().upper()
        if target not in allowed_targets:
            raise ValueError(f"market driver target is not configured: {target[:80]}")
        driver_key = str(raw.get("driver_key") or "").strip().casefold()
        if not _KEY_RE.fullmatch(driver_key) or len(driver_key) > 100:
            raise ValueError("market driver key is invalid")
        identity = (target, driver_key)
        if identity in seen:
            raise ValueError("duplicate market driver")
        seen.add(identity)
        direction = str(raw.get("direction") or "").strip().casefold()
        if direction not in {item.value for item in DriverDirection}:
            raise ValueError("market driver direction is invalid")
        strength = str(raw.get("strength") or "").strip().casefold()
        if strength not in {item.value for item in Strength}:
            raise ValueError("market driver strength is invalid")
        horizon = str(raw.get("horizon") or "").strip().casefold()
        if horizon not in {item.value for item in Horizon}:
            raise ValueError("market driver horizon is invalid")
        references = validate_evidence_references(raw.get("evidence_ids"), catalog)
        if not references:
            raise ValueError("market drivers require supplied evidence")
        label = _text(raw.get("driver_label"), 160, "driver label", required=True)
        mechanism = _text(raw.get("mechanism"), 800, "driver mechanism", required=True)
        conditions = _conditions(raw.get("invalidation_conditions"))
        rationale = _text(
            raw.get("confidence_rationale"), 500, "confidence rationale", required=True
        )
        if scan_prohibited_language(raw):
            raise ValueError("market driver contains prohibited advisory language")
        reject_unsupported_numeric_text(
            {
                "driver_label": label,
                "mechanism": mechanism,
                "invalidation_conditions": conditions,
                "confidence_rationale": rationale,
            },
            evidence,
        )
        candidate_key = (
            direction,
            strength,
            horizon,
            mechanism.casefold(),
            tuple(sorted(references)),
            tuple(condition.casefold() for condition in conditions),
        )
        prior_row = prior.get(identity)
        changed = prior_row is None or _prior_key(prior_row) != candidate_key
        drafts.append(
            MarketDriverDraft(
                target=target,
                driver_key=driver_key,
                driver_label=label,
                direction=direction,
                strength=strength,
                horizon=horizon,
                mechanism=mechanism,
                evidence_ids=references,
                changed_since_prior=changed,
                invalidation_conditions=conditions,
                confidence=_confidence(raw.get("confidence")),
                confidence_rationale=rationale,
            )
        )
    return tuple(drafts)


def validate_factor_market_output(
    output: Any,
    evidence: Sequence[NormalizedEvidence],
    market_universe: Sequence[str],
    *,
    prior_drivers: Sequence[Mapping[str, Any]] = (),
    maximum_factors: int = 8,
    maximum_drivers: int = 8,
) -> FactorMarketAssessment:
    """Validate one factor state once, then its bounded target transmissions."""
    if not isinstance(output, Mapping) or set(output) != {"abstained", "factors"}:
        raise ValueError("factor-market output must contain exactly abstained and factors")
    reject_embedded_evidence_references(output)
    if not isinstance(output.get("abstained"), bool):
        raise ValueError("factor-market abstained flag must be boolean")
    raw_factors = output.get("factors")
    if not isinstance(raw_factors, list) or len(raw_factors) > maximum_factors:
        raise ValueError("economic factor count exceeds configured bound")
    if output["abstained"]:
        if raw_factors:
            raise ValueError("abstained factor-market output cannot include factors")
        return FactorMarketAssessment((), ())

    catalog = evidence_catalog(evidence)
    allowed_targets = {str(value).strip().upper() for value in market_universe}
    prior = {
        (str(row.get("target") or "").upper(), str(row.get("driver_key") or "")): row
        for row in prior_drivers[:200]
    }
    factors: list[EconomicFactorDraft] = []
    drivers: list[MarketDriverDraft] = []
    seen_factors: set[str] = set()
    for raw in raw_factors:
        if not isinstance(raw, Mapping) or set(raw) != _FACTOR_KEYS:
            raise ValueError("economic factor keys do not match the strict contract")
        factor_key = str(raw.get("factor_key") or "").strip().casefold()
        if not _KEY_RE.fullmatch(factor_key) or len(factor_key) > 100:
            raise ValueError("economic factor key is invalid")
        if factor_key in seen_factors:
            raise ValueError("duplicate economic factor")
        seen_factors.add(factor_key)
        factor_label = _text(
            raw.get("factor_label"), 160, "economic factor label", required=True
        )
        state = str(raw.get("state") or "").strip().casefold()
        if state not in {item.value for item in FactorState}:
            raise ValueError("economic factor state is invalid")
        strength = str(raw.get("strength") or "").strip().casefold()
        if strength not in {item.value for item in Strength}:
            raise ValueError("economic factor strength is invalid")
        horizon = str(raw.get("horizon") or "").strip().casefold()
        if horizon not in {item.value for item in Horizon}:
            raise ValueError("economic factor horizon is invalid")
        mechanism = _text(
            raw.get("mechanism"), 800, "economic factor mechanism", required=True
        )
        references = validate_evidence_references(raw.get("evidence_ids"), catalog)
        if not references:
            raise ValueError("economic factors require supplied evidence")
        factor_conditions = _conditions(raw.get("invalidation_conditions"))
        rationale = _text(
            raw.get("confidence_rationale"),
            500,
            "economic factor confidence rationale",
            required=True,
        )
        raw_transmissions = raw.get("transmissions")
        if not isinstance(raw_transmissions, list) or len(raw_transmissions) > 8:
            raise ValueError("factor transmission count exceeds bound")
        if not raw_transmissions:
            raise ValueError("economic factor requires a target transmission")
        transmissions: list[FactorTransmissionDraft] = []
        seen_targets: set[str] = set()
        for raw_transmission in raw_transmissions:
            if (
                not isinstance(raw_transmission, Mapping)
                or set(raw_transmission) != _TRANSMISSION_KEYS
            ):
                raise ValueError("factor transmission keys do not match strict contract")
            target = str(raw_transmission.get("target") or "").strip().upper()
            if target not in allowed_targets:
                raise ValueError(
                    f"factor transmission target is not configured: {target[:80]}"
                )
            if target in seen_targets:
                raise ValueError("duplicate target transmission for economic factor")
            seen_targets.add(target)
            direction = str(
                raw_transmission.get("direction") or ""
            ).strip().casefold()
            if direction not in {item.value for item in DriverDirection}:
                raise ValueError("factor transmission direction is invalid")
            transmission_mechanism = _text(
                raw_transmission.get("mechanism"),
                800,
                "factor transmission mechanism",
                required=True,
            )
            transmission_conditions = _conditions(
                raw_transmission.get("invalidation_conditions")
            )
            transmissions.append(
                FactorTransmissionDraft(
                    target=target,
                    direction=direction,
                    mechanism=transmission_mechanism,
                    invalidation_conditions=transmission_conditions,
                )
            )
            all_conditions = tuple(
                dict.fromkeys((*factor_conditions, *transmission_conditions))
            )
            candidate_key = (
                direction,
                strength,
                horizon,
                transmission_mechanism.casefold(),
                tuple(sorted(references)),
                tuple(condition.casefold() for condition in all_conditions),
            )
            prior_row = prior.get((target, factor_key))
            drivers.append(
                MarketDriverDraft(
                    target=target,
                    driver_key=factor_key,
                    driver_label=factor_label,
                    direction=direction,
                    strength=strength,
                    horizon=horizon,
                    mechanism=transmission_mechanism,
                    evidence_ids=references,
                    changed_since_prior=(
                        prior_row is None or _prior_key(prior_row) != candidate_key
                    ),
                    invalidation_conditions=all_conditions,
                    confidence=_confidence(raw.get("confidence")),
                    confidence_rationale=rationale,
                )
            )
        reject_unsupported_numeric_text(
            {
                "factor_label": factor_label,
                "mechanism": mechanism,
                "confidence_rationale": rationale,
                "invalidation_conditions": factor_conditions,
                "transmissions": [
                    {
                        "mechanism": item.mechanism,
                        "invalidation_conditions": item.invalidation_conditions,
                    }
                    for item in transmissions
                ],
            },
            evidence,
        )
        factors.append(
            EconomicFactorDraft(
                factor_key=factor_key,
                factor_label=factor_label,
                state=state,
                strength=strength,
                horizon=horizon,
                mechanism=mechanism,
                evidence_ids=references,
                confidence=_confidence(raw.get("confidence")),
                confidence_rationale=rationale,
                invalidation_conditions=factor_conditions,
                transmissions=tuple(transmissions),
            )
        )
    if len(drivers) > maximum_drivers:
        raise ValueError("market driver count exceeds configured bound")
    if scan_prohibited_language(output):
        raise ValueError("factor-market output contains prohibited advisory language")
    return FactorMarketAssessment(tuple(factors), tuple(drivers))


def market_driver_input_fingerprint(
    evidence: Sequence[NormalizedEvidence], market_universe: Sequence[str]
) -> str:
    return canonical_fingerprint(
        {
            "markets": [str(value).upper() for value in market_universe],
            "evidence": [item.content_fingerprint for item in evidence],
            "prompt": "macro_transmission_v3",
        }
    )


__all__ = [
    "FactorMarketAssessment",
    "market_driver_input_fingerprint",
    "validate_factor_market_output",
    "validate_market_driver_output",
]
