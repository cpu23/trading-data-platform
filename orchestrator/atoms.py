"""Reusable, evidence-linked analytical claims.

Atoms separate observation, interpretation, scenario, and unknowns.  Every
atom cites only validated evidence ids, carries a deterministic expiry, and
remains auditable after expiry or supersession.  All persistence uses the
caller's transaction and bounded allowlisted queries.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy import text

from contracts.db_results import result_first, result_rows

STATUSES = ("draft", "validated", "published", "superseded", "expired", "retracted")
CURRENT_STATUSES = ("draft", "validated", "published")
RELATIONSHIPS = ("supports", "contradicts", "context", "invalidation")
EVIDENCE_TYPES = (
    "macro_series",
    "market_data",
    "market_events",
    "econ_events",
    "story_cluster",
    "opinion",
)
_ATOM_COLUMNS = (
    "id",
    "subject_type",
    "subject_id",
    "claim_type",
    "claim",
    "observation_text",
    "interpretation_text",
    "scenario_text",
    "unknowns",
    "affected_assets",
    "time_horizon",
    "confidence",
    "confidence_components",
    "valid_from",
    "expires_at",
    "carry_forward",
    "invalidation_conditions",
    "status",
    "supersedes_atom_id",
    "source_event_id",
    "prompt_version",
    "model_slug",
    "generation_attempt_id",
    "input_fingerprint",
    "created_at",
    "published_at",
)
_MAX_LIST = 200


def _timestamp(value: Any, default: datetime | None = None) -> datetime | None:
    if value is None:
        return default
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return default
    else:
        return default
    if result.tzinfo is None or result.utcoffset() is None:
        return default
    return result.astimezone(UTC)


def _finite(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _bounded_text(value: Any, maximum: int) -> str | None:
    text_value = str(value or "").strip()
    return text_value[:maximum] if text_value else None




def _settings(config: Any) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    value = config.get("analysis_atoms", {})
    return value if isinstance(value, Mapping) else {}


def session_close_target(now: datetime, settings: Mapping[str, Any]) -> datetime:
    close = time(21, 0)
    raw = settings.get("intraday_session_close", "21:00:00")
    if isinstance(raw, str):
        try:
            close = time.fromisoformat(raw)
        except ValueError:
            pass
    target = datetime.combine(now.date(), close, tzinfo=UTC)
    return target if target > now else target + timedelta(days=1)


def validate_evidence(
    session: Any, evidence: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve bounded evidence rows; unknown ids or types are rejected."""
    resolved: list[dict[str, Any]] = []
    errors: list[str] = []
    for item in evidence[:50]:
        evidence_type = str(item.get("evidence_type") or "").strip().lower()
        evidence_id = str(item.get("evidence_id") or "").strip()
        relationship = str(item.get("relationship") or "").strip().lower()
        if evidence_type not in EVIDENCE_TYPES:
            errors.append(f"unsupported_evidence_type:{evidence_type[:32]}")
            continue
        if relationship not in RELATIONSHIPS:
            errors.append(f"unsupported_relationship:{relationship[:32]}")
            continue
        if not evidence_id or len(evidence_id) > 200:
            errors.append("invalid_evidence_id")
            continue
        row = _resolve_evidence_row(session, evidence_type, evidence_id)
        if row is None:
            errors.append(f"unknown_evidence:{evidence_type}:{evidence_id[:64]}")
            continue
        resolved.append(
            {
                "evidence_type": evidence_type,
                "evidence_id": evidence_id,
                "relationship": relationship,
                "excerpt": _bounded_text(item.get("excerpt"), 500),
                "source_timestamp": row["source_timestamp"],
            }
        )
    return resolved, errors


def _resolve_evidence_row(
    session: Any, evidence_type: str, evidence_id: str
) -> dict[str, Any] | None:
    queries = {
        "macro_series": (
            "SELECT observed_at AS source_timestamp FROM macro_series "
            "WHERE series_id = :id ORDER BY observed_at DESC LIMIT 1"
        ),
        "market_data": (
            "SELECT timestamp AS source_timestamp FROM market_data "
            "WHERE symbol = :id AND timeframe = 'PRICE' "
            "ORDER BY timestamp DESC LIMIT 1"
        ),
        "market_events": (
            "SELECT COALESCE(published_at, effective_at, observed_at) "
            "AS source_timestamp FROM market_events WHERE id = CAST(:id AS UUID) LIMIT 1"
        ),
        "econ_events": (
            "SELECT scheduled_at AS source_timestamp FROM econ_events "
            "WHERE event_id = :id LIMIT 1"
        ),
        "story_cluster": (
            "SELECT last_seen_at AS source_timestamp FROM story_clusters "
            "WHERE id = CAST(:id AS UUID) LIMIT 1"
        ),
        "opinion": (
            "SELECT created_at AS source_timestamp FROM structured_opinions "
            "WHERE opinion_id = CAST(:id AS UUID) LIMIT 1"
        ),
    }
    return result_first(session.execute(text(queries[evidence_type]), {"id": evidence_id}))


