"""Deterministic falsification and citation auditing for the thesis-fusion desk.

This module owns no SQL, no I/O, and no model calls: every check is a
deterministic pure calculation over one frozen ``ThesisSnapshot`` and the
bounded ``EvidenceSignal`` rows supplied alongside it. The repository layer
(``thesis_fusion.py``) consumes these APIs in a later integration wave;
durable jobs and UI are out of scope here.

Falsifier rules
---------------
- An evidence row with relationship ``invalidation`` deterministically
  breaches the thesis and is reported by evidence id.
- Machine-readable numeric/date conditions breach only when an observed value
  is present and fails the condition predicate (exact boundary semantics:
  ``>=``/``<=``/``==`` hold at equality, ``>``/``<``/``!=`` breach at
  equality). Missing observations are never invented: they surface as
  required data and threaten the thesis. A date condition whose deadline has
  passed with no observation is threatened (the observation is missing, so
  the thesis cannot be confirmed), never breached.
- Evidence observed more than ``STALE_EVIDENCE_HORIZON`` before the
  snapshot's ``as_of`` is stale: a threat plus a freshness data request.
  Stale evidence never breaches on its own.
- Scenario probability defects come from ``thesis_scoring.scenario_valuation``
  (missing probabilities are never defaulted to conviction; a probability sum
  off by more than ``PROBABILITY_TOLERANCE`` is reported); both threaten.
- Contradicting evidence threatens the thesis; its measured mass
  (``contradiction_strength`` from ``thesis_scoring.assess_evidence``) feeds
  the queue priority.
- Evidence-level checks operate on fingerprint-deduplicated rows (first
  occurrence): identical content repeated by any number of agents or
  syndicated sources is the same evidence and flags once. Agent/model
  agreement never counts anywhere.

Citation-auditor rules
----------------------
- Every claim must cite at least one supplied evidence id (bare id or
  ``evidence_type:id`` ref). Claims without citations fail as ``uncited``;
  citations that resolve to no supplied row fail as ``unknown_evidence``.
- Citations to evidence whose provenance names a model slug outside the
  supplied ``known_models`` registry are rejected (``unknown_model``). Source
  evidence without a model slug is accepted: only model attribution is
  registered, and agreement between models is never evidence.
- Duplicate origin: multiple distinct fingerprints sharing one ``origin_key``
  are flagged (``duplicate_origin``); identical content syndicated under any
  number of origins is the same evidence and is never duplication.
- The falsifier and citation audit are independent from any generator: they
  consume only the persisted snapshot and the supplied evidence rows.

Challenge runner
----------------
- An optional injected ``ChallengeRunner`` may propose counter-evidence or
  alternative mechanisms. Proposals must cite supplied evidence; proposals
  with unknown citations are rejected and the runner is marked failed.
  Runner exceptions are isolated: the deterministic decision is still
  produced and the failure is reported. The runner receives immutable inputs
  and can never mutate the thesis or its evidence.
- Valid proposals add ``ChallengeFinding`` entries and threaten the thesis;
  proposals never breach — only the deterministic checks breach.

Decision
--------
- ``state`` is intact / threatened / breached.
- ``recommended_priority`` derives deterministically: breached -> critical;
  threatened with contradiction_strength >= 0.5 or any citation failure ->
  high; threatened otherwise -> medium; intact -> low.
- Missing values stay unknown (reported as required data), inputs are
  bounded, and the same snapshot plus evidence rows reproduce an identical
  decision (point-in-time history safe).
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType
from typing import Any, Protocol

from research_intelligence.contracts import (
    MAX_ABS_RETURN,
    EvidenceSignal,
    Scenario,
    canonical_fingerprint,
)
from thesis_scoring import (
    MAX_SCENARIOS,
    ScenarioValuation,
    assess_evidence,
    scenario_valuation,
)

# Bounded finite inputs (mirror the scoring layer).
MAX_EVIDENCE = 256
MAX_CONDITIONS = 64
MAX_CLAIMS = 200
MAX_CITATIONS_PER_CLAIM = 50
MAX_RUNNER_FINDINGS = 32

# Deterministic thresholds.
STALE_EVIDENCE_HORIZON = timedelta(days=90)
PROBABILITY_TOLERANCE = 1e-9
CONTRADICTION_HIGH_THRESHOLD = 0.5

# Domain vocabularies.
STATES = ("intact", "threatened", "breached")
PRIORITY_LEVELS = ("critical", "high", "medium", "low")
PRIORITY_RANK = MappingProxyType({"critical": 0, "high": 1, "medium": 2, "low": 3})
DIRECTIONS = ("long", "short", "neutral")
CONDITION_KINDS = ("numeric", "date")
CONDITION_OPERATORS = (">=", ">", "<=", "<", "==", "!=")
CITATION_FAILURE_REASONS = (
    "uncited",
    "unknown_evidence",
    "unknown_model",
    "duplicate_origin",
)
REQUIRED_DATA_KINDS = (
    "evidence",
    "observation",
    "date_observation",
    "freshness",
    "probability",
    "normalization",
)
PROPOSAL_KINDS = ("counter_evidence", "alternative_mechanism")


def _as_utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ValueError("as_of must be datetime or ISO text")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_date(value: Any, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"invalid {field}") from None
    raise ValueError(f"invalid {field}")


def _finite_float(value: Any, field: str) -> float:
    if value is None or isinstance(value, bool) or isinstance(value, str):
        raise ValueError(f"invalid {field}")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"invalid {field}") from None
    if not math.isfinite(parsed):
        raise ValueError(f"invalid {field}")
    return parsed


def _bounded_text(value: Any, maximum: int, *, required: bool = False) -> str | None:
    text_value = " ".join(str(value or "").split())
    if required and not text_value:
        raise ValueError("text is required")
    return text_value[:maximum] if text_value else None


def _citation_list(
    value: Any, maximum: int = MAX_CITATIONS_PER_CLAIM
) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError("citations must be a list")
    if len(value) > maximum:
        raise ValueError("too many items")
    cleaned: list[str] = []
    for item in value:
        citation = _bounded_text(item, 240, required=True)
        assert citation is not None
        cleaned.append(citation)
    return tuple(cleaned)


@dataclass(frozen=True, slots=True)
class ThesisCondition:
    """One machine-readable thesis condition.

    ``kind`` is ``numeric`` or ``date``; ``operator`` is one of ``>=``, ``>``,
    ``<=``, ``<``, ``==``, ``!=`` against ``threshold``. ``observed`` is the
    measured value or None (unknown — never invented). A condition breaches
    only when an observed value is present and fails the predicate.
    """

    condition_id: str
    kind: str
    operator: str
    threshold: float | date
    observed: float | date | None
    unit: str | None

    @classmethod
    def create(
        cls,
        *,
        condition_id: Any,
        kind: Any,
        operator: Any,
        threshold: Any,
        observed: Any = None,
        unit: Any = None,
    ) -> ThesisCondition:
        identifier = _bounded_text(condition_id, 240, required=True)
        assert identifier is not None
        kind_value = _bounded_text(kind, 40, required=True)
        assert kind_value is not None
        if kind_value not in CONDITION_KINDS:
            raise ValueError("unsupported condition kind")
        operator_value = _bounded_text(operator, 8, required=True)
        assert operator_value is not None
        if operator_value not in CONDITION_OPERATORS:
            raise ValueError("unsupported condition operator")
        if kind_value == "numeric":
            threshold_value: float | date = _finite_float(threshold, "threshold")
            observed_value: float | date | None = (
                _finite_float(observed, "observed") if observed is not None else None
            )
        else:
            threshold_value = _to_date(threshold, "threshold")
            observed_value = (
                _to_date(observed, "observed") if observed is not None else None
            )
        return cls(
            condition_id=identifier,
            kind=kind_value,
            operator=operator_value,
            threshold=threshold_value,
            observed=observed_value,
            unit=_bounded_text(unit, 40),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "kind": self.kind,
            "operator": self.operator,
            "threshold": (
                self.threshold.isoformat()
                if isinstance(self.threshold, date)
                else self.threshold
            ),
            "observed": (
                self.observed.isoformat()
                if isinstance(self.observed, date)
                else self.observed
            ),
            "unit": self.unit,
        }


@dataclass(frozen=True, slots=True)
class ThesisClaim:
    """One thesis claim with citations into the supplied evidence set."""

    claim_id: str
    statement: str
    citations: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        claim_id: Any,
        statement: Any,
        citations: Any = None,
    ) -> ThesisClaim:
        identifier = _bounded_text(claim_id, 240, required=True)
        assert identifier is not None
        text = _bounded_text(statement, 2000, required=True)
        assert text is not None
        return cls(
            claim_id=identifier,
            statement=text,
            citations=_citation_list(citations),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "statement": self.statement,
            "citations": list(self.citations),
        }


@dataclass(frozen=True, slots=True)
class ThesisSnapshot:
    """One persisted thesis snapshot frozen at a point in time.

    Identity and history are anchored by ``as_of`` plus ``fingerprint``
    (canonical hash of the full snapshot payload), so re-auditing the same
    snapshot always reproduces the same decision.
    """

    thesis_id: str
    statement: str
    direction: str
    as_of: datetime
    cost: float
    conditions: tuple[ThesisCondition, ...]
    scenarios: tuple[Scenario, ...]
    claims: tuple[ThesisClaim, ...]

    @classmethod
    def create(
        cls,
        *,
        thesis_id: Any,
        statement: Any,
        direction: Any = "long",
        as_of: datetime | str,
        cost: Any = 0.0,
        conditions: Sequence[Any] | None = None,
        scenarios: Sequence[Any] | None = None,
        claims: Sequence[Any] | None = None,
    ) -> ThesisSnapshot:
        identifier = _bounded_text(thesis_id, 120, required=True)
        assert identifier is not None
        text = _bounded_text(statement, 4000, required=True)
        assert text is not None
        direction_value = _bounded_text(direction, 20, required=True)
        assert direction_value is not None
        if direction_value not in DIRECTIONS:
            raise ValueError("unsupported thesis direction")
        cost_value = _finite_float(cost, "cost")
        if abs(cost_value) > MAX_ABS_RETURN:
            raise ValueError(f"cost must be within +/-{MAX_ABS_RETURN:g}")
        condition_items = tuple(conditions or [])
        scenario_items = tuple(scenarios or [])
        claim_items = tuple(claims or [])
        for item in condition_items:
            if not isinstance(item, ThesisCondition):
                raise ValueError("conditions must be ThesisCondition")
        for item in scenario_items:
            if not isinstance(item, Scenario):
                raise ValueError("scenarios must be Scenario")
        for item in claim_items:
            if not isinstance(item, ThesisClaim):
                raise ValueError("claims must be ThesisClaim")
        return cls(
            thesis_id=identifier,
            statement=text,
            direction=direction_value,
            as_of=_as_utc(as_of),
            cost=cost_value,
            conditions=condition_items[:MAX_CONDITIONS],
            scenarios=scenario_items[:MAX_SCENARIOS],
            claims=claim_items[:MAX_CLAIMS],
        )

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "thesis_id": self.thesis_id,
            "statement": self.statement,
            "direction": self.direction,
            "as_of": self.as_of.isoformat(),
            "cost": self.cost,
            "conditions": [condition.to_dict() for condition in self.conditions],
            "scenarios": [scenario.to_dict() for scenario in self.scenarios],
            "claims": [claim.to_dict() for claim in self.claims],
        }


@dataclass(frozen=True, slots=True)
class CitationFailure:
    """One rejected citation.

    ``claim_id`` is the offending claim, or None for evidence-level failures
    (``duplicate_origin``). ``reason`` is one of ``CITATION_FAILURE_REASONS``
    and ``refs`` names the rejected evidence ids (empty for ``uncited``).
    """

    claim_id: str | None
    reason: str
    refs: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        claim_id: Any = None,
        reason: Any,
        refs: Any = None,
    ) -> CitationFailure:
        reason_value = _bounded_text(reason, 40, required=True)
        assert reason_value is not None
        if reason_value not in CITATION_FAILURE_REASONS:
            raise ValueError("unsupported citation failure reason")
        identifier = _bounded_text(claim_id, 240) if claim_id is not None else None
        return cls(
            claim_id=identifier,
            reason=reason_value,
            refs=_citation_list(refs, maximum=MAX_EVIDENCE),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "reason": self.reason,
            "refs": list(self.refs),
        }


@dataclass(frozen=True, slots=True)
class RequiredData:
    """One explicit missing input; the value stays unknown, never invented."""

    kind: str
    detail: str
    refs: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        kind: Any,
        detail: Any,
        refs: Any = None,
    ) -> RequiredData:
        kind_value = _bounded_text(kind, 40, required=True)
        assert kind_value is not None
        if kind_value not in REQUIRED_DATA_KINDS:
            raise ValueError("unsupported required data kind")
        text = _bounded_text(detail, 500, required=True)
        assert text is not None
        return cls(
            kind=kind_value,
            detail=text,
            refs=_citation_list(refs, maximum=MAX_EVIDENCE),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "refs": list(self.refs),
        }


@dataclass(frozen=True, slots=True)
class ChallengeProposal:
    """One runner proposal: counter-evidence or an alternative mechanism.

    ``citations`` must reference supplied evidence; the falsifier rejects
    proposals that cite unknown rows.
    """

    kind: str
    statement: str
    citations: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        kind: Any,
        statement: Any,
        citations: Any = None,
    ) -> ChallengeProposal:
        kind_value = _bounded_text(kind, 40, required=True)
        assert kind_value is not None
        if kind_value not in PROPOSAL_KINDS:
            raise ValueError("unsupported proposal kind")
        text = _bounded_text(statement, 2000, required=True)
        assert text is not None
        return cls(
            kind=kind_value,
            statement=text,
            citations=_citation_list(citations),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "statement": self.statement,
            "citations": list(self.citations),
        }


@dataclass(frozen=True, slots=True)
class ChallengeFinding:
    """A validated runner proposal that threatens the thesis."""

    kind: str
    statement: str
    citations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "statement": self.statement,
            "citations": list(self.citations),
        }


class ChallengeRunner(Protocol):
    """Optional injected challenger.

    Receives the immutable snapshot and evidence rows; must return a
    ``ChallengeProposal`` or None and must never mutate its inputs.
    """

    def challenge(
        self,
        snapshot: ThesisSnapshot,
        evidence: Sequence[EvidenceSignal],
    ) -> ChallengeProposal | None: ...


@dataclass(frozen=True, slots=True)
class FalsificationAudit:
    """Deterministic falsifier result (independent of the citation audit)."""

    invalidation_ids: tuple[str, ...]
    breached_condition_ids: tuple[str, ...]
    stale_evidence_ids: tuple[str, ...]
    contradiction_strength: float
    contradiction_count: int
    required_data: tuple[RequiredData, ...]
    valuation: ScenarioValuation | None
    breached: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "invalidation_ids": list(self.invalidation_ids),
            "breached_condition_ids": list(self.breached_condition_ids),
            "stale_evidence_ids": list(self.stale_evidence_ids),
            "contradiction_strength": self.contradiction_strength,
            "contradiction_count": self.contradiction_count,
            "required_data": [item.to_dict() for item in self.required_data],
            "valuation": self.valuation.to_dict()
            if self.valuation is not None
            else None,
            "breached": self.breached,
        }


@dataclass(frozen=True, slots=True)
class CitationAudit:
    """Deterministic citation-auditor result (independent of the falsifier)."""

    claim_count: int
    cited_evidence_count: int
    failures: tuple[CitationFailure, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_count": self.claim_count,
            "cited_evidence_count": self.cited_evidence_count,
            "failures": [failure.to_dict() for failure in self.failures],
        }


@dataclass(frozen=True, slots=True)
class RunnerAudit:
    """Isolated result of the optional injected challenger."""

    findings: tuple[ChallengeFinding, ...]
    rejected: tuple[CitationFailure, ...]
    failed: bool
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [finding.to_dict() for finding in self.findings],
            "rejected": [failure.to_dict() for failure in self.rejected],
            "failed": self.failed,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class ChallengeDecision:
    """Immutable falsification decision for one thesis snapshot.

    ``state`` is intact / threatened / breached; ``recommended_priority`` is
    derived deterministically (see module docstring). Every failure is
    explicit and bounded; missing inputs are reported through
    ``required_data`` and never invented.
    """

    thesis_id: str
    as_of: datetime
    snapshot_fingerprint: str
    state: str
    contradiction_strength: float
    invalidation_ids: tuple[str, ...]
    breached_condition_ids: tuple[str, ...]
    citation_failures: tuple[CitationFailure, ...]
    required_data: tuple[RequiredData, ...]
    valuation: ScenarioValuation | None
    runner_findings: tuple[ChallengeFinding, ...]
    runner_failed: bool
    runner_error: str | None
    recommended_priority: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "thesis_id": self.thesis_id,
            "as_of": self.as_of.isoformat(),
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "state": self.state,
            "contradiction_strength": self.contradiction_strength,
            "invalidation_ids": list(self.invalidation_ids),
            "breached_condition_ids": list(self.breached_condition_ids),
            "citation_failures": [
                failure.to_dict() for failure in self.citation_failures
            ],
            "required_data": [item.to_dict() for item in self.required_data],
            "valuation": self.valuation.to_dict()
            if self.valuation is not None
            else None,
            "runner_findings": [finding.to_dict() for finding in self.runner_findings],
            "runner_failed": self.runner_failed,
            "runner_error": self.runner_error,
            "recommended_priority": self.recommended_priority,
        }


def _holds(value: float | date, operator: str, threshold: float | date) -> bool:
    """Exact boundary semantics: inclusive operators hold at equality."""
    if operator == ">=":
        return value >= threshold
    if operator == ">":
        return value > threshold
    if operator == "<=":
        return value <= threshold
    if operator == "<":
        return value < threshold
    if operator == "==":
        return value == threshold
    return value != threshold


def _evidence_maps(
    evidence: Sequence[EvidenceSignal],
) -> tuple[dict[str, EvidenceSignal], dict[str, EvidenceSignal]]:
    id_map: dict[str, EvidenceSignal] = {}
    ref_map: dict[str, EvidenceSignal] = {}
    for signal in evidence:
        id_map.setdefault(signal.evidence_id, signal)
        ref_map.setdefault(signal.ref, signal)
    return id_map, ref_map


def _deduplicated(evidence: Sequence[EvidenceSignal]) -> tuple[EvidenceSignal, ...]:
    """First occurrence per fingerprint: identical content is one evidence."""
    seen: set[str] = set()
    unique: list[EvidenceSignal] = []
    for signal in evidence:
        if signal.evidence_fingerprint in seen:
            continue
        seen.add(signal.evidence_fingerprint)
        unique.append(signal)
    return tuple(unique)


def audit_falsification(
    snapshot: ThesisSnapshot,
    evidence: Sequence[EvidenceSignal],
    *,
    limit: int = MAX_EVIDENCE,
) -> FalsificationAudit:
    """Deterministic falsifier over one snapshot plus evidence rows.

    Checks invalidation relationships, breached numeric/date conditions,
    stale evidence, scenario probability defects, and contradiction mass.
    Missing values surface as ``RequiredData`` and never breach.
    """
    if not isinstance(snapshot, ThesisSnapshot):
        raise TypeError("snapshot must be a ThesisSnapshot")
    if not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    rows = tuple(evidence[:limit])
    unique = _deduplicated(rows)
    reference = snapshot.as_of

    score = assess_evidence(rows, limit=limit)
    contradiction_strength = score.contradiction_mass
    contradiction_count = score.contradiction_count

    invalidation_ids = tuple(
        signal.evidence_id for signal in unique if signal.relationship == "invalidation"
    )

    required: list[RequiredData] = []
    if not rows:
        required.append(
            RequiredData.create(
                kind="evidence",
                detail="thesis has no attached evidence",
            )
        )

    breached_condition_ids: list[str] = []
    for condition in snapshot.conditions:
        if condition.observed is None:
            if condition.kind == "date" and reference.date() > condition.threshold:
                required.append(
                    RequiredData.create(
                        kind="date_observation",
                        detail="date condition deadline passed without an observation",
                        refs=[condition.condition_id],
                    )
                )
            else:
                required.append(
                    RequiredData.create(
                        kind="observation",
                        detail="condition lacks an observed value",
                        refs=[condition.condition_id],
                    )
                )
        elif not _holds(condition.observed, condition.operator, condition.threshold):
            breached_condition_ids.append(condition.condition_id)

    stale_ids = tuple(
        signal.evidence_id
        for signal in unique
        if reference - signal.source_timestamp > STALE_EVIDENCE_HORIZON
    )
    if stale_ids:
        required.append(
            RequiredData.create(
                kind="freshness",
                detail=(
                    f"evidence older than {STALE_EVIDENCE_HORIZON.days} days at as_of"
                ),
                refs=list(stale_ids),
            )
        )

    valuation: ScenarioValuation | None = None
    if snapshot.scenarios:
        valuation = scenario_valuation(
            snapshot.scenarios, cost=snapshot.cost, limit=MAX_SCENARIOS
        )
        if valuation.missing_probability_count:
            required.append(
                RequiredData.create(
                    kind="probability",
                    detail="scenario probabilities are missing and never defaulted",
                    refs=list(valuation.missing_probability_labels),
                )
            )
        if not valuation.probabilities_sum_to_one:
            required.append(
                RequiredData.create(
                    kind="normalization",
                    detail=(
                        f"scenario probabilities sum to "
                        f"{valuation.probability_sum:.6g}, not 1"
                    ),
                    refs=[scenario.label for scenario in snapshot.scenarios],
                )
            )

    return FalsificationAudit(
        invalidation_ids=invalidation_ids,
        breached_condition_ids=tuple(breached_condition_ids),
        stale_evidence_ids=stale_ids,
        contradiction_strength=contradiction_strength,
        contradiction_count=contradiction_count,
        required_data=tuple(required),
        valuation=valuation,
        breached=bool(invalidation_ids or breached_condition_ids),
    )


def audit_citations(
    snapshot: ThesisSnapshot,
    evidence: Sequence[EvidenceSignal],
    *,
    known_models: Collection[str] | None = None,
    limit: int = MAX_EVIDENCE,
) -> CitationAudit:
    """Deterministic citation auditor over one snapshot plus evidence rows.

    Rejects uncited claims, citations to unknown evidence, citations to
    unregistered model provenance, and duplicate-origin evidence. Resolution
    accepts bare evidence ids and ``evidence_type:id`` refs.
    """
    if not isinstance(snapshot, ThesisSnapshot):
        raise TypeError("snapshot must be a ThesisSnapshot")
    if not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    rows = tuple(evidence[:limit])
    id_map, ref_map = _evidence_maps(rows)
    known = frozenset(str(model) for model in (known_models or ()))

    failures: list[CitationFailure] = []
    cited_ids: set[str] = set()
    for claim in snapshot.claims:
        if not claim.citations:
            failures.append(
                CitationFailure.create(
                    claim_id=claim.claim_id,
                    reason="uncited",
                )
            )
            continue
        missing = [
            citation
            for citation in claim.citations
            if citation not in id_map and citation not in ref_map
        ]
        if missing:
            failures.append(
                CitationFailure.create(
                    claim_id=claim.claim_id,
                    reason="unknown_evidence",
                    refs=missing,
                )
            )
        model_bad: list[str] = []
        for citation in claim.citations:
            signal = id_map.get(citation) or ref_map.get(citation)
            if signal is None:
                continue
            cited_ids.add(signal.evidence_id)
            slug = signal.provenance.get("model_slug")
            if isinstance(slug, str) and slug and slug not in known:
                model_bad.append(citation)
        if model_bad:
            failures.append(
                CitationFailure.create(
                    claim_id=claim.claim_id,
                    reason="unknown_model",
                    refs=model_bad,
                )
            )

    groups: dict[str, dict[str, list[str]]] = {}
    for signal in rows:
        if not signal.origin_key:
            continue
        fingerprints = groups.setdefault(signal.origin_key, {})
        fingerprints.setdefault(signal.evidence_fingerprint, []).append(
            signal.evidence_id
        )
    for origin in sorted(groups):
        fingerprint_groups = groups[origin]
        if len(fingerprint_groups) >= 2:
            duplicated = sorted(
                evidence_id
                for ids in fingerprint_groups.values()
                for evidence_id in ids
            )
            failures.append(
                CitationFailure.create(
                    reason="duplicate_origin",
                    refs=duplicated,
                )
            )

    return CitationAudit(
        claim_count=len(snapshot.claims),
        cited_evidence_count=len(cited_ids),
        failures=tuple(failures),
    )


def _run_challenger(
    runner: Any,
    snapshot: ThesisSnapshot,
    evidence: Sequence[EvidenceSignal],
    id_map: Mapping[str, EvidenceSignal],
    ref_map: Mapping[str, EvidenceSignal],
) -> RunnerAudit:
    """Invoke the optional runner; every failure mode is isolated."""
    if runner is None:
        return RunnerAudit(findings=(), rejected=(), failed=False, error=None)
    call = getattr(runner, "challenge", None)
    if call is None:
        return RunnerAudit(
            findings=(),
            rejected=(),
            failed=True,
            error="runner must provide a challenge() method",
        )
    try:
        proposal = call(snapshot, evidence)
    except Exception as exc:  # noqa: BLE001 - isolation is the contract
        return RunnerAudit(
            findings=(),
            rejected=(),
            failed=True,
            error=f"{type(exc).__name__}: {str(exc)[:400]}",
        )
    if proposal is None:
        return RunnerAudit(findings=(), rejected=(), failed=False, error=None)
    if not isinstance(proposal, ChallengeProposal):
        return RunnerAudit(
            findings=(),
            rejected=(),
            failed=True,
            error="runner must return ChallengeProposal or None",
        )
    unknown = [
        citation
        for citation in proposal.citations
        if citation not in id_map and citation not in ref_map
    ]
    if unknown:
        return RunnerAudit(
            findings=(),
            rejected=(
                CitationFailure.create(
                    reason="unknown_evidence",
                    refs=unknown,
                ),
            ),
            failed=True,
            error="proposal cites unknown evidence",
        )
    finding = ChallengeFinding(
        kind=proposal.kind,
        statement=proposal.statement,
        citations=proposal.citations,
    )
    return RunnerAudit(
        findings=(finding,),
        rejected=(),
        failed=False,
        error=None,
    )


def derive_priority(
    *,
    state: str,
    contradiction_strength: Any = None,
    citation_failure_count: Any = 0,
) -> str:
    """Deterministic queue priority from state and measured defects."""
    if state not in STATES:
        raise ValueError(f"unsupported state:{str(state)[:32]}")
    strength = (
        0.0
        if contradiction_strength is None
        else _finite_float(contradiction_strength, "contradiction_strength")
    )
    if not 0.0 <= strength <= 1.0:
        raise ValueError("contradiction_strength must be within [0, 1]")
    if (
        isinstance(citation_failure_count, bool)
        or not isinstance(citation_failure_count, int)
        or citation_failure_count < 0
    ):
        raise ValueError("citation_failure_count must be a non-negative integer")
    if state == "breached":
        return "critical"
    if state == "threatened":
        if strength >= CONTRADICTION_HIGH_THRESHOLD or citation_failure_count > 0:
            return "high"
        return "medium"
    return "low"


def challenge_thesis(
    snapshot: ThesisSnapshot,
    evidence: Sequence[EvidenceSignal],
    *,
    known_models: Collection[str] | None = None,
    runner: ChallengeRunner | None = None,
) -> ChallengeDecision:
    """Falsify and audit one persisted thesis snapshot end to end.

    Deterministic falsifier and citation audits run first and never depend on
    the optional runner; the runner's proposals are validated against the
    supplied evidence, and any runner failure is isolated and reported.
    """
    if not isinstance(snapshot, ThesisSnapshot):
        raise TypeError("snapshot must be a ThesisSnapshot")
    rows = tuple(evidence[:MAX_EVIDENCE])
    id_map, ref_map = _evidence_maps(rows)

    falsification = audit_falsification(snapshot, rows)
    citation = audit_citations(snapshot, rows, known_models=known_models)
    runner_audit = _run_challenger(runner, snapshot, rows, id_map, ref_map)

    breached = falsification.breached
    threatened = (
        falsification.contradiction_count > 0
        or bool(falsification.required_data)
        or bool(citation.failures)
        or bool(runner_audit.findings)
    )
    state = "breached" if breached else ("threatened" if threatened else "intact")
    priority = derive_priority(
        state=state,
        contradiction_strength=falsification.contradiction_strength,
        citation_failure_count=len(citation.failures) + len(runner_audit.rejected),
    )
    return ChallengeDecision(
        thesis_id=snapshot.thesis_id,
        as_of=snapshot.as_of,
        snapshot_fingerprint=snapshot.fingerprint,
        state=state,
        contradiction_strength=falsification.contradiction_strength,
        invalidation_ids=falsification.invalidation_ids,
        breached_condition_ids=falsification.breached_condition_ids,
        citation_failures=citation.failures + runner_audit.rejected,
        required_data=falsification.required_data,
        valuation=falsification.valuation,
        runner_findings=runner_audit.findings[:MAX_RUNNER_FINDINGS],
        runner_failed=runner_audit.failed,
        runner_error=runner_audit.error,
        recommended_priority=priority,
    )


__all__ = [
    "CITATION_FAILURE_REASONS",
    "CONDITION_KINDS",
    "CONDITION_OPERATORS",
    "CONTRADICTION_HIGH_THRESHOLD",
    "ChallengeDecision",
    "ChallengeFinding",
    "ChallengeProposal",
    "ChallengeRunner",
    "CitationAudit",
    "CitationFailure",
    "DIRECTIONS",
    "FalsificationAudit",
    "MAX_CITATIONS_PER_CLAIM",
    "MAX_CLAIMS",
    "MAX_CONDITIONS",
    "MAX_EVIDENCE",
    "MAX_RUNNER_FINDINGS",
    "PROBABILITY_TOLERANCE",
    "PROPOSAL_KINDS",
    "PRIORITY_LEVELS",
    "PRIORITY_RANK",
    "REQUIRED_DATA_KINDS",
    "RunnerAudit",
    "STATES",
    "STALE_EVIDENCE_HORIZON",
    "ThesisClaim",
    "ThesisCondition",
    "ThesisSnapshot",
    "audit_citations",
    "audit_falsification",
    "challenge_thesis",
    "derive_priority",
]
