"""Autonomous thesis-fusion desk: repository and domain APIs.

This module owns all SQL for the thesis desk built on migration 049
(``investment_thesis_groups``, ``investment_thesis_group_members``,
``investment_thesis_scenarios``, ``investment_thesis_forecasts``,
``investment_forecast_outcomes``, ``investment_opportunity_snapshots``,
``investment_thesis_falsification_runs``, ``position_thesis_links``, plus the
additive columns on ``investment_theses`` and ``investment_thesis_evidence``).

Every helper takes the caller's session and never commits or rolls back; all
queries are bounded and deterministic so the API layer can fail soft without
leaking private payloads. Pure scoring math lives in ``thesis_scoring.py`` and
is consumed here: this module never re-implements support/contradiction mass,
neglect, catalyst readiness, expected value, or opportunity gates.

Autonomous claims carry the accepted-reference guard from migration 055
(``investment_theses.fusion_reference_at`` paired with
``investment_theses.fusion_candidate_fingerprint``): a merge claims a
thesis only when the incoming cycle reference is at least the stored guard,
and at an equal reference only when it can prove the identical candidate
fingerprint (see ``merge_or_create_thesis``).  Accepted-reference order,
never completion order, decides which cycle's claim, version, scenario,
catalyst, evidence, evaluation, and challenge state is current -- and at one
reference, only the first proven fingerprint is authoritative.

Identity and merging
--------------------
``canonical_thesis_key`` deterministically maps (theme, subject, direction,
horizon, mechanism) to a SHA-256 key.  Merging a candidate into an existing
thesis is allowed only when the canonical keys match, so a bull and a bear
thesis on the same subject are always distinct records (competitors inside a
group); a contradiction is expressed as contradictory evidence or a linked
competitor, never by flattening.  Agent/model agreement is never evidence:
evidence identity is the content ``evidence_fingerprint``, agent/role
provenance is never persisted or scored, and only the persisted verbatim
``excerpt`` (plus, for contradictions, a structured observation payload)
feeds the auditable-evidence predicate.

Evidence semantics
------------------
Legacy manual rows (``source_family = 'manual'``, stored scores 0.0) are read
back as unknown scores by ``research_intelligence.contracts.EvidenceSignal``;
their content identity is synthesized deterministically from the row key so
they participate in desk scoring without being re-fingerprinted.  Only
auditable evidence contributes directional mass under the shared predicate
(``thesis_scoring.is_auditable_evidence``: nonblank persisted excerpt plus
explicitly positive quality and entailment); unscored, null-excerpt
placeholder rows stay historical/context evidence and never raise evidence
strength or rank eligibility.  Desk evidence requires an explicit
``source_family`` and either a content payload or a precomputed fingerprint,
is deduplicated by fingerprint (identical content scores once) and capped by
``independence_key`` (one row per independent source per thesis), mirroring
the partial unique index from migration 049.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text

from research_intelligence.contracts import (
    EvidenceSignal,
    Scenario,
    canonical_fingerprint,
)
from thesis_scoring import (
    DOWNSIDE_NORMALIZER,
    CatalystSignal,
    assess_evidence,
    assess_opportunity,
    calculate_neglect,
    catalyst_readiness,
    scenario_valuation,
)

ORIGINS = ("manual", "generated", "fusion")
DIRECTIONS = ("long", "short", "neutral")
GROUP_STATUSES = ("active", "archived")
EVIDENCE_RELATIONSHIPS = ("supports", "contradicts", "context", "invalidation")
FORECAST_TYPES = ("price", "earnings", "revenue", "relative", "other")
FORECAST_DIRECTIONS = ("up", "down", "flat")
OUTCOME_STATUSES = ("hit", "miss", "inconclusive")
FALSIFICATION_STATUSES = (
    "pending",
    "in_progress",
    "not_falsified",
    "falsified",
    "inconclusive",
)
LINK_TYPES = ("primary", "secondary", "hedge", "watch")

_MAX_ATTACH_EVIDENCE = 50
_MAX_LOAD_EVIDENCE = 256
_MAX_LOAD_CATALYSTS = 64
_MAX_LOAD_SCENARIOS = 64
_MAX_GROUP_MEMBERS = 50
_MAX_RANKED_OPPORTUNITIES = 100
# Rank-eligibility gates, in fixed order. ``list_ranked_opportunities``
# treats a thesis as rank-eligible only when every gate passes: its status
# remains candidate/active; it has a strictly positive gated opportunity
# score, complete current bull/base/bear scenario legs (non-null
# probabilities summing to one with nonblank descriptions), at least one
# structured risk, at least one auditable supporting evidence row (nonblank
# excerpt, positive quality and entailment), and a latest falsification run
# that is ``not_falsified``; its actionable fields retain complete citations
# from three source families; and a complementary eligible long/short thesis
# exists for the same canonical security and horizon. The per-row eligibility
# columns are ``eligibility_<gate>`` and the reported ``blockers`` reuse the
# same bounded codes.
_ELIGIBILITY_GATES = (
    "status",
    "score",
    "scenarios",
    "risks",
    "evidence",
    "falsification",
    "actionability",
    "opposition",
)
_MAX_TOURNAMENT_THESES = 20
_MAX_TOURNAMENT_CHILDREN = 20
_MAX_FINDINGS = 200
_EXCERPT_LIMIT = 500
_LEGACY_SOURCE_FAMILY = "manual"
_MAX_DETAIL_ROWS = 50
_MAX_STATUS_JOBS = 100
_AUTONOMY_JOB_TYPE = "thesis_autonomy_run"
_INGESTION_SOURCES = (
    "issuer_news",
    "issuer_transcripts",
    "company_expectations",
    "public_equities",
    "cftc",
    "sec_form4",
    "finra_short_volume",
    "cboe_options",
    "fred",
    "filings",
)
# (table, source-timestamp column, acquired/created availability column)
_SOURCE_DATA_TABLES = {
    "issuer_news": ("source_documents", "published_at", "acquired_at"),
    "issuer_transcripts": ("source_documents", "published_at", "acquired_at"),
    "company_expectations": ("source_documents", "published_at", "acquired_at"),
    "public_equities": ("market_data", "timestamp", "created_at"),
    "cftc": ("positioning_reports", "report_date", "acquired_at"),
    "sec_form4": ("positioning_reports", "report_date", "acquired_at"),
    "finra_short_volume": ("positioning_reports", "report_date", "acquired_at"),
    "cboe_options": ("option_chain_snapshots", "captured_at", "created_at"),
    "fred": ("macro_series", "observed_at", "acquired_at"),
    "filings": ("investment_filing_deltas", "created_at", "created_at"),
}
_SOURCE_FILTERED_TABLES = frozenset(
    {
        "source_documents",
        "market_data",
        "positioning_reports",
        "option_chain_snapshots",
        "macro_series",
    }
)
_JOB_FAILED_STATES = frozenset({"failed_retryable", "failed_terminal"})
_ATTENTION_EVIDENCE_TARGET = 10
_LIQUIDITY_LOOKBACK_BARS = 20
# Median daily notional turnover in quote currency: <= 1m maps to zero and
# >= 1bn maps to one. The opportunity gate therefore requires roughly 4m.
_LIQUIDITY_LOG_FLOOR = 6.0
_LIQUIDITY_LOG_CEILING = 9.0


def _rows(result: Any) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in result.mappings().all()]
    except AttributeError:
        return [dict(row) for row in result]


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


def _uuid(value: Any, field: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"invalid {field}") from None


def _text(value: Any, maximum: int) -> str | None:
    text_value = " ".join(str(value or "").split())
    return text_value[:maximum] if text_value else None


def _text_required(value: Any, maximum: int, field: str) -> str:
    result = _text(value, maximum)
    if not result:
        raise ValueError(f"{field} is required")
    return result


def _excerpt(value: Any) -> str | None:
    text_value = str(value or "").strip()
    return text_value[:_EXCERPT_LIMIT] if text_value else None


def _score(value: Any, field: str, *, required: bool = False) -> float | None:
    """Finite 0..1 score or None; NaN/inf/out-of-range raise ValueError."""
    if value is None:
        if required:
            raise ValueError(f"{field} is required")
        return None
    if isinstance(value, bool) or isinstance(value, str):
        raise ValueError(f"invalid {field}")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"invalid {field}") from None
    if not math.isfinite(parsed) or not (0.0 <= parsed <= 1.0):
        raise ValueError(f"invalid {field}")
    return parsed


def _expected_return(value: Any) -> float:
    """Finite, magnitude-bounded fractional return or 0.0 when unknown.

    Mirrors the domain cap (``MAX_ABS_RETURN`` = 100) enforced by the
    scenario CHECK in migration 049; None normalizes to the column default
    so callers can omit the leg return without inventing conviction.
    """
    if value is None:
        return 0.0
    parsed = _finite_number(value, "expected_return")
    if parsed is None or abs(parsed) > 100.0:
        raise ValueError("invalid expected_return")
    return parsed


def _finite_number(value: Any, field: str, *, required: bool = False) -> float | None:
    if value is None:
        if required:
            raise ValueError(f"{field} is required")
        return None
    if isinstance(value, bool) or isinstance(value, str):
        raise ValueError(f"invalid {field}")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"invalid {field}") from None
    if not math.isfinite(parsed):
        raise ValueError(f"invalid {field}")
    return parsed


def _date(value: Any, field: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"invalid {field}") from None
    raise ValueError(f"invalid {field}")


def _timestamp(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"invalid {field}") from None
    else:
        raise ValueError(f"invalid {field}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _json_list(value: Any, field: str, maximum: int) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be a list")
    if len(value) > maximum:
        raise ValueError(f"{field} has too many items")
    return list(value)


def _citation_map(value: Any) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("citation_map must be an object")
    if len(value) > 20:
        raise ValueError("citation_map has too many fields")
    output: dict[str, list[str]] = {}
    for raw_field, raw_refs in value.items():
        field = _text_required(raw_field, 50, "citation_map field")
        refs = _json_list(raw_refs, f"citation_map.{field}", 20)
        cleaned = [ref for item in refs if (ref := _text(item, 240)) is not None]
        if len(cleaned) != len(refs):
            raise ValueError(f"citation_map.{field} contains a blank reference")
        output[field] = list(dict.fromkeys(cleaned))
    return output


def _same(a: Any, b: Any) -> bool:
    """None-aware equality used for version-change detection."""
    if a is None or b is None:
        return a is None and b is None
    return a == b


def _theme_exists(session: Any, theme_id: str) -> bool:
    row = _first(
        session.execute(
            text(
                "SELECT 1 AS present FROM investment_themes "
                "WHERE id = CAST(:id AS UUID) LIMIT 1"
            ),
            {"id": theme_id},
        )
    )
    return row is not None


def _thesis_exists(session: Any, thesis_id: str) -> bool:
    row = _first(
        session.execute(
            text(
                "SELECT 1 AS present FROM investment_theses "
                "WHERE id = CAST(:id AS UUID) LIMIT 1"
            ),
            {"id": thesis_id},
        )
    )
    return row is not None


def _group_exists(session: Any, group_id: str) -> bool:
    row = _first(
        session.execute(
            text(
                "SELECT 1 AS present FROM investment_thesis_groups "
                "WHERE id = CAST(:id AS UUID) LIMIT 1"
            ),
            {"id": group_id},
        )
    )
    return row is not None


def _normalized_subject(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def canonical_thesis_key(
    *,
    theme_id: Any,
    subject: Any,
    direction: Any,
    horizon: Any,
    mechanism: Any,
) -> str:
    """Deterministic identity of a thesis candidate.

    The key is derived from exactly the fields that merging requires to be
    compatible: theme, subject, direction, horizon, and mechanism.  Two
    candidates that share a key are the same thesis; candidates that differ
    in any of these fields (for example a bull and a bear thesis) are
    distinct records that compete inside a group.  Pure and deterministic:
    no database access.
    """
    parsed_direction = str(direction or "").strip().lower()
    if parsed_direction not in DIRECTIONS:
        raise ValueError(f"unsupported direction:{parsed_direction[:32]}")
    return canonical_fingerprint(
        {
            "theme_id": str(_uuid(theme_id, "theme_id")),
            "subject": _normalized_subject(subject),
            "direction": parsed_direction,
            "horizon": _normalized_subject(horizon),
            "mechanism": _normalized_subject(mechanism),
        }
    )


def create_find_group(
    session: Any,
    *,
    name: str,
    description: str | None = None,
    status: str = "active",
) -> dict[str, Any]:
    """Find or create one thesis group by unique name.

    Returns ``{"id": ..., "created": bool}``; a second call with the same
    name returns the existing group without writing.  Creation is
    concurrency-safe: the INSERT takes the unique-name conflict and returns
    no row when a concurrent transaction created the group first, so the
    caller falls back to SELECTing the winner's row (never committing the
    caller's transaction).  If that SELECT misses, the competing INSERT was
    rolled back and the insert is retried.
    """
    group_name = _text_required(name, 200, "name")
    group_description = _text(description, 5000)
    if status not in GROUP_STATUSES:
        raise ValueError(f"unsupported group status:{str(status)[:32]}")
    for _ in range(2):
        row = _first(
            session.execute(
                text(
                    """INSERT INTO investment_thesis_groups
                       (name, description, status)
                       VALUES (:name, :description, :status)
                       ON CONFLICT (name) DO NOTHING
                       RETURNING id"""
                ),
                {
                    "name": group_name,
                    "description": group_description,
                    "status": status,
                },
            )
        )
        if row is not None:
            return {"id": str(row["id"]), "created": True}
        existing = _first(
            session.execute(
                text(
                    "SELECT id FROM investment_thesis_groups WHERE name = :name LIMIT 1"
                ),
                {"name": group_name},
            )
        )
        if existing is not None:
            return {"id": str(existing["id"]), "created": False}
    raise RuntimeError(f"group creation failed for name:{group_name[:64]}")


def _subject_for_key(*, subject: Any, company: Any, symbol: Any) -> str:
    explicit = _normalized_subject(subject)
    if explicit:
        return explicit
    company_value = _normalized_subject(company)
    if company_value:
        return company_value
    return _normalized_subject(symbol)


def _append_version(
    session: Any,
    thesis_id: str,
    *,
    claim: str,
    variant_perception: str | None,
    confidence: float | None,
    rationale: str | None,
    changed_by: str,
    trend_context: str | None = None,
    valuation_context: str | None = None,
    sentiment_context: str | None = None,
    citation_map: Mapping[str, Any] | None = None,
) -> int:
    version_row = _first(
        session.execute(
            text(
                "SELECT COALESCE(MAX(version), 0) AS max_version "
                "FROM investment_thesis_versions "
                "WHERE thesis_id = CAST(:id AS UUID)"
            ),
            {"id": thesis_id},
        )
    )
    next_version = int(version_row["max_version"]) + 1
    session.execute(
        text(
            """INSERT INTO investment_thesis_versions
               (thesis_id, version, claim, variant_perception, confidence,
                trend_context, valuation_context, sentiment_context, citation_map,
                rationale, changed_by)
               VALUES (CAST(:thesis_id AS UUID), :version, :claim,
                       :variant_perception, :confidence, :trend_context,
                       :valuation_context, :sentiment_context,
                       CAST(:citation_map AS JSONB), :rationale, :changed_by)"""
        ),
        {
            "thesis_id": thesis_id,
            "version": next_version,
            "claim": claim,
            "variant_perception": variant_perception,
            "confidence": confidence,
            "trend_context": _text(trend_context, 2000),
            "valuation_context": _text(valuation_context, 2000),
            "sentiment_context": _text(sentiment_context, 2000),
            "citation_map": json.dumps(_citation_map(citation_map), sort_keys=True),
            "rationale": _text(rationale, 5000),
            "changed_by": changed_by,
        },
    )
    return next_version


def merge_or_create_thesis(
    session: Any,
    *,
    theme_id: str,
    company: str | None = None,
    symbol: str | None = None,
    subject: str | None = None,
    claim: str,
    variant_perception: str | None = None,
    horizon: str | None = None,
    mechanism: str | None = None,
    direction: str = "neutral",
    catalyst_summary: str | None = None,
    confidence: float | None = None,
    trend_context: str | None = None,
    valuation_context: str | None = None,
    sentiment_context: str | None = None,
    citation_map: Mapping[str, Any] | None = None,
    invalidation_conditions: list[Any] | None = None,
    rationale: str | None = None,
    origin: str = "generated",
    input_fingerprint: str | None = None,
    accepted_reference: datetime | None = None,
) -> dict[str, Any]:
    """Merge a candidate into the matching thesis or create a new one.

    Candidate identity is the canonical key over (theme, subject, direction,
    horizon, mechanism); a candidate whose key matches an existing thesis is
    merged into it (appending an ``investment_thesis_versions`` row whenever
    the claim, variant, or confidence changed, preserving status), and a
    candidate with a different key is created as its own record with version
    1.  ``input_fingerprint`` is globally unique (migration 049) and must be
    content-addressed over thesis identity + inputs: a candidate whose
    fingerprint already belongs to a thesis merges into it, and a
    fingerprint inconsistent with the computed identity raises ValueError.

    ``accepted_reference`` is the monotonic accepted-reference guard
    (migration 055): autonomous cycles always pass the cycle reference and
    MUST also pass a nonblank ``input_fingerprint`` (the content-addressed
    candidate fingerprint), or the merge fails with ValueError before any
    lock or write.  An existing thesis is claimed only when its stored
    ``fusion_reference_at`` is NULL or less than the incoming reference, or
    equals it with a stored ``fusion_candidate_fingerprint`` identical to
    the incoming one (an idempotent/resumable rerun); the thesis row is
    locked (``FOR UPDATE``) before the check, so concurrent claims
    serialize on it.  Equal reference with a different or unprovable
    (NULL) stored fingerprint is stale: at one reference only the first
    proven fingerprint is authoritative, so lock/completion order can never
    choose between different model outputs.  Autonomous merges additionally
    take a transaction-scoped advisory lock keyed by the canonical identity
    before the lookups, so two concurrent cycles can never both see "no
    thesis" and race the canonical_key unique index: the loser waits, then
    finds the winner's row and claims it or is rejected as stale.  When
    the incoming claim is stale, the merge is an explicit no-op returning
    ``stale: True`` -- no version is appended and no
    claim/confidence/current field is mutated -- and the caller must skip
    every child-state write for that candidate.  A successful autonomous
    claim atomically persists reference and fingerprint on new inserts and
    on changed or unchanged claims, and advances the stored guard even when
    the claim content is unchanged, so an older cycle finishing later can
    never write after a newer one.  Manual/non-autonomy callers omit the
    argument: they never reject on the guard, never take the advisory lock,
    and never modify or erase the stored guard pair.
    Returns ``{"id", "created", "version", "changed", "stale",
    "canonical_key"}``.
    """
    theme_id = _uuid(theme_id, "theme_id")
    if not _theme_exists(session, theme_id):
        raise ValueError("unknown theme")
    if direction not in DIRECTIONS:
        raise ValueError(f"unsupported direction:{str(direction)[:32]}")
    if origin not in ORIGINS:
        raise ValueError(f"unsupported origin:{str(origin)[:32]}")
    claim_text = _text_required(claim, 5000, "claim")
    variant = _text(variant_perception, 2000)
    thesis_confidence = _score(confidence, "confidence")
    trend = _text(trend_context, 2000)
    valuation = _text(valuation_context, 2000)
    sentiment = _text(sentiment_context, 2000)
    citations = _citation_map(citation_map)
    conditions = _json_list(
        invalidation_conditions or [], "invalidation_conditions", 200
    )
    reference = _timestamp(accepted_reference, "accepted_reference")
    fingerprint = _text(input_fingerprint, 200)
    if reference is not None and not fingerprint:
        # An accepted-reference claim must prove WHICH candidate it is:
        # without a fingerprint, distinct model outputs could claim the
        # same reference interchangeably and completion order would decide.
        # Fail before any write -- no advisory lock, no lookup, no
        # INSERT/UPDATE -- so an autonomous caller can never persist a
        # reference it cannot prove.
        raise ValueError("accepted_reference requires a nonblank candidate fingerprint")
    key_subject = _subject_for_key(subject=subject, company=company, symbol=symbol)
    key = canonical_thesis_key(
        theme_id=theme_id,
        subject=key_subject,
        direction=direction,
        horizon=horizon,
        mechanism=mechanism,
    )
    if reference is not None:
        # Transaction-scoped advisory lock keyed by the canonical identity.
        # Two autonomous cycles can otherwise both see "no thesis" and race
        # the canonical_key unique index on create; serializing on the key
        # makes the loser wait for the winner's transaction, then find the
        # winner's row and claim it (or be rejected as stale) instead of
        # failing with a unique violation.  The fingerprint path is covered
        # too: the fingerprint is content-addressed over identity + inputs,
        # so identical submissions share this key.  Manual/non-autonomy
        # merges never take the lock and keep their pre-055 semantics.
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": key},
        )
    existing = None
    if fingerprint is not None:
        # input_fingerprint is globally unique and content-addressed over
        # thesis identity + inputs, so an identical candidate submission
        # must merge into the thesis that produced it.  The row is locked
        # so the accepted-reference claim below is race-free.
        existing = _first(
            session.execute(
                text(
                    """SELECT id, claim, variant_perception, confidence, status,
                              trend_context, valuation_context, sentiment_context,
                              citation_map, canonical_key, fusion_reference_at,
                              fusion_candidate_fingerprint
                       FROM investment_theses
                       WHERE input_fingerprint = :fingerprint
                       LIMIT 1 FOR UPDATE"""
                ),
                {"fingerprint": fingerprint},
            )
        )
        if existing is not None and str(existing.get("canonical_key")) != key:
            raise ValueError("input_fingerprint conflicts with thesis identity")
    if existing is None:
        existing = _first(
            session.execute(
                text(
                    """SELECT id, claim, variant_perception, confidence, status,
                              trend_context, valuation_context, sentiment_context,
                              citation_map, canonical_key, fusion_reference_at,
                              fusion_candidate_fingerprint
                       FROM investment_theses
                       WHERE canonical_key = :key
                       LIMIT 1 FOR UPDATE"""
                ),
                {"key": key},
            )
        )
    if existing is not None:
        thesis_id = str(existing["id"])
        stored_reference = _timestamp(
            existing.get("fusion_reference_at"), "fusion_reference_at"
        )
        stored_fingerprint = _text(existing.get("fusion_candidate_fingerprint"), 200)
        stale_claim = False
        if reference is not None:
            if stored_reference is not None and stored_reference > reference:
                # A newer accepted reference already claimed this thesis.
                stale_claim = True
            elif stored_reference == reference and (
                stored_fingerprint is None or stored_fingerprint != fingerprint
            ):
                # The same accepted reference is already proven to a
                # different candidate output -- or to a fingerprint this
                # cycle cannot prove (NULL/legacy).  Only the first proven
                # fingerprint is authoritative at a reference, so this
                # candidate is stale even though its reference is not older:
                # lock/completion order must never choose between distinct
                # outputs at one reference.
                stale_claim = True
        if stale_claim:
            # The incoming candidate is stale.  Nothing is claimed or
            # written -- no version, no claim/confidence/current-field
            # mutation -- and the explicit outcome lets the caller skip
            # every child-state write (evidence, catalyst, scenarios,
            # playbook, position link, forecast, evaluation, challenge).
            current_version = _first(
                session.execute(
                    text(
                        "SELECT COALESCE(MAX(version), 0) AS max_version "
                        "FROM investment_thesis_versions "
                        "WHERE thesis_id = CAST(:id AS UUID)"
                    ),
                    {"id": thesis_id},
                )
            )
            return {
                "id": thesis_id,
                "created": False,
                "version": int(current_version["max_version"]) or 1,
                "changed": False,
                "stale": True,
                "canonical_key": key,
            }
        changed = not (
            _same(existing.get("claim"), claim_text)
            and _same(existing.get("variant_perception"), variant)
            and _same(existing.get("confidence"), thesis_confidence)
            and _same(existing.get("trend_context"), trend)
            and _same(existing.get("valuation_context"), valuation)
            and _same(existing.get("sentiment_context"), sentiment)
            and _same(existing.get("citation_map") or {}, citations)
        )
        if changed:
            next_version = _append_version(
                session,
                thesis_id,
                claim=claim_text,
                variant_perception=variant,
                confidence=thesis_confidence,
                trend_context=trend,
                valuation_context=valuation,
                sentiment_context=sentiment,
                citation_map=citations,
                rationale=rationale or "merged candidate update",
                changed_by="fusion",
            )
            if reference is not None:
                session.execute(
                    text(
                        """UPDATE investment_theses
                           SET claim = :claim, variant_perception = :variant_perception,
                               confidence = :confidence, catalyst_summary = :summary,
                               trend_context = :trend_context,
                               valuation_context = :valuation_context,
                               sentiment_context = :sentiment_context,
                               citation_map = CAST(:citation_map AS JSONB),
                               fusion_reference_at = :accepted_reference,
                               fusion_candidate_fingerprint = :accepted_fingerprint,
                               updated_at = NOW()
                           WHERE id = CAST(:id AS UUID)"""
                    ),
                    {
                        "id": thesis_id,
                        "claim": claim_text,
                        "variant_perception": variant,
                        "confidence": thesis_confidence,
                        "summary": _text(catalyst_summary, 2000),
                        "trend_context": trend,
                        "valuation_context": valuation,
                        "sentiment_context": sentiment,
                        "citation_map": json.dumps(citations, sort_keys=True),
                        "accepted_reference": reference,
                        "accepted_fingerprint": fingerprint,
                    },
                )
            else:
                session.execute(
                    text(
                        """UPDATE investment_theses
                           SET claim = :claim, variant_perception = :variant_perception,
                               confidence = :confidence, catalyst_summary = :summary,
                               trend_context = :trend_context,
                               valuation_context = :valuation_context,
                               sentiment_context = :sentiment_context,
                               citation_map = CAST(:citation_map AS JSONB),
                               updated_at = NOW()
                           WHERE id = CAST(:id AS UUID)"""
                    ),
                    {
                        "id": thesis_id,
                        "claim": claim_text,
                        "variant_perception": variant,
                        "confidence": thesis_confidence,
                        "summary": _text(catalyst_summary, 2000),
                        "trend_context": trend,
                        "valuation_context": valuation,
                        "sentiment_context": sentiment,
                        "citation_map": json.dumps(citations, sort_keys=True),
                    },
                )
        else:
            if reference is not None:
                # Claim the thesis at the incoming reference even when the
                # content is unchanged: the cycle still accepted it, and its
                # child-state writes must stay guarded against an older
                # cycle that finishes later.  Reference and fingerprint are
                # persisted together; the WHERE makes identical re-claims
                # (the resumable equal-reference rerun) a no-op.
                session.execute(
                    text(
                        """UPDATE investment_theses
                           SET fusion_reference_at = :accepted_reference,
                               fusion_candidate_fingerprint = :accepted_fingerprint
                           WHERE id = CAST(:id AS UUID)
                             AND fusion_reference_at
                                 IS DISTINCT FROM :accepted_reference"""
                    ),
                    {
                        "id": thesis_id,
                        "accepted_reference": reference,
                        "accepted_fingerprint": fingerprint,
                    },
                )
            next_version = _first(
                session.execute(
                    text(
                        "SELECT COALESCE(MAX(version), 0) AS max_version "
                        "FROM investment_thesis_versions "
                        "WHERE thesis_id = CAST(:id AS UUID)"
                    ),
                    {"id": thesis_id},
                )
            )
            next_version = int(next_version["max_version"]) or 1
        return {
            "id": thesis_id,
            "created": False,
            "version": next_version,
            "changed": changed,
            "stale": False,
            "canonical_key": key,
        }
    row = _first(
        session.execute(
            text(
                """INSERT INTO investment_theses
                   (theme_id, company, symbol, claim, variant_perception,
                    horizon, mechanism, direction, catalyst_summary, confidence,
                    trend_context, valuation_context, sentiment_context, citation_map,
                    invalidation_conditions, origin, canonical_key,
                    input_fingerprint, fusion_reference_at,
                    fusion_candidate_fingerprint)
                   VALUES (:theme_id, :company, :symbol, :claim,
                           :variant_perception, :horizon, :mechanism, :direction,
                           :catalyst_summary, :confidence, :trend_context,
                           :valuation_context, :sentiment_context,
                           CAST(:citation_map AS JSONB),
                           CAST(:invalidation_conditions AS JSONB), :origin,
                           :canonical_key, :input_fingerprint, :fusion_reference_at,
                           :fusion_candidate_fingerprint)
                   RETURNING id"""
            ),
            {
                "theme_id": theme_id,
                "company": _text(company, 200),
                "symbol": _text(symbol, 20),
                "claim": claim_text,
                "variant_perception": variant,
                "horizon": _text(horizon, 50),
                "mechanism": _text(mechanism, 1000),
                "direction": direction,
                "catalyst_summary": _text(catalyst_summary, 2000),
                "confidence": thesis_confidence,
                "trend_context": trend,
                "valuation_context": valuation,
                "sentiment_context": sentiment,
                "citation_map": json.dumps(citations, sort_keys=True),
                "invalidation_conditions": json.dumps(conditions),
                "origin": origin,
                "canonical_key": key,
                "input_fingerprint": _text(input_fingerprint, 200),
                "fusion_reference_at": reference,
                # The fingerprint pair is meaningful only under an accepted
                # reference: manual creations stay outside the guard.
                "fusion_candidate_fingerprint": (
                    fingerprint if reference is not None else None
                ),
            },
        )
    )
    thesis_id = str(row["id"])
    _append_version(
        session,
        thesis_id,
        claim=claim_text,
        variant_perception=variant,
        confidence=thesis_confidence,
        trend_context=trend,
        valuation_context=valuation,
        sentiment_context=sentiment,
        citation_map=citations,
        rationale=rationale or "initial candidate",
        changed_by="fusion",
    )
    return {
        "id": thesis_id,
        "created": True,
        "version": 1,
        "changed": True,
        "stale": False,
        "canonical_key": key,
    }


def add_group_membership(
    session: Any,
    group_id: str,
    thesis_id: str,
    *,
    note: str | None = None,
) -> bool:
    """Add a thesis to a group; idempotent for active memberships.

    Enforces the desk invariant transactionally: a thesis belongs to at most
    one active group.  Adding a thesis that already holds an active
    membership in another group raises ValueError (call
    ``remove_group_membership`` first to move it); adding to its current
    group is a no-op returning False.  The active membership row and the
    ``investment_theses.group_id`` snapshot are written in the caller's
    transaction, and membership ends only by setting ``removed_at``
    (append-only table), never by deletion.  The thesis row is locked for
    the transaction so concurrent add/remove races serialize on it.
    """
    group_id = _uuid(group_id, "group_id")
    thesis_id = _uuid(thesis_id, "thesis_id")
    note_text = _text(note, 500)
    if not _group_exists(session, group_id):
        raise ValueError("unknown group")
    if not _thesis_exists(session, thesis_id):
        raise ValueError("unknown thesis")
    # Serialize concurrent membership changes for this thesis: the row lock
    # makes the probe/insert below race-free across transactions.
    _first(
        session.execute(
            text(
                "SELECT id FROM investment_theses "
                "WHERE id = CAST(:id AS UUID) FOR UPDATE"
            ),
            {"id": thesis_id},
        )
    )
    active = _first(
        session.execute(
            text(
                """SELECT 1 AS present
                   FROM investment_thesis_group_members
                   WHERE group_id = CAST(:group_id AS UUID)
                     AND thesis_id = CAST(:thesis_id AS UUID)
                     AND removed_at IS NULL LIMIT 1"""
            ),
            {"group_id": group_id, "thesis_id": thesis_id},
        )
    )
    if active is not None:
        return False
    other = _first(
        session.execute(
            text(
                """SELECT 1 AS present
                   FROM investment_thesis_group_members
                   WHERE thesis_id = CAST(:thesis_id AS UUID)
                     AND group_id <> CAST(:group_id AS UUID)
                     AND removed_at IS NULL LIMIT 1"""
            ),
            {"group_id": group_id, "thesis_id": thesis_id},
        )
    )
    if other is not None:
        raise ValueError("thesis already belongs to another group")
    session.execute(
        text(
            """INSERT INTO investment_thesis_group_members
               (group_id, thesis_id, note)
               VALUES (CAST(:group_id AS UUID), CAST(:thesis_id AS UUID), :note)
               ON CONFLICT DO NOTHING"""
        ),
        {"group_id": group_id, "thesis_id": thesis_id, "note": note_text},
    )
    # Keep the snapshot column in sync with the versioned membership row.
    session.execute(
        text(
            """UPDATE investment_theses
               SET group_id = CAST(:group_id AS UUID)
               WHERE id = CAST(:thesis_id AS UUID)"""
        ),
        {"group_id": group_id, "thesis_id": thesis_id},
    )
    return True


def remove_group_membership(
    session: Any,
    group_id: str,
    thesis_id: str,
) -> bool:
    """End one thesis's active membership in a group (append-only).

    Sets ``removed_at`` on the active membership row (identity columns are
    immutable and DELETE is rejected by the migration 049 trigger), then
    clears the ``investment_theses.group_id`` snapshot when it still points
    at this group.  Returns True when an active membership was ended, False
    when none was active.  Re-adding afterwards inserts a fresh row.
    """
    group_id = _uuid(group_id, "group_id")
    thesis_id = _uuid(thesis_id, "thesis_id")
    active = _first(
        session.execute(
            text(
                """SELECT id FROM investment_thesis_group_members
                   WHERE group_id = CAST(:group_id AS UUID)
                     AND thesis_id = CAST(:thesis_id AS UUID)
                     AND removed_at IS NULL LIMIT 1"""
            ),
            {"group_id": group_id, "thesis_id": thesis_id},
        )
    )
    if active is None:
        return False
    session.execute(
        text(
            """UPDATE investment_thesis_group_members
               SET removed_at = NOW()
               WHERE id = CAST(:id AS UUID) AND removed_at IS NULL"""
        ),
        {"id": str(active["id"])},
    )
    session.execute(
        text(
            """UPDATE investment_theses
               SET group_id = NULL
               WHERE id = CAST(:thesis_id AS UUID)
                 AND group_id = CAST(:group_id AS UUID)"""
        ),
        {"thesis_id": thesis_id, "group_id": group_id},
    )
    return True


def _existing_evidence_keys(session: Any, thesis_id: str) -> tuple[set[str], set[str]]:
    rows = _rows(
        session.execute(
            text(
                """SELECT evidence_fingerprint, independence_key
                   FROM investment_thesis_evidence
                   WHERE thesis_id = CAST(:id AS UUID)
                     AND (evidence_fingerprint IS NOT NULL
                          OR independence_key IS NOT NULL)
                   ORDER BY created_at, evidence_type, evidence_id
                   LIMIT :limit"""
            ),
            {"id": thesis_id, "limit": _MAX_LOAD_EVIDENCE},
        )
    )
    fingerprints = {
        str(row["evidence_fingerprint"])
        for row in rows
        if row.get("evidence_fingerprint")
    }
    independence = {
        str(row["independence_key"]) for row in rows if row.get("independence_key")
    }
    return fingerprints, independence


def attach_evidence(
    session: Any,
    thesis_id: str,
    evidence: Sequence[Mapping[str, Any]] | None = None,
    *,
    limit: int = _MAX_ATTACH_EVIDENCE,
) -> dict[str, int]:
    """Attach evidence with stable provenance and weight metadata.

    Every row is validated through ``EvidenceSignal`` (bounded, finite,
    deterministic fingerprint from content when not supplied).  Attachment
    is idempotent: evidence whose fingerprint is already attached to the
    thesis is skipped, and evidence sharing an ``independence_key`` with an
    attached row is skipped (correlated-source cap, mirroring the partial
    unique index).  Returns ``{"attached", "skipped_duplicate_fingerprint",
    "skipped_correlated"}`` counts.  ``source_family`` is required for desk
    evidence; legacy rows keep the default ``'manual'`` family.  No commit.
    """
    if evidence is None:
        evidence = []
    thesis_id = _uuid(thesis_id, "thesis_id")
    bounded = _bounded(limit, _MAX_ATTACH_EVIDENCE, _MAX_ATTACH_EVIDENCE)
    items = list(evidence)[:bounded]
    signals: list[tuple[EvidenceSignal, dict[str, Any]]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("invalid evidence row")
        source_family = _text_required(item.get("source_family"), 120, "source_family")
        try:
            signal = EvidenceSignal.create(
                evidence_id=item.get("evidence_id"),
                evidence_type=item.get("evidence_type"),
                relationship=item.get("relationship"),
                source_name=item.get("source_name") or source_family,
                source_family=source_family,
                origin_key=item.get("origin_key"),
                independence_key=item.get("independence_key"),
                evidence_fingerprint=item.get("evidence_fingerprint"),
                content=item.get("content"),
                source_timestamp=item.get("source_timestamp"),
                available_at=item.get("available_at"),
                quality_score=item.get("quality_score"),
                entailment_score=item.get("entailment_score"),
                freshness_score=item.get("freshness_score"),
                effective_weight=item.get("effective_weight"),
            )
        except ValueError as error:
            raise ValueError(f"invalid evidence row: {error}") from error
        signals.append((signal, dict(item)))
    if not _thesis_exists(session, thesis_id):
        raise ValueError("unknown thesis")
    if not signals:
        return {
            "attached": 0,
            "skipped_duplicate_fingerprint": 0,
            "skipped_correlated": 0,
        }
    fingerprints, independence = _existing_evidence_keys(session, thesis_id)
    rows: list[dict[str, Any]] = []
    skipped_fingerprint = 0
    skipped_correlated = 0
    batch_fingerprints: set[str] = set()
    batch_independence: set[str] = set()
    for signal, item in signals:
        if (
            signal.evidence_fingerprint in fingerprints
            or signal.evidence_fingerprint in batch_fingerprints
        ):
            skipped_fingerprint += 1
            continue
        if signal.independence_key is not None and (
            signal.independence_key in independence
            or signal.independence_key in batch_independence
        ):
            skipped_correlated += 1
            continue
        quality = signal.quality_score if signal.quality_score is not None else 0.0
        entailment = (
            signal.entailment_score if signal.entailment_score is not None else 0.0
        )
        freshness = (
            signal.freshness_score if signal.freshness_score is not None else 0.0
        )
        weight = signal.effective_weight if signal.effective_weight is not None else 1.0
        rows.append(
            {
                "thesis_id": thesis_id,
                "evidence_type": signal.evidence_type,
                "evidence_id": signal.evidence_id,
                "relationship": signal.relationship,
                "excerpt": _excerpt(item.get("excerpt")),
                "source_family": signal.source_family,
                "origin_key": signal.origin_key,
                "independence_key": signal.independence_key,
                "evidence_fingerprint": signal.evidence_fingerprint,
                "source_timestamp": signal.source_timestamp,
                "available_at": signal.available_at,
                "quality_score": quality,
                "entailment_score": entailment,
                "freshness_score": freshness,
                "effective_weight": weight,
            }
        )
        batch_fingerprints.add(signal.evidence_fingerprint)
        if signal.independence_key is not None:
            batch_independence.add(signal.independence_key)
    if rows:
        session.execute(
            text(
                """INSERT INTO investment_thesis_evidence
                   (thesis_id, evidence_type, evidence_id, relationship, excerpt,
                    source_family, origin_key, independence_key,
                    evidence_fingerprint, source_timestamp, available_at,
                    quality_score, entailment_score, freshness_score,
                    effective_weight)
                   VALUES (CAST(:thesis_id AS UUID), :evidence_type,
                           :evidence_id, :relationship, :excerpt, :source_family,
                           :origin_key, :independence_key, :evidence_fingerprint,
                           :source_timestamp, :available_at, :quality_score,
                           :entailment_score, :freshness_score, :effective_weight)
                   ON CONFLICT DO NOTHING"""
            ),
            rows,
        )
        session.execute(
            text(
                "UPDATE investment_theses SET last_evidence_at = NOW() "
                "WHERE id = CAST(:id AS UUID)"
            ),
            {"id": thesis_id},
        )
    return {
        "attached": len(rows),
        "skipped_duplicate_fingerprint": skipped_fingerprint,
        "skipped_correlated": skipped_correlated,
    }


def upsert_scenario(
    session: Any,
    thesis_id: str,
    *,
    name: str,
    description: str | None = None,
    probability: float | None = None,
    expected_return: float | None = None,
    is_base_case: bool = False,
) -> dict[str, Any]:
    """Upsert one scenario as an immutable version replacement.

    ``probability`` is a finite 0..1 value or None (unknown): an unknown
    leg is persisted as NULL and never defaulted to conviction.
    ``expected_return`` is a finite, magnitude-bounded (+/-100) fractional
    return; None persists as the column default 0 (a neutral, not a
    conviction).  Revisions insert a new version and supersede the active
    row (the old row is never updated or deleted); an identical revision is
    a no-op.  Only one active base case per thesis: promoting a different
    scenario locks and supersedes the current base row, then appends an
    immutable non-base successor with identical content so the old base's
    version chain stays complete; promoting the current base itself just
    revisions it.  Returns ``{"id", "version", "changed"}``.
    """
    thesis_id = _uuid(thesis_id, "thesis_id")
    scenario_name = _text_required(name, 200, "name")
    scenario_description = _text(description, 2000)
    scenario_probability = _score(probability, "probability")
    scenario_return = _expected_return(expected_return)
    base_case = bool(is_base_case)
    if not _thesis_exists(session, thesis_id):
        raise ValueError("unknown thesis")
    active = _first(
        session.execute(
            text(
                """SELECT id, version, description, probability,
                          expected_return, is_base_case
                   FROM investment_thesis_scenarios
                   WHERE thesis_id = CAST(:thesis_id AS UUID)
                     AND name = :name AND superseded_at IS NULL LIMIT 1"""
            ),
            {"thesis_id": thesis_id, "name": scenario_name},
        )
    )
    if active is not None and (
        _same(active.get("description"), scenario_description)
        and _same(active.get("probability"), scenario_probability)
        and _same(active.get("expected_return"), scenario_return)
        and bool(active.get("is_base_case")) == base_case
    ):
        return {
            "id": str(active["id"]),
            "version": int(active["version"]),
            "changed": False,
        }
    if base_case:
        # Lock the current active base.  When a different scenario takes
        # over, supersede the old base row and append an immutable non-base
        # successor with identical content (never update it in place);
        # when the promoted scenario is the base row itself, its revision
        # below replaces it directly and no successor is needed.
        base = _first(
            session.execute(
                text(
                    """SELECT id, name, version, description, probability,
                              expected_return
                       FROM investment_thesis_scenarios
                       WHERE thesis_id = CAST(:thesis_id AS UUID)
                         AND is_base_case AND superseded_at IS NULL
                       LIMIT 1 FOR UPDATE"""
                ),
                {"thesis_id": thesis_id},
            )
        )
        if base is not None and (
            active is None or str(base["id"]) != str(active["id"])
        ):
            session.execute(
                text(
                    """UPDATE investment_thesis_scenarios
                       SET superseded_at = NOW()
                       WHERE id = CAST(:id AS UUID) AND superseded_at IS NULL"""
                ),
                {"id": str(base["id"])},
            )
            _first(
                session.execute(
                    text(
                        """INSERT INTO investment_thesis_scenarios
                           (thesis_id, name, description, probability,
                            expected_return, is_base_case, version)
                           VALUES (CAST(:thesis_id AS UUID), :name, :description,
                                   :probability, :expected_return, :is_base_case,
                                   :version)
                           RETURNING id"""
                    ),
                    {
                        "thesis_id": thesis_id,
                        "name": str(base["name"]),
                        "description": base.get("description"),
                        "probability": base.get("probability"),
                        "expected_return": base.get("expected_return"),
                        "is_base_case": False,
                        "version": int(base["version"]) + 1,
                    },
                )
            )
    if active is not None:
        session.execute(
            text(
                """UPDATE investment_thesis_scenarios
                   SET superseded_at = NOW()
                   WHERE id = CAST(:id AS UUID) AND superseded_at IS NULL"""
            ),
            {"id": str(active["id"])},
        )
        next_version = int(active["version"]) + 1
    else:
        next_version = 1
    row = _first(
        session.execute(
            text(
                """INSERT INTO investment_thesis_scenarios
                   (thesis_id, name, description, probability,
                    expected_return, is_base_case, version)
                   VALUES (CAST(:thesis_id AS UUID), :name, :description,
                           :probability, :expected_return, :is_base_case,
                           :version)
                   RETURNING id"""
            ),
            {
                "thesis_id": thesis_id,
                "name": scenario_name,
                "description": scenario_description,
                "probability": scenario_probability,
                "expected_return": scenario_return,
                "is_base_case": base_case,
                "version": next_version,
            },
        )
    )
    return {
        "id": str(row["id"]),
        "version": next_version,
        "changed": True,
    }


class _ForecastSuccessorLost(Exception):
    """Internal freeze_forecast control flow.

    Raised inside the revision savepoint when the successor INSERT loses
    the unique-index race: the savepoint rolls back, undoing the
    supersede so the caller's previously active forecast stays
    authoritative.  Never escapes freeze_forecast.
    """


def _forecast_content_same(
    active: Mapping[str, Any], values: Mapping[str, Any]
) -> bool:
    return (
        _same(active.get("forecast_type"), values["forecast_type"])
        and _same(active.get("direction"), values["direction"])
        and _same(active.get("target_value"), values["target_value"])
        and _same(
            _date(active.get("target_date"), "target_date"),
            values["target_date"],
        )
        and _same(active.get("scenario_id"), values["scenario_id"])
    )


def freeze_forecast(
    session: Any,
    thesis_id: str,
    *,
    forecast_key: str,
    forecast_type: str = "price",
    direction: str = "up",
    target_value: float | None = None,
    target_date: date | str | None = None,
    as_of: datetime | str | None = None,
    scenario_id: str | None = None,
) -> dict[str, Any]:
    """Freeze one forecast at a point in time (append-only versioning).

    A frozen forecast row is immutable; re-freezing the same key with
    different content supersedes the active row and inserts a new version,
    preserving the earlier point-in-time record for outcome measurement.
    Re-freezing with identical content is a no-op.  Forecast keys are global
    (``UNIQUE (forecast_key, version)``), so re-using an active key on a
    different thesis raises ValueError.  The insert is atomic: concurrent
    creators are serialized by the database unique indexes (one active
    forecast per key and per non-null scenario), so a job that loses the
    race gets an idempotent no-op instead of an aborted transaction, and
    the winner's row (first frozen as_of/reference/target/date values) is
    reported with ``changed`` False.  Before any revision the target
    scenario's own row is locked (the consistent serialization point for
    scenario ownership), the key-leg active row is locked (``FOR
    UPDATE``), and the target scenario is preflighted: an active forecast
    owned by another key is authoritative, so the revision reports that
    owner under the loser contract without touching the caller's active
    row.  A revision supersede and its successor INSERT run inside a
    nested savepoint; if the INSERT loses (only a non-conforming
    concurrent writer can make it do so), the savepoint rolls back and
    the supersede is undone, so a call that reports unchanged or conflict
    can never silently retire the caller's previously active forecast —
    even when the caller catches the error and keeps the transaction
    open.  Returns ``{"id", "version", "changed"}``.
    """
    thesis_id = _uuid(thesis_id, "thesis_id")
    key = _text_required(forecast_key, 200, "forecast_key")
    if forecast_type not in FORECAST_TYPES:
        raise ValueError(f"unsupported forecast_type:{str(forecast_type)[:32]}")
    if direction not in FORECAST_DIRECTIONS:
        raise ValueError(f"unsupported direction:{str(direction)[:32]}")
    target = _finite_number(target_value, "target_value")
    target_day = _date(target_date, "target_date")
    # as_of is NOT NULL in migration 049: an omitted timestamp materializes
    # to a timezone-aware UTC now so the bound parameter is never NULL.
    frozen_at = _timestamp(as_of, "as_of") or datetime.now(UTC)
    scenario = _uuid(scenario_id, "scenario_id") if scenario_id is not None else None
    if not _thesis_exists(session, thesis_id):
        raise ValueError("unknown thesis")
    if scenario is not None:
        known = _first(
            session.execute(
                text(
                    """SELECT 1 AS present FROM investment_thesis_scenarios
                       WHERE id = CAST(:id AS UUID)
                         AND thesis_id = CAST(:thesis_id AS UUID) LIMIT 1"""
                ),
                {"id": scenario, "thesis_id": thesis_id},
            )
        )
        if known is None:
            raise ValueError("unknown scenario for thesis")
        # Lock the target scenario's own row for every freeze: the single
        # serialization point for ownership of that scenario.  Concurrent
        # freezes targeting the same scenario queue on this lock, so the
        # preflight below is authoritative and the successor INSERT can
        # never lose the scenario leg to a conforming writer.
        session.execute(
            text(
                """SELECT id FROM investment_thesis_scenarios
                   WHERE id = CAST(:id AS UUID)
                     AND thesis_id = CAST(:thesis_id AS UUID)
                   FOR UPDATE"""
            ),
            {"id": scenario, "thesis_id": thesis_id},
        )
    active = _first(
        session.execute(
            text(
                """SELECT id, thesis_id, scenario_id, version, forecast_type,
                          direction, target_value, target_date
                   FROM investment_thesis_forecasts
                   WHERE forecast_key = :key AND superseded_at IS NULL LIMIT 1
                   FOR UPDATE"""
            ),
            {"key": key},
        )
    )
    values = {
        "forecast_type": forecast_type,
        "direction": direction,
        "target_value": target,
        "target_date": target_day,
        "scenario_id": scenario,
    }
    if active is not None:
        if str(active["thesis_id"]) != thesis_id:
            raise ValueError("forecast_key already in use by another thesis")
        if _forecast_content_same(active, values):
            return {
                "id": str(active["id"]),
                "version": int(active["version"]),
                "changed": False,
            }
        next_version = int(active["version"]) + 1
    else:
        next_version = 1
    if scenario is not None:
        # Preflight the target scenario before any supersede, while still
        # holding the scenario-row lock: at most one active forecast per
        # scenario, and an active owner under a different key is
        # authoritative.  Report that owner under the loser contract and
        # leave the caller's active row untouched.  Every conforming
        # freeze takes this lock before writing, so the ownership read
        # cannot go stale before the supersede/INSERT below.
        owner = _first(
            session.execute(
                text(
                    """SELECT id, thesis_id, forecast_key, version
                       FROM investment_thesis_forecasts
                       WHERE scenario_id = CAST(:scenario_id AS UUID)
                         AND superseded_at IS NULL LIMIT 1"""
                ),
                {"scenario_id": scenario},
            )
        )
        if owner is not None and str(owner["forecast_key"]) != key:
            if str(owner["thesis_id"]) != thesis_id:
                raise ValueError("forecast_key already in use by another thesis")
            return {
                "id": str(owner["id"]),
                "version": int(owner["version"]),
                "changed": False,
            }

    def _insert() -> dict[str, Any] | None:
        # Atomic insert guarded by the partial unique indexes (one active
        # row per forecast_key and per non-null scenario); a loser gets a
        # no-op, never an aborted transaction.
        return _first(
            session.execute(
                text(
                    """INSERT INTO investment_thesis_forecasts
                       (thesis_id, scenario_id, forecast_key, forecast_type,
                        direction, target_value, target_date, as_of, version)
                       VALUES (CAST(:thesis_id AS UUID), :scenario_id,
                               :forecast_key, :forecast_type, :direction,
                               :target_value, :target_date, :as_of, :version)
                       ON CONFLICT DO NOTHING
                       RETURNING id"""
                ),
                {
                    "thesis_id": thesis_id,
                    "scenario_id": scenario,
                    "forecast_key": key,
                    "forecast_type": forecast_type,
                    "direction": direction,
                    "target_value": target,
                    "target_date": target_day,
                    "as_of": frozen_at,
                    "version": next_version,
                },
            )
        )

    if active is not None:
        # The partial active-key index allows at most one unsuperseded row
        # per forecast_key, so the old row must be retired before the
        # successor INSERT.  That ordering is safe: the scenario-row lock
        # (above) serializes every conforming freeze on the target
        # scenario, making the preflight authoritative, and the key-leg
        # FOR UPDATE serializes same-key revisions, so the successor
        # INSERT is guaranteed to win.  The supersede still runs inside a
        # nested savepoint: if the INSERT ever loses anyway (only a
        # non-conforming concurrent writer could make it do so), the
        # savepoint rolls back and the supersede is undone, so a conflict
        # report can never silently retire the caller's previously active
        # forecast — even when the caller catches the error and keeps the
        # transaction open.
        try:
            with session.begin_nested():
                session.execute(
                    text(
                        """UPDATE investment_thesis_forecasts
                           SET superseded_at = NOW()
                           WHERE id = CAST(:id AS UUID) AND superseded_at IS NULL"""
                    ),
                    {"id": str(active["id"])},
                )
                row = _insert()
                if row is None:
                    raise _ForecastSuccessorLost()
        except _ForecastSuccessorLost:
            # Savepoint rolled back: the supersede is undone and the
            # caller's previously active row is authoritative again.
            row = None
    else:
        row = _insert()
    if row is not None:
        return {
            "id": str(row["id"]),
            "version": next_version,
            "changed": True,
        }
    # A concurrent job froze the same active forecast key or scenario
    # first: the unique indexes are the final authority, this INSERT was
    # an atomic no-op, and the loser reports the winner's row without
    # claiming a write.  The first frozen as_of/reference/target/date
    # values win; nothing is superseded or overwritten here.  In the
    # revision path the savepoint rollback restored the caller's own row,
    # so a key-leg hit on that own row falls through to the scenario leg.
    winner = _first(
        session.execute(
            text(
                """SELECT id, thesis_id, version
                   FROM investment_thesis_forecasts
                   WHERE forecast_key = :key AND superseded_at IS NULL LIMIT 1"""
            ),
            {"key": key},
        )
    )
    if winner is not None and (
        active is None or str(winner["id"]) != str(active["id"])
    ):
        if str(winner["thesis_id"]) != thesis_id:
            raise ValueError("forecast_key already in use by another thesis")
        return {
            "id": str(winner["id"]),
            "version": int(winner["version"]),
            "changed": False,
        }
    if scenario is not None:
        # The collision was on the active-scenario unique index with a
        # different forecast_key (rerun fingerprint/target drift): locate
        # the winner through the scenario leg instead.
        winner = _first(
            session.execute(
                text(
                    """SELECT id, thesis_id, version
                       FROM investment_thesis_forecasts
                       WHERE scenario_id = CAST(:scenario_id AS UUID)
                         AND superseded_at IS NULL LIMIT 1"""
                ),
                {"scenario_id": scenario},
            )
        )
        if winner is not None:
            if str(winner["thesis_id"]) != thesis_id:
                raise ValueError("forecast_key already in use by another thesis")
            return {
                "id": str(winner["id"]),
                "version": int(winner["version"]),
                "changed": False,
            }
    # The concurrent winner rolled back after our no-op: retry the insert
    # once (still atomic and conflict-safe), then surface the invariant.
    row = _insert()
    if row is not None:
        return {
            "id": str(row["id"]),
            "version": next_version,
            "changed": True,
        }
    raise RuntimeError("forecast insert lost without a winner")


def record_forecast_outcome(
    session: Any,
    forecast_id: str,
    *,
    status: str,
    actual_value: float | None = None,
    measured_at: datetime | str | None = None,
    notes: str | None = None,
) -> bool:
    """Record one terminal outcome against a frozen forecast version.

    Outcomes are unique per forecast (``UNIQUE (forecast_id)``); a second
    recording for the same forecast is an idempotent no-op returning False.
    """
    forecast_id = _uuid(forecast_id, "forecast_id")
    if status not in OUTCOME_STATUSES:
        raise ValueError(f"unsupported outcome status:{str(status)[:32]}")
    actual = _finite_number(actual_value, "actual_value")
    # measured_at is NOT NULL in migration 049: an omitted timestamp
    # materializes to a timezone-aware UTC now so the bound parameter is
    # never NULL.
    measured = _timestamp(measured_at, "measured_at") or datetime.now(UTC)
    notes_text = _text(notes, 2000)
    known = _first(
        session.execute(
            text(
                "SELECT 1 AS present FROM investment_thesis_forecasts "
                "WHERE id = CAST(:id AS UUID) LIMIT 1"
            ),
            {"id": forecast_id},
        )
    )
    if known is None:
        raise ValueError("unknown forecast")
    existing = _first(
        session.execute(
            text(
                "SELECT 1 AS present FROM investment_forecast_outcomes "
                "WHERE forecast_id = CAST(:id AS UUID) LIMIT 1"
            ),
            {"id": forecast_id},
        )
    )
    if existing is not None:
        return False
    row = _first(
        session.execute(
            text(
                """INSERT INTO investment_forecast_outcomes
                   (forecast_id, status, actual_value, measured_at, notes)
                   VALUES (CAST(:forecast_id AS UUID), :status, :actual_value,
                           :measured_at, :notes)
                   ON CONFLICT (forecast_id) DO NOTHING
                   RETURNING id"""
            ),
            {
                "forecast_id": forecast_id,
                "status": status,
                "actual_value": actual,
                "measured_at": measured,
                "notes": notes_text,
            },
        )
    )
    # A concurrent winner makes the INSERT a no-op: report the truthful
    # insertion result instead of claiming a write that never happened.
    return row is not None


def append_opportunity_snapshot(
    session: Any,
    thesis_id: str,
    *,
    snapshot_key: str,
    opportunity_score: float,
    expected_value: float = 0.0,
    expected_shortfall: float = 0.0,
    confidence_score: float = 0.0,
    neglect_score: float = 0.0,
    catalyst_score: float = 0.0,
    evidence_strength: float = 0.0,
    contradiction_strength: float = 0.0,
    input_fingerprint: str | None = None,
    captured_at: datetime | str | None = None,
) -> bool:
    """Append one frozen opportunity snapshot (append-only table).

    Snapshots are keyed by ``(thesis_id, snapshot_key)``; re-appending an
    existing key is an idempotent no-op returning False.
    """
    thesis_id = _uuid(thesis_id, "thesis_id")
    key = _text_required(snapshot_key, 200, "snapshot_key")
    scores = {
        "opportunity_score": _score(
            opportunity_score, "opportunity_score", required=True
        ),
        "confidence_score": _score(confidence_score, "confidence_score"),
        "neglect_score": _score(neglect_score, "neglect_score"),
        "catalyst_score": _score(catalyst_score, "catalyst_score"),
        "evidence_strength": _score(evidence_strength, "evidence_strength"),
        "contradiction_strength": _score(
            contradiction_strength, "contradiction_strength"
        ),
    }
    expected = {
        "expected_value": _finite_number(expected_value, "expected_value"),
        "expected_shortfall": _finite_number(expected_shortfall, "expected_shortfall"),
    }
    # captured_at is NOT NULL in migration 049: an omitted timestamp
    # materializes to a timezone-aware UTC now so the bound parameter is
    # never NULL.
    captured = _timestamp(captured_at, "captured_at") or datetime.now(UTC)
    if not _thesis_exists(session, thesis_id):
        raise ValueError("unknown thesis")
    existing = _first(
        session.execute(
            text(
                """SELECT 1 AS present FROM investment_opportunity_snapshots
                   WHERE thesis_id = CAST(:thesis_id AS UUID)
                     AND snapshot_key = :snapshot_key LIMIT 1"""
            ),
            {"thesis_id": thesis_id, "snapshot_key": key},
        )
    )
    if existing is not None:
        return False
    row = _first(
        session.execute(
            text(
                """INSERT INTO investment_opportunity_snapshots
                   (thesis_id, snapshot_key, input_fingerprint, opportunity_score,
                    expected_value, expected_shortfall, confidence_score,
                    neglect_score, catalyst_score, evidence_strength,
                    contradiction_strength, captured_at)
                   VALUES (CAST(:thesis_id AS UUID), :snapshot_key,
                           :input_fingerprint, :opportunity_score, :expected_value,
                           :expected_shortfall, :confidence_score, :neglect_score,
                           :catalyst_score, :evidence_strength,
                           :contradiction_strength, :captured_at)
                   ON CONFLICT (thesis_id, snapshot_key) DO NOTHING
                   RETURNING id"""
            ),
            {
                "thesis_id": thesis_id,
                "snapshot_key": key,
                "input_fingerprint": _text(input_fingerprint, 200),
                # Unknown sub-metrics stay NULL (migration 057) so a frozen
                # snapshot never turns an absent input into a favorable zero.
                "opportunity_score": scores["opportunity_score"],
                "expected_value": expected["expected_value"],
                "expected_shortfall": expected["expected_shortfall"],
                "confidence_score": scores["confidence_score"],
                "neglect_score": scores["neglect_score"],
                "catalyst_score": scores["catalyst_score"],
                "evidence_strength": scores["evidence_strength"],
                "contradiction_strength": scores["contradiction_strength"],
                "captured_at": captured,
            },
        )
    )
    # A concurrent winner makes the INSERT a no-op: report the truthful
    # insertion result instead of claiming a write that never happened.
    return row is not None


def record_falsification_run(
    session: Any,
    thesis_id: str,
    *,
    run_key: str,
    status: str = "pending",
    findings: list[Any] | None = None,
    started_at: datetime | str | None = None,
) -> str:
    """Record one falsification run; idempotent per (thesis_id, run_key).

    Returns the run id (existing when the run was already recorded).  Status
    transitions are applied through ``update_falsification_run``.
    """
    thesis_id = _uuid(thesis_id, "thesis_id")
    key = _text_required(run_key, 200, "run_key")
    if status not in FALSIFICATION_STATUSES:
        raise ValueError(f"unsupported run status:{str(status)[:32]}")
    findings_list = _json_list(findings or [], "findings", _MAX_FINDINGS)
    # started_at is NOT NULL in migration 049: an omitted timestamp
    # materializes to a timezone-aware UTC now so the bound parameter is
    # never NULL.
    started = _timestamp(started_at, "started_at") or datetime.now(UTC)
    if not _thesis_exists(session, thesis_id):
        raise ValueError("unknown thesis")
    existing = _first(
        session.execute(
            text(
                """SELECT id FROM investment_thesis_falsification_runs
                   WHERE thesis_id = CAST(:thesis_id AS UUID)
                     AND run_key = :run_key LIMIT 1"""
            ),
            {"thesis_id": thesis_id, "run_key": key},
        )
    )
    if existing is not None:
        return str(existing["id"])
    row = _first(
        session.execute(
            text(
                """INSERT INTO investment_thesis_falsification_runs
                   (thesis_id, run_key, status, findings, started_at)
                   VALUES (CAST(:thesis_id AS UUID), :run_key, :status,
                           CAST(:findings AS JSONB), :started_at)
                   ON CONFLICT (thesis_id, run_key) DO NOTHING
                   RETURNING id"""
            ),
            {
                "thesis_id": thesis_id,
                "run_key": key,
                "status": status,
                "findings": json.dumps(findings_list),
                "started_at": started,
            },
        )
    )
    if row is not None:
        return str(row["id"])
    # A concurrent winner made the INSERT a no-op: return the winner's id
    # through a bounded lookup and never mutate the existing run.
    winner = _first(
        session.execute(
            text(
                """SELECT id FROM investment_thesis_falsification_runs
                   WHERE thesis_id = CAST(:thesis_id AS UUID)
                     AND run_key = :run_key LIMIT 1"""
            ),
            {"thesis_id": thesis_id, "run_key": key},
        )
    )
    if winner is None:
        raise RuntimeError("falsification run insert lost without a winner")
    return str(winner["id"])


def update_falsification_run(
    session: Any,
    run_id: str,
    *,
    status: str,
    findings: list[Any] | None = None,
    completed_at: datetime | str | None = None,
) -> None:
    """Advance one falsification run through its lifecycle.

    Runs are append-only with a frozen identity; status moves from
    ``pending``/``in_progress`` to a terminal state exactly once.  Terminal
    runs reject further updates; ``completed_at`` defaults to NOW() when a
    run becomes terminal.
    """
    run_id = _uuid(run_id, "run_id")
    if status not in FALSIFICATION_STATUSES:
        raise ValueError(f"unsupported run status:{str(status)[:32]}")
    findings_list = _json_list(findings or [], "findings", _MAX_FINDINGS)
    completed = _timestamp(completed_at, "completed_at")
    current = _first(
        session.execute(
            text(
                """SELECT status, started_at, completed_at
                   FROM investment_thesis_falsification_runs
                   WHERE id = CAST(:id AS UUID) LIMIT 1"""
            ),
            {"id": run_id},
        )
    )
    if current is None:
        raise ValueError("unknown falsification run")
    if str(current.get("status")) not in ("pending", "in_progress"):
        raise ValueError("falsification run status is final")
    if status in ("pending", "in_progress"):
        if completed is not None:
            raise ValueError("completed_at requires a terminal status")
    else:
        if completed is None:
            completed = datetime.now(UTC)
        started = current.get("started_at")
        if started is not None and completed < started:
            raise ValueError("completed_at before started_at")
    session.execute(
        text(
            """UPDATE investment_thesis_falsification_runs
               SET status = :status, findings = CAST(:findings AS JSONB),
                   completed_at = :completed_at
               WHERE id = CAST(:id AS UUID)"""
        ),
        {
            "id": run_id,
            "status": status,
            "findings": json.dumps(findings_list),
            "completed_at": completed,
        },
    )


def link_position(
    session: Any,
    thesis_id: str,
    position_id: str,
    *,
    link_type: str = "primary",
) -> bool:
    """Link a portfolio position to a thesis; idempotent.

    Returns False when the same active (position, thesis, link_type) link
    already exists.  Links are append-only audit records (migration 049):
    only rows with ``removed_at IS NULL`` count as active, so a relink after
    ``unlink_position`` inserts a fresh row, and concurrent linkers race
    safely on the partial active unique index.
    """
    thesis_id = _uuid(thesis_id, "thesis_id")
    position_id = _uuid(position_id, "position_id")
    if link_type not in LINK_TYPES:
        raise ValueError(f"unsupported link_type:{str(link_type)[:32]}")
    if not _thesis_exists(session, thesis_id):
        raise ValueError("unknown thesis")
    holding = _first(
        session.execute(
            text(
                "SELECT 1 AS present FROM portfolio_holdings "
                "WHERE id = CAST(:id AS UUID) LIMIT 1"
            ),
            {"id": position_id},
        )
    )
    if holding is None:
        raise ValueError("unknown position")
    existing = _first(
        session.execute(
            text(
                """SELECT 1 AS present FROM position_thesis_links
                   WHERE position_id = CAST(:position_id AS UUID)
                     AND thesis_id = CAST(:thesis_id AS UUID)
                     AND link_type = :link_type
                     AND removed_at IS NULL LIMIT 1"""
            ),
            {
                "position_id": position_id,
                "thesis_id": thesis_id,
                "link_type": link_type,
            },
        )
    )
    if existing is not None:
        return False
    row = _first(
        session.execute(
            text(
                """INSERT INTO position_thesis_links
                   (position_id, thesis_id, link_type)
                   VALUES (CAST(:position_id AS UUID), CAST(:thesis_id AS UUID),
                           :link_type)
                   ON CONFLICT (position_id, thesis_id, link_type)
                       WHERE removed_at IS NULL
                   DO NOTHING
                   RETURNING id"""
            ),
            {
                "position_id": position_id,
                "thesis_id": thesis_id,
                "link_type": link_type,
            },
        )
    )
    # A concurrent active-link winner makes the INSERT a no-op: report the
    # truthful insertion result instead of claiming a write that never
    # happened.  The precheck above still short-circuits the common case.
    return row is not None


def unlink_position(
    session: Any,
    thesis_id: str,
    position_id: str,
    *,
    link_type: str = "primary",
) -> bool:
    """End one position-thesis link (versioned audit trail).

    Links are append-only: unlinking sets ``removed_at`` on the active row
    (identity columns are immutable and DELETE is rejected by the migration
    049 trigger), so the audit record survives.  Returns True when an active
    link was ended, False when none was active.  Re-linking afterwards
    inserts a fresh row.
    """
    thesis_id = _uuid(thesis_id, "thesis_id")
    position_id = _uuid(position_id, "position_id")
    if link_type not in LINK_TYPES:
        raise ValueError(f"unsupported link_type:{str(link_type)[:32]}")
    active = _first(
        session.execute(
            text(
                """SELECT id FROM position_thesis_links
                   WHERE position_id = CAST(:position_id AS UUID)
                     AND thesis_id = CAST(:thesis_id AS UUID)
                     AND link_type = :link_type
                     AND removed_at IS NULL LIMIT 1"""
            ),
            {
                "position_id": position_id,
                "thesis_id": thesis_id,
                "link_type": link_type,
            },
        )
    )
    if active is None:
        return False
    session.execute(
        text(
            """UPDATE position_thesis_links
               SET removed_at = NOW()
               WHERE id = CAST(:id AS UUID) AND removed_at IS NULL"""
        ),
        {"id": str(active["id"])},
    )
    return True


def _evidence_signal_from_row(row: Mapping[str, Any]) -> EvidenceSignal:
    """Build a scoring signal from one persisted evidence row.

    Legacy manual rows carry no fingerprint and zero stored scores; their
    content identity is synthesized deterministically from the row key and
    the stored 0.0 scores normalize to unknown inside ``EvidenceSignal``,
    so unscored placeholder rows fail the auditable-evidence predicate and
    stay historical/context evidence (they never raise evidence strength or
    rank eligibility).  The persisted ``excerpt`` column travels into
    ``provenance["excerpt"]`` so the auditable-evidence predicate
    (``thesis_scoring.is_auditable_evidence``) sees the same bounded verbatim
    excerpt the persisted path and the same-cycle path score identically.
    """
    fingerprint = row.get("evidence_fingerprint")
    if not fingerprint:
        fingerprint = canonical_fingerprint(
            {
                "legacy_evidence": (
                    row.get("evidence_type"),
                    row.get("evidence_id"),
                    row.get("relationship"),
                    row.get("source_family") or _LEGACY_SOURCE_FAMILY,
                )
            }
        )
    family = row.get("source_family") or _LEGACY_SOURCE_FAMILY
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


def _derived_attention(signals: Sequence[EvidenceSignal]) -> float | None:
    """Use bounded unique evidence density as a transparent attention proxy.

    Counts the merged scoring signals (cutoff-valid persisted rows plus any
    explicit current-cycle evidence), so a fresh attach that postdates the
    cutoff still contributes the same attention it would have in a live run.
    """
    identities = {
        signal.evidence_fingerprint or f"{signal.evidence_type}:{signal.evidence_id}"
        for signal in signals
    }
    identities.discard(":")
    if not identities:
        return None
    return min(1.0, len(identities) / float(_ATTENTION_EVIDENCE_TARGET))


def _market_liquidity_score(
    session: Any,
    thesis_id: str,
    reference: datetime,
) -> float | None:
    """Map recent median daily notional turnover to a fixed 0..1 score.

    Replay-safe: only bars timestamped at/before ``reference`` whose row
    revision time (``COALESCE(updated_at, created_at)``) is at/before
    ``reference`` can contribute, so a bar backfilled after the reference
    never changes a historical score, and neither does a pre-reference row
    revised after the reference (any row mutation bumps ``updated_at``).
    """
    rows = _rows(
        session.execute(
            text(
                """SELECT m.close, m.volume
                   FROM investment_theses t
                   JOIN market_data m ON m.symbol = t.symbol
                   WHERE t.id = CAST(:id AS UUID)
                     AND t.symbol IS NOT NULL
                     AND m.timeframe = '1d'
                     AND m.timestamp <= :as_of
                     AND COALESCE(m.updated_at, m.created_at) <= :as_of
                     AND m.close IS NOT NULL
                     AND m.volume IS NOT NULL
                   ORDER BY m.timestamp DESC, m.source, m.created_at DESC
                   LIMIT :limit"""
            ),
            {
                "id": thesis_id,
                "as_of": reference,
                "limit": _LIQUIDITY_LOOKBACK_BARS,
            },
        )
    )
    notionals: list[float] = []
    for row in rows:
        try:
            close = float(row.get("close"))
            volume = float(row.get("volume"))
        except (TypeError, ValueError, OverflowError):
            continue
        notional = close * volume
        if math.isfinite(notional) and notional > 0.0:
            notionals.append(notional)
    if not notionals:
        return None
    ordered = sorted(notionals)
    midpoint = len(ordered) // 2
    median = (
        ordered[midpoint]
        if len(ordered) % 2
        else (ordered[midpoint - 1] + ordered[midpoint]) / 2.0
    )
    span = _LIQUIDITY_LOG_CEILING - _LIQUIDITY_LOG_FLOOR
    return min(1.0, max(0.0, (math.log10(median) - _LIQUIDITY_LOG_FLOOR) / span))


def _scenario_downside(valuation: Any) -> float | None:
    """Normalize complete expected shortfall; partial scenarios stay unknown."""
    if (
        int(valuation.scenario_count) <= 0
        or int(valuation.missing_probability_count) > 0
        or not bool(valuation.probabilities_sum_to_one)
    ):
        return None
    return min(1.0, max(0.0, float(valuation.expected_shortfall) / DOWNSIDE_NORMALIZER))


def _merge_current_cycle_scenarios(
    scenarios: Sequence[Scenario],
    explicit: Sequence[Scenario],
) -> list[Scenario]:
    """Merge cutoff-valid persisted scenarios with current-cycle legs.

    A current-cycle leg replaces any persisted row with the same label:
    the cycle's immutable upsert superseded that row during the run, so the
    explicit leg is the artifact the current cycle derived (its content may
    only encode source inputs available by the reference cutoff).
    """
    explicit_labels = {item.label.casefold() for item in explicit}
    merged = [
        item for item in scenarios if item.label.casefold() not in explicit_labels
    ]
    merged.extend(explicit)
    return merged


def _merge_current_cycle_catalysts(
    catalysts: Sequence[CatalystSignal],
    explicit: Sequence[CatalystSignal],
) -> list[CatalystSignal]:
    """Merge cutoff-valid persisted catalysts with current-cycle ones.

    A persisted row wins on identical description: the cycle's catalyst
    insert is a no-op when the description already exists, so the persisted
    row is the artifact (including its state).  Current-cycle catalysts
    with a new description are appended.
    """
    descriptions = {item.description.casefold() for item in catalysts}
    merged = list(catalysts)
    merged.extend(
        item for item in explicit if item.description.casefold() not in descriptions
    )
    return merged


def _merge_current_cycle_evidence(
    evidence: Sequence[EvidenceSignal],
    explicit: Sequence[EvidenceSignal],
) -> list[EvidenceSignal]:
    """Merge cutoff-valid persisted evidence with current-cycle signals.

    Evidence identity is the content fingerprint, and a persisted row wins
    on an identical fingerprint: the stored row is the canonical artifact
    (it carries the persisted quality/weight scores the cycle or desk
    stored).  Current-cycle signals -- cited evidence or challenger
    contradictions attached during the run whose link rows postdate the
    cutoff -- are appended only when their fingerprint is new, so each
    piece of evidence contributes exactly once and the merge stays
    deterministic (persisted order first, then explicit input order).
    """
    fingerprints = {item.evidence_fingerprint for item in evidence}
    merged = list(evidence)
    for item in explicit:
        if item.evidence_fingerprint not in fingerprints:
            fingerprints.add(item.evidence_fingerprint)
            merged.append(item)
    return merged


def evaluate_thesis(
    session: Any,
    thesis_id: str,
    *,
    as_of: datetime | str | None = None,
    expected_returns: Mapping[str, float] | None = None,
    cost: float = 0.0,
    attention: float | None = None,
    crowding: float | None = None,
    liquidity: float | None = None,
    downside: float | None = None,
    snapshot_key: str | None = None,
    current_scenarios: Sequence[Scenario] | None = None,
    current_catalysts: Sequence[CatalystSignal] | None = None,
    current_evidence: Sequence[EvidenceSignal] | None = None,
) -> dict[str, Any]:
    """Score one thesis end to end and persist the current score columns.

    Loads bounded evidence, catalysts, and active scenarios, delegates every
    calculation to ``thesis_scoring``, then writes the thesis-level score
    columns (``evidence_strength``, ``contradiction_strength``,
    ``neglect_score``, ``catalyst_score``, ``confidence_score``,
    ``expected_value``, ``expected_shortfall``, ``opportunity_score``,
    ``last_evaluated_at``).  When ``snapshot_key`` is given, the same result
    is also frozen as an opportunity snapshot in the caller's transaction.
    Scenario legs use their persisted ``expected_return`` (migration 049) so
    point-in-time scoring is stable; ``expected_returns`` is a fallback for
    rows stored before the column existed, and unpriced legs default to 0.0.

    Replay safety: when ``as_of`` is supplied every persisted input query is
    bounded by it.  Evidence rows must be persisted as thesis links
    (``created_at``) at/before the cutoff AND available at it (effective
    source and availability timestamps at/before ``as_of``), so an old
    source attached to the thesis after the cutoff is never admitted into a
    historical score.  Market bars must be timestamped AND ingested
    at/before ``as_of``.  Catalysts are restricted to rows created AND last
    updated at/before the cutoff (``updated_at`` stamps pre-migration
    mutations, migration 054, so later-mutated rows cannot leak into older
    replays), and scenarios to versions created and not superseded before
    it.  ``current_scenarios``/``current_catalysts``/``current_evidence``
    carry the current cycle's own derived artifacts (bounded input path)
    and are merged deterministically over the cutoff-valid persisted rows:
    persisted rows win on identical scenario label / catalyst description /
    evidence fingerprint, and new explicit artifacts are appended.  Later
    source evidence, bars, attached links, or derived versions can never
    alter an older accepted cutoff.

    Score persistence is monotonic: ``last_evaluated_at`` is set to the
    accepted ``as_of`` reference and the UPDATE only applies when the
    thesis was never evaluated or was last evaluated at/before the
    reference, so an older finishing job can never regress newer current
    ranking columns.  The computed result is still returned and a frozen
    ``snapshot_key`` opportunity row is still appended (immutable history)
    even when the UPDATE is skipped as stale.
    """
    thesis_id = _uuid(thesis_id, "thesis_id")
    reference = _timestamp(as_of, "as_of") or datetime.now(UTC)
    if not _thesis_exists(session, thesis_id):
        raise ValueError("unknown thesis")
    returns = dict(expected_returns or {})
    for name, value in returns.items():
        _finite_number(value, f"expected_return:{str(name)[:64]}")
    evidence_rows = _rows(
        session.execute(
            text(
                """SELECT evidence_type, evidence_id, relationship, excerpt,
                          source_family, origin_key, independence_key,
                          evidence_fingerprint, source_timestamp, available_at,
                          quality_score, entailment_score, freshness_score,
                          effective_weight, created_at
                   FROM investment_thesis_evidence
                   WHERE thesis_id = CAST(:id AS UUID)
                     AND created_at <= :as_of
                     AND COALESCE(source_timestamp, created_at) <= :as_of
                     AND COALESCE(available_at, source_timestamp, created_at) <= :as_of
                   ORDER BY created_at, evidence_type, evidence_id
                   LIMIT :limit"""
            ),
            {"id": thesis_id, "as_of": reference, "limit": _MAX_LOAD_EVIDENCE},
        )
    )
    signals = [_evidence_signal_from_row(row) for row in evidence_rows]
    if current_evidence:
        signals = _merge_current_cycle_evidence(signals, current_evidence)
    catalyst_rows = _rows(
        session.execute(
            text(
                """SELECT description, state, expected_at
                   FROM investment_catalysts
                   WHERE thesis_id = CAST(:id AS UUID)
                     AND created_at <= :as_of
                     AND updated_at <= :as_of
                   ORDER BY expected_at NULLS LAST, created_at, id
                   LIMIT :limit"""
            ),
            {"id": thesis_id, "as_of": reference, "limit": _MAX_LOAD_CATALYSTS},
        )
    )
    catalysts = [
        CatalystSignal.create(
            description=row.get("description"),
            state=row.get("state") or "pending",
            expected_at=row.get("expected_at"),
        )
        for row in catalyst_rows
    ]
    if current_catalysts:
        catalysts = _merge_current_cycle_catalysts(catalysts, current_catalysts)
    scenario_rows = _rows(
        session.execute(
            text(
                """SELECT name, probability, expected_return
                   FROM investment_thesis_scenarios
                   WHERE thesis_id = CAST(:id AS UUID)
                     AND created_at <= :as_of
                     AND (superseded_at IS NULL OR superseded_at > :as_of)
                   ORDER BY is_base_case DESC, created_at, name
                   LIMIT :limit"""
            ),
            {"id": thesis_id, "as_of": reference, "limit": _MAX_LOAD_SCENARIOS},
        )
    )
    scenarios = [
        Scenario.create(
            label=row.get("name") or "scenario",
            probability=row.get("probability"),
            expected_return=(
                row.get("expected_return")
                if row.get("expected_return") is not None
                else returns.get(row.get("name"), 0.0)
            ),
        )
        for row in scenario_rows
    ]
    if current_scenarios:
        scenarios = _merge_current_cycle_scenarios(scenarios, current_scenarios)
    evidence_score = assess_evidence(signals)
    attention_value = (
        _score(attention, "attention")
        if attention is not None
        else _derived_attention(signals)
    )
    crowding_value = _score(crowding, "crowding")
    neglect = calculate_neglect(
        attention=attention_value,
        crowding=crowding_value,
    )
    catalyst = catalyst_readiness(catalysts, as_of=reference)
    valuation = scenario_valuation(scenarios, cost=_finite_number(cost, "cost") or 0.0)
    liquidity_value = _score(liquidity, "liquidity")
    if liquidity_value is None:
        liquidity_value = _market_liquidity_score(session, thesis_id, reference)
    downside_value = _score(downside, "downside")
    if downside_value is None:
        downside_value = _scenario_downside(valuation)
    opportunity = assess_opportunity(
        evidence_strength=evidence_score.support_mass,
        confidence=evidence_score.confidence,
        neglect=neglect.neglect,
        catalyst_ready=catalyst.readiness,
        liquidity=liquidity_value,
        downside=downside_value,
    )
    session.execute(
        text(
            """UPDATE investment_theses
               SET evidence_strength = :evidence_strength,
                   contradiction_strength = :contradiction_strength,
                   neglect_score = :neglect_score,
                   catalyst_score = :catalyst_score,
                   confidence_score = :confidence_score,
                   expected_value = :expected_value,
                   expected_shortfall = :expected_shortfall,
                   opportunity_score = :opportunity_score,
                   last_evaluated_at = :as_of
               WHERE id = CAST(:id AS UUID)
                 AND (last_evaluated_at IS NULL OR last_evaluated_at <= :as_of)"""
        ),
        {
            "id": thesis_id,
            "as_of": reference,
            "evidence_strength": evidence_score.support_mass,
            "contradiction_strength": evidence_score.contradiction_mass,
            # Unknown sub-metrics persist as NULL (migration 057): an absent
            # neglect input, catalyst set, or directional evidence is
            # unknown, never a favorable zero.  Evaluated zeros — empty
            # support/contradiction mass, a zero expected value or a gated
            # zero opportunity — remain numeric.
            "neglect_score": neglect.neglect,
            "catalyst_score": catalyst.readiness,
            "confidence_score": evidence_score.confidence,
            "expected_value": valuation.expected_value,
            "expected_shortfall": valuation.expected_shortfall,
            "opportunity_score": opportunity.opportunity,
        },
    )
    result = {
        "thesis_id": thesis_id,
        "as_of": reference.isoformat(),
        "evidence": evidence_score.to_dict(),
        "neglect": neglect.to_dict(),
        "catalyst": catalyst.to_dict(),
        "valuation": valuation.to_dict(),
        "opportunity": opportunity.to_dict(),
    }
    if snapshot_key is not None:
        evidence_input_fingerprint = None
        if signals:
            evidence_input_fingerprint = canonical_fingerprint(
                sorted(signal.evidence_fingerprint for signal in signals)
            )
        append_opportunity_snapshot(
            session,
            thesis_id,
            snapshot_key=snapshot_key,
            opportunity_score=opportunity.opportunity,
            expected_value=valuation.expected_value,
            expected_shortfall=valuation.expected_shortfall,
            confidence_score=evidence_score.confidence,
            neglect_score=neglect.neglect,
            catalyst_score=catalyst.readiness,
            evidence_strength=evidence_score.support_mass,
            contradiction_strength=evidence_score.contradiction_mass,
            input_fingerprint=evidence_input_fingerprint,
            captured_at=reference,
        )
    return result


def list_ranked_opportunities(
    session: Any,
    *,
    limit: int = 50,
    group_id: str | None = None,
    minimum_score: float = 0.0,
    include_ineligible: bool = False,
) -> list[dict[str, Any]]:
    """List canonical exposures in deterministic investability order.

    Only rank-eligible theses are returned by default: a positive gated
    opportunity score alone is not enough. A thesis is rank-eligible when
    every gate in ``_ELIGIBILITY_GATES`` passes against *current* rows only:
    its status is ``candidate`` or ``active``; its gated
    ``opportunity_score`` is strictly positive; its active
    (``superseded_at IS NULL``) bull/base/bear scenario legs each carry a
    non-null probability, a finite expected return, and a nonblank
    description, and collectively sum to one within floating-point tolerance
    (matching ``thesis_scoring``'s 1e-9 sum check); at least one structured
    risk row exists; at least one auditable supporting evidence row exists
    (``supports`` relationship, nonblank excerpt, positive quality and
    entailment scores); its latest falsification run — deterministically the
    most recently started, tie-broken by run key — is ``not_falsified``; its
    actionable fields retain complete citations from at least three source
    families; and a complementary base-eligible long/short thesis exists for
    the same canonical symbol (or, when both symbols are unavailable, exact
    company) and horizon. Historical superseded scenarios, earlier
    falsification runs, paused/closed theses, and an opponent failing any
    base gate never satisfy eligibility. Eligible theses rank before blocked
    theses, then by expected value, opportunity score, confidence, catalyst
    readiness, neglect, evaluation recency, and id. Semantically repetitive
    fusion rows for the same security/company, direction, and horizon collapse
    to the highest-ranked row; complementary directions remain competitors.
    Group filtering goes through the versioned membership table
    (``investment_thesis_group_members``).

    ``include_ineligible`` explicitly opts into seeing blocked rows as
    well; every returned row then (and, for a uniform truthful response,
    by default too) carries ``eligible`` (bool) and ``blockers`` (the
    bounded failing gate codes from ``_ELIGIBILITY_GATES``).  Zero-score
    and never-evaluated (NULL-score) rows are reachable only through this
    explicit opt-in; they are always marked ineligible, and NULL scores
    rank after every measured value (including zero) because the ordering
    pins ``NULLS LAST`` on every metric column.
    """
    bounded = _bounded(limit, 50, _MAX_RANKED_OPPORTUNITIES)
    threshold = _score(minimum_score, "minimum_score") or 0.0
    include_all = bool(include_ineligible)
    # A never-evaluated thesis has a NULL opportunity score.  It is never
    # rank-eligible and, by default, never listed; the explicit bounded
    # opt-in reveals it (marked ineligible) so operators can see the gap.
    # NULL is never treated as a zero observation: the eligibility gate
    # ``opportunity_score > 0`` fails for NULL and the ORDER BY places
    # NULL rows last.
    if include_all:
        conditions = [
            "(t.opportunity_score >= :minimum_score "
            "OR t.opportunity_score IS NULL)"
        ]
    else:
        conditions = ["t.opportunity_score >= :minimum_score"]
    params: dict[str, Any] = {
        "minimum_score": threshold,
        "limit": min(_MAX_RANKED_OPPORTUNITIES, bounded * 4),
    }
    if group_id is not None:
        conditions.append(
            """EXISTS (
                   SELECT 1 FROM investment_thesis_group_members m
                   WHERE m.group_id = CAST(:group_id AS UUID)
                     AND m.thesis_id = t.id
                     AND m.removed_at IS NULL
               )"""
        )
        params["group_id"] = _uuid(group_id, "group_id")
    if not include_all:
        conditions.append("eligibility.eligible")
    rows = _rows(
        session.execute(
            text(
                """WITH base_eligibility AS (
                       SELECT thesis_id, symbol_key, company_key, direction_key,
                              horizon_key, status_ok, score_ok, scenarios_ok,
                              risks_ok, evidence_ok, falsification_ok,
                              actionability_ok,
                              status_ok AND score_ok AND scenarios_ok AND risks_ok
                                  AND evidence_ok AND falsification_ok
                                  AND actionability_ok AS base_eligible
                       FROM (
                           SELECT t.id AS thesis_id,
                                  LOWER(BTRIM(COALESCE(t.symbol, ''))) AS symbol_key,
                                  LOWER(BTRIM(COALESCE(t.company, ''))) AS company_key,
                                  LOWER(BTRIM(COALESCE(t.direction, '')))
                                      AS direction_key,
                                  LOWER(BTRIM(COALESCE(t.horizon, ''))) AS horizon_key,
                                  t.status IN ('candidate', 'active') AS status_ok,
                                  t.opportunity_score > 0 AS score_ok,
                                  EXISTS (
                                      SELECT 1
                                      FROM (
                                          SELECT s.name, s.probability,
                                                 s.expected_return, s.description
                                          FROM investment_thesis_scenarios s
                                          WHERE s.thesis_id = t.id
                                            AND s.superseded_at IS NULL
                                            AND s.name IN ('bull', 'base', 'bear')
                                      ) legs
                                      HAVING COUNT(*) = 3
                                         AND COUNT(*) FILTER (
                                             WHERE legs.probability IS NOT NULL
                                               AND legs.expected_return IS NOT NULL
                                               AND BTRIM(COALESCE(legs.description, ''))
                                                   <> ''
                                         ) = 3
                                         AND ABS(SUM(legs.probability) - 1.0) < 1e-9
                                  ) AS scenarios_ok,
                                  EXISTS (
                                      SELECT 1 FROM investment_risks r
                                      WHERE r.thesis_id = t.id
                                        AND BTRIM(r.description) <> ''
                                  ) AS risks_ok,
                                  EXISTS (
                                      SELECT 1 FROM investment_thesis_evidence e
                                      WHERE e.thesis_id = t.id
                                        AND e.relationship = 'supports'
                                        AND BTRIM(COALESCE(e.excerpt, '')) <> ''
                                        AND e.quality_score > 0
                                        AND e.entailment_score > 0
                                  ) AS evidence_ok,
                                  EXISTS (
                                      SELECT 1
                                      FROM (
                                          SELECT DISTINCT ON (f.thesis_id)
                                                 f.status
                                          FROM investment_thesis_falsification_runs f
                                          WHERE f.thesis_id = t.id
                                          ORDER BY f.thesis_id, f.started_at DESC,
                                                   f.run_key
                                      ) latest_run
                                      WHERE latest_run.status = 'not_falsified'
                                  ) AS falsification_ok,
                                  (
                                      BTRIM(COALESCE(t.trend_context, '')) <> ''
                                      AND BTRIM(COALESCE(t.valuation_context, '')) <> ''
                                      AND BTRIM(COALESCE(t.sentiment_context, '')) <> ''
                                      AND t.citation_map ?& ARRAY[
                                          'claim', 'consensus', 'variant_perception',
                                          'mechanism', 'catalyst', 'trend',
                                          'valuation', 'sentiment'
                                      ]::TEXT[]
                                      AND NOT EXISTS (
                                          SELECT 1
                                          FROM JSONB_EACH(t.citation_map) field
                                          WHERE JSONB_TYPEOF(field.value) <> 'array'
                                             OR JSONB_ARRAY_LENGTH(
                                                 CASE
                                                   WHEN JSONB_TYPEOF(field.value) = 'array'
                                                     THEN field.value
                                                   ELSE '[]'::JSONB
                                                 END
                                             ) = 0
                                      )
                                      AND NOT EXISTS (
                                          SELECT 1
                                          FROM JSONB_EACH(t.citation_map) field
                                          CROSS JOIN LATERAL JSONB_ARRAY_ELEMENTS_TEXT(
                                              CASE
                                                WHEN JSONB_TYPEOF(field.value) = 'array'
                                                  THEN field.value
                                                ELSE '[]'::JSONB
                                              END
                                          ) cited(ref)
                                          WHERE NOT EXISTS (
                                              SELECT 1
                                              FROM investment_thesis_evidence e
                                              WHERE e.thesis_id = t.id
                                                AND e.relationship = 'supports'
                                                AND BTRIM(COALESCE(e.excerpt, '')) <> ''
                                                AND e.quality_score > 0
                                                AND e.entailment_score > 0
                                                AND e.evidence_type || ':' ||
                                                    e.evidence_id = cited.ref
                                          )
                                      )
                                      AND (
                                          SELECT COUNT(DISTINCT LOWER(e.source_family))
                                          FROM investment_thesis_evidence e
                                          WHERE e.thesis_id = t.id
                                            AND e.relationship = 'supports'
                                            AND BTRIM(COALESCE(e.excerpt, '')) <> ''
                                            AND e.quality_score > 0
                                            AND e.entailment_score > 0
                                            AND e.evidence_type || ':' ||
                                                e.evidence_id IN (
                                                    SELECT cited.ref
                                                    FROM JSONB_EACH(t.citation_map) field
                                                    CROSS JOIN LATERAL
                                                        JSONB_ARRAY_ELEMENTS_TEXT(
                                                            CASE
                                                              WHEN JSONB_TYPEOF(field.value) = 'array'
                                                                THEN field.value
                                                              ELSE '[]'::JSONB
                                                            END
                                                        ) cited(ref)
                                                )
                                      ) >= 3
                                  ) AS actionability_ok
                           FROM investment_theses t
                       ) gates
                   ),
                   eligibility AS (
                       SELECT candidate.thesis_id, candidate.status_ok,
                              candidate.score_ok, candidate.scenarios_ok,
                              candidate.risks_ok, candidate.evidence_ok,
                              candidate.falsification_ok,
                              candidate.actionability_ok,
                              opposition.opposition_ok,
                              candidate.base_eligible
                                  AND opposition.opposition_ok AS eligible
                       FROM base_eligibility candidate
                       CROSS JOIN LATERAL (
                           SELECT EXISTS (
                               SELECT 1
                               FROM base_eligibility opponent
                               WHERE opponent.thesis_id <> candidate.thesis_id
                                 AND opponent.base_eligible
                                 AND opponent.horizon_key = candidate.horizon_key
                                 AND (
                                     (
                                         candidate.symbol_key <> ''
                                         AND opponent.symbol_key
                                             = candidate.symbol_key
                                     )
                                     OR (
                                         candidate.symbol_key = ''
                                         AND opponent.symbol_key = ''
                                         AND candidate.company_key <> ''
                                         AND opponent.company_key
                                             = candidate.company_key
                                     )
                                 )
                                 AND (
                                     (
                                         candidate.direction_key = 'long'
                                         AND opponent.direction_key = 'short'
                                     )
                                     OR (
                                         candidate.direction_key = 'short'
                                         AND opponent.direction_key = 'long'
                                     )
                                 )
                           ) AS opposition_ok
                       ) opposition
                   )
                   SELECT t.id, t.theme_id, t.company, t.symbol, t.claim,
                          t.direction, t.mechanism, t.horizon, t.status,
                          t.origin, t.trend_context, t.valuation_context,
                          t.sentiment_context, t.citation_map,
                          t.evidence_strength, t.contradiction_strength,
                          t.neglect_score, t.catalyst_score, t.confidence_score,
                          t.expected_value, t.expected_shortfall,
                          t.opportunity_score, t.last_evaluated_at,
                          t.last_evidence_at, g.group_id, g.group_name,
                          eligibility.status_ok AS eligibility_status,
                          eligibility.score_ok AS eligibility_score,
                          eligibility.scenarios_ok AS eligibility_scenarios,
                          eligibility.risks_ok AS eligibility_risks,
                          eligibility.evidence_ok AS eligibility_evidence,
                          eligibility.falsification_ok
                              AS eligibility_falsification,
                          eligibility.actionability_ok
                              AS eligibility_actionability,
                          eligibility.opposition_ok
                              AS eligibility_opposition,
                          eligibility.eligible
                   FROM investment_theses t
                   JOIN eligibility ON eligibility.thesis_id = t.id
                   LEFT JOIN LATERAL (
                       SELECT m.group_id, gr.name AS group_name
                       FROM investment_thesis_group_members m
                       JOIN investment_thesis_groups gr ON gr.id = m.group_id
                       WHERE m.thesis_id = t.id AND m.removed_at IS NULL
                       ORDER BY m.added_at, m.group_id
                       LIMIT 1
                   ) g ON TRUE
                   WHERE """
                + " AND ".join(conditions)
                # Every DESC column pins NULLS LAST explicitly: an unknown
                # metric (NULL) must rank after every measured value,
                # including zero, and can never be a favorable zero rank.
                + """ ORDER BY (t.opportunity_score > 0) DESC NULLS LAST,
                          eligibility.eligible DESC NULLS LAST,
                          t.expected_value DESC NULLS LAST,
                          t.opportunity_score DESC NULLS LAST,
                          t.confidence_score DESC NULLS LAST,
                          t.catalyst_score DESC NULLS LAST,
                          t.neglect_score DESC NULLS LAST,
                          t.last_evaluated_at DESC NULLS LAST, t.id
                   LIMIT :limit"""
            ),
            params,
        )
    )
    canonical: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        subject = str(row.get("symbol") or row.get("company") or row.get("id")).strip()
        key = (
            subject.casefold(),
            str(row.get("direction") or "").strip().casefold(),
            str(row.get("horizon") or "").strip().casefold(),
        )
        flags = tuple(
            bool(row.get(f"eligibility_{gate}")) for gate in _ELIGIBILITY_GATES
        )
        if not include_all and not all(flags):
            continue
        if key in seen:
            continue
        seen.add(key)
        row = dict(row)
        for gate in _ELIGIBILITY_GATES:
            row.pop(f"eligibility_{gate}", None)
        row["eligible"] = all(flags)
        row["blockers"] = [
            gate for gate, ok in zip(_ELIGIBILITY_GATES, flags, strict=True) if not ok
        ]
        canonical.append(row)
        if len(canonical) >= bounded:
            break
    return canonical


def list_thesis_groups(
    session: Any,
    *,
    limit: int = 50,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """List thesis groups with bounded member/score aggregates.

    Each row carries the active member count, direction counts among active
    members, the maximum opportunity and contradiction scores, and the
    latest evaluation timestamp of any active member.  The row set is
    bounded by ``limit`` and ordered deterministically (name, then id);
    ``status`` filters on the group status when given and must be one of
    ``GROUP_STATUSES``.
    """
    bounded = _bounded(limit, 50, _MAX_RANKED_OPPORTUNITIES)
    if status is not None and status not in GROUP_STATUSES:
        raise ValueError(f"unsupported group status:{str(status)[:32]}")
    return _rows(
        session.execute(
            text(
                """SELECT g.id, g.name, g.description, g.status,
                          g.created_at, g.updated_at,
                          COUNT(m.thesis_id) AS active_members,
                          COUNT(*) FILTER (WHERE t.direction = 'long')
                              AS long_count,
                          COUNT(*) FILTER (WHERE t.direction = 'short')
                              AS short_count,
                          COUNT(*) FILTER (WHERE t.direction = 'neutral')
                              AS neutral_count,
                          MAX(t.opportunity_score) AS max_opportunity,
                          MAX(t.contradiction_strength) AS max_contradiction,
                          MAX(t.last_evaluated_at) AS last_evaluation
                   FROM investment_thesis_groups g
                   LEFT JOIN investment_thesis_group_members m
                          ON m.group_id = g.id AND m.removed_at IS NULL
                   LEFT JOIN investment_theses t ON t.id = m.thesis_id
                   WHERE (:status IS NULL OR g.status = :status)
                   GROUP BY g.id, g.name, g.description, g.status,
                            g.created_at, g.updated_at
                   ORDER BY g.name, g.id
                   LIMIT :limit"""
            ),
            {"status": status, "limit": bounded},
        )
    )


def load_thesis_detail(
    session: Any,
    thesis_id: str,
    *,
    limit: int = _MAX_DETAIL_ROWS,
) -> dict[str, Any] | None:
    """Load one thesis with its full bounded desk state.

    Returns the core row plus bounded, deterministically ordered children:
    version history, active scenarios (including ``expected_return``),
    evidence with provenance and scores, catalysts, risks, active forecasts,
    forecast outcomes, opportunity snapshot history, falsification runs,
    active group memberships, active position links, event playbook
    versions, and the playbook match ledger joined to market events.  A
    missing thesis returns None; every child query is bounded so the
    payload stays stable.
    """
    thesis_id = _uuid(thesis_id, "thesis_id")
    bounded = _bounded(limit, _MAX_DETAIL_ROWS, _MAX_DETAIL_ROWS)
    thesis = _first(
        session.execute(
            text(
                """SELECT id, theme_id, company, symbol, claim,
                          variant_perception, status, horizon, direction,
                          mechanism, catalyst_summary, confidence, origin,
                          trend_context, valuation_context, sentiment_context,
                          citation_map, canonical_key, evidence_strength,
                          contradiction_strength, neglect_score,
                          catalyst_score, confidence_score, expected_value,
                          expected_shortfall, opportunity_score,
                          last_evaluated_at, last_evidence_at, created_at,
                          updated_at
                   FROM investment_theses
                   WHERE id = CAST(:id AS UUID) LIMIT 1"""
            ),
            {"id": thesis_id},
        )
    )
    if thesis is None:
        return None
    versions = _rows(
        session.execute(
            text(
                """SELECT version, claim, variant_perception, confidence,
                          trend_context, valuation_context, sentiment_context,
                          citation_map, rationale, changed_by, created_at
                   FROM investment_thesis_versions
                   WHERE thesis_id = CAST(:id AS UUID)
                   ORDER BY version DESC
                   LIMIT :limit"""
            ),
            {"id": thesis_id, "limit": bounded},
        )
    )
    scenarios = _rows(
        session.execute(
            text(
                """SELECT id, name, description, probability,
                          expected_return, is_base_case, version, created_at
                   FROM investment_thesis_scenarios
                   WHERE thesis_id = CAST(:id AS UUID)
                     AND superseded_at IS NULL
                   ORDER BY is_base_case DESC, created_at, name
                   LIMIT :limit"""
            ),
            {"id": thesis_id, "limit": bounded},
        )
    )
    evidence = _rows(
        session.execute(
            text(
                """SELECT evidence_type, evidence_id, relationship, excerpt,
                          source_family, origin_key, independence_key,
                          evidence_fingerprint, source_timestamp,
                          available_at, quality_score, entailment_score,
                          freshness_score, effective_weight, created_at
                   FROM investment_thesis_evidence
                   WHERE thesis_id = CAST(:id AS UUID)
                   ORDER BY created_at, evidence_type, evidence_id
                   LIMIT :limit"""
            ),
            {"id": thesis_id, "limit": bounded},
        )
    )
    catalysts = _rows(
        session.execute(
            text(
                """SELECT id, description, expected_at, state, created_at,
                          updated_at
                   FROM investment_catalysts
                   WHERE thesis_id = CAST(:id AS UUID)
                   ORDER BY expected_at NULLS LAST, created_at, id
                   LIMIT :limit"""
            ),
            {"id": thesis_id, "limit": bounded},
        )
    )
    risks = _rows(
        session.execute(
            text(
                """SELECT id, description, kind, severity, created_at,
                          updated_at
                   FROM investment_risks
                   WHERE thesis_id = CAST(:id AS UUID)
                   ORDER BY created_at, id
                   LIMIT :limit"""
            ),
            {"id": thesis_id, "limit": bounded},
        )
    )
    forecasts = _rows(
        session.execute(
            text(
                """SELECT id, scenario_id, forecast_key, forecast_type,
                          direction, target_value, target_date, as_of,
                          version, created_at
                   FROM investment_thesis_forecasts
                   WHERE thesis_id = CAST(:id AS UUID)
                     AND superseded_at IS NULL
                   ORDER BY as_of DESC, forecast_key
                   LIMIT :limit"""
            ),
            {"id": thesis_id, "limit": bounded},
        )
    )
    outcomes = _rows(
        session.execute(
            text(
                """SELECT o.id, o.forecast_id, f.forecast_key, o.status,
                          o.actual_value, o.measured_at, o.notes, o.created_at
                   FROM investment_forecast_outcomes o
                   JOIN investment_thesis_forecasts f ON f.id = o.forecast_id
                   WHERE f.thesis_id = CAST(:id AS UUID)
                   ORDER BY o.measured_at DESC, f.forecast_key
                   LIMIT :limit"""
            ),
            {"id": thesis_id, "limit": bounded},
        )
    )
    snapshots = _rows(
        session.execute(
            text(
                """SELECT id, snapshot_key, captured_at, input_fingerprint,
                          opportunity_score, expected_value,
                          expected_shortfall, confidence_score, neglect_score,
                          catalyst_score, evidence_strength,
                          contradiction_strength, created_at
                   FROM investment_opportunity_snapshots
                   WHERE thesis_id = CAST(:id AS UUID)
                   ORDER BY captured_at DESC, snapshot_key
                   LIMIT :limit"""
            ),
            {"id": thesis_id, "limit": bounded},
        )
    )
    falsification_runs = _rows(
        session.execute(
            text(
                """SELECT id, run_key, status, started_at, completed_at,
                          findings, created_at
                   FROM investment_thesis_falsification_runs
                   WHERE thesis_id = CAST(:id AS UUID)
                   ORDER BY started_at DESC, run_key
                   LIMIT :limit"""
            ),
            {"id": thesis_id, "limit": bounded},
        )
    )
    groups = _rows(
        session.execute(
            text(
                """SELECT g.id, g.name, g.status, gm.added_at, gm.note
                   FROM investment_thesis_group_members gm
                   JOIN investment_thesis_groups g ON g.id = gm.group_id
                   WHERE gm.thesis_id = CAST(:id AS UUID)
                     AND gm.removed_at IS NULL
                   ORDER BY gm.added_at, g.id
                   LIMIT :limit"""
            ),
            {"id": thesis_id, "limit": bounded},
        )
    )
    positions = _rows(
        session.execute(
            text(
                """SELECT position_id, link_type, created_at
                   FROM position_thesis_links
                   WHERE thesis_id = CAST(:id AS UUID)
                     AND removed_at IS NULL
                   ORDER BY created_at, position_id
                   LIMIT :limit"""
            ),
            {"id": thesis_id, "limit": bounded},
        )
    )
    playbooks = _rows(
        session.execute(
            text(
                """SELECT id, playbook_key AS key, version, thesis_version,
                          catalyst, horizon, expected_at, event_types,
                          trigger_conditions, confirmation_conditions,
                          invalidation_conditions, bull_scenario,
                          base_scenario, bear_scenario, cited_evidence_refs,
                          superseded_at, created_at
                   FROM investment_thesis_event_playbooks
                   WHERE thesis_id = CAST(:id AS UUID)
                   ORDER BY created_at DESC, playbook_key, version DESC
                   LIMIT :limit"""
            ),
            {"id": thesis_id, "limit": bounded},
        )
    )
    playbook_matches = _rows(
        session.execute(
            text(
                """SELECT m.id, m.playbook_id, m.market_event_id AS event_id,
                          m.match_kind AS kind, m.evidence_refs,
                          m.assessment, m.created_at, e.event_type, e.source,
                          e.observed_at
                   FROM investment_thesis_event_matches m
                   JOIN investment_thesis_event_playbooks p
                        ON p.id = m.playbook_id
                   JOIN market_events e ON e.id = m.market_event_id
                   WHERE p.thesis_id = CAST(:id AS UUID)
                   ORDER BY m.created_at DESC, m.id
                   LIMIT :limit"""
            ),
            {"id": thesis_id, "limit": bounded},
        )
    )
    return {
        "thesis": thesis,
        "versions": versions,
        "scenarios": scenarios,
        "evidence": evidence,
        "catalysts": catalysts,
        "risks": risks,
        "forecasts": forecasts,
        "outcomes": outcomes,
        "opportunity_snapshots": snapshots,
        "falsification_runs": falsification_runs,
        "groups": groups,
        "positions": positions,
        "playbooks": playbooks,
        "playbook_matches": playbook_matches,
    }


def _collection_error_class(status: Any) -> str | None:
    """Safe bounded error category from a persisted collection status.

    The taxonomy is a fixed mapping over status values: raw error text is
    never returned, so no provider or database diagnostics can leak.
    """
    normalized = str(status or "").strip()
    if normalized in {
        "success",
        "accepted",
        "queued",
        "leased",
        "running",
        "suppressed_duplicate",
    }:
        return None
    if normalized == "partial":
        return "partial"
    if normalized == "rate_limited":
        return "rate_limited"
    if normalized:
        return "error"
    return None


def thesis_desk_status(
    session: Any,
    *,
    limit: int = 20,
) -> dict[str, Any]:
    """Bounded desk status: counts, calibration, ingestion, and jobs.

    Reports thesis/group totals and status breakdowns, ranked and
    position-linked thesis counts, evidence totals per relationship, active
    and matured forecast counts with terminal outcome totals and the
    empirical hit rate (null until at least one terminal hit/miss exists —
    never a fabricated zero, and never a profitability claim), today's
    bounded autonomy model cost (null when nothing is known, never an
    invented zero), latest
    evaluation and falsification timestamps, bounded ingestion readiness
    for the fixed source allowlist (latest collection state plus per-table
    data freshness, never raw error text), and the latest bounded
    ``analysis_jobs`` rows for ``thesis_autonomy_run``.  ``available``
    reflects whether the migration 049 desk schema is present so callers
    can distinguish an empty desk from an un-migrated one; every query is
    bounded and deterministically ordered, and missing data is reported
    through explicit ``never_run``/empty/unavailable states.
    """
    bounded = _bounded(limit, 20, _MAX_STATUS_JOBS)
    schema = _first(
        session.execute(
            text("SELECT to_regclass(:name) AS present"),
            {"name": "investment_thesis_groups"},
        )
    )
    unavailable_sources = {
        source: {
            "collection": {
                "status": "unavailable",
                "finished_at": None,
                "records_written": 0,
                "error_class": None,
            },
            "data": {
                "available": False,
                "latest_timestamp": None,
                "acquired_at": None,
            },
            **({"transcript_states": {}} if source == "issuer_transcripts" else {}),
        }
        for source in _INGESTION_SOURCES
    }
    if schema is None or schema.get("present") is None:
        return {
            "available": False,
            "theses": {"total": 0, "by_status": {}},
            "groups": {"total": 0, "by_status": {}},
            "ranked_theses": 0,
            "linked_theses": 0,
            "evidence": {"total": 0, "by_relationship": {}},
            "forecasts": {"active": 0, "matured": 0},
            "outcomes": {"hit": 0, "miss": 0, "inconclusive": 0},
            "hit_rate": None,
            "calibration": {
                "resolved_with_probability": 0,
                "brier_score": None,
                "bins": [],
            },
            "model_cost": {
                "attempts": 0,
                "known_cost_attempts": 0,
                "unknown_cost_attempts": 0,
                "today_usd": None,
                "latest_attempt_at": None,
            },
            "latest_evaluation_at": None,
            "latest_falsification_at": None,
            "sources": unavailable_sources,
            "autonomy_jobs": [],
        }
    thesis_total = _first(
        session.execute(text("SELECT COUNT(*) AS total FROM investment_theses"))
    )
    thesis_statuses = _rows(
        session.execute(
            text(
                "SELECT status, COUNT(*) AS count FROM investment_theses "
                "GROUP BY status ORDER BY status"
            )
        )
    )
    group_total = _first(
        session.execute(text("SELECT COUNT(*) AS total FROM investment_thesis_groups"))
    )
    group_statuses = _rows(
        session.execute(
            text(
                "SELECT status, COUNT(*) AS count FROM investment_thesis_groups "
                "GROUP BY status ORDER BY status"
            )
        )
    )
    ranked = _first(
        session.execute(
            text(
                "SELECT COUNT(*) AS total FROM investment_theses "
                "WHERE opportunity_score > 0"
            )
        )
    )
    linked = _first(
        session.execute(
            text(
                "SELECT COUNT(DISTINCT thesis_id) AS total "
                "FROM position_thesis_links WHERE removed_at IS NULL"
            )
        )
    )
    evidence_total = _first(
        session.execute(
            text("SELECT COUNT(*) AS total FROM investment_thesis_evidence")
        )
    )
    evidence_relationships = _rows(
        session.execute(
            text(
                "SELECT relationship, COUNT(*) AS count "
                "FROM investment_thesis_evidence "
                "GROUP BY relationship ORDER BY relationship"
            )
        )
    )
    active_forecasts = _first(
        session.execute(
            text(
                """SELECT COUNT(*) AS total
                   FROM investment_thesis_forecasts f
                   WHERE f.superseded_at IS NULL
                     AND NOT EXISTS (
                         SELECT 1 FROM investment_forecast_outcomes o
                         WHERE o.forecast_id = f.id
                     )"""
            )
        )
    )
    matured_forecasts = _first(
        session.execute(
            text(
                """SELECT COUNT(*) AS total
                   FROM investment_thesis_forecasts f
                   WHERE EXISTS (
                       SELECT 1 FROM investment_forecast_outcomes o
                       WHERE o.forecast_id = f.id
                   )"""
            )
        )
    )
    outcome_counts = _rows(
        session.execute(
            text(
                "SELECT status, COUNT(*) AS count FROM investment_forecast_outcomes "
                "GROUP BY status ORDER BY status"
            )
        )
    )
    outcome_by_status = {
        str(row["status"]): int(row["count"]) for row in outcome_counts
    }
    hits = outcome_by_status.get("hit", 0)
    misses = outcome_by_status.get("miss", 0)
    resolved = hits + misses
    # Empirical hit rate only when a terminal hit/miss exists: null (never
    # zero) while there is nothing to measure.
    hit_rate = hits / resolved if resolved > 0 else None
    calibration_rows = _rows(
        session.execute(
            text(
                """
                WITH resolved AS (
                    SELECT f.thesis_id, f.as_of, f.target_date, s.name,
                           s.probability, f.target_value, o.actual_value
                    FROM investment_forecast_outcomes o
                    JOIN investment_thesis_forecasts f ON f.id = o.forecast_id
                    JOIN investment_thesis_scenarios s ON s.id = f.scenario_id
                    WHERE o.status IN ('hit', 'miss')
                      AND s.name IN ('bull', 'base', 'bear')
                      AND s.probability IS NOT NULL
                      AND s.probability BETWEEN 0 AND 1
                      AND f.target_value IS NOT NULL
                      AND o.actual_value IS NOT NULL
                ),
                complete_sets AS (
                    SELECT thesis_id, as_of, target_date,
                           MIN(actual_value) AS actual_value
                    FROM resolved
                    GROUP BY thesis_id, as_of, target_date
                    HAVING COUNT(*) = 3
                       AND COUNT(DISTINCT name) = 3
                       AND ABS(SUM(probability) - 1.0) < 1e-9
                       AND ABS(MAX(actual_value) - MIN(actual_value)) < 1e-9
                ),
                ranked AS (
                    SELECT r.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY r.thesis_id, r.as_of, r.target_date
                               ORDER BY ABS(r.target_value - sets.actual_value),
                                        r.name
                           ) AS realized_rank
                    FROM resolved r
                    JOIN complete_sets sets
                      ON sets.thesis_id = r.thesis_id
                     AND sets.as_of = r.as_of
                     AND sets.target_date = r.target_date
                ),
                scored AS (
                    SELECT thesis_id, as_of, target_date, probability,
                           CASE WHEN realized_rank = 1 THEN 1.0 ELSE 0.0 END
                               AS actual
                    FROM ranked
                ),
                set_scores AS (
                    SELECT thesis_id, as_of, target_date,
                           SUM(POWER(probability - actual, 2)) AS brier_score
                    FROM scored
                    GROUP BY thesis_id, as_of, target_date
                ),
                binned AS (
                    SELECT LEAST(
                               4,
                               GREATEST(0, FLOOR(probability * 5)::INTEGER)
                           ) AS bucket,
                           COUNT(*) AS count,
                           AVG(probability) AS mean_probability,
                           AVG(actual) AS observed_hit_rate,
                           AVG(POWER(probability - actual, 2)) AS brier_score
                    FROM scored
                    GROUP BY bucket
                )
                SELECT bucket, count, mean_probability, observed_hit_rate,
                       brier_score,
                       (SELECT COUNT(*) FROM set_scores) AS set_count,
                       (SELECT AVG(brier_score) FROM set_scores) AS overall_brier
                FROM binned
                ORDER BY bucket
                """
            )
        )
    )
    calibrated_count = (
        int(calibration_rows[0].get("set_count") or 0) if calibration_rows else 0
    )
    calibration_brier = (
        float(calibration_rows[0]["overall_brier"])
        if calibration_rows and calibration_rows[0].get("overall_brier") is not None
        else None
    )
    calibration_bins = [
        {
            "lower": int(row["bucket"]) / 5.0,
            "upper": (int(row["bucket"]) + 1) / 5.0,
            "count": int(row["count"]),
            "mean_probability": float(row["mean_probability"]),
            "observed_hit_rate": float(row["observed_hit_rate"]),
            "brier_score": float(row["brier_score"]),
        }
        for row in calibration_rows
    ]
    latest_evaluation = _first(
        session.execute(
            text("SELECT MAX(last_evaluated_at) AS latest FROM investment_theses")
        )
    )
    latest_falsification = _first(
        session.execute(
            text(
                "SELECT MAX(started_at) AS latest "
                "FROM investment_thesis_falsification_runs"
            )
        )
    )
    today = datetime.now(UTC).date()
    today_start = datetime.combine(today, time.min, tzinfo=UTC)
    model_cost = _first(
        session.execute(
            text(
                """SELECT COUNT(*) AS attempts,
                          COUNT(cost_usd) AS known_cost_attempts,
                          COUNT(*) - COUNT(cost_usd) AS unknown_cost_attempts,
                          SUM(cost_usd) AS today_usd,
                          MAX(created_at) AS latest_attempt_at
                   FROM generation_attempts
                   WHERE processor = :processor
                     AND created_at >= :today_start
                     AND created_at < :tomorrow_start"""
            ),
            {
                "processor": "thesis_autonomy",
                "today_start": today_start,
                "tomorrow_start": today_start + timedelta(days=1),
            },
        )
    )
    collection_rows = _rows(
        session.execute(
            text(
                """WITH latest_collections AS (
                       SELECT DISTINCT ON (collector) collector, status,
                              completed_at, records_written
                       FROM collection_log
                       WHERE collector IN ("""
                + ", ".join(f":s{index}" for index in range(len(_INGESTION_SOURCES)))
                + """)
                       ORDER BY collector, started_at DESC
                   ),
                   latest_filings AS (
                       SELECT 'filings' AS collector,
                              CASE
                                WHEN completed_at IS NULL THEN status
                                WHEN COALESCE(result_status, status) IN
                                     ('completed', 'success') THEN 'success'
                                WHEN COALESCE(result_status, status) = 'partial'
                                     THEN 'partial'
                                ELSE 'failed'
                              END AS status,
                              completed_at,
                              CASE
                                WHEN JSONB_TYPEOF(summary->'ingested') = 'number'
                                  THEN (summary->>'ingested')::INTEGER
                                ELSE 0
                              END AS records_written
                       FROM cycle_runs
                       WHERE run_kind = 'filings'
                         AND requested_component = 'investment_filings'
                       ORDER BY accepted_at DESC
                       LIMIT 1
                   )
                   SELECT collector, status, completed_at, records_written
                   FROM latest_collections
                   UNION ALL
                   SELECT collector, status, completed_at, records_written
                   FROM latest_filings"""
            ),
            {f"s{index}": source for index, source in enumerate(_INGESTION_SOURCES)},
        )
    )
    collection_by_source = {str(row["collector"]): row for row in collection_rows}
    sources: dict[str, Any] = {}
    for source in _INGESTION_SOURCES:
        table, source_ts, availability_ts = _SOURCE_DATA_TABLES[source]
        if table in _SOURCE_FILTERED_TABLES:
            data_sql = text(
                f"SELECT MAX({source_ts}) AS latest_timestamp, "
                f"MAX({availability_ts}) AS acquired_at "
                f"FROM {table} WHERE source = :source"
            )
            data_params: dict[str, Any] = {"source": source}
        else:
            data_sql = text(
                f"SELECT MAX({source_ts}) AS latest_timestamp, "
                f"MAX({availability_ts}) AS acquired_at FROM {table}"
            )
            data_params = {}
        data_row = _first(session.execute(data_sql, data_params)) or {}
        entry: dict[str, Any] = {
            "collection": {
                "status": "never_run",
                "finished_at": None,
                "records_written": 0,
                "error_class": None,
            },
            "data": {
                "available": data_row.get("latest_timestamp") is not None,
                "latest_timestamp": data_row.get("latest_timestamp"),
                "acquired_at": data_row.get("acquired_at"),
            },
        }
        latest = collection_by_source.get(source)
        if latest is not None:
            entry["collection"] = {
                "status": str(latest["status"]),
                "finished_at": latest.get("completed_at"),
                "records_written": int(latest.get("records_written") or 0),
                "error_class": _collection_error_class(latest["status"]),
            }
        if source == "issuer_transcripts":
            transcript_rows = _rows(
                session.execute(
                    text(
                        """SELECT COALESCE(metadata->>'state', 'available')
                                  AS state, COUNT(*) AS count
                           FROM source_documents
                           WHERE source = 'issuer_transcripts'
                           GROUP BY state ORDER BY state LIMIT 20"""
                    )
                )
            )
            entry["transcript_states"] = {
                str(row["state"]): int(row["count"]) for row in transcript_rows
            }
        sources[source] = entry
    autonomy_jobs = _rows(
        session.execute(
            text(
                """SELECT id, job_type, state, priority, dedupe_key,
                          input_fingerprint, not_before, attempt_count,
                          max_attempts, correlation_id, created_at,
                          started_at, completed_at, result_ref, payload
                   FROM analysis_jobs
                   WHERE job_type = :job_type
                   ORDER BY created_at DESC, id DESC
                   LIMIT :limit"""
            ),
            {"job_type": _AUTONOMY_JOB_TYPE, "limit": bounded},
        )
    )
    return {
        "available": True,
        "theses": {
            "total": int(thesis_total["total"]),
            "by_status": {
                str(row["status"]): int(row["count"]) for row in thesis_statuses
            },
        },
        "groups": {
            "total": int(group_total["total"]),
            "by_status": {
                str(row["status"]): int(row["count"]) for row in group_statuses
            },
        },
        "ranked_theses": int(ranked["total"]),
        "linked_theses": int(linked["total"]),
        "evidence": {
            "total": int(evidence_total["total"]),
            "by_relationship": {
                str(row["relationship"]): int(row["count"])
                for row in evidence_relationships
            },
        },
        "forecasts": {
            "active": int(active_forecasts["total"]),
            "matured": int(matured_forecasts["total"]),
        },
        "outcomes": {
            "hit": outcome_by_status.get("hit", 0),
            "miss": outcome_by_status.get("miss", 0),
            "inconclusive": outcome_by_status.get("inconclusive", 0),
        },
        "hit_rate": hit_rate,
        "calibration": {
            "resolved_with_probability": calibrated_count,
            "brier_score": calibration_brier,
            "bins": calibration_bins,
        },
        "model_cost": {
            "attempts": int(model_cost["attempts"]),
            "known_cost_attempts": int(model_cost["known_cost_attempts"]),
            "unknown_cost_attempts": int(model_cost["unknown_cost_attempts"]),
            "today_usd": model_cost.get("today_usd"),
            "latest_attempt_at": model_cost.get("latest_attempt_at"),
        },
        "latest_evaluation_at": latest_evaluation.get("latest"),
        "latest_falsification_at": latest_falsification.get("latest"),
        "sources": sources,
        "autonomy_jobs": [
            {**job, "failed": str(job.get("state")) in _JOB_FAILED_STATES}
            for job in autonomy_jobs
        ],
    }


def load_group_tournament(
    session: Any,
    group_id: str,
    *,
    limit: int = _MAX_TOURNAMENT_THESES,
) -> dict[str, Any] | None:
    """Load one group's competing theses with their desk state.

    Returns the group record plus active members, each member's current
    thesis row, evidence counts, latest version, active scenarios and active
    forecasts, plus the group's forecast outcomes and falsification runs.
    All children are bounded; a missing group returns None.
    """
    group_id = _uuid(group_id, "group_id")
    bounded = _bounded(limit, _MAX_TOURNAMENT_THESES, _MAX_TOURNAMENT_THESES)
    group = _first(
        session.execute(
            text(
                """SELECT id, name, description, status, created_at, updated_at
                   FROM investment_thesis_groups
                   WHERE id = CAST(:id AS UUID) LIMIT 1"""
            ),
            {"id": group_id},
        )
    )
    if group is None:
        return None
    members = _rows(
        session.execute(
            text(
                """SELECT thesis_id, added_at, note
                   FROM investment_thesis_group_members
                   WHERE group_id = CAST(:group_id AS UUID)
                     AND removed_at IS NULL
                   ORDER BY added_at, thesis_id
                   LIMIT :limit"""
            ),
            {"group_id": group_id, "limit": bounded},
        )
    )
    theses: list[dict[str, Any]] = []
    thesis_ids: list[str] = []
    for member in members:
        thesis_id = str(member["thesis_id"])
        thesis_ids.append(thesis_id)
        thesis = _first(
            session.execute(
                text(
                    """SELECT id, theme_id, company, symbol, claim,
                              variant_perception, status, horizon, direction,
                              mechanism, catalyst_summary, confidence, origin,
                              trend_context, valuation_context, sentiment_context,
                              citation_map, canonical_key, evidence_strength,
                              contradiction_strength, neglect_score,
                              catalyst_score, confidence_score, expected_value,
                              expected_shortfall, opportunity_score,
                              last_evaluated_at, last_evidence_at, created_at,
                              updated_at
                       FROM investment_theses
                       WHERE id = CAST(:id AS UUID) LIMIT 1"""
                ),
                {"id": thesis_id},
            )
        )
        if thesis is None:
            continue
        thesis["evidence_counts"] = _rows(
            session.execute(
                text(
                    """SELECT relationship, COUNT(*) AS count
                       FROM investment_thesis_evidence
                       WHERE thesis_id = CAST(:id AS UUID)
                       GROUP BY relationship ORDER BY relationship LIMIT 10"""
                ),
                {"id": thesis_id},
            )
        )
        thesis["latest_version"] = _first(
            session.execute(
                text(
                    """SELECT version, claim, variant_perception, confidence,
                              trend_context, valuation_context, sentiment_context,
                              citation_map, rationale, changed_by, created_at
                       FROM investment_thesis_versions
                       WHERE thesis_id = CAST(:id AS UUID)
                       ORDER BY version DESC LIMIT 1"""
                ),
                {"id": thesis_id},
            )
        )
        thesis["scenarios"] = _rows(
            session.execute(
                text(
                    """SELECT id, name, description, probability,
                              expected_return, is_base_case, version, created_at
                       FROM investment_thesis_scenarios
                       WHERE thesis_id = CAST(:id AS UUID)
                         AND superseded_at IS NULL
                       ORDER BY is_base_case DESC, created_at, name
                       LIMIT :limit"""
                ),
                {"id": thesis_id, "limit": _MAX_TOURNAMENT_CHILDREN},
            )
        )
        thesis["forecasts"] = _rows(
            session.execute(
                text(
                    """SELECT id, scenario_id, forecast_key, forecast_type,
                              direction, target_value, target_date, as_of,
                              version, created_at
                       FROM investment_thesis_forecasts
                       WHERE thesis_id = CAST(:id AS UUID)
                         AND superseded_at IS NULL
                       ORDER BY as_of DESC, forecast_key
                       LIMIT :limit"""
                ),
                {"id": thesis_id, "limit": _MAX_TOURNAMENT_CHILDREN},
            )
        )
        theses.append(thesis)
    outcomes: list[dict[str, Any]] = []
    falsification_runs: list[dict[str, Any]] = []
    if thesis_ids:
        outcomes = _rows(
            session.execute(
                text(
                    """SELECT o.id, o.forecast_id, f.thesis_id, f.forecast_key,
                              o.status, o.actual_value, o.measured_at, o.notes,
                              o.created_at
                       FROM investment_forecast_outcomes o
                       JOIN investment_thesis_forecasts f ON f.id = o.forecast_id
                       JOIN investment_thesis_group_members m
                            ON m.thesis_id = f.thesis_id
                       WHERE m.group_id = CAST(:group_id AS UUID)
                         AND m.removed_at IS NULL
                       ORDER BY o.measured_at DESC, f.forecast_key
                       LIMIT :limit"""
                ),
                {"group_id": group_id, "limit": _MAX_TOURNAMENT_CHILDREN * 2},
            )
        )
        falsification_runs = _rows(
            session.execute(
                text(
                    """SELECT r.id, r.thesis_id, r.run_key, r.status,
                              r.started_at, r.completed_at, r.findings,
                              r.created_at
                       FROM investment_thesis_falsification_runs r
                       JOIN investment_thesis_group_members m
                            ON m.thesis_id = r.thesis_id
                       WHERE m.group_id = CAST(:group_id AS UUID)
                         AND m.removed_at IS NULL
                       ORDER BY r.started_at DESC, r.run_key
                       LIMIT :limit"""
                ),
                {"group_id": group_id, "limit": _MAX_TOURNAMENT_CHILDREN * 2},
            )
        )
    return {
        "group": {
            "id": str(group["id"]),
            "name": group["name"],
            "description": group["description"],
            "status": group["status"],
            "created_at": group["created_at"],
            "updated_at": group["updated_at"],
        },
        "theses": theses,
        "outcomes": outcomes,
        "falsification_runs": falsification_runs,
    }


__all__ = [
    "DIRECTIONS",
    "EVIDENCE_RELATIONSHIPS",
    "FALSIFICATION_STATUSES",
    "FORECAST_DIRECTIONS",
    "FORECAST_TYPES",
    "GROUP_STATUSES",
    "LINK_TYPES",
    "ORIGINS",
    "OUTCOME_STATUSES",
    "add_group_membership",
    "append_opportunity_snapshot",
    "attach_evidence",
    "canonical_thesis_key",
    "create_find_group",
    "evaluate_thesis",
    "freeze_forecast",
    "link_position",
    "list_ranked_opportunities",
    "list_thesis_groups",
    "load_group_tournament",
    "load_thesis_detail",
    "merge_or_create_thesis",
    "record_falsification_run",
    "record_forecast_outcome",
    "remove_group_membership",
    "thesis_desk_status",
    "unlink_position",
    "update_falsification_run",
    "upsert_scenario",
]
