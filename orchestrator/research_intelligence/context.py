"""Unified live/replay context and deterministic point-in-time leakage guards."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from research_intelligence.contracts import NormalizedEvidence, canonical_fingerprint

_MAX_AUDIT_DETAILS = 200
_BENCHMARK_ONLY_KEYS = frozenset(
    {
        "expected_developments",
        "plausible_second_order_areas",
        "expected_unknowns",
        "forbidden_hindsight",
        "human_annotations",
        "manual_milestones",
    }
)
_TIMESTAMP_KEYS = frozenset(
    {
        "available_at",
        "published_at",
        "released_at",
        "source_timestamp",
        "target_at",
        "observed_at",
        "valid_from",
    }
)
_REFERENCE_KEYS = frozenset(
    {
        "evidence_ref",
        "evidence_ids",
        "supporting_evidence_ids",
        "contradicting_evidence_ids",
        "context_evidence_ids",
    }
)


class ResearchMode(StrEnum):
    LIVE = "live"
    REPLAY = "replay"


class ReplayLeakageError(ValueError):
    """Raised before a model call or successful replay when integrity fails."""


def _utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    elif isinstance(value, datetime):
        parsed = value
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


@dataclass(slots=True)
class ReplayAudit:
    replay_as_of: datetime
    evidence_considered: int = 0
    evidence_included: int = 0
    evidence_excluded_as_future: int = 0
    integrity_exclusions: int = 0
    revisions_vintages_excluded: int = 0
    reaction_windows_excluded: int = 0
    versions_excluded: int = 0
    earliest_included_evidence_at: datetime | None = None
    latest_included_evidence_at: datetime | None = None
    model_stages_executed: list[dict[str, Any]] = field(default_factory=list)
    generated_cases: list[str] = field(default_factory=list)
    exclusions: list[dict[str, str]] = field(default_factory=list)
    leakage_violations: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    model_outputs_excluded: int = 0
    _evidence_fingerprints: set[str] = field(default_factory=set, repr=False)

    def include(self, item: NormalizedEvidence) -> None:
        self.evidence_included += 1
        timestamp = item.available_at
        if self.earliest_included_evidence_at is None or timestamp < self.earliest_included_evidence_at:
            self.earliest_included_evidence_at = timestamp
        if self.latest_included_evidence_at is None or timestamp > self.latest_included_evidence_at:
            self.latest_included_evidence_at = timestamp
        self._evidence_fingerprints.add(item.content_fingerprint)

    @property
    def evidence_fingerprint(self) -> str:
        return canonical_fingerprint(sorted(self._evidence_fingerprints))

    @property
    def future_evidence_excluded(self) -> int:
        return self.evidence_excluded_as_future

    @property
    def future_revisions_excluded(self) -> int:
        return self.revisions_vintages_excluded

    @property
    def future_reaction_windows_excluded(self) -> int:
        return self.reaction_windows_excluded

    @property
    def future_model_outputs_excluded(self) -> int:
        return self.model_outputs_excluded

    def exclude(self, item: NormalizedEvidence, reason: str, category: str) -> None:
        if category == "future":
            self.evidence_excluded_as_future += 1
        elif category == "integrity":
            self.integrity_exclusions += 1
        elif category == "revision":
            self.revisions_vintages_excluded += 1
        elif category == "reaction":
            self.reaction_windows_excluded += 1
        elif category == "version":
            self.versions_excluded += 1
        if len(self.exclusions) < _MAX_AUDIT_DETAILS:
            self.exclusions.append(
                {
                    "evidence_ref": item.ref,
                    "reason": reason[:200],
                    "available_at": item.available_at.isoformat(),
                }
            )

    def violation(self, message: str) -> None:
        cleaned = " ".join(str(message).split())[:300]
        if cleaned and cleaned not in self.leakage_violations:
            self.leakage_violations.append(cleaned)

    def to_dict(self) -> dict[str, Any]:
        return {
            "replay_as_of": self.replay_as_of.isoformat(),
            "evidence_considered": self.evidence_considered,
            "evidence_included": self.evidence_included,
            "evidence_excluded_as_future": self.evidence_excluded_as_future,
            "integrity_exclusions": self.integrity_exclusions,
            "revisions_vintages_excluded": self.revisions_vintages_excluded,
            "reaction_windows_excluded": self.reaction_windows_excluded,
            "versions_excluded": self.versions_excluded,
            "future_evidence_excluded": self.future_evidence_excluded,
            "future_revisions_excluded": self.future_revisions_excluded,
            "future_reaction_windows_excluded": self.future_reaction_windows_excluded,
            "future_model_outputs_excluded": self.future_model_outputs_excluded,
            "evidence_fingerprint": self.evidence_fingerprint,
            "earliest_included_evidence_at": (
                self.earliest_included_evidence_at.isoformat()
                if self.earliest_included_evidence_at
                else None
            ),
            "latest_included_evidence_at": (
                self.latest_included_evidence_at.isoformat()
                if self.latest_included_evidence_at
                else None
            ),
            "model_stages_executed": list(self.model_stages_executed),
            "generated_cases": list(self.generated_cases),
            "cost_usd": round(self.cost_usd, 10),
            "leakage_violations": list(self.leakage_violations),
            "exclusions": list(self.exclusions),
        }


@dataclass(slots=True)
class ResearchContext:
    """One clock and one evidence boundary for live or historical research."""

    mode: ResearchMode
    as_of: datetime | None
    run_id: str
    correlation_id: str | None = None
    audit: ReplayAudit | None = None
    benchmark_id: str | None = None
    _allowed_evidence_refs: set[str] = field(default_factory=set, repr=False)
    _evidence_decisions: dict[str, bool] = field(default_factory=dict, repr=False)

    @classmethod
    def live(
        cls,
        *,
        run_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ResearchContext:
        return cls(
            mode=ResearchMode.LIVE,
            as_of=None,
            run_id=run_id or str(uuid4()),
            correlation_id=correlation_id,
        )

    @classmethod
    def replay(
        cls,
        as_of: datetime | str,
        *,
        run_id: str | None = None,
        correlation_id: str | None = None,
        benchmark_id: str | None = None,
    ) -> ResearchContext:
        cutoff = _utc(as_of)
        if cutoff is None:
            raise ValueError("replay as_of must be a timezone-aware timestamp")
        return cls(
            mode=ResearchMode.REPLAY,
            as_of=cutoff,
            run_id=run_id or str(uuid4()),
            correlation_id=correlation_id,
            audit=ReplayAudit(cutoff),
            benchmark_id=benchmark_id,
        )

    @property
    def is_replay(self) -> bool:
        return self.mode is ResearchMode.REPLAY

    @property
    def effective_time(self) -> datetime:
        return self.effective_now()

    def effective_now(self, fallback: datetime | None = None) -> datetime:
        if self.as_of is not None:
            return self.as_of
        current = fallback or datetime.now(UTC)
        parsed = _utc(current)
        if parsed is None:
            raise ValueError("research clock must be timezone-aware")
        return parsed

    def to_prompt_metadata(self) -> dict[str, Any]:
        """Expose only as-of controls; experiment identity stays evaluator-side."""
        return {
            "mode": self.mode.value,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "point_in_time_required": self.is_replay,
        }

    def deterministic_fingerprint(
        self, *, extra: Mapping[str, Any] | None = None
    ) -> str:
        audit = self.audit
        return canonical_fingerprint(
            {
                "mode": self.mode.value,
                "as_of": self.as_of.isoformat() if self.as_of else None,
                "evidence_fingerprint": (
                    audit.evidence_fingerprint
                    if audit is not None
                    else (
                        canonical_fingerprint(sorted(self._allowed_evidence_refs))
                        if self._allowed_evidence_refs
                        else None
                    )
                ),
                "extra": dict(extra or {}),
                "audit_evidence_count": audit.evidence_included if audit else None,
            }
        )

    def _exclude_reason(self, item: NormalizedEvidence) -> tuple[str, str] | None:
        if not self.is_replay:
            return None
        assert self.as_of is not None
        if not item.point_in_time_safe:
            return "point-in-time integrity cannot be established", "integrity"
        if item.source_timestamp > self.as_of or item.available_at > self.as_of:
            return "evidence became available after replay cutoff", "future"
        if item.valid_from is not None and item.valid_from > self.as_of:
            return "evidence version begins after replay cutoff", "version"
        if item.valid_to is not None and self.as_of >= item.valid_to:
            return "evidence version was not valid at replay cutoff", "version"
        revision_at = _utc(item.structured_fields.get("revision_at")) or _utc(
            item.provenance.get("revision_at")
        )
        if revision_at is not None and revision_at > self.as_of:
            return "later revision has no usable vintage at replay cutoff", "revision"
        if item.provenance.get("vintage_available") is False:
            return "original source vintage is unavailable", "revision"
        if item.evidence_type == "market_confirmation":
            target_at = _utc(item.structured_fields.get("target_at"))
            observed_at = _utc(item.structured_fields.get("observed_at"))
            if (target_at is not None and target_at > self.as_of) or (
                observed_at is not None and observed_at > self.as_of
            ):
                return "reaction window extends beyond replay cutoff", "reaction"
        return None

    def filter_evidence(
        self, evidence: list[NormalizedEvidence] | tuple[NormalizedEvidence, ...]
    ) -> tuple[NormalizedEvidence, ...]:
        if not self.is_replay:
            included = tuple(evidence)
            self._allowed_evidence_refs.update(item.ref for item in included)
            return included
        assert self.audit is not None
        included: list[NormalizedEvidence] = []
        for item in evidence:
            prior_decision = self._evidence_decisions.get(item.content_fingerprint)
            if prior_decision is not None:
                if prior_decision:
                    included.append(item)
                continue
            self.audit.evidence_considered += 1
            exclusion = self._exclude_reason(item)
            if exclusion is not None:
                reason, category = exclusion
                self.audit.exclude(item, reason, category)
                self._evidence_decisions[item.content_fingerprint] = False
                continue
            included.append(item)
            self._evidence_decisions[item.content_fingerprint] = True
            self._allowed_evidence_refs.add(item.ref)
            self.audit.include(item)
        return tuple(included)

    def guard_model_input(self, payload: Any, *, stage: str) -> None:
        """Reject benchmark answers, future timestamps, and non-supplied citations."""

        if not self.is_replay:
            return
        assert self.as_of is not None and self.audit is not None
        problems: list[str] = []

        def walk(value: Any, path: str) -> None:
            if isinstance(value, dict):
                for raw_key, child in value.items():
                    key = str(raw_key)
                    child_path = f"{path}.{key}" if path else key
                    if key in _BENCHMARK_ONLY_KEYS:
                        problems.append(f"benchmark evaluator field entered {stage}: {child_path}")
                    if key in _TIMESTAMP_KEYS:
                        if (parsed := _utc(child)) is not None and parsed > self.as_of:
                            problems.append(f"future timestamp entered {stage}: {child_path}")
                    if key in _REFERENCE_KEYS:
                        refs = child if isinstance(child, list) else [child]
                        for reference in refs:
                            if isinstance(reference, str) and ":" in reference:
                                if reference not in self._allowed_evidence_refs:
                                    problems.append(
                                        f"non-as-of evidence entered {stage}: {reference[:240]}"
                                    )
                    walk(child, child_path)
            elif isinstance(value, (list, tuple)):
                for index, child in enumerate(value):
                    walk(child, f"{path}[{index}]")

        walk(payload, "")
        for problem in problems[:20]:
            self.audit.violation(problem)
        if problems:
            raise ReplayLeakageError(problems[0])

    def guard_model_output(self, value: Any, *, stage: str) -> None:
        cleaned = asdict(value) if is_dataclass(value) else value
        self.guard_model_input(cleaned, stage=f"{stage}.output")

    def record_stage(self, stage: str, result: Any) -> None:
        if self.audit is None:
            return
        provenance = getattr(result, "provenance", None)
        entry = {
            "stage": stage,
            "prompt_version": getattr(provenance, "prompt_version", None),
            "model": getattr(provenance, "model_slug", None),
            "input_fingerprint": getattr(provenance, "input_fingerprint", None),
            "tokens_input": int(getattr(result, "tokens_input", 0) or 0),
            "tokens_output": int(getattr(result, "tokens_output", 0) or 0),
            "duration_ms": int(getattr(result, "duration_ms", 0) or 0),
            "cost_usd": float(getattr(result, "cost_usd", 0.0) or 0.0),
        }
        self.audit.model_stages_executed.append(entry)
        self.audit.cost_usd += entry["cost_usd"]

    def record_case(self, case_key: str) -> None:
        if self.audit is not None and case_key not in self.audit.generated_cases:
            self.audit.generated_cases.append(case_key[:240])

    def assert_clean(self) -> None:
        if self.audit is not None and self.audit.leakage_violations:
            raise ReplayLeakageError(self.audit.leakage_violations[0])

    def assert_no_leakage(self) -> None:
        self.assert_clean()


__all__ = [
    "ReplayAudit",
    "ReplayLeakageError",
    "ResearchContext",
    "ResearchMode",
]