def publish_atom(
    session: Any,
    atom: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
    *,
    status: str = "published",
    now: Any = None,
) -> dict[str, Any]:
    """Validate and persist one atom with evidence; supersede the prior atom."""
    current = _timestamp(now, datetime.now(UTC)) or datetime.now(UTC)
    if status not in STATUSES:
        raise ValueError("unsupported atom status")
    subject_type = _bounded_text(atom.get("subject_type"), 40)
    subject_id = _bounded_text(atom.get("subject_id"), 120)
    claim_type = _bounded_text(atom.get("claim_type"), 40)
    claim = _bounded_text(atom.get("claim"), 2000)
    fingerprint = str(atom.get("input_fingerprint") or "").strip()
    if not (subject_type and subject_id and claim_type and claim):
        raise ValueError(
            "atom subject, claim type, claim, and fingerprint are required"
        )
    if len(fingerprint) != 64:
        raise ValueError("atom input fingerprint must be a 64-char digest")
    resolved, errors = validate_evidence(session, evidence)
    if errors:
        raise ValueError("atom evidence validation failed: " + ";".join(errors[:10]))
    confidence = min(1.0, max(0.0, _finite(atom.get("confidence"), 0.0)))
    valid_from = _timestamp(atom.get("valid_from"), current) or current
    expires_at = _timestamp(atom.get("expires_at"))
    prior = None
    supersedes = atom.get("supersedes_atom_id")
    if supersedes is not None:
        prior = result_first(session.execute(
            text(
                "SELECT id, status FROM analysis_atoms WHERE id = CAST(:id AS UUID) LIMIT 1"
            ),
            {"id": str(supersedes)},
        ))
        if prior is None:
            raise ValueError("superseded atom does not exist")
    inserted = result_first(session.execute(
        text(
            """INSERT INTO analysis_atoms
        (subject_type, subject_id, claim_type, claim, observation_text,
         interpretation_text, scenario_text, unknowns, affected_assets,
         time_horizon, confidence, confidence_components, valid_from,
         expires_at, carry_forward, invalidation_conditions, status,
         supersedes_atom_id, source_event_id, prompt_version, model_slug,
         generation_attempt_id, input_fingerprint, published_at)
        VALUES (:subject_type, :subject_id, :claim_type, :claim,
         :observation_text, :interpretation_text, :scenario_text,
         CAST(:unknowns AS TEXT[]), CAST(:affected_assets AS JSONB),
         :time_horizon, :confidence, CAST(:confidence_components AS JSONB),
         :valid_from, :expires_at, :carry_forward,
         CAST(:invalidation_conditions AS JSONB), :status,
         CAST(:supersedes_atom_id AS UUID), CAST(:source_event_id AS UUID),
         :prompt_version, :model_slug, CAST(:generation_attempt_id AS UUID),
         :input_fingerprint, :published_at)
        ON CONFLICT DO NOTHING RETURNING *"""
        ),
        {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "claim_type": claim_type,
            "claim": claim,
            "observation_text": _bounded_text(atom.get("observation_text"), 4000),
            "interpretation_text": _bounded_text(
                atom.get("interpretation_text"), 4000
            ),
            "scenario_text": _bounded_text(atom.get("scenario_text"), 4000),
            "unknowns": [
                str(item).strip()[:200]
                for item in (atom.get("unknowns") or [])[:20]
                if str(item).strip()
            ],
            "affected_assets": _json_text(
                [
                    str(item).strip()[:32]
                    for item in (atom.get("affected_assets") or [])[:50]
                    if str(item).strip()
                ]
            ),
            "time_horizon": _bounded_text(atom.get("time_horizon"), 40)
            or "unspecified",
            "confidence": confidence,
            "confidence_components": _json_text(
                dict(atom.get("confidence_components") or {})
            ),
            "valid_from": valid_from,
            "expires_at": expires_at,
            "carry_forward": bool(atom.get("carry_forward", False)),
            "invalidation_conditions": _json_text(
                [
                    str(item).strip()[:500]
                    for item in (atom.get("invalidation_conditions") or [])[:20]
                    if str(item).strip()
                ]
            ),
            "status": status,
            "supersedes_atom_id": str(prior["id"]) if prior else None,
            "source_event_id": atom.get("source_event_id"),
            "prompt_version": _bounded_text(atom.get("prompt_version"), 80),
            "model_slug": _bounded_text(atom.get("model_slug"), 120),
            "generation_attempt_id": atom.get("generation_attempt_id"),
            "input_fingerprint": fingerprint,
            "published_at": current if status == "published" else None,
        },
    ))
    if inserted is None:
        return {"status": "duplicate", "atom_id": None, "evidence": 0}
    for item in resolved:
        session.execute(
            text(
                """INSERT INTO analysis_atom_evidence
            (atom_id, evidence_type, evidence_id, relationship, excerpt, source_timestamp)
            VALUES (:atom_id, :evidence_type, :evidence_id, :relationship,
                    :excerpt, :source_timestamp)
            ON CONFLICT DO NOTHING"""
            ),
            {"atom_id": inserted["id"], **item},
        )
    if prior is not None and prior["status"] in CURRENT_STATUSES:
        session.execute(
            text(
                "UPDATE analysis_atoms SET status = 'superseded', updated_at = :now "
                "WHERE id = :id AND status IN ('draft', 'validated', 'published')"
            ),
            {"id": prior["id"], "now": current},
        )
    return {"status": status, "atom_id": inserted["id"], "evidence": len(resolved)}


