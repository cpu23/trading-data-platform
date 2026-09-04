"""Durable autonomous thesis-fusion cycle.

This module wires the pure thesis desk foundation into a scheduled,
event-driven production cycle.  It owns no commits: every helper takes the
caller's session and every persistence API runs inside the caller's
transaction.  Model calls are strictly bounded (per-run budget cap plus the
global daily budget through ``llm_client.LLMStage``) and injectable for
tests; every model output must pass the pure validators (the tournament
candidate contract, the falsification proposal contract, evidence signals)
before it can influence any output.

Pipeline (one cycle)
--------------------
1. Collect bounded point-in-time-safe evidence through
   ``EvidenceRegistry(DEFAULT_ADAPTERS)`` with deterministic ordering and
   content fingerprints, and ensure the single durable system theme
   concurrency-safely (INSERT .. ON CONFLICT, then a SELECT fallback).
2. Run the eight-role tournament with the configured bounds and scoring
   inputs (cost/liquidity/downside stay optional finite scores or None).
3. For each promoted candidate: merge/create the thesis (canonical identity,
   content-addressed input fingerprint), attach ONLY its cited evidence as
   support with stable source family/fingerprint/timestamps, persist
   bull/base/bear scenario legs (nullable probability, explicit expected
   returns), group true competitors by normalized subject+horizon, evaluate
   and freeze the opportunity snapshot, build a frozen ``ThesisSnapshot``
   whose claim cites the candidate refs, and independently
   ``challenge_thesis`` against the broader collected evidence.
4. Persist falsification decisions through
   ``record_falsification_run``/``update_falsification_run``; attach valid
   challenger-cited rows as ``contradicts`` and recompute scores; pause
   (never close/delete) breached active/candidate theses.
5. Run a second bounded falsification pass over high-opportunity existing
   active/candidate theses (linked open positions first) even when nothing
   was regenerated, loading a frozen snapshot plus relevant new+attached
   evidence and never mutating prior snapshots.  Selection and the pause
   both fail closed on reference-visible state: current score/fusion
   recency (``last_evaluated_at``/``fusion_reference_at``) must not
   postdate the cutoff, and a breached thesis is paused only through an
   optimistic conditional UPDATE against the tokens selected at the
   reference (a stale verdict never pauses newer state).
6. Return bounded counts, errors, and model cost; one failing candidate or
   runner never sinks the cycle.

Idempotency: identical inputs reproduce identical identities — thesis
``input_fingerprint``, evidence fingerprints, scenario content, opportunity
``snapshot_key``, and falsification ``run_key`` are all content- or
cycle-addressed, so re-running the same cycle creates no duplicate rows.
The cycle identity embeds the full accepted reference (seconds plus
fractional microseconds, UTC), so distinct accepted references never
collide on snapshots, falsification runs, or challenge claims while an
exact-reference rerun coalesces on every one of them.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from typing import Any
from uuid import uuid4

from budgets import BudgetContext
from jobs import enqueue_job
from llm_client import LLMStage
from research_intelligence.context import ResearchContext
from research_intelligence.contracts import (
    EvidenceSignal,
    Scenario,
    canonical_fingerprint,
)
from research_intelligence.evidence import (
    DEFAULT_ADAPTERS,
    EvidenceRegistry,
)
from sqlalchemy import text
from thesis_challenges import (
    MAX_CITATIONS_PER_CLAIM,
    PROPOSAL_KINDS,
    ChallengeProposal,
    ChallengeRunner,
    ThesisClaim,
    ThesisSnapshot,
    challenge_thesis,
)
from thesis_fusion import (
    canonical_thesis_key,
    create_thesis_proposal,
    record_forecast_outcome,
)
from thesis_scoring import (
    DOWNSIDE_NORMALIZER,
    CatalystSignal,
    assess_evidence,
    assess_opportunity,
    calculate_neglect,
    catalyst_readiness,
    evidence_quality_prior,
    is_auditable_evidence,
    scenario_valuation,
)
from thesis_tournament import (
    CITATION_FIELDS,
    MAX_SEMANTIC_AUDIT_BATCH,
    ROLES,
    RoleRunner,
    resolve_candidate_entities,
    run_tournament,
)

from contracts.db_results import result_first, result_rows
from contracts.runtime_config import ThesisAutonomyConfig
from db import get_session
from orchestrator import accept_run, finalize_run_safely, start_run

JOB_TYPE = "thesis_autonomy_run"

#: Single durable system theme owned by the autonomous desk.  Candidates are
#: always merged into this theme so every cycle shares one theme identity.
SYSTEM_THEME_NAME = "autonomous-thesis-fusion"
SYSTEM_THEME_DEFINITION = (
    "System theme for the autonomous thesis-fusion desk: bounded, "
    "evidence-cited, point-in-time-safe candidate theses."
)

#: Bounded config subset that participates in the durable job identity.
_IDENTITY_KEYS = (
    "lookback_days",
    "maximum_evidence",
    "maximum_promoted",
    "maximum_challenges_per_run",
    "event_debounce_minutes",
    "maximum_event_runs_per_day",
    "falsification_budget_fraction",
    "minimum_supporting_source_families",
    "require_cited_excerpts",
    "require_opposing_variants",
    "model_budget_usd_per_run",
    "reasoning_effort",
    "model_override",
    "max_output_tokens",
    "cost",
    "liquidity",
    "downside",
)

_MAX_ERRORS = 20
_MAX_OUTCOME_RESOLUTION = 100
MAX_AUDIT_CANDIDATES = MAX_SEMANTIC_AUDIT_BATCH
MAX_AUDIT_CITED_REFS = 30
MAX_UNSUPPORTED_CLAIMS = 10

#: Bounded horizon mapping for deterministic forecast target dates (days).
_HORIZON_DAYS = {
    "intraday": 1,
    "days": 7,
    "weeks": 30,
    "months": 90,
    "multi_year": 730,
}
_DEFAULT_HORIZON_DAYS = 90
_MAX_HORIZON_DAYS = 730

#: Grace before a matured forecast without any market price is recorded as
#: inconclusive; before the grace it stays open.
_FORECAST_GRACE_DAYS = 7
_MAX_PRICE_AGE_DAYS = 7


def _bounded(value: Any, default: int, maximum: int) -> int:
    try:
        return max(1, min(maximum, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _as_utc(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ValueError("as_of must be datetime or ISO text")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _cycle_key(reference: datetime) -> str:
    """Deterministic fixed-width identity for one accepted reference.

    The reference is normalized to aware UTC first (naive input is treated
    as UTC, exactly like ``_as_utc``) and rendered as ``%Y%m%dT%H%M%S.%f``
    (seconds plus fractional microseconds, always six digits), so distinct
    accepted references — even 40 seconds apart or differing only in
    microseconds — never share a cycle identity, while an exact rerun at
    the same reference reproduces the identical key and coalesces on every
    cycle-addressed artifact (ResearchContext run id, opportunity snapshot
    keys, falsification run keys, challenge claim ids).
    """
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return reference.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%f")


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _canonical_market_symbol(value: Any) -> str | None:
    """Canonical platform equity symbol: trimmed and uppercased, with dots
    and exchange suffixes preserved (``BRK.B``, ``ICG.L``, ``ACME.NYSE``).

    Yahoo's dot-to-dash class-share mapping exists only at the provider
    request boundary (``public_equities._provider_symbol``); it is never a
    platform identity transform.  Blank values canonicalize to None so
    symbol-less theses stay symbol-less.
    """
    if value is None:
        return None
    symbol = str(value).strip()
    return symbol.upper() if symbol else None


def _target_boundary(day: Any) -> datetime:
    """End-of-target-calendar-day UTC for one price-forecast target date.

    This is the deterministic resolution boundary: a forecast may resolve
    only after this instant, and its terminal close is the latest daily bar
    timestamped at/before it (weekend/holiday targets fall back to the
    prior available close).  Delayed runs never look past it.
    """
    if isinstance(day, datetime):
        day = day.date()
    if isinstance(day, str):
        day = date.fromisoformat(day.split("T", 1)[0])
    return datetime.combine(day, dt_time.max, tzinfo=UTC)


def _parse_json(content: Any) -> Any:
    """Parse model JSON text (tolerating a single fenced block)."""
    if not isinstance(content, str):
        raise ValueError("model content must be JSON text")
    value = content.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(value)


def _settings(config: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the thesis_autonomy section through the frozen validator.

    Missing keys keep the frozen defaults; unknown/out-of-bounds keys are
    rejected exactly as they would be at config load.
    """
    section = config.get("thesis_autonomy", {})
    if isinstance(section, ThesisAutonomyConfig):
        return dict(section)
    if not isinstance(section, Mapping):
        section = {}
    return dict(ThesisAutonomyConfig.model_validate(dict(section)))


