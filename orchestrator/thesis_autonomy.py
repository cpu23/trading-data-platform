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

from sqlalchemy import text

from analysis_jobs import enqueue_job
from budgets import BudgetContext
from contracts.runtime_config import ThesisAutonomyConfig
from db import get_session
from llm_client import LLMStage
from orchestrator import accept_run, finalize_run_safely, start_run
from research_intelligence.context import ResearchContext
from research_intelligence.contracts import (
    EvidenceSignal,
    Scenario,
    canonical_fingerprint,
)
from research_intelligence.evidence import (
    DEFAULT_ADAPTERS,
    EvidenceRegistry,
    exact_evidence_lookup,
)
from thesis_challenges import (
    MAX_CITATIONS_PER_CLAIM,
    PROPOSAL_KINDS,
    ChallengeProposal,
    ChallengeRunner,
    ThesisClaim,
    ThesisCondition,
    ThesisSnapshot,
    challenge_thesis,
)
from thesis_fusion import (
    add_group_membership,
    attach_evidence,
    create_find_group,
    evaluate_thesis,
    freeze_forecast,
    link_position,
    merge_or_create_thesis,
    record_falsification_run,
    record_forecast_outcome,
    update_falsification_run,
    upsert_scenario,
)
from thesis_playbooks import build_event_playbook, upsert_event_playbook
from thesis_scoring import (
    CatalystSignal,
    evidence_quality_prior,
    is_auditable_evidence,
)
from thesis_tournament import (
    CITATION_FIELDS,
    MAX_SEMANTIC_AUDIT_BATCH,
    ROLES,
    RoleRunner,
    resolve_candidate_entities,
    resolve_evidence_market_identity,
    run_tournament,
)

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
_MAX_ATTACH_EVIDENCE = 50
_MAX_ATTACH_CONTRADICTIONS = 50
_MAX_SECOND_PASS_SCENARIOS = 64
_MAX_SECOND_PASS_EVIDENCE = 256
_MAX_CONDITIONS = 64
_MAX_GROUP_NAME = 200
_MAX_OUTCOME_RESOLUTION = 100
_MAX_FORECAST_BACKFILL = 300
MAX_AUDIT_CANDIDATES = MAX_SEMANTIC_AUDIT_BATCH
MAX_AUDIT_CITED_REFS = 30
MAX_UNSUPPORTED_CLAIMS = 10

#: Conservative deterministic risk materialization for promoted candidate
#: invalidators: every explicit invalidation condition is a counter-thesis
#: risk, and severity is the neutral conservative default — never inferred
#: from text, never understated, never inflated.
_CANDIDATE_RISK_KIND = "counter_thesis"
_CANDIDATE_RISK_SEVERITY = "moderate"

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

#: Recent context-match window for second-pass prioritization: theses whose
#: playbooks matched recent market events rank ahead of generic
#: high-opportunity candidates (linked positions still first).
_CONTEXT_MATCH_WINDOW_DAYS = 7

_FALSIFICATION_STATUS_BY_STATE = {
    "breached": "falsified",
    "intact": "not_falsified",
    "threatened": "inconclusive",
}


def _rows(result: Any) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in result.mappings().all()]
    except AttributeError:
        return [dict(row._mapping) for row in result]