def expire_atoms(session: Any, config: Any = None, now: Any = None) -> dict[str, int]:
    """Deterministically expire atoms whose horizon or revision passed."""
    settings = _settings(config)
    current = _timestamp(now, datetime.now(UTC)) or datetime.now(UTC)
    try:
        limit = max(1, min(_MAX_LIST, int(settings.get("expire_limit", 100))))
        interpretation_hours = max(
            1, min(168, int(settings.get("event_interpretation_hours", 48)))
        )
        regime_hours = max(1, min(720, int(settings.get("regime_hours", 168))))
    except (TypeError, ValueError, OverflowError):
        limit, interpretation_hours, regime_hours = 100, 48, 168
    session_close = session_close_target(current, settings)
    expired = 0

    def expire(where: str, params: dict[str, Any]) -> int:
        nonlocal expired
        rows = result_rows(session.execute(
            text(
                f"SELECT id FROM analysis_atoms WHERE {where} "
                "ORDER BY valid_from DESC LIMIT :limit"
            ),
            {**params, "limit": limit},
        ))
        for row in rows:
            session.execute(
                text(
                    "UPDATE analysis_atoms SET status = 'expired', updated_at = :now "
                    "WHERE id = :id AND status IN ('draft', 'validated', 'published')"
                ),
                {"id": row["id"], "now": current},
            )
            expired += 1
        return len(rows)

    expire(
        "expires_at IS NOT NULL AND expires_at <= :now AND NOT carry_forward "
        "AND status IN ('draft', 'validated', 'published')",
        {"now": current},
    )
    expire(
        "claim_type = 'intraday' AND expires_at IS NULL AND NOT carry_forward "
        "AND valid_from < :close AND status IN ('draft', 'validated', 'published')",
        {"close": session_close},
    )
    expire(
        "claim_type = 'event_interpretation' AND source_event_id IS NOT NULL "
        "AND valid_from <= :cutoff AND status IN ('draft', 'validated', 'published')",
        {"cutoff": current - timedelta(hours=interpretation_hours)},
    )
    expire(
        "claim_type = 'regime' AND valid_from <= :cutoff "
        "AND status IN ('draft', 'validated', 'published')",
        {"cutoff": current - timedelta(hours=regime_hours)},
    )
    return {"expired": expired}


def current_atoms(
    session: Any,
    *,
    subject_type: str | None = None,
    subject_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    bounded = max(1, min(_MAX_LIST, int(limit)))
    return result_rows(session.execute(
        text(
            """SELECT a.*, e.evidence FROM analysis_atoms a
        LEFT JOIN LATERAL (
          SELECT jsonb_agg(to_jsonb(evidence_row)) AS evidence
          FROM (
            SELECT evidence_type, evidence_id, relationship, excerpt,
                   source_timestamp
            FROM analysis_atom_evidence
            WHERE atom_id = a.id
            ORDER BY evidence_type, evidence_id, relationship
            LIMIT 20
          ) evidence_row
        ) e ON TRUE
        WHERE a.status IN ('validated', 'published')
          AND (:subject_type IS NULL OR a.subject_type = :subject_type)
          AND (:subject_id IS NULL OR a.subject_id = :subject_id)
        ORDER BY a.valid_from DESC, a.id DESC LIMIT :limit"""
        ),
        {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "limit": bounded,
        },
    ))


def atom_history(
    session: Any, subject_type: str, subject_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    """Bounded audit history including superseded and expired atoms."""
    bounded = max(1, min(_MAX_LIST, int(limit)))
    return result_rows(session.execute(
        text(
            """SELECT id, subject_type, subject_id, claim_type, claim, status,
            confidence, valid_from, expires_at, supersedes_atom_id,
            source_event_id, model_slug, prompt_version, input_fingerprint,
            created_at, published_at, updated_at
            FROM analysis_atoms
            WHERE subject_type = :subject_type AND subject_id = :subject_id
            ORDER BY valid_from DESC, created_at DESC LIMIT :limit"""
        ),
        {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "limit": bounded,
        },
    ))


def assemble_atom_context(
    session: Any, config: Any = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Current atoms for report assembly; identical sets reuse prior reports."""
    rows = current_atoms(session, limit=limit)
    for row in rows:
        row["evidence"] = row.get("evidence") or []
    return rows


__all__ = [
    "CURRENT_STATUSES",
    "EVIDENCE_TYPES",
    "RELATIONSHIPS",
    "STATUSES",
    "assemble_atom_context",
    "atom_history",
    "current_atoms",
    "expire_atoms",
    "publish_atom",
    "session_close_target",
    "validate_evidence",
]