def thesis_autonomy_identity(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return the bounded config subset that shapes cycle output.

    Shared by every enqueue path so scheduled, manual, and API-triggered
    runs dedupe identically for identical settings.
    """
    settings = _settings(config)
    return {key: settings[key] for key in _IDENTITY_KEYS}


# ---------------------------------------------------------------------------
# Production model adapters (injectable runners for tests)
# ---------------------------------------------------------------------------


class _ModelBudget:
    """One shared per-run model budget ledger for every desk LLM call.

    The role runner, challenger, and citation auditor all charge the same
    ledger, so the configured per-run ceiling is a hard cap across the whole
    cycle; the global daily budget (enforced inside ``LLMStage``) remains
    the ultimate cap.
    """

    __slots__ = ("cap_usd", "cost_usd")

    def __init__(self, cap_usd: float | None) -> None:
        self.cap_usd = cap_usd
        self.cost_usd = 0.0

    def check_before(self) -> None:
        if self.cap_usd is not None and self.cost_usd >= self.cap_usd:
            raise RuntimeError("thesis autonomy model budget exhausted")

    def charge(self, amount: float) -> None:
        self.cost_usd += max(0.0, float(amount or 0.0))
        if self.cap_usd is not None and self.cost_usd > self.cap_usd:
            raise RuntimeError("thesis autonomy model budget exceeded")


def _record_generation_attempt(
    session: Any,
    *,
    stage: str,
    attempt_number: int,
    prompt: str,
    result: Mapping[str, Any] | None,
    issues: Sequence[str],
    correlation_id: str | None,
) -> str:
    """Insert one bounded generation_attempts audit row.

    Never persists provider exception text or secrets: ``issues`` carries
    only bounded type names, and the raw response is the model's own output.
    Processor is always ``thesis_autonomy``; the stage names the role or
    ``challenger``.
    """
    attempt_id = str(uuid4())
    payload = dict(result) if result is not None else {}
    bounded_issues = [str(issue).replace("\n", " ")[:200] for issue in issues][:20]
    session.execute(
        text(
            """INSERT INTO generation_attempts
               (attempt_id, correlation_id, processor, stage, attempt_number,
                status, prompt_text, raw_response, validation_issues, model_used,
                tokens_input, tokens_output, cost_usd, duration_ms,
                request_metadata)
               VALUES (:attempt_id, :correlation_id, :processor, :stage,
                       :attempt_number, :status, :prompt_text, :raw_response,
                       CAST(:validation_issues AS JSONB), :model_used,
                       :tokens_input, :tokens_output, :cost_usd, :duration_ms,
                       CAST(:request_metadata AS JSONB))"""
        ),
        {
            "attempt_id": attempt_id,
            "correlation_id": correlation_id,
            "processor": "thesis_autonomy",
            "stage": stage,
            "attempt_number": max(1, int(attempt_number)),
            "status": "validated" if not bounded_issues else "validation_failed",
            "prompt_text": prompt,
            "raw_response": payload.get("content"),
            "validation_issues": json.dumps(bounded_issues),
            "model_used": payload.get("model"),
            "tokens_input": int(payload.get("tokens_input") or 0),
            "tokens_output": int(payload.get("tokens_output") or 0),
            "cost_usd": float(payload.get("cost_usd") or 0),
            "duration_ms": int(payload.get("duration_ms") or 0),
            "request_metadata": json.dumps(
                {
                    "input_fingerprint": canonical_fingerprint(
                        {"stage": stage, "prompt": prompt}
                    ),
                    "requested_model": payload.get("requested_model"),
                    "provider": payload.get("provider"),
                    "generation_id": payload.get("generation_id"),
                    "tokens_reasoning": payload.get("tokens_reasoning"),
                    "tokens_cached": payload.get("tokens_cached"),
                    "retry_count": payload.get("retry_count"),
                },
                sort_keys=True,
                default=str,
            ),
        },
    )
    return attempt_id


def _validate_shape(value: Any, shape: str) -> None:
    """Immediate parse-level shape check (never semantic validation).

    Semantic citation/numeric failures belong to the tournament and
    challenge validators and are never repaired.
    """
    if shape == "array":
        if not isinstance(value, list):
            raise ValueError("output must be a JSON array")
        for index, item in enumerate(value):
            if not isinstance(item, Mapping):
                raise ValueError(f"candidate {index} must be an object")
        return
    if shape == "object":
        if value is not None and not isinstance(value, Mapping):
            raise ValueError("output must be an object or null")
        return
    raise ValueError(f"unsupported output shape:{str(shape)[:40]}")


def _validate_response_schema(
    value: Any,
    schema: Mapping[str, Any],
    *,
    path: str = "$",
    depth: int = 0,
) -> None:
    """Validate the strict schema subset used by autonomous model stages."""
    if depth > 20:
        raise ValueError(f"{path} schema depth exceeds limit")
    expected = schema.get("type")
    expected_types = (
        tuple(expected)
        if isinstance(expected, list)
        else (expected,)
        if isinstance(expected, str)
        else ()
    )

    def matches(kind: str) -> bool:
        if kind == "null":
            return value is None
        if kind == "object":
            return isinstance(value, Mapping)
        if kind == "array":
            return isinstance(value, list)
        if kind == "string":
            return isinstance(value, str)
        if kind == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if kind == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if kind == "boolean":
            return isinstance(value, bool)
        return False

    if expected_types and not any(matches(kind) for kind in expected_types):
        raise ValueError(f"{path} must be {' or '.join(expected_types)}")
    if value is None:
        return
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        raise ValueError(f"{path} is not an allowed value")
    if isinstance(value, Mapping):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, Mapping) else {}
        required = schema.get("required")
        required = required if isinstance(required, list) else []
        missing = [str(key) for key in required if key not in value]
        if missing:
            raise ValueError(f"{path} missing required fields: {', '.join(missing)}")
        if schema.get("additionalProperties") is False:
            unexpected = sorted(str(key) for key in value if key not in properties)
            if unexpected:
                raise ValueError(
                    f"{path} has unexpected fields: {', '.join(unexpected)}"
                )
        for key, item in value.items():
            child = properties.get(key)
            if isinstance(child, Mapping):
                _validate_response_schema(
                    item,
                    child,
                    path=f"{path}.{key}",
                    depth=depth + 1,
                )
        return
    if isinstance(value, list):
        minimum_items = schema.get("minItems")
        maximum_items = schema.get("maxItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            raise ValueError(f"{path} has too few items")
        if isinstance(maximum_items, int) and len(value) > maximum_items:
            raise ValueError(f"{path} has too many items")
        child = schema.get("items")
        if isinstance(child, Mapping):
            for index, item in enumerate(value):
                _validate_response_schema(
                    item,
                    child,
                    path=f"{path}[{index}]",
                    depth=depth + 1,
                )
        return
    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        maximum_length = schema.get("maxLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            raise ValueError(f"{path} is too short")
        if isinstance(maximum_length, int) and len(value) > maximum_length:
            raise ValueError(f"{path} is too long")
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise ValueError(f"{path} is below minimum")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise ValueError(f"{path} exceeds maximum")


def _repair_prompt(
    original_prompt: str,
    invalid: Any,
    issues: Sequence[str],
) -> str:
    """Terse no-new-claims repair prompt for a parse/shape failure."""
    return (
        "Repair the JSON once. Return only a complete replacement that matches "
        "the original strict schema. Reduce item counts and shorten text "
        "aggressively so the replacement is complete; explicit unknowns and "
        "abstention are preferable to exhaustive prose. Keep valid evidence IDs "
        "and do not introduce claims, numbers, entities, or evidence.\n"
        f"Validation issue: {', '.join(str(item) for item in issues)[:300]}\n"
        f"Invalid response:\n{str(invalid or '')[:4000]}\n"
        f"Original request:\n{original_prompt}"
    )


def _call_stage_once(
    ledger: Any,
    *,
    stage: str,
    prompt: str,
    schema: Mapping[str, Any],
    attempt_number: int,
) -> tuple[Any, list[str], Any]:
    """One LLMStage call with immediate shape validation and audit row.

    Returns ``(parsed, issues, raw_content)``; on LLMStage failure the
    exception propagates (the caller's fail-soft boundary isolates it) after
    a failure audit row is recorded.  Budget caps are checked before and
    after the call; there are no provider/network retries beyond LLMStage
    policy.
    """
    ledger._check_budget()
    started = time.monotonic()
    try:
        stage_runner = LLMStage(
            ledger.config,
            "thesis_autonomy",
            correlation_id=ledger.correlation_id,
            budget_context=ledger.budget_context,
            response_schema=dict(schema),
            model=ledger.model,
            reasoning_effort=ledger.reasoning_effort,
            max_output_tokens=ledger.max_output_tokens,
        )
        result = stage_runner.call(prompt)
    except Exception as exc:
        issues = [type(exc).__name__]
        _record_generation_attempt(
            ledger.session,
            stage=stage,
            attempt_number=attempt_number,
            prompt=prompt,
            result=None,
            issues=issues,
            correlation_id=ledger.correlation_id,
        )
        ledger.errors.append(f"{stage}:{type(exc).__name__}")
        raise
    payload = dict(result)
    payload.setdefault("duration_ms", max(0, int((time.monotonic() - started) * 1000)))
    ledger.calls += 1
    ledger.cost_usd += float(payload.get("cost_usd") or 0.0)
    ledger.budget.charge(float(payload.get("cost_usd") or 0.0))
    ledger._check_budget()
    raw_content = payload.get("content")
    try:
        parsed = _parse_json(raw_content)
    except Exception:
        issues = ["JSONDecodeError"]
        _record_generation_attempt(
            ledger.session,
            stage=stage,
            attempt_number=attempt_number,
            prompt=prompt,
            result=payload,
            issues=issues,
            correlation_id=ledger.correlation_id,
        )
        return None, issues, raw_content
    try:
        _validate_shape(parsed, ledger.shape)
        _validate_response_schema(parsed, schema)
    except Exception as exc:
        issues = [f"schema:{str(exc)[:180]}"]
        _record_generation_attempt(
            ledger.session,
            stage=stage,
            attempt_number=attempt_number,
            prompt=prompt,
            result=payload,
            issues=issues,
            correlation_id=ledger.correlation_id,
        )
        return None, issues, raw_content
    _record_generation_attempt(
        ledger.session,
        stage=stage,
        attempt_number=attempt_number,
        prompt=prompt,
        result=payload,
        issues=[],
        correlation_id=ledger.correlation_id,
    )
    return parsed, [], raw_content


class LLMRoleRunner:
    """Production tournament role runner over ``LLMStage``.

    Every call uses the strict role output schema; the parsed response is
    shape-validated immediately and repaired exactly once (a terse
    no-new-claims prompt) when it fails JSON parse or shape.  Semantic
    citation/numeric failures belong to the tournament validators and are
    never repaired.  Each attempt is recorded as a bounded
    ``generation_attempts`` row; accumulated cost never exceeds
    ``budget_cap_usd``.
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        correlation_id: str | None = None,
        budget_context: BudgetContext | None = None,
        session: Any = None,
        settings: Mapping[str, Any] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        max_output_tokens: int | None = None,
        budget: _ModelBudget | None = None,
        budget_cap_usd: float | None = None,
    ) -> None:
        self.config = config
        self.correlation_id = correlation_id
        self.budget_context = budget_context or BudgetContext()
        self.session = session
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.budget = budget if budget is not None else _ModelBudget(budget_cap_usd)
        self.shape = "array"
        self.cost_usd = 0.0
        self.calls = 0
        self.errors: list[str] = []

    def _check_budget(self) -> None:
        self.budget.check_before()

    def run(self, *, role: str, prompt: str, schema: Mapping[str, Any]) -> Any:
        parsed, issues, raw = _call_stage_once(
            self, stage=role, prompt=prompt, schema=schema, attempt_number=1
        )
        if issues:
            # Exactly one repair call, still inside the per-run budget.
            parsed, issues, _raw = _call_stage_once(
                self,
                stage=role,
                prompt=_repair_prompt(prompt, raw, issues),
                schema=schema,
                attempt_number=2,
            )
            if issues:
                raise ValueError("role output failed validation:" + ",".join(issues))
        return parsed


def challenge_output_schema() -> dict[str, Any]:
    """Strict one-proposal-or-null schema for the production challenger."""
    return {
        "type": ["object", "null"],
        "additionalProperties": False,
        "required": ["kind", "statement", "citations"],
        "properties": {
            "kind": {"enum": sorted(PROPOSAL_KINDS)},
            "statement": {"type": "string", "maxLength": 2000},
            "citations": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_CITATIONS_PER_CLAIM,
            },
        },
    }