def _first(result: Any) -> dict[str, Any] | None:
    try:
        row = result.mappings().first()
    except AttributeError:
        row = result.first()
    return dict(row) if row is not None else None


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
    row = _first(
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
    existing = _first(
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


def _signal_from_row(row: Mapping[str, Any]) -> EvidenceSignal:
    """Rebuild a desk signal from one persisted evidence row.

    Mirrors ``thesis_fusion._evidence_signal_from_row``: legacy rows without
    a fingerprint synthesize a deterministic content identity from the row
    key so they participate in scoring without being re-fingerprinted.
    """
    fingerprint = row.get("evidence_fingerprint")
    if not fingerprint:
        fingerprint = canonical_fingerprint(
            {
                "legacy_evidence": (
                    row.get("evidence_type"),
                    row.get("evidence_id"),
                    row.get("relationship"),
                    row.get("source_family") or "manual",
                )
            }
        )
    family = row.get("source_family") or "manual"
    return EvidenceSignal.create(
        evidence_id=row.get("evidence_id"),
        evidence_type=row.get("evidence_type") or "source_claim",
        relationship=row.get("relationship") or "context",
        source_name=family,
        source_family=family,
        origin_key=row.get("origin_key"),
        independence_key=row.get("independence_key"),
        evidence_fingerprint=fingerprint,
        source_timestamp=row.get("source_timestamp") or row.get("created_at"),
        available_at=row.get("available_at")
        or row.get("source_timestamp")
        or row.get("created_at"),
        quality_score=row.get("quality_score"),
        entailment_score=row.get("entailment_score"),
        freshness_score=row.get("freshness_score"),
        effective_weight=row.get("effective_weight"),
        provenance={"excerpt": row.get("excerpt")},
    )


def _attach_cited_evidence(
    session: Any,
    thesis_id: str,
    candidate: Any,
    catalog: Mapping[str, Any],
    *,
    entailment_score: float,
) -> dict[str, int]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in candidate.evidence_refs:
        item = catalog.get(ref)
        if item is None or item.content_fingerprint in seen:
            continue
        seen.add(item.content_fingerprint)
        signal = _graded_signal(item, entailment_score=entailment_score)
        rows.append(
            {
                "evidence_type": signal.evidence_type,
                "evidence_id": signal.evidence_id,
                "relationship": signal.relationship,
                "excerpt": item.bounded_excerpt,
                "source_name": signal.source_name,
                "source_family": signal.source_family,
                "origin_key": signal.origin_key,
                "independence_key": signal.independence_key,
                "evidence_fingerprint": signal.evidence_fingerprint,
                "source_timestamp": signal.source_timestamp,
                "available_at": signal.available_at,
                "quality_score": signal.quality_score,
                "entailment_score": signal.entailment_score,
                "freshness_score": signal.freshness_score,
                "effective_weight": signal.effective_weight,
            }
        )
    return attach_evidence(session, thesis_id, rows, limit=_MAX_ATTACH_EVIDENCE)


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


def _persist_scenarios(
    session: Any,
    thesis_id: str,
    candidate: Any,
) -> tuple[dict[str, str], int]:
    """Persist bull/base/bear legs; returns ({label: scenario_id}, changed).

    Each leg's bounded path/assumptions description is persisted verbatim
    to ``investment_thesis_scenarios.description`` alongside its
    probability and expected return.
    """
    scenario_ids: dict[str, str] = {}
    changed_count = 0
    for leg, path in zip(candidate.scenarios, candidate.scenario_paths, strict=True):
        result = upsert_scenario(
            session,
            thesis_id,
            name=leg.label,
            description=path,
            probability=leg.probability,
            expected_return=leg.expected_return,
            is_base_case=leg.label == "base",
        )
        scenario_ids[leg.label] = str(result["id"])
        if result.get("changed"):
            changed_count += 1
    return scenario_ids, changed_count


def _persist_candidate_risks(
    session: Any,
    thesis_id: str,
    candidate: Any,
) -> int:
    """Materialize each validated candidate invalidator as one risk row.

    One structured ``investment_risks`` row per supplied invalidator —
    never more, never invented from other content.  Kind and severity are
    conservative deterministic constants: an explicit invalidation
    condition is always a ``counter_thesis`` risk, and severity is the
    neutral ``moderate`` default (never understated, never inferred from
    text).

    Reruns are idempotent: the absence check is closed by a
    transaction-scoped advisory lock keyed by the exact identity
    ``(thesis_id, normalized description)`` plus a ``NOT EXISTS`` guard,
    so two concurrent cycles can never insert the same risk twice.  The
    promote path already holds the thesis's fusion canonical-key lock
    (merge is reentrant), and this identity lock is the only later lock,
    so lock order stays single and acyclic.  Returns the number of rows
    newly inserted.
    """
    persisted = 0
    for invalidator in candidate.invalidators:
        risk = " ".join(str(invalidator or "").split())[:2000]
        if not risk:
            continue
        session.execute(
            text(
                "/* risk_identity_lock */ "
                "SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"
            ),
            {"lock_key": f"risk_identity:{thesis_id}:{risk}"},
        )
        row = _first(
            session.execute(
                text(
                    """INSERT INTO investment_risks
                       (thesis_id, description, kind, severity)
                       SELECT CAST(:thesis_id AS UUID), :description,
                              :kind, :severity
                       WHERE NOT EXISTS (
                           SELECT 1 FROM investment_risks
                           WHERE thesis_id = CAST(:thesis_id AS UUID)
                             AND description = :description
                       )
                       RETURNING id"""
                ),
                {
                    "thesis_id": thesis_id,
                    "description": risk,
                    "kind": _CANDIDATE_RISK_KIND,
                    "severity": _CANDIDATE_RISK_SEVERITY,
                },
            )
        )
        if row is not None:
            persisted += 1
    return persisted


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

    Mirrors what ``_ensure_candidate_catalyst`` persists so the explicit
    scoring input and the persisted row stay identical.
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


def _horizon_days(horizon: Any) -> int:
    try:
        return max(
            1,
            min(
                _MAX_HORIZON_DAYS,
                int(
                    _HORIZON_DAYS.get(
                        str(horizon).strip().casefold(), _DEFAULT_HORIZON_DAYS
                    )
                ),
            ),
        )
    except (TypeError, ValueError):
        return _DEFAULT_HORIZON_DAYS


def _enrich_thesis_market_identity(
    session: Any,
    thesis_id: str,
    *,
    company: str | None,
    symbol: str | None,
    normalize_symbol: bool = False,
) -> bool:
    """Fill legacy-null identity fields without overwriting curated values.

    Curated non-null company/symbol values are always preserved.  When
    ``normalize_symbol`` is set, a populated symbol is rewritten to the
    supplied canonical form only when it is the same symbol identity
    (identical after canonical trim/uppercase) — a genuinely different
    symbol is never overwritten.
    """
    if company is None and symbol is None and not normalize_symbol:
        return False
    result = session.execute(
        text(
            """UPDATE investment_theses
               SET company = CASE
                       WHEN NULLIF(BTRIM(company), '') IS NULL THEN :company
                       ELSE company
                   END,
                   symbol = CASE
                       WHEN NULLIF(BTRIM(symbol), '') IS NULL THEN :symbol
                       WHEN :normalize_symbol AND :symbol IS NOT NULL
                            AND UPPER(BTRIM(symbol)) = UPPER(BTRIM(:symbol))
                            THEN :symbol
                       ELSE symbol
                   END,
                   updated_at = NOW()
               WHERE id = CAST(:id AS UUID)
                 AND (
                     (NULLIF(BTRIM(company), '') IS NULL AND :company IS NOT NULL)
                     OR
                     (NULLIF(BTRIM(symbol), '') IS NULL AND :symbol IS NOT NULL)
                     OR
                     (:normalize_symbol AND :symbol IS NOT NULL
                      AND UPPER(BTRIM(symbol)) = UPPER(BTRIM(:symbol)))
                 )"""
        ),
        {
            "id": thesis_id,
            "company": company,
            "symbol": symbol,
            "normalize_symbol": normalize_symbol,
        },
    )
    return bool(getattr(result, "rowcount", 0))


def _backfill_missing_market_identities(
    session: Any,
    catalog: Mapping[str, Any],
    *,
    reference: datetime | None = None,
    limit: int = 100,
) -> int:
    """Backfill bounded legacy fusion theses from their persisted citations.

    A historical/delayed run never rewrites a thesis whose current state is
    not provably visible at its accepted ``reference``: the thesis must be
    created and last updated at/before the reference and must not carry an
    accepted fusion reference later than it (``fusion_reference_at`` NULL
    or <= reference), so an older or stale job cannot overwrite identity
    state on a thesis a newer cycle already claimed or updated.  Missing
    timestamps fail closed (a NULL ``created_at``/``updated_at`` can never
    be proven visible at the reference); without a reference bound the
    legacy no-cutoff behavior applies.  Citations resolve against the
    current rolling evidence catalog first; when that yields no identity at
    all, citations outside the catalog/lookback are recovered by exact ID
    from persisted source records (bounded, point-in-time-checked at
    ``reference``) and merged into a second deterministic resolution.
    Catalog-first ordering means recovered evidence never overturns an
    already-resolved or already-ambiguous catalog outcome.  Only exact
    citation identities participate — uncited or ambiguous records never
    supply company/symbol.
    """
    rows = _rows(
        session.execute(
            text(
                """/* autonomy_identity_backfill */
                   SELECT t.id, t.claim, t.company, t.symbol,
                          e.evidence_type, e.evidence_id
                   FROM (
                       SELECT id, claim, company, symbol, created_at
                       FROM investment_theses
                       WHERE origin = 'fusion'
                         AND (
                             NULLIF(BTRIM(company), '') IS NULL
                             OR NULLIF(BTRIM(symbol), '') IS NULL
                         )
                         AND (:reference IS NULL OR created_at <= :reference)
                         AND (:reference IS NULL OR updated_at <= :reference)
                         AND (:reference IS NULL
                              OR fusion_reference_at IS NULL
                              OR fusion_reference_at <= :reference)
                       ORDER BY created_at, id
                       LIMIT :limit
                   ) t
                   LEFT JOIN investment_thesis_evidence e ON e.thesis_id = t.id
                   ORDER BY t.created_at, t.id, e.evidence_id"""
            ),
            {
                "reference": reference,
                "limit": max(1, min(int(limit), 500)),
            },
        )
    )
    pending: dict[str, dict[str, Any]] = {}
    missing_refs: list[str] = []
    missing_seen: set[str] = set()
    for row in rows:
        thesis_id = str(row["id"])
        item = pending.setdefault(
            thesis_id,
            {
                "claim": str(row.get("claim") or ""),
                "company": row.get("company"),
                "symbol": row.get("symbol"),
                "evidence_refs": [],
            },
        )
        evidence_id = str(row.get("evidence_id") or "")
        evidence_type = str(row.get("evidence_type") or "")
        typed_ref = (
            f"{evidence_type}:{evidence_id}" if evidence_type and evidence_id else ""
        )
        if typed_ref:
            item["evidence_refs"].append(typed_ref)
            if typed_ref not in catalog and typed_ref not in missing_seen:
                missing_seen.add(typed_ref)
                missing_refs.append(typed_ref)
        # Legacy rows may carry an untyped/bare id; only the in-memory
        # catalog can resolve those (persistence needs the type).
        if evidence_id:
            item["evidence_refs"].append(evidence_id)

    recovered = exact_evidence_lookup(
        session, tuple(missing_refs), available_by=reference, limit=2000
    )
    merged: dict[str, Any] = {**recovered, **catalog}

    changed = 0
    for thesis_id, item in pending.items():
        evidence_refs = tuple(dict.fromkeys(item["evidence_refs"]))
        subject = str(item["company"] or item["claim"])
        instrument = str(item["symbol"] or item["claim"])
        company, symbol = resolve_evidence_market_identity(
            subject=subject,
            instrument=instrument,
            evidence_refs=evidence_refs,
            evidence=catalog,
        )
        if company is None and symbol is None:
            # No identity from the rolling catalog: retry with the exact-ID
            # recovered citations merged in (catalog entities still win on
            # duplicate refs).
            company, symbol = resolve_evidence_market_identity(
                subject=subject,
                instrument=instrument,
                evidence_refs=evidence_refs,
                evidence=merged,
            )
        if symbol is not None:
            symbol = _canonical_market_symbol(symbol) or symbol
        normalize_symbol = False
        if item["symbol"] is not None and symbol is not None:
            stored_canonical = _canonical_market_symbol(item["symbol"])
            resolved_canonical = _canonical_market_symbol(symbol)
            if (
                stored_canonical is not None
                and resolved_canonical is not None
                and stored_canonical == resolved_canonical
            ):
                normalize_symbol = True
        changed += int(
            _enrich_thesis_market_identity(
                session,
                thesis_id,
                company=company,
                symbol=symbol,
                normalize_symbol=normalize_symbol,
            )
        )
    return changed


def _ensure_candidate_catalyst(
    session: Any,
    thesis_id: str,
    description: Any,
) -> bool:
    """Persist one generated pending catalyst without duplicating reruns.

    The absence check is closed by a transaction-scoped advisory lock
    keyed by the exact identity ``(thesis_id, normalized description)``:
    two concurrent cycles can otherwise both see "no catalyst" and race
    the same absent row through the ``NOT EXISTS`` guard, permanently
    inserting a duplicate.  The loser waits for the winner's transaction,
    then its guard sees the winner's row and truthfully reports a no-op.
    Rows stay immutable and append-only (migration 054); reruns never
    mutate or delete existing rows.

    Lock order is global: the thesis's fusion canonical-key lock is
    acquired BEFORE the catalyst identity lock.  The promote path already
    holds the fusion lock (merge is reentrant), and the backfill path
    takes it here, so a catalyst lock is never retained across a later
    fusion acquisition for the same thesis — otherwise a candidate cycle
    (fusion K then catalyst C) and a backfilling cycle (catalyst C then
    fusion K) could deadlock each other and abort a whole cycle.
    """
    catalyst = " ".join(str(description or "").split())[:2000]
    if not catalyst:
        return False
    # The stored canonical key is the exact identity merge_thesis locks,
    # so this serializes on the same lock a concurrent candidate cycle
    # holds while promoting the thesis.  Legacy theses without a key have
    # no merge identity to contend on and skip the lock.
    thesis = _first(
        session.execute(
            text(
                """/* catalyst_identity_guard */
                   SELECT canonical_key FROM investment_theses
                   WHERE id = CAST(:thesis_id AS UUID)"""
            ),
            {"thesis_id": thesis_id},
        )
    )
    if thesis is not None and thesis.get("canonical_key"):
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": str(thesis["canonical_key"])},
        )
    # Serialize the absence check + insert for this exact identity across
    # every connection.  The namespace keeps this lock disjoint from the
    # fusion canonical-key lock and all other advisory-lock users; the
    # per-identity key keeps distinct descriptions independent.
    session.execute(
        text(
            "/* catalyst_identity_lock */ "
            "SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"
        ),
        {"lock_key": f"catalyst_identity:{thesis_id}:{catalyst}"},
    )
    row = _first(
        session.execute(
            text(
                """INSERT INTO investment_catalysts
                   (thesis_id, description, expected_at, state)
                   SELECT CAST(:thesis_id AS UUID), :description, NULL, 'pending'
                   WHERE NOT EXISTS (
                       SELECT 1 FROM investment_catalysts
                       WHERE thesis_id = CAST(:thesis_id AS UUID)
                         AND description = :description
                   )
                   RETURNING id"""
            ),
            {"thesis_id": thesis_id, "description": catalyst},
        )
    )
    return row is not None


def _backfill_generated_catalysts(
    session: Any,
    reference: datetime,
    *,
    limit: int = 100,
) -> tuple[tuple[str, str], ...]:
    """Materialize bounded legacy catalyst summaries through the live table.

    A historical/delayed run never materializes a catalyst for thesis state
    that is not provably visible at its accepted ``reference``: the thesis
    must be created and last updated at/before the reference and must not
    carry an accepted fusion reference later than it (``fusion_reference_at``
    NULL or <= reference).  Missing timestamps fail closed (a NULL
    ``created_at``/``updated_at`` can never be proven visible at the
    reference).  Batch bounds/ordering and insert-once idempotency are
    unchanged.  Returns ``(thesis_id, catalyst_summary)`` pairs so the cycle
    can hand the just-derived catalysts back to scoring as explicit
    current-cycle inputs (their persisted rows postdate the cycle
    reference).
    """
    rows = _rows(
        session.execute(
            text(
                """/* autonomy_catalyst_backfill */
                   SELECT t.id, t.catalyst_summary
                   FROM investment_theses t
                   WHERE t.origin = 'fusion'
                     AND NULLIF(BTRIM(t.catalyst_summary), '') IS NOT NULL
                     AND t.created_at <= :reference
                     AND t.updated_at <= :reference
                     AND (t.fusion_reference_at IS NULL
                          OR t.fusion_reference_at <= :reference)
                     AND NOT EXISTS (
                         SELECT 1 FROM investment_catalysts c
                         WHERE c.thesis_id = t.id
                           AND c.description = t.catalyst_summary
                     )
                   ORDER BY t.created_at, t.id
                   LIMIT :limit"""
            ),
            {
                "reference": reference,
                "limit": max(1, min(int(limit), 500)),
            },
        )
    )
    inserted: list[tuple[str, str]] = []
    for row in rows:
        thesis_id = str(row["id"])
        summary = " ".join(str(row.get("catalyst_summary") or "").split())[:2000]
        if _ensure_candidate_catalyst(session, thesis_id, summary):
            inserted.append((thesis_id, summary))
    return tuple(inserted)


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
    row = _first(
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


def _scenario_forecast_present(session: Any, scenario_id: str) -> bool:
    """Bounded precheck: one unsuperseded forecast already exists for the
    scenario (LIMIT 1), so a later rerun must not freeze another one.

    Mirrors the partial unique index (``scenario_id`` where active) added
    by migration 053; the index stays the concurrency-safe backstop while
    this check keeps ordinary reruns from raising.
    """
    row = _first(
        session.execute(
            text(
                """SELECT 1 AS present FROM investment_thesis_forecasts
                   WHERE scenario_id = CAST(:scenario_id AS UUID)
                     AND superseded_at IS NULL
                   LIMIT 1"""
            ),
            {"scenario_id": scenario_id},
        )
    )
    return row is not None


def _freeze_scenario_forecasts(
    session: Any,
    thesis_id: str,
    scenarios: Sequence[tuple[str, Any, str | None]],
    *,
    direction: Any,
    horizon: Any,
    input_fingerprint: Any,
    market_symbol: Any,
    reference: datetime,
) -> int:
    """Freeze deterministic price targets for supplied persisted scenarios.

    At most one unsuperseded forecast per non-null scenario: scenarios that
    already carry an active forecast are skipped, so the first frozen
    as_of/close/target/target date wins and a rerun can never create a
    second active forecast (or a duplicate outcome).  Scenario-less
    forecasts stay valid and fall back to forecast_key idempotency.
    """
    close = _close_at_or_before(session, market_symbol, reference)
    if close is None or close <= 0:
        return 0
    thesis_direction = str(direction or "").strip().casefold()
    if thesis_direction not in ("long", "short"):
        return 0
    fingerprint = str(input_fingerprint or "legacy")[:16]
    target_date = reference.date() + timedelta(days=_horizon_days(horizon))
    frozen = 0
    for label, raw_return, scenario_id in scenarios:
        if scenario_id is not None and _scenario_forecast_present(session, scenario_id):
            continue
        try:
            expected_return = float(raw_return) if raw_return is not None else None
        except (TypeError, ValueError, OverflowError):
            expected_return = None
        if expected_return is None or not math.isfinite(expected_return):
            continue
        factor = (
            1.0 + expected_return
            if thesis_direction == "long"
            else 1.0 - expected_return
        )
        target_value = round(close * factor, 12)
        if not math.isfinite(target_value) or target_value <= 0:
            continue
        if target_value > close:
            forecast_direction = "up"
        elif target_value < close:
            forecast_direction = "down"
        else:
            forecast_direction = "flat"
        forecast_key = (
            f"autonomy:{thesis_id}:{label}:{target_date.isoformat()}:{fingerprint}"
        )
        result = freeze_forecast(
            session,
            thesis_id,
            forecast_key=forecast_key,
            forecast_type="price",
            direction=forecast_direction,
            target_value=target_value,
            target_date=target_date,
            as_of=reference,
            scenario_id=scenario_id,
        )
        if result.get("changed"):
            frozen += 1
    return frozen


def _freeze_candidate_forecasts(
    session: Any,
    thesis_id: str,
    candidate: Any,
    scenario_ids: Mapping[str, str],
    *,
    market_symbol: Any,
    reference: datetime,
) -> int:
    """Freeze one evidence-resolved price forecast per candidate scenario."""
    return _freeze_scenario_forecasts(
        session,
        thesis_id,
        [
            (
                leg.label,
                leg.expected_return,
                scenario_ids.get(leg.label),
            )
            for leg in candidate.scenarios
        ],
        direction=candidate.direction,
        horizon=candidate.horizon,
        input_fingerprint=candidate.content_fingerprint,
        market_symbol=market_symbol,
        reference=reference,
    )


def _backfill_missing_forecasts(
    session: Any,
    reference: datetime,
    *,
    limit: int = _MAX_FORECAST_BACKFILL,
) -> int:
    """Freeze bounded forecast targets for scenario legs visible at ``reference``.

    A historical/delayed run never backdates a forecast for thesis or
    scenario state that did not exist at its accepted reference: the thesis
    must be created and last updated at/before the reference, must not carry
    an accepted fusion reference later than it (``fusion_reference_at`` NULL
    or <= reference), and each scenario must be created at/before the
    reference and not yet superseded on/before it (``superseded_at`` NULL or
    > reference).  A scenario is frozen only when it carries no forecast at
    the accepted reference (no row frozen at/before it — ``as_of`` at/before
    the reference — that is not superseded on/before it) and no
    currently-active forecast either: a forecast that was active at the
    reference and was later superseded or moved to another scenario never
    makes the scenario eligible again, so a replay cannot create a forecast
    the original run at the reference would not have made.  Batch
    bounds/ordering, canonical market identity, the price availability
    cutoff, first-frozen uniqueness, target-boundary semantics, and
    price-only ownership are unchanged.
    """
    rows = _rows(
        session.execute(
            text(
                """/* autonomy_forecast_backfill */
                   SELECT t.id AS thesis_id, t.symbol, t.direction, t.horizon,
                          t.input_fingerprint, s.id AS scenario_id,
                          s.name, s.expected_return
                   FROM investment_theses t
                   JOIN investment_thesis_scenarios s ON s.thesis_id = t.id
                   WHERE t.origin = 'fusion'
                     AND t.status IN ('active', 'candidate')
                     AND NULLIF(BTRIM(t.symbol), '') IS NOT NULL
                     AND t.created_at <= :reference
                     AND t.updated_at <= :reference
                     AND (t.fusion_reference_at IS NULL
                          OR t.fusion_reference_at <= :reference)
                     AND s.created_at <= :reference
                     AND (s.superseded_at IS NULL
                          OR s.superseded_at > :reference)
                     AND NOT EXISTS (
                         SELECT 1 FROM investment_thesis_forecasts f
                         WHERE f.scenario_id = s.id
                           AND f.superseded_at IS NULL
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM investment_thesis_forecasts f
                         WHERE f.scenario_id = s.id
                           AND f.as_of <= :reference
                           AND (f.superseded_at IS NULL
                                OR f.superseded_at > :reference)
                     )
                   ORDER BY t.updated_at, t.id, s.name
                   LIMIT :limit"""
            ),
            {
                "reference": reference,
                "limit": max(1, min(int(limit), _MAX_FORECAST_BACKFILL)),
            },
        )
    )
    pending: dict[str, dict[str, Any]] = {}
    for row in rows:
        thesis_id = str(row["thesis_id"])
        item = pending.setdefault(
            thesis_id,
            {
                "symbol": row.get("symbol"),
                "direction": row.get("direction"),
                "horizon": row.get("horizon"),
                "input_fingerprint": row.get("input_fingerprint"),
                "scenarios": [],
            },
        )
        item["scenarios"].append(
            (
                str(row.get("name") or "scenario"),
                row.get("expected_return"),
                str(row["scenario_id"]),
            )
        )
    return sum(
        _freeze_scenario_forecasts(
            session,
            thesis_id,
            item["scenarios"],
            direction=item["direction"],
            horizon=item["horizon"],
            input_fingerprint=item["input_fingerprint"],
            market_symbol=item["symbol"],
            reference=reference,
        )
        for thesis_id, item in pending.items()
    )


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
    rows = _rows(
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
    row = _first(
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


def _competitor_group_name(company: Any, symbol: Any, horizon: Any) -> str:
    subject = symbol or company
    name = "fusion:" + _normalized(subject)[:80] + ":" + _normalized(horizon)[:40]
    return name[:_MAX_GROUP_NAME]


def _group_candidate(
    session: Any,
    thesis_id: str,
    candidate: Any,
    group_ids: dict[str, str],
    *,
    company: str | None,
    symbol: str | None,
) -> str | None:
    name = _competitor_group_name(company, symbol, candidate.horizon)
    group_id = group_ids.get(name)
    if group_id is None:
        group = create_find_group(
            session,
            name=name,
            description="autonomous fusion competitors for one subject and horizon",
        )
        group_id = str(group["id"])
        group_ids[name] = group_id
    add_group_membership(
        session, group_id, thesis_id, note="autonomous fusion candidate"
    )
    return group_id


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


def _attach_contradictions(
    session: Any,
    thesis_id: str,
    decision: Any,
    signals: Sequence[EvidenceSignal],
) -> int:
    rows: list[dict[str, Any]] = []
    for signal in _contradiction_signals(decision, signals):
        rows.append(
            {
                "evidence_type": signal.evidence_type,
                "evidence_id": signal.evidence_id,
                "relationship": "contradicts",
                "source_name": signal.source_name,
                "source_family": signal.source_family or signal.source_name,
                "origin_key": signal.origin_key,
                "independence_key": signal.independence_key,
                "evidence_fingerprint": signal.evidence_fingerprint,
                "source_timestamp": signal.source_timestamp,
                "available_at": signal.available_at,
                "excerpt": signal.provenance.get("excerpt"),
                "quality_score": signal.quality_score,
                "entailment_score": signal.entailment_score,
                "freshness_score": signal.freshness_score,
                "effective_weight": signal.effective_weight,
            }
        )
    if not rows:
        return 0
    result = attach_evidence(session, thesis_id, rows, limit=_MAX_ATTACH_CONTRADICTIONS)
    return int(result.get("attached") or 0)


def _persist_falsification(
    session: Any,
    thesis_id: str,
    decision: Any,
    *,
    run_key: str,
    reference: datetime,
) -> tuple[str, bool]:
    """Persist one falsification decision; re-runs are idempotent no-ops."""
    run_id = record_falsification_run(
        session,
        thesis_id,
        run_key=run_key,
        status="in_progress",
        started_at=reference,
    )
    current = _first(
        session.execute(
            text(
                "SELECT status FROM investment_thesis_falsification_runs "
                "WHERE id = CAST(:id AS UUID) LIMIT 1"
            ),
            {"id": run_id},
        )
    )
    if current is None or str(current.get("status")) not in ("pending", "in_progress"):
        return run_id, False
    status = _FALSIFICATION_STATUS_BY_STATE.get(decision.state, "inconclusive")
    update_falsification_run(
        session,
        run_id,
        status=status,
        findings=[decision.to_dict()],
        completed_at=reference,
    )
    return run_id, True


def _pause_thesis(session: Any, thesis_id: str) -> bool:
    """Pause a breached thesis; never close or delete it."""
    result = session.execute(
        text(
            "UPDATE investment_theses SET status = 'paused' "
            "WHERE id = CAST(:id AS UUID) AND status IN ('active', 'candidate')"
        ),
        {"id": thesis_id},
    )
    return bool(getattr(result, "rowcount", 0))


def _pause_thesis_if_unchanged(
    session: Any,
    thesis_id: str,
    *,
    status: str,
    updated_at: datetime | None,
    last_evaluated_at: datetime | None,
    fusion_reference_at: datetime | None,
    reference: datetime,
) -> bool:
    """Pause a breached thesis only while its row still matches the tokens
    selected at ``reference``; never unconditional after model latency.

    The challenge verdict was computed against state visible at
    ``reference``, while the pause is a current-state write.  A concurrent
    or newer cycle may re-evaluate, re-fuse, or re-state the thesis between
    selection and pause; pausing then would apply an older verdict to
    newer state, so the UPDATE is conditional on the optimistic tokens:

    * ``status`` and ``updated_at`` must still equal the selected values
      exactly (any row mutation bumps ``updated_at``);
    * ``last_evaluated_at`` must still equal the selected token or sit
      exactly at the reference -- the same-cycle contradiction recompute
      may set it to the reference, which remains safe, while a newer
      cycle's evaluation postdates it;
    * ``fusion_reference_at`` must still equal the selected token or sit
      exactly at the reference, so a thesis claimed by a newer cycle after
      the reference is never paused from this older verdict.

    On success the row is paused and ``updated_at`` is stamped NOW()
    (bookkeeping only; scoring inputs are untouched).  The caller detects
    the outcome via rowcount/RETURNING and records a bounded
    ``second_pass_stale_skipped`` diagnostic instead of pausing when a
    concurrent change made the row stale.
    """
    result = session.execute(
        text(
            """UPDATE investment_theses
               SET status = 'paused', updated_at = NOW()
               WHERE id = CAST(:id AS UUID)
                 AND status = :status
                 AND updated_at = :updated_at
                 AND (last_evaluated_at IS NOT DISTINCT FROM :last_evaluated_at
                      OR last_evaluated_at = :reference)
                 AND (fusion_reference_at IS NOT DISTINCT FROM :fusion_reference_at
                      OR fusion_reference_at = :reference)"""
        ),
        {
            "id": thesis_id,
            "status": status,
            "updated_at": updated_at,
            "last_evaluated_at": last_evaluated_at,
            "fusion_reference_at": fusion_reference_at,
            "reference": reference,
        },
    )
    return bool(getattr(result, "rowcount", 0))


def _link_watch_positions(session: Any, thesis_id: str, symbol: Any) -> int:
    """Boundedly link a candidate symbol to matching active holdings.

    Exact normalized-symbol match only (whitespace-collapsed, casefolded);
    nothing about sizing, direction, or the holding itself is ever inferred
    or altered.  ``link_position`` with ``link_type='watch'`` is idempotent,
    so re-running identical inputs never duplicates links.
    """
    normalized = _normalized(symbol)
    if not normalized:
        return 0
    row = _first(
        session.execute(
            text(
                """SELECT id FROM portfolio_holdings
                   WHERE LOWER(TRIM(symbol)) = :symbol
                   LIMIT 1"""
            ),
            {"symbol": normalized},
        )
    )
    if row is None:
        return 0
    position_id = str(row["id"])
    return 1 if link_position(session, thesis_id, position_id, link_type="watch") else 0


def _candidate_expected_at(
    candidate: Any,
    catalog: Mapping[str, Any],
    *,
    reference: datetime,
) -> datetime | None:
    """Earliest cited announced company event date, without inference."""
    announced: list[datetime] = []
    for ref in candidate.evidence_refs:
        item = catalog.get(ref)
        if item is None or not isinstance(item.provenance, Mapping):
            continue
        if (
            str(item.provenance.get("source") or "").casefold()
            != "company_expectations"
        ):
            continue
        metadata = item.provenance.get("metadata")
        event = metadata.get("next_earnings") if isinstance(metadata, Mapping) else None
        raw_date = event.get("reportDate") if isinstance(event, Mapping) else None
        try:
            event_date = date.fromisoformat(str(raw_date))
        except (TypeError, ValueError):
            continue
        expected = datetime.combine(event_date, dt_time.min, tzinfo=UTC)
        if expected >= reference.replace(hour=0, minute=0, second=0, microsecond=0):
            announced.append(expected)
    return min(announced) if announced else None


def _upsert_candidate_playbook(
    session: Any,
    candidate: Any,
    catalog: Mapping[str, Any],
    *,
    thesis_id: str,
    thesis_version: Any,
    reference: datetime,
) -> int:
    """Derive and persist one deterministic event-playbook draft.

    The draft is built only from the validated candidate and its cited
    evidence; persistence is idempotent (identical content is a no-op,
    changed content supersedes to a new immutable version).
    """
    draft = build_event_playbook(
        candidate,
        list(catalog.values()),
        thesis_id=thesis_id,
        thesis_version=thesis_version,
        as_of=reference,
        expected_at=_candidate_expected_at(candidate, catalog, reference=reference),
    )
    result = upsert_event_playbook(session, draft)
    return 1 if result.get("changed") else 0


def _evaluate_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "cost": float(settings.get("cost") or 0.0),
        "attention": None,
        "crowding": None,
        "liquidity": settings.get("liquidity"),
        "downside": settings.get("downside"),
    }


def _second_pass_candidates(
    session: Any,
    *,
    limit: int,
    excluded_ids: Sequence[str],
    reference: datetime,
    context_since: datetime,
) -> list[dict[str, Any]]:
    """Select second-pass challenge candidates visible at ``reference``.

    The caller must pass the authoritative replay/reference cutoff
    explicitly: this helper consumes current thesis rows and their
    attachments, so a historical path can never invoke it without making
    the reference-safety decision.  Selection fails closed on unversioned
    state — a thesis whose created/updated timestamps are missing or
    postdate ``reference`` is excluded, as are position links created
    after the reference (or removed by it) and context matches whose
    match/playbook rows were not reference-visible by the cutoff.
    Current scoring and fusion state is reference-bounded the same way:
    a thesis whose ``last_evaluated_at`` (current score recency) or
    ``fusion_reference_at`` (last accepted autonomous fusion reference)
    postdates the reference is excluded, so newer opportunity scores can
    never steer an older run's challenges.  Each selected row carries its
    ``updated_at``, ``last_evaluated_at``, ``fusion_reference_at``, and
    ``status`` as optimistic tokens for the conditional pause.
    """
    if int(limit) <= 0:
        return []
    rows = _rows(
        session.execute(
            text(
                """SELECT t.id, t.claim, t.direction, t.status,
                          t.invalidation_conditions, t.opportunity_score,
                          t.last_evaluated_at, t.updated_at,
                          t.fusion_reference_at,
                          (EXISTS (SELECT 1 FROM position_thesis_links l
                                   WHERE l.thesis_id = t.id
                                     AND l.created_at <= :reference
                                     AND (l.removed_at IS NULL
                                          OR l.removed_at > :reference)))
                              AS has_link,
                          (EXISTS (SELECT 1 FROM investment_thesis_event_matches m
                                   JOIN investment_thesis_event_playbooks p
                                        ON p.id = m.playbook_id
                                   WHERE p.thesis_id = t.id
                                     AND m.match_kind = 'context'
                                     AND m.observed_at >= :context_since
                                     AND m.observed_at <= :reference
                                     AND m.created_at <= :reference
                                     AND p.created_at <= :reference
                                     AND (p.superseded_at IS NULL
                                          OR p.superseded_at > :reference)))
                              AS has_context
                   FROM investment_theses t
                   WHERE t.status IN ('active', 'candidate')
                     AND t.created_at <= :reference
                     AND t.updated_at <= :reference
                     AND (t.last_evaluated_at IS NULL
                          OR t.last_evaluated_at <= :reference)
                     AND (t.fusion_reference_at IS NULL
                          OR t.fusion_reference_at <= :reference)
                   ORDER BY
                       has_link DESC,
                       has_context DESC,
                       t.opportunity_score DESC,
                       t.last_evaluated_at DESC NULLS LAST,
                       t.id
                   LIMIT :limit"""
            ),
            {
                "limit": max(1, int(limit) * 2),
                "reference": reference,
                "context_since": context_since,
            },
        )
    )
    excluded = {str(value) for value in excluded_ids}
    selected: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("id")) in excluded:
            continue
        selected.append(row)
        if len(selected) >= int(limit):
            break
    return selected


def _count_unversioned_second_pass_candidates(
    session: Any,
    *,
    reference: datetime,
) -> int:
    """Count active/candidate theses the second pass must fail closed on.

    These are theses whose persisted state cannot be proven visible at
    ``reference``: missing or post-reference created/updated timestamps,
    or current scoring/fusion state (``last_evaluated_at``,
    ``fusion_reference_at``) that postdates the reference.  Their status,
    context matches, scenarios, and attachments may reflect future
    mutations, so the second pass excludes them.  The count keeps that
    conservative exclusion observable in cycle diagnostics as one
    bounded scalar (never row content).
    """
    row = _first(
        session.execute(
            text(
                """SELECT COUNT(*) AS count
                   FROM investment_theses
                   WHERE status IN ('active', 'candidate')
                     AND (created_at IS NULL
                          OR updated_at IS NULL
                          OR created_at > :reference
                          OR updated_at > :reference
                          OR last_evaluated_at > :reference
                          OR fusion_reference_at > :reference)"""
            ),
            {"reference": reference},
        )
    )
    return int((row or {}).get("count") or 0)


def _load_second_pass_snapshot(
    session: Any,
    row: Mapping[str, Any],
    *,
    reference: datetime,
    cost: float,
    cycle_key: str,
) -> tuple[ThesisSnapshot, tuple[EvidenceSignal, ...]]:
    """Rebuild a challenge snapshot from state visible at ``reference``.

    The caller must pass the authoritative reference cutoff explicitly:
    this helper consumes current scenario/evidence attachments, so a
    historical path can never invoke it without making the reference-
    safety decision.  Attachments whose rows (or source/availability
    timestamps) postdate the reference are excluded, so a replay can
    never challenge against future state.
    """
    thesis_id = str(row["id"])
    scenario_rows = _rows(
        session.execute(
            text(
                """SELECT name, probability, expected_return
                   FROM investment_thesis_scenarios
                   WHERE thesis_id = CAST(:id AS UUID)
                     AND created_at <= :reference
                     AND (superseded_at IS NULL OR superseded_at > :reference)
                   ORDER BY is_base_case DESC, created_at, name
                   LIMIT :limit"""
            ),
            {
                "id": thesis_id,
                "limit": _MAX_SECOND_PASS_SCENARIOS,
                "reference": reference,
            },
        )
    )
    scenarios = tuple(
        Scenario.create(
            label=scenario.get("name") or "scenario",
            probability=scenario.get("probability"),
            expected_return=scenario.get("expected_return") or 0.0,
        )
        for scenario in scenario_rows
    )
    evidence_rows = _rows(
        session.execute(
            text(
                """SELECT evidence_type, evidence_id, relationship, source_family,
                          origin_key, independence_key, evidence_fingerprint,
                          source_timestamp, available_at, quality_score,
                          entailment_score, freshness_score, effective_weight,
                          excerpt, created_at
                   FROM investment_thesis_evidence
                   WHERE thesis_id = CAST(:id AS UUID)
                     AND created_at <= :reference
                     AND COALESCE(source_timestamp, created_at) <= :reference
                     AND COALESCE(available_at, source_timestamp, created_at)
                         <= :reference
                   ORDER BY created_at, evidence_type, evidence_id
                   LIMIT :limit"""
            ),
            {
                "id": thesis_id,
                "limit": _MAX_SECOND_PASS_EVIDENCE,
                "reference": reference,
            },
        )
    )
    attached_signals = tuple(_signal_from_row(evidence) for evidence in evidence_rows)
    conditions: list[ThesisCondition] = []
    raw_conditions = row.get("invalidation_conditions")
    if isinstance(raw_conditions, list):
        for index, item in enumerate(raw_conditions[: _MAX_CONDITIONS * 2]):
            if not isinstance(item, Mapping):
                continue
            if not {"kind", "operator", "threshold"} <= set(item):
                continue
            try:
                conditions.append(
                    ThesisCondition.create(
                        condition_id=item.get("condition_id") or f"condition-{index}",
                        kind=item.get("kind"),
                        operator=item.get("operator"),
                        threshold=item.get("threshold"),
                        observed=item.get("observed"),
                        unit=item.get("unit"),
                    )
                )
            except ValueError:
                continue
            if len(conditions) >= _MAX_CONDITIONS:
                break
    return ThesisSnapshot.create(
        thesis_id=thesis_id,
        statement=row.get("claim") or "unknown thesis statement",
        direction=row.get("direction") or "neutral",
        as_of=reference,
        cost=cost,
        conditions=tuple(conditions),
        scenarios=scenarios,
        claims=[
            ThesisClaim.create(
                claim_id=f"{thesis_id}:autonomy:{cycle_key}",
                statement=row.get("claim") or "unknown thesis statement",
                citations=[signal.evidence_id for signal in attached_signals],
            )
        ],
    ), attached_signals


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
    """Run one bounded autonomous thesis-fusion cycle in the caller's transaction.

    The session is never committed or rolled back here; the caller owns the
    transaction.  ``runner``/``challenger``/``auditor`` default to the
    production ``LLMRoleRunner``/``LLMChallenger``/``LLMSemanticCitationAuditor``
    adapters (all sharing one per-run model budget) and can be replaced with
    credential-free fakes for tests.  Returns bounded counts, errors, and
    model cost.

    ``as_of`` is the evidence/market availability cutoff: evidence
    collection runs under a replay ``ResearchContext`` at the reference and
    every scoring query in ``evaluate_thesis`` is bounded by it, so no
    source or availability timestamp after the reference can affect
    discovery, scoring, or liquidity.  Artifacts the current cycle derives
    (candidate scenario legs, generated/backfilled catalysts, cited
    evidence links, challenger contradictions) may be persisted after that
    cutoff but only encode source inputs available by it; they enter
    scoring as explicit current-cycle inputs.

    Replay-time maintenance backfills are reference-bound the same way:
    legacy identity recovery and generated-catalyst materialization select
    only theses whose existence/current/fusion state is provable at the
    reference (created/updated at/before it and ``fusion_reference_at``
    NULL or at/before it, with missing timestamps failing closed), so a
    historical or delayed run performs no maintenance write and supplies no
    explicit score input for any thesis a newer cycle created, updated, or
    claimed after the reference.

    The second falsification pass is reconstructed at the same reference
    and fails closed on unversioned state: candidate theses must have
    created/updated timestamps at/before the cutoff, current scoring and
    fusion state (``last_evaluated_at``, ``fusion_reference_at``) must be
    at/before the cutoff, position links must be created by it (and not
    removed by it), context matches and their playbooks must be
    reference-visible, and snapshot scenarios/evidence attachments must
    exist (with source/availability) by the cutoff.  Post-reference
    mutations therefore can never be challenged, paused, or rewritten
    from, and ``second_pass_unversioned_excluded`` reports how many
    active/candidate theses the pass had to exclude for that reason (one
    bounded scalar, never row content).

    Pausing a breached second-pass thesis is an optimistic conditional
    write: after model latency the UPDATE applies only while the row's
    ``status``/``updated_at`` still match the tokens selected at the
    reference and its ``last_evaluated_at``/``fusion_reference_at`` still
    match the selected tokens or sit exactly at the reference, and stamps
    ``updated_at = NOW()`` on success.  A thesis changed by a
    concurrent/newer cycle is never paused from the older verdict;
    ``second_pass_stale_skipped`` reports those skipped pauses as one
    bounded diagnostic while the reference-bounded challenge and
    falsification audit rows are still persisted.
    """
    settings = _settings(config)
    if not settings["enabled"]:
        return {"status": "disabled", "error_count": 0, "cost_usd": 0.0}
    reference = _as_utc(as_of)
    cycle_key = _cycle_key(reference)
    evaluate_inputs = _evaluate_settings(settings)
    error_count = 0
    errors: list[str] = []

    # Resolve matured active forecasts once from point-in-time market_data
    # (bounded; outcomes are recorded exactly once and never overwritten).
    outcome_counts = _resolve_matured_forecasts(session, reference)
    forecast_backfills = _backfill_missing_forecasts(session, reference)

    evidence_items, evidence_failures = _collect_evidence(
        session, settings, reference=reference
    )
    catalog = {item.ref: item for item in evidence_items}
    identity_backfills = _backfill_missing_market_identities(
        session, catalog, reference=reference
    )
    legacy_catalyst_ids = _backfill_generated_catalysts(session, reference)
    backfilled_catalysts = dict(legacy_catalyst_ids)
    for thesis_id, catalyst_summary in legacy_catalyst_ids:
        evaluate_thesis(
            session,
            thesis_id,
            as_of=reference,
            cost=evaluate_inputs["cost"],
            attention=evaluate_inputs["attention"],
            crowding=evaluate_inputs["crowding"],
            liquidity=evaluate_inputs["liquidity"],
            downside=evaluate_inputs["downside"],
            # The backfilled catalyst row postdates the cycle reference; the
            # summary is a current-cycle derived artifact (it encodes only
            # thesis content that existed at the reference), so it enters
            # scoring explicitly instead of being dropped by the cutoff.
            current_catalysts=_catalyst_signals(catalyst_summary),
        )
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
    thesis_count = 0
    scenario_upserts = 0
    risk_upserts = 0
    forecasts_frozen = 0
    watch_links = 0
    playbook_upserts = 0
    catalyst_upserts = 0
    group_ids: dict[str, str] = {}
    groups_used: set[str] = set()
    opportunity_snapshots = 0
    falsification_runs = 0
    contradictions_attached = 0
    paused_count = 0
    challenger_failures = 0
    stale_candidates = 0
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
            merged = merge_or_create_thesis(
                session,
                theme_id=theme_id,
                company=company,
                symbol=market_symbol,
                subject=candidate.subject,
                claim=candidate.claim,
                variant_perception=candidate.variant_perception,
                horizon=candidate.horizon,
                mechanism=identity_mechanism,
                direction=candidate.direction,
                catalyst_summary=candidate.catalyst,
                confidence=candidate.confidence,
                trend_context=candidate.trend_context,
                valuation_context=candidate.valuation_context,
                sentiment_context=candidate.sentiment_context,
                citation_map={field: list(refs) for field, refs in candidate.citations},
                invalidation_conditions=list(candidate.invalidators),
                rationale="autonomous fusion tournament candidate",
                origin="fusion",
                input_fingerprint=canonical_fingerprint(
                    {
                        "candidate": candidate.content_fingerprint,
                        "identity_mechanism": identity_mechanism,
                    }
                ),
                accepted_reference=reference,
            )
            if merged.get("stale"):
                # A newer accepted reference already claimed this thesis;
                # this candidate is a complete no-op.  Skipping everything
                # below keeps the stale cycle from overwriting or appending
                # current child state (evidence links, catalyst, scenarios,
                # playbook, position link, forecast, evaluation, challenge)
                # after the newer job.
                stale_candidates += 1
                continue
            thesis_id = str(merged["id"])
            decision = replace(predecision, thesis_id=thesis_id)
            if not merged.get("created"):
                _enrich_thesis_market_identity(
                    session,
                    thesis_id,
                    company=company,
                    symbol=market_symbol,
                )
            catalyst_upserts += int(
                _ensure_candidate_catalyst(session, thesis_id, candidate.catalyst)
            )
            promoted_ids.append(thesis_id)
            thesis_count += 1
            watch_links += _link_watch_positions(session, thesis_id, market_symbol)
            playbook_upserts += _upsert_candidate_playbook(
                session,
                candidate,
                catalog,
                thesis_id=thesis_id,
                thesis_version=merged.get("version"),
                reference=reference,
            )

            _attach_cited_evidence(
                session,
                thesis_id,
                candidate,
                catalog,
                entailment_score=entailment_score,
            )
            scenario_ids, scenario_changed = _persist_scenarios(
                session, thesis_id, candidate
            )
            scenario_upserts += scenario_changed
            risk_upserts += _persist_candidate_risks(session, thesis_id, candidate)
            forecasts_frozen += _freeze_candidate_forecasts(
                session,
                thesis_id,
                candidate,
                scenario_ids,
                market_symbol=market_symbol,
                reference=reference,
            )
            group_id = _group_candidate(
                session,
                thesis_id,
                candidate,
                group_ids,
                company=company,
                symbol=market_symbol,
            )
            if group_id is not None:
                groups_used.add(group_id)

            evaluated = evaluate_thesis(
                session,
                thesis_id,
                as_of=reference,
                expected_returns=None,
                cost=evaluate_inputs["cost"],
                attention=evaluate_inputs["attention"],
                crowding=evaluate_inputs["crowding"],
                liquidity=evaluate_inputs["liquidity"],
                downside=evaluate_inputs["downside"],
                snapshot_key=f"autonomy:{cycle_key}",
                # The just-persisted scenario legs, catalyst, and evidence
                # links postdate the cycle reference; they are current-cycle
                # derived artifacts (encoding only cutoff-bounded source
                # inputs) and enter scoring explicitly so the fresh
                # candidate still scores its own legs, catalyst, and cited
                # evidence.
                current_scenarios=_candidate_scenarios(candidate),
                current_catalysts=_candidate_catalysts(candidate),
                current_evidence=_candidate_evidence(
                    candidate,
                    catalog,
                    entailment_score=entailment_score,
                ),
            )
            if evaluated.get("opportunity", {}).get("opportunity") is not None:
                opportunity_snapshots += 1

            _, changed = _persist_falsification(
                session,
                thesis_id,
                decision,
                run_key=f"autonomy:{cycle_key}",
                reference=reference,
            )
            if changed:
                falsification_runs += 1

            attached = _attach_contradictions(
                session, thesis_id, decision, challenge_signals
            )
            if attached:
                contradictions_attached += attached
                # Recompute the frozen scores with the contradiction mass;
                # the existing snapshot row (same key) stays untouched.
                # The cited evidence AND the contradiction links all
                # postdate the cycle reference, so both enter scoring as
                # explicit current-cycle signals (persisted rows win on an
                # identical fingerprint, so duplicates collapse exactly as
                # the persisted path would).
                evaluate_thesis(
                    session,
                    thesis_id,
                    as_of=reference,
                    cost=evaluate_inputs["cost"],
                    attention=evaluate_inputs["attention"],
                    crowding=evaluate_inputs["crowding"],
                    liquidity=evaluate_inputs["liquidity"],
                    downside=evaluate_inputs["downside"],
                    current_scenarios=_candidate_scenarios(candidate),
                    current_catalysts=_candidate_catalysts(candidate),
                    current_evidence=(
                        _candidate_evidence(
                            candidate,
                            catalog,
                            entailment_score=entailment_score,
                        )
                        + _contradiction_signals(decision, challenge_signals)
                    ),
                )

            if decision.state == "breached" and _pause_thesis(session, thesis_id):
                paused_count += 1
        except Exception as exc:
            error_count += 1
            errors.append(type(exc).__name__[:60])
            if len(errors) >= _MAX_ERRORS:
                break

    # Second bounded falsification pass over high-opportunity existing
    # theses, even when nothing was regenerated this cycle.  Frozen
    # snapshots are rebuilt from persisted rows visible at the reference
    # plus relevant new+attached evidence; prior snapshot rows are never
    # mutated.
    second_pass_limit = max(0, challenge_limit - challenge_attempts)
    # Second-pass reconstruction is reference-bounded and fails closed: a
    # thesis whose current state is not provable at the reference (missing
    # or post-reference created/updated timestamps) is never selected,
    # challenged, or paused, and the conservative exclusion stays
    # observable as a bounded diagnostic counter.
    second_pass_candidates = _second_pass_candidates(
        session,
        limit=second_pass_limit,
        excluded_ids=promoted_ids,
        reference=reference,
        context_since=reference - timedelta(days=_CONTEXT_MATCH_WINDOW_DAYS),
    )
    second_pass_unversioned_excluded = _count_unversioned_second_pass_candidates(
        session,
        reference=reference,
    )
    second_pass_challenged = 0
    second_pass_stale_skipped = 0
    context_affected = sum(
        1 for row in second_pass_candidates if row.get("has_context")
    )
    for row in second_pass_candidates:
        thesis_id = str(row["id"])
        try:
            snapshot, attached_signals = _load_second_pass_snapshot(
                session,
                row,
                reference=reference,
                cost=evaluate_inputs["cost"],
                cycle_key=cycle_key,
            )
            combined = tuple(list(attached_signals) + list(challenge_signals))[
                :_MAX_SECOND_PASS_EVIDENCE
            ]
            challenge_attempts += 1
            decision = challenge_thesis(
                snapshot,
                combined,
                runner=challenger,
            )
            if decision.runner_failed:
                challenger_failures += 1
            _, changed = _persist_falsification(
                session,
                thesis_id,
                decision,
                run_key=f"autonomy:{cycle_key}",
                reference=reference,
            )
            if changed:
                falsification_runs += 1
                second_pass_challenged += 1
            attached = _attach_contradictions(session, thesis_id, decision, combined)
            if attached:
                contradictions_attached += attached
                evaluate_thesis(
                    session,
                    thesis_id,
                    as_of=reference,
                    cost=evaluate_inputs["cost"],
                    attention=evaluate_inputs["attention"],
                    crowding=evaluate_inputs["crowding"],
                    liquidity=evaluate_inputs["liquidity"],
                    downside=evaluate_inputs["downside"],
                    # A catalyst backfilled earlier this cycle postdates the
                    # reference; keep it scoring here exactly as the backfill
                    # evaluation did (a no-op for every other thesis).  The
                    # contradiction links just attached also postdate the
                    # reference, so the attached signals enter scoring
                    # explicitly too.
                    current_catalysts=_catalyst_signals(
                        backfilled_catalysts.get(thesis_id)
                    ),
                    current_evidence=_contradiction_signals(decision, combined),
                )
            if decision.state == "breached":
                if _pause_thesis_if_unchanged(
                    session,
                    thesis_id,
                    status=str(row["status"]),
                    updated_at=row.get("updated_at"),
                    last_evaluated_at=row.get("last_evaluated_at"),
                    fusion_reference_at=row.get("fusion_reference_at"),
                    reference=reference,
                ):
                    paused_count += 1
                else:
                    # A concurrent/newer cycle re-evaluated, re-fused, or
                    # re-stated the thesis after selection (model latency);
                    # the older verdict must not pause newer state.  The
                    # challenge/falsification audit above is reference-
                    # bounded and already persisted, so only the pause is
                    # skipped, as one bounded diagnostic.
                    second_pass_stale_skipped += 1
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
        "legacy_catalyst_backfills": len(legacy_catalyst_ids),
        "forecast_backfills": forecast_backfills,
        "raw_candidate_count": int(tournament.raw_candidate_count),
        "promoted_count": thesis_count,
        "tournament_promoted_count": len(tournament.ranked),
        "promotion_gate_rejections": promotion_gate_rejections,
        "source_gate_rejections": source_gate_rejections,
        "actionability_gate_rejections": actionability_gate_rejections,
        "opposition_gate_rejections": opposition_gate_rejections,
        "rejected_count": len(tournament.rejected),
        "thesis_count": thesis_count,
        "stale_candidates": stale_candidates,
        "scenario_upserts": scenario_upserts,
        "risk_upserts": risk_upserts,
        "forecasts_frozen": forecasts_frozen,
        "identity_backfills": identity_backfills,
        "outcome_resolution": outcome_counts,
        "watch_links": watch_links,
        "playbook_upserts": playbook_upserts,
        "catalyst_upserts": catalyst_upserts,
        "group_count": len(groups_used),
        "opportunity_snapshots": opportunity_snapshots,
        "falsification_runs": falsification_runs,
        "challenge_attempts": challenge_attempts,
        "challenge_limit": challenge_limit,
        "contradictions_attached": contradictions_attached,
        "paused_count": paused_count,
        "second_pass_candidates": len(second_pass_candidates),
        "second_pass_challenged": second_pass_challenged,
        "second_pass_unversioned_excluded": second_pass_unversioned_excluded,
        "second_pass_stale_skipped": second_pass_stale_skipped,
        "context_affected": context_affected,
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
