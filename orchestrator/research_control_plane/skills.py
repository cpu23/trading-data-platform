"""Checked-in skill registry and deterministic production skill executors."""

from __future__ import annotations

import json
import math
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import text

from contracts.db_results import result_first, result_rows
from thesis_autonomy import (
    _close_at_or_before,
    _load_second_pass_snapshot,
    _persist_falsification,
    _target_boundary,
)
from thesis_challenges import challenge_thesis
from thesis_fusion import record_forecast_outcome

from .domain import canonical_json, content_fingerprint
from .repository import (
    complete_work_order,
    mark_work_order_running,
    record_effect_dependency,
)

MAX_EVIDENCE_REFS = 256
MAX_EFFECTS = 32


@dataclass(frozen=True, slots=True)
class SkillInput:
    work_order_id: uuid.UUID
    question_id: uuid.UUID
    question_type: str
    atomic_question: str
    target_kind: str
    target_ref: str
    accepted_cutoff: datetime
    skill_version_id: uuid.UUID
    skill_key: str
    skill_version: int
    skill_fingerprint: str


@dataclass(frozen=True, slots=True)
class SkillResult:
    status: Literal["resolved", "unresolved", "noop"]
    summary: str
    evidence_refs: tuple[str, ...] = ()
    source_families: tuple[str, ...] = ()
    effect_type: str = "justified_noop"
    material: bool = False
    detail: Mapping[str, Any] | None = None
    cost_usd: float = 0.0
    evidence_reused_count: int = 0
    evidence_acquired_count: int = 0
    justified_noop_reason: str | None = None

    def __post_init__(self) -> None:
        if len(self.summary.strip()) == 0 or len(self.summary) > 4000:
            raise ValueError("skill summary must be bounded and nonblank")
        if len(self.evidence_refs) > MAX_EVIDENCE_REFS:
            raise ValueError("too many evidence references")
        if not math.isfinite(self.cost_usd) or not 0 <= self.cost_usd <= 100:
            raise ValueError("skill cost must be finite and bounded")
        if self.material:
            if self.effect_type == "justified_noop" or self.justified_noop_reason:
                raise ValueError("material effects cannot be justified no-ops")
        elif self.effect_type != "justified_noop" or not self.justified_noop_reason:
            raise ValueError("non-material skill results require a justified no-op")

    def as_mapping(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "evidence_refs": list(self.evidence_refs),
            "source_families": list(self.source_families),
            "effects": [
                {
                    "effect_type": self.effect_type,
                    "material": self.material,
                    "target_changed": self.material,
                }
            ],
            "detail": dict(self.detail or {}),
            "cost_usd": self.cost_usd,
            "evidence_reused_count": self.evidence_reused_count,
            "evidence_acquired_count": self.evidence_acquired_count,
            "justified_noop_reason": self.justified_noop_reason,
        }




def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return _as_utc(parsed)
    raise ValueError("accepted_cutoff must be timezone-aware datetime")