def _challenge_prompt(
    snapshot: ThesisSnapshot,
    evidence: Sequence[EvidenceSignal],
    *,
    evidence_catalog: Mapping[str, Any] | None = None,
) -> str:
    catalog = evidence_catalog or {}
    lines: list[str] = []
    for signal in list(evidence)[:200]:
        source = catalog.get(signal.ref)
        title = " ".join(str(getattr(source, "title", "") or "").split())
        excerpt = " ".join(str(getattr(source, "bounded_excerpt", "") or "").split())
        stamp = signal.source_timestamp.isoformat()
        line = (
            f"- {signal.ref} | {signal.evidence_type} | {signal.source_name} | {stamp}"
        )
        if title:
            line += f" | {title[:240]}"
        if excerpt:
            line += f" | {excerpt[:300]}"
        lines.append(line)
    omitted = max(0, len(evidence) - 200)
    brief = "\n".join(lines) + (
        f"\n({omitted} further supplied items omitted)" if omitted else ""
    )
    scenario_lines = "\n".join(
        f"- {leg.label}: probability={leg.probability!r}, "
        f"expected_return={leg.expected_return!r}"
        for leg in snapshot.scenarios
    )
    return f"""You are the independent challenger for one thesis in the autonomous desk.

THESIS
- id: {snapshot.thesis_id}
- statement: {snapshot.statement}
- direction: {snapshot.direction}
- as_of: {snapshot.as_of.isoformat()}
- cost: {snapshot.cost}

SCENARIOS
{scenario_lines or "- none"}

SUPPLIED EVIDENCE (cite ONLY these refs, by exact evidence_ref value)
{brief}

TASK
Independently falsify the thesis against the supplied evidence. Return either
null (no material counter-evidence or alternative mechanism) or ONE object with
exactly these keys:
- kind (string): "counter_evidence" or "alternative_mechanism"
- statement (string, <=2000): the strongest counter-evidence or alternative
  mechanism the supplied evidence supports
- citations (array of strings, 1..{MAX_CITATIONS_PER_CLAIM}): ONLY evidence_ref
  values from the supplied list that bear on the challenge

HARD RULES
- Cite only supplied evidence_ref values; never invent or paraphrase
  citations in prose.
- Never include trade instructions, entry/exit levels, stops, position
  sizing, allocation, or risk/reward advice.
- Reason point-in-time from the evidence as of its timestamp.
- Missing or weak opposition is honestly expressed as null, never invented."""


class LLMChallenger:
    """Production independent challenger over ``LLMStage``.

    The strict one-proposal-or-null schema is enforced on every call with
    immediate shape validation and exactly one repair call; proposals are
    constructed only through ``ChallengeProposal.create`` and validated
    against the supplied evidence by ``challenge_thesis`` (semantic
    failures are never repaired).  Each attempt is recorded as a bounded
    ``generation_attempts`` row; accumulated cost never exceeds
    ``budget_cap_usd``.
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        correlation_id: str | None = None,
        budget_context: BudgetContext | None = None,
        session: Any = None,
        settings: Mapping[str, Any] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        max_output_tokens: int | None = None,
        budget: _ModelBudget | None = None,
        evidence_catalog: Mapping[str, Any] | None = None,
        budget_cap_usd: float | None = None,
    ) -> None:
        self.config = config
        self.correlation_id = correlation_id
        self.budget_context = budget_context or BudgetContext()
        self.session = session
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.evidence_catalog = evidence_catalog or {}
        self.budget = budget if budget is not None else _ModelBudget(budget_cap_usd)
        self.shape = "object"
        self.cost_usd = 0.0
        self.calls = 0
        self.errors: list[str] = []

    def _check_budget(self) -> None:
        self.budget.check_before()

    def challenge(
        self,
        snapshot: ThesisSnapshot,
        evidence: Sequence[EvidenceSignal],
    ) -> ChallengeProposal | None:
        prompt = _challenge_prompt(
            snapshot, evidence, evidence_catalog=self.evidence_catalog
        )
        parsed, issues, raw = _call_stage_once(
            self,
            stage="challenger",
            prompt=prompt,
            schema=challenge_output_schema(),
            attempt_number=1,
        )
        if issues:
            parsed, issues, _raw = _call_stage_once(
                self,
                stage="challenger",
                prompt=_repair_prompt(prompt, raw, issues),
                schema=challenge_output_schema(),
                attempt_number=2,
            )
            if issues:
                raise ValueError(
                    "challenger output failed validation:" + ",".join(issues)
                )
        if parsed is None:
            return None
        if not isinstance(parsed, Mapping):
            raise ValueError("challenger output must be an object or null")
        return ChallengeProposal.create(
            kind=parsed.get("kind"),
            statement=parsed.get("statement"),
            citations=parsed.get("citations"),
        )


def citation_audit_output_schema() -> dict[str, Any]:
    """Strict decision schema for the semantic citation auditor."""
    return {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "candidate_key",
                "verdict",
                "cited_refs",
                "unsupported_claims",
                "rationale",
            ],
            "properties": {
                "candidate_key": {"type": "string", "maxLength": 240},
                "verdict": {
                    "enum": ["entailed", "mixed", "unsupported", "contradicted"]
                },
                "cited_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_AUDIT_CITED_REFS,
                },
                "unsupported_claims": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_UNSUPPORTED_CLAIMS,
                },
                "rationale": {"type": "string", "maxLength": 2000},
            },
        },
    }


def _citation_audit_prompt(
    candidates: Sequence[Mapping[str, Any]],
    evidence: Mapping[str, Any],
) -> str:
    """Claim-vs-exact-cited-excerpt audit prompt.

    The auditor judges each claim ONLY against the exact cited excerpts; it
    must never introduce new evidence, claims, or numbers, and never reuse
    role outputs or generator confidence.
    """
    blocks: list[str] = []
    value_keys = {
        "claim": "claim",
        "consensus": "consensus",
        "variant_perception": "variant_perception",
        "mechanism": "mechanism",
        "catalyst": "catalyst",
        "trend": "trend_context",
        "valuation": "valuation_context",
        "sentiment": "sentiment_context",
    }
    for candidate in list(candidates)[:MAX_AUDIT_CANDIDATES]:
        candidate_key = str(candidate.get("candidate_key") or "unknown")
        citation_map = candidate.get("citations")
        citation_map = citation_map if isinstance(citation_map, Mapping) else {}
        field_blocks: list[str] = []
        for field in CITATION_FIELDS:
            value = " ".join(str(candidate.get(value_keys[field]) or "").split())[:2000]
            excerpts: list[str] = []
            for ref in citation_map.get(field) or ():
                item = evidence.get(ref)
                if item is None:
                    continue
                excerpt = " ".join(str(item.bounded_excerpt or "").split())
                if excerpt:
                    excerpts.append(f"  - {ref}: {excerpt[:500]}")
            field_blocks.append(
                f"FIELD {field}\ntext: {value}\nexact cited excerpts:\n"
                + ("\n".join(excerpts) if excerpts else "  - none supplied")
            )
        blocks.append(f"CANDIDATE {candidate_key}\n" + "\n".join(field_blocks))
    return f"""You are the independent semantic citation auditor for one tournament batch.

For each candidate decide whether ALL factual fields are ENTAILED by each
field's own EXACT cited excerpts supplied below. Judge strictly: no world
knowledge, no evidence from another field, no inference beyond the excerpts,
and no new evidence, claims, or numbers of your own.

VERDICTS
- entailed: every factual field follows from its field-level cited excerpts
- mixed: at least one field is supported and at least one is not
- unsupported: the field-level citations do not support the candidate
- contradicted: one or more exact excerpts contradict a factual field

Return a JSON array with ONE decision object per candidate, each with EXACTLY
these keys:
- candidate_key (string): the candidate's key from the input
- verdict (string): one of entailed, mixed, unsupported, contradicted
- cited_refs (array of strings): the subset of all field-level cited refs that
  actually support their named fields (may be empty)
- unsupported_claims (array of strings, <=10): the specific field and claim
  parts not supported by that field's excerpts
- rationale (string, <=2000): concise evidence-anchored rationale

HARD RULES
- Judge ONLY against the exact cited excerpts; never introduce new evidence,
  claims, or numbers.
- Never include trade instructions, entry/exit levels, stops, position
  sizing, allocation, or risk/reward advice.
- Weak or missing support is honestly unsupported or mixed, never invented.

CANDIDATES
{chr(10).join(blocks)}"""


class LLMSemanticCitationAuditor:
    """Production semantic citation auditor over a SEPARATE LLMStage.

    Independent from the role runner and challenger: it consumes only the
    compacted candidate payloads and the evidence catalog, never role
    outputs or generator confidence.  The strict decision schema is enforced
    with immediate shape validation and exactly one repair call; each
    attempt is recorded as a bounded ``generation_attempts`` row and all
    calls charge the shared per-run model budget.
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        correlation_id: str | None = None,
        budget_context: BudgetContext | None = None,
        session: Any = None,
        settings: Mapping[str, Any] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        max_output_tokens: int | None = None,
        budget: _ModelBudget | None = None,
        budget_cap_usd: float | None = None,
    ) -> None:
        self.config = config
        self.correlation_id = correlation_id
        self.budget_context = budget_context or BudgetContext()
        self.session = session
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.budget = budget if budget is not None else _ModelBudget(budget_cap_usd)
        self.shape = "array"
        self.cost_usd = 0.0
        self.calls = 0
        self.errors: list[str] = []

    def _check_budget(self) -> None:
        self.budget.check_before()

    def audit(
        self,
        *,
        candidates: Sequence[Any],
        evidence: Mapping[str, Any],
    ) -> Any:
        prompt = _citation_audit_prompt(candidates, evidence)
        parsed, issues, raw = _call_stage_once(
            self,
            stage="citation_audit",
            prompt=prompt,
            schema=citation_audit_output_schema(),
            attempt_number=1,
        )
        if issues:
            parsed, issues, _raw = _call_stage_once(
                self,
                stage="citation_audit",
                prompt=_repair_prompt(prompt, raw, issues),
                schema=citation_audit_output_schema(),
                attempt_number=2,
            )
            if issues:
                raise ValueError(
                    "citation audit output failed validation:" + ",".join(issues)
                )
        return parsed


# ---------------------------------------------------------------------------
# Durable enqueue helper (scheduler, API trigger, manual runs)
# ---------------------------------------------------------------------------


def enqueue_thesis_autonomy_job(
    config: dict[str, Any],
    *,
    triggered_by: str = "manual",
    force: bool = False,
    request_nonce: str | None = None,
) -> dict[str, Any]:
    """Accept a durable run and enqueue one bounded autonomy job.

    Scheduled, manual, and API-triggered runs share the job type and the
    ``thesis-autonomy:global`` identity; normal refreshes coalesce per
    request date while explicit forced runs receive a unique identity so a
    completed job never suppresses new work.  The event-driven path uses its
    own per-bucket identity (see ``events.routing``).
    """
    settings = _settings(config)
    if not settings["enabled"]:
        raise ValueError("thesis autonomy is disabled")
    correlation_id = str(uuid4())
    accepted_at = accept_run(
        config,
        correlation_id,
        triggered_by,
        # cycle_runs.run_kind is constrained to the durable lifecycle kinds
        # (no autonomy kind exists yet); the research kind is the closest
        # match and the component carries the autonomy identity.
        "research",
        "thesis_autonomy",
        request_summary={
            "job_type": JOB_TYPE,
            "force": bool(force),
        },
    )
    worker_id = f"thesis-autonomy-enqueue:{uuid4()}"
    try:
        started = start_run(config, correlation_id, worker_id)
    except Exception:
        finalize_run_safely(
            correlation_id,
            "failed",
            {"status": "failed", "reason": "thesis autonomy run start unavailable"},
            config,
            "thesis autonomy run start unavailable",
            run_kind="research",
            component="thesis_autonomy",
        )
        raise
    if not started:
        raise RuntimeError("accepted thesis autonomy run could not be claimed")
    try:
        identity = {
            "job_type": JOB_TYPE,
            "config": thesis_autonomy_identity(config),
            "request_date": accepted_at.astimezone(UTC).date().isoformat(),
            # Normal refreshes coalesce. Explicit forced runs receive a
            # unique identity so a completed prior job cannot suppress them.
            "request_nonce": request_nonce or (correlation_id if force else None),
        }
        input_fingerprint = canonical_fingerprint(identity)
        with get_session(config) as session:
            enqueued = enqueue_job(
                session,
                job_type=JOB_TYPE,
                dedupe_key="thesis-autonomy:global",
                input_fingerprint=input_fingerprint,
                payload={"force": bool(force), "as_of": accepted_at.isoformat()},
                correlation_id=correlation_id,
                priority=90 if force else 80,
                max_attempts=3,
            )
        job = enqueued.job
        result = {
            "status": "queued" if enqueued.inserted else "already_queued",
            "job_id": str(job.id) if job is not None else None,
            "correlation_id": (
                str(job.correlation_id) if job is not None else correlation_id
            ),
            "accepted_at": accepted_at.isoformat(),
            "inserted": enqueued.inserted,
            "force": bool(force),
        }
        finalize_run_safely(
            correlation_id,
            "success",
            result,
            config,
            None,
            worker_id=worker_id,
            run_kind="research",
            component="thesis_autonomy",
        )
        return result
    except Exception:
        finalize_run_safely(
            correlation_id,
            "failed",
            {},
            config,
            "thesis autonomy enqueue failed",
            worker_id=worker_id,
            run_kind="research",
            component="thesis_autonomy",
        )
        raise


# ---------------------------------------------------------------------------
# Cycle helpers
# ---------------------------------------------------------------------------


def _ensure_system_theme(session: Any) -> tuple[str, bool]:
    """Find or create the single durable system theme concurrency-safely."""
    row = result_first(
        session.execute(
            text(
                """INSERT INTO investment_themes
               (name, definition, horizon, macro_drivers, key_indicators,
                status, origin)
               VALUES (:name, :definition, 'multi_year',
                       ARRAY[]::TEXT[], ARRAY[]::TEXT[], 'active', 'discovered')
               ON CONFLICT (name) DO NOTHING
               RETURNING id"""
            ),
            {
                "name": SYSTEM_THEME_NAME,
                "definition": SYSTEM_THEME_DEFINITION,
            },
        )
    )
    if row is not None:
        return str(row["id"]), True
    existing = result_first(
        session.execute(
            text("SELECT id FROM investment_themes WHERE name = :name LIMIT 1"),
            {"name": SYSTEM_THEME_NAME},
        )
    )
    if existing is None:
        raise RuntimeError("system theme creation did not return an identity")
    return str(existing["id"]), False