def _bounded(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


@lru_cache(maxsize=1)
def load_skill_specs() -> tuple[Mapping[str, Any], ...]:
    path = Path(__file__).with_name("skill_specs.v1.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not 1 <= len(raw) <= 32:
        raise ValueError("skill specification file must contain a bounded list")
    required = {
        "skill_key",
        "version",
        "supported_question_types",
        "input_schema",
        "output_schema",
        "allowed_tools",
        "allowed_source_families",
        "point_in_time_requirements",
        "model_allowed",
        "model_policy",
        "maximum_cost_usd",
        "maximum_runtime_seconds",
        "maximum_attempts",
        "validators",
        "promotion_status",
    }
    output: list[Mapping[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != required:
            raise ValueError("skill specification has an invalid shape")
        identity = (str(item["skill_key"]), int(item["version"]))
        if identity not in _EXECUTORS:
            raise ValueError("skill specification has no exact-version executor")
        if (
            not item["supported_question_types"]
            or len(item["supported_question_types"]) > 32
        ):
            raise ValueError("skill question types must be bounded")
        if item["model_allowed"] is False and item["model_policy"] != {}:
            raise ValueError("model-disabled skill cannot declare a model policy")
        if not 0 <= float(item["maximum_cost_usd"]) <= 100:
            raise ValueError("skill maximum cost must be bounded")
        if not 1 <= int(item["maximum_runtime_seconds"]) <= 86400:
            raise ValueError("skill maximum runtime must be bounded")
        output.append(item)
    return tuple(output)


def ensure_skill_versions(session: Any) -> tuple[uuid.UUID, ...]:
    """Register exact immutable skill content; reject same-version drift."""
    registered: list[uuid.UUID] = []
    for spec in load_skill_specs():
        fingerprint = content_fingerprint(spec)
        params = {
            **spec,
            "input_schema": canonical_json(spec["input_schema"]),
            "output_schema": canonical_json(spec["output_schema"]),
            "point_in_time_requirements": canonical_json(
                spec["point_in_time_requirements"]
            ),
            "model_policy": canonical_json(spec["model_policy"]),
            "content_fingerprint": fingerprint,
        }
        row = result_first(session.execute(
            text(
                """
                INSERT INTO research_skill_versions (
                    skill_key, version, supported_question_types,
                    input_schema, output_schema, allowed_tools,
                    allowed_source_families, point_in_time_requirements,
                    model_allowed, model_policy, maximum_cost_usd,
                    maximum_runtime_seconds, maximum_attempts, validators,
                    promotion_status, content_fingerprint, promoted_at
                ) VALUES (
                    :skill_key, :version, :supported_question_types,
                    CAST(:input_schema AS JSONB), CAST(:output_schema AS JSONB),
                    :allowed_tools, :allowed_source_families,
                    CAST(:point_in_time_requirements AS JSONB),
                    :model_allowed, CAST(:model_policy AS JSONB),
                    :maximum_cost_usd, :maximum_runtime_seconds,
                    :maximum_attempts, :validators, :promotion_status,
                    :content_fingerprint,
                    CASE WHEN :promotion_status = 'draft' THEN NULL ELSE NOW() END
                )
                ON CONFLICT (skill_key, version) DO NOTHING
                RETURNING id, content_fingerprint
                """
            ),
            params,
        ))
        if row is None:
            row = result_first(session.execute(
                text(
                    """
                    SELECT id, content_fingerprint
                    FROM research_skill_versions
                    WHERE skill_key = :skill_key AND version = :version
                    """
                ),
                {"skill_key": spec["skill_key"], "version": spec["version"]},
            ))
        if row is None or row["content_fingerprint"] != fingerprint:
            raise ValueError(
                f"immutable skill version drift: {spec['skill_key']} v{spec['version']}"
            )
        registered.append(uuid.UUID(str(row["id"])))
    return tuple(registered)


def _target_context(session: Any, item: SkillInput) -> Mapping[str, Any]:
    if item.target_kind == "thesis":
        query = """
            SELECT id AS thesis_id, company, symbol, theme_id, direction
            FROM investment_theses
            WHERE id = CAST(:ref AS UUID)
              AND created_at <= :cutoff
              AND updated_at <= :cutoff
        """
    elif item.target_kind == "catalyst":
        query = """
            SELECT t.id AS thesis_id, t.company, t.symbol, t.theme_id, t.direction
            FROM investment_catalysts c
            JOIN investment_theses t ON t.id = c.thesis_id
            WHERE c.id = CAST(:ref AS UUID)
              AND c.created_at <= :cutoff
              AND t.created_at <= :cutoff
              AND t.updated_at <= :cutoff
        """
    elif item.target_kind == "forecast":
        query = """
            SELECT t.id AS thesis_id, t.company, t.symbol, t.theme_id, t.direction
            FROM investment_thesis_forecasts f
            JOIN investment_theses t ON t.id = f.thesis_id
            WHERE f.id = CAST(:ref AS UUID)
              AND f.created_at <= :cutoff
              AND f.as_of <= :cutoff
              AND (f.superseded_at IS NULL OR f.superseded_at > :cutoff)
              AND t.created_at <= :cutoff
              AND t.updated_at <= :cutoff
        """
    elif item.target_kind == "entity":
        return {
            "thesis_id": None,
            "company": item.target_ref,
            "symbol": item.target_ref,
            "theme_id": None,
            "direction": None,
        }
    else:
        return {
            "thesis_id": None,
            "company": None,
            "symbol": None,
            "theme_id": None,
            "direction": None,
        }
    return (
        result_first(session.execute(
            text(query),
            {"ref": item.target_ref, "cutoff": item.accepted_cutoff},
        ))
        or {}
    )


def _noop(
    reason: str, summary: str, *, detail: Mapping[str, Any] | None = None
) -> SkillResult:
    return SkillResult(
        status="unresolved",
        summary=_bounded(summary, 4000),
        effect_type="justified_noop",
        material=False,
        detail=detail,
        justified_noop_reason=_bounded(reason, 1000),
    )


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _threshold_sign(value: Any, *, threshold: float) -> int | None:
    number = _finite_number(value)
    if number is None:
        return None
    if number > threshold:
        return 1
    if number < -threshold:
        return -1
    return 0


def _option_skew_sign(options: list[Mapping[str, Any]]) -> int | None:
    """Return bearish/bullish/flat from the first valid put-call IV skew."""
    for row in options:
        analytics = row.get("analytics")
        if not isinstance(analytics, Mapping):
            continue
        expiries = analytics.get("expiries")
        if not isinstance(expiries, list):
            continue
        for expiry in expiries[:32]:
            if not isinstance(expiry, Mapping):
                continue
            skew = expiry.get("put_call_skew")
            if not isinstance(skew, Mapping) or skew.get("state") != "ok":
                continue
            sign = _threshold_sign(skew.get("value"), threshold=0.005)
            if sign is not None:
                # Positive put-minus-call skew is bearish positioning.
                return -sign
    return None


def _assess_materiality(
    *,
    policy_version: str,
    status: str,
    effect_type: str,
    detail: Mapping[str, Any] | None,
) -> bool:
    """Apply the deterministic v1 material-effect categories."""
    if policy_version != "v1":
        raise ValueError("unsupported materiality policy version")
    if status != "resolved":
        return False
    values = detail if isinstance(detail, Mapping) else {}
    if effect_type == "forecast":
        return True
    if effect_type == "falsification_state":
        return values.get("persisted") is True
    if effect_type != "core_evidence":
        return False
    if "divergence_detected" in values:
        return values.get("divergence_detected") is True
    deltas = values.get("deltas")
    if isinstance(deltas, list):
        return any(
            isinstance(delta, Mapping)
            and delta.get("change_kind") in {"new", "changed", "removed"}
            for delta in deltas
        )
    peers = values.get("peers")
    return isinstance(peers, list) and bool(peers)


def _filing_guidance_delta(session: Any, item: SkillInput) -> SkillResult:
    target = _target_context(session, item)
    rows = result_rows(session.execute(
        text(
            """
            SELECT d.document_id, d.content_sha256, d.company, d.symbol,
                   d.report_date, fd.category, fd.change_kind,
                   fd.excerpt, fd.previous_excerpt, fd.metrics
            FROM investment_filing_deltas fd
            JOIN investment_documents d ON d.document_id = fd.document_id
            WHERE fd.created_at <= :cutoff
              AND d.created_at <= :cutoff
              AND (d.report_date IS NULL OR d.report_date <= :cutoff_date)
              AND fd.category IN ('guidance', 'margins_cashflow', 'management_language')
              AND (
                  (:symbol IS NOT NULL AND UPPER(d.symbol) = UPPER(:symbol))
                  OR (:company IS NOT NULL AND LOWER(d.company) = LOWER(:company))
              )
            ORDER BY d.report_date DESC NULLS LAST, fd.created_at DESC,
                     fd.category, fd.id
            LIMIT 20
            """
        ),
        {
            "cutoff": item.accepted_cutoff,
            "cutoff_date": item.accepted_cutoff.date(),
            "symbol": target.get("symbol"),
            "company": target.get("company"),
        },
    ))
    if not rows:
        return _noop(
            "no_cutoff_safe_filing_delta",
            "No accepted filing guidance delta was available at the question cutoff.",
        )
    evidence_refs = tuple(f"filing:{row['content_sha256']}" for row in rows)
    changed = [row for row in rows if row.get("change_kind") != "unchanged"]
    details = [
        {
            "category": row.get("category"),
            "change_kind": row.get("change_kind"),
            "report_date": row.get("report_date"),
            "excerpt": _bounded(row.get("excerpt"), 1000) or None,
            "previous_excerpt": _bounded(row.get("previous_excerpt"), 1000) or None,
            "metrics": row.get("metrics")
            if isinstance(row.get("metrics"), Mapping)
            else {},
        }
        for row in rows
    ]
    if not changed:
        return SkillResult(
            status="noop",
            summary="Accepted filing deltas were unchanged; no research state update was justified.",
            evidence_refs=evidence_refs,
            source_families=("issuer_filing",),
            effect_type="justified_noop",
            material=False,
            detail={"deltas": details},
            evidence_reused_count=len(evidence_refs),
            justified_noop_reason="accepted_filing_deltas_unchanged",
        )
    return SkillResult(
        status="resolved",
        summary=f"Resolved from {len(changed)} changed accepted filing section(s).",
        evidence_refs=evidence_refs,
        source_families=("issuer_filing",),
        effect_type="core_evidence",
        material=True,
        detail={"deltas": details},
        evidence_reused_count=len(evidence_refs),
    )


def _filing_peer_readthrough(session: Any, item: SkillInput) -> SkillResult:
    target = _target_context(session, item)
    industry = result_first(session.execute(
        text(
            """
            SELECT industry FROM investment_documents
            WHERE created_at <= :cutoff
              AND (report_date IS NULL OR report_date <= :cutoff_date)
              AND ((:symbol IS NOT NULL AND UPPER(symbol) = UPPER(:symbol))
                   OR (:company IS NOT NULL AND LOWER(company) = LOWER(:company)))
            ORDER BY report_date DESC NULLS LAST, created_at DESC
            LIMIT 1
            """
        ),
        {
            "cutoff": item.accepted_cutoff,
            "cutoff_date": item.accepted_cutoff.date(),
            "symbol": target.get("symbol"),
            "company": target.get("company"),
        },
    ))
    if not industry:
        return _noop(
            "target_industry_unknown",
            "No cutoff-safe target industry was available for peer selection.",
        )
    rows = result_rows(session.execute(
        text(
            """
            SELECT d.content_sha256, d.company, d.symbol, d.report_date,
                   fd.category, fd.change_kind, fd.excerpt
            FROM investment_documents d
            JOIN investment_filing_deltas fd ON fd.document_id = d.document_id
            WHERE d.industry = :industry
              AND d.created_at <= :cutoff AND fd.created_at <= :cutoff
              AND (d.report_date IS NULL OR d.report_date <= :cutoff_date)
              AND fd.change_kind IN ('new', 'changed', 'removed')
              AND NOT (
                  (:symbol IS NOT NULL AND UPPER(d.symbol) = UPPER(:symbol))
                  OR (:company IS NOT NULL AND LOWER(d.company) = LOWER(:company))
              )
            ORDER BY d.report_date DESC NULLS LAST, d.company, fd.category
            LIMIT 20
            """
        ),
        {
            "industry": industry["industry"],
            "cutoff": item.accepted_cutoff,
            "cutoff_date": item.accepted_cutoff.date(),
            "symbol": target.get("symbol"),
            "company": target.get("company"),
        },
    ))
    if not rows:
        return _noop(
            "no_peer_filing_delta",
            "No changed same-industry peer filing delta was accepted at the cutoff.",
        )
    refs = tuple(f"filing:{row['content_sha256']}" for row in rows)
    peers = [
        {
            "company": row.get("company"),
            "symbol": row.get("symbol"),
            "report_date": row.get("report_date"),
            "category": row.get("category"),
            "change_kind": row.get("change_kind"),
            "excerpt": _bounded(row.get("excerpt"), 1000) or None,
        }
        for row in rows
    ]
    return SkillResult(
        status="resolved",
        summary=f"Resolved from {len(rows)} accepted same-industry peer filing delta(s).",
        evidence_refs=refs,
        source_families=("issuer_filing",),
        effect_type="core_evidence",
        material=True,
        detail={"industry": industry["industry"], "peers": peers},
        evidence_reused_count=len(refs),
    )


def _positioning_divergence(session: Any, item: SkillInput) -> SkillResult:
    target = _target_context(session, item)
    symbol = _bounded(target.get("symbol") or item.target_ref, 32).upper()
    options = result_rows(session.execute(
        text(
            """
            SELECT source, symbol, captured_at, feature_version, analytics
            FROM option_snapshot_features
            WHERE symbol = :symbol
              AND available_at <= :cutoff AND created_at <= :cutoff
            ORDER BY captured_at DESC, source
            LIMIT 2
            """
        ),
        {"symbol": symbol, "cutoff": item.accepted_cutoff},
    ))
    positioning = result_rows(session.execute(
        text(
            """
            SELECT source, market_id, report_date, category, long_positions,
                   short_positions, net_position, open_interest,
                   net_pct_open_interest
            FROM positioning_reports
            WHERE market_id = :symbol
              AND source IN ('cftc', 'finra_short_volume')
              AND report_date <= :cutoff_date
              AND acquired_at <= :cutoff
              AND created_at <= :cutoff
            ORDER BY report_date DESC, source, category
            LIMIT 10
            """
        ),
        {
            "symbol": symbol,
            "cutoff": item.accepted_cutoff,
            "cutoff_date": item.accepted_cutoff.date(),
        },
    ))
    prices = result_rows(session.execute(
        text(
            """
            (SELECT 'current' AS period, source, timestamp, close
             FROM market_data
             WHERE symbol = :symbol
               AND timeframe IN ('1d', 'daily', 'day')
               AND timestamp <= :cutoff AND created_at <= :cutoff
               AND close IS NOT NULL
             ORDER BY timestamp DESC
             LIMIT 1)
            UNION ALL
            (SELECT 'prior' AS period, source, timestamp, close
             FROM market_data
             WHERE symbol = :symbol
               AND timeframe IN ('1d', 'daily', 'day')
               AND timestamp <= :cutoff - INTERVAL '20 days'
               AND created_at <= :cutoff
               AND close IS NOT NULL
             ORDER BY timestamp DESC
             LIMIT 1)
            """
        ),
        {"symbol": symbol, "cutoff": item.accepted_cutoff},
    ))
    if not options and not positioning and not prices:
        return _noop(
            "no_cutoff_safe_positioning_data",
            "No accepted price, options, or positioning state was available at the cutoff.",
        )

    current_price = next(
        (
            _finite_number(row.get("close"))
            for row in prices
            if row.get("period") == "current"
        ),
        None,
    )
    prior_price = next(
        (
            _finite_number(row.get("close"))
            for row in prices
            if row.get("period") == "prior"
        ),
        None,
    )
    price_change = (
        None
        if current_price is None or prior_price in {None, 0.0}
        else (current_price - prior_price) / abs(prior_price)
    )
    reported_sign = None
    for row in positioning:
        reported_sign = _threshold_sign(row.get("net_pct_open_interest"), threshold=1.0)
        if reported_sign is None:
            reported_sign = _threshold_sign(row.get("net_position"), threshold=0.0)
        if reported_sign is not None:
            break
    direction_sign = {"long": 1, "short": -1, "neutral": 0}.get(
        str(target.get("direction") or "").casefold()
    )
    signals = {
        "thesis_direction": direction_sign,
        "price_trend_20d": _threshold_sign(price_change, threshold=0.01),
        "options_put_call_skew": _option_skew_sign(options),
        "reported_positioning": reported_sign,
    }
    nonzero_signs = {sign for sign in signals.values() if sign in {-1, 1}}
    divergence_detected = nonzero_signs == {-1, 1}
    refs = tuple(
        [
            f"options:{row['source']}:{row['symbol']}:{row['captured_at'].isoformat()}"
            for row in options
        ]
        + [
            f"positioning:{row['source']}:{row['market_id']}:{row['report_date']}:{row['category']}"
            for row in positioning
        ]
        + [
            f"market-price:{row['source']}:{symbol}:{row['timestamp'].isoformat()}"
            for row in prices
        ]
    )
    detail = {
        "signals": signals,
        "divergence_detected": divergence_detected,
        "price_change_20d": price_change,
        "options": [
            {
                "source": row.get("source"),
                "captured_at": row.get("captured_at"),
                "feature_version": row.get("feature_version"),
                "analytics": (
                    row.get("analytics")
                    if isinstance(row.get("analytics"), Mapping)
                    else {}
                ),
            }
            for row in options
        ],
        "reported_positioning": [dict(row) for row in positioning],
        "price_observations": [dict(row) for row in prices],
        "expectations_state": "unavailable",
        "measure_semantics": (
            "fundamentals, price, options, and reported positioning remain separate"
        ),
    }
    source_families = tuple(
        sorted(
            ({"options"} if options else set())
            | ({"market_price"} if prices else set())
            | {
                family
                for row in positioning
                for family in ("cftc", "finra")
                if family in str(row["source"]).casefold()
            }
        )
    )
    if not divergence_detected:
        return SkillResult(
            status="noop",
            summary=(
                "Accepted fundamentals, price, options, and reported positioning "
                "did not establish a directional disagreement."
            ),
            evidence_refs=refs,
            source_families=source_families,
            effect_type="justified_noop",
            material=False,
            detail=detail,
            evidence_reused_count=len(refs),
            justified_noop_reason="no_directional_positioning_divergence",
        )
    return SkillResult(
        status="resolved",
        summary=(
            "Accepted fundamentals, price, options, and reported positioning "
            "contained a deterministic directional disagreement."
        ),
        evidence_refs=refs,
        source_families=source_families,
        effect_type="core_evidence",
        material=True,
        detail=detail,
        evidence_reused_count=len(refs),
    )


def _targeted_challenge(session: Any, item: SkillInput) -> SkillResult:
    if item.target_kind != "thesis":
        return _noop(
            "challenge_target_not_thesis",
            "The targeted challenge requires a thesis target.",
        )
    row = result_first(session.execute(
        text(
            """
            SELECT id, claim, direction, status, invalidation_conditions,
                   opportunity_score, last_evaluated_at, updated_at,
                   fusion_reference_at
            FROM investment_theses
            WHERE id = CAST(:id AS UUID)
              AND created_at <= :reference
              AND updated_at <= :reference
              AND (last_evaluated_at IS NULL OR last_evaluated_at <= :reference)
              AND (fusion_reference_at IS NULL OR fusion_reference_at <= :reference)
            """
        ),
        {"id": item.target_ref, "reference": item.accepted_cutoff},
    ))
    if row is None:
        return _noop(
            "thesis_not_visible_at_cutoff",
            "The thesis state was not provably visible at the accepted cutoff.",
        )
    snapshot, evidence = _load_second_pass_snapshot(
        session,
        row,
        reference=item.accepted_cutoff,
        cost=0.0,
        cycle_key=f"research-skill:{item.work_order_id}",
    )
    decision = challenge_thesis(snapshot, evidence, runner=None)
    _, changed = _persist_falsification(
        session,
        item.target_ref,
        decision,
        run_key=f"research-skill:{item.work_order_id}",
        reference=item.accepted_cutoff,
    )
    refs = tuple(signal.evidence_id for signal in evidence)[:MAX_EVIDENCE_REFS]
    detail = {
        "state": decision.state,
        "contradiction_strength": decision.contradiction_strength,
        "required_data": list(decision.required_data),
        "citation_failures": [
            failure.to_dict() for failure in decision.citation_failures
        ],
        "invalidation_ids": list(decision.invalidation_ids),
        "snapshot_fingerprint": decision.snapshot_fingerprint,
        "persisted": changed,
    }
    if not changed:
        return SkillResult(
            status="noop",
            summary="The exact accepted thesis snapshot was already challenged; no duplicate run was created.",
            evidence_refs=refs,
            source_families=tuple(
                sorted(
                    {
                        signal.source_family
                        for signal in evidence
                        if signal.source_family
                    }
                )
            )[:32],
            effect_type="justified_noop",
            material=False,
            detail=detail,
            evidence_reused_count=len(refs),
            justified_noop_reason="exact_challenge_snapshot_already_persisted",
        )
    return SkillResult(
        status="resolved",
        summary=f"Independent deterministic challenge persisted with state {decision.state}.",
        evidence_refs=refs,
        source_families=tuple(
            sorted(
                {signal.source_family for signal in evidence if signal.source_family}
            )
        )[:32],
        effect_type="falsification_state",
        material=True,
        detail=detail,
        evidence_reused_count=len(refs),
    )


def _forecast_resolve(session: Any, item: SkillInput) -> SkillResult:
    if item.target_kind != "forecast":
        return _noop(
            "resolution_target_not_forecast",
            "Forecast resolution requires a forecast target.",
        )
    row = result_first(session.execute(
        text(
            """
            SELECT f.id, f.thesis_id, f.direction, f.target_value,
                   f.target_date, f.forecast_type, f.created_at, f.as_of,
                   t.symbol, o.status AS outcome_status,
                   o.actual_value AS outcome_actual_value,
                   o.measured_at AS outcome_measured_at
            FROM investment_thesis_forecasts f
            JOIN investment_theses t ON t.id = f.thesis_id
            LEFT JOIN investment_forecast_outcomes o
              ON o.forecast_id = f.id
             AND o.created_at <= :reference
             AND o.measured_at <= :reference
            WHERE f.id = CAST(:id AS UUID)
              AND f.created_at <= :reference AND f.as_of <= :reference
              AND (f.superseded_at IS NULL OR f.superseded_at > :reference)
              AND t.created_at <= :reference
              AND t.updated_at <= :reference
            """
        ),
        {"id": item.target_ref, "reference": item.accepted_cutoff},
    ))
    if row is None:
        return _noop(
            "forecast_not_visible_at_cutoff",
            "The forecast was not active and visible at the accepted cutoff.",
        )
    if row.get("outcome_status"):
        return SkillResult(
            status="noop",
            summary="The forecast outcome was already recorded; resolution replay was coalesced.",
            evidence_refs=(f"forecast-outcome:{item.target_ref}",),
            source_families=("market_price",),
            effect_type="justified_noop",
            material=False,
            detail={
                "status": row.get("outcome_status"),
                "actual_value": row.get("outcome_actual_value"),
                "measured_at": row.get("outcome_measured_at"),
            },
            evidence_reused_count=1,
            justified_noop_reason="forecast_outcome_already_recorded",
        )
    target_day = row.get("target_date")
    if isinstance(target_day, str):
        target_day = date.fromisoformat(target_day.split("T", 1)[0])
    if not isinstance(target_day, date) or item.accepted_cutoff.date() <= target_day:
        return _noop(
            "forecast_not_matured",
            "The forecast had not reached its target boundary at the accepted cutoff.",
        )
    if row.get("forecast_type") != "price":
        return _noop(
            "unsupported_forecast_type",
            "Only price forecasts have a deterministic production resolver.",
        )
    close = _close_at_or_before(
        session,
        row.get("symbol"),
        _target_boundary(target_day),
        available_at=item.accepted_cutoff,
    )
    target_value = row.get("target_value")
    try:
        target_number = float(target_value) if target_value is not None else None
    except (TypeError, ValueError, OverflowError):
        target_number = None
    if close is None or target_number is None:
        if item.accepted_cutoff.date() <= target_day + timedelta(days=7):
            return _noop(
                "forecast_price_within_grace",
                "No cutoff-safe terminal price was available within the forecast grace period.",
            )
        outcome = "inconclusive"
        actual = None
        notes = "no market price available within grace"
    else:
        direction = str(row.get("direction") or "up")
        if direction == "down":
            outcome = "hit" if close <= target_number else "miss"
        elif direction == "flat":
            outcome = "hit" if close == target_number else "miss"
        else:
            outcome = "hit" if close >= target_number else "miss"
        actual = close
        notes = "research skill outcome resolution"
    created = record_forecast_outcome(
        session,
        item.target_ref,
        status=outcome,
        actual_value=actual,
        measured_at=item.accepted_cutoff,
        notes=notes,
    )
    if not created:
        return _noop(
            "forecast_outcome_coalesced",
            "A concurrent forecast resolver recorded the terminal outcome first.",
        )
    ref = f"market-price:{row.get('symbol')}:{_target_boundary(target_day).isoformat()}"
    return SkillResult(
        status="resolved",
        summary=f"Forecast resolved deterministically as {outcome} at its target boundary.",
        evidence_refs=(ref,),
        source_families=("market_price",),
        effect_type="forecast",
        material=True,
        detail={
            "status": outcome,
            "actual_value": actual,
            "target_value": target_number,
            "target_date": target_day,
        },
        evidence_reused_count=1,
    )


_EXECUTORS: dict[tuple[str, int], Callable[[Any, SkillInput], SkillResult]] = {
    ("filing.earnings_guidance_delta", 1): _filing_guidance_delta,
    ("filing.peer_readthrough", 1): _filing_peer_readthrough,
    ("expectations.positioning_divergence", 1): _positioning_divergence,
    ("thesis.targeted_challenge", 1): _targeted_challenge,
    ("forecast.resolve", 1): _forecast_resolve,
}


def _load_work_order(
    session: Any, work_order_id: uuid.UUID
) -> Mapping[str, Any] | None:
    return result_first(session.execute(
        text(
            """
            SELECT w.id AS work_order_id, w.question_id, w.accepted_cutoff,
                   w.skill_version_id, w.attempt_count,
                   q.question_type, q.atomic_question,
                   q.target_kind, q.target_ref, q.status AS question_status,
                   q.expires_at AS question_expires_at,
                   s.skill_key, s.version, s.content_fingerprint,
                   s.supported_question_types, s.allowed_tools,
                   s.allowed_source_families, s.model_allowed,
                   s.maximum_cost_usd, s.maximum_runtime_seconds,
                   s.maximum_attempts
            FROM research_work_orders w
            JOIN research_questions q ON q.id = w.question_id
            JOIN research_skill_versions s ON s.id = w.skill_version_id
            WHERE w.id = :work_order_id
            """
        ),
        {"work_order_id": work_order_id},
    ))


def _record_effect(
    session: Any,
    *,
    item: SkillInput,
    result: SkillResult,
    runtime_ms: int,
    materiality_policy_version: str,
) -> None:
    before = content_fingerprint(
        {
            "target_kind": item.target_kind,
            "target_ref": item.target_ref,
            "accepted_cutoff": item.accepted_cutoff,
        }
    )
    after = (
        content_fingerprint({"before": before, "result": result.as_mapping()})
        if result.material
        else before
    )
    session.execute(
        text(
            """
            INSERT INTO research_effects (
                work_order_id, question_id, affected_target_kind,
                affected_target_ref, before_state_fingerprint,
                after_state_fingerprint, effect_type, material,
                materiality_policy_version, evidence_attached,
                source_families, scenario_changes, forecast_changes,
                status_changes, cost_usd, runtime_ms,
                evidence_reused_count, evidence_acquired_count,
                justified_noop_reason, skill_version_id, question_type,
                accepted_cutoff
            ) VALUES (
                :work_order_id, :question_id, :target_kind, :target_ref,
                :before_fingerprint, :after_fingerprint, :effect_type,
                :material, :materiality_policy_version, :evidence_attached,
                :source_families, '{}'::JSONB, CAST(:forecast_changes AS JSONB),
                CAST(:status_changes AS JSONB), :cost_usd, :runtime_ms,
                :evidence_reused_count, :evidence_acquired_count,
                :justified_noop_reason, :skill_version_id, :question_type,
                :accepted_cutoff
            )
            ON CONFLICT (work_order_id) DO NOTHING
            """
        ),
        {
            "work_order_id": item.work_order_id,
            "question_id": item.question_id,
            "target_kind": item.target_kind,
            "target_ref": item.target_ref,
            "before_fingerprint": before,
            "after_fingerprint": after,
            "effect_type": result.effect_type,
            "material": result.material,
            "materiality_policy_version": materiality_policy_version,
            "evidence_attached": list(result.evidence_refs),
            "source_families": list(result.source_families),
            "forecast_changes": canonical_json(result.detail or {})
            if result.effect_type == "forecast"
            else "{}",
            "status_changes": canonical_json({"result": result.status}),
            "cost_usd": result.cost_usd,
            "runtime_ms": max(0, min(int(runtime_ms), 86400000)),
            "evidence_reused_count": result.evidence_reused_count,
            "evidence_acquired_count": result.evidence_acquired_count,
            "justified_noop_reason": result.justified_noop_reason,
            "skill_version_id": item.skill_version_id,
            "question_type": item.question_type,
            "accepted_cutoff": item.accepted_cutoff,
        },
    )
    record_effect_dependency(
        session,
        work_order_id=item.work_order_id,
        question_id=item.question_id,
        target_kind=item.target_kind,
        target_ref=item.target_ref,
        accepted_cutoff=item.accepted_cutoff,
        effect_type=result.effect_type,
        material=result.material,
        resolved=result.status in {"resolved", "noop"},
    )
    if result.effect_type == "forecast" and item.target_kind == "forecast":
        session.execute(
            text(
                """
                INSERT INTO research_outcome_attributions (
                    forecast_outcome_id, work_order_id, skill_version_id,
                    question_type, source_families, horizon_context,
                    accepted_cutoff, outcome_status
                )
                SELECT o.id, :work_order_id, :skill_version_id,
                       :question_type, :source_families,
                       COALESCE(NULLIF(BTRIM(t.horizon), ''), 'unknown'),
                       :accepted_cutoff, o.status
                FROM investment_forecast_outcomes o
                JOIN investment_thesis_forecasts f ON f.id = o.forecast_id
                JOIN investment_theses t ON t.id = f.thesis_id
                WHERE o.forecast_id = CAST(:forecast_id AS UUID)
                ON CONFLICT (forecast_outcome_id, work_order_id) DO NOTHING
                """
            ),
            {
                "work_order_id": item.work_order_id,
                "skill_version_id": item.skill_version_id,
                "question_type": item.question_type,
                "source_families": list(result.source_families),
                "accepted_cutoff": item.accepted_cutoff,
                "forecast_id": item.target_ref,
            },
        )
    from ui_events import append_ui_invalidations

    append_ui_invalidations(
        session,
        {
            "research_questions",
            "research_work_orders",
            "research_effects",
            "research_control_plane",
            "system_topology",
        },
    )


_SOURCE_GAP_REASONS = frozenset(
    {
        "no_cutoff_safe_filing_delta",
        "target_industry_unknown",
        "no_peer_filing_delta",
        "no_cutoff_safe_positioning_data",
        "forecast_price_within_grace",
    }
)


def _update_source_gap(session: Any, *, item: SkillInput, result: SkillResult) -> None:
    reason = result.justified_noop_reason
    if result.status == "unresolved" and reason in _SOURCE_GAP_REASONS:
        fingerprint = content_fingerprint(
            {
                "question_type": item.question_type,
                "target_kind": item.target_kind,
                "target_ref": item.target_ref,
                "missing_capability": reason,
            }
        )
        session.execute(
            text(
                """
                INSERT INTO research_source_gaps (
                    fingerprint, question_type, target_kind, target_ref,
                    missing_capability, occurrence_count, first_observed_at,
                    last_observed_at, active, bounded_summary
                ) VALUES (
                    :fingerprint, :question_type, :target_kind, :target_ref,
                    :missing_capability, 1, :observed_at, :observed_at,
                    TRUE, :bounded_summary
                )
                ON CONFLICT (fingerprint) WHERE active DO UPDATE
                SET occurrence_count = LEAST(
                        research_source_gaps.occurrence_count + 1, 1000000
                    ),
                    last_observed_at = GREATEST(
                        research_source_gaps.last_observed_at,
                        EXCLUDED.last_observed_at
                    ),
                    bounded_summary = EXCLUDED.bounded_summary
                """
            ),
            {
                "fingerprint": fingerprint,
                "question_type": item.question_type,
                "target_kind": item.target_kind,
                "target_ref": item.target_ref,
                "missing_capability": reason,
                "observed_at": item.accepted_cutoff,
                "bounded_summary": result.summary,
            },
        )
    elif result.status in {"resolved", "noop"}:
        session.execute(
            text(
                """
                UPDATE research_source_gaps
                SET active = FALSE,
                    last_observed_at = GREATEST(last_observed_at, :observed_at)
                WHERE active
                  AND question_type = :question_type
                  AND target_kind = :target_kind
                  AND target_ref = :target_ref
                """
            ),
            {
                "observed_at": item.accepted_cutoff,
                "question_type": item.question_type,
                "target_kind": item.target_kind,
                "target_ref": item.target_ref,
            },
        )


def execute_work_order(
    session: Any,
    *,
    work_order_id: uuid.UUID,
    worker_id: str,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Execute one exact skill version and persist its effect atomically."""
    row = _load_work_order(session, work_order_id)
    if row is None:
        raise ValueError("unknown research work order")
    item = SkillInput(
        work_order_id=uuid.UUID(str(row["work_order_id"])),
        question_id=uuid.UUID(str(row["question_id"])),
        question_type=str(row["question_type"]),
        atomic_question=str(row["atomic_question"]),
        target_kind=str(row["target_kind"]),
        target_ref=str(row["target_ref"]),
        accepted_cutoff=_as_utc(row["accepted_cutoff"]),
        skill_version_id=uuid.UUID(str(row["skill_version_id"])),
        skill_key=str(row["skill_key"]),
        skill_version=int(row["version"]),
        skill_fingerprint=str(row["content_fingerprint"]),
    )
    skill_identity = (item.skill_key, item.skill_version)
    executor = _EXECUTORS.get(skill_identity)
    if executor is None:
        raise ValueError("exact research skill version executor is unavailable")
    spec = next(
        (
            candidate
            for candidate in load_skill_specs()
            if (
                str(candidate["skill_key"]),
                int(candidate["version"]),
            )
            == skill_identity
        ),
        None,
    )
    if spec is None or content_fingerprint(spec) != item.skill_fingerprint:
        raise ValueError("research skill version content is not inspectable")
    supported_question_types = {
        str(value) for value in (row.get("supported_question_types") or ())
    }
    question_expires_at = row.get("question_expires_at")
    if question_expires_at is not None and _as_utc(question_expires_at) <= datetime.now(
        UTC
    ):
        raise ValueError("research question expired before execution")
    if int(row.get("attempt_count") or 0) >= int(row["maximum_attempts"]):
        raise ValueError("research skill maximum attempts exhausted")
    if item.question_type not in supported_question_types:
        raise ValueError("research skill version does not support the question type")
    allowed_tools = {str(value) for value in (row.get("allowed_tools") or ())}
    if "postgresql" not in allowed_tools or row.get("model_allowed") is not False:
        raise ValueError("research skill execution policy is incompatible")
    if not mark_work_order_running(
        session, work_order_id=work_order_id, worker_id=worker_id
    ):
        completed = result_first(session.execute(
            text("SELECT status, result FROM research_work_orders WHERE id = :id"),
            {"id": work_order_id},
        ))
        if completed and completed.get("status") == "completed":
            existing = completed.get("result")
            return (
                dict(existing)
                if isinstance(existing, Mapping)
                else {"status": "completed"}
            )
        raise ValueError("research work order is not executable")
    started = time.monotonic()
    result = executor(session, item)
    runtime_ms = max(0, int((time.monotonic() - started) * 1000))
    maximum_cost = float(row["maximum_cost_usd"])
    maximum_runtime_ms = int(row["maximum_runtime_seconds"]) * 1000
    if result.cost_usd > maximum_cost:
        raise ValueError("research skill exceeded its declared cost ceiling")
    if runtime_ms > maximum_runtime_ms:
        raise ValueError("research skill exceeded its declared runtime ceiling")
    allowed_sources = {
        str(value) for value in (row.get("allowed_source_families") or ())
    }
    if "all_attached_point_in_time_evidence" not in allowed_sources and not set(
        result.source_families
    ).issubset(allowed_sources):
        raise ValueError("research skill returned an undeclared source family")
    settings = config.get("research_control_plane")
    settings = settings if isinstance(settings, Mapping) else {}
    materiality_policy_version = str(settings.get("materiality_policy_version", "v1"))
    expected_materiality = _assess_materiality(
        policy_version=materiality_policy_version,
        status=result.status,
        effect_type=result.effect_type,
        detail=result.detail,
    )
    if result.material is not expected_materiality:
        raise ValueError("research skill result violates materiality policy")
    _update_source_gap(session, item=item, result=result)
    _record_effect(
        session,
        item=item,
        result=result,
        runtime_ms=runtime_ms,
        materiality_policy_version=materiality_policy_version,
    )
    completion_status = complete_work_order(
        session,
        work_order_id=work_order_id,
        accepted_cutoff=item.accepted_cutoff,
        result=result.as_mapping(),
        material_effect_summary=result.summary,
        resolution_summary=result.summary,
        resolution_evidence_refs=result.evidence_refs,
    )
    return {
        "status": completion_status,
        "work_order_id": str(work_order_id),
        "skill_key": item.skill_key,
        "result_status": result.status,
        "material": result.material,
        "effect_type": result.effect_type,
        "cost_usd": result.cost_usd,
        "runtime_ms": runtime_ms,
        "evidence_reused_count": result.evidence_reused_count,
        "evidence_acquired_count": result.evidence_acquired_count,
    }


__all__ = [
    "SkillInput",
    "SkillResult",
    "ensure_skill_versions",
    "execute_work_order",
    "load_skill_specs",
]