def _collect_evidence(
    session: Any,
    settings: Mapping[str, Any],
    *,
    reference: datetime,
) -> tuple[tuple[Any, ...], dict[str, str]]:
    """Collect bounded point-in-time-safe evidence with stable ordering.

    ``reference`` is enforced as a replay cutoff: the registry receives a
    replay ``ResearchContext`` at the cycle reference so every adapter
    bounds source timestamps by ``until`` and ``filter_evidence`` enforces
    the source/availability cutoffs.  No row that became available after
    the reference can drive candidate generation, promotion, or scoring.
    """
    registry = EvidenceRegistry(DEFAULT_ADAPTERS)
    context = ResearchContext.replay(
        reference, run_id="autonomy:" + _cycle_key(reference)
    )
    collection = registry.collect(
        session,
        rolling_window_days=_bounded(settings.get("lookback_days", 30), 30, 3650),
        limit=_bounded(settings.get("maximum_evidence", 96), 96, 2000),
        now=reference,
        context=context,
    )
    items = tuple(
        sorted(
            (item for item in collection.items if item.point_in_time_safe),
            key=lambda item: (item.source_timestamp, item.ref),
        )
    )[: _bounded(settings.get("maximum_evidence", 96), 96, 2000)]
    return items, dict(collection.failures)


def _source_family(item: Any) -> str:
    provenance = item.provenance if isinstance(item.provenance, Mapping) else {}
    return str(
        provenance.get("source_family")
        or provenance.get("source")
        or item.source_name
        or "unknown"
    )


def _evidence_quality(item: Any) -> float:
    """Deterministic source/content quality prior, never a model judgment.

    Delegates to the one shared table in ``thesis_scoring`` so every
    signal-grading path (cycle, tournament, persisted rebuilds) uses the
    same quality prior.
    """
    return evidence_quality_prior(item)


def _graded_signal(
    item: Any,
    *,
    relationship: str = "supports",
    entailment_score: float = 0.8,
) -> EvidenceSignal:
    provenance = item.provenance if isinstance(item.provenance, Mapping) else {}
    family = _source_family(item)
    freshness = 0.9 if str(item.freshness).casefold() == "current" else 0.6
    signal_provenance: dict[str, Any] = {
        "excerpt": item.bounded_excerpt,
        "source_reference": item.source_reference,
    }
    # Deterministic observation rows keep their structured payload so the
    # auditable-evidence predicate can accept excerpt-less contradictions
    # that still carry real observation content (never empty placeholders).
    structured = (
        item.structured_fields if isinstance(item.structured_fields, Mapping) else {}
    )
    if structured:
        signal_provenance["structured_fields"] = dict(structured)
    return EvidenceSignal.create(
        evidence_id=item.evidence_id,
        evidence_type=item.evidence_type,
        relationship=relationship,
        source_name=item.source_name,
        source_family=family,
        origin_key=(provenance.get("origin_key") or item.source_reference or item.ref),
        independence_key=provenance.get("independence_key") or family,
        evidence_fingerprint=item.content_fingerprint,
        source_timestamp=item.source_timestamp,
        available_at=item.available_at,
        quality_score=_evidence_quality(item),
        entailment_score=entailment_score,
        freshness_score=freshness,
        effective_weight=1.0,
        provenance=signal_provenance,
    )


def _signal(item: Any) -> EvidenceSignal:
    """Derive a graded desk challenge signal from collected evidence."""
    return _graded_signal(item)


def _candidate_evidence(
    candidate: Any,
    catalog: Mapping[str, Any],
    *,
    entailment_score: float,
) -> tuple[EvidenceSignal, ...]:
    """Return graded candidate evidence for same-cycle scoring.
    Mirrors ``_attach_cited_evidence`` exactly (same refs, same fingerprint
    dedup, same signal fields): the link rows the cycle just persisted
    postdate the cycle reference, so the same signals enter
    ``evaluate_thesis`` explicitly instead of being dropped by the link
    ``created_at`` cutoff.
    """
    signals: list[EvidenceSignal] = []
    seen: set[str] = set()
    for ref in candidate.evidence_refs:
        item = catalog.get(ref)
        if item is None or item.content_fingerprint in seen:
            continue
        seen.add(item.content_fingerprint)
        signals.append(_graded_signal(item, entailment_score=entailment_score))
    return tuple(signals)


def _candidate_source_gate(
    candidate: Any,
    catalog: Mapping[str, Any],
    *,
    minimum_families: int,
    require_excerpts: bool,
    entailment_score: float,
) -> str | None:
    """Return a bounded rejection reason for unauditable supporting evidence.

    The gate fails closed when no cited item is auditable under the shared
    ``is_auditable_evidence`` predicate (nonblank bounded excerpt, positive
    quality, positive entailment): one valid positive-quality entailed
    report satisfies the support gate, and all-unusable support rejects
    promotion.  ``require_excerpts`` additionally rejects when ANY cited
    item lacks a verbatim excerpt; ``minimum_families`` bounds independent
    source families as before.
    """
    families: set[str] = set()
    seen: set[str] = set()
    cited = 0
    auditable = 0
    for ref in candidate.evidence_refs:
        item = catalog.get(ref)
        if item is None or item.content_fingerprint in seen:
            continue
        seen.add(item.content_fingerprint)
        cited += 1
        if require_excerpts and not str(item.bounded_excerpt or "").strip():
            return "supporting evidence has no verbatim excerpt"
        if is_auditable_evidence(
            _graded_signal(item, entailment_score=entailment_score)
        ):
            auditable += 1
        families.add(_source_family(item).casefold())
    if cited == 0:
        return "no resolvable supporting evidence"
    if auditable == 0:
        return "no auditable supporting evidence"
    if len(families) < minimum_families:
        return (
            f"only {len(families)} independent source families; "
            f"{minimum_families} required"
        )[:160]
    return None


def _candidate_actionability_gate(
    candidate: Any,
    catalog: Mapping[str, Any],
) -> str | None:
    """Require the source types that make a thesis actionable rather than generic."""
    if not all(
        str(value or "").strip()
        for value in (
            candidate.trend_context,
            candidate.valuation_context,
            candidate.sentiment_context,
        )
    ):
        return "trend, valuation, and sentiment context are required"
    citations = {field: refs for field, refs in candidate.citations}

    def adapters(field: str) -> set[str]:
        output: set[str] = set()
        for ref in citations.get(field, ()):
            item = catalog.get(ref)
            if item is None:
                continue
            provenance = item.provenance if isinstance(item.provenance, Mapping) else {}
            output.add(str(provenance.get("adapter") or "").casefold())
        return output

    if "public_equity_trends" not in adapters("trend"):
        return "trend citations require quantified public-equity trend evidence"
    if not adapters("sentiment").intersection(
        {"expectations_sentiment", "positioning_reports", "option_chain_snapshots"}
    ):
        return "sentiment citations require dated expectations or positioning evidence"
    valuation_adapters = adapters("valuation")
    if (
        "investment_analyses" not in valuation_adapters
        or not valuation_adapters.intersection(
            {"public_equities", "public_equity_trends"}
        )
    ):
        return "valuation citations require filing analysis and public market evidence"
    return None


def _derived_attention(signals: Sequence[EvidenceSignal]) -> float | None:
    """Use bounded unique evidence density as a transparent attention proxy."""
    unique = {
        signal.evidence_fingerprint
        for signal in signals
        if signal.evidence_fingerprint is not None
    }
    if not unique:
        return None
    return min(1.0, len(unique) / 20.0)


def _candidate_scenarios(candidate: Any) -> tuple[Scenario, ...]:
    """Current-cycle scenario legs as explicit scoring inputs.

    The candidate legs are the artifacts the cycle just derived and
    persisted; handing them to ``evaluate_thesis`` keeps scoring
    deterministic when their DB rows postdate the cycle reference.
    """
    return tuple(
        Scenario.create(
            label=leg.label,
            probability=leg.probability,
            expected_return=leg.expected_return,
        )
        for leg in candidate.scenarios
    )


def _catalyst_signals(*descriptions: Any) -> tuple[CatalystSignal, ...]:
    """Bounded current-cycle catalyst signals (pending, no expected date).

    This is the explicit current-cycle scoring input for an unreviewed
    candidate.
    """
    signals: list[CatalystSignal] = []
    for description in descriptions:
        catalyst = " ".join(str(description or "").split())
        if catalyst:
            signals.append(
                CatalystSignal.create(
                    description=catalyst, state="pending", expected_at=None
                )
            )
    return tuple(signals)


def _candidate_catalysts(candidate: Any) -> tuple[CatalystSignal, ...]:
    """Current-cycle catalyst signal as an explicit scoring input."""
    return _catalyst_signals(candidate.catalyst)


def _close_at_or_before(
    session: Any,
    symbol: Any,
    as_of: datetime,
    *,
    available_at: datetime | None = None,
    max_age_days: int = _MAX_PRICE_AGE_DAYS,
) -> float | None:
    """Finite, fresh market close timestamped at/before ``as_of``.

    Rows older than ``max_age_days`` are rejected so forecast targets and
    terminal outcomes cannot silently use a months-old price after a feed
    outage or delisting. Row availability remains bounded by
    ``COALESCE(updated_at, created_at)`` at ``available_at`` (default
    ``as_of``), preserving point-in-time replay semantics.
    """
    symbol = _canonical_market_symbol(symbol)
    if symbol is None:
        return None
    available = available_at if available_at is not None else as_of
    earliest = as_of - timedelta(days=max(1, min(int(max_age_days), 31)))
    row = result_first(
        session.execute(
            text(
                """SELECT close FROM market_data
               WHERE symbol = :symbol
                 AND timestamp <= :as_of
                 AND timestamp >= :earliest
                 AND COALESCE(updated_at, created_at) <= :available_at
                 AND close IS NOT NULL
               ORDER BY timestamp DESC,
                        CASE WHEN timeframe = 'PRICE' THEN 0 ELSE 1 END,
                        timeframe ASC, source ASC
               LIMIT 1"""
            ),
            {
                "symbol": symbol[:20],
                "as_of": as_of,
                "earliest": earliest,
                "available_at": available,
            },
        )
    )
    if row is None:
        return None
    try:
        value = float(row.get("close"))
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _resolve_matured_forecasts(
    session: Any,
    reference: datetime,
    *,
    limit: int = _MAX_OUTCOME_RESOLUTION,
) -> dict[str, int]:
    """Resolve matured active PRICE forecasts once, from market_data frozen
    at the deterministic target boundary.

    The boundary is end-of-target-calendar-day UTC: a forecast may resolve
    only after it (a run on the target day leaves it open) and its terminal
    close is the latest daily bar timestamped at/before the boundary that
    was persisted (``created_at``) at/before this run/replay time — never a
    later bar, however delayed the run.  Weekend/holiday targets use the
    prior available close.  Only ``forecast_type='price'`` forecasts are
    owned here; other types remain open for their domain-specific resolver.

    A matured forecast is a hit when the terminal close crosses the frozen
    target in the forecast direction, a miss otherwise.  With no eligible
    price it stays open during the bounded grace and becomes inconclusive
    after it.  Outcomes are recorded once through ``record_forecast_outcome``
    and never overwritten.

    Selection is point-in-time at ``reference``: only forecasts already
    persisted (``created_at``) and frozen (``as_of``) at/before the
    reference are visible, and a forecast is active at the reference when
    it is not superseded yet (``superseded_at`` NULL or after the
    reference).  Forecasts created, frozen, or superseded after the
    reference never enter a historical replay.
    """
    rows = result_rows(
        session.execute(
            text(
                """/* autonomy_forecast_resolution */
               SELECT f.id, f.thesis_id, f.direction, f.target_value,
                      f.target_date, t.symbol
               FROM investment_thesis_forecasts f
               JOIN investment_theses t ON t.id = f.thesis_id
               WHERE (f.superseded_at IS NULL OR f.superseded_at > :reference)
                 AND f.as_of <= :reference
                 AND f.created_at <= :reference
                 AND f.forecast_type = 'price'
                 AND f.target_date IS NOT NULL
                 AND f.target_date < :as_of_date
                 AND NOT EXISTS (
                     SELECT 1 FROM investment_forecast_outcomes o
                     WHERE o.forecast_id = f.id
                 )
               ORDER BY f.target_date, f.id
               LIMIT :limit"""
            ),
            {
                "reference": reference,
                "as_of_date": reference.date(),
                "limit": max(1, min(limit, _MAX_OUTCOME_RESOLUTION)),
            },
        )
    )
    counts = {"hit": 0, "miss": 0, "inconclusive": 0, "open": 0}
    grace = timedelta(days=_FORECAST_GRACE_DAYS)
    for row in rows:
        forecast_id = str(row["id"])
        target_day = row.get("target_date")
        if target_day is None:
            counts["open"] += 1
            continue
        if isinstance(target_day, str):
            target_day = date.fromisoformat(target_day.split("T", 1)[0])
        close = _close_at_or_before(
            session,
            row.get("symbol"),
            _target_boundary(target_day),
            available_at=reference,
        )
        target = row.get("target_value")
        try:
            target_value = float(target) if target is not None else None
        except (TypeError, ValueError, OverflowError):
            target_value = None
        if close is None or target_value is None:
            if reference.date() > target_day + grace:
                record_forecast_outcome(
                    session,
                    forecast_id,
                    status="inconclusive",
                    measured_at=reference,
                    notes="no market price available within grace",
                )
                counts["inconclusive"] += 1
            else:
                counts["open"] += 1
            continue
        direction = str(row.get("direction") or "up")
        if direction == "down":
            status = "hit" if close <= target_value else "miss"
        elif direction == "flat":
            status = "hit" if close == target_value else "miss"
        else:
            status = "hit" if close >= target_value else "miss"
        if record_forecast_outcome(
            session,
            forecast_id,
            status=status,
            actual_value=close,
            measured_at=reference,
            notes="autonomous outcome resolution",
        ):
            counts[status] += 1
        else:
            counts["open"] += 1
    return counts


def _existing_fusion_mechanism(
    session: Any,
    *,
    company: str | None,
    symbol: str | None,
    direction: str,
    horizon: str,
    fallback: str | None,
) -> str | None:
    """Reuse the best live exposure identity instead of creating paraphrases."""
    row = result_first(
        session.execute(
            text(
                """SELECT mechanism
               FROM investment_theses
               WHERE origin = 'fusion'
                 AND status IN ('candidate', 'active', 'paused')
                 AND direction = :direction
                 AND LOWER(BTRIM(COALESCE(horizon, ''))) = :horizon
                 AND (
                     (:symbol IS NOT NULL AND UPPER(BTRIM(symbol)) = :symbol)
                     OR
                     (:symbol IS NULL AND :company IS NOT NULL
                      AND LOWER(BTRIM(company)) = :company)
                 )
               ORDER BY opportunity_score DESC NULLS LAST,
                        last_evaluated_at DESC NULLS LAST, id
               LIMIT 1"""
            ),
            {
                "company": str(company).strip().casefold() if company else None,
                "symbol": _canonical_market_symbol(symbol),
                "direction": str(direction).strip().casefold(),
                "horizon": _normalized(horizon),
            },
        )
    )
    return row.get("mechanism") if row is not None else fallback


def _snapshot_for_candidate(
    thesis_id: str,
    candidate: Any,
    *,
    reference: datetime,
    cost: float,
) -> ThesisSnapshot:
    scenarios = tuple(
        Scenario.create(
            label=leg.label,
            probability=leg.probability,
            expected_return=leg.expected_return,
        )
        for leg in candidate.scenarios
    )
    return ThesisSnapshot.create(
        thesis_id=thesis_id,
        statement=candidate.claim,
        direction=candidate.direction,
        as_of=reference,
        cost=cost,
        scenarios=scenarios,
        claims=[
            ThesisClaim.create(
                claim_id=f"{thesis_id}:autonomy:{_cycle_key(reference)}",
                statement=candidate.claim,
                citations=list(candidate.evidence_refs),
            )
        ],
    )


def _signal_maps(
    signals: Sequence[EvidenceSignal],
) -> tuple[dict[str, EvidenceSignal], dict[str, EvidenceSignal]]:
    id_map: dict[str, EvidenceSignal] = {}
    ref_map: dict[str, EvidenceSignal] = {}
    for signal in signals:
        id_map.setdefault(signal.evidence_id, signal)
        ref_map.setdefault(signal.ref, signal)
    return id_map, ref_map


def _contradiction_signals(
    decision: Any,
    signals: Sequence[EvidenceSignal],
) -> tuple[EvidenceSignal, ...]:
    """The challenger-cited signals a decision attaches as contradictions.

    Mirrors ``_attach_contradictions`` selection exactly (same citation
    resolution, same fingerprint dedup), so the recompute hands the exact
    signals the run persisted as ``contradicts`` rows -- whose links
    postdate the cycle reference -- back to ``evaluate_thesis`` explicitly.
    Only auditable contradictions are picked: the citation is audited as
    the exact artifact that would attach (relationship flipped to
    ``contradicts``), and it must carry a nonblank bounded excerpt or a
    non-empty structured observation payload with positive quality, so
    empty zero-quality FRED/story placeholders and excerpt-less rows
    without a structured payload never attach or contribute contradiction
    mass.
    """
    id_map, ref_map = _signal_maps(signals)
    picked: list[EvidenceSignal] = []
    seen: set[str] = set()
    for finding in decision.runner_findings:
        for citation in finding.citations:
            signal = id_map.get(citation) or ref_map.get(citation)
            if signal is None or signal.evidence_fingerprint in seen:
                continue
            seen.add(signal.evidence_fingerprint)
            # Audit the exact artifact that would attach: the citation as a
            # contradiction.  The structured-payload alternative applies
            # only to contradictions, so a structured support/context row
            # without a verbatim excerpt is still dropped here.
            contradiction = replace(signal, relationship="contradicts")
            if not is_auditable_evidence(contradiction, allow_structured=True):
                continue
            picked.append(contradiction)
    return tuple(picked)


def _evaluate_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "cost": float(settings.get("cost") or 0.0),
        "attention": None,
        "crowding": None,
        "liquidity": settings.get("liquidity"),
        "downside": settings.get("downside"),
    }


def _tracked_cost(obj: Any) -> float:
    try:
        return float(getattr(obj, "cost_usd", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _tracked_calls(obj: Any) -> int:
    try:
        return int(getattr(obj, "calls", 0) or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Public cycle entry point
# ---------------------------------------------------------------------------


def run_autonomous_thesis_cycle(
    session: Any,
    config: Mapping[str, Any],
    *,
    correlation_id: str | None = None,
    as_of: datetime | str | None = None,
    runner: RoleRunner | None = None,
    challenger: ChallengeRunner | None = None,
    auditor: Any = None,
) -> dict[str, Any]:
    """Run one bounded autonomous thesis cycle in the caller's transaction.

    The caller owns commit and rollback. ``runner``, ``challenger``, and
    ``auditor`` default to the production adapters, which share one bounded
    per-run model budget, and may be replaced with credential-free test fakes.

    ``as_of`` is the availability cutoff for evidence and market inputs. Two
    independent generation roles produce competing candidates. Deterministic
    citation, opposition, scoring, and budget gates run before surviving
    candidates are staged as immutable ``investment_thesis_proposals``.
    Generated output never mutates canonical thesis records; only an explicit
    human review action may approve, reject, or request revision.
    """
    settings = _settings(config)
    if not settings["enabled"]:
        return {"status": "disabled", "error_count": 0, "cost_usd": 0.0}
    reference = _as_utc(as_of)
    cycle_key = _cycle_key(reference)
    evaluate_inputs = _evaluate_settings(settings)
    error_count = 0
    errors: list[str] = []

    outcome_counts = _resolve_matured_forecasts(session, reference)
    evidence_items, evidence_failures = _collect_evidence(
        session, settings, reference=reference
    )
    catalog = {item.ref: item for item in evidence_items}
    theme_id, theme_created = _ensure_system_theme(session)

    # Generation/citation auditing and falsification have independent child
    # ceilings whose sum is the hard per-run cap. Generation can never spend
    # the allocation required to challenge promoted and existing theses.
    total_budget = float(settings.get("model_budget_usd_per_run") or 0.0)
    falsification_fraction = float(
        settings.get("falsification_budget_fraction") or 0.45
    )
    generation_budget = _ModelBudget(total_budget * (1.0 - falsification_fraction))
    falsification_budget = _ModelBudget(total_budget * falsification_fraction)
    if runner is None:
        runner = LLMRoleRunner(
            config,
            correlation_id=correlation_id,
            session=session,
            settings=settings,
            model=settings.get("model_override"),
            reasoning_effort=settings.get("reasoning_effort"),
            max_output_tokens=settings.get("max_output_tokens"),
            budget=generation_budget,
        )
    if challenger is None:
        challenger = LLMChallenger(
            config,
            correlation_id=correlation_id,
            session=session,
            settings=settings,
            model=settings.get("model_override"),
            reasoning_effort=settings.get("reasoning_effort"),
            max_output_tokens=settings.get("max_output_tokens"),
            budget=falsification_budget,
            evidence_catalog=catalog,
        )
    if auditor is None:
        auditor = LLMSemanticCitationAuditor(
            config,
            correlation_id=correlation_id,
            session=session,
            settings=settings,
            model=settings.get("model_override"),
            reasoning_effort=settings.get("reasoning_effort"),
            max_output_tokens=settings.get("max_output_tokens"),
            budget=generation_budget,
        )

    tournament = run_tournament(
        theme_id=theme_id,
        runner=runner,
        auditor=auditor,
        evidence=evidence_items,
        roles=ROLES,
        max_raw_candidates=_bounded(settings.get("maximum_evidence", 96), 96, 2000),
        max_promoted=_bounded(settings.get("maximum_promoted", 64), 64, 64),
        cost=evaluate_inputs["cost"],
        attention=evaluate_inputs["attention"],
        crowding=evaluate_inputs["crowding"],
        liquidity=evaluate_inputs["liquidity"],
        downside=evaluate_inputs["downside"],
        as_of=reference,
    )
    audit_entailment = {
        decision.candidate_key: {
            "entailed": 1.0,
            "mixed": 0.6,
            "unsupported": 0.0,
            "contradicted": 0.0,
        }.get(str(decision.verdict), 0.0)
        for decision in tournament.audit_decisions
    }
    challenge_signals = tuple(_signal(item) for item in evidence_items)

    role_failures = sum(
        1
        for rejected in tournament.rejected
        if str(rejected.reason).startswith("role runner failed")
    )

    promoted_ids: list[str] = []
    proposals_staged = 0
    proposals_created = 0
    proposals_replayed = 0
    challenger_failures = 0
    promotion_gate_rejections = 0
    source_gate_rejections = 0
    actionability_gate_rejections = 0
    opposition_gate_rejections = 0

    minimum_families = _bounded(
        settings.get("minimum_supporting_source_families", 1), 1, 10
    )
    require_excerpts = bool(settings.get("require_cited_excerpts", False))
    require_opposition = bool(settings.get("require_opposing_variants", False))
    challenge_limit = _bounded(settings.get("maximum_challenges_per_run", 25), 25, 100)
    challenge_attempts = 0
    promotion_inputs: dict[str, tuple[Any, ...]] = {}
    surviving_directions: dict[tuple[str, str], set[str]] = {}
    for ranked in tournament.ranked:
        candidate = ranked.candidate
        entailment_score = audit_entailment.get(candidate.candidate_key, 0.8)
        source_rejection = _candidate_source_gate(
            candidate,
            catalog,
            minimum_families=minimum_families,
            require_excerpts=require_excerpts,
            entailment_score=entailment_score,
        )
        if source_rejection is not None:
            source_gate_rejections += 1
            promotion_gate_rejections += 1
            continue
        actionability_rejection = _candidate_actionability_gate(candidate, catalog)
        if actionability_rejection is not None:
            actionability_gate_rejections += 1
            promotion_gate_rejections += 1
            continue
        if challenge_attempts >= challenge_limit:
            promotion_gate_rejections += 1
            continue
        try:
            company, market_symbol = resolve_candidate_entities(candidate, catalog)
            market_symbol = _canonical_market_symbol(market_symbol)
            subject = (
                (market_symbol or company or candidate.subject or "").strip().casefold()
            )
            competition_key = (subject, _normalized(candidate.horizon))
            challenge_attempts += 1
            predecision = challenge_thesis(
                _snapshot_for_candidate(
                    candidate.content_fingerprint[:120],
                    candidate,
                    reference=reference,
                    cost=evaluate_inputs["cost"],
                ),
                challenge_signals,
                runner=challenger,
            )
            if predecision.runner_failed:
                challenger_failures += 1
                promotion_gate_rejections += 1
                continue
            if predecision.citation_failures or predecision.state == "breached":
                promotion_gate_rejections += 1
                continue
            direction = str(candidate.direction).strip().casefold()
            promotion_inputs[candidate.content_fingerprint] = (
                company,
                market_symbol,
                competition_key,
                entailment_score,
                predecision,
            )
            surviving_directions.setdefault(competition_key, set()).add(direction)
        except Exception as exc:
            error_count += 1
            errors.append(type(exc).__name__[:60])
            if len(errors) >= _MAX_ERRORS:
                break

    accepted_exposures: set[tuple[tuple[str, str], str]] = set()
    for ranked in tournament.ranked:
        candidate = ranked.candidate
        promotion_input = promotion_inputs.get(candidate.content_fingerprint)
        if promotion_input is None:
            continue
        company, market_symbol, competition_key, entailment_score, predecision = (
            promotion_input
        )
        try:
            direction = str(candidate.direction).strip().casefold()
            opposite_direction = {"long": "short", "short": "long"}.get(direction)
            if require_opposition and (
                opposite_direction is None
                or opposite_direction
                not in surviving_directions.get(competition_key, set())
            ):
                opposition_gate_rejections += 1
                promotion_gate_rejections += 1
                continue
            exposure_key = (competition_key, direction)
            if exposure_key in accepted_exposures:
                opposition_gate_rejections += 1
                promotion_gate_rejections += 1
                continue
            accepted_exposures.add(exposure_key)
            identity_mechanism = (
                _existing_fusion_mechanism(
                    session,
                    company=company,
                    symbol=market_symbol,
                    direction=candidate.direction,
                    horizon=candidate.horizon,
                    fallback=candidate.mechanism,
                )
                if require_opposition
                else candidate.mechanism
            )
            canonical_key = canonical_thesis_key(
                theme_id=theme_id,
                subject=candidate.subject or company or market_symbol,
                direction=candidate.direction,
                horizon=candidate.horizon,
                mechanism=identity_mechanism,
            )

            # Pure candidate scoring (in memory)
            candidate_scenarios = _candidate_scenarios(candidate)
            candidate_catalysts = _candidate_catalysts(candidate)
            candidate_evidence = _candidate_evidence(
                candidate,
                catalog,
                entailment_score=entailment_score,
            )

            decision = predecision
            contradiction_sigs = _contradiction_signals(decision, challenge_signals)
            all_scoring_evidence = list(candidate_evidence) + list(contradiction_sigs)

            evidence_assessment = assess_evidence(all_scoring_evidence)
            val = scenario_valuation(candidate_scenarios)
            cat = catalyst_readiness(candidate_catalysts, as_of=reference)
            neglect = calculate_neglect(
                attention=_derived_attention(all_scoring_evidence),
                crowding=evaluate_inputs.get("crowding"),
            )
            downside_value = evaluate_inputs.get("downside")
            if downside_value is None:
                if (
                    int(val.scenario_count) > 0
                    and int(val.missing_probability_count) == 0
                    and bool(val.probabilities_sum_to_one)
                ):
                    downside_value = min(
                        1.0,
                        max(0.0, float(val.expected_shortfall) / DOWNSIDE_NORMALIZER),
                    )
            scored_opp = assess_opportunity(
                evidence_strength=evidence_assessment.support_mass,
                confidence=evidence_assessment.confidence,
                neglect=neglect.neglect,
                catalyst_ready=cat.readiness,
                liquidity=evaluate_inputs.get("liquidity"),
                downside=downside_value,
            )

            # Query matching canonical thesis (read-only)
            matching_row = result_first(
                session.execute(
                    text(
                        """SELECT id, subject, claim, variant_perception, catalyst_summary,
                                  direction, horizon, mechanism, confidence_score,
                                  opportunity_score, evidence_strength, contradiction_strength,
                                  version, updated_at
                           FROM investment_theses
                           WHERE canonical_key = :canonical_key
                           LIMIT 1"""
                    ),
                    {"canonical_key": canonical_key},
                )
            )

            matching_thesis_id = None
            diff_payload: dict[str, Any] = {}
            if matching_row:
                matching_thesis_id = str(matching_row["id"])
                changed_fields: list[str] = []
                diff_payload = {
                    "matching_thesis_id": matching_thesis_id,
                    "existing_version": matching_row.get("version"),
                    "claim": {
                        "old": matching_row.get("claim"),
                        "new": candidate.claim,
                    },
                    "variant_perception": {
                        "old": matching_row.get("variant_perception"),
                        "new": candidate.variant_perception,
                    },
                    "catalyst_summary": {
                        "old": matching_row.get("catalyst_summary"),
                        "new": candidate.catalyst,
                    },
                    "confidence": {
                        "old": matching_row.get("confidence_score"),
                        "new": candidate.confidence,
                    },
                    "opportunity_score": {
                        "old": matching_row.get("opportunity_score"),
                        "new": scored_opp.opportunity,
                    },
                }
                for fld in ("claim", "variant_perception", "catalyst_summary"):
                    if diff_payload[fld]["old"] != diff_payload[fld]["new"]:
                        changed_fields.append(fld)
                diff_payload["changed_fields"] = changed_fields
            else:
                diff_payload = {
                    "is_new": True,
                    "matching_thesis_id": None,
                }

            staged_scenarios = [
                {
                    "name": leg.label,
                    "description": path,
                    "probability": leg.probability,
                    "expected_return": leg.expected_return,
                    "is_base_case": leg.label == "base",
                }
                for leg, path in zip(
                    candidate.scenarios, candidate.scenario_paths, strict=True
                )
            ]
            staged_evidence = []
            for sig in all_scoring_evidence:
                prov = (
                    dict(sig.provenance) if isinstance(sig.provenance, Mapping) else {}
                )
                staged_evidence.append(
                    {
                        "evidence_id": sig.evidence_id,
                        "evidence_type": sig.evidence_type,
                        "evidence_ref": sig.ref,
                        "relationship": sig.relationship,
                        "source_name": sig.source_name,
                        "source_family": sig.source_family,
                        "origin_key": sig.origin_key,
                        "independence_key": sig.independence_key,
                        "evidence_fingerprint": sig.evidence_fingerprint,
                        "source_timestamp": (
                            sig.source_timestamp.isoformat()
                            if hasattr(sig.source_timestamp, "isoformat")
                            else str(sig.source_timestamp)
                        ),
                        "available_at": (
                            sig.available_at.isoformat()
                            if hasattr(sig.available_at, "isoformat")
                            else str(sig.available_at)
                        ),
                        "quality_score": sig.quality_score,
                        "entailment_score": sig.entailment_score,
                        "freshness_score": sig.freshness_score,
                        "effective_weight": sig.effective_weight,
                        "excerpt": prov.get("excerpt"),
                        "structured_fields": prov.get("structured_fields"),
                        "provenance": prov,
                    }
                )
            staged_scoring = {
                "opportunity_score": scored_opp.opportunity,
                "expected_value": val.expected_value,
                "expected_shortfall": val.expected_shortfall,
                "confidence_score": scored_opp.confidence,
                "neglect_score": scored_opp.neglect,
                "catalyst_score": scored_opp.catalyst_ready,
                "evidence_strength": scored_opp.evidence_strength,
                "contradiction_strength": evidence_assessment.contradiction_mass,
                "opportunity_status": (
                    "blocked" if scored_opp.blocked_by else "evaluated"
                ),
            }
            staged_challenge = {
                "state": decision.state,
                "runner_failed": decision.runner_failed,
                "contradiction_refs": [
                    s.evidence_fingerprint for s in contradiction_sigs
                ],
                "findings": [
                    f.to_dict() if hasattr(f, "to_dict") else f
                    for f in getattr(decision, "runner_findings", ())
                ],
            }

            proposal_key = f"proposal:{cycle_key}:{candidate.content_fingerprint}"
            candidate_payload = candidate.to_dict()
            candidate_payload["opportunity_score"] = scored_opp.opportunity

            proposal_res = create_thesis_proposal(
                session,
                proposal_key=proposal_key,
                canonical_key=canonical_key,
                theme_id=theme_id,
                company=company,
                symbol=market_symbol,
                subject=candidate.subject,
                direction=candidate.direction,
                horizon=candidate.horizon,
                mechanism=identity_mechanism,
                payload=candidate_payload,
                evidence=staged_evidence,
                scenarios=staged_scenarios,
                scoring=staged_scoring,
                challenge=staged_challenge,
                diff=diff_payload,
                matching_thesis_id=matching_thesis_id,
                accepted_reference=reference,
            )
            proposals_staged += 1
            if proposal_res.get("created"):
                proposals_created += 1
            else:
                proposals_replayed += 1
            promoted_ids.append(str(proposal_res.get("id")))

        except Exception as exc:
            error_count += 1
            errors.append(type(exc).__name__[:60])
            if len(errors) >= _MAX_ERRORS:
                break

    return {
        "status": (
            "partial"
            if error_count or role_failures or challenger_failures or evidence_failures
            else "completed"
        ),
        "as_of": reference.isoformat(),
        "cycle_key": cycle_key,
        "theme_id": theme_id,
        "theme_created": theme_created,
        "correlation_id": correlation_id,
        "evidence_collected": len(evidence_items),
        "evidence_failures": dict(evidence_failures),
        "raw_candidate_count": int(tournament.raw_candidate_count),
        "promoted_count": 0,
        "proposals_staged": proposals_staged,
        "proposals_created": proposals_created,
        "proposals_replayed": proposals_replayed,
        "tournament_promoted_count": len(tournament.ranked),
        "promotion_gate_rejections": promotion_gate_rejections,
        "source_gate_rejections": source_gate_rejections,
        "actionability_gate_rejections": actionability_gate_rejections,
        "opposition_gate_rejections": opposition_gate_rejections,
        "rejected_count": len(tournament.rejected),
        "outcome_resolution": outcome_counts,
        "challenge_attempts": challenge_attempts,
        "challenge_limit": challenge_limit,
        "semantic_audit_rejections": sum(
            1
            for rejected in tournament.rejected
            if str(rejected.reason).startswith("citation audit failed:verdict:")
            or str(rejected.reason).startswith("citation audit failed:missing decision")
        ),
        "role_failures": role_failures,
        "challenger_failures": challenger_failures,
        "error_count": error_count,
        "errors": errors,
        "cost_usd": (
            _tracked_cost(runner) + _tracked_cost(challenger) + _tracked_cost(auditor)
        ),
        "model_calls": (
            _tracked_calls(runner)
            + _tracked_calls(challenger)
            + _tracked_calls(auditor)
        ),
    }


__all__ = [
    "JOB_TYPE",
    "LLMChallenger",
    "LLMRoleRunner",
    "LLMSemanticCitationAuditor",
    "SYSTEM_THEME_DEFINITION",
    "SYSTEM_THEME_NAME",
    "challenge_output_schema",
    "citation_audit_output_schema",
    "enqueue_thesis_autonomy_job",
    "run_autonomous_thesis_cycle",
    "thesis_autonomy_identity",
]
